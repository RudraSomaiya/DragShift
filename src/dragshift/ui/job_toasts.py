"""Toasts for jobs and other events (FR-4.5, FR-5.3, FR-5.5, FR-5.6).

- A job still running after PROGRESS_AFTER_MS gets a progress toast with Cancel (FR-4.5).
- When it finishes, the same toast shows the result: Open · Show · Undo, or a one-line reason
  and Details, which copies the error and the log path (FR-5.5). A cancelled job's toast goes.
- Undo goes through History, so the tray's History knows the job was undone (FR-5.6, FR-5.7).

UI-process module: no engine libraries (CLAUDE.md rule 2).
"""

import dataclasses
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtGui import QGuiApplication

from dragshift.engine import registry
from dragshift.engine.jobs import JobManager, JobResult
from dragshift.engine.presets import human_size
from dragshift.history import History
from dragshift.platform import files
from dragshift.ui.toast import ToastContent, ToastStack

__all__ = ["JobToasts", "human_size", "primary_output", "progress_content", "result_content"]

log = logging.getLogger(__name__)


def _op_label(op_id: str) -> tuple[bool, str]:
    """(converts to a format, label) of an op; (False, op_id) for one the registry does not know.
    Format petals are upper case ("PNG", "TAR.GZ"): "Converted to PNG". The others say what
    they do ("Extract", "Merge to PDF") and are shown as they are."""
    try:
        op = registry.get(op_id)
    except KeyError:
        return False, op_id
    return op.wheel == "convert" and op.label.isupper(), op.label


def primary_output(result: JobResult) -> Path | None:
    """What Open and Show act on: the output folder the job made when it holds every output
    (``<stem> pages``), else the first output."""
    if not result.outputs:
        return None
    for folder in map(Path, reversed(result.undo.get("dirs", ()))):  # deepest first
        if all(o.is_relative_to(folder) for o in result.outputs):
            return folder
    return result.outputs[0]


def size_of(path: Path) -> int:
    """A file's size, or the total of the files in a folder output (an extracted archive)."""
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size


def total_size(paths: Sequence[Path]) -> int | None:
    try:
        return sum(map(size_of, paths))
    except OSError:
        return None


def result_content(result: JobResult) -> ToastContent:
    to_format, label = _op_label(result.op_id)
    if not result.ok:
        title = f"Couldn't convert to {label}" if to_format else f"{label} failed"
        reason = result.message.splitlines()[0] if result.message else "Something went wrong"
        return ToastContent(title, reason, ("Details",), kind="error")
    main = primary_output(result)
    if main is None:  # an op with no file output, e.g. Copy path: what it copied
        copied = result.copied.splitlines()[0] if result.copied else ""
        return ToastContent(label, result.message or copied or "Done")
    parts = [f"Converted to {label}" if to_format else label]
    if len(result.outputs) > 1:
        parts.append(f"{len(result.outputs)} files")
    size = total_size(result.outputs)
    if size is not None:
        parts.append(human_size(size))
    # A note on a successful job (it went to Downloads, FR-5.3) replaces the summary.
    subtitle = result.message or " · ".join(parts)
    return ToastContent(main.name, subtitle, ("Open", "Show", "Undo"), icon=main)


def progress_content(
    op_id: str, inputs: Sequence[Path], fraction: float | None, text: str
) -> ToastContent:
    to_format, label = _op_label(op_id)
    title = f"Converting to {label}" if to_format else label
    what = inputs[0].name if len(inputs) == 1 else f"{len(inputs)} files"
    icon = inputs[0] if inputs else None
    return ToastContent(
        title, text or what, ("Cancel",), kind="progress", icon=icon, progress=fraction
    )


@dataclass
class _Running:
    op_id: str
    inputs: list[Path]
    fraction: float | None = None
    text: str = ""
    shown: bool = False


