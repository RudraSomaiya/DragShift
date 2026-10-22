"""The Custom compress dialog (PRD §8.6, FR-4.3): a target MB or a quality, and an estimate."""

from pathlib import Path

from pytestqt.qtbot import QtBot

from dragshift.engine.presets import MB
from dragshift.ui.dialogs.compress import CompressDialog


def file_of(path: Path, size: int) -> Path:
    path.write_bytes(b"\0" * size)
    return path


def test_target_size_mode(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CompressDialog([file_of(tmp_path / "a.mp4", 48 * MB)])
    qtbot.addWidget(dialog)
    assert dialog.target.value() == 10.0  # Discord-sized by default
    assert dialog.params() == {"target_mb": 10.0}
    assert dialog.estimate.text() == "Now 48 MB → at most 10 MB (−79%)"


def test_the_default_target_is_never_above_half_the_file(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CompressDialog([file_of(tmp_path / "a.jpg", 3 * MB)])
    qtbot.addWidget(dialog)
    assert dialog.target.value() == 1.5


def test_quality_mode(qtbot: QtBot, tmp_path: Path) -> None:
    dialog = CompressDialog([file_of(tmp_path / "a.jpg", 2 * MB)])
    qtbot.addWidget(dialog)
    dialog.by_quality.setChecked(True)
    dialog.quality.setValue(40)
    assert dialog.params() == {"quality": 40}
    assert not dialog.target.isEnabled()
    assert dialog.quality_value.text() == "40"
    assert "Lower quality" in dialog.estimate.text()


def test_several_files_and_one_already_small(qtbot: QtBot, tmp_path: Path) -> None:
    files = [file_of(tmp_path / f"{n}.jpg", 4 * MB) for n in "ab"]
    dialog = CompressDialog(files)
    qtbot.addWidget(dialog)
    dialog.target.setValue(1.0)
    assert dialog.estimate.text() == "Now 2 files, 8 MB → at most 1 MB each (−75%)"
    dialog.target.setValue(5.0)
    assert dialog.estimate.text() == "Now 2 files, 8 MB: already under 5 MB"


def test_the_estimate_looks_at_each_file(qtbot: QtBot, tmp_path: Path) -> None:
    """Not the average: a 30 MB file is not "already under 20 MB" next to a 1 MB one."""
    files = [file_of(tmp_path / "small.jpg", 1 * MB), file_of(tmp_path / "big.mp4", 30 * MB)]
    dialog = CompressDialog(files)
    qtbot.addWidget(dialog)
    assert dialog.target.value() == 10.0  # half the largest file, at most 10 MB
    dialog.target.setValue(20.0)
    assert dialog.estimate.text() == "Now 2 files, 31 MB → at most 20 MB each (−32%)"
