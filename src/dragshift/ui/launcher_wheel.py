"""The Quick Launch wheels: Apps and Sites (PRD FR-3.3-FR-3.6, FR-2.6, §8.3).

A WheelWindow whose petals are the 8 launcher slots instead of Ops, so it has the same glass,
geometry, hover and motion. It differs where FR-3.5 says it does:

- It is clicked, not dropped on: hover follows the mouse and a click on a petal picks it.
- It may take focus, so ``1``-``8`` and Esc reach it (``platform/foreground.py``). If Windows
  refuses, ``on_input`` (fed by the hooks) still sees Esc, the digits and clicks outside.
- It closes on a pick, Esc, a click outside the disc, losing focus, or 10 s without activity.

Petal ``i`` is slot ``i``, clockwise from 12 o'clock. Empty slots show "+ Add" (FR-3.6).
The wheel only reports what was picked; app.py launches it.
"""

import math
import os
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QFileInfo, QPoint, QPointF, QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QBrush, QCursor, QKeyEvent, QMouseEvent, QPainter, QPixmap
from PySide6.QtWidgets import QFileIconProvider

from dragshift.config import Launcher, LauncherApp, LauncherSite
from dragshift.launcher import apps, sites
from dragshift.platform.foreground import bring_to_front
from dragshift.platform.hooks import InputKind, RawInput
from dragshift.ui import wheel_geometry as geo
from dragshift.ui.theme import label_font
from dragshift.ui.wheel import WheelWindow, _Shape
from dragshift.ui.wheel_model import Petal, WheelLayout

SLOTS = 8  # FR-3.3, FR-3.4
INACTIVITY_MS = 10_000  # FR-3.5
ICON = 24  # px, above the label
ADD_LABEL = "+ Add"
TITLES = {"apps": "APPS", "sites": "SITES"}
VK_ESCAPE = 0x1B
VK_1, VK_NUMPAD1 = 0x31, 0x61  # 1-8 on the top row and on the number pad

_LABEL_DROP = 10  # with an icon, the label sits this far below the petal's middle
_ICON_RISE = 12  # ...and the icon this far above it
_ADD_OPACITY = 0.6  # "+ Add" petals at rest


def slot_of(op_id: str) -> tuple[str, int, bool]:
    """``"apps:3"`` -> ("apps", 3, False); ``"sites:5:add"`` -> ("sites", 5, True)."""
    kind, index, *add = op_id.split(":")
    return kind, int(index), bool(add)


def monogram(text: str, size: int, brush_for: Callable[[QRectF], QBrush]) -> QPixmap:
    """A letter tile for slots without an icon (Store apps, sites without a favicon)."""
    pm = QPixmap(size * 2, size * 2)  # drawn at 2x, so it stays sharp on 200 % screens
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    box = QRectF(0, 0, size, size)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(brush_for(box))
    p.drawRoundedRect(box, size * 0.28, size * 0.28)
    p.setFont(label_font(int(size * 0.5)))
    p.setPen(Qt.GlobalColor.white)
    letter = next((c for c in text if c.isalnum()), "?").upper()
    p.drawText(box, Qt.AlignmentFlag.AlignCenter, letter)
    p.end()
    return pm


