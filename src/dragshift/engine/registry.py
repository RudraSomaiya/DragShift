"""The Op registry (PRD §9.4).

Ops are declared next to their worker function with ``@op``. Op modules MUST import engine
libraries (Pillow, fitz, …) inside the function bodies, never at module level: the UI process
imports them to build its wheels (CLAUDE.md rule 2).
"""

import importlib
import pkgutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dragshift.engine.kinds import Kind, kind_of, normalize_ext

Wheel = Literal["convert", "tools", "actions"]


@dataclass(frozen=True)
class Op:
    id: str  # stable, e.g. "convert.image.png", "tools.image.compress", "actions.copy_path"
    wheel: Wheel
    label: str  # petal label, e.g. "PNG", "Compress"
    kinds: frozenset[Kind]  # input kinds accepted
    func: str  # dotted path "dragshift.engine.converters.images:to_png", imported in the worker
    multi: Literal["each", "all"] = "each"
    min_files: int = 1
    icon: str | None = None  # name of an icon in ui/icons
    dialog: str | None = None  # "crop" | "add_bg" | "compress" | "pages"; the UI opens it
    children: tuple[str, ...] = ()  # sub-ops shown in the outer ring (e.g. compress presets)
    where: Literal["worker", "ui"] = "worker"
    available: Callable[[], bool] = lambda: True  # e.g. office installed
    hide_if_same_ext: str | None = None  # hide the "PNG" petal for .png input


# Packages whose modules declare Ops. Add tools/actions here when those packages exist.
_OP_PACKAGES = ("dragshift.engine.converters",)

_OPS: dict[str, Op] = {}  # insertion order = default petal order (PRD §6)
_loaded = False


def register(op: Op) -> Op:
    if op.id in _OPS:
        if _OPS[op.id] == op:
            return op  # same module imported again after a failed load
        raise ValueError(f"duplicate op id: {op.id}")
    _OPS[op.id] = op
    return op


def op(**fields: Any) -> Callable[[Callable], Callable]:
    """Decorator: register an Op whose ``func`` is the decorated worker function."""

    def deco(fn: Callable) -> Callable:
        register(Op(func=f"{fn.__module__}:{fn.__qualname__}", **fields))
        return fn

    return deco


def _ensure_loaded() -> None:
    """Import every Op module once. If one fails, the error is raised again on the next call
    rather than leaving a silently partial registry."""
    global _loaded
    if _loaded:
        return
    for pkg_name in _OP_PACKAGES:
        pkg = importlib.import_module(pkg_name)
        for mod in pkgutil.iter_modules(pkg.__path__, f"{pkg_name}."):
            importlib.import_module(mod.name)
    _loaded = True


def get(op_id: str) -> Op:
    _ensure_loaded()
    return _OPS[op_id]


_Info = tuple[Kind, str]  # (kind, normalized extension) of one input path


def _infos(paths: Sequence[Path]) -> list[_Info]:
    return [(kind_of(p), normalize_ext(p.suffix)) for p in paths]  # one stat per path


def _fits(op: Op, infos: list[_Info]) -> bool:
    if not infos or len(infos) < op.min_files:
        return False
    same = normalize_ext(op.hide_if_same_ext) if op.hide_if_same_ext else None
    return all(kind in op.kinds and ext != same for kind, ext in infos)


def accepts(op: Op, paths: Sequence[Path]) -> bool:
    """True if ``op`` can run on all of ``paths`` (kinds, file count, same-format rule)."""
    return _fits(op, _infos(paths))


def ops_for(paths: Sequence[Path], wheel: Wheel) -> list[Op]:
    """Ops on ``wheel`` valid for every path (FR-1.11), available (FR-2.4), in default order."""
    _ensure_loaded()
    infos = _infos(paths)
    return [o for o in _OPS.values() if o.wheel == wheel and _fits(o, infos) and o.available()]
