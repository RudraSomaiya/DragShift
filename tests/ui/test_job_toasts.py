"""Job toasts (FR-4.5, FR-5.3, FR-5.5, FR-5.6): what they say, and what their buttons do."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from pytestqt.qtbot import QtBot

from dragshift.config import Appearance
from dragshift.engine import jobs as jobs_module
from dragshift.engine.jobs import CANCELLED, REDIRECTED, JobResult
from dragshift.history import History
from dragshift.ui import job_toasts as jt
from dragshift.ui.job_toasts import (
    JobToasts,
    human_size,
    primary_output,
    progress_content,
    result_content,
)

STILL = Appearance(theme="dark", accent="#4C8DFF", reduced_motion="on")  # no motion


def result(
    outputs: list[Path], ok: bool = True, message: str = "", dirs=(), op="convert.image.jpg"
):
    undo: dict = {"type": "trash", "paths": outputs} if outputs else {}
    if dirs:
        undo["dirs"] = list(dirs)
    return JobResult(op, [Path("C:/x/a.png")], outputs, ok, message, undo)


@pytest.mark.parametrize(
    ("n", "text"),
    [(0, "0 B"), (1023, "1023 B"), (880_640, "860 KB"), (1_258_291, "1.2 MB"), (14 << 20, "14 MB")],
)
def test_human_size(n: int, text: str) -> None:
    assert human_size(n) == text


def test_open_and_show_use_the_pages_folder_when_it_holds_every_output(tmp_path: Path) -> None:
    pages = tmp_path / "doc pages"
    outs = [pages / "doc 001.png", pages / "doc 002.png"]
    assert primary_output(result(outs, dirs=[pages])) == pages


def test_open_and_show_use_the_document_when_images_sit_in_a_media_folder(tmp_path: Path) -> None:
    media = tmp_path / "doc media"
    outs = [tmp_path / "doc.md", media / "image1.png"]
    assert primary_output(result(outs, dirs=[media])) == tmp_path / "doc.md"


def test_a_success_names_the_output_and_what_was_done(tmp_path: Path) -> None:
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x" * 2048)
    c = result_content(result([out]))
    assert (c.title, c.subtitle, c.buttons) == (
        "a.jpg",
        "Converted to JPG · 2.0 KB",
        ("Open", "Show", "Undo"),
    )
    assert c.kind == "done" and c.icon == out


def test_several_outputs_are_counted(tmp_path: Path) -> None:
    pages = tmp_path / "doc pages"
    pages.mkdir()
    outs = [pages / "doc 001.png", pages / "doc 002.png"]
    for o in outs:
        o.write_bytes(b"x" * 600)
    c = result_content(result(outs, dirs=[pages], op="convert.pdf.png"))
    assert (c.title, c.subtitle) == ("doc pages", "Converted to PNG · 2 files · 1.2 KB")


def test_downloads_note_replaces_the_summary(tmp_path: Path) -> None:
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    assert result_content(result([out], message=REDIRECTED)).subtitle == REDIRECTED


def test_a_failure_shows_a_one_line_reason_and_details() -> None:
    c = result_content(result([], ok=False, message="OSError: disk full\nTraceback…"))
    assert (c.title, c.subtitle, c.buttons) == (
        "Couldn't convert to JPG",
        "OSError: disk full",
        ("Details",),
    )
    assert c.kind == "error"


def test_an_op_without_outputs_just_confirms() -> None:
    c = result_content(result([], op="unknown.op"))
    assert (c.title, c.subtitle, c.buttons) == ("unknown.op", "Done", ())


def test_progress_names_the_file_and_offers_cancel() -> None:
    c = progress_content("convert.image.jpg", [Path("C:/x/a.png")], 0.4, "")
    assert (c.title, c.subtitle, c.buttons, c.progress) == (
        "Converting to JPG",
        "a.png",
        ("Cancel",),
        0.4,
    )
    assert (
        progress_content("convert.image.jpg", [Path("a"), Path("b")], None, "").subtitle
        == "2 files"
    )


# --- JobToasts --------------------------------------------------------------------------------
class FakeJobs(QObject):
    progressed = Signal(str, float, str)

    def __init__(self) -> None:
        super().__init__()
        self.cancelled: list[str] = []

    def cancel(self, job_id: str) -> None:
        self.cancelled.append(job_id)


@pytest.fixture
def rig(qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple]:
    monkeypatch.setattr(JobToasts, "PROGRESS_AFTER_MS", 20)
    jobs = FakeJobs()
    history = History(tmp_path / "history.json")
    toasts = JobToasts(jobs, history, tmp_path / "dragshift.log")  # type: ignore[arg-type]
    toasts.stack.set_appearance(STILL)
    yield toasts, jobs, history
    toasts.shutdown()


def click(toasts: JobToasts, key: str, button: str) -> None:
    toasts.stack.clicked.emit(key, button)


def test_a_long_job_gets_a_progress_toast_that_becomes_its_result(
    qtbot: QtBot, rig: tuple, tmp_path: Path
) -> None:
    toasts, jobs, _history = rig
    toasts.started("j", "convert.image.jpg", [tmp_path / "a.png"])
    assert toasts.stack.get("j") is None  # not before 1 s (20 ms here)
    qtbot.waitUntil(lambda: toasts.stack.get("j") is not None, timeout=2000)
    toast = toasts.stack.get("j")
    assert toast.content.kind == "progress" and toast.content.progress is None
    jobs.progressed.emit("j", 0.5, "page 2 of 4")
    assert (toast.content.progress, toast.content.subtitle) == (0.5, "page 2 of 4")
    click(toasts, "j", "Cancel")
    assert jobs.cancelled == ["j"]
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    toasts.finished("j", result([out]))
    assert toasts.stack.get("j") is toast and toast.content.title == "a.jpg"


def test_a_quick_job_shows_only_its_result(qtbot: QtBot, rig: tuple, tmp_path: Path) -> None:
    toasts, *_ = rig
    toasts.started("j", "convert.image.jpg", [tmp_path / "a.png"])
    toasts.finished("j", result([], ok=False, message="boom"))
    qtbot.wait(60)  # past PROGRESS_AFTER_MS
    assert toasts.stack.get("j").content.kind == "error"


def test_a_cancelled_job_s_toast_goes_away(qtbot: QtBot, rig: tuple, tmp_path: Path) -> None:
    toasts, *_ = rig
    toasts.started("j", "convert.image.jpg", [tmp_path / "a.png"])
    qtbot.waitUntil(lambda: toasts.stack.get("j") is not None, timeout=2000)
    cancelled = JobResult("convert.image.jpg", [], [], False, CANCELLED, {}, cancelled=True)
    toasts.finished("j", cancelled)
    assert toasts.stack.get("j") is None


def test_an_evicted_progress_toast_does_not_come_back_on_progress(
    qtbot: QtBot, rig: tuple, tmp_path: Path
) -> None:
    """With more long jobs than slots, toasts must not push each other out on every tick."""
    toasts, jobs, _history = rig
    for i in range(4):
        toasts.started(f"j{i}", "convert.image.jpg", [tmp_path / f"{i}.png"])
    qtbot.waitUntil(lambda: all(r.shown for r in toasts._running.values()), timeout=2000)
    up = toasts.stack.keys()
    (evicted,) = {"j0", "j1", "j2", "j3"} - set(up)  # one made room (timer order is not fixed)
    for _ in range(3):
        for i in range(4):
            jobs.progressed.emit(f"j{i}", 0.5, "")
    assert toasts.stack.keys() == up  # nobody pushed anybody out
    out = tmp_path / "out.jpg"
    out.write_bytes(b"x")
    toasts.finished(evicted, result([out]))  # its result still shows
    assert toasts.stack.get(evicted).content.title == "out.jpg"


def test_a_folder_output_counts_the_files_inside(tmp_path: Path) -> None:
    folder = tmp_path / "pack"
    (folder / "sub").mkdir(parents=True)
    (folder / "a.txt").write_bytes(b"x" * 1000)
    (folder / "sub" / "b.txt").write_bytes(b"x" * 1048)
    c = result_content(result([folder], op="convert.archive.extract"))
    assert c.subtitle == "Extract · 2.0 KB"  # not "Converted to Extract"


def test_undo_says_which_changed_files_it_kept(
    rig: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toasts, _jobs, history = rig
    monkeypatch.setattr(jobs_module, "_trash", lambda _paths: None)
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    res = result([out])
    res.undo["stamps"] = [[999, 0]]  # not what is there now
    history.record("j", res)
    toasts.finished("j", res)
    click(toasts, "j", "Undo")
    assert toasts.stack.get("j").content.subtitle == "Kept a.jpg: changed since it was made"


def test_undo_from_the_toast(rig: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toasts, _jobs, history = rig
    trashed: list[Path] = []
    monkeypatch.setattr(jobs_module, "_trash", trashed.extend)
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    res = result([out])
    history.record("j", res)  # App records before it shows the toast
    toasts.finished("j", res)
    click(toasts, "j", "Undo")
    assert trashed == [out] and history.get("j").undone
    content = toasts.stack.get("j").content
    assert (content.title, content.buttons) == ("Undone", ())


def test_undo_failure_is_reported(
    rig: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    toasts, _jobs, history = rig

    def refuse(_paths: list[Path]) -> None:
        raise OSError("the Recycle Bin is full")

    monkeypatch.setattr(jobs_module, "_trash", refuse)
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    history.record("j", result([out]))
    toasts.finished("j", result([out]))
    click(toasts, "j", "Undo")
    content = toasts.stack.get("j").content
    assert (content.kind, content.title, content.subtitle) == (
        "error",
        "Couldn't undo",
        "the Recycle Bin is full",
    )


def test_open_and_show(rig: tuple, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    toasts, *_ = rig
    calls: list[tuple] = []
    monkeypatch.setattr(jt.files, "open_path", lambda p: calls.append(("open", p)))
    monkeypatch.setattr(jt.files, "show_in_folder", lambda p: calls.append(("show", p)))
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    toasts.finished("j", result([out]))
    click(toasts, "j", "Open")
    click(toasts, "j", "Show")
    assert calls == [("open", out), ("show", out)]


def test_details_copies_the_error_and_the_log_path(rig: tuple, tmp_path: Path) -> None:
    toasts, *_ = rig
    toasts.finished("j", result([], ok=False, message="OSError: disk full"))
    click(toasts, "j", "Details")
    text = QGuiApplication.clipboard().text()
    assert text == f"OSError: disk full\nLog: {tmp_path / 'dragshift.log'}"
    assert toasts.stack.get("j").content.buttons == ()


def test_an_error_outside_a_job(rig: tuple) -> None:
    toasts, *_ = rig
    toasts.error("Couldn't open VS Code", "file not found")
    (key,) = toasts.stack.keys()
    content = toasts.stack.get(key).content
    assert (content.title, content.kind, content.buttons) == (
        "Couldn't open VS Code",
        "error",
        ("Details",),
    )
