"""The Apps and Sites wheels (FR-3.3-FR-3.6, FR-2.6): slots, picking by click and 1-8, closing."""

from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QImage
from pytestqt.qtbot import QtBot

from dragshift.config import Appearance, Launcher, LauncherApp, LauncherSite
from dragshift.platform.hooks import InputKind, RawInput
from dragshift.ui import launcher_wheel as lw
from dragshift.ui import wheel_geometry as geo
from dragshift.ui.launcher_wheel import LauncherWheel

AT = QPoint(400, 300)  # inside the 800 x 600 offscreen screen
STILL = Appearance(theme="dark", accent="#4C8DFF", reduced_motion="on")  # no motion


@pytest.fixture
def launcher(tmp_path: Path) -> Launcher:
    exe = tmp_path / "Tool.exe"
    exe.write_bytes(b"MZ")
    return Launcher(
        apps=(
            LauncherApp(label="Code", target=str(exe)),
            LauncherApp(target="Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
            LauncherApp(label="Ghost", target=""),  # an unfilled slot
        ),
        sites=(LauncherSite(url="github.com"), LauncherSite(label="Mail", url="mail.google.com")),
    )


@pytest.fixture
def wheel(qtbot: QtBot, tmp_path: Path, launcher: Launcher) -> LauncherWheel:
    focused: list[int] = []
    w = LauncherWheel(tmp_path / "icons", focus=focused.append)
    w.focused = focused  # type: ignore[attr-defined]
    w.set_appearance(STILL)
    w.set_launcher(launcher)
    qtbot.addWidget(w)
    return w


def petal_pos(w: LauncherWheel, i: int) -> QPoint:
    _, mid, _ = geo.sector_angles(len(w.layout.petals))[i]
    p = QPointF(w.width() / 2, w.height() / 2) + geo.polar(mid, 100)
    return p.toPoint()


def recorder(signal) -> list:
    got: list = []
    signal.connect(lambda *args: got.append(args))
    return got


def labels(w: LauncherWheel) -> list[str]:
    return [p.label for p in w.layout.petals]


# --- what it shows ---------------------------------------------------------------------------
def test_eight_slots_with_empty_ones_as_add(wheel: LauncherWheel) -> None:
    wheel.open("apps", AT)
    assert labels(wheel) == ["Code", "Calculator", *["+ Add"] * 6]
    wheel.open("sites", AT)
    assert labels(wheel) == ["github.com", "Mail", *["+ Add"] * 6]
    assert wheel.kind == "sites"


def test_every_filled_slot_has_an_icon(wheel: LauncherWheel) -> None:
    wheel.open("apps", AT)
    assert sorted(wheel._petal_icons) == [0, 1]  # the exe's shell icon, a monogram for the AUMID
    assert all(not icon.isNull() for icon in wheel._petal_icons.values())


def test_a_cached_favicon_is_used_and_a_new_one_shows_up(
    wheel: LauncherWheel, tmp_path: Path
) -> None:
    wheel.open("sites", AT)
    before = wheel._petal_icons[0].toImage()
    folder = tmp_path / "icons"
    folder.mkdir()
    red = QImage(16, 16, QImage.Format.Format_ARGB32)
    red.fill(Qt.GlobalColor.red)
    red.save(str(folder / "github.com.png"))
    wheel.reload_icons()  # FaviconCache.updated
    after = wheel._petal_icons[0].toImage()
    assert after != before
    assert after.pixelColor(8, 8).red() == 255


def test_it_paints(wheel: LauncherWheel) -> None:
    wheel.open("apps", AT)
    assert not wheel.grab().isNull()


def test_it_may_take_focus(wheel: LauncherWheel) -> None:
    assert not wheel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert not wheel.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    wheel.open("apps", AT)
    assert wheel.focused == [int(wheel.winId())]


# --- picking ---------------------------------------------------------------------------------
def test_a_click_on_a_petal_picks_that_slot_and_closes(qtbot: QtBot, wheel: LauncherWheel) -> None:
    picked = recorder(wheel.picked)
    wheel.open("apps", AT)
    qtbot.mouseMove(wheel, petal_pos(wheel, 1))
    assert wheel.hub_text() == "Calculator"
    qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton, pos=petal_pos(wheel, 1))
    assert picked == [("apps", 1)]
    assert not wheel.isVisible()


