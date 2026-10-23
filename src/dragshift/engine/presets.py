"""Size presets shared by every Compress tool (PRD FR-4.3, FR-4.4).

``tools.<kind>.compress`` has an outer ring of presets: Email, WhatsApp, Discord, Small and
Custom… All of them run the same worker function, which reads the preset from
``params["preset"]`` (``params_for`` gives it from the ring Op's id; a drop on Compress itself
runs Small). Custom… opens the compress dialog, which adds ``target_mb`` or ``quality``.

Sizes are counted as Windows Explorer shows them (1 MB = 1,048,576 bytes), so the size in a
toast, in the note and in Explorer always agree.

Pure Python: the UI process imports this module to register Ops (CONTRIBUTING.md rule 2).
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op, register

KB = 1024
MB = 1024 * KB
RETRY_MARGIN = 0.97  # a second encode aims this much under the scaled size
SMALL = "small"  # a drop on Compress itself
CUSTOM = "custom"


@dataclass(frozen=True)
class Preset:
    key: str
    label: str  # ring petal label, also the toast's summary
    mb: float | None = None  # None: half the source size (Small), or the dialog's (Custom…)


RING = (
    Preset("email", "Email 25 MB", 25),
    Preset("whatsapp", "WhatsApp 16 MB", 16),
    Preset("discord", "Discord 10 MB", 10),
    Preset(SMALL, "Small 50 %"),
    Preset(CUSTOM, "Custom…"),
)
_BY_KEY = {p.key: p for p in RING}


@dataclass(frozen=True)
class Target:
    """What one file is compressed to: a size in bytes, or a quality (0-100)."""

    size: int | None = None  # the size to stay under; None in quality mode
    quality: int | None = None
    limit: int | None = None  # the size the user asked for, for the FR-4.4 note
    already_under: bool = False  # the source is under ``limit`` already


def compress_op(kind: Kind) -> Callable[[Callable], Callable]:
    """Decorator: register ``tools.<kind>.compress`` and its preset ring (FR-4.3), all running
    the decorated function."""

    def deco(fn: Callable) -> Callable:
        func = f"{fn.__module__}:{fn.__qualname__}"
        kinds = frozenset({kind})
        base = f"tools.{kind}.compress"
        ring = tuple(f"{base}.{p.key}" for p in RING)
        register(Op(id=base, wheel="tools", label="Compress ▸", kinds=kinds, func=func,
                    children=ring))  # fmt: skip
        for p, op_id in zip(RING, ring, strict=True):
            dialog = "compress" if p.key == CUSTOM else None
            register(Op(id=op_id, wheel="tools", label=p.label, kinds=kinds, func=func,
                        dialog=dialog))  # fmt: skip
        return fn

    return deco


def is_compress(op_id: str) -> bool:
    """A Compress Op or one of its presets."""
    return op_id.startswith("tools.") and (op_id.endswith(".compress") or ".compress." in op_id)


def params_for(op_id: str) -> dict:
    """The params a preset ring Op needs: its preset (``tools.video.compress.discord``)."""
    head, _, key = op_id.rpartition(".")
    return {"preset": key} if head.endswith(".compress") and key in _BY_KEY else {}


def target_for(params: dict, src_size: int) -> Target:
    """The target for one source of ``src_size`` bytes. Raises ValueError on a bad preset."""
    key = params.get("preset") or SMALL
    if key == CUSTOM:
        if params.get("target_mb") is not None:
            mb = float(params["target_mb"])
            if mb <= 0:
                raise ValueError("the target size must be more than 0 MB")
            return _sized(int(mb * MB), src_size)
        return Target(quality=min(100, max(1, int(params.get("quality", 75)))))
    preset = _BY_KEY.get(key)
    if preset is None:
        known = ", ".join(_BY_KEY)
        raise ValueError(f"no size preset {key!r} (one of: {known})")
    if preset.mb is None:  # Small: half the source
        half = max(1, src_size // 2)
        return Target(size=half, limit=half)
    return _sized(int(preset.mb * MB), src_size)


def _sized(limit: int, src_size: int) -> Target:
    """A file under the limit already is still compressed, but never made larger than it was."""
    return Target(size=min(limit, src_size), limit=limit, already_under=src_size <= limit)


def retarget(target: Target, out_size: int) -> Target | None:
    """For an encoder that missed: the size to ask for on one more try, scaled by how far it
    overshot (two-pass x264 and AAC land a few % over on short files). None if it fit."""
    if target.size is None or out_size <= target.size:
        return None
    return replace(target, size=int(target.size * target.size / out_size * RETRY_MARGIN))


def human_size(n: int) -> str:
    """Bytes as Explorer shows them: 512 B, 860 KB, 1.2 MB, 14 MB."""
    if n < KB:
        return f"{n} B"
    value = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        value /= KB
        if value < KB or unit == "TB":
            break
    return f"{value:.1f} {unit}" if value < 10 else f"{value:.0f} {unit}"


def mb_text(n: int, places: int = 1) -> str:
    """``10 MB``, ``2.5 MB``, ``14.2 MB``: a limit as the user typed it, a size to ``places``.
    Under 1 MB: ``640 KB``."""
    if n < MB:
        return f"{n} B" if n < KB else f"{n / KB:.0f} KB"
    value = n / MB
    return f"{value:.0f} MB" if value == round(value) else f"{value:.{places}f} MB"


def size_note(target: Target, out_size: int) -> str:
    """FR-4.4: the line for a successful job's toast, or "" when the target was met."""
    if target.limit is None:
        return ""
    if target.already_under:
        return f"Already under {mb_text(target.limit)}"
    if out_size > target.limit:
        # One more place when one would round down to the limit: never "10 MB — got 10.0 MB".
        places = 2 if round(out_size / MB, 1) <= target.limit / MB else 1
        return f"Couldn't reach {mb_text(target.limit)} — got {mb_text(out_size, places)}"
    return ""
