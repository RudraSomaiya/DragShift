"""The Crop dialog (PRD §8.6, FR-2.5): the image fitted to 560 x 560 with a crop rectangle (8
handles, rule-of-thirds while dragging), an aspect ratio, and W / H fields in image pixels.

It only collects params: the App submits the job when it is accepted. ``params()`` is the box as
fractions of the picture, ``{"crop": [left, top, right, bottom]}``, so the worker applies it to the
upright image and several dropped pictures get the same relative crop (the first is previewed).

The preview is decoded by Qt, so HEIC and AVIF have none and cannot be cropped here.

UI-process module: it must never import an engine library (CLAUDE.md rule 2).
"""

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QImage,
    QImageReader,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox, QWidget

from dragshift.ui.dialogs.base_glass import GlassDialog, Segmented, primary_button, secondary_button
from dragshift.ui.theme import Theme

PREVIEW = 560  # the picture is fitted into PREVIEW x PREVIEW
CHROME = 300  # what the dialog needs besides the picture: title bar, ratio, W/H, footer, margins
PAD = 10  # room around the picture so the edge handles can be grabbed
HANDLE = 10  # drawn size of a handle, px
GRAB = 10  # how close the pointer must be to a handle to take it, px
MIN_SIDE = 8  # smallest crop side, image px
RATIOS = {"Free": None, "1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4}

Box = tuple[int, int, int, int]  # left, top, right, bottom, in image pixels
# handle -> which edge it moves on each axis (-1 the left/top one, 1 the right/bottom one)
HANDLES = {"tl": (-1, -1), "t": (0, -1), "tr": (1, -1), "r": (1, 0),
           "br": (1, 1), "b": (0, 1), "bl": (-1, 1), "l": (-1, 0)}  # fmt: skip
CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
           "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
           "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
           "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor}  # fmt: skip


# --- The box maths (pure, so it is tested without a mouse) ------------------------------------
def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def resize_box(box: Box, handle: str, x: float, y: float, size: tuple[int, int],
               ratio: float | None) -> Box:  # fmt: skip
    """``box`` with ``handle`` dragged to (x, y), kept inside ``size``. With a ratio the
    opposite corner (or edge) stays put and the box keeps that ratio."""
    l, t, r, b = box
    width, height = size
    sx, sy = HANDLES[handle]
    x, y = _clamp(x, 0, width), _clamp(y, 0, height)
    if ratio is None:
        if sx < 0:
            l = round(min(x, r - MIN_SIDE))
        elif sx > 0:
            r = round(max(x, l + MIN_SIDE))
        if sy < 0:
            t = round(min(y, b - MIN_SIDE))
        elif sy > 0:
            b = round(max(y, t + MIN_SIDE))
        return l, t, r, b
    ax, ay = (r if sx < 0 else l), (b if sy < 0 else t)  # the fixed corner or edge
    room_w = ax if sx < 0 else width - ax
    room_h = ay if sy < 0 else height - ay
    least_w = max(MIN_SIDE, MIN_SIDE * ratio)
    if sx and sy:  # a corner: the larger of the two pointer distances sets the size
        w = max(abs(x - ax), abs(y - ay) * ratio)
        w = round(min(max(w, least_w), room_w, room_h * ratio))
        h = round(w / ratio)
        return (ax - w if sx < 0 else ax), (ay - h if sy < 0 else ay), \
            (ax if sx < 0 else ax + w), (ay if sy < 0 else ay + h)  # fmt: skip
    if sx:  # a side edge: grows about the vertical centre
        cy = (t + b) / 2
        w = abs(x - ax)
        w = round(min(max(w, least_w), room_w, 2 * min(cy, height - cy) * ratio))
        h = round(w / ratio)
        top = round(_clamp(cy - h / 2, 0, height - h))
        return (ax - w if sx < 0 else ax), top, (ax if sx < 0 else ax + w), top + h
    cx = (l + r) / 2  # a top or bottom edge: grows about the horizontal centre
    h = abs(y - ay)
    h = round(min(max(h, least_w / ratio), room_h, 2 * min(cx, width - cx) / ratio))
    w = round(h * ratio)
    left = round(_clamp(cx - w / 2, 0, width - w))
    return left, (ay - h if sy < 0 else ay), left + w, (ay if sy < 0 else ay + h)


def move_box(box: Box, dx: float, dy: float, size: tuple[int, int]) -> Box:
    l, t, r, b = box
    w, h = r - l, b - t
    left = round(_clamp(l + dx, 0, size[0] - w))
    top = round(_clamp(t + dy, 0, size[1] - h))
    return left, top, left + w, top + h


def fit_ratio(box: Box, ratio: float) -> Box:
    """The largest box of this ratio inside ``box``, centred in it."""
    l, t, r, b = box
    w, h = r - l, b - t
    nw, nh = (round(h * ratio), h) if w / h > ratio else (w, round(w / ratio))
    left, top = l + (w - nw) // 2, t + (h - nh) // 2
    return left, top, left + nw, top + nh


def with_extent(box: Box, size: tuple[int, int], ratio: float | None, *,
                width: int | None = None, height: int | None = None) -> Box:  # fmt: skip
    """``box`` made ``width`` or ``height`` px (the other follows a ratio), from its top left;
    pulled back inside the picture."""
    l, t, r, b = box
    w, h = r - l, b - t
    if width is not None:
        w = width
        h = round(w / ratio) if ratio else h
    elif height is not None:
        h = height
        w = round(h * ratio) if ratio else w
    if ratio:  # both sides must fit, so the larger one limits the other
        w = min(w, size[0], round(size[1] * ratio))
        h = round(w / ratio)
    w, h = round(_clamp(w, MIN_SIDE, size[0])), round(_clamp(h, MIN_SIDE, size[1]))
    left, top = min(l, size[0] - w), min(t, size[1] - h)
    return left, top, left + w, top + h


def preview_limit() -> int:
    """PREVIEW, or less on a screen too short to hold the dialog around it."""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    return min(PREVIEW, screen.availableGeometry().height() - CHROME) if screen else PREVIEW


def load_preview(path: Path, limit: int | None = None) -> tuple[QImage, QSize] | None:
    """The picture upright and fitted to ``limit`` (default ``preview_limit()``), with its full
    size; None if Qt cannot read it."""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)  # apply the EXIF orientation, as the worker does
    raw = reader.size()
    if not raw.isValid():
        return None
    # size() is the stored one: a quarter turn swaps the sides.
    full = raw.transposed() if int(reader.transformation().value) & 4 else raw
    limit = limit or max(preview_limit(), 100)
    fit = full.scaled(limit, limit, Qt.AspectRatioMode.KeepAspectRatio)
    reader.setScaledSize(fit.transposed() if full != raw else fit)  # decode no more than shown
    image = reader.read()
    return None if image.isNull() else (image, full)