class JobToasts(QObject):
    PROGRESS_AFTER_MS = 1000  # FR-4.5: "long jobs (> 1 s)"

    def __init__(
        self,
        jobs: JobManager,
        history: History,
        log_path: Path,
        stack: ToastStack | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.stack = stack or ToastStack(self)
        self._jobs = jobs
        self._history = history
        self._log_path = log_path
        self._running: dict[str, _Running] = {}
        self._results: dict[str, JobResult] = {}  # whose result toast is up
        self._details: dict[str, str] = {}  # what Details copies, per toast
        jobs.progressed.connect(self._on_progress)
        self.stack.clicked.connect(self._on_clicked)
        self.stack.closed.connect(self._forget)

    def started(self, job_id: str, op_id: str, inputs: Sequence[Path]) -> None:
        self._running[job_id] = _Running(op_id, list(inputs))
        QTimer.singleShot(self.PROGRESS_AFTER_MS, self, lambda: self._show_progress(job_id))

    def finished(self, job_id: str, result: JobResult) -> None:
        self._running.pop(job_id, None)
        if result.cancelled:
            self.stack.dismiss(job_id)
            return
        if result.ok and not (result.outputs or result.message or result.copied):
            self.stack.dismiss(job_id)  # nothing to tell: the terminal it opened says it all
            return
        self._results[job_id] = result
        if not result.ok:
            self._details[job_id] = result.message
        self.stack.show(job_id, result_content(result))

    def error(self, title: str, message: str) -> None:
        """A failure outside a job, e.g. a Quick Launch slot that would not open."""
        key = uuid.uuid4().hex
        self._details[key] = message
        self.stack.show(key, ToastContent(title, message, ("Details",), kind="error"))

    def shutdown(self) -> None:
        self._running.clear()
        self.stack.close_all()

    # --- internals ----------------------------------------------------------------------------
    def _show_progress(self, job_id: str) -> None:
        run = self._running.get(job_id)
        if run is not None:
            run.shown = True
            self._update_progress(job_id, run)

    def _update_progress(self, job_id: str, run: _Running) -> None:
        content = progress_content(run.op_id, run.inputs, run.fraction, run.text)
        self.stack.show(job_id, content)

    @Slot(str, float, str)
    def _on_progress(self, job_id: str, fraction: float, text: str) -> None:
        run = self._running.get(job_id)
        if run is None:
            return
        run.fraction, run.text = fraction, text
        # Only update a progress toast that is still up. One that made room for a newer toast
        # stays gone until the result: bringing it back would push out another, every tick.
        if run.shown and self.stack.get(job_id) is not None:
            self._update_progress(job_id, run)

    @Slot(str, str)
    def _on_clicked(self, key: str, button: str) -> None:
        result = self._results.get(key)
        try:
            if button == "Cancel":
                self._jobs.cancel(key)
            elif button == "Details":
                self._copy_details(key)
            elif result is None:
                return
            elif button == "Open":
                files.open_path(primary_output(result))
            elif button == "Show":
                files.show_in_folder(primary_output(result))
            elif button == "Undo":
                self._undo(key, result)
        except (OSError, ValueError) as e:  # ValueError: an undo type apply_undo does not know
            log.warning("%s failed: %s", button, e)
            what = f"Couldn't {button.lower()}"
            self.stack.show(key, ToastContent(what, str(e), kind="error"))

    def _undo(self, key: str, result: JobResult) -> None:
        main = primary_output(result)
        name = main.name if main else ""
        try:
            kept = self._history.undo(key)
        except KeyError:
            self.stack.show(key, ToastContent("Already undone", name))
            return
        del self._results[key]
        if kept:  # changed since the job: no longer the file DragShift wrote, so it stays
            what = kept[0].name if len(kept) == 1 else f"{len(kept)} files"
            note = f"Kept {what}: changed since it was made"
        else:
            note = f"{name} is in the Recycle Bin"
        self.stack.show(key, ToastContent("Undone", note))

    def _copy_details(self, key: str) -> None:
        QGuiApplication.clipboard().setText(f"{self._details.get(key, '')}\nLog: {self._log_path}")
        toast = self.stack.get(key)
        if toast is not None:
            copied = "Copied the error and the log path"
            self.stack.show(key, dataclasses.replace(toast.content, subtitle=copied, buttons=()))

    @Slot(str)
    def _forget(self, key: str) -> None:
        self._results.pop(key, None)
        self._details.pop(key, None)
