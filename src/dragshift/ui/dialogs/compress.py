"""The Custom compress dialog (PRD §8.6, FR-4.3): a target size in MB or a quality, plus an
estimate line.

It is a plain window for now; DS-24 gives it the Acrylic base and the styled controls. It only
collects params: the App submits the job when it is accepted.

UI-process module: it must never import an engine library (CLAUDE.md rule 2).
"""

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QRadioButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from dragshift.engine.presets import MB, mb_text
from dragshift.ui.job_toasts import size_of

DEFAULT_MB = 10.0
DEFAULT_QUALITY = 75


def _sizes(paths: Sequence[Path]) -> list[int]:
    sizes = []
    for path in paths:
        try:
            sizes.append(size_of(path))
        except OSError:
            sizes.append(0)  # gone since the drop: the job reports it
    return sizes


class CompressDialog(QDialog):
    """``params()`` is ``{"target_mb": x}`` or ``{"quality": q}``."""

    def __init__(self, paths: Sequence[Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Custom compress")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
        self._sizes = _sizes(paths)
        self._size = sum(self._sizes)

        self.by_size = QRadioButton("Target size")
        self.by_quality = QRadioButton("Quality")
        modes = QButtonGroup(self)
        modes.addButton(self.by_size)
        modes.addButton(self.by_quality)
        self.by_size.setChecked(True)

        self.target = QDoubleSpinBox()
        self.target.setRange(0.1, 4000)
        self.target.setDecimals(1)
        self.target.setSuffix(" MB")
        largest = max(self._sizes, default=0) / MB
        self.target.setValue(min(DEFAULT_MB, round(largest / 2, 1)) if largest else DEFAULT_MB)

        self.quality = QSlider(Qt.Orientation.Horizontal)
        self.quality.setRange(1, 100)
        self.quality.setValue(DEFAULT_QUALITY)
        self.quality_value = QLabel()

        self.estimate = QLabel()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Compress")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        grid = QGridLayout()
        grid.addWidget(self.by_size, 0, 0)
        grid.addWidget(self.target, 0, 1, 1, 2)
        grid.addWidget(self.by_quality, 1, 0)
        grid.addWidget(self.quality, 1, 1)
        grid.addWidget(self.quality_value, 1, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(self.estimate)
        layout.addWidget(buttons)

        for signal in (self.by_size.toggled, self.target.valueChanged, self.quality.valueChanged):
            signal.connect(self._update)
        self._update()

    def params(self) -> dict:
        if self.by_size.isChecked():
            return {"target_mb": self.target.value()}
        return {"quality": self.quality.value()}

    def _update(self) -> None:
        sized = self.by_size.isChecked()
        self.target.setEnabled(sized)
        self.quality.setEnabled(not sized)
        self.quality_value.setText(str(self.quality.value()))
        self.estimate.setText(self.estimate_text())

    def estimate_text(self) -> str:
        n = len(self._sizes)
        now = f"{n} files, {mb_text(self._size)}" if n > 1 else mb_text(self._size)
        if not self.by_size.isChecked():
            return f"Now {now}. Lower quality gives a smaller file."
        goal = int(self.target.value() * MB)
        if all(size <= goal for size in self._sizes):
            return f"Now {now}: already under {mb_text(goal)}"
        per_file = " each" if n > 1 else ""
        # Each file is brought down to the goal on its own; one under it stays about as it is.
        after = sum(min(size, goal) for size in self._sizes)
        return f"Now {now} → at most {mb_text(goal)}{per_file} (−{1 - after / self._size:.0%})"