class CropView(QWidget):
    """The picture with its crop rectangle. ``box`` is in image pixels."""

    box_changed = Signal()

    def __init__(self, image: QImage | None, full: QSize | None, theme: Theme) -> None:
        super().__init__()
        self.theme = theme
        self.image = image
        self.size_px = (full.width(), full.height()) if full else (0, 0)
        self.box: Box = (0, 0, *self.size_px)
        self.ratio: float | None = None
        self._drag: tuple[str, QPointF, Box] | None = None  # handle or "move", press, box then
        self.setMouseTracking(True)
        if image is None:
            self.setFixedSize(PREVIEW, 160)
        else:
            self.setFixedSize(image.width() + 2 * PAD, image.height() + 2 * PAD)

    # image px <-> widget px
    def _scale(self) -> tuple[float, float]:
        return self.image.width() / self.size_px[0], self.image.height() / self.size_px[1]

    def _to_widget(self, x: float, y: float) -> QPointF:
        fx, fy = self._scale()
        return QPointF(PAD + x * fx, PAD + y * fy)

    def _to_image(self, p: QPointF) -> QPointF:
        fx, fy = self._scale()
        return QPointF((p.x() - PAD) / fx, (p.y() - PAD) / fy)

    def _rect(self) -> QRectF:
        l, t, r, b = self.box
        return QRectF(self._to_widget(l, t), self._to_widget(r, b))

    def _handle_points(self) -> dict[str, QPointF]:
        rect = self._rect()
        xs = {-1: rect.left(), 0: rect.center().x(), 1: rect.right()}
        ys = {-1: rect.top(), 0: rect.center().y(), 1: rect.bottom()}
        return {name: QPointF(xs[sx], ys[sy]) for name, (sx, sy) in HANDLES.items()}

    def _hit(self, p: QPointF) -> str | None:
        for name, at in self._handle_points().items():  # handles first: they sit on the edge
            if abs(at.x() - p.x()) <= GRAB and abs(at.y() - p.y()) <= GRAB:
                return name
        return "move" if self._rect().contains(p) else None

    def set_box(self, box: Box) -> None:
        if box != self.box:
            self.box = box
            self.update()
            self.box_changed.emit()

    def set_ratio(self, ratio: float | None) -> None:
        self.ratio = ratio
        if ratio is not None and self.image is not None:
            self.set_box(fit_ratio(self.box, ratio))

    def reset(self) -> None:
        self.set_box((0, 0, *self.size_px))

    # --- mouse ---
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.image is not None:
            hit = self._hit(event.position())
            if hit:
                self._drag = (hit, event.position(), self.box)
                self.update()  # the thirds appear

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.image is None:
            return
        if self._drag is None:
            hit = self._hit(event.position())
            self.setCursor(
                CURSORS.get(
                    hit, Qt.CursorShape.SizeAllCursor if hit else Qt.CursorShape.ArrowCursor
                )
            )
            return
        hit, start, origin = self._drag
        now = self._to_image(event.position())
        if hit == "move":
            before = self._to_image(start)
            self.set_box(move_box(origin, now.x() - before.x(), now.y() - before.y(), self.size_px))
        else:
            self.set_box(resize_box(origin, hit, now.x(), now.y(), self.size_px, self.ratio))

    def leaveEvent(self, _event) -> None:
        self.unsetCursor()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._drag is not None:
            self._drag = None
            self.update()

    # --- painting ---
    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.image is None:
            p.setPen(self.theme.text_secondary)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No preview for this format")
            return
        picture = QRectF(PAD, PAD, self.image.width(), self.image.height())
        p.drawImage(picture.topLeft(), self.image)
        crop = self._rect()
        outside = QPainterPath()
        outside.addRect(picture)
        outside.addRect(crop)  # odd-even fill: the picture minus the crop
        outside.setFillRule(Qt.FillRule.OddEvenFill)
        p.fillPath(outside, QColor(0, 0, 0, 130))
        accent = self.theme.accent
        p.setPen(QPen(accent, 2))
        p.drawRect(crop)
        if self._drag is not None:  # rule of thirds
            p.setPen(QPen(QColor(255, 255, 255, 150), 1))
            for i in (1, 2):
                x, y = crop.left() + crop.width() * i / 3, crop.top() + crop.height() * i / 3
                p.drawLine(QPointF(x, crop.top()), QPointF(x, crop.bottom()))
                p.drawLine(QPointF(crop.left(), y), QPointF(crop.right(), y))
        p.setPen(QPen(accent, 1))
        p.setBrush(self.theme.on_accent(1.0))
        for at in self._handle_points().values():
            p.drawRoundedRect(
                QRectF(at.x() - HANDLE / 2, at.y() - HANDLE / 2, HANDLE, HANDLE), 2, 2
            )


