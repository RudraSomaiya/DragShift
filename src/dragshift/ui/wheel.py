"""The wheel window (PRD §5.1-§5.2, §8.3, §9.4, §9.6).

Hover and drop come from Qt's drag events: during an OLE drag the window gets no mouse events.
Painting is interim flat paint; DS-12 replaces it with the glass renderer, theme tokens and motion.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QFileInfo, QMimeData, QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QFileIconProvider, QWidget

from dragshift.engine.registry import Wheel, ops_for
from dragshift.ui import wheel_geometry as geo
from dragshift.ui.wheel_model import (
    Petal,
    WheelLayout,
    build_layout,
    input_kind,
    order_ops,
)

# Interim flat paint (see the module docstring); the accent is the Glacier fallback of §8.2.
_ACCENT = QColor("#4C8DFF")
_DISC = QColor(22, 27, 38, 205)
_PETAL = QColor(255, 255, 255, 26)
_HUB = QColor(255, 255, 255, 30)
_STROKE = QColor(255, 255, 255, 70)
_TEXT = QColor("#F2F5FA")
_TEXT_2 = QColor(242, 245, 250, 158)
_HOVER_ALPHA = 0.55  # petal.hover, dark theme is 0.65 (§8.2)
_DISABLED_OPACITY = 0.35  # §8.3


@dataclass(frozen=True)
class _Shape:
    petal: Petal
    path: QPainterPath  # around the wheel centre
    anchor: QPointF  # label position, relative to the wheel centre


def _label_font(pixel_size: int = 11) -> QFont:
    font = QFont()
    font.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
    font.setPixelSize(pixel_size)
    font.setWeight(QFont.Weight.DemiBold)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.4)
    return font


def _local_files(mime: QMimeData) -> list[Path]:
    return [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]


def _decide(event: QDragMoveEvent | QDropEvent, accept: bool) -> None:
    """FR-1.8. Report CopyAction and never MoveAction, or Explorer deletes the source on a
    Shift-drop. Never accept the proposed action: it is Move while Shift is held."""
    if accept:
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
    else:
        event.ignore()


class WheelWindow(QWidget):
    dropped = Signal(str, object)  # op_id, list[Path]: a drop on an enabled petal
    closed = Signal()  # after any close, including a drop anywhere (FR-1.9)

    MORE_DELAY_MS = 400  # hover time on More… before its ring opens (FR-2.1)

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setFixedSize(geo.WIN, geo.WIN)

        self._wheel: Wheel = "convert"
        self._files: list[Path] = []
        self._unsupported = False  # the dropped data has no local files (FR-1.10)
        self._pinned: Mapping[str, Sequence[str]] = {}
        self._usage: Mapping[str, Mapping[str, int]] = {}
        self._layout = WheelLayout()
        self._shapes: list[_Shape] = []
        self._n_main = 0  # the first _n_main shapes are the wheel, the rest the More… ring
        self._hover: int | None = None
        self._ring_open = False
        self._centre_global = QPoint()  # the wheel centre in screen coordinates
        self._icons: QFileIconProvider | None = None
        self._thumbs: dict[tuple[str, bool], QPixmap] = {}
        self._thumb = QPixmap()
        self._font = _label_font()
        self._more_timer = QTimer(self)
        self._more_timer.setSingleShot(True)
        self._more_timer.timeout.connect(self._open_ring)

    # --- public API (PRD §9.4) ------------------------------------------------------------
    def open(self, kind: Wheel, at: QPoint, prefill: Sequence[Path] = ()) -> None:
        """Show the ``kind`` wheel centred on ``at``, clamped to the monitor's work area."""
        self._wheel = kind
        self._set_files(list(prefill))
        self._place(at)
        self.show()

    def swap(self, kind: Wheel) -> None:
        """Show another wheel for the same files, in the same place (FR-1.6)."""
        self._wheel = kind
        self._relayout()

    def set_ordering(
        self,
        pinned: Mapping[str, Sequence[str]] | None = None,
        usage: Mapping[str, Mapping[str, int]] | None = None,
    ) -> None:
        """Pinned order per input kind (config) and usage counts (history). Applied the next time
        a wheel opens or swaps, so petals never move under the cursor (FR-2.3)."""
        self._pinned = pinned or {}
        self._usage = usage or {}

    @property
    def layout(self) -> WheelLayout:
        return self._layout

    @property
    def ring_open(self) -> bool:
        return self._ring_open

    def hub_text(self) -> str:
        """What the hub shows below the thumbnail (FR-1.7, FR-1.10)."""
        if self._is_unsupported():
            return "Unsupported"
        if self._hover is not None:
            return self._shapes[self._hover].petal.label
        return ""

    # --- layout -----------------------------------------------------------------------------
    def _is_unsupported(self) -> bool:
        return self._unsupported or (bool(self._files) and not self._layout.petals)

    def _set_files(self, files: list[Path]) -> None:
        self._files = files
        self._unsupported = False
        self._thumb = self._thumbnail(files[0]) if files else QPixmap()
        self._relayout()

    def _relayout(self) -> None:
        """Rebuild the petals for the current wheel and files. Resets hover and the ring."""
        ops = ops_for(self._files, self._wheel) if self._files else []
        key = input_kind(self._files)
        ops = order_ops(ops, self._pinned.get(key, ()), self._usage.get(key, {}))
        self._layout = build_layout(ops, enabled=not self._unsupported)

        main, ring = self._layout.petals, self._layout.ring
        self._n_main = len(main)
        self._shapes = self._shapes_for(main, geo.R_IN, geo.R_OUT)
        self._shapes += self._shapes_for(ring, geo.RING_IN, geo.RING_OUT)
        self._ring_open = False
        self._set_hover(None)
        if self.isVisible():
            self._apply_size(geo.WIN)
        self.update()

    @staticmethod
    def _shapes_for(petals: Sequence[Petal], r_in: float, r_out: float) -> list[_Shape]:
        paths = geo.sector_paths(len(petals), r_in, r_out)
        angles = geo.sector_angles(len(petals))
        return [
            _Shape(petal, path, geo.polar(mid, (r_in + r_out) / 2))
            for petal, path, (_, mid, _) in zip(petals, paths, angles, strict=True)
        ]

    def _place(self, at: QPoint) -> None:
        """Centre the wheel on ``at``, keeping the window (and the ring, if the wheel has one)
        inside the monitor's work area (FR-1.3)."""
        screen = QGuiApplication.screenAt(at) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        half = (geo.RING_WIN if self._layout.ring else geo.WIN) // 2
        x = max(area.left() + half, min(at.x(), area.right() + 1 - half))
        y = max(area.top() + half, min(at.y(), area.bottom() + 1 - half))
        self._centre_global = QPoint(x, y)
        self._apply_size(geo.WIN)

    def _apply_size(self, side: int) -> None:
        """Resize around the fixed wheel centre."""
        c = self._centre_global
        self.setFixedSize(side, side)
        self.setGeometry(QRect(c.x() - side // 2, c.y() - side // 2, side, side))

    def _centre(self) -> QPointF:
        return QPointF(self.width() / 2, self.height() / 2)

    def _open_ring(self) -> None:
        if self._layout.ring and not self._ring_open:
            self._ring_open = True
            self._apply_size(geo.RING_WIN)
            self.update()

    # --- hover and hit-testing --------------------------------------------------------------
    def _hit(self, pos: QPointF) -> int | None:
        active = self._shapes if self._ring_open else self._shapes[: self._n_main]
        return geo.hit_test([s.path for s in active], pos - self._centre())

    def _set_hover(self, index: int | None) -> None:
        if index == self._hover:
            return
        self._hover = index
        self.update()
        petal = self._shapes[index].petal if index is not None else None
        if petal is not None and petal.is_more and petal.enabled and not self._ring_open:
            self._more_timer.start(self.MORE_DELAY_MS)
        else:
            self._more_timer.stop()

    def _is_target(self, index: int | None) -> bool:
        """An enabled petal that runs an Op: the only place a drop is accepted (FR-1.8)."""
        if index is None:
            return False
        petal = self._shapes[index].petal
        return petal.enabled and not petal.is_more

    # --- drag and drop (FR-1.5, FR-1.8-FR-1.10) ---------------------------------------------
    def _sync(self, files: list[Path]) -> None:
        """The dropped paths are authoritative over the pre-fill (FR-1.5, FR-1.10)."""
        if not files:
            if not self._unsupported:
                self._unsupported = True
                self._relayout()
        elif files != self._files or self._unsupported:
            self._set_files(files)

    def _track(self, event: QDragMoveEvent) -> None:
        index = self._hit(event.position())
        self._set_hover(index)
        _decide(event, self._is_target(index))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        # Windows sends later moves and the drop only to a drop target that accepted the enter,
        # and the wheel opens with the cursor on the hub. So the enter is always accepted, with
        # CopyAction only. Qt follows it at once with a move, which is accepted only over an
        # enabled petal, so the effect reported to Explorer is still "none" anywhere else.
        self._sync(_local_files(event.mimeData()))
        self._set_hover(self._hit(event.position()))
        _decide(event, True)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._track(event)

    def dragLeaveEvent(self, _event) -> None:
        self._set_hover(None)

    def dropEvent(self, event: QDropEvent) -> None:
        files = _local_files(event.mimeData())
        self._sync(files)
        index = self._hit(event.position())
        accept = self._is_target(index) and bool(files)
        _decide(event, accept)
        if accept:
            self.dropped.emit(self._shapes[index].petal.op_id, files)
        QTimer.singleShot(0, self.close)  # a drop anywhere closes the wheel (FR-1.9)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._more_timer.stop()
        self._hover = None
        self._ring_open = False
        super().closeEvent(event)
        self.closed.emit()

    # --- painting ---------------------------------------------------------------------------
    def _thumbnail(self, path: Path) -> QPixmap:
        """The shell icon of the first dragged file, cached per extension (§8.3)."""
        key = (path.suffix.lower(), path.is_dir())
        if key not in self._thumbs:
            self._icons = self._icons or QFileIconProvider()
            self._thumbs[key] = self._icons.icon(QFileInfo(str(path))).pixmap(80, 80)
        return self._thumbs[key]

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        p.translate(self._centre())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(_DISC)
        p.drawEllipse(QPointF(0, 0), geo.R_DISC, geo.R_DISC)
        p.setFont(self._font)
        count = len(self._shapes) if self._ring_open else self._n_main
        for i, shape in enumerate(self._shapes[:count]):
            self._paint_petal(p, shape, hovered=i == self._hover)
        self._paint_hub(p)
        p.end()

    def _paint_petal(self, p: QPainter, shape: _Shape, *, hovered: bool) -> None:
        petal = shape.petal
        p.save()
        if not petal.enabled:
            p.setOpacity(_DISABLED_OPACITY)
        hovered = hovered and petal.enabled
        if hovered:  # scale about the wheel centre, which is the painter origin (§8.3)
            p.scale(geo.HOVER_SCALE, geo.HOVER_SCALE)
            fill = QColor(_ACCENT)
            fill.setAlphaF(_HOVER_ALPHA)
            p.fillPath(shape.path, fill)
            sheen = QRadialGradient(shape.anchor + QPointF(-14, -14), 70)
            sheen.setColorAt(0, QColor(255, 255, 255, 90))
            sheen.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillPath(shape.path, sheen)
        else:
            p.fillPath(shape.path, _PETAL)
        p.setPen(QColor("white") if hovered else _TEXT)
        text = QFontMetrics(self._font).elidedText(
            petal.label.upper(), Qt.TextElideMode.ElideRight, 72
        )
        box = QRectF(shape.anchor.x() - 40, shape.anchor.y() - 10, 80, 20)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    def _paint_hub(self, p: QPainter) -> None:
        p.setPen(QPen(_STROKE, 1))
        p.setBrush(_HUB)
        p.drawEllipse(QPointF(0, 0), geo.R_HUB, geo.R_HUB)
        if not self._thumb.isNull():
            p.drawPixmap(QRectF(-20, -30, 40, 40), self._thumb, QRectF(self._thumb.rect()))
        if len(self._files) > 1:  # count badge
            badge = QRectF(18, -42, 20, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(_ACCENT)
            p.drawEllipse(badge)
            p.setPen(QColor("white"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(len(self._files)))
        text = self.hub_text()
        if not text:
            return
        if self._is_unsupported():
            p.setPen(_TEXT_2)
            p.setFont(_label_font(10))
            p.drawText(QRectF(-40, 12, 80, 18), Qt.AlignmentFlag.AlignCenter, text)
            p.setFont(self._font)
            return
        metrics = QFontMetrics(self._font)
        text = metrics.elidedText(text.upper(), Qt.TextElideMode.ElideRight, 54)
        pill = QRectF(0, 14, max(48, metrics.horizontalAdvance(text) + 12), 18)
        pill.moveLeft(-pill.width() / 2)
        pill_fill = QColor(_ACCENT)
        pill_fill.setAlphaF(0.85)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pill_fill)
        p.drawRoundedRect(pill, 9, 9)
        p.setPen(QColor("white"))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
