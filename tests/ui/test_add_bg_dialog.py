"""The Add BG dialog (PRD §8.6): swatches, sliders, ratio, the live preview and Save."""

from pathlib import Path

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage
from pytestqt.qtbot import QtBot

from dragshift.engine import backdrop
from dragshift.ui import theme
from dragshift.ui.dialogs import add_bg, crop
from dragshift.ui.dialogs.add_bg import AddBgDialog, render_preview

THEME = theme.make(dark=True)
SIZE = (400, 200)


def picture(path: Path, size: tuple[int, int] = SIZE) -> Path:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    assert image.save(str(path))
    return path


def red(size: tuple[int, int] = (100, 50)) -> QImage:
    image = QImage(*size, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.red)
    return image


@pytest.fixture(autouse=True)
def tall_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crop, "preview_limit", lambda: crop.PREVIEW)  # the offscreen one is small
    monkeypatch.setattr(add_bg, "preview_limit", lambda: crop.PREVIEW)


def made(qtbot: QtBot, tmp_path: Path, size: tuple[int, int] = SIZE) -> AddBgDialog:
    dialog = AddBgDialog([picture(tmp_path / "a.png", size)], THEME)
    qtbot.addWidget(dialog)
    return dialog


def test_the_defaults_are_the_spec(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    assert dialog.params() == {
        "bg": {"kind": "gradient", "colors": ["#A1C4FD", "#C2E9FB"]},
        "padding": 96,
        "corners": 24,
        "shadow": 24,
        "ratio": "Auto",
    }
    assert dialog.windowTitle() == "Add BG"


def test_there_are_eight_gradients_and_eight_solids(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    names = list(backdrop.GRADIENTS)
    assert names == ["Glacier", "Aurora", "Mint", "Dusk", "Mono", "Peach", "Ocean", "Candy"]
    assert len(backdrop.SOLIDS) == 8
    assert all(n in dialog.swatches for n in names) and "Image…" in dialog.swatches
    assert all(s in dialog.swatches for s in backdrop.SOLIDS)
    maxima = (dialog.padding.maximum(), dialog.corners.maximum(), dialog.shadow.maximum())
    assert maxima == (400, 200, 100)
    assert list(dialog.ratio.buttons) == ["Auto", "1:1", "16:9", "4:5"]


def test_clicking_swatches_picks_a_gradient_or_a_solid(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    dialog.swatches["Ocean"].click()
    ocean = list(backdrop.GRADIENTS["Ocean"])
    assert dialog.params()["bg"] == {"kind": "gradient", "colors": ocean}
    dialog.swatches["Charcoal"].click()
    assert dialog.params()["bg"] == {"kind": "solid", "color": "#1F2937"}
    assert not dialog.swatches["Ocean"].isChecked()


def test_the_colour_picker_makes_a_solid_and_cancel_keeps_the_old_one(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = made(qtbot, tmp_path)
    monkeypatch.setattr(add_bg.QColorDialog, "getColor", lambda *a, **k: QColor("#123456"))
    dialog.custom_swatch.click()
    assert dialog.params()["bg"] == {"kind": "solid", "color": "#123456"}
    dialog.swatches["Mint"].click()
    monkeypatch.setattr(add_bg.QColorDialog, "getColor", lambda *a, **k: QColor())  # cancelled
    dialog.custom_swatch.click()
    assert dialog.params()["bg"]["kind"] == "gradient" and dialog.swatches["Mint"].isChecked()


def test_the_image_swatch_picks_a_file_and_a_cancel_or_junk_file_changes_nothing(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = made(qtbot, tmp_path)
    wall = picture(tmp_path / "wall.png", (60, 60))
    monkeypatch.setattr(add_bg.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    dialog.image_swatch.click()
    assert dialog.params()["bg"]["kind"] == "gradient" and dialog.swatches["Glacier"].isChecked()
    junk = tmp_path / "junk.png"
    junk.write_bytes(b"not a picture")
    monkeypatch.setattr(add_bg.QFileDialog, "getOpenFileName", lambda *a, **k: (str(junk), ""))
    dialog.image_swatch.click()
    assert dialog.params()["bg"]["kind"] == "gradient"
    monkeypatch.setattr(add_bg.QFileDialog, "getOpenFileName", lambda *a, **k: (str(wall), ""))
    dialog.image_swatch.click()
    assert dialog.params()["bg"] == {"kind": "image", "path": str(wall)}


def test_sliders_and_ratio_change_the_params_and_the_preview(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    before = dialog.view.result.size()
    dialog.padding.setValue(0)
    dialog.corners.setValue(10)
    dialog.shadow.setValue(0)
    dialog.ratio.set_value("1:1")
    params = dialog.params()
    assert (params["padding"], params["corners"], params["shadow"], params["ratio"]) == (
        0, 10, 0, "1:1")  # fmt: skip
    after = dialog.view.result
    assert after.width() == after.height() and after.size() != before


def test_the_preview_fits_its_area_and_keeps_the_canvas_shape(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path, (3000, 1000))
    box, result = dialog.box, dialog.view.result
    assert box.width() <= 800 and box.height() <= 800
    assert result.width() <= box.width() and result.height() <= box.height()
    (cw, ch), _ = backdrop.layout((3000, 1000), 96, "Auto")
    assert abs(result.width() / result.height() - cw / ch) < 0.02


def test_a_short_screen_makes_a_smaller_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(add_bg, "preview_limit", lambda: 300)
    box = add_bg.preview_box()
    assert box.width() <= 300 and box.height() <= 160


def test_the_preview_paints_the_padding_in_the_backdrop_and_the_picture_inside() -> None:
    solid = {"kind": "solid", "color": "#00ff00"}
    params = backdrop.DEFAULTS | {"bg": solid, "padding": 50, "corners": 0, "shadow": 0}
    out = render_preview(red(), QSize(100, 50), params, QSize(400, 400), None)
    assert out.size() == QSize(400, 300)  # a 200 x 150 canvas at 2x
    assert QColor(out.pixel(2, 2)) == QColor("#00ff00")
    assert QColor(out.pixel(200, 150)) == QColor("red")


def test_a_shadow_darkens_the_backdrop_under_the_picture() -> None:
    white = {"kind": "solid", "color": "#ffffff"}
    params = backdrop.DEFAULTS | {"bg": white, "padding": 60, "corners": 0, "shadow": 80}
    out = render_preview(red(), QSize(100, 50), params, QSize(220, 170), None)
    assert QColor(out.pixel(2, 2)).lightness() > 250
    assert QColor(out.pixel(110, 60 + 50 + 6)).lightness() < 235


def test_reset_restores_the_defaults(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    dialog.swatches["Black"].click()
    dialog.padding.setValue(300)
    dialog.shadow.setValue(90)
    dialog.ratio.set_value("4:5")
    dialog.reset.click()
    assert dialog.params() == backdrop.DEFAULTS
    assert dialog.swatches["Glacier"].isChecked()


def test_save_accepts(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    with qtbot.waitSignal(dialog.accepted):
        dialog.save.click()


def test_a_format_qt_cannot_read_has_no_preview_but_can_be_saved(
    qtbot: QtBot, tmp_path: Path
) -> None:
    heic = tmp_path / "a.heic"
    heic.write_bytes(b"\0" * 64)
    dialog = AddBgDialog([heic], THEME)
    qtbot.addWidget(dialog)
    assert dialog.view.result is None and dialog.save.isEnabled()
    dialog.padding.setValue(10)  # still no crash
    assert dialog.params()["padding"] == 10


def test_the_dialog_follows_a_theme_change(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = made(qtbot, tmp_path)
    light = theme.make(dark=False)
    dialog.set_theme(light)
    assert dialog.view.theme is light and all(s.theme is light for s in dialog.swatches.values())


def test_a_shadow_follows_the_pictures_transparency() -> None:
    clear = QImage(100, 50, QImage.Format.Format_ARGB32_Premultiplied)
    clear.fill(Qt.GlobalColor.transparent)
    white = {"kind": "solid", "color": "#ffffff"}
    params = backdrop.DEFAULTS | {"bg": white, "padding": 60, "corners": 0, "shadow": 80}
    out = render_preview(clear, QSize(100, 50), params, QSize(220, 170), None)
    assert all(
        QColor(out.pixel(x, y)).lightness() > 250 for x in (2, 110, 200) for y in (2, 90, 120)
    )
    out = render_preview(red(), QSize(100, 50), params, QSize(220, 170), None)
    assert QColor(out.pixel(110, 60 + 50 + 6)).lightness() < 235


def test_swatches_have_names_and_show_where_the_keyboard_is(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = made(qtbot, tmp_path)
    charcoal = dialog.swatches["Charcoal"]
    assert charcoal.accessibleName() == charcoal.toolTip() == "Charcoal"
    plain = charcoal.grab().toImage()
    monkeypatch.setattr(add_bg.Swatch, "hasFocus", lambda self: True)
    assert charcoal.grab().toImage() != plain  # the focus ring


def test_a_picked_image_or_colour_repaints_its_swatch(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialog = made(qtbot, tmp_path)
    painted: list[add_bg.Swatch] = []
    monkeypatch.setattr(add_bg.Swatch, "update", lambda self, *a: painted.append(self))
    wall = picture(tmp_path / "wall.png", (60, 60))
    monkeypatch.setattr(add_bg.QFileDialog, "getOpenFileName", lambda *a, **k: (str(wall), ""))
    dialog.image_swatch.click()
    assert dialog.image_swatch in painted
    monkeypatch.setattr(add_bg.QColorDialog, "getColor", lambda *a, **k: QColor("#123456"))
    dialog.custom_swatch.click()
    assert dialog.custom_swatch in painted
