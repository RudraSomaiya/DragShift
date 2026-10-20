"""Config: ``%APPDATA%\\DragShift\\config.json``, schema v1 (PRD §10.1, FR-8.2).

- The file is loaded into frozen dataclasses with defaults. Missing keys get their default.
- Unknown keys are preserved (``extra`` on every dataclass) and written back on save.
- A file that is not valid (bad JSON, wrong types, an unknown choice) is moved to
  ``config.bad.json`` and replaced with defaults.
- ``version`` drives migrations: ``_MIGRATIONS[n]`` upgrades a v``n`` dict to v``n+1``. A file
  from a *newer* app is loaded best effort and keeps its version.
- ``ConfigStore`` owns the current Config on the Qt thread and emits ``changed`` on every update.
- Saves go through ``config.json.part`` and an atomic rename.

UI-process module: it must stay light, so only the stdlib and QtCore are imported.
"""

import dataclasses
import json
import logging
import os
import types
import typing
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

__all__ = [
    "Config",
    "ConfigError",
    "ConfigStore",
    "default_config_path",
    "load_config",
    "parse_config",
    "save_config",
]

log = logging.getLogger(__name__)

CURRENT_VERSION = 1
# from_version -> function returning the dict upgraded to from_version + 1 (and its "version" set)
_MIGRATIONS: dict[int, Callable[[dict[str, Any]], dict[str, Any]]] = {}

MODIFIERS = ("shift", "ctrl", "alt")


class ConfigError(ValueError):
    """The config data does not match the schema. The message names the offending key path."""


def _choice(default: str, *choices: str) -> Any:
    return field(default=default, metadata={"choices": choices})


