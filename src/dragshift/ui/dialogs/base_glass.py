"""The Acrylic dialog base (PRD §8.6): a frameless window with a 40 px title bar, DWM Acrylic
behind it, and the styled controls the dialogs share.

On Windows 10, or if DWM refuses the backdrop, the window paints an opaque panel instead
(``#F3F5F9`` light, ``#1B1F27`` dark, at 96 % alpha).

UI-process module: it must never import an engine library (CLAUDE.md rule 2).
"""

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dragshift.platform import dwm
from dragshift.ui.theme import Theme, css, ui_font

__all__ = ["GlassDialog", "Segmented", "primary_button", "secondary_button"]

TITLE_BAR = 40
RADIUS = dwm.CORNER_RADIUS  # the fallback panel matches DWMWCP_ROUND
FALLBACK_LIGHT, FALLBACK_DARK = QColor("#F3F5F9"), QColor("#1B1F27")
FALLBACK_ALPHA = 0.96


def stylesheet(theme: Theme) -> str:
    """Styled controls (§8.6): accent-filled sliders and primary button, glass secondary button."""
    accent, text = theme.accent.name(), css(theme.text)
    on_accent = theme.on_accent(1.0).name()
    field, stroke, muted = css(theme.petal), css(theme.stroke), css(theme.text_secondary)
    return f"""
    QLabel {{ color: {text}; background: transparent; }}
    QRadioButton {{ color: {text}; background: transparent; }}
    QDoubleSpinBox, QSpinBox, QLineEdit {{
        color: {text}; background: {field}; border: 1px solid {stroke};
        border-radius: 8px; padding: 4px 8px; }}
    QSlider::groove:horizontal {{ height: 4px; border-radius: 2px; background: {field}; }}
    QSlider::sub-page:horizontal {{ border-radius: 2px; background: {accent}; }}
    QSlider::handle:horizontal {{
        width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; background: {accent}; }}
    QSlider::sub-page:horizontal:disabled, QSlider::handle:horizontal:disabled {{
        background: {muted}; }}
    QDoubleSpinBox:disabled, QSpinBox:disabled, QLineEdit:disabled, QRadioButton:disabled {{
        color: {muted}; }}
    QPushButton {{
        color: {text}; background: {field}; border: 1px solid {stroke};
        border-radius: 15px; padding: 6px 18px; }}
    QPushButton:hover {{ background: {css(theme.hover)}; }}
    QPushButton[primary="true"] {{ color: {on_accent}; background: {accent}; border: none; }}
    QPushButton[segment="true"] {{
        border: none; border-radius: 13px; background: transparent; padding: 4px 14px; }}
    QPushButton[segment="true"]:checked {{ color: {on_accent}; background: {accent}; }}
    QPushButton[close="true"] {{ border: none; background: transparent; padding: 0; }}
    QPushButton:focus, QSlider:focus {{ border: 1px solid {accent}; }}
    QPushButton[close="true"]:focus {{ border: 1px solid {accent}; }}
    """


def primary_button(text: str) -> QPushButton:
    """Accent fill with white (or ink, whichever contrasts) text."""
    button = QPushButton(text)
    button.setProperty("primary", True)
    button.setDefault(True)
    return button


def secondary_button(text: str) -> QPushButton:
    """The glass petal style."""
    return QPushButton(text)


class Segmented(QWidget):
    """A pill group of exclusive segments; the active one is filled with the accent."""

    changed = Signal(str)

    def __init__(self, options: list[str], current: str | None = None, parent=None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(2)
        self.buttons: dict[str, QAbstractButton] = {}
        for name in options:
            b = QPushButton(name)
            b.setCheckable(True)
            b.setProperty("segment", True)
            b.clicked.connect(lambda _=False, n=name: self.set_value(n))
            row.addWidget(b)
            self.buttons[name] = b
        self.value = ""
        self.set_value(current or options[0])

    def set_value(self, name: str) -> None:
        for n, b in self.buttons.items():
            b.setChecked(n == name)
        if name != self.value:
            self.value = name
            self.changed.emit(name)


class GlassDialog(QDialog):
    """Frameless Acrylic dialog. Put content in ``body`` (a QVBoxLayout)."""

    def __init__(self, title: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        self.acrylic = False
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(stylesheet(theme))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._title_bar(title))
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(20, 8, 20, 20)
        self.body.setSpacing(12)
        outer.addWidget(content)

    def _title_bar(self, title: str) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(TITLE_BAR)
        label = QLabel(title, bar)
        label.setFont(ui_font(14, semibold=True))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row = QHBoxLayout(bar)
        row.setContentsMargins(52, 0, 52, 0)  # keeps the title centred past the ✕
        row.addWidget(label)
        close = QPushButton("✕", bar)  # at the left, the way the Tangerine dialogs look
        close.setProperty("close", True)
        close.setFixedSize(28, 28)
        close.setToolTip("Close")
        close.setAccessibleName("Close")
        close.move(12, (TITLE_BAR - 28) // 2)
        close.clicked.connect(self.reject)
        self.close_button = close
        self.title_bar = bar
        return bar

    def set_theme(self, theme: Theme) -> None:
        """Follow a change in Windows light/dark or accent while the dialog is open."""
        self.theme = theme
        self.setStyleSheet(stylesheet(theme))
        if self.isVisible():
            self.acrylic = dwm.apply_acrylic(int(self.winId()), theme.dark)
        self.update()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.acrylic = dwm.apply_acrylic(int(self.winId()), self.theme.dark)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < TITLE_BAR:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()  # drag by the title bar
                return
        super().mousePressEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        shape = QPainterPath()
        shape.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), RADIUS, RADIUS)
        if self.acrylic:
            p.fillPath(shape, self.theme.tint)  # a light tint over the DWM Acrylic
        else:
            panel = QColor(FALLBACK_DARK if self.theme.dark else FALLBACK_LIGHT)
            panel.setAlphaF(FALLBACK_ALPHA)
            p.fillPath(shape, panel)
        p.setPen(QPen(self.theme.stroke, 1))
        p.drawPath(shape)
