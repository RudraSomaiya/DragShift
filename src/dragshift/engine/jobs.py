"""JobManager: runs Ops in a lazy worker pool and reports back on the Qt thread (PRD §9.2, §9.4).

- The pool (2 spawn workers) is created on the first worker job and shut down after
  ``IDLE_SHUTDOWN_MS`` with no jobs (NFR-1). Progress comes back through a
  ``multiprocessing.SimpleQueue`` handed to each worker at start-up; cancel flags are empty files
  in ``CANCEL_DIR``. No Manager process, which would block the Qt thread for ~300 ms on every
  pool start.
- Workers write ``*.part`` files; on success they are renamed to their final names here.
- A Qt timer polls futures and drains progress while jobs run, so signals fire on the Qt thread.
- Undo (FR-5.6) is ``apply_undo`` on a ``JobResult.undo``; ``History.undo`` is its caller, so a
  job is undone once and from the toast and History alike. It sends outputs to the Recycle Bin
  from the UI process: ``send2trash`` is a thin shell API wrapper, imported only when needed.
  Move to is undone by moving the files back.
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
    CANCELLED,
    CancelDir,
    JobContext,
    final_path,
    finalize,
    init_worker,
    move_path,
    run_job,
)

__all__ = [
    "CANCELLED",
    "OUTPUT_FOLDER",
    "JobContext",
    "JobManager",
    "JobResult",
    "apply_undo",
    "stamp",
]

REDIRECTED = "Saved to Downloads: the source folder is read-only"  # FR-5.3 toast note
OUTPUT_FOLDER = "output_folder"  # submit() param: Settings > Output > one folder (FR-8.1)


def _trash(paths: list[Path]) -> None:
    """Send ``paths`` to the Recycle Bin. Refuses drives that have none: there the shell
    would delete them for good, without asking."""
    from dragshift.platform.files import has_recycle_bin

    no_bin = [p for p in paths if not has_recycle_bin(p)]
    if no_bin:
        raise OSError(f"{no_bin[0].anchor} has no Recycle Bin. Delete the file yourself instead.")
    from send2trash import send2trash  # only loaded when Undo is used

    send2trash(paths)


def stamp(path: Path) -> list[int]:
    """[size, mtime_ns]: how Undo recognises an output it wrote (JSON-friendly)."""
    st = path.stat()
    return [st.st_size, st.st_mtime_ns]


def _move_back(undo: dict) -> list[Path]:
    """Undo Move to: each moved item goes back where it was. One that changed since the move,
    or whose old place is taken again, stays where it is: those are returned."""
    moves = [(Path(a), Path(b)) for a, b in undo.get("moves", ())]
    stamps = undo.get("stamps") or [None] * len(moves)
    kept: list[Path] = []
    for (origin, moved), known in zip(moves, stamps, strict=True):
        if not moved.exists():
            continue
        if origin.exists() or (known is not None and stamp(moved) != list(known)):
            kept.append(moved)
            continue
        move_path(moved, origin)
    return kept


def apply_undo(undo: dict) -> list[Path]:
    """Reverse a job from its ``JobResult.undo`` (FR-5.6). ``"trash"`` sends the outputs that
    still exist to the Recycle Bin, then removes the output folders the job made once they are
    empty. An output whose size or modification time changed since the job (``stamps``) is not
    the file the job wrote any more, so it is kept: those are returned. ``"move"`` moves the
    items of a Move to job back.

    Raises ``ValueError`` for an undo type it does not know and ``OSError`` if the Recycle Bin
    refuses."""
    kind = undo.get("type")
    if kind == "move":
        return _move_back(undo)
    if kind != "trash":
        raise ValueError(f"cannot undo {kind!r}")
    paths = [Path(p) for p in undo.get("paths", ())]
    stamps = undo.get("stamps") or [None] * len(paths)
    trash: list[Path] = []
    kept: list[Path] = []
    for path, known in zip(paths, stamps, strict=True):
        if not path.exists():
            continue
        (kept if known is not None and stamp(path) != list(known) else trash).append(path)
    if trash:
        _trash(trash)
    for folder in reversed([Path(d) for d in undo.get("dirs", ())]):  # deepest first
        try:
            folder.rmdir()  # only succeeds when empty: anything the user added stays
        except OSError:
            pass
    return kept


@dataclass
class JobResult:
    op_id: str
    inputs: list[Path]
    outputs: list[Path]
    ok: bool
    message: str  # the failure, or a note on a successful job (it went to Downloads, FR-5.3)
    undo: dict
    cancelled: bool = False  # cancelled by the user or on quit: no toast, no history
    copied: str = ""  # text the job put on the clipboard (Copy path, SHA-256)


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
        # Settings > Output > one folder (FR-8.1): for JobContext, not the op's own params.
        folder = params.pop(OUTPUT_FOLDER, None)
        folder = Path(folder) if folder else None
        if op.where == "worker":
            try:
                future = self._submit_to_pool(job_id, op.func, groups, params, folder)
            except BrokenProcessPool:  # a worker died since the last tick (NFR-9)
                self._stop_pool()
                future = self._submit_to_pool(job_id, op.func, groups, params, folder)
            pool = self._pool
        else:
            # "ui" ops (copy path, …) run on the Qt thread, but still after submit() returns.
            ctx = JobContext(job_id, self._local_queue, self._local_cancel, folder)
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
        self,
        job_id: str,
        func: str,
        groups: list[list[Path]],
        params: dict,
        folder: Path | None = None,
    ) -> Future:
        self._ensure_pool()
        ctx = JobContext(job_id, None, self._cancel_dir, folder)  # None: the worker's own queue
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
        def fail(message: str, cancelled: bool = False) -> JobResult:
            return JobResult(job.op_id, job.inputs, [], False, message, {}, cancelled)

        if job.future.cancelled():
            return fail(CANCELLED, cancelled=True)
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
        if raw.get("cancelled"):
            return fail(raw["message"], cancelled=True)

        # Partial results are kept (FR-4.2): finalize every output the worker produced.
        outputs: list[Path] = []
        messages = [raw["message"]] if raw["message"] else []
        final = raw.get("final", ())
        for part in raw["outputs"]:
            if part in final:  # renamed by the op itself (Copy to, Move to)
                outputs.append(part)
                continue
            try:
                outputs.append(finalize(part))
            except OSError as e:
                Path(part).unlink(missing_ok=True)
                messages.append(f"Could not save {final_path(Path(part)).name}: {e}")
        ok = raw["ok"] and not messages
        if outputs:
            messages += raw.get("notes", ())  # FR-4.4 "Couldn't reach 10 MB", a success note
        if raw.get("redirected") and outputs:
            messages.append(REDIRECTED)  # a note on a successful job too, for its toast
        undo: dict = {}
        if raw.get("moves"):  # Move to: Undo moves them back (FR-5.6)
            undo = {"type": "move", "moves": raw["moves"]}
            try:
                undo["stamps"] = [stamp(Path(moved)) for _, moved in raw["moves"]]
            except OSError:
                pass
        elif outputs:
            undo = {"type": "trash", "paths": outputs}
            try:  # so Undo can tell these files from ones saved later under the same names
                undo["stamps"] = [stamp(o) for o in outputs]
            except OSError:
                pass  # without stamps Undo trashes whatever is there, as before
            if raw.get("dirs"):
                undo["dirs"] = list(raw["dirs"])
        message = "; ".join(messages)
        copied = raw.get("copied", "")
        return JobResult(job.op_id, job.inputs, outputs, ok, message, undo, copied=copied)
