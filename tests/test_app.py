"""App wiring (DS-11): a drop on a petal becomes a job, and the source is never touched."""

import hashlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from dragshift import app as app_module
from dragshift.app import App
from dragshift.config import ConfigStore
from dragshift.engine import registry
from dragshift.engine.jobs import JobManager, JobResult
from dragshift.engine.registry import Op


@pytest.fixture
def the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qapp: object) -> Iterator[App]:
    monkeypatch.setattr(JobManager, "CANCEL_DIR", tmp_path / "cancel")
    instance = App(ConfigStore(tmp_path / "config.json"))
    yield instance
    instance.shutdown()


def png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGBA", (24, 16), (200, 30, 30, 128)).save(path, format="PNG")
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_drop_on_a_petal_submits_that_op_for_those_files(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[tuple] = []
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append((a, k)) or "id")
    files = [Path("C:/x/a.png"), Path("C:/x/b.png")]
    the_app.wheel.dropped.emit("convert.image.jpg", files)
    assert submitted == [(("convert.image.jpg", files), {})]


def test_an_op_that_needs_a_dialog_is_not_run_blindly(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    crop = Op(
        id="tools.image.crop",
        wheel="tools",
        label="Crop",
        kinds=frozenset(),
        func="x:y",
        dialog="crop",
    )
    monkeypatch.setattr(registry, "get", lambda op_id: crop)
    monkeypatch.setattr(the_app.jobs, "submit", lambda *_: pytest.fail("must not submit"))
    the_app.wheel.dropped.emit("tools.image.crop", [Path("C:/x/a.png")])


def test_the_real_pipeline_converts_and_leaves_the_source_alone(
    the_app: App, tmp_path: Path, qtbot: QtBot
) -> None:
    """Checklist item 1, minus the mouse: drop on the JPG petal -> a JPG appears, the PNG stays."""
    source = png(tmp_path / "photo.png")
    before = (sha(source), source.stat().st_mtime_ns)
    results: list[JobResult] = []
    the_app.jobs.finished.connect(lambda _id, r: results.append(r))
    with qtbot.waitSignal(the_app.jobs.finished, timeout=30000):
        the_app.wheel.dropped.emit("convert.image.jpg", [source])
    (result,) = results
    assert result.ok, result.message
    assert [p.name for p in result.outputs] == ["photo.jpg"]
    assert (tmp_path / "photo.jpg").is_file()
    assert (sha(source), source.stat().st_mtime_ns) == before  # not moved, rewritten or touched
    assert not list(tmp_path.glob("*.part"))


def test_hooks_are_not_started_by_building_the_app(the_app: App) -> None:
    assert not the_app.hooks.is_installed()
    the_app.shutdown()  # safe when nothing was started


def test_app_module_keeps_the_ui_process_lean() -> None:
    heavy = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]
    script = f"import sys, dragshift.app;print(','.join(m for m in {heavy!r} if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""


def test_help_does_not_start_the_app(capsys: pytest.CaptureFixture[str]) -> None:
    from dragshift.__main__ import main

    with pytest.raises(SystemExit):
        main(["--help"])
    assert "dragshift" in capsys.readouterr().out
    assert app_module.run  # imported lazily, but importable
