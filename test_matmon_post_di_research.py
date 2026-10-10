from matmon_post_di_research import (
    capture_post_di_window,
    current_monotonic_clean,
    consistency_clean,
)


def tick(t, bid, ask):
    return {
        "received_at": t,
        "depth": {
            "buy": [{"price": bid}],
            "sell": [{"price": ask}],
        },
    }


def test_capture_is_exactly_post_di():
    t0 = 100.0

    ticks = [
        tick(99.9, 100.00, 100.05),   # pre-DI: exclude
        tick(100.0, 100.00, 100.05),
        tick(101.0, 100.05, 100.10),
        tick(102.0, 100.10, 100.15),
        tick(103.0, 100.15, 100.20),
        tick(103.1, 100.20, 100.25),  # after window: exclude
    ]

    points = capture_post_di_window(
        ticks,
        di_passed_at=t0,
        window_seconds=3.0,
    )

    assert len(points) == 4
    assert points[0].received_at == 100.0
    assert points[-1].received_at == 103.0


def test_perfect_buy_passes_both():
    points = capture_post_di_window(
        [
            tick(100, 100.00, 100.05),
            tick(101, 100.05, 100.10),
            tick(102, 100.10, 100.15),
            tick(103, 100.15, 100.20),
        ],
        di_passed_at=100,
    )

    assert current_monotonic_clean(points, "BUY").accepted
    assert consistency_clean(points, "BUY").accepted


def test_small_pullback_fails_current():
    points = capture_post_di_window(
        [
            tick(100.0, 100.00, 100.05),
            tick(100.6, 100.05, 100.10),
            tick(101.2, 100.10, 100.15),
            tick(101.8, 100.05, 100.10),  # small pullback
            tick(102.4, 100.15, 100.20),
            tick(103.0, 100.20, 100.25),
        ],
        di_passed_at=100,
    )

    old = current_monotonic_clean(points, "BUY")

    assert not old.accepted


def test_small_pullback_can_pass_consistency():
    points = capture_post_di_window(
        [
            tick(100.0, 100.00, 100.05),
            tick(100.6, 100.05, 100.10),
            tick(101.2, 100.10, 100.15),
            tick(101.8, 100.05, 100.10),  # one opposing transition
            tick(102.4, 100.15, 100.20),
            tick(103.0, 100.20, 100.25),
        ],
        di_passed_at=100,
    )

    new = consistency_clean(
        points,
        "BUY",
        minimum_directional_fraction=0.80,
    )

    assert new.accepted
    assert new.directional_fraction == 0.80


def test_wrong_net_direction_still_rejected():
    points = capture_post_di_window(
        [
            tick(100, 100.20, 100.25),
            tick(101, 100.15, 100.20),
            tick(102, 100.10, 100.15),
            tick(103, 100.05, 100.10),
        ],
        di_passed_at=100,
    )

    assert not consistency_clean(points, "BUY").accepted
    assert consistency_clean(points, "SELL").accepted


def test_less_than_three_seconds_rejected():
    points = capture_post_di_window(
        [
            tick(100, 100.00, 100.05),
            tick(101, 100.05, 100.10),
            tick(102, 100.10, 100.15),
        ],
        di_passed_at=100,
    )

    assert not consistency_clean(points, "BUY").accepted