class LauncherWheel(WheelWindow):
    picked = Signal(str, int)  # "apps" | "sites", slot index: launch it
    add_requested = Signal(str, int)  # an empty slot was picked: open Settings > Launcher

    def __init__(
        self,
        icons_dir: Path | None = None,
        focus: Callable[[int], bool] = bring_to_front,
    ) -> None:
        super().__init__()
        # FR-3.5: unlike the drag wheel, this one may take focus so it gets 1-8 and Esc.
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)
        self.setAcceptDrops(False)
        self.setMouseTracking(True)
        self.winId()  # the flags above re-create the native window: do it now, not on open
        self._focus = focus
        self._kind = "apps"
        self._launcher = Launcher()
        self._icons_dir = icons_dir or sites.icons_dir()
        self._app_icons: dict[str, QPixmap] = {}  # target -> shell icon
        self._petal_icons: dict[int, QPixmap] = {}  # petal index -> icon, for the open wheel
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.timeout.connect(self.close)

    # --- public API -------------------------------------------------------------------------
    @property
    def kind(self) -> str:
        return self._kind

    def set_launcher(self, launcher: Launcher) -> None:
        """The slots from the config. App icons load now, so opening the wheel stays fast."""
        self._launcher = launcher
        provider = QFileIconProvider()
        for app in launcher.apps[:SLOTS]:
            target = app.target.strip()
            if target and target not in self._app_icons and apps.aumid_of(target) is None:
                path = Path(os.path.expandvars(target))
                if path.exists():
                    self._app_icons[target] = provider.icon(QFileInfo(str(path))).pixmap(ICON, ICON)

    def reload_icons(self) -> None:
        """A favicon arrived: show it, if the Sites wheel is open."""
        if self.isVisible() and self._kind == "sites":
            self._relayout()

    def open(self, kind: str, at: QPoint, prefill: Sequence[Path] = ()) -> None:  # type: ignore[override]
        """Show the ``"apps"`` or ``"sites"`` wheel centred on ``at`` and take the keyboard."""
        self._kind = kind
        super().open(kind, at)  # type: ignore[arg-type]
        self.activateWindow()
        self._focus(int(self.winId()))
        self._idle.start(INACTIVITY_MS)

    @Slot(object)
    def on_input(self, ev: RawInput) -> None:
        """Hook events, for when the wheel did not get focus: Esc, 1-8, clicks outside."""
        if not self.isVisible() or self._closing.running:
            return
        if ev.kind is InputKind.KEY_DOWN:
            if ev.vk == VK_ESCAPE:
                self.close()
            elif not self.isActiveWindow():  # with focus, keyPressEvent has the digits
                for first in (VK_1, VK_NUMPAD1):
                    if first <= ev.vk < first + SLOTS:
                        self._pick(ev.vk - first)
        elif ev.kind in (InputKind.LBUTTON_DOWN, InputKind.MOUSE_OTHER) and not self._over_disc(
            QCursor.pos()
        ):
            self.close()  # a click outside (FR-3.5)

    # --- layout -----------------------------------------------------------------------------
    def _slots(self) -> Sequence[LauncherApp | LauncherSite]:
        return (self._launcher.apps if self._kind == "apps" else self._launcher.sites)[:SLOTS]

    def _label(self, slot: LauncherApp | LauncherSite) -> str:
        if isinstance(slot, LauncherApp):
            return apps.label_of(slot) if slot.target.strip() else ""
        return sites.label_of(slot) if sites.normalize_url(slot.url) else ""

    def _build_layout(self) -> WheelLayout:
        slots = self._slots()
        petals = []
        self._petal_icons = {}
        for i in range(SLOTS):
            label = self._label(slots[i]) if i < len(slots) else ""
            if not label:
                petals.append(Petal(f"{self._kind}:{i}:add", ADD_LABEL))
                continue
            petals.append(Petal(f"{self._kind}:{i}", label))
            self._petal_icons[i] = self._icon(slots[i], label)
        return WheelLayout(tuple(petals))

    def _icon(self, slot: LauncherApp | LauncherSite, label: str) -> QPixmap:
        if isinstance(slot, LauncherApp):
            icon = self._app_icons.get(slot.target.strip())
        else:
            path = sites.cached_icon(slot.url, self._icons_dir)
            icon = QPixmap(str(path)) if path else None
        if icon is None or icon.isNull():
            icon = monogram(label, ICON, self._theme.accent_brush)
        return icon

    def _shapes_for(self, petals: Sequence[Petal], r_in: float, r_out: float) -> list[_Shape]:  # type: ignore[override]
        """As the drag wheel, with the label moved down under the icon."""
        shapes = WheelWindow._shapes_for(petals, r_in, r_out)
        return [
            s
            if s.petal.op_id.endswith(":add")
            else replace(s, anchor=s.anchor + QPointF(0, _LABEL_DROP))
            for s in shapes
        ]

    # --- painting ---------------------------------------------------------------------------
    def _paint_petal(self, p: QPainter, shape: _Shape, level: float) -> None:
        if shape.petal.op_id.endswith(":add") and not level:
            p.save()
            p.setOpacity(p.opacity() * _ADD_OPACITY)  # quieter than the real slots
            super()._paint_petal(p, shape, level)
            p.restore()
            return
        super()._paint_petal(p, shape, level)
        icon = self._petal_icons.get(slot_of(shape.petal.op_id)[1])
        if icon is None:
            return
        p.save()
        if level:  # the same hover scale as the petal under it
            s = 1 + (geo.HOVER_SCALE - 1) * level
            p.scale(s, s)
        centre = shape.anchor - QPointF(0, _LABEL_DROP + _ICON_RISE)
        p.drawPixmap(
            QRectF(centre.x() - ICON / 2, centre.y() - ICON / 2, ICON, ICON),
            icon,
            QRectF(icon.rect()),
        )
        p.restore()

    def _paint_hub(self, p: QPainter) -> None:
        super()._paint_hub(p)
        if self._hover is None:  # the hovered petal's label replaces the title
            p.setPen(self._theme.text_secondary)
            p.setFont(label_font(10))
            p.drawText(
                QRectF(-40, -9, 80, 18), Qt.AlignmentFlag.AlignCenter, TITLES.get(self._kind, "")
            )
            p.setFont(self._font)

    # --- input ------------------------------------------------------------------------------
    def _over_disc(self, global_pos: QPoint) -> bool:
        d = global_pos - self._centre_global
        return math.hypot(d.x(), d.y()) <= geo.R_DISC

    def _pick(self, index: int) -> None:
        if not self.isVisible() or self._closing.running or not 0 <= index < self._n_main:
            return
        kind, slot, add = slot_of(self._shapes[index].petal.op_id)
        (self.add_requested if add else self.picked).emit(kind, slot)
        self.close()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._set_hover(self._hit(event.position()))
        self._idle.start(INACTIVITY_MS)

    def leaveEvent(self, _event: QEvent) -> None:
        self._set_hover(None)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        index = self._hit(event.position())
        if index is not None:
            self._pick(index)
        elif not self._over_disc(event.globalPosition().toPoint()):
            self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self.close()
        elif Qt.Key.Key_1 <= key <= Qt.Key.Key_8:
            self._pick(key - Qt.Key.Key_1)  # FR-2.6, number pad included
        else:
            self._idle.start(INACTIVITY_MS)

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.WindowDeactivate and self.isVisible():
            QTimer.singleShot(0, self.close)  # the user went elsewhere (FR-3.5)
        return super().event(event)

    def closeEvent(self, event) -> None:
        self._idle.stop()
        super().closeEvent(event)
