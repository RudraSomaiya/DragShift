"""The Crop dialog (PRD §8.6): box maths, the ratio and W/H fields, Reset / Apply."""

from pathlib import Path

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from pytestqt.qtbot import QtBot

from dragshift.ui import theme
from dragshift.ui.dialogs import crop
from dragshift.ui.dialogs.crop import (
    PAD,
    PREVIEW,
    CropDialog,
    fit_ratio,
    move_box,
    resize_box,
    with_extent,
)

THEME = theme.make(dark=True)
SIZE = (400, 200)


def picture(path: Path, size: tuple[int, int] = SIZE) -> Path:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(path))
    return path


def test_free_resize_moves_only_the_dragged_edges() -> None:
    assert resize_box((0, 0, 400, 200), "br", 300, 100, SIZE, None) == (0, 0, 300, 100)
    assert resize_box((0, 0, 400, 200), "l", 50, 999, SIZE, None) == (50, 0, 400, 200)


def test_a_box_stays_inside_the_picture_and_above_the_minimum() -> None:
    assert resize_box((0, 0, 400, 200), "br", 900, 900, SIZE, None) == (0, 0, 400, 200)
    l, t, r, b = resize_box((100, 50, 300, 150), "tl", 500, 500, SIZE, None)
    assert (r - l, b - t) == (8, 8)


@pytest.mark.parametrize("handle", ["tl", "tr", "bl", "br", "l", "r", "t", "b"])
def test_a_ratio_is_kept_by_every_handle(handle: str) -> None:
    for x, y in ((60, 40), (390, 190), (10, 10)):
        l, t, r, b = resize_box((100, 50, 300, 150), handle, x, y, SIZE, 2.0)
        assert 0 <= l < r <= 400 and 0 <= t < b <= 200
        assert abs((r - l) / (b - t) - 2.0) < 0.05


def test_move_stops_at_the_edges() -> None:
    assert move_box((10, 10, 110, 60), -50, 500, SIZE) == (0, 150, 100, 200)


def test_choosing_a_ratio_fits_the_largest_centred_box() -> None:
    assert fit_ratio((0, 0, 400, 200), 1.0) == (100, 0, 300, 200)
    assert fit_ratio((0, 0, 400, 200), 9 / 16) == (144, 0, 256, 200)


def test_typed_sizes_follow_the_ratio_and_stay_inside() -> None:
    assert with_extent((0, 0, 400, 200), SIZE, 1.0, width=100) == (0, 0, 100, 100)
    assert with_extent((0, 0, 100, 100), SIZE, 1.0, height=500) == (0, 0, 200, 200)
    assert with_extent((350, 0, 400, 50), SIZE, None, width=100) == (300, 0, 400, 50)


@pytest.fixture(autouse=True)
def tall_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crop, "preview_limit", lambda: PREVIEW)  # the offscreen screen is small


def test_the_preview_shrinks_to_fit_a_short_screen(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crop, "preview_limit", lambda: 400)
    dialog = CropDialog([picture(tmp_path / "a.png", (700, 1400))], THEME)
    qtbot.addWidget(dialog)
    assert (dialog.view.image.width(), dialog.view.image.height()) == (200, 400)
    assert dialog.view.size_px == (700, 1400)  # the fields still count real pixels


def test_the_preview_limit_leaves_room_for_the_rest_of_the_dialog(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()  # the real one
    assert 100 <= max(crop.preview_limit(), 100) <= PREVIEW


def test_the_preview_is_fitted_to_560(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CropDialog([picture(tmp_path / "a.png", (1400, 700))], THEME)
    qtbot.addWidget(dialog)
    assert (dialog.view.image.width(), dialog.view.image.height()) == (PREVIEW, PREVIEW // 2)
    assert dialog.view.size_px == (1400, 700)
    assert (dialog.width_field.value(), dialog.height_field.value()) == (1400, 700)


def test_the_orientation_is_applied_to_the_preview(qtbot: QtBot, tmp_path: Path) -> None:
    from PIL import Image

    src = tmp_path / "phone.jpg"
    exif = Image.Exif()
    exif[0x0112] = 6  # stored 400x200, upright 200x400
    Image.new("RGB", SIZE).save(src, exif=exif)
    dialog = CropDialog([src], THEME)
    qtbot.addWidget(dialog)
    assert dialog.view.size_px == (200, 400)
    assert dialog.view.image.height() > dialog.view.image.width()


def test_a_ratio_and_the_fields(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CropDialog([picture(tmp_path / "a.png")], THEME)
    qtbot.addWidget(dialog)
    dialog.ratio.set_value("1:1")
    assert (dialog.width_field.value(), dialog.height_field.value()) == (200, 200)
    dialog.width_field.setValue(100)  # editing W moves H with the ratio
    assert dialog.height_field.value() == 100
    dialog.ratio.set_value("Free")
    dialog.height_field.setValue(50)
    assert (dialog.width_field.value(), dialog.height_field.value()) == (100, 50)
    assert dialog.params() == {"crop": [0.25, 0.0, 0.5, 0.25]}


def test_reset_restores_the_whole_picture_and_free_ratio(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CropDialog([picture(tmp_path / "a.png")], THEME)
    qtbot.addWidget(dialog)
    dialog.ratio.set_value("16:9")
    dialog._reset()
    assert dialog.ratio.value == "Free"
    assert dialog.params() == {"crop": [0.0, 0.0, 1.0, 1.0]}


def test_dragging_a_handle_with_the_mouse(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CropDialog([picture(tmp_path / "a.png")], THEME)
    qtbot.addWidget(dialog)
    view = dialog.view
    view.show()
    scale = view.image.width() / 400
    corner = QPointF(PAD + 400 * scale, PAD + 200 * scale).toPoint()
    target = QPointF(PAD + 200 * scale, PAD + 100 * scale).toPoint()
    QTest.mousePress(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, corner)
    assert view._drag is not None  # the thirds show while dragging
    QTest.mouseMove(view, target)
    QTest.mouseRelease(view, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target)
    assert view._drag is None
    assert view.box == (0, 0, 200, 100)
    assert (dialog.width_field.value(), dialog.height_field.value()) == (200, 100)


def test_apply_accepts_and_a_format_qt_cannot_read_has_no_apply(
    qtbot: QtBot, tmp_path: Path
) -> None:
    dialog = CropDialog([picture(tmp_path / "a.png")], THEME)
    qtbot.addWidget(dialog)
    with qtbot.waitSignal(dialog.accepted):
        dialog.apply.click()
    heic = tmp_path / "a.heic"
    heic.write_bytes(b"\0" * 64)
    dialog = CropDialog([heic], THEME)
    qtbot.addWidget(dialog)
    assert dialog.view.image is None and not dialog.apply.isEnabled()
