"""Config (DS-8): schema, defaults, unknown keys, bad files, migration, atomic save, live signal."""

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from dragshift import config as cfg
from dragshift.config import (
    Config,
    ConfigError,
    ConfigStore,
    load_config,
    parse_config,
    save_config,
)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "DragShift" / "config.json"


def write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- defaults and the §10.1 shape ----------------------------------------------------------------
def test_defaults_match_the_prd_schema(path: Path) -> None:
    save_config(Config(), path)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {
        "version": 1,
        "keys": {
            "convert": "shift",
            "tools": "ctrl",
            "actions": "alt",
            "launch_apps": "shift",
            "launch_sites": "ctrl",
            "tap_mode": "single",
        },
        "wheels": {"pinned": {}},
        "launcher": {"enabled": True, "apps": [], "sites": []},
        "appearance": {"theme": "system", "accent": "system", "reduced_motion": "system"},
        "output": {
            "location": "next_to_source",
            "fixed_folder": None,
            "jpeg_quality": 85,
            "pdf_dpi": 300,
            "allow_format_change_for_size": False,
        },
        "folders": {"pinned": []},
        "general": {
            "autostart": False,
            "trim_memory_when_idle": True,
            "paused": False,
            "blocklist": [],
        },
    }


def test_missing_file_gives_defaults_and_is_not_created(path: Path) -> None:
    assert load_config(path) == Config()
    assert not path.exists()


def test_missing_keys_get_defaults() -> None:
    config = parse_config({"keys": {"convert": "alt"}, "output": {"jpeg_quality": 70}})
    assert config.keys.convert == "alt"
    assert config.keys.tools == "ctrl"
    assert config.output.jpeg_quality == 70
    assert config.output.pdf_dpi == 300
    assert config.general == cfg.General()


def test_config_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        Config().general.paused = True  # type: ignore[misc]


def test_utf8_bom_is_tolerated(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"general": {"paused": True}}).encode())
    assert load_config(path).general.paused is True


# --- round trip and unknown keys -----------------------------------------------------------------
def test_round_trip_keeps_everything(path: Path) -> None:
    config = Config(
        keys=cfg.Keys(convert="ctrl", tools="shift", tap_mode="double"),
        wheels=cfg.Wheels(pinned={"image": ("convert.image.png", "convert.image.jpg")}),
        launcher=cfg.Launcher(
            apps=(cfg.LauncherApp("VS Code", "C:\\Code.exe"),),
            sites=(cfg.LauncherSite("GitHub", "https://github.com"),),
        ),
        output=cfg.Output(location="fixed_folder", fixed_folder="D:\\Out", pdf_dpi=150),
        folders=cfg.Folders(pinned=("D:\\A", "D:\\B")),
        general=cfg.General(autostart=True, blocklist=("game.exe",)),
    )
    save_config(config, path)
    assert load_config(path) == config


