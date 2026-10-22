"""The tray (FR-7.1) and its History menu (FR-5.7)."""

import dataclasses
from pathlib import Path

import pytest
from PySide6.QtGui import QColor
from pytestqt.qtbot import QtBot

from dragshift.engine.jobs import JobResult
from dragshift.history import History
from dragshift.ui.tray import Tray, draw_icon, entry_text


@pytest.fixture
def history(tmp_path: Path) -> History:
    return History(tmp_path / "history.json")


@pytest.fixture
def tray(qtbot: QtBot, history: History) -> Tray:
    return Tray(history)


def texts(menu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_the_menu_of_fr_7_1(tray: Tray) -> None:
    assert texts(tray.menu) == ["Settings…", "History", "Pause gestures", "Open log folder", "Quit"]


def test_menu_items_ask_the_app(tray: Tray, qtbot: QtBot) -> None:
    for text, signal in (
        ("Settings…", tray.settings_requested),
        ("Open log folder", tray.logs_requested),
        ("Quit", tray.quit_requested),
    ):
        action = next(a for a in tray.menu.actions() if a.text() == text)
        with qtbot.waitSignal(signal, timeout=1000):
            action.trigger()
    with qtbot.waitSignal(tray.pause_toggled, timeout=1000) as got:
        tray.pause_action.trigger()
    assert got.args == [True]


def test_set_paused_follows_the_config_quietly(tray: Tray, qtbot: QtBot) -> None:
    with qtbot.assertNotEmitted(tray.pause_toggled):
        tray.set_paused(True)
    assert tray.pause_action.isChecked()
    assert tray.icon.toolTip() == "DragShift (paused)"


def test_the_icon_is_drawn_in_the_colour() -> None:
    img = draw_icon(QColor("#FF0000"), 32).toImage()
    colours = {img.pixelColor(x, y).name() for x in range(32) for y in range(32)}
    assert "#ff0000" in colours


def test_history_lists_jobs_with_open_show_and_undo(
    tray: Tray, history: History, tmp_path: Path, qtbot: QtBot
) -> None:
    tray._fill_history()
    assert texts(tray.history_menu) == ["No jobs yet"]
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    history.record(
        "j1",
        JobResult(
            "convert.image.jpg",
            [tmp_path / "a.png"],
            [out],
            True,
            "",
            {"type": "trash", "paths": [out]},
        ),
    )
    history.record(
        "j2", JobResult("convert.image.webp", [tmp_path / "b.png"], [], False, "boom", {})
    )
    tray._fill_history()
    failed, done = tray.history_menu.actions()
    assert failed.text().startswith("✕ b.png — WEBP failed") and not failed.isEnabled()
    assert done.text().startswith("a.jpg — JPG")
    sub = done.menu()
    assert texts(sub) == ["Open", "Show in folder", "Undo"]
    with qtbot.waitSignal(tray.history_action) as got:
        sub.actions()[2].trigger()
    assert got.args == ["j1", "undo"]


def test_an_undone_job_says_so(history: History, tmp_path: Path) -> None:
    result = JobResult(
        "convert.image.jpg", [tmp_path / "a.png"], [tmp_path / "a.jpg"], True, "", {}
    )
    history.record("j", result)
    entry = history.entries[0]
    assert "(undone)" not in entry_text(entry)
    assert "(undone)" in entry_text(dataclasses.replace(entry, undone=True))
