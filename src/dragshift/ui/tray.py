"""The tray icon and its menu (FR-7.1): Settings…, History ▸, Pause gestures, Open log folder,
Quit.

- History lists the last jobs, newest first (FR-5.7). Each has Open, Show in folder and Undo,
  the same as its toast; a job undone already, or one that failed, has none of them.
- The icon is drawn here (a wheel in the accent colour), grey while gestures are paused.
- A click on the icon opens Settings.

UI-process module: it must never import an engine library (CLAUDE.md rule 2).
"""

import math
from datetime import datetime

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from dragshift.engine import registry
from dragshift.history import History, HistoryEntry

__all__ = ["Tray", "draw_icon", "entry_text"]

PAUSED_COLOUR = QColor("#8A8F98")
ICON_SIZES = (16, 20, 24, 32, 40, 48, 64)
APP_NAME = "DragShift"


def draw_icon(colour: QColor, size: int) -> QPixmap:
    """A six-petal wheel around a hub: the wheel the app is about."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QPointF(size / 2, size / 2)
    r_out, r_in = size * 0.48, size * 0.22
    petals = QPainterPath()
    for i in range(6):
        a0 = math.degrees(i * math.pi / 3) + 6  # a small gap between petals
        sweep = 60 - 12
        outer = QRectF(c.x() - r_out, c.y() - r_out, 2 * r_out, 2 * r_out)
        inner = QRectF(c.x() - r_in, c.y() - r_in, 2 * r_in, 2 * r_in)
        petal = QPainterPath()
        petal.arcMoveTo(outer, a0)
        petal.arcTo(outer, a0, sweep)
        petal.arcTo(inner, a0 + sweep, -sweep)
        petal.closeSubpath()
        petals.addPath(petal)
    p.fillPath(petals, colour)
    p.setPen(QPen(colour, max(1.0, size / 16)))
    p.drawEllipse(c, r_in * 0.55, r_in * 0.55)
    p.end()
    return pm


def _icon(colour: QColor) -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(draw_icon(colour, size))
    return icon


def entry_text(entry: HistoryEntry) -> str:
    """One History line: "photo.jpg — JPG · 14:02" or "✕ photo.png — JPG failed · 14:02"."""
    try:
        label = registry.get(entry.op).label.rstrip(" ▸")
    except KeyError:
        label = entry.op
    files = entry.outputs if entry.ok and entry.outputs else entry.inputs
    name = files[0].name if files else label
    if len(files) > 1:
        name = f"{name} +{len(files) - 1}"
    try:
        when = datetime.fromisoformat(entry.ts).strftime("%H:%M")
    except ValueError:
        when = ""
    what = f"{label} failed" if not entry.ok else label
    text = f"{'✕ ' if not entry.ok else ''}{name} — {what}"
    if entry.undone:
        text += " (undone)"
    return f"{text} · {when}" if when else text


class Tray(QObject):
    settings_requested = Signal()
    pause_toggled = Signal(bool)
    logs_requested = Signal()
    quit_requested = Signal()
    history_action = Signal(str, str)  # job id, "open" | "show" | "undo"

    def __init__(self, history: History, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._history = history
        self._accent = QColor("#4C8DFF")
        self._paused = False
        self.icon = QSystemTrayIcon(self)
        self.menu = QMenu()
        self.menu.addAction("Settings…", self.settings_requested.emit)
        self.history_menu = self.menu.addMenu("History")
        self.history_menu.aboutToShow.connect(self._fill_history)
        self.pause_action = QAction("Pause gestures", self.menu, checkable=True)
        self.pause_action.toggled.connect(self.pause_toggled.emit)
        self.menu.addAction(self.pause_action)
        self.menu.addAction("Open log folder", self.logs_requested.emit)
        self.menu.addSeparator()
        self.menu.addAction("Quit", self.quit_requested.emit)
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self._fill_history()
        self._refresh()

    def show(self) -> None:
        self.icon.show()

    def hide(self) -> None:
        self.icon.hide()

    def set_accent(self, colour: QColor) -> None:
        self._accent = QColor(colour)
        self._refresh()

    def set_paused(self, paused: bool) -> None:
        """Follow the config: the menu tick and a grey icon. Does not emit ``pause_toggled``."""
        self._paused = paused
        self.pause_action.blockSignals(True)
        self.pause_action.setChecked(paused)
        self.pause_action.blockSignals(False)
        self._refresh()

    def _refresh(self) -> None:
        self.icon.setIcon(_icon(PAUSED_COLOUR if self._paused else self._accent))
        self.icon.setToolTip(f"{APP_NAME} (paused)" if self._paused else APP_NAME)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.settings_requested.emit()

    def _fill_history(self) -> None:
        """Rebuilt each time it opens, so it is never out of date."""
        menu = self.history_menu
        menu.clear()
        entries = self._history.entries
        if not entries:
            menu.addAction("No jobs yet").setEnabled(False)
            return
        for entry in entries:
            if not entry.ok or entry.undone or not entry.outputs:
                menu.addAction(entry_text(entry)).setEnabled(False)
                continue
            sub = menu.addMenu(entry_text(entry))
            for action, text in (("open", "Open"), ("show", "Show in folder"), ("undo", "Undo")):
                item = sub.addAction(text)
                item.triggered.connect(
                    lambda _=False, job=entry.id, a=action: self.history_action.emit(job, a)
                )
                item.setEnabled(action != "undo" or entry.can_undo)
