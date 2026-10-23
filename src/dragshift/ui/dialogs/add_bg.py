"""The Add BG dialog (PRD §8.6, S3): a live preview of the picture on its background, two rows of
swatches (8 gradients + an image, 8 solids + a colour picker), Padding / Corners / Shadow sliders
and a Ratio.

It only collects params: the App submits the job when Save is clicked. ``params()`` is the dict
``engine/backdrop.py`` describes, so several dropped pictures get the same look (the first is
previewed). The preview is QPainter work at the size of the dialog, never above 800 px; the full
render is Pillow's, in a worker, from the same numbers.

The preview is decoded by Qt, so HEIC and AVIF show "No preview" but can still be saved.

UI-process module: it must never import an engine library (CONTRIBUTING.md rule 2).
"""

import math
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QColorDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSlider,
    QWidget,
)

from dragshift.engine import backdrop
from dragshift.ui.dialogs.base_glass import GlassDialog, Segmented, primary_button, secondary_button
from dragshift.ui.dialogs.crop import load_preview, preview_limit
from dragshift.ui.theme import Theme

PREVIEW_W, PREVIEW_H = 560, 360  # the most the preview takes (<= 800: §8.6)
CHROME = 140  # taken off the Crop dialog's picture limit: this one has more controls under it
SWATCH = 30
IMAGE_SWATCH_W = 74
BG_LIMIT = 800  # a background picture is decoded at most this big
IMAGE_FILTER = "Pictures (*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff)"


def preview_box() -> QSize:
    """The preview area: PREVIEW_W x PREVIEW_H, or less on a screen too short for the dialog."""
    limit = max(preview_limit(), 100)
    return QSize(min(PREVIEW_W, limit), max(120, min(PREVIEW_H, limit - CHROME)))


def paint_background(p: QPainter, rect: QRectF, spec: dict, image: QImage | None) -> None:
    """The backdrop ``spec`` over ``rect``: Qt's version of the worker's ``_backdrop``."""
    if spec["kind"] == "solid":
        p.fillRect(rect, QColor(spec["color"]))
    elif spec["kind"] == "gradient":
        fill = QLinearGradient(rect.topLeft(), rect.bottomRight())
        fill.setColorAt(0, QColor(spec["colors"][0]))
        fill.setColorAt(1, QColor(spec["colors"][1]))
        p.fillRect(rect, fill)
    elif image is not None and not image.isNull():  # an image covers the rectangle
        zoom = max(rect.width() / image.width(), rect.height() / image.height())
        w, h = rect.width() / zoom, rect.height() / zoom
        p.drawImage(rect, image, QRectF((image.width() - w) / 2, (image.height() - h) / 2, w, h))


