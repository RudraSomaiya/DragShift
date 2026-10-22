"""App wiring (DS-11): a drop on a petal becomes a job, and the source is never touched."""

import dataclasses
import hashlib
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
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
    toast = the_app.toasts.stack.get(the_app.history.entries[0].id)
    assert toast.content.title == "photo.jpg"  # FR-5.5
    assert toast.content.buttons == ("Open", "Show", "Undo")


def test_a_drop_starts_the_progress_toast_clock(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: "job-1")
    started: list[tuple] = []
    monkeypatch.setattr(the_app.toasts, "started", lambda *a: started.append(a))
    files = [Path("C:/x/a.png")]
    the_app.wheel.dropped.emit("convert.image.jpg", files)
    assert started == [("job-1", "convert.image.jpg", files)]


def test_a_finished_job_is_recorded_toasted_and_reorders_the_petals(
    the_app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    orderings: list[tuple] = []
    monkeypatch.setattr(the_app.wheel, "set_ordering", lambda *a: orderings.append(a))
    shown: list[tuple] = []
    monkeypatch.setattr(the_app.toasts, "finished", lambda *a: shown.append(a))
    result = JobResult("convert.image.webp", [Path("C:/x/a.png")], [], True, "", {})
    the_app.jobs.finished.emit("job-1", result)
    assert [e.id for e in the_app.history.entries] == ["job-1"]
    assert (tmp_path / "history.json").exists()  # next to config.json (§10.2)
    assert shown == [("job-1", result)]
    assert orderings[-1] == ({}, {"image": {"convert.image.webp": 1}})  # FR-2.2


def test_pinned_petals_from_the_config_reach_the_wheel(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    orderings: list[tuple] = []
    monkeypatch.setattr(the_app.wheel, "set_ordering", lambda *a: orderings.append(a))
    wheels = dataclasses.replace(
        the_app.store.config.wheels, pinned={"image": ("convert.image.png",)}
    )
    the_app.store.update(dataclasses.replace(the_app.store.config, wheels=wheels))
    assert orderings[-1][0] == {"image": ("convert.image.png",)}


def test_a_quick_launch_failure_shows_a_toast(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dragshift.config import Config, Launcher, LauncherApp

    config = Config(launcher=Launcher(apps=(LauncherApp(label="Tool", target="C:/gone.exe"),)))
    monkeypatch.setattr(type(the_app.store), "config", property(lambda _self: config))
    monkeypatch.setattr(app_module.apps, "launch_app", lambda _slot: "file not found")
    errors: list[tuple] = []
    monkeypatch.setattr(the_app.toasts, "error", lambda *a: errors.append(a))
    the_app.launcher.picked.emit("apps", 0)
    assert errors == [("Couldn't open Tool", "file not found")]


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


def test_pinned_folders_reach_the_rings_and_the_jobs(
    the_app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dragshift.engine.actions import files as actions

    folders = dataclasses.replace(the_app.store.config.folders, pinned=(str(tmp_path),))
    the_app.store.update(dataclasses.replace(the_app.store.config, folders=folders))
    try:
        ring = registry.children_for(registry.get("actions.move_to"), [Path("C:/x/a.png")])
        assert [o.label for o in ring] == [tmp_path.name]
        submitted: list[tuple] = []
        monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append(a) or "id")
        the_app.wheel.dropped.emit("actions.move_to.1", [Path("C:/x/a.png")])
        assert submitted[0][2]["folder"] == str(tmp_path)
    finally:
        actions.pin_folders(())


def test_copied_text_goes_on_the_clipboard(the_app: App) -> None:
    from PySide6.QtGui import QGuiApplication

    result = JobResult("actions.copy_name", [Path("C:/x/a.png")], [], True, "", {}, copied="a.png")
    the_app.jobs.finished.emit("job-1", result)
    assert QGuiApplication.clipboard().text() == "a.png"


def test_a_drop_on_a_compress_preset_sends_its_preset(
    the_app: App, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-4.3: one drop on the ring is enough; Settings decides whether PNG may become WebP."""
    submitted: list[tuple] = []
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append(a) or "id")
    the_app.wheel.dropped.emit("tools.image.compress.discord", [Path("C:/x/a.png")])
    the_app.wheel.dropped.emit("tools.image.compress", [Path("C:/x/a.png")])
    assert [s[2] for s in submitted] == [
        {"quality": 85, "dpi": 300, "preset": "discord", "allow_format_change": False},
        {"quality": 85, "dpi": 300, "allow_format_change": False},  # Small, in the worker
    ]


def test_custom_compress_asks_first_and_runs_on_accept(
    the_app: App, monkeypatch: pytest.MonkeyPatch, qtbot: QtBot, tmp_path: Path
) -> None:
    """FR-2.5: the dialog opens after the drop, takes focus, and the job runs only on accept."""
    submitted: list[tuple] = []
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append(a) or "id")
    src = png(tmp_path / "a.png")
    the_app.wheel.dropped.emit("tools.image.compress.custom", [src])
    dialog = the_app._dialog
    assert dialog.isVisible() and submitted == []
    dialog.target.setValue(2.5)
    dialog.accept()
    assert submitted == [
        (
            "tools.image.compress.custom",
            [src],
            {"quality": 85, "dpi": 300, "preset": "custom", "allow_format_change": False,
             "target_mb": 2.5},
        )
    ]  # fmt: skip


def test_cancelling_the_compress_dialog_runs_nothing(
    the_app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(the_app.jobs, "submit", lambda *_: pytest.fail("must not submit"))
    the_app.wheel.dropped.emit("tools.pdf.compress.custom", [tmp_path / "a.pdf"])
    the_app._dialog.reject()


def test_a_second_custom_drop_replaces_the_open_dialog(
    the_app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    submitted: list[tuple] = []
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append(a) or "id")
    the_app.wheel.dropped.emit("tools.image.compress.custom", [png(tmp_path / "a.png")])
    first = the_app._dialog
    the_app.wheel.dropped.emit("tools.image.compress.custom", [png(tmp_path / "b.png")])
    assert the_app._dialog is not first
    assert first.result() == first.DialogCode.Rejected
    the_app._dialog.accept()
    assert [s[1][0].name for s in submitted] == ["b.png"]  # one job, for the latest drop
    assert the_app._dialog is None


def test_the_dialog_gets_the_resolved_theme_and_follows_changes(
    the_app: App, tmp_path: Path
) -> None:
    src = png(tmp_path / "a.png")
    the_app.wheel.dropped.emit("tools.image.compress.custom", [src])
    dialog = the_app._dialog
    assert dialog.theme.dark in (True, False)
    config = the_app.store.config
    dark = config.appearance.theme != "dark"
    appearance = replace(config.appearance, theme="dark" if dark else "light")
    the_app.on_config_changed(replace(config, appearance=appearance))
    assert dialog.theme.dark is dark


def test_the_output_folder_setting_reaches_the_job(
    the_app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dragshift.engine.jobs import OUTPUT_FOLDER

    output = dataclasses.replace(
        the_app.store.config.output, location="fixed_folder", fixed_folder=str(tmp_path)
    )
    the_app.store.update(dataclasses.replace(the_app.store.config, output=output))
    submitted: list[tuple] = []
    monkeypatch.setattr(the_app.jobs, "submit", lambda *a, **k: submitted.append(a) or "id")
    the_app.wheel.dropped.emit("convert.image.jpg", [Path("C:/x/a.png")])
    assert submitted[0][2][OUTPUT_FOLDER] == str(tmp_path)


def test_settings_opens_once_and_add_opens_the_slot(the_app: App) -> None:
    window = the_app.on_show_settings()
    assert the_app.on_show_settings() is window
    the_app.on_add_requested("sites", 3)
    assert window.pages.currentWidget() is window.launcher
    assert window.launcher.sites.currentRow() == 3
    window.close()
    assert the_app.settings is None


def test_pause_from_the_tray_is_saved_and_shown(the_app: App) -> None:
    the_app.tray.pause_toggled.emit(True)
    assert the_app.store.config.general.paused
    assert the_app.tray.pause_action.isChecked()


def test_undo_from_the_tray_s_history(
    the_app: App, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dragshift.engine import jobs

    trashed: list[Path] = []
    monkeypatch.setattr(jobs, "_trash", trashed.extend)
    out = tmp_path / "a.jpg"
    out.write_bytes(b"x")
    result = JobResult(
        "convert.image.jpg",
        [tmp_path / "a.png"],
        [out],
        True,
        "",
        {"type": "trash", "paths": [out]},
    )
    the_app.history.record("j1", result)
    the_app.tray.history_action.emit("j1", "undo")
    assert trashed == [out]
    assert the_app.history.get("j1").undone
    assert the_app.toasts.stack.get("j1").content.title == "Undone"