class CropDialog(GlassDialog):
    """``params()`` is ``{"crop": [left, top, right, bottom]}`` as fractions of the picture."""

    def __init__(self, paths: Sequence[Path], theme: Theme) -> None:
        super().__init__("Crop Image", theme)
        loaded = load_preview(paths[0])
        self.view = CropView(*(loaded if loaded else (None, None)), theme)
        self.ratio = Segmented(list(RATIOS))
        self.width_field, self.height_field = QSpinBox(), QSpinBox()
        for field in (self.width_field, self.height_field):
            field.setRange(1, max(self.view.size_px[0], self.view.size_px[1], 1))
            field.setSuffix(" px")
            field.setKeyboardTracking(False)  # act on Enter or leaving, not on every digit
        reset, self.apply = secondary_button("Reset"), primary_button("Apply")
        self.apply.setEnabled(self.view.image is not None)

        sizes = QHBoxLayout()
        sizes.addStretch()
        for text, field in (("W", self.width_field), ("H", self.height_field)):
            sizes.addWidget(QLabel(text))
            sizes.addWidget(field)
        footer = QHBoxLayout()
        footer.addWidget(reset)
        footer.addStretch()
        footer.addWidget(self.apply)
        self.body.addWidget(self.view, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.body.addWidget(self.ratio, alignment=Qt.AlignmentFlag.AlignHCenter)
        self.body.addLayout(sizes)
        self.body.addLayout(footer)

        self.view.box_changed.connect(self._sync_fields)
        self.ratio.changed.connect(lambda name: self.view.set_ratio(RATIOS[name]))
        self.width_field.valueChanged.connect(lambda v: self._typed(width=v))
        self.height_field.valueChanged.connect(lambda v: self._typed(height=v))
        reset.clicked.connect(self._reset)
        self.apply.clicked.connect(self.accept)
        self._sync_fields()

    def _typed(self, **extent: int) -> None:
        view = self.view
        view.set_box(with_extent(view.box, view.size_px, view.ratio, **extent))
        self._sync_fields()  # shows what the box was limited to

    def _reset(self) -> None:
        self.ratio.set_value("Free")
        self.view.reset()

    def _sync_fields(self) -> None:
        l, t, r, b = self.view.box
        for field, value in ((self.width_field, r - l), (self.height_field, b - t)):
            field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(False)
        self.width_field.setEnabled(self.view.image is not None)
        self.height_field.setEnabled(self.view.image is not None)

    def params(self) -> dict:
        if self.view.image is None:
            return {}  # nothing to crop (Apply is disabled)
        (w, h), (l, t, r, b) = self.view.size_px, self.view.box
        return {"crop": [l / w, t / h, r / w, b / h]}

    def set_theme(self, theme: Theme) -> None:
        super().set_theme(theme)
        self.view.theme = theme
        self.view.update()