def test_an_empty_slot_asks_for_settings(qtbot: QtBot, wheel: LauncherWheel) -> None:
    picked, add = recorder(wheel.picked), recorder(wheel.add_requested)
    wheel.open("sites", AT)
    qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton, pos=petal_pos(wheel, 5))
    assert (picked, add) == ([], [("sites", 5)])


def test_the_hub_is_not_a_petal(qtbot: QtBot, wheel: LauncherWheel) -> None:
    picked = recorder(wheel.picked)
    wheel.open("apps", AT)
    qtbot.mouseClick(wheel, Qt.MouseButton.LeftButton, pos=wheel.rect().center())
    assert picked == []
    assert wheel.isVisible()


@pytest.mark.parametrize(("key", "slot"), [(Qt.Key.Key_1, 0), (Qt.Key.Key_2, 1)])
def test_digit_keys_pick_clockwise(
    qtbot: QtBot, wheel: LauncherWheel, key: Qt.Key, slot: int
) -> None:
    picked = recorder(wheel.picked)
    wheel.open("apps", AT)
    qtbot.keyClick(wheel, key)
    assert picked == [("apps", slot)]


def test_a_digit_on_an_empty_slot_and_nine(qtbot: QtBot, wheel: LauncherWheel) -> None:
    picked, add = recorder(wheel.picked), recorder(wheel.add_requested)
    wheel.open("apps", AT)
    qtbot.keyClick(wheel, Qt.Key.Key_9)  # there is no ninth petal
    assert wheel.isVisible()
    qtbot.keyClick(wheel, Qt.Key.Key_8)
    assert (picked, add) == ([], [("apps", 7)])


# --- closing (FR-3.5) ------------------------------------------------------------------------
def test_esc_closes(qtbot: QtBot, wheel: LauncherWheel) -> None:
    wheel.open("apps", AT)
    qtbot.keyClick(wheel, Qt.Key.Key_Escape)
    assert not wheel.isVisible()


def test_ten_quiet_seconds_close_it(
    qtbot: QtBot, wheel: LauncherWheel, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lw, "INACTIVITY_MS", 50)
    wheel.open("apps", AT)
    qtbot.waitUntil(lambda: not wheel.isVisible(), timeout=2000)


# --- hook input, for when Windows did not give it focus ---------------------------------------
def hook(kind: InputKind, vk: int = 0) -> RawInput:
    return RawInput(kind, 0, 0, vk, 0)


def test_hook_esc_and_digits(wheel: LauncherWheel) -> None:
    picked = recorder(wheel.picked)
    wheel.open("apps", AT)
    wheel.on_input(hook(InputKind.KEY_DOWN, 0x62))  # number pad 2
    assert picked == [("apps", 1)]
    wheel.open("apps", AT)
    wheel.on_input(hook(InputKind.KEY_DOWN, lw.VK_ESCAPE))
    assert not wheel.isVisible()


def test_hook_digits_are_ignored_when_the_wheel_has_focus(
    wheel: LauncherWheel, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keyPressEvent has them then: handling both would launch twice."""
    picked = recorder(wheel.picked)
    wheel.open("apps", AT)
    monkeypatch.setattr(wheel, "isActiveWindow", lambda: True)
    wheel.on_input(hook(InputKind.KEY_DOWN, 0x31))
    assert picked == []


@pytest.mark.parametrize(
    ("cursor", "still_open"),
    [(QPoint(400 + geo.R_DISC + 5, 300), False), (QPoint(400 + 100, 300), True)],
)
def test_a_click_outside_the_disc_closes_it(
    wheel: LauncherWheel, monkeypatch: pytest.MonkeyPatch, cursor: QPoint, still_open: bool
) -> None:
    monkeypatch.setattr(lw.QCursor, "pos", staticmethod(lambda: cursor))
    wheel.open("apps", AT)
    wheel.on_input(hook(InputKind.LBUTTON_DOWN))
    assert wheel.isVisible() is still_open


def test_closed_wheels_ignore_hook_input(wheel: LauncherWheel) -> None:
    picked = recorder(wheel.picked)
    wheel.on_input(hook(InputKind.KEY_DOWN, 0x31))
    assert picked == []


# --- helpers ---------------------------------------------------------------------------------
def test_slot_ids() -> None:
    assert lw.slot_of("apps:3") == ("apps", 3, False)
    assert lw.slot_of("sites:7:add") == ("sites", 7, True)
