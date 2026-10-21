"""Headless sub-commands (FR-6.4): ``ops``, ``convert`` and ``tool``.

They run Ops through the same JobManager and worker pool as the app, so naming, ``.part``
files and "never touch the source" are identical. Output paths go to stdout, one per line;
errors go to stderr. Exit codes: 0 done, 1 the Op failed or was cancelled, 2 bad usage.
They do not need, and do not check for, a running DragShift.
"""

import signal
import sys
from pathlib import Path

from dragshift.engine import registry
from dragshift.engine.registry import Op

OK, FAILED, USAGE = 0, 1, 2
WHEELS = ("convert", "tools", "actions")


def _err(message: str) -> None:
    print(f"dragshift: {message}", file=sys.stderr)


def _existing(paths: list[str]) -> list[Path] | None:
    resolved = [Path(p).resolve() for p in paths]
    missing = [str(p) for p in resolved if not p.exists()]
    if missing:
        _err("no such file: " + ", ".join(missing))
        return None
    return resolved


def ops(paths: list[str]) -> int:
    """Every Op the wheels would show for these files, per wheel."""
    files = _existing(paths)
    if files is None:
        return USAGE
    for wheel in WHEELS:
        found = registry.ops_for(files, wheel)
        if found:
            print(f"{wheel}:")
            for o in found:
                print(f"  {o.id:<28} {o.label}")
    return OK


def convert(paths: list[str], to: str) -> int:
    """The Convert-wheel Op whose label is ``to`` (``png``, ``tar.gz``, ``merge to pdf``)."""
    files = _existing(paths)
    if files is None:
        return USAGE
    choices = registry.ops_for(files, "convert")
    match = [o for o in choices if o.label.casefold() == to.strip().casefold()]
    if not match:
        valid = ", ".join(o.label for o in choices) or "none"
        _err(f"cannot convert these files to {to!r}. Valid: {valid}")
        return USAGE
    return run_op(match[0], files, {})


def tool(op_id: str, paths: list[str], preset: str | None) -> int:
    files = _existing(paths)
    if files is None:
        return USAGE
    try:
        found = registry.get(op_id)
    except KeyError:
        _err(f"no such op: {op_id!r} (see: dragshift ops <file>)")
        return USAGE
    if not registry.accepts(found, files):
        _err(f"{op_id} does not accept {', '.join(p.name for p in files)}")
        return USAGE
    return run_op(found, files, {"preset": preset} if preset else {})


def run_op(found: Op, files: list[Path], params: dict) -> int:
    """Run ``found`` through a JobManager and wait. Ctrl+C cancels it cleanly."""
    if found.dialog:
        _err(f"{found.id} needs its {found.dialog} dialog: use it from the wheel")
        return USAGE
    if found.where == "ui":
        _err(f"{found.id} acts on the desktop (clipboard, terminal): use it from the wheel")
        return USAGE
    from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

    from dragshift.engine.jobs import JobManager, JobResult

    _app = QCoreApplication.instance() or QCoreApplication([])  # kept alive until we return
    jobs = JobManager()
    loop = QEventLoop()
    results: list[JobResult] = []
    show_progress = sys.stderr is not None and sys.stderr.isatty()

    def finished(_job: str, result: JobResult) -> None:
        results.append(result)
        loop.quit()

    def progressed(_job: str, fraction: float, text: str) -> None:
        if show_progress:
            print(f"\r{fraction:4.0%} {text[:60]:<60}", end="", file=sys.stderr, flush=True)

    jobs.finished.connect(finished)
    jobs.progressed.connect(progressed)
    job = jobs.submit(found.id, files, params)
    previous = signal.signal(signal.SIGINT, lambda *_: jobs.cancel(job))
    keepalive = QTimer()  # lets Python run the Ctrl+C handler while Qt waits
    keepalive.timeout.connect(lambda: None)
    keepalive.start(200)
    try:
        loop.exec()
    finally:
        keepalive.stop()
        signal.signal(signal.SIGINT, previous)
        jobs.shutdown()
        if show_progress:
            print(file=sys.stderr)
    (result,) = results
    for path in result.outputs:
        print(path)
    if result.copied:  # SHA-256: the wheel copies it; here it is printed
        print(result.copied)
    if not result.ok:
        _err(result.message)
        return FAILED
    if result.message:  # a note on a successful job, e.g. it went to Downloads (FR-5.3)
        _err(result.message)
    return OK
