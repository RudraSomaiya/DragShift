from collections.abc import Callable
from pathlib import Path

import pytest

from dragshift.engine.registry import Op
from dragshift.ui.wheel_model import MAX_PETALS, MAX_RING, build_layout, input_kind, order_ops


def ids(ops: list[Op]) -> list[str]:
    return [o.id for o in ops]


# --- ordering (FR-2.2) ----------------------------------------------------------------------
def test_default_order_is_kept_without_pins_or_usage(make_ops: Callable[[int], list[Op]]) -> None:
    ops = make_ops(4)
    assert order_ops(ops) == ops


def test_pinned_ops_come_first_in_pinned_order(make_ops: Callable[[int], list[Op]]) -> None:
    ops = make_ops(5)
    out = order_ops(ops, pinned=("convert.image.op3", "convert.image.op1"))
    assert ids(out) == [f"convert.image.op{i}" for i in (3, 1, 0, 2, 4)]


def test_unknown_pinned_ids_are_ignored(make_ops: Callable[[int], list[Op]]) -> None:
    out = order_ops(make_ops(3), pinned=("convert.image.gone", "convert.image.op2"))
    assert ids(out) == [f"convert.image.op{i}" for i in (2, 0, 1)]


def test_usage_orders_the_rest_and_ties_keep_the_default_order(
    make_ops: Callable[[int], list[Op]],
) -> None:
    usage = {"convert.image.op4": 5, "convert.image.op3": 5, "convert.image.op1": 1}
    out = order_ops(make_ops(5), usage=usage)
    assert ids(out) == [f"convert.image.op{i}" for i in (3, 4, 1, 0, 2)]


def test_pinned_beats_usage(make_ops: Callable[[int], list[Op]]) -> None:
    out = order_ops(make_ops(3), pinned=("convert.image.op0",), usage={"convert.image.op2": 99})
    assert ids(out) == [f"convert.image.op{i}" for i in (0, 2, 1)]


# --- 8 petals and the More… ring (FR-2.1) ---------------------------------------------------
def test_no_ops_means_no_petals() -> None:
    layout = build_layout([])
    assert layout.petals == () and layout.ring == ()


@pytest.mark.parametrize("n", (1, 2, MAX_PETALS))
def test_up_to_eight_ops_are_all_petals(make_ops: Callable[[int], list[Op]], n: int) -> None:
    layout = build_layout(make_ops(n))
    assert [p.op_id for p in layout.petals] == ids(make_ops(n))
    assert layout.ring == ()
    assert not any(p.is_more for p in layout.petals)


def test_nine_ops_become_seven_petals_more_and_a_ring(
    make_ops: Callable[[int], list[Op]],
) -> None:
    ops = make_ops(9)
    layout = build_layout(ops)
    assert len(layout.petals) == MAX_PETALS
    assert [p.op_id for p in layout.petals[:7]] == ids(ops[:7])
    assert layout.petals[7].is_more and layout.petals[7].label == "More…"
    assert [p.op_id for p in layout.ring] == ids(ops[7:])


@pytest.mark.parametrize("n", (7 + MAX_RING, 7 + MAX_RING + 5))
def test_the_ring_holds_at_most_sixteen(make_ops: Callable[[int], list[Op]], n: int) -> None:
    ops = make_ops(n)
    layout = build_layout(ops)
    assert [p.op_id for p in layout.ring] == ids(ops[7 : 7 + MAX_RING])


def test_disabled_layout_disables_every_petal_including_more(
    make_ops: Callable[[int], list[Op]],
) -> None:
    layout = build_layout(make_ops(12), enabled=False)
    assert not any(p.enabled for p in (*layout.petals, *layout.ring))


# --- input kind -----------------------------------------------------------------------------
def test_input_kind() -> None:
    assert input_kind([Path("a.png"), Path("b.JPG")]) == "image"
    assert input_kind([Path("a.png"), Path("b.pdf")]) is None
    assert input_kind([]) is None
