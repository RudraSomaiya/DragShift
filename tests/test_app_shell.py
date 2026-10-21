"""App shell (DS-26): single instance, --background, logs, autostart, CLI sub-commands.

Nothing here touches the real Run key or the real single-instance names.
"""

import logging
import os
import uuid
import winreg
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest
from PIL import Image
from pytestqt.qtbot import QtBot

from dragshift import __main__ as entry
from dragshift import app as app_module
from dragshift import cli
from dragshift.config import Config, ConfigStore, General
from dragshift.engine import registry
from dragshift.engine.jobs import JobManager
from dragshift.engine.registry import Op
from dragshift.platform import autostart
from dragshift.platform.single_instance import SingleInstance

TEST_KEY = r"Software\DragShiftTest\Run"


# --- single instance (FR-6.3) ----------------------------------------------------------------
def names() -> tuple[str, str]:
    tag = uuid.uuid4().hex
    return f"Local\\DragShiftTest.{tag}.Mutex", f"Local\\DragShiftTest.{tag}.Event"


def test_only_the_first_instance_runs_and_a_second_asks_it_for_settings(
    qtbot: QtBot,
) -> None:
    mutex, event = names()
    first = SingleInstance(mutex, event)
    second = SingleInstance(mutex, event)
    assert first.first and not second.first
    with qtbot.waitSignal(first.show_settings, timeout=2000):
        second.notify_first()
    second.release()
    first.release()
    third = SingleInstance(mutex, event)
    assert third.first  # released: the next launch runs
    third.release()


def test_a_second_launch_exits_before_starting_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class Running:
        first = False

        def notify_first(self) -> None:
            calls.append("notified")

    monkeypatch.setattr(app_module, "SingleInstance", Running)
    monkeypatch.setattr(app_module, "setup_logging", lambda: None)
    monkeypatch.setattr(app_module, "QApplication", lambda *_: pytest.fail("must not start Qt"))
    assert app_module.run() == 0
    assert calls == ["notified"]


# --- --background (FR-6.2) -------------------------------------------------------------------
def test_background_relaunches_with_pythonw_detached(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[tuple] = []
    monkeypatch.setattr(entry.subprocess, "Popen", lambda cmd, **kw: launched.append((cmd, kw)))
    monkeypatch.setattr(app_module, "run", lambda: pytest.fail("must not run here"))
    assert entry.main(["--background"]) == 0
    ((cmd, kw),) = launched
    assert cmd[0].lower().endswith("pythonw.exe") and cmd[1:] == ["-m", "dragshift"]
    assert kw["creationflags"] & entry.DETACHED_PROCESS


def test_background_under_pythonw_just_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(entry.sys, "stdout", None)  # how pythonw starts us
    monkeypatch.setattr(app_module, "run", lambda: 7)
    monkeypatch.setattr(entry.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no relaunch"))
    assert entry.main(["--background"]) == 7


# --- logs (FR-6.5) ---------------------------------------------------------------------------
@pytest.fixture
def root_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    yield
    for handler in root.handlers:
        handler.close()
    root.handlers, root.level = saved[0], saved[1]


@pytest.mark.usefixtures("root_logging")
def test_logs_rotate_in_one_file(tmp_path: Path) -> None:
    path = app_module.setup_logging(tmp_path)
    logging.getLogger("dragshift.test").info("hello log")
    (rotating,) = [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]
    assert (rotating.maxBytes, rotating.backupCount) == (1_000_000, 3)
    rotating.flush()
    assert "hello log" in path.read_text(encoding="utf-8")


@pytest.mark.usefixtures("root_logging")
def test_no_console_handler_under_pythonw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module.sys, "stderr", None)
    app_module.setup_logging(tmp_path)
    kinds = {type(h) for h in logging.getLogger().handlers}
    assert kinds == {RotatingFileHandler}


def test_the_log_folder_is_under_local_app_data() -> None:
    assert app_module.log_dir() == Path(os.environ["LOCALAPPDATA"], "DragShift", "logs")


# --- autostart (FR-7.2) ----------------------------------------------------------------------
@pytest.fixture
def run_key() -> Iterator[str]:
    yield TEST_KEY
    for key in (TEST_KEY, r"Software\DragShiftTest"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except FileNotFoundError:
            pass


def test_autostart_writes_and_removes_the_run_value(run_key: str) -> None:
    assert not autostart.is_enabled(run_key)
    autostart.sync(True, run_key)
    assert autostart.is_enabled(run_key)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as k:
        value, kind = winreg.QueryValueEx(k, "DragShift")
    assert kind == winreg.REG_SZ
    assert value == f'"{autostart.pythonw()}" -m dragshift'  # quoted: the path has spaces
    autostart.sync(False, run_key)
    assert not autostart.is_enabled(run_key)
    autostart.disable(run_key)  # already off: fine


def test_pythonw_sits_next_to_the_venv_python() -> None:
    assert autostart.pythonw().name.lower() == "pythonw.exe"
    assert autostart.pythonw().exists()


def test_the_app_keeps_autostart_in_step_with_the_config(tmp_path: Path, qapp: object) -> None:
    calls: list[bool] = []
    store = ConfigStore(tmp_path / "config.json")
    the_app = app_module.App(store, autostart=calls.append)
    store.update(Config(general=General(autostart=True)))
    assert calls == [False, True]  # at start, then on the change
    the_app.shutdown()


# --- CLI (FR-6.4) ----------------------------------------------------------------------------
@pytest.fixture
def png(tmp_path: Path) -> Path:
    Image.new("RGB", (16, 16), (200, 30, 30)).save(path := tmp_path / "red.png")
    return path


def test_ops_lists_the_wheels(png: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["ops", str(png)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("convert:\n")
    assert "convert.image.jpg" in out and "convert.image.png" not in out


def test_convert_runs_through_the_worker_pool_and_prints_the_output(
    png: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(JobManager, "CANCEL_DIR", tmp_path / "cancel")
    assert entry.main(["convert", str(png), "--to", "jpg"]) == 0
    assert capsys.readouterr().out.strip() == str(png.with_suffix(".jpg"))
    assert png.with_suffix(".jpg").is_file()


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["convert", "{png}", "--to", "mp3"], "Valid: JPG"),
        (["convert", "{missing}", "--to", "jpg"], "no such file"),
        (["tool", "nope", "{png}"], "no such op"),
        (["tool", "convert.audio.mp3", "{png}"], "does not accept"),
        (["ops", "{missing}"], "no such file"),
    ],
)
def test_usage_errors_exit_2(
    png: Path, capsys: pytest.CaptureFixture[str], argv: list[str], error: str
) -> None:
    args = [a.format(png=png, missing=png.with_name("missing.png")) for a in argv]
    assert entry.main(args) == cli.USAGE
    assert error in capsys.readouterr().err


def test_ops_that_need_a_dialog_or_the_desktop_are_refused(
    png: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    crop = Op(id="tools.image.crop", wheel="tools", label="Crop",
              kinds=frozenset({registry.kind_of(png)}), func="x:y", dialog="crop")  # fmt: skip
    copy = Op(id="actions.copy_path", wheel="actions", label="Copy path",
              kinds=frozenset({registry.kind_of(png)}), func="x:y", where="ui")  # fmt: skip
    for op in (crop, copy):
        monkeypatch.setattr(registry, "get", lambda _id, op=op: op)
        assert entry.main(["tool", op.id, str(png)]) == cli.USAGE
    err = capsys.readouterr().err
    assert "needs its crop dialog" in err and "acts on the desktop" in err
