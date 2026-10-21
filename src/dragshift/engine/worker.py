"""Worker-side job code: output naming, JobContext, and the pool entry point.

This module is unpickled inside worker processes, so it imports neither Qt nor engine libraries.
The public home of ``JobContext`` is ``dragshift.engine.jobs`` (PRD §9.4), which re-exports it.
"""

import importlib
import os
import shutil
from pathlib import Path
from typing import Any

PART = ".part"
CANCELLED = "Cancelled"  # the message of a cancelled job

# This worker process's progress queue, set once by init_worker() when the pool starts it. It is
# a multiprocessing.SimpleQueue: put() writes straight into the pipe (no feeder thread), so a
# worker never hangs at exit flushing a buffer, and every progress message is in the pipe
# before the job's result is sent.
_worker_queue: Any = None


def init_worker(queue: Any) -> None:
    """ProcessPoolExecutor initializer: receive the progress queue (no Manager process needed)."""
    global _worker_queue
    _worker_queue = queue


def numbered(path: Path, n: int) -> Path:
    """``a.jpg`` → ``a (n).jpg`` for n ≥ 2 (FR-5.2)."""
    return path if n < 2 else path.with_name(f"{path.stem} ({n}){path.suffix}")


def free_path(path: Path) -> Path:
    """First of ``path``, ``path (2)``, ``path (3)``, … that does not exist."""
    n = 1
    while numbered(path, n).exists():
        n += 1
    return numbered(path, n)


def downloads_dir() -> Path:
    """FR-5.3: where outputs go when the source folder cannot be written to."""
    return Path(os.environ.get("USERPROFILE") or Path.home(), "Downloads")


def new_folder(base: Path, ctx: "JobContext") -> Path:
    """Create the output folder ``base``, or ``base (2)``, … if that exists, so new outputs never
    mix with old files. ``mkdir`` is atomic, so two jobs never share a folder. A failed or
    cancelled job removes it again while it is empty. If ``base``'s folder cannot be written
    to, the folder is made in Downloads instead (FR-5.3)."""
    try:
        return _new_folder(base, ctx)
    except PermissionError:
        return _new_folder(ctx.redirect(base), ctx)


def _new_folder(base: Path, ctx: "JobContext") -> Path:
    n = 1
    while True:
        # Not numbered(): a folder name has no extension ("v1.2 report pages").
        folder = base if n < 2 else base.with_name(f"{base.name} ({n})")
        try:
            folder.mkdir()
        except FileExistsError:
            n += 1
            continue
        ctx.made_dirs.append(folder)
        ctx.out_dirs.append(folder)
        return folder


ERROR_NOT_SAME_DEVICE = 17  # winerror: a rename across drives


def move_path(src: Path, dst: Path) -> None:
    """Move a file or folder to ``dst``, which must not exist. A rename on the same drive;
    across drives a copy, then the original is deleted once the copy is complete."""
    try:
        os.rename(src, dst)  # fails rather than replace on Windows
        return
    except OSError as e:
        if getattr(e, "winerror", None) != ERROR_NOT_SAME_DEVICE:
            raise
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
        shutil.rmtree(src)
    else:
        with src.open("rb") as f, dst.open("xb") as out:  # "x": never over a file made since
            shutil.copyfileobj(f, out, 1 << 20)
        shutil.copystat(src, dst)
        src.unlink()


def final_path(part: Path) -> Path:
    return part.with_name(part.name.removesuffix(PART))


def finalize(part: Path) -> Path:
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


