"""
When a staff alert rings again (owner, 2026-11-11): every 2 minutes for 30
minutes (at most 15 rings); a ring that would fall between 22:00 and 07:00
Colombo moves to 07:00 and starts a fresh 30-minute window there.
"""
from datetime import datetime, timedelta, timezone

from app.services.staff_order_alert_service import MAX_RINGS, plan_next_ring

UTC = timezone.utc


def colombo(h, m=0, day=11):
    # Colombo is UTC+05:30, no daylight saving.
    return datetime(2026, 11, day, h, m, tzinfo=UTC) - timedelta(hours=5, minutes=30) + timedelta(0)


def test_day_order_rings_every_two_minutes():
    t0 = colombo(10)
    nxt, window, rings = plan_next_ring(t0, window_started_at=t0, rings=1)
    assert nxt == t0 + timedelta(minutes=2)
    assert window == t0 and rings == 1


def test_stops_after_thirty_minutes():
    t0 = colombo(10)
    at = t0 + timedelta(minutes=28)
    nxt, _, _ = plan_next_ring(at, window_started_at=t0, rings=14)
    assert nxt == t0 + timedelta(minutes=30), "the 30th minute still rings"
    nxt, window, rings = plan_next_ring(t0 + timedelta(minutes=30), window_started_at=t0, rings=14)
    # Not accepted after a day window: once more from the next 07:00.
    assert nxt == colombo(7, 0, day=12) and window == nxt and rings == 0


def test_never_more_than_fifteen_rings():
    t0 = colombo(10)
    nxt, _, _ = plan_next_ring(t0 + timedelta(minutes=5), window_started_at=t0, rings=MAX_RINGS)
    assert nxt == colombo(7, 0, day=12), "the cap ends the day window, not the order"


def test_full_day_window_is_fifteen_rings():
    t0 = colombo(10)
    at, window, rings, count = t0, t0, 1, 1
    while True:
        nxt, window, rings = plan_next_ring(at, window_started_at=window, rings=rings)
        if rings == 0:
            break  # moved to the next morning
        at, rings, count = nxt, rings + 1, count + 1
    assert count == MAX_RINGS
    assert at == t0 + timedelta(minutes=28)
    assert window == colombo(7, 0, day=12), "then the 07:00 restart"


def test_night_order_rings_once_then_at_seven():
    t0 = colombo(23, 15)
    nxt, window, rings = plan_next_ring(t0, window_started_at=t0, rings=1)
    assert nxt == colombo(7, 0, day=12)
    assert window == nxt and rings == 0, "07:00 opens a fresh window"
    # 07:00 ring is ring 1 of the new window, then every 2 minutes.
    nxt2, window2, _ = plan_next_ring(nxt, window_started_at=window, rings=1)
    assert nxt2 == nxt + timedelta(minutes=2) and window2 == window


def test_early_morning_order_waits_for_seven_the_same_day():
    t0 = colombo(5, 30, day=12)
    nxt, _, _ = plan_next_ring(t0, window_started_at=t0, rings=1)
    assert nxt == colombo(7, 0, day=12)


def test_evening_ringing_that_runs_into_the_night_pauses_until_morning():
    t0 = colombo(21, 50)
    nxt, _, _ = plan_next_ring(colombo(21, 58), window_started_at=t0, rings=5)
    assert nxt == colombo(7, 0, day=12)


def test_a_morning_window_that_ends_stops_for_good():
    seven = colombo(7, 0, day=12)
    nxt, _, _ = plan_next_ring(seven + timedelta(minutes=28), window_started_at=seven, rings=15)
    assert nxt is None
    nxt, _, _ = plan_next_ring(seven + timedelta(minutes=30), window_started_at=seven, rings=5)
    assert nxt is None


def test_a_day_order_that_rang_out_comes_back_at_seven_then_stops():
    t0 = colombo(18)
    nxt, window, rings = plan_next_ring(t0 + timedelta(minutes=28), window_started_at=t0, rings=15)
    assert nxt == colombo(7, 0, day=12)
    at, count = nxt, 1
    while True:
        nxt, window, rings = plan_next_ring(at, window_started_at=window, rings=count)
        if nxt is None:
            break
        at, count = nxt, count + 1
    assert count == MAX_RINGS

