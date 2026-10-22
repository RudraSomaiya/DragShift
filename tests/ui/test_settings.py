"""The Settings window (FR-8.1, FR-8.2): every change is saved at once and shows up live."""

import dataclasses
import json
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from dragshift.config import ConfigStore, LauncherApp, LauncherSite
from dragshift.engine.kinds import Kind
from dragshift.launcher import apps
from dragshift.ui import theme
from dragshift.ui.dialogs.settings import TABS, SettingsWindow, StartAppsPicker, ops_of


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def window(qtbot: QtBot, store: ConfigStore) -> SettingsWindow:
    w = SettingsWindow(store, theme.make(dark=True))
    qtbot.addWidget(w)
    w.show()
    return w


def saved(store: ConfigStore) -> dict:
    return json.loads(store.path.read_text(encoding="utf-8"))


def settle(qtbot: QtBot) -> None:
    qtbot.wait(5)  # the window shows what was saved after the event


def test_six_tabs_in_a_760_by_520_window(window: SettingsWindow) -> None:
    assert [window.nav.item(i).text() for i in range(window.nav.count())] == list(TABS)
    assert (window.width(), window.height()) == (760, 520)
    window.show_tab("Output")
    assert window.pages.currentWidget() is window.output


# --- Keys -----------------------------------------------------------------------------------
def test_picking_a_key_in_use_swaps_the_two_wheels(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    window.keys.keys["convert"].buttons["Ctrl"].click()
    assert (store.config.keys.convert, store.config.keys.tools) == ("ctrl", "shift")
    assert saved(store)["keys"]["convert"] == "ctrl"  # saved at once (FR-8.2)
    settle(qtbot)
    assert window.keys.keys["tools"].value == "Shift"
    window.keys.keys["launch_sites"].buttons["Shift"].click()
    assert (store.config.keys.launch_apps, store.config.keys.launch_sites) == ("ctrl", "shift")


def test_double_tap(window: SettingsWindow, store: ConfigStore) -> None:
    window.keys.tap.buttons["Double tap"].click()
    assert store.config.keys.tap_mode == "double"


# --- Wheels ---------------------------------------------------------------------------------
def test_petals_of_a_kind_include_every_extension_s_targets() -> None:
    labels = [op.label for op in ops_of(Kind.IMAGE, "convert")]
    assert "PNG" in labels and "JPG" in labels  # a PNG has no PNG petal, an image does


def test_ticking_petals_pins_them_in_list_order(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    window.show_tab("Wheels")
    items = window.wheels.list
    ids = [items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(items.count())]
    items.item(2).setCheckState(Qt.CheckState.Checked)
    items.item(0).setCheckState(Qt.CheckState.Checked)
    assert store.config.wheels.pinned["image"] == (ids[0], ids[2])
    settle(qtbot)
    items.item(0).setCheckState(Qt.CheckState.Unchecked)
    items.item(1).setCheckState(Qt.CheckState.Unchecked)
    assert "image" not in store.config.wheels.pinned


def test_pins_of_another_wheel_are_kept(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    wheels = dataclasses.replace(store.config.wheels, pinned={"image": ("actions.zip",)})
    store.update(dataclasses.replace(store.config, wheels=wheels))
    settle(qtbot)
    window.wheels.list.item(0).setCheckState(Qt.CheckState.Checked)
    first = window.wheels.list.item(0).data(Qt.ItemDataRole.UserRole)
    assert store.config.wheels.pinned["image"] == ("actions.zip", first)


def test_reset_usage_is_asked_for(window: SettingsWindow, qtbot: QtBot) -> None:
    with qtbot.waitSignal(window.reset_usage, timeout=1000):
        for button in window.wheels.findChildren(type(window.close_button)):
            if button.text() == "Reset usage counts":
                button.click()


# --- Launcher -------------------------------------------------------------------------------
def drop_file(target, row: int, path: Path) -> QDropEvent:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(path))])
    pos = QPointF(target.visualItemRect(target.item(row)).center())
    actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
    args = (pos, actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    for kind in (QDragEnterEvent, QDragMoveEvent):  # as a real drag from Explorer
        QApplication.sendEvent(target.viewport(), kind(*args))
    event = QDropEvent(*args)
    QApplication.sendEvent(target.viewport(), event)
    return event


def test_dropping_a_file_on_a_slot_fills_it(
    window: SettingsWindow, store: ConfigStore, tmp_path: Path
) -> None:
    window.show_tab("Launcher")
    tool = tmp_path / "tool.exe"
    tool.write_bytes(b"")
    event = drop_file(window.launcher.apps, 2, tool)
    accepted, action = event.isAccepted(), event.dropAction()
    assert accepted and action == Qt.DropAction.CopyAction  # never Move
    assert store.config.launcher.apps == (
        LauncherApp(),
        LauncherApp(),
        LauncherApp(target=str(tool)),
    )


def test_a_start_menu_app_is_saved_as_an_apps_folder_target(
    window: SettingsWindow, store: ConfigStore
) -> None:
    window.launcher.set_app(0, apps.APPS_FOLDER + "Microsoft.WindowsCalculator!App", "Calculator")
    (slot,) = store.config.launcher.apps
    assert apps.aumid_of(slot.target) == "Microsoft.WindowsCalculator!App"
    assert slot.label == "Calculator"


def test_clearing_the_last_slot_shortens_the_list(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    window.launcher.set_app(0, "C:/a.exe")
    window.launcher.set_app(1, "C:/b.exe")
    settle(qtbot)
    window.launcher._clear("apps", 1)
    assert [a.target for a in store.config.launcher.apps] == ["C:/a.exe"]


def test_slots_can_be_reordered(window: SettingsWindow, store: ConfigStore, qtbot: QtBot) -> None:
    window.launcher.set_app(0, "C:/a.exe")
    window.launcher.set_app(1, "C:/b.exe")
    settle(qtbot)
    window.launcher._reorder("apps", [1, 0, 2, 3, 4, 5, 6, 7])
    assert [a.target for a in store.config.launcher.apps] == ["C:/b.exe", "C:/a.exe"]


def test_a_site_needs_a_web_address(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    page = window.launcher
    page.show_slot("sites", 1)
    page.site_url.setText("javascript:alert(1)")
    page._save_site()
    assert store.config.launcher.sites == ()
    assert "not a web address" in page.site_error.text()
    page.site_label.setText("Docs")
    page.site_url.setText("docs.python.org")
    page._save_site()
    assert store.config.launcher.sites == (LauncherSite(), LauncherSite("Docs", "docs.python.org"))


def test_the_start_apps_list_filters_as_you_type(qtbot: QtBot, monkeypatch) -> None:
    from PySide6.QtCore import QProcess

    monkeypatch.setattr(QProcess, "start", lambda *a, **k: None)  # no PowerShell in tests
    picker = StartAppsPicker(theme.make(dark=False))
    qtbot.addWidget(picker)
    picker.set_apps([("Notepad", "notepad"), ("Calculator", "Calc!App")])
    picker.search.setText("calc")
    visible = [picker.list.item(i).text() for i in range(2) if not picker.list.item(i).isHidden()]
    assert visible == ["Calculator"]
    picker.list.setCurrentRow(0)
    with qtbot.waitSignal(picker.picked) as got:
        picker._accept()
    assert got.args == ["Calculator", "Calc!App"]


def test_add_petal_opens_its_slot(window: SettingsWindow) -> None:
    window.launcher.show_slot("sites", 5)
    assert window.launcher.stack.currentIndex() == 1
    assert window.launcher.sites.currentRow() == 5


# --- Appearance, Output, General ------------------------------------------------------------
def test_appearance_choices(window: SettingsWindow, store: ConfigStore) -> None:
    page = window.appearance
    page.theme_.buttons["Light"].click()
    page.motion.buttons["On"].click()
    page.accent.buttons["Custom"].click()
    a = store.config.appearance
    assert (a.theme, a.reduced_motion) == ("light", "on")
    assert a.accent.startswith("#")
    page.accent.buttons["Windows"].click()
    assert store.config.appearance.accent == "system"


def test_output_settings(
    window: SettingsWindow, store: ConfigStore, tmp_path: Path, monkeypatch
) -> None:
    from PySide6.QtWidgets import QFileDialog

    page = window.output
    page.dpi.setValue(150)
    page.quality.setValue(60)
    page.format_change.setChecked(True)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: str(tmp_path))
    page.location.buttons["One folder"].click()
    o = store.config.output
    assert (o.pdf_dpi, o.jpeg_quality, o.allow_format_change_for_size) == (150, 60, True)
    assert (o.location, o.fixed_folder) == ("fixed_folder", str(tmp_path))
    page.location.buttons["Next to the file"].click()
    assert store.config.output.location == "next_to_source"
    assert store.config.output.fixed_folder == str(tmp_path)  # remembered for next time


def test_one_folder_without_a_folder_stays_next_to_the_file(
    window: SettingsWindow, store: ConfigStore, monkeypatch, qtbot: QtBot
) -> None:
    from PySide6.QtWidgets import QFileDialog

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a: "")  # cancelled
    window.output.location.buttons["One folder"].click()
    assert store.config.output.location == "next_to_source"
    assert window.output.location.value == "Next to the file"


def test_general_settings_and_blocklist(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    page = window.general
    page.autostart.setChecked(True)
    page.quick.setChecked(False)
    page.add_entry("Game.exe")
    page.add_entry("game.exe")  # once, whatever the case
    assert store.config.general.autostart and not store.config.launcher.enabled
    assert store.config.general.blocklist == ("Game.exe",)
    settle(qtbot)
    page.blocklist.setCurrentRow(0)
    page._remove()
    assert store.config.general.blocklist == ()


def test_a_change_made_elsewhere_shows_up(
    window: SettingsWindow, store: ConfigStore, qtbot: QtBot
) -> None:
    general = dataclasses.replace(store.config.general, autostart=True)
    store.update(dataclasses.replace(store.config, general=general))  # e.g. a hand edit
    assert window.general.autostart.isChecked()


def test_a_closed_window_stops_listening(window: SettingsWindow, store: ConfigStore) -> None:
    window.close()
    general = dataclasses.replace(store.config.general, autostart=True)
    store.update(dataclasses.replace(store.config, general=general))  # no error, no reload
    assert not window.general.autostart.isChecked()
