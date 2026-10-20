import math

import pytest

from dragshift.ui import wheel_geometry as geo

MID_R = (geo.R_IN + geo.R_OUT) / 2
COUNTS = range(1, 9)


@pytest.mark.parametrize("n", COUNTS)
def test_sectors_run_clockwise_from_twelve_oclock(n: int) -> None:
    angles = geo.sector_angles(n)
    step = 2 * math.pi / n
    assert len(angles) == n
    assert angles[0][1] == pytest.approx(-math.pi / 2)  # 12 o'clock on a y-down screen
    for i, (start, mid, end) in enumerate(angles):
        assert mid == pytest.approx(-math.pi / 2 + i * step)  # increasing angle = clockwise
        assert end - start == pytest.approx(step)


def test_no_sectors_for_nothing() -> None:
    assert geo.sector_angles(0) == []
    assert geo.sector_paths(0, geo.R_IN, geo.R_OUT) == []


@pytest.mark.parametrize("n", COUNTS)
def test_hit_test_finds_each_petal_at_its_centre(n: int) -> None:
    paths = geo.sector_paths(n, geo.R_IN, geo.R_OUT)
    for i, (_, mid, _) in enumerate(geo.sector_angles(n)):
        assert geo.hit_test(paths, geo.polar(mid, MID_R)) == i


@pytest.mark.parametrize("n", COUNTS)
def test_gaps_between_petals_hit_nothing(n: int) -> None:
    paths = geo.sector_paths(n, geo.R_IN, geo.R_OUT)
    for _, _, boundary in geo.sector_angles(n):
        for r in (70, 100, 130):
            assert geo.hit_test(paths, geo.polar(boundary, r)) is None


@pytest.mark.parametrize("n", range(2, 9))
def test_gap_is_six_pixels_wide(n: int) -> None:
    paths = geo.sector_paths(n, geo.R_IN, geo.R_OUT)
    _, _, boundary = geo.sector_angles(n)[0]
    r = 100
    inside_gap = math.asin(2.5 / r)  # 2.5 px from the boundary line, gap half-width is 3 px
    beside_gap = math.asin(4 / r)
    assert geo.hit_test(paths, geo.polar(boundary - inside_gap, r)) is None
    assert geo.hit_test(paths, geo.polar(boundary + inside_gap, r)) is None
    assert geo.hit_test(paths, geo.polar(boundary - beside_gap, r)) == 0
    assert geo.hit_test(paths, geo.polar(boundary + beside_gap, r)) == 1 % n


@pytest.mark.parametrize("n", (1, 2, 5, 8))
@pytest.mark.parametrize("r", (0, 20, geo.R_HUB, 53, geo.R_IN - 2))  # hub and its dead band
def test_hub_hits_nothing(n: int, r: float) -> None:
    paths = geo.sector_paths(n, geo.R_IN, geo.R_OUT)
    for _, mid, _ in geo.sector_angles(n):
        assert geo.hit_test(paths, geo.polar(mid, r)) is None


@pytest.mark.parametrize("n", (1, 2, 5, 8))
@pytest.mark.parametrize("r", (geo.R_OUT + 2, geo.R_DISC, 300))
def test_outside_the_petal_ring_hits_nothing(n: int, r: float) -> None:
    paths = geo.sector_paths(n, geo.R_IN, geo.R_OUT)
    for _, mid, _ in geo.sector_angles(n):
        assert geo.hit_test(paths, geo.polar(mid, r)) is None


@pytest.mark.parametrize("n", range(2, 9))
def test_petals_stay_in_their_annulus_and_do_not_touch(n: int) -> None:
    paths = geo.sector_paths(n, geo.R_IN, geo.R_OUT)
    for path in paths:
        box = path.boundingRect()
        assert max(abs(box.left()), abs(box.right()), abs(box.top()), abs(box.bottom())) <= (
            geo.R_OUT + 0.5
        )
    for a, b in zip(paths, paths[1:] + paths[:1], strict=True):
        assert not a.intersects(b)


def test_a_single_petal_is_a_ring_with_its_gap_at_six_oclock() -> None:
    paths = geo.sector_paths(1, geo.R_IN, geo.R_OUT)
    assert geo.hit_test(paths, geo.polar(-math.pi / 2, MID_R)) == 0  # 12
    assert geo.hit_test(paths, geo.polar(0, MID_R)) == 0  # 3
    assert geo.hit_test(paths, geo.polar(math.pi, MID_R)) == 0  # 9
    assert geo.hit_test(paths, geo.polar(math.pi / 2, MID_R)) is None  # 6, the gap


@pytest.mark.parametrize("n", (1, 2, 9, 16))
def test_more_ring_hit_test(n: int) -> None:
    paths = geo.sector_paths(n, geo.RING_IN, geo.RING_OUT)
    ring_mid = (geo.RING_IN + geo.RING_OUT) / 2
    for i, (_, mid, boundary) in enumerate(geo.sector_angles(n)):
        assert geo.hit_test(paths, geo.polar(mid, ring_mid)) == i
        assert geo.hit_test(paths, geo.polar(boundary, ring_mid)) is None  # gap
        assert geo.hit_test(paths, geo.polar(mid, geo.RING_IN - 5)) is None  # inside the ring
        assert geo.hit_test(paths, geo.polar(mid, geo.RING_OUT + 5)) is None  # outside it