def test_unknown_keys_survive_load_and_save(path: Path) -> None:
    write(
        path,
        {
            "version": 1,
            "mystery": {"a": [1, 2]},
            "keys": {"convert": "alt", "future_key": True},
            "launcher": {"apps": [{"label": "X", "target": "x.exe", "icon": "x.png"}]},
        },
    )
    config = load_config(path)
    assert config.extra == {"mystery": {"a": [1, 2]}}
    assert config.keys.extra == {"future_key": True}
    save_config(dataclasses.replace(config, general=cfg.General(paused=True)), path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["mystery"] == {"a": [1, 2]}
    assert saved["keys"]["future_key"] is True
    assert saved["launcher"]["apps"][0]["icon"] == "x.png"
    assert saved["general"]["paused"] is True


# --- invalid files (§10.1: back up to config.bad.json, use defaults) ------------------------------
@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        "[]",
        '{"keys": []}',
        '{"keys": {"convert": "meta"}}',  # not a modifier
        '{"keys": {"tap_mode": "triple"}}',
        '{"appearance": {"theme": "sepia"}}',
        '{"output": {"jpeg_quality": "high"}}',
        '{"output": {"jpeg_quality": true}}',  # a bool is not an int
        '{"general": {"autostart": 1}}',  # an int is not a bool
        '{"general": {"blocklist": "a.exe"}}',
        '{"launcher": {"apps": [{"label": 5}]}}',
        '{"version": "1"}',
        '{"version": 0}',
    ],
)
def test_invalid_file_is_backed_up_and_replaced_with_defaults(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(content, encoding="utf-8")
    assert load_config(path) == Config()
    assert (path.parent / "config.bad.json").read_text(encoding="utf-8") == content
    assert load_config(path) == Config()  # the replacement is valid and now loads quietly
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1


def test_a_newer_bad_file_replaces_the_older_backup(path: Path) -> None:
    path.parent.mkdir(parents=True)
    bad = path.parent / "config.bad.json"
    bad.write_text("old", encoding="utf-8")
    path.write_text("newer", encoding="utf-8")
    load_config(path)
    assert bad.read_text(encoding="utf-8") == "newer"


def test_errors_name_the_key_path() -> None:
    with pytest.raises(ConfigError, match=r"launcher\.apps\[0\]\.label"):
        parse_config({"launcher": {"apps": [{"label": 5}]}})
    with pytest.raises(ConfigError, match=r"keys\.convert.*shift, ctrl, alt"):
        parse_config({"keys": {"convert": "meta"}})


# --- migration ---------------------------------------------------------------------------------
def test_old_versions_are_migrated_in_order_and_written_back(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def v1_to_v2(d: dict) -> dict:
        d = {**d, "version": 2}
        d["general"] = {**d.get("general", {}), "paused": True}
        return d

    def v2_to_v3(d: dict) -> dict:
        d = {**d, "version": 3}
        d["output"] = {**d.get("output", {}), "jpeg_quality": d["output"]["jpeg_quality"] - 5}
        return d

    monkeypatch.setattr(cfg, "CURRENT_VERSION", 3)
    monkeypatch.setattr(cfg, "_MIGRATIONS", {1: v1_to_v2, 2: v2_to_v3})
    write(path, {"version": 1, "output": {"jpeg_quality": 85}})
    config = load_config(path)
    assert (config.version, config.general.paused, config.output.jpeg_quality) == (3, True, 80)
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 3  # written back


def test_a_missing_version_is_treated_as_v1(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "CURRENT_VERSION", 2)
    monkeypatch.setattr(cfg, "_MIGRATIONS", {1: lambda d: {**d, "version": 2}})
    write(path, {"general": {"paused": True}})
    assert load_config(path).version == 2


def test_a_newer_file_is_loaded_best_effort_and_keeps_its_version(path: Path) -> None:
    write(path, {"version": 7, "general": {"paused": True}, "shiny": 1})
    config = load_config(path)
    assert config.version == 7
    assert config.general.paused is True
    assert config.extra == {"shiny": 1}
    assert not (path.parent / "config.bad.json").exists()


# --- atomic save ---------------------------------------------------------------------------------
def test_save_leaves_no_part_file(path: Path) -> None:
    save_config(Config(), path)
    assert [p.name for p in path.parent.iterdir()] == ["config.json"]


def test_a_failed_save_keeps_the_old_file(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_config(Config(general=cfg.General(paused=True)), path)
    before = path.read_text(encoding="utf-8")

    def boom(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        save_config(Config(), path)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before


def test_default_path_uses_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert cfg.default_config_path() == tmp_path / "DragShift" / "config.json"


# --- ConfigStore: the live-reload signal ------------------------------------------------------------
@pytest.fixture
def store(path: Path, qapp: object) -> ConfigStore:
    return ConfigStore(path)


def test_update_saves_and_emits_once(store: ConfigStore, path: Path) -> None:
    seen: list[Config] = []
    store.changed.connect(seen.append)
    new = dataclasses.replace(store.config, general=cfg.General(paused=True))
    store.update(new)
    assert seen == [new]
    assert store.config is new
    assert load_config(path) == new


def test_update_with_an_equal_config_does_nothing(store: ConfigStore, path: Path) -> None:
    seen: list[Config] = []
    store.changed.connect(seen.append)
    store.update(Config())
    assert seen == []
    assert not path.exists()


def test_reload_picks_up_a_hand_edit_and_emits(store: ConfigStore, path: Path) -> None:
    seen: list[Config] = []
    store.changed.connect(seen.append)
    write(path, {"keys": {"convert": "alt"}})
    store.reload()
    assert store.config.keys.convert == "alt"
    assert len(seen) == 1
    store.reload()  # unchanged on disk
    assert len(seen) == 1


def test_reload_of_a_broken_edit_falls_back_to_defaults(
    store: ConfigStore, path: Path, qtbot: QtBot
) -> None:
    store.update(dataclasses.replace(store.config, general=cfg.General(paused=True)))
    path.write_text("{ oops", encoding="utf-8")
    with qtbot.waitSignal(store.changed, timeout=1000) as blocker:
        store.reload()
    assert blocker.args == [Config()]
    assert (path.parent / "config.bad.json").read_text(encoding="utf-8") == "{ oops"


def test_store_loads_the_existing_file(path: Path, qapp: object) -> None:
    write(path, {"general": {"autostart": True}})
    assert ConfigStore(path).config.general.autostart is True


# --- the UI process stays lean (CONTRIBUTING.md rule 2) ----------------------------------------------------
def test_config_imports_no_engine_libraries() -> None:
    heavy = ["PIL", "fitz", "pandas", "numpy", "pdf2docx", "pypandoc"]
    script = (
        f"import sys, dragshift.config;print(','.join(m for m in {heavy!r} if m in sys.modules))"
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == ""
