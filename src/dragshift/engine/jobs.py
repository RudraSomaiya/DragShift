"""JobManager: runs Ops in a lazy worker pool and reports back on the Qt thread (PRD §9.2, §9.4).

- The pool (2 spawn workers) is created on the first worker job and shut down after
  ``IDLE_SHUTDOWN_MS`` with no jobs (NFR-1). Progress comes back through a
  ``multiprocessing.SimpleQueue`` handed to each worker at start-up; cancel flags are empty files
  in ``CANCEL_DIR``. No Manager process, which would block the Qt thread for ~300 ms on every
  pool start.
- Workers write ``*.part`` files; on success they are renamed to their final names here.
- A Qt timer polls futures and drains progress while jobs run, so signals fire on the Qt thread.
"""

import multiprocessing
import os
import queue
import tempfile
import time
import uuid
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from dragshift.engine import registry
from dragshift.engine.worker import (
    PART,
    CancelDir,
    JobContext,
    final_path,
    free_path,
    init_worker,
    run_job,
)

__all__ = ["JobContext", "JobManager", "JobResult"]


@dataclass
class JobResult:
    op_id: str
    inputs: list[Path]
    outputs: list[Path]
    ok: bool
    message: str
    undo: dict


@dataclass
class _Job:
    op_id: str
    inputs: list[Path]
    future: Future
    pool: ProcessPoolExecutor | None  # the pool it runs in; None for "ui" ops