def _shadow(picture: QImage, at: QRectF, radius: float, sigma: float) -> tuple[QImage, int]:
    """The picture's silhouette (corners and transparency) in black, blurred by shrinking it and
    growing it again, and the margin round it that the blur needs."""
    margin = math.ceil(2 * sigma)
    w, h = max(1, round(at.width())), max(1, round(at.height()))
    shadow = QImage(w + 2 * margin, h + 2 * margin, QImage.Format.Format_ARGB32_Premultiplied)
    shadow.fill(Qt.GlobalColor.transparent)
    q = QPainter(shadow)
    q.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    inside = QRectF(margin, margin, w, h)
    clip = QPainterPath()
    clip.addRoundedRect(inside, radius, radius)
    q.setClipPath(clip)
    q.drawImage(inside, picture)
    q.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    q.fillRect(shadow.rect(), Qt.GlobalColor.black)  # keeps the alpha, drops the colour
    q.end()
    step = max(1, round(sigma * 1.5))
    if step > 1:
        small = shadow.scaled(
            max(1, shadow.width() // step), max(1, shadow.height() // step),
            Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )  # fmt: skip
        shadow = small.scaled(
            shadow.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )  # fmt: skip
    return shadow, margin


def render_preview(picture: QImage, full: QSize, params: dict, box: QSize,
                   bg_image: QImage | None) -> QImage:  # fmt: skip
    """The result at the size that fits ``box``: ``picture`` (a scaled copy of the ``full``-size
    original) on its backdrop, with the padding, corners and shadow in source pixels scaled."""
    size = (full.width(), full.height())
    (cw, ch), (x, y) = backdrop.layout(size, params["padding"], params["ratio"])
    zoom = min(box.width() / cw, box.height() / ch)
    out = QImage(max(1, round(cw * zoom)), max(1, round(ch * zoom)), QImage.Format.Format_RGB32)
    p = QPainter(out)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
    scale = out.width() / cw
    paint_background(p, QRectF(out.rect()), params["bg"], bg_image)

    at = QRectF(x * scale, y * scale, full.width() * scale, full.height() * scale)
    radius = backdrop.corner_radius(size, params["corners"]) * scale
    opacity, sigma, drop = backdrop.shadow_look(params["shadow"])
    if opacity > 0:
        shadow, margin = _shadow(picture, at, radius, sigma * scale)
        p.setOpacity(opacity)
        p.drawImage(QPointF(at.x() - margin, at.y() + drop * scale - margin), shadow)
        p.setOpacity(1)
    clip = QPainterPath()
    clip.addRoundedRect(at, radius, radius)
    p.setClipPath(clip)
    p.drawImage(at, picture)
    p.end()
    return out


class Swatch(QAbstractButton):
    """A background choice. ``spec`` is its ``bg`` param; a picked picture or colour edits it."""

    def __init__(self, name: str, spec: dict, theme: Theme, *, wide: bool = False) -> None:
        super().__init__()
        self.name, self.spec, self.theme = name, spec, theme
        self.image: QImage | None = None
        self.setCheckable(True)
        self.setToolTip(name)
        self.setAccessibleName(name)
        self.setFixedSize(IMAGE_SWATCH_W if wide else SWATCH, SWATCH)

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        shape = QPainterPath()
        shape.addRoundedRect(inner, 8, 8)
        p.save()
        p.setClipPath(shape)
        if self.spec.get("path") is None and self.spec["kind"] == "image":
            p.fillRect(inner, self.theme.petal)  # not chosen yet: a label
            p.setPen(self.theme.text)
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, "Image…")
        elif self.spec["kind"] == "custom" and "color" not in self.spec:
            p.fillRect(inner, self.theme.petal)  # no colour picked yet: a plus
            p.setPen(self.theme.text)
            p.drawText(inner, Qt.AlignmentFlag.AlignCenter, "+")
        elif self.spec["kind"] == "custom":
            p.fillRect(inner, QColor(self.spec["color"]))
        else:
            paint_background(p, inner, self.spec, self.image)
        p.restore()
        p.setPen(QPen(self.theme.accent if self.isChecked() else self.theme.stroke,
                      2 if self.isChecked() else 1))  # fmt: skip
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(shape)
        if self.hasFocus():  # a keyboard user's place: a second ring just outside
            p.setPen(QPen(self.theme.accent, 1))
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)


def _slider_row(label: str, high: int, value: int, suffix: str) -> tuple[QHBoxLayout, QSlider]:
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, high)
    slider.setValue(value)
    name, readout = QLabel(label), QLabel()
    name.setFixedWidth(64)
    readout.setFixedWidth(60)
    readout.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    slider.valueChanged.connect(lambda v: readout.setText(f"{v}{suffix}"))
    readout.setText(f"{value}{suffix}")
    row = QHBoxLayout()
    for widget in (name, slider, readout):
        row.addWidget(widget)
    return row, slider


