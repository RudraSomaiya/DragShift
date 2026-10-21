"""Glass toasts (PRD §8.7, §8.5, FR-4.5, FR-5.5): bottom-right cards that stack upward, max 3.

The glass is rendered once when a toast first shows, from a snapshot of the desktop under it
(``ui/glass.py``), as the wheel does. A toast never moves after that: a new one takes the lowest
free slot, so its backdrop stays where it was taken. Buttons are painted, like the petals, and
report their label through ``clicked``.
"""

from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QFileInfo,
    QObject,
    QPointF,
    QRect,
    QRectF,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QFileIconProvider, QStyle, QWidget

from dragshift.config import Appearance
from dragshift.platform import screen
from dragshift.ui import glass
from dragshift.ui.theme import TOAST_MS, Theme, resolve, ui_font

__all__ = ["MAX_TOASTS", "Toast", "ToastContent", "ToastStack"]

ToastKind = Literal["done", "error", "progress"]

CARD_W, CARD_H = 360, 72  # §8.7
MARGIN = 20  # around the card: room for its shadow and the slide-up
EDGE = 16  # card to the work area's right and bottom edges ("16 px above the taskbar")
GAP = 8  # between stacked cards
RADIUS = 12  # dialog radius (§8.2)
SLIDE = 12  # the card rises this far while it fades in (§8.5)
PAD = 14
ICON = 32
BUTTON_H, BUTTON_RADIUS, BUTTON_GAP, BUTTON_PAD = 26, 8, 4, 10
BAR_H = 4
SHADOW_DY = 6
AUTO_HIDE_MS = 6000
MAX_TOASTS = 3
SPIN_MS = 1200  # one sweep of the indeterminate progress bar

_OUT_CUBIC = QEasingCurve(QEasingCurve.Type.OutCubic)


@dataclass(frozen=True)
class ToastContent:
    title: str
    subtitle: str = ""
    buttons: tuple[str, ...] = ()
    kind: ToastKind = "done"  # "progress" never hides by itself; "error" shows a warning icon
    icon: Path | None = None  # the file whose shell icon shows
    progress: float | None = None  # a progress toast's fraction, 0-1; None: indeterminate


_icons: QFileIconProvider | None = None


def _icon_for(content: ToastContent) -> QPixmap:
    global _icons
    if content.kind == "error":
        style = QApplication.style()
        return style.standardIcon(QStyle.StandardPixmap.SP_MessageBoxWarning).pixmap(ICON, ICON)
    if content.icon is None:
        return QPixmap()
    _icons = _icons or QFileIconProvider()
    return _icons.icon(QFileInfo(str(content.icon))).pixmap(ICON, ICON)


_shadows: dict[tuple[bool, float], QImage] = {}