class CancelDir:
    """Cancel flags shared with worker processes as empty files named after the job id."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def __contains__(self, job_id: object) -> bool:
        return (self.path / str(job_id)).exists()

    def set(self, job_id: str) -> None:
        (self.path / job_id).touch()

    def clear(self, job_id: str) -> None:
        (self.path / job_id).unlink(missing_ok=True)


class JobContext:
    """Passed to every worker function. Picklable: it crosses into the worker process."""

    def __init__(self, job_id: str, queue: Any = None, cancel_flags: Any = None) -> None:
        self.job_id = job_id
        self._queue = queue  # .put((job_id, fraction, text)); None = this worker's queue
        self._cancel = cancel_flags if cancel_flags is not None else {}  # job_id in → cancelled
        self._slot = (0, 1)  # (file group index, group count) for overall progress
        self.reserved: list[Path] = []  # .part files this job created
        self.made_dirs: list[Path] = []  # every folder this job created, for failure cleanup
        # The job's own output folders (new_folder: "<stem> pages"), not parents made on the
        # way such as a missing Downloads: these are what Undo removes and Open shows.
        self.out_dirs: list[Path] = []
        self.redirected = False  # an output went to Downloads (FR-5.3)
        self.copied = ""  # text for the clipboard, set on the Qt thread (Copy path, SHA-256)
        self.final: list[Path] = []  # outputs the op renamed itself: JobManager leaves them be
        self.moves: list[tuple[Path, Path]] = []  # (from, to) per moved source, for Undo

    def output_path(
        self, src: Path, *, ext: str, label: str | None = None, folder: Path | None = None
    ) -> Path:
        """Reserve and return ``<folder>/<stem>[ <label>].<ext>.part`` (FR-5.1, FR-5.2).

        The name never collides with an existing output or another job's ``.part``. Pass the
        format to the writer explicitly, since the ``.part`` suffix hides the real extension.
        If the folder cannot be written to, the file is reserved in Downloads (FR-5.3).
        """
        out_dir = folder if folder is not None else src.parent
        stem = src.name if src.is_dir() else src.stem
        ext = ext.lstrip(".")
        name = f"{stem}{f' {label}' if label else ''}{f'.{ext}' if ext else ''}"
        try:
            return self.reserve(out_dir / name)
        except PermissionError:
            return self.reserve(self.redirect(out_dir / name))

    def redirect(self, path: Path) -> Path:
        """FR-5.3: where ``path`` goes after its folder refused a write: the same name in
        Downloads, which is created if missing. Raises PermissionError if ``path`` is in
        Downloads already. Every way of writing an output calls this on PermissionError."""
        fallback = downloads_dir() / path.name
        if path.parent == fallback.parent:
            raise PermissionError(f"cannot write to {path.parent}")
        self.redirected = True
        self._make_dirs(fallback.parent)
        return fallback

    def reserve(self, base: Path) -> Path:
        """Create the first free ``base``, ``base (2)``, … as an empty ``.part`` file and
        return it. Unlike ``output_path`` it never goes to Downloads: Copy to and Move to
        write where the user pointed, or fail."""
        self._make_dirs(base.parent)
        n = 1
        while True:
            final = numbered(base, n)
            part = final.with_name(final.name + PART)
            if final.exists():
                n += 1
                continue
            try:
                part.open("x").close()  # exclusive create = atomic reservation
            except FileExistsError:
                n += 1
                continue
            except PermissionError:
                if part.exists():  # a folder by that name, not a read-only folder
                    n += 1
                    continue
                raise
            except FileNotFoundError:
                # Another job's failure cleanup removed the shared, empty folder: recreate it.
                self._make_dirs(base.parent)
                continue
            self.reserved.append(part)
            return part

    def _make_dirs(self, folder: Path) -> None:
        """Create ``folder`` and any missing parents, remembering each one for cleanup."""
        missing = []
        while not folder.exists():
            missing.append(folder)
            folder = folder.parent
        for d in reversed(missing):  # top-most first
            d.mkdir(exist_ok=True)
            self.made_dirs.append(d)

    def discard(self, parts: list[Path]) -> None:
        """Delete these reserved ``.part`` files and any output folder this job left empty."""
        for part in parts:
            part.unlink(missing_ok=True)
        for folder in reversed(self.made_dirs):  # deepest first
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                pass

    def progress(self, fraction: float, text: str = "") -> None:
        queue = self._queue if self._queue is not None else _worker_queue
        if queue is not None:
            i, count = self._slot
            overall = (i + min(max(fraction, 0.0), 1.0)) / count
            try:
                queue.put((self.job_id, overall, text))
            except OSError:
                pass  # the UI process has gone away; progress is best-effort

    def cancelled(self) -> bool:
        return self.job_id in self._cancel

    def copy_text(self, text: str) -> None:
        """Put ``text`` on the clipboard when the job finishes. The UI process does it, since a
        worker has no window to own the clipboard."""
        self.copied = text


def resolve(func: str) -> Any:
    module, _, attr = func.partition(":")
    return getattr(importlib.import_module(module), attr)


def _error(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


def run_job(func: str, groups: list[list[Path]], params: dict, ctx: JobContext) -> dict[str, Any]:
    """Pool entry point: run ``func`` once per path group and report a plain, picklable result.

    Groups are independent (FR-4.2): a failing file loses only its own ``.part`` files and the
    outputs of the other files are kept. A cancel discards everything this job wrote. Source
    files are never touched here (CLAUDE.md rule 4).
    """
    try:
        fn = resolve(func)
    except Exception as e:  # noqa: BLE001 - any Op failure becomes a toast (NFR-9)
        return {"ok": False, "outputs": [], "message": _error(e)}
    outputs: list[Path] = []
    errors: list[str] = []
    try:
        for i, paths in enumerate(groups):
            if ctx.cancelled():
                break
            ctx._slot = (i, len(groups))
            start = len(ctx.reserved)
            try:
                outputs += fn(paths, params, ctx)
            except Exception as e:  # noqa: BLE001 - any Op failure becomes a toast (NFR-9)
                ctx.discard(ctx.reserved[start:])
                name = paths[0].name if len(paths) == 1 else f"{len(paths)} files"
                errors.append(_error(e) if len(groups) == 1 else f"{name}: {_error(e)}")
    except BaseException:  # SystemExit and the like: clean up, then let it propagate
        ctx.discard(ctx.reserved)
        raise
    if ctx.cancelled():
        ctx.discard(ctx.reserved)
        return {"ok": False, "outputs": [], "message": CANCELLED, "cancelled": True}
    if not errors:
        message = ""
    elif len(groups) == 1:
        message = errors[0]
    else:
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        message = f"{len(groups) - len(errors)} of {len(groups)} done; {errors[0]}{more}"
    result: dict[str, Any] = {"ok": not errors, "outputs": outputs, "message": message}
    # Optional keys: the output folders still there (Undo removes them once empty, FR-5.6) and
    # whether anything went to Downloads (FR-5.3).
    dirs = [d for d in ctx.out_dirs if d.is_dir()]
    if dirs:
        result["dirs"] = dirs
    if ctx.redirected and outputs:
        result["redirected"] = True
    # Copy to and Move to rename their own outputs: a moved file named "x.part" must keep its
    # name, and a moved source is never left as a .part that a failed rename would delete.
    final = [o for o in outputs if o in ctx.final]
    if final:
        result["final"] = final
    if ctx.moves:
        result["moves"] = [list(m) for m in ctx.moves]
    if ctx.copied:
        result["copied"] = ctx.copied
    return result
