"""The wheel window (PRD §5.1-§5.2, §8.2-§8.5, §9.4, §9.6).

Hover and drop come from Qt's drag events: during an OLE drag the window gets no mouse events.
The glass is rendered once per open from a snapshot of the desktop taken before the window shows
(``ui/glass.py``). Petals, the hub, hover and motion are painted on top of it every frame.
"""

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (
    QEasingCurve,
    QElapsedTimer,
    QFileInfo,
    QMimeData,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    QSizeF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
    QRegion,
)
from PySide6.QtWidgets import QFileIconProvider, QWidget

from dragshift.config import Appearance
from dragshift.engine.registry import Wheel, children_for, ops_for
from dragshift.platform import screen
from dragshift.ui import glass
from dragshift.ui import wheel_geometry as geo
from dragshift.ui.theme import (
    CLOSE_MS,
    HOVER_MS,
    OPEN_MS,
    OPEN_OVERSHOOT,
    OPEN_SCALE_FROM,
    PETAL_STAGGER_MS,
    RING_MS,
    SWAP_MS,
    Theme,
    label_font,
    resolve,
)
from dragshift.ui.wheel_model import (
    Petal,
    WheelLayout,
    build_layout,
    input_kind,
    order_ops,
)

_DISABLED_OPACITY = 0.35  # §8.3
_PILL_ALPHA = 0.85  # the hub's label pill
_FRAME_MS = 16  # hover transition tick: 60 fps (NFR-2)


def _curve(kind: QEasingCurve.Type, overshoot: float | None = None) -> QEasingCurve:
    curve = QEasingCurve(kind)
    if overshoot is not None:
        curve.setOvershoot(overshoot)
    return curve


_OUT_BACK = _curve(QEasingCurve.Type.OutBack, OPEN_OVERSHOOT)  # §8.5 open
_IN_CUBIC = _curve(QEasingCurve.Type.InCubic)  # §8.5 close
_OUT_CUBIC = _curve(QEasingCurve.Type.OutCubic)  # §8.5 More… ring


def _mix(a: QColor, b: QColor, f: float) -> QColor:
    """``a`` at f = 0 to ``b`` at f = 1."""
    if f <= 0:
        return a
    if f >= 1:
        return b
    return QColor.fromRgbF(
        *(x + (y - x) * f for x, y in zip(a.getRgbF(), b.getRgbF(), strict=True))
    )


class _Tween:
    """A linear 0 to 1 clock over ``ms`` that repaints ``owner`` on every frame. Callers apply
    their own easing. ``start(0)`` is instant: nothing runs and ``done`` is called at once."""

    def __init__(self, owner: QWidget, done: Callable[[], None] | None = None) -> None:
        self._anim = QVariantAnimation(owner)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(owner.update)
        self._done = done
        if done is not None:
            self._anim.finished.connect(done)

    def start(self, ms: int) -> None:
        self._anim.stop()
        if ms <= 0:
            if self._done is not None:
                self._done()
            return
        self._anim.setDuration(ms)
        self._anim.start()

    def stop(self) -> None:
        """Stop where it is, without calling ``done``."""
        self._anim.stop()

    @property
    def running(self) -> bool:
        return self._anim.state() == QVariantAnimation.State.Running

    @property
    def progress(self) -> float:
        """0 to 1 while running, 1 otherwise."""
        return float(self._anim.currentValue()) if self.running else 1.0

    @property
    def elapsed_ms(self) -> float:
        return float(self._anim.currentTime()) if self.running else math.inf


@dataclass(frozen=True)
class _Shape:
    petal: Petal
    path: QPainterPath  # around the wheel centre
    anchor: QPointF  # label position, relative to the wheel centre


def _icon_key(path: Path) -> tuple[str, bool]:
    return path.suffix.lower(), path.is_dir()


def trimmed(icon: QPixmap) -> QPixmap:
    """``icon`` without its transparent margin. When an app registers no large icon (Brave's PDF
    icon, for one), Windows returns its small icon in the top-left corner of a transparent
    256 px square, which drawn whole is a speck in the corner of the hub."""
    if icon.isNull() or not icon.hasAlpha():
        return icon
    box = QRegion(icon.mask()).boundingRect()  # opaque pixels, in device pixels
    if box.isEmpty() or box == icon.rect():
        return icon
    out = icon.copy(box)
    out.setDevicePixelRatio(icon.devicePixelRatio())
    return out