class Preview(QWidget):
    """The rendered result, centred in a fixed area."""

    def __init__(self, box: QSize, theme: Theme) -> None:
        super().__init__()
        self.theme, self.result = theme, None
        self.setFixedSize(box)

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        if self.result is None:
            p.setPen(self.theme.text_secondary)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No preview for this format")
            return
        p.drawImage((self.width() - self.result.width()) // 2,
                    (self.height() - self.result.height()) // 2, self.result)  # fmt: skip


class AddBgDialog(GlassDialog):
    """``params()`` is the dict ``engine/backdrop.py`` describes."""

    def __init__(self, paths: Sequence[Path], theme: Theme) -> None:
        super().__init__("Add BG", theme)
        loaded = load_preview(paths[0])
        self.picture, self.full = loaded if loaded else (None, None)
        box = preview_box()
        self.view = Preview(box, theme)
        self.box = box

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.swatches: dict[str, Swatch] = {}
        first = QHBoxLayout()
        second = QHBoxLayout()
        for row in (first, second):
            row.setSpacing(6)
            row.addStretch()
        for name, colours in backdrop.GRADIENTS.items():
            self._add_swatch(first, Swatch(name, {"kind": "gradient", "colors": list(colours)},
                                           theme))  # fmt: skip
        self.image_swatch = Swatch("Image…", {"kind": "image", "path": None}, theme, wide=True)
        self._add_swatch(first, self.image_swatch)
        for name, colour in backdrop.SOLIDS.items():
            self._add_swatch(second, Swatch(name, {"kind": "solid", "color": colour}, theme))
        self.custom_swatch = Swatch("Custom colour…", {"kind": "custom"}, theme)
        self._add_swatch(second, self.custom_swatch)
        for row in (first, second):
            row.addStretch()

        d = backdrop.DEFAULTS
        pad_row, self.padding = _slider_row("Padding", backdrop.MAX_PADDING, d["padding"], " px")
        round_row, self.corners = _slider_row("Corners", backdrop.MAX_CORNERS, d["corners"], " px")
        shadow_row, self.shadow = _slider_row("Shadow", backdrop.MAX_SHADOW, d["shadow"], "")
        self.ratio = Segmented(list(backdrop.RATIOS))
        ratio_row = QHBoxLayout()
        label = QLabel("Ratio")
        label.setFixedWidth(64)
        ratio_row.addWidget(label)
        ratio_row.addWidget(self.ratio)
        ratio_row.addStretch()
        self.reset = secondary_button("Reset")
        self.save = primary_button("Save with Background")
        footer = QHBoxLayout()
        footer.addWidget(self.reset)
        footer.addStretch()
        footer.addWidget(self.save)

        self.body.addWidget(self.view, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.body.addLayout(first)
        self.body.addLayout(second)
        for row in (pad_row, round_row, shadow_row, ratio_row, footer):
            self.body.addLayout(row)

        self.current = self.swatches["Glacier"]
        self.current.setChecked(True)
        self.group.buttonClicked.connect(self._chosen)
        for slider in (self.padding, self.corners, self.shadow):
            slider.valueChanged.connect(self._refresh)
        self.ratio.changed.connect(self._refresh)
        self.reset.clicked.connect(self._reset)
        self.save.clicked.connect(self.accept)
        self._refresh()

    def _add_swatch(self, row: QHBoxLayout, swatch: Swatch) -> None:
        self.group.addButton(swatch)
        self.swatches[swatch.name] = swatch
        row.addWidget(swatch)

    # --- choosing a background ---
    def _chosen(self, swatch: QAbstractButton) -> None:
        """Image… and the colour picker ask for something first; cancelling keeps the old one."""
        if swatch is self.image_swatch:
            path, _ = QFileDialog.getOpenFileName(self, "Background picture", "", IMAGE_FILTER)
            loaded = load_preview(Path(path), BG_LIMIT) if path else None
            if loaded is None:
                self.current.setChecked(True)  # cancelled, or Qt cannot read it
                return
            self.image_swatch.spec["path"], self.image_swatch.image = path, loaded[0]
            self.image_swatch.setToolTip(Path(path).name)
        elif swatch is self.custom_swatch:
            was = self.custom_swatch.spec.get("color", self.current.spec.get("color", "#FFFFFF"))
            colour = QColorDialog.getColor(QColor(was), self, "Background colour")
            if not colour.isValid():
                self.current.setChecked(True)
                return
            self.custom_swatch.spec = {"kind": "custom", "color": colour.name()}
        self.current = swatch
        swatch.update()  # it repainted as checked while the picker was open, before its new look
        self._refresh()

    def _reset(self) -> None:
        d = backdrop.DEFAULTS
        self.swatches["Glacier"].setChecked(True)
        self.current = self.swatches["Glacier"]
        self.padding.setValue(d["padding"])
        self.corners.setValue(d["corners"])
        self.shadow.setValue(d["shadow"])
        self.ratio.set_value(d["ratio"])
        self._refresh()

    # --- params and preview ---
    def _background(self) -> dict:
        spec = self.current.spec
        if spec["kind"] == "custom":
            return {"kind": "solid", "color": spec["color"]}  # the worker knows no "custom"
        return dict(spec)

    def params(self) -> dict:
        return {
            "bg": self._background(),
            "padding": self.padding.value(),
            "corners": self.corners.value(),
            "shadow": self.shadow.value(),
            "ratio": self.ratio.value,
        }

    def _refresh(self, *_args) -> None:
        if self.picture is not None:
            bg = self.image_swatch.image if self.current is self.image_swatch else None
            self.view.result = render_preview(self.picture, self.full, self.params(), self.box, bg)
        self.view.update()

    def set_theme(self, theme: Theme) -> None:
        super().set_theme(theme)
        self.view.theme = theme
        for swatch in self.swatches.values():
            swatch.theme = theme
            swatch.update()
        self.view.update()