class JobManager(QObject):
    progressed = Signal(str, float, str)  # job_id, fraction, text
    finished = Signal(str, object)  # job_id, JobResult

    IDLE_SHUTDOWN_MS = 60_000
    POLL_MS = 50
    MAX_WORKERS = 2
    # One fixed folder (not a new temp dir per pool start), so a crashed UI leaks nothing new.
    CANCEL_DIR = Path(
        os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(), "DragShift", "cancel"
    )
    STALE_FLAG_S = 3600  # flags left by jobs abandoned on quit are pruned after this

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._jobs: dict[str, _Job] = {}
        self._pool: ProcessPoolExecutor | None = None
        self._queue: Any = None  # multiprocessing.SimpleQueue while the pool lives
        self._cancel_dir = CancelDir(self.CANCEL_DIR)
        self._local_queue: queue.SimpleQueue = queue.SimpleQueue()  # progress of "ui" ops
        self._local_cancel: dict[str, bool] = {}
        self._poll = QTimer(self)
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self._tick)
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self._shutdown_if_idle)

    @property
    def pool_active(self) -> bool:
        return self._pool is not None

    def submit(self, op_id: str, paths: list[Path], params: dict | None = None) -> str:
        """Start ``op_id`` on ``paths``. Raises ``KeyError`` for an unknown op and
        ``ValueError`` when the op does not accept these files."""
        op = registry.get(op_id)
        paths = [Path(p) for p in paths]
        if not registry.accepts(op, paths):
            raise ValueError(f"{op_id} does not accept {[p.name for p in paths]}")
        groups = [[p] for p in paths] if op.multi == "each" else [paths]
        job_id = uuid.uuid4().hex
        params = dict(params or {})
        if op.where == "worker":
            try:
                future = self._submit_to_pool(job_id, op.func, groups, params)
            except BrokenProcessPool:  # a worker died since the last tick (NFR-9)
                self._stop_pool()
                future = self._submit_to_pool(job_id, op.func, groups, params)
            pool = self._pool
        else:
            # "ui" ops (copy path, …) run on the Qt thread, but still after submit() returns.
            ctx = JobContext(job_id, self._local_queue, self._local_cancel)
            future = Future()

            def run_inline() -> None:
                if future.set_running_or_notify_cancel():
                    future.set_result(run_job(op.func, groups, params, ctx))

            QTimer.singleShot(0, run_inline)
            pool = None
        self._jobs[job_id] = _Job(op.id, paths, future, pool)
        self._idle.stop()
        self._poll.start()
        return job_id

    def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.future.cancel():
            return  # finished already, or never started
        if job.pool is None:
            self._local_cancel[job_id] = True
        else:
            self._cancel_dir.set(job_id)  # the worker sees it through ctx.cancelled()

    def undo(self, job_id: str) -> None:
        raise NotImplementedError("Undo lands in DS-18 (FR-5.6)")

    def shutdown(self) -> None:
        """Stop the pool now; call on app quit. Running worker jobs are cancelled, so they
        delete their ``.part`` files instead of leaving them next to the sources."""
        self._poll.stop()
        self._idle.stop()
        for job_id, job in self._jobs.items():
            if job.pool is not None and not job.future.cancel():
                self._cancel_dir.set(job_id)  # left in place: the worker may outlive us
        self._jobs.clear()
        self._stop_pool()

    # --- internals ---------------------------------------------------------------------------
    def _stop_pool(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None
        if self._queue is not None:
            self._queue.close()
            self._queue = None

    def _ensure_pool(self) -> None:
        if self._pool is not None:
            return
        self._prepare_cancel_dir()
        mp = multiprocessing.get_context("spawn")
        self._queue = mp.SimpleQueue()
        self._pool = ProcessPoolExecutor(
            max_workers=self.MAX_WORKERS,
            mp_context=mp,
            initializer=init_worker,
            initargs=(self._queue,),
        )

    def _submit_to_pool(
        self, job_id: str, func: str, groups: list[list[Path]], params: dict
    ) -> Future:
        self._ensure_pool()
        ctx = JobContext(job_id, None, self._cancel_dir)  # None: the worker's own queue
        return self._pool.submit(run_job, func, groups, params, ctx)

    def _prepare_cancel_dir(self) -> None:
        """Create CANCEL_DIR and prune flags older than STALE_FLAG_S (jobs abandoned on quit)."""
        folder = self._cancel_dir.path
        folder.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - self.STALE_FLAG_S
        for flag in folder.iterdir():
            try:
                if flag.stat().st_mtime < cutoff:
                    flag.unlink()
            except OSError:
                pass

    def _shutdown_if_idle(self) -> None:
        if not self._jobs:
            self.shutdown()

    def _drain_progress(self) -> None:
        items = []
        while True:
            try:
                items.append(self._local_queue.get_nowait())
            except queue.Empty:
                break
        while self._queue is not None and not self._queue.empty():
            items.append(self._queue.get())
        for job_id, fraction, text in items:
            if job_id in self._jobs:
                self.progressed.emit(job_id, fraction, text)

    def _tick(self) -> None:
        # Snapshot finished jobs *before* draining: a worker's progress is written to the pipe
        # before its result is sent, so this drain sees all of it before `finished` is emitted.
        done = [(job_id, job) for job_id, job in self._jobs.items() if job.future.done()]
        self._drain_progress()
        for job_id, job in done:
            del self._jobs[job_id]
            result = self._result(job)
            if job.pool is not None:
                self._cancel_dir.clear(job_id)
            self._local_cancel.pop(job_id, None)
            self.finished.emit(job_id, result)
        if not self._jobs:
            self._poll.stop()
            if self._pool is not None:
                self._idle.start(self.IDLE_SHUTDOWN_MS)

    def _result(self, job: _Job) -> JobResult:
        def fail(message: str) -> JobResult:
            return JobResult(job.op_id, job.inputs, [], False, message, {})

        if job.future.cancelled():
            return fail("Cancelled")
        try:
            raw = job.future.result()
        except BrokenProcessPool:
            # A worker died (NFR-9). Drop that pool; the next job creates a fresh one. A newer
            # pool built since then is left alone.
            if job.pool is self._pool:
                self._stop_pool()
            return fail("The worker process crashed")
        except Exception as e:  # noqa: BLE001 - e.g. pickling; run_job catches the rest
            return fail(f"{type(e).__name__}: {e}")

        # Partial results are kept (FR-4.2): finalize every output the worker produced.
        outputs: list[Path] = []
        messages = [raw["message"]] if raw["message"] else []
        for part in raw["outputs"]:
            try:
                outputs.append(self._finalize(part))
            except OSError as e:
                Path(part).unlink(missing_ok=True)
                messages.append(f"Could not save {final_path(Path(part)).name}: {e}")
        ok = raw["ok"] and not messages
        undo = {"type": "trash", "paths": outputs} if outputs else {}
        return JobResult(job.op_id, job.inputs, outputs, ok, "; ".join(messages), undo)

    @staticmethod
    def _finalize(part: Path) -> Path:
        """Rename ``x.jpg.part`` → ``x.jpg``, never over an existing file (FR-5.2, NFR-4)."""
        if part.suffix != PART:
            return part
        while True:
            target = free_path(final_path(part))
            try:
                part.rename(target)  # on Windows rename fails rather than replace
                return target
            except FileExistsError:
                continue  # someone created it in between; pick the next free name