def fit(icon: QPixmap, centre: QPointF, side: float) -> QRectF:
    """Where to draw ``icon``: as large as fits a ``side`` square around ``centre``, keeping its
    aspect ratio."""
    size = QSizeF(icon.size()).scaled(side, side, Qt.AspectRatioMode.KeepAspectRatio)
    return QRectF(centre.x() - size.width() / 2, centre.y() - size.height() / 2, *size.toTuple())


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
        self._n_main = 0  # the first _n_main shapes are the wheel, the rest the open ring
        self._hover: int | None = None
        self._ring_of: int | None = None  # the petal whose ring is open: More… or an Op's
        self._ring_clip: QPainterPath | None = None  # the glass under an Op's ring
        self._centre_global = QPoint()  # the wheel centre in screen coordinates
        self._icons: QFileIconProvider | None = None
        self._thumbs: dict[tuple[str, bool], QPixmap] = {}
        self._thumb = QPixmap()
        self._thumb_path: Path | None = None  # whose icon is still to load, after a frame
        self._font = label_font()
        self._more_timer = QTimer(self)
        self._more_timer.setSingleShot(True)
        self._more_timer.timeout.connect(self._open_ring)

        self._appearance = Appearance()
        self._theme = resolve(self._appearance)
        self._glass: glass.Glass | None = None
        self._levels: dict[int, float] = {}  # hover level per shape, 0 to 1 (§8.3 transition)
        self._hover_clock = QTimer(self)
        self._hover_clock.setInterval(_FRAME_MS)
        self._hover_clock.timeout.connect(self._step_hover)
        self._hover_dt = QElapsedTimer()
        self._opening = _Tween(self)
        self._closing = _Tween(self, done=self._finish_close)
        self._swapping = _Tween(self)
        self._ringing = _Tween(self)
        self._swap_from = QPixmap()  # the previous wheel, cross-faded out on a swap
        self._close_now = False
        self._warm_up()

    # --- public API (PRD §9.4) ------------------------------------------------------------
    def open(self, kind: Wheel, at: QPoint, prefill: Sequence[Path] = ()) -> None:
        """Show the ``kind`` wheel centred on ``at``, clamped to the monitor's work area."""
        self._closing.stop()
        self._swapping.stop()
        if self.isVisible():
            self.hide()  # reopened while fading out: keep the old wheel out of the snapshot
        self._theme = resolve(self._appearance)  # follows Windows changes since the last open
        self._wheel = kind
        self._set_files(list(prefill))
        self._place(at)
        self._glass = self._render_glass()
        stagger = PETAL_STAGGER_MS * max(0, self._n_main - 1)
        self._opening.start(self._theme.ms(OPEN_MS + stagger))
        self.show()

    def swap(self, kind: Wheel) -> None:
        """Show another wheel for the same files, in the same place (FR-1.6)."""
        animate = self.isVisible() and self._theme.ms(SWAP_MS) > 0
        old = self._render_content() if animate else QPixmap()
        self._wheel = kind
        self._relayout()
        self._swap_from = old
        if animate:
            self._swapping.start(self._theme.ms(SWAP_MS))

    def set_appearance(self, appearance: Appearance) -> None:
        """Theme, accent and reduced motion from the config (§8.2, NFR-8). Applied on the next
        open, when Windows' own settings are read again too."""
        self._appearance = appearance

    @property
    def theme(self) -> Theme:
        return self._theme

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
        return self._ring_of is not None

    @property
    def animating(self) -> bool:
        """An open, close, swap, ring or hover animation is running (§8.5)."""
        tweens = (self._opening, self._closing, self._swapping, self._ringing)
        return any(t.running for t in tweens) or self._hover_clock.isActive()

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
        # A new file type's shell icon costs 15-100 ms, so it loads after the first frame
        # (NFR-2) and shows up during the open animation; known types show at once.
        self._thumb_path = files[0] if files else None
        self._thumb = self._thumbs.get(_icon_key(files[0]), QPixmap()) if files else QPixmap()
        self._relayout()

    def _build_layout(self) -> WheelLayout:
        """The petals for the current wheel and files. LauncherWheel overrides this (DS-19)."""
        ops = ops_for(self._files, self._wheel) if self._files else []
        key = input_kind(self._files)
        ops = order_ops(ops, self._pinned.get(key, ()), self._usage.get(key, {}))
        children = {o.id: children_for(o, self._files) for o in ops if o.children}
        return build_layout(ops, enabled=not self._unsupported, children=children)

    def _relayout(self) -> None:
        """Rebuild the petals for the current wheel and files. Resets hover and the ring."""
        self._layout = self._build_layout()

        main = self._layout.petals
        self._n_main = len(main)
        self._shapes = self._shapes_for(main, geo.R_IN, geo.R_OUT)
        self._ring_of = None
        self._ring_clip = None
        self._ringing.stop()
        self._levels.clear()
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
        rings = self._layout.ring or any(p.ring for p in self._layout.petals)
        half = (geo.RING_WIN if rings else geo.WIN) // 2
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

    def _ring_petals(self, index: int) -> tuple[Petal, ...]:
        """What the main petal ``index`` opens on hover: the More… ring or its Op's children."""
        petal = self._shapes[index].petal
        return self._layout.ring if petal.is_more else petal.ring

    def _open_ring(self) -> None:
        """Open the hovered petal's ring, in place of any other open one. More…'s ring goes
        all the way round; an Op's ring fans out beside its petal."""
        index = self._hover
        if index is None or index >= self._n_main or index == self._ring_of:
            return
        ring = self._ring_petals(index)
        if not ring:
            return
        if self._shapes[index].petal.is_more:
            shapes = self._shapes_for(ring, geo.RING_IN, geo.RING_OUT)
            self._ring_clip = None
        else:
            _, mid, _ = geo.sector_angles(self._n_main)[index]
            r = (geo.RING_IN + geo.RING_OUT) / 2
            shapes = [
                _Shape(petal, geo.sector_path(a0, a1, geo.RING_IN, geo.RING_OUT), geo.polar(m, r))
                for petal, (a0, m, a1) in zip(ring, geo.fan_angles(mid, len(ring)), strict=True)
            ]
            self._ring_clip = QPainterPath()
            for shape in shapes:
                self._ring_clip = self._ring_clip.united(shape.path)
        self._shapes = self._shapes[: self._n_main] + shapes
        self._levels = {i: v for i, v in self._levels.items() if i < self._n_main}
        self._ring_of = index
        self._apply_size(geo.RING_WIN)
        self._ringing.start(self._theme.ms(RING_MS))
        self.update()

    def _render_glass(self) -> glass.Glass:
        """§8.4: snapshot the desktop under the wheel (and its ring) and render the glass."""
        scr = QGuiApplication.screenAt(self._centre_global) or QGuiApplication.primaryScreen()
        half = glass.SIZE // 2
        rect = QRect(self._centre_global - QPoint(half, half), QSize(glass.SIZE, glass.SIZE))
        snapshot = screen.grab(screen.physical_rect(rect, scr))
        snapshot.setDevicePixelRatio(scr.devicePixelRatio())
        return glass.render(snapshot, self._theme)

    # --- hover and hit-testing --------------------------------------------------------------
    def _hit(self, pos: QPointF) -> int | None:
        active = self._shapes if self.ring_open else self._shapes[: self._n_main]
        return geo.hit_test([s.path for s in active], pos - self._centre())

    def _set_hover(self, index: int | None) -> None:
        if index == self._hover:
            return
        self._hover = index
        self._animate_hover()
        opens = (
            index is not None
            and index < self._n_main
            and index != self._ring_of
            and self._shapes[index].petal.enabled
            and bool(self._ring_petals(index))
        )
        if opens:
            self._more_timer.start(self.MORE_DELAY_MS)
        else:
            self._more_timer.stop()

    def _animate_hover(self) -> None:
        """Fade the new hover in and the old one out over HOVER_MS (§8.3)."""
        if not self._theme.ms(HOVER_MS):
            self._levels = {} if self._hover is None else {self._hover: 1.0}
            self._hover_clock.stop()
            self.update()
        elif not self._hover_clock.isActive():
            self._hover_dt.start()
            self._hover_clock.start()

    def _step_hover(self) -> None:
        step = self._hover_dt.restart() / HOVER_MS
        levels: dict[int, float] = {}
        for i in {*self._levels, *([] if self._hover is None else [self._hover])}:
            level = self._levels.get(i, 0.0)
            level = min(1.0, level + step) if i == self._hover else max(0.0, level - step)
            if level > 0:
                levels[i] = level
        self._levels = levels
        if all(level == 1.0 and i == self._hover for i, level in levels.items()):
            self._hover_clock.stop()
        self.update()

    def _is_target(self, index: int | None) -> bool:
        """An enabled petal that runs an Op: the only place a drop is accepted (FR-1.8)."""
        if index is None:
            return False
        petal = self._shapes[index].petal
        return petal.enabled and not petal.is_more and not petal.ring

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
        if self._closing.running:  # fading out: no longer a drop target
            event.ignore()
            return
        # Windows sends later moves and the drop only to a drop target that accepted the enter,
        # and the wheel opens with the cursor on the hub. So the enter is always accepted, with
        # CopyAction only. Qt follows it at once with a move, which is accepted only over an
        # enabled petal, so the effect reported to Explorer is still "none" anywhere else.
        self._sync(_local_files(event.mimeData()))
        self._set_hover(self._hit(event.position()))
        _decide(event, True)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._closing.running:
            event.ignore()
            return
        self._track(event)

    def dragLeaveEvent(self, _event) -> None:
        self._set_hover(None)

    def dropEvent(self, event: QDropEvent) -> None:
        if self._closing.running:
            event.ignore()
            return
        files = _local_files(event.mimeData())
        self._sync(files)
        index = self._hit(event.position())
        accept = self._is_target(index) and bool(files)
        _decide(event, accept)
        if accept:
            self.dropped.emit(self._shapes[index].petal.op_id, files)
        QTimer.singleShot(0, self.close)  # a drop anywhere closes the wheel (FR-1.9)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Fade out over CLOSE_MS (§8.5), then close for real and emit ``closed``. While it
        fades the wheel takes no drops, and a second close waits for the first."""
        self._more_timer.stop()
        if self._closing.running:
            event.ignore()
            return
        if not self._close_now and self.isVisible() and self._theme.ms(CLOSE_MS):
            event.ignore()
            self._closing.start(self._theme.ms(CLOSE_MS))
            return
        self._close_now = False
        for tween in (self._opening, self._swapping, self._ringing):
            tween.stop()
        self._hover_clock.stop()
        self._hover = None
        self._levels.clear()
        self._ring_of = None
        super().closeEvent(event)
        self.closed.emit()

    def _finish_close(self) -> None:
        self._close_now = True
        self.close()

    # --- painting ---------------------------------------------------------------------------
    def _thumbnail(self, path: Path) -> QPixmap:
        """The shell icon of the first dragged file, cached per extension (§8.3)."""
        key = _icon_key(path)
        if key not in self._thumbs:
            self._icons = self._icons or QFileIconProvider()
            self._thumbs[key] = trimmed(self._icons.icon(QFileInfo(str(path))).pixmap(80, 80))
        return self._thumbs[key]

    def _load_thumb(self) -> None:
        if self._thumb_path is not None:
            self._thumb = self._thumbnail(self._thumb_path)
            self._thumb_path = None
            self.update()

    def _warm_up(self) -> None:
        """Pay one-time costs now rather than on the first wheel (NFR-2, DS-5): the first
        drawText loads the font database (0.4 s, 3 s on a cold disk), the first shell icon
        costs ~130 ms (measured in DS-12), and the native window costs its first show."""
        img = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
        p = QPainter(img)
        p.setFont(self._font)
        p.drawText(0, 8, "PNG")
        p.end()
        self._icons = QFileIconProvider()
        self._icons.icon(QFileInfo("warm-up.txt"))
        self.winId()  # creates the native window without showing it

    def _envelope(self) -> tuple[float, float]:
        """Opacity and scale of the whole wheel for the open and close animations (§8.5)."""
        opacity, scale = 1.0, 1.0
        if self._opening.running:
            t = min(1.0, self._opening.elapsed_ms / OPEN_MS)
            opacity = t
            scale = OPEN_SCALE_FROM + (1 - OPEN_SCALE_FROM) * _OUT_BACK.valueForProgress(t)
        if self._closing.running:
            opacity *= 1 - _IN_CUBIC.valueForProgress(self._closing.progress)
        return opacity, scale

    def _petal_alpha(self, i: int) -> float:
        """Petals fade in one after another, PETAL_STAGGER_MS apart (§8.5)."""
        if not self._opening.running:
            return 1.0
        return max(0.0, min(1.0, (self._opening.elapsed_ms - PETAL_STAGGER_MS * i) / OPEN_MS))

    def _clip_ring(self, p: QPainter) -> None:
        """The More… ring expands radially from its inner edge (§8.5)."""
        if self._ringing.running:
            t = _OUT_CUBIC.valueForProgress(self._ringing.progress)
            r = geo.RING_IN + (geo.RING_OUT + 1 - geo.RING_IN) * t
            circle = QPainterPath()
            circle.addEllipse(QPointF(0, 0), r, r)
            p.setClipPath(circle, Qt.ClipOperation.IntersectClip)

    @staticmethod
    def _draw_layer(p: QPainter, layer: QPixmap) -> None:
        """A glass.SIZE layer centred on the painter origin (the wheel centre)."""
        p.drawPixmap(QPointF(-glass.SIZE / 2, -glass.SIZE / 2), layer)

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        p.translate(self._centre())
        opacity, scale = self._envelope()
        p.scale(scale, scale)
        p.setOpacity(opacity)
        if self._glass is not None:
            self._draw_layer(p, self._glass.disc)
            if self.ring_open:
                p.save()
                self._clip_ring(p)
                if self._ring_clip is not None:
                    p.setClipPath(self._ring_clip, Qt.ClipOperation.IntersectClip)
                self._draw_layer(p, self._glass.ring)
                p.restore()
        if self._swapping.running and not self._swap_from.isNull():
            v = self._swapping.progress  # §8.5 swap: cross-fade
            p.setOpacity(opacity * (1 - v))
            self._draw_layer(p, self._swap_from)
            p.setOpacity(opacity * v)
        self._paint_content(p)
        p.end()
        if self._thumb_path is not None:
            QTimer.singleShot(0, self._load_thumb)

    def _render_content(self) -> QPixmap:
        """Petals and hub as they are now, as a glass.SIZE layer (for the swap cross-fade)."""
        dpr = self.devicePixelRatioF()
        pm = QPixmap(round(glass.SIZE * dpr), round(glass.SIZE * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        p.translate(glass.SIZE / 2, glass.SIZE / 2)
        self._paint_content(p)
        p.end()
        return pm

    def _paint_content(self, p: QPainter) -> None:
        """Petals, the open ring's petals and the hub, around the painter origin."""
        base = p.opacity()
        p.setFont(self._font)
        for i, shape in enumerate(self._shapes[: self._n_main]):
            p.setOpacity(base * self._petal_alpha(i))
            self._paint_petal(p, shape, self._levels.get(i, 0.0))
        p.setOpacity(base)
        if self.ring_open:
            p.save()
            self._clip_ring(p)
            for i in range(self._n_main, len(self._shapes)):
                self._paint_petal(p, self._shapes[i], self._levels.get(i, 0.0))
            p.restore()
        self._paint_hub(p)

    def _paint_petal(self, p: QPainter, shape: _Shape, level: float) -> None:
        """One petal; ``level`` is its hover transition, 0 (rest) to 1 (hovered) (§8.3)."""
        petal, t = shape.petal, self._theme
        p.save()
        if not petal.enabled:
            p.setOpacity(p.opacity() * _DISABLED_OPACITY)
            level = 0.0
        if level:  # scale about the wheel centre, which is the painter origin
            s = 1 + (geo.HOVER_SCALE - 1) * level
            p.scale(s, s)
        p.fillPath(shape.path, t.petal)
        if level:
            base = p.opacity()
            p.setOpacity(base * level)
            p.fillPath(shape.path, t.accent_brush(shape.path.boundingRect(), t.hover.alphaF()))
            sheen = QRadialGradient(shape.anchor + QPointF(-14, -14), 70)
            sheen.setColorAt(0, QColor(255, 255, 255, 90))
            sheen.setColorAt(1, QColor(255, 255, 255, 0))
            p.fillPath(shape.path, sheen)
            p.setOpacity(base)
        p.setPen(_mix(t.text, t.on_accent(), level))
        text = QFontMetrics(self._font).elidedText(
            petal.label.upper(), Qt.TextElideMode.ElideRight, 72
        )
        box = QRectF(shape.anchor.x() - 40, shape.anchor.y() - 10, 80, 20)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.restore()

    def _paint_hub(self, p: QPainter) -> None:
        t = self._theme
        p.setPen(QPen(t.stroke, 1))
        p.setBrush(t.petal)
        p.drawEllipse(QPointF(0, 0), geo.R_HUB, geo.R_HUB)
        if not self._thumb.isNull():
            box = fit(self._thumb, QPointF(0, -10), 40)
            p.drawPixmap(box, self._thumb, QRectF(self._thumb.rect()))
        if len(self._files) > 1:  # count badge
            badge = QRectF(18, -42, 20, 20)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(t.accent_brush(badge))
            p.drawEllipse(badge)
            p.setPen(t.on_accent(1.0))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(len(self._files)))
        text = self.hub_text()
        if not text:
            return
        if self._is_unsupported():
            p.setPen(t.text_secondary)
            p.setFont(label_font(10))
            p.drawText(QRectF(-40, 12, 80, 18), Qt.AlignmentFlag.AlignCenter, text)
            p.setFont(self._font)
            return
        metrics = QFontMetrics(self._font)
        text = metrics.elidedText(text.upper(), Qt.TextElideMode.ElideRight, 54)
        pill = QRectF(0, 14, max(48, metrics.horizontalAdvance(text) + 12), 18)
        pill.moveLeft(-pill.width() / 2)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.accent_brush(pill, _PILL_ALPHA))
        p.drawRoundedRect(pill, 9, 9)
        p.setPen(t.on_accent(_PILL_ALPHA))
        p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
