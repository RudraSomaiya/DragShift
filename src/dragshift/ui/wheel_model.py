"""What a wheel shows: Op ordering (FR-2.2) and the 8-petal / More… split (FR-2.1)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dragshift.engine.kinds import kind_of
from dragshift.engine.registry import Op

MAX_PETALS = 8  # petals on the wheel itself, More… included
MAX_RING = 16  # Ops in the More… ring; any beyond this are dropped
MORE_LABEL = "More…"


@dataclass(frozen=True)
class Petal:
    op_id: str | None  # None marks the More… petal
    label: str
    enabled: bool = True

    @property
    def is_more(self) -> bool:
        return self.op_id is None


@dataclass(frozen=True)
class WheelLayout:
    petals: tuple[Petal, ...] = ()
    ring: tuple[Petal, ...] = ()  # the More… ring; empty unless the last petal is More…


def input_kind(paths: Sequence[Path]) -> str | None:
    """The one Kind shared by ``paths`` (the key of pinned order and usage counts), else None."""
    kinds = {kind_of(p) for p in paths}
    return kinds.pop().value if len(kinds) == 1 else None


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


def build_layout(ops: Sequence[Op], *, enabled: bool = True) -> WheelLayout:
    """Up to 8 petals. With more Ops, 7 plus More… and a ring of up to 16 (FR-2.1)."""

    def petal(o: Op) -> Petal:
        return Petal(o.id, o.label, enabled)

    if len(ops) <= MAX_PETALS:
        return WheelLayout(tuple(map(petal, ops)))
    head = ops[: MAX_PETALS - 1]
    ring = ops[MAX_PETALS - 1 :][:MAX_RING]
    more = Petal(None, MORE_LABEL, enabled)
    return WheelLayout((*map(petal, head), more), tuple(map(petal, ring)))
