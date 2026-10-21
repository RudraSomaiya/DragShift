"""App wiring (DS-11): a drop on a petal becomes a job, and the source is never touched."""

import dataclasses
import hashlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from pytestqt.qtbot import QtBot

from dragshift import app as app_module
from dragshift.app import App
from dragshift.config import Appearance, ConfigStore
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
    assert submitted == [(("convert.image.jpg", files, {"quality": 85, "dpi": 300}), {})]


def test_drops_use_the_quality_and_dpi_from_settings(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[tuple] = []
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append(a) or "id")
    output = dataclasses.replace(the_app.store.config.output, jpeg_quality=60, pdf_dpi=150)
    the_app.store.update(dataclasses.replace(the_app.store.config, output=output))
    the_app.wheel.dropped.emit("convert.image.webp", [Path("C:/x/a.png")])
    assert submitted[0][2] == {"quality": 60, "dpi": 150}


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


def test_a_quick_launch_tap_opens_that_launcher_wheel(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[tuple] = []
    monkeypatch.setattr(the_app.launcher, "open", lambda kind, at: opened.append((kind, at)))
    the_app.gesture.quick_launch.emit("sites", QPoint(10, 20))
    assert opened == [("sites", QPoint(10, 20))]


def test_a_picked_slot_is_launched(the_app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    from dragshift import app as app_module
    from dragshift.config import Config, Launcher, LauncherApp, LauncherSite

    config = Config(
        launcher=Launcher(
            apps=(LauncherApp(target="C:/a.exe"),), sites=(LauncherSite(url="x.com"),)
        )
    )
    monkeypatch.setattr(type(the_app.store), "config", property(lambda _self: config))
    launched: list = []
    monkeypatch.setattr(app_module.apps, "launch_app", lambda s: launched.append(s.target))
    monkeypatch.setattr(app_module.sites, "launch_site", lambda s: launched.append(s.url))
    the_app.launcher.picked.emit("apps", 0)
    the_app.launcher.picked.emit("sites", 0)
    assert launched == ["C:/a.exe", "x.com"]


def test_hooks_are_not_started_by_building_the_app(the_app: App) -> None:
    assert not the_app.hooks.is_installed()
    the_app.shutdown()  # safe when nothing was started


def test_appearance_changes_reach_the_wheel(the_app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[Appearance] = []
    monkeypatch.setattr(the_app.wheel, "set_appearance", got.append)
    new = Appearance(theme="light", accent="#00FF00", reduced_motion="on")
    the_app.store.update(dataclasses.replace(the_app.store.config, appearance=new))
    assert got == [new]


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


def test_taps_with_a_launcher_open_swap_or_close_it(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    visible = [False]
    monkeypatch.setattr(the_app.launcher, "isVisible", lambda: visible[0])
    monkeypatch.setattr(type(the_app.launcher), "kind", property(lambda _self: "apps"))
    monkeypatch.setattr(the_app.launcher, "open", lambda kind, at: calls.append(("open", kind)))
    monkeypatch.setattr(the_app.launcher, "swap", lambda kind: calls.append(("swap", kind)))
    monkeypatch.setattr(the_app.launcher, "close", lambda: calls.append(("close",)))
    the_app.on_quick_launch("apps", QPoint())
    visible[0] = True
    the_app.on_quick_launch("sites", QPoint())  # the other key: swap in place
    the_app.on_quick_launch("apps", QPoint())  # the open wheel's own key: close
    assert calls == [("open", "apps"), ("swap", "sites"), ("close",)]
