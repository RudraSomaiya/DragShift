"""Tools with an outer ring of fixed choices (PRD §6.2): Resize ▸, Rotate ▸, Bitrate ▸.

``@ring_op`` registers the ring's parent Op and one child per choice, all running the decorated
worker function. The worker reads the choice from its params (``params_for`` gives them from the
Op's id, Compress presets included; a drop on the parent itself runs the ``default`` choice).

Pure Python: the UI process imports this module to register Ops (CLAUDE.md rule 2).
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from dragshift.engine import presets
from dragshift.engine.kinds import Kind
from dragshift.engine.registry import Op, register

_PARAMS: dict[str, dict] = {}  # Op id -> the params of that choice


@dataclass(frozen=True)
class Choice:
    key: str  # last part of the Op id, e.g. "1080p"
    label: str  # ring petal label
    params: dict = field(default_factory=dict)


def ring_op(
    kind: Kind, name: str, label: str, choices: Sequence[Choice], default: str | None = None
) -> Callable:
    """Decorator: register ``tools.<kind>.<name>`` and ``tools.<kind>.<name>.<key>`` per choice.
    A drop on the parent runs the choice with key ``default`` (the first one if not given)."""

    def deco(fn: Callable) -> Callable:
        func = f"{fn.__module__}:{fn.__qualname__}"
        kinds = frozenset({kind})
        base = f"tools.{kind}.{name}"
        ids = tuple(f"{base}.{c.key}" for c in choices)
        register(Op(id=base, wheel="tools", label=f"{label} ▸", kinds=kinds, func=func,
                    children=ids))  # fmt: skip
        _PARAMS[base] = next((c for c in choices if c.key == default), choices[0]).params
        for c, op_id in zip(choices, ids, strict=True):
            register(Op(id=op_id, wheel="tools", label=c.label, kinds=kinds, func=func))
            _PARAMS[op_id] = c.params
        return fn

    return deco


def params_for(op_id: str) -> dict:
    """The params of a ring Op: a Compress preset (``tools.video.compress.discord``) or a choice
    (``tools.image.resize.1080p``); empty for any other Op."""
    return presets.params_for(op_id) | _PARAMS.get(op_id, {})
