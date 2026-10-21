"""What a wheel shows: Op ordering (FR-2.2), the 8-petal / More… split (FR-2.1), and the outer
rings of Ops with children (Copy to ▸, Compress ▸)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from dragshift.engine.kinds import input_kind
from dragshift.engine.registry import Op

__all__ = [
    "MAX_PETALS",
    "MAX_RING",
    "Petal",
    "WheelLayout",
    "build_layout",
    "input_kind",
    "order_ops",
]

MAX_PETALS = 8  # petals on the wheel itself, More… included
MAX_RING = 16  # Ops in the More… ring; any beyond this are dropped
MORE_LABEL = "More…"


@dataclass(frozen=True)
class Petal:
    op_id: str | None  # None marks the More… petal
    label: str
    enabled: bool = True
    ring: tuple["Petal", ...] = ()  # an Op's children, shown in an outer ring on hover

    @property
    def is_more(self) -> bool:
        return self.op_id is None


@dataclass(frozen=True)
class WheelLayout:
    petals: tuple[Petal, ...] = ()
    ring: tuple[Petal, ...] = ()  # the More… ring; empty unless the last petal is More…


def order_ops(
    ops: Sequence[Op], pinned: Sequence[str] = (), usage: Mapping[str, int] | None = None
) -> list[Op]:
    """FR-2.2: pinned Ops first in pinned order, then by usage count (descending).

    Ties keep the registry's default order, because the sort is stable.
    """
    usage = usage or {}
    rank = {op_id: i for i, op_id in enumerate(pinned)}
    unpinned = len(rank)
    return sorted(
        ops, key=lambda o: (rank[o.id], 0) if o.id in rank else (unpinned, -usage.get(o.id, 0))
    )


def build_layout(
    ops: Sequence[Op],
    *,
    enabled: bool = True,
    children: Mapping[str, Sequence[Op]] | None = None,
) -> WheelLayout:
    """Up to 8 petals. With more Ops, 7 plus More… and a ring of up to 16 (FR-2.1).
    ``children`` holds, per Op id, the Ops of that petal's own outer ring."""
    children = children or {}

    def petal(o: Op) -> Petal:
        ring = tuple(Petal(c.id, c.label, enabled) for c in children.get(o.id, ()))
        return Petal(o.id, o.label, enabled, ring)

    if len(ops) <= MAX_PETALS:
        return WheelLayout(tuple(map(petal, ops)))
    head = ops[: MAX_PETALS - 1]
    ring = ops[MAX_PETALS - 1 :][:MAX_RING]
    more = Petal(None, MORE_LABEL, enabled)
    return WheelLayout((*map(petal, head), more), tuple(map(petal, ring)))