def _shadow(theme: Theme, dpr: float) -> QImage:
    """The card's soft shadow, blurred once per mode and DPR."""
    key = (theme.dark, dpr)
    if key not in _shadows:
        w, h = CARD_W + 2 * MARGIN, CARD_H + 2 * MARGIN
        img = QImage(round(w * dpr), round(h * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        img.setDevicePixelRatio(dpr)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.shadow)
        p.drawRoundedRect(QRectF(MARGIN, MARGIN + SHADOW_DY, CARD_W, CARD_H), RADIUS, RADIUS)
        p.end()
        _shadows[key] = glass.blur(img)
    return _shadows[key]


class Toast(QWidget):
    clicked = Signal(str)  # a button's label
    gone = Signal()  # closed for good: faded out, or closed at once

    def __init__(self, theme: Theme, slot: int) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFixedSize(CARD_W + 2 * MARGIN, CARD_H + 2 * MARGIN)
        self.slot = slot
        self._theme = theme
        self._content = ToastContent("")
        self._icon = QPixmap()
        self._backdrop = QImage()
        self._title_font = ui_font(13, semibold=True)
        self._sub_font = ui_font(12)
        self._button_font = ui_font(12, semibold=True)
        self._hover: int | None = None  # the button under the mouse
        self._pressed: int | None = None
        self._inside = False
        self._remaining: int | None = None  # auto-hide time left, while the mouse is inside
        self._closing = False
        self._level = 0.0  # 0 hidden to 1 shown: opacity, and the slide-up (§8.5)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.dismiss)
        self._fade = QVariantAnimation(self)
        self._fade.valueChanged.connect(self._set_level)
        self._fade.finished.connect(self._faded)
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(1.0)
        self._spin.setDuration(SPIN_MS)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self.update)

    # --- public -----------------------------------------------------------------------------
    @property
    def content(self) -> ToastContent:
        return self._content

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def auto_hide_active(self) -> bool:
        return self._hide_timer.isActive()

    def set_content(self, content: ToastContent) -> None:
        """Show ``content``. A result restarts the 6 s auto-hide; a progress toast stays."""
        old = self._content
        self._content = content
        if (content.kind, content.icon) != (old.kind, old.icon) or self._icon.isNull():
            self._icon = _icon_for(content)
        if content.buttons != old.buttons:
            self._hover = self._pressed = None
        self._arm_hide_timer()
        self._arm_spin()
        self.update()

    def popup(self, card: QRect) -> None:
        """Snapshot the desktop under ``card`` (screen coordinates), then show and fade in."""
        self.setGeometry(card.adjusted(-MARGIN, -MARGIN, MARGIN, MARGIN))
        self._backdrop = self._grab()
        self.show()
        self._animate(1.0)
        self._arm_spin()

    def dismiss(self) -> None:
        """Fade out, then close and emit ``gone``."""
        if self._closing:
            return
        self._closing = True
        self._hide_timer.stop()
        self._animate(0.0)

    def close_now(self) -> None:
        """Close at once (a fourth toast needs the slot) and emit ``gone``."""
        self._closing = True
        self._fade.stop()
        self._level = 0.0
        self._faded()

    def button_rect(self, label: str) -> QRectF:
        return self._button_rects()[self._content.buttons.index(label)]

    # --- timers and motion --------------------------------------------------------------------
    def _arm_hide_timer(self) -> None:
        self._hide_timer.stop()
        self._remaining = None
        if self._content.kind == "progress" or self._closing:
            return
        if self._inside:
            self._remaining = AUTO_HIDE_MS  # runs once the mouse leaves (§8.5)
        else:
            self._hide_timer.start(AUTO_HIDE_MS)

    def _arm_spin(self) -> None:
        c = self._content
        spin = (
            self.isVisible()
            and c.kind == "progress"
            and c.progress is None
            and not self._theme.reduced_motion
        )
        if spin and self._spin.state() != QVariantAnimation.State.Running:
            self._spin.start()
        elif not spin:
            self._spin.stop()

    def _animate(self, to: float) -> None:
        self._fade.stop()
        ms = self._theme.ms(TOAST_MS)
        if ms <= 0:
            self._set_level(to)
            self._faded()
            return
        self._fade.setDuration(ms)
        self._fade.setStartValue(self._level)
        self._fade.setEndValue(to)
        self._fade.start()

    def _set_level(self, value: object) -> None:
        self._level = float(value)
        self.update()

    def _faded(self) -> None:
        if self._closing:
            self._spin.stop()
            self._hide_timer.stop()
            self.close()
            self.gone.emit()

    def _grab(self) -> QImage:
        """§8.4 steps 1-2 for the toast's window rect. Call before showing it."""
        rect = self.geometry()
        scr = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        snapshot = screen.grab(screen.physical_rect(rect, scr))
        snapshot.setDevicePixelRatio(scr.devicePixelRatio())
        return glass.blur(snapshot, glass.SATURATION)

    # --- mouse --------------------------------------------------------------------------------
    def _button_rects(self) -> list[QRectF]:
        metrics = QFontMetrics(self._button_font)
        rects: list[QRectF] = []
        right = MARGIN + CARD_W - PAD
        for label in reversed(self._content.buttons):
            w = metrics.horizontalAdvance(label) + 2 * BUTTON_PAD
            rects.insert(0, QRectF(right - w, MARGIN + (CARD_H - BUTTON_H) / 2, w, BUTTON_H))
            right -= w + BUTTON_GAP
        return rects

    def _button_at(self, pos: QPointF) -> int | None:
        return next((i for i, r in enumerate(self._button_rects()) if r.contains(pos)), None)

    def enterEvent(self, _event: QEnterEvent) -> None:
        self._inside = True
        if self._hide_timer.isActive():  # pause while hovered (§8.5)
            self._remaining = self._hide_timer.remainingTime()
            self._hide_timer.stop()

    def leaveEvent(self, _event: QEvent) -> None:
        self._inside = False
        self._hover = self._pressed = None
        self.update()
        if self._remaining is not None and not self._closing:
            self._hide_timer.start(self._remaining)
            self._remaining = None

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        index = self._button_at(event.position())
        if index != self._hover:
            self._hover = index
            self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = self._button_at(event.position())

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index, self._pressed = self._pressed, None
        if index is not None and index == self._button_at(event.position()) and not self._closing:
            self.clicked.emit(self._content.buttons[index])

    # --- painting -----------------------------------------------------------------------------
    def paintEvent(self, _event: QPaintEvent) -> None:
        t = self._theme
        p = QPainter(self)
        p.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.TextAntialiasing
        )
        p.setOpacity(self._level)
        dy = (1 - _OUT_CUBIC.valueForProgress(self._level)) * SLIDE
        card = QRectF(MARGIN, MARGIN + dy, CARD_W, CARD_H)
        path = QPainterPath()
        path.addRoundedRect(card, RADIUS, RADIUS)
        p.drawImage(QPointF(0, dy), _shadow(t, self.devicePixelRatioF()))
        p.save()
        p.setClipPath(path)
        if not self._backdrop.isNull():
            p.drawImage(0, 0, self._backdrop)  # fixed: the desktop does not slide
        p.fillPath(path, t.tint)
        p.restore()
        # The 1 px stroke and a top highlight fading out to the right (§8.4 step 5).
        p.setPen(QPen(t.stroke, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        lit = t.highlight
        clear = QColor(lit)
        clear.setAlpha(0)
        rim = QLinearGradient(card.left() + RADIUS, 0, card.right() - RADIUS, 0)
        rim.setColorAt(0, lit)
        rim.setColorAt(1, clear)
        p.setPen(QPen(rim, 1))
        p.drawLine(
            QPointF(card.left() + RADIUS, card.top() + 1.5),
            QPointF(card.right() - RADIUS, card.top() + 1.5),
        )
        p.translate(0, dy)
        self._paint_body(p)
        p.end()

    def _paint_body(self, p: QPainter) -> None:
        t, c = self._theme, self._content
        buttons = self._button_rects()
        x0 = MARGIN + PAD + ICON + 12
        x1 = (buttons[0].left() if buttons else MARGIN + CARD_W - PAD) - 12
        width = max(0.0, x1 - x0)
        if not self._icon.isNull():
            box = QRectF(MARGIN + PAD, MARGIN + (CARD_H - ICON) / 2, ICON, ICON)
            p.drawPixmap(box, self._icon, QRectF(self._icon.rect()))

        progress = c.kind == "progress"
        title_y, sub_y = (12, 31) if progress else (16, 37)
        p.setPen(t.text)
        p.setFont(self._title_font)
        title = QFontMetrics(self._title_font).elidedText(
            c.title, Qt.TextElideMode.ElideMiddle, round(width)
        )
        flags = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        p.drawText(QRectF(x0, MARGIN + title_y, width, 18), flags, title)
        if c.subtitle:
            p.setPen(t.text_secondary)
            p.setFont(self._sub_font)
            sub = QFontMetrics(self._sub_font).elidedText(
                c.subtitle, Qt.TextElideMode.ElideRight, round(width)
            )
            p.drawText(QRectF(x0, MARGIN + sub_y, width, 16), flags, sub)
        if progress:
            self._paint_bar(p, QRectF(x0, MARGIN + 52, width, BAR_H))

        p.setFont(self._button_font)
        for i, (label, rect) in enumerate(zip(c.buttons, buttons, strict=True)):
            hovered = i == self._hover
            p.setPen(QPen(t.stroke, 1))
            p.setBrush(t.accent_brush(rect, t.hover.alphaF()) if hovered else t.petal)
            p.drawRoundedRect(rect, BUTTON_RADIUS, BUTTON_RADIUS)
            p.setPen(t.on_accent() if hovered else t.text)
            p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_bar(self, p: QPainter, track: QRectF) -> None:
        t, fraction = self._theme, self._content.progress
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(t.petal)
        p.drawRoundedRect(track, BAR_H / 2, BAR_H / 2)
        if fraction is not None:
            fill = QRectF(
                track.left(), track.top(), track.width() * min(max(fraction, 0), 1), BAR_H
            )
            alpha = 1.0
        elif self._spin.state() == QVariantAnimation.State.Running:
            seg = track.width() * 0.3  # a segment sweeping left to right
            x = track.left() - seg + (track.width() + seg) * float(self._spin.currentValue())
            fill = QRectF(x, track.top(), seg, BAR_H).intersected(track)
            alpha = 1.0
        else:
            fill, alpha = track, 0.4  # indeterminate with reduced motion: a still, dim bar
        if fill.width() > 0:
            p.setBrush(t.accent_brush(track, alpha))
            p.drawRoundedRect(fill, BAR_H / 2, BAR_H / 2)


class ToastStack(QObject):
    """Up to MAX_TOASTS toasts, keyed (a job id, say). ``show`` with a known key updates that
    toast in place, so a progress toast turns into its result."""

    clicked = Signal(str, str)  # key, button label
    closed = Signal(str)  # key: the toast is gone

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._toasts: dict[str, Toast] = {}  # oldest first
        self._appearance = Appearance()

    def set_appearance(self, appearance: Appearance) -> None:
        """Theme, accent and reduced motion (§8.2, NFR-8), for toasts shown from now on."""
        self._appearance = appearance

    def get(self, key: str) -> Toast | None:
        return self._toasts.get(key)

    def keys(self) -> list[str]:
        return list(self._toasts)

    def show(self, key: str, content: ToastContent) -> None:
        toast = self._toasts.get(key)
        if toast is not None and not toast.closing:
            toast.set_content(content)
            return
        if toast is not None:
            toast.close_now()  # fading out: replace it
        if len(self._toasts) >= MAX_TOASTS:
            # Make room: the oldest result goes first; progress toasts (with Cancel) last.
            done = [k for k, t in self._toasts.items() if t.content.kind != "progress"]
            self._toasts[done[0] if done else next(iter(self._toasts))].close_now()
        used = {t.slot for t in self._toasts.values()}
        slot = min(set(range(MAX_TOASTS)) - used)
        toast = Toast(resolve(self._appearance), slot)
        # partial, not a lambda over `toast`: the connection must not keep the toast alive.
        toast.clicked.connect(partial(self.clicked.emit, key))
        toast.gone.connect(partial(self._remove, key))
        self._toasts[key] = toast
        toast.set_content(content)
        toast.popup(self._card_rect(slot))

    def dismiss(self, key: str) -> None:
        toast = self._toasts.get(key)
        if toast is not None:
            toast.dismiss()

    def close_all(self) -> None:
        for toast in list(self._toasts.values()):
            toast.close_now()

    @staticmethod
    def _card_rect(slot: int) -> QRect:
        """Bottom-right of the primary screen's work area, ``slot`` cards up (§8.7)."""
        area = QGuiApplication.primaryScreen().availableGeometry()
        right = area.right() + 1 - EDGE
        bottom = area.bottom() + 1 - EDGE - slot * (CARD_H + GAP)
        return QRect(right - CARD_W, bottom - CARD_H, CARD_W, CARD_H)

    def _remove(self, key: str) -> None:
        """A toast is gone. A toast emits ``gone`` once, and a replacement for its key is only
        added after that, so the toast under ``key`` is always the one that went."""
        toast = self._toasts.pop(key, None)
        if toast is not None:
            self.closed.emit(key)
            toast.deleteLater()