@dataclass(frozen=True)
class Keys:
    convert: str = _choice("shift", *MODIFIERS)
    tools: str = _choice("ctrl", *MODIFIERS)
    actions: str = _choice("alt", *MODIFIERS)
    launch_apps: str = _choice("shift", *MODIFIERS)
    launch_sites: str = _choice("ctrl", *MODIFIERS)
    tap_mode: str = _choice("single", "single", "double")
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class Wheels:
    # input kind -> pinned op ids, in order (FR-2.2). Treat as read-only; use dataclasses.replace.
    pinned: dict[str, tuple[str, ...]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class LauncherApp:
    label: str = ""
    target: str = ""  # exe, lnk, folder or AUMID
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class LauncherSite:
    label: str = ""
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class Launcher:
    enabled: bool = True
    apps: tuple[LauncherApp, ...] = ()
    sites: tuple[LauncherSite, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class Appearance:
    theme: str = _choice("system", "system", "light", "dark")
    accent: str = "system"  # "system" or a custom colour; parsed by the theme code
    reduced_motion: str = "system"
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class Output:
    location: str = _choice("next_to_source", "next_to_source", "fixed_folder")
    fixed_folder: str | None = None
    jpeg_quality: int = 85
    pdf_dpi: int = 300
    allow_format_change_for_size: bool = False
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class Folders:
    pinned: tuple[str, ...] = ()  # folder paths for Copy to / Move to
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class General:
    autostart: bool = False
    trim_memory_when_idle: bool = True
    paused: bool = False
    blocklist: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


@dataclass(frozen=True)
class Config:
    version: int = CURRENT_VERSION
    keys: Keys = field(default_factory=Keys)
    wheels: Wheels = field(default_factory=Wheels)
    launcher: Launcher = field(default_factory=Launcher)
    appearance: Appearance = field(default_factory=Appearance)
    output: Output = field(default_factory=Output)
    folders: Folders = field(default_factory=Folders)
    general: General = field(default_factory=General)
    extra: dict[str, Any] = field(default_factory=dict)  # unknown keys, preserved on save


# --- dict <-> dataclasses ------------------------------------------------------------------------
def _load_value(hint: Any, value: Any, path: str) -> Any:
    origin = typing.get_origin(hint)
    if dataclasses.is_dataclass(hint):
        return _load_dataclass(hint, value, path)
    if origin is tuple:
        item_hint = typing.get_args(hint)[0]
        if not isinstance(value, list):
            raise ConfigError(f"{path}: expected a list")
        return tuple(_load_value(item_hint, v, f"{path}[{i}]") for i, v in enumerate(value))
    if origin is dict:
        value_hint = typing.get_args(hint)[1]
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected an object")
        return {k: _load_value(value_hint, v, f"{path}.{k}") for k, v in value.items()}
    if origin is types.UnionType:  # only "X | None" is used
        if value is None:
            return None
        hint = next(a for a in typing.get_args(hint) if a is not type(None))
    if hint is bool:
        ok = isinstance(value, bool)
    elif hint is int:
        ok = isinstance(value, int) and not isinstance(value, bool)
    else:
        ok = isinstance(value, hint)
    if not ok:
        raise ConfigError(f"{path}: expected {hint.__name__}, got {type(value).__name__}")
    return value


def _load_dataclass(cls: type, data: Any, path: str) -> Any:
    if not isinstance(data, dict):
        raise ConfigError(f"{path or 'config'}: expected an object")
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name == "extra" or f.name not in data:
            continue  # a missing key keeps its default
        sub = f"{path}.{f.name}" if path else f.name
        value = _load_value(hints[f.name], data[f.name], sub)
        choices = f.metadata.get("choices")
        if choices and value not in choices:
            raise ConfigError(f"{sub}: {value!r} is not one of {', '.join(choices)}")
        kwargs[f.name] = value
    known = {f.name for f in dataclasses.fields(cls)}
    return cls(**kwargs, extra={k: v for k, v in data.items() if k not in known})


def _dump(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {
            f.name: _dump(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name != "extra"
        }
        for k, v in value.extra.items():
            out.setdefault(k, v)  # a known key always wins over an extra
        return out
    if isinstance(value, tuple | list):
        return [_dump(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    return value


def parse_config(data: Any) -> Config:
    """Validate a decoded JSON value and turn it into a Config. Raises ConfigError."""
    if not isinstance(data, dict):
        raise ConfigError("config: expected an object")
    version = data.get("version", 1)
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ConfigError(f"version: expected a positive integer, got {version!r}")
    while version < CURRENT_VERSION:
        data = _MIGRATIONS[version](data)
        version = data["version"]
    return _load_dataclass(Config, data, "")


# --- files ---------------------------------------------------------------------------------------
def default_config_path() -> Path:
    return Path(os.environ["APPDATA"]) / "DragShift" / "config.json"


def save_config(config: Config, path: Path | None = None) -> None:
    """Write the config atomically (``*.part``, then rename)."""
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(
        json.dumps(_dump(config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(part, path)  # atomic on the same volume


def load_config(path: Path | None = None) -> Config:
    """Load the config. A missing file gives defaults and is not created.

    An invalid file is moved to ``config.bad.json`` and replaced with defaults. A migrated file
    is written back in the new version.
    """
    path = path or default_config_path()
    try:
        text = path.read_text(encoding="utf-8-sig")  # tolerate a BOM from Notepad
    except FileNotFoundError:
        return Config()
    try:
        data = json.loads(text)
        config = parse_config(data)
    except (json.JSONDecodeError, ConfigError) as exc:
        log.warning("invalid config %s (%s); backing it up and using defaults", path, exc)
        _replace_with_defaults(path)
        return Config()
    if isinstance(data, dict) and data.get("version", 1) < config.version:
        save_config(config, path)
    return config


def _replace_with_defaults(path: Path) -> None:
    bad = path.with_name("config.bad.json")
    try:
        os.replace(path, bad)
        save_config(Config(), path)
    except OSError:
        log.exception("could not back up %s; keeping defaults in memory only", path)


class ConfigStore(QObject):
    """The live config. Settings code calls ``update``; everything else listens to ``changed``."""

    changed = Signal(object)  # the new Config

    def __init__(self, path: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path or default_config_path()
        self._config = load_config(self._path)

    @property
    def config(self) -> Config:
        return self._config

    def update(self, new: Config) -> None:
        """Save ``new`` (atomically) and announce it. Does nothing if it equals the current one."""
        if new == self._config:
            return
        save_config(new, self._path)
        self._config = new
        self.changed.emit(new)

    def reload(self) -> None:
        """Re-read the file, e.g. after it was edited by hand. Emits ``changed`` if it differs."""
        fresh = load_config(self._path)
        if fresh != self._config:
            self._config = fresh
            self.changed.emit(fresh)
