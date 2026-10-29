"""When scheduled treats happen."""

from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from custom_components.furbo_treats.planner import (
    MODE_FIXED,
    MODE_RANDOM,
    ScheduleSettings,
    fixed_times,
    plan_remaining,
    random_times,
)

TZ = ZoneInfo("America/New_York")
DAY = dt.date(2026, 10, 29)


def at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(DAY, dt.time(hour, minute), tzinfo=TZ)


def settings(**kw) -> ScheduleSettings:
    base = {"enabled": True, "per_day": 3, "start": dt.time(8), "end": dt.time(18), "mode": MODE_FIXED}
    return ScheduleSettings(**{**base, **kw})


def test_fixed_spreads_from_start_to_end():
    assert fixed_times(at(8), at(18), 3) == [at(8), at(13), at(18)]
    assert fixed_times(at(8), at(18), 1) == [at(13)]
    assert fixed_times(at(8), at(18), 0) == []


def test_plan_before_window_is_whole_day():
    plan = plan_remaining(settings(), DAY, at(6), 0, random.Random(1))
    assert plan == [at(8), at(13), at(18)]


def test_fixed_skips_past_slots_never_makes_them_up():
    # Home Assistant came up at 10:00 with nothing given: the 08:00 treat is gone.
    assert plan_remaining(settings(), DAY, at(10), 0, random.Random(1)) == [at(13), at(18)]


def test_fixed_counts_treats_already_given():
    plan = plan_remaining(settings(per_day=3), DAY, at(12), 1, random.Random(1))
    assert plan == [at(13), at(18)]
    assert plan_remaining(settings(per_day=3), DAY, at(12), 3, random.Random(1)) == []
    # Lowered mid-day below what was already given: nothing more today.
    assert plan_remaining(settings(per_day=1), DAY, at(12), 2, random.Random(1)) == []


def test_random_within_window_gapped_and_sorted():
    rng = random.Random(42)
    for _ in range(200):
        plan = plan_remaining(settings(per_day=6, mode=MODE_RANDOM), DAY, at(6), 0, rng)
        assert len(plan) == 6
        assert plan == sorted(plan)
        assert at(8) <= plan[0] and plan[-1] <= at(18)
        gaps = [b - a for a, b in zip(plan, plan[1:], strict=False)]
        assert min(gaps) >= dt.timedelta(minutes=15) - dt.timedelta(seconds=1)


def test_random_differs_day_to_day():
    s = settings(mode=MODE_RANDOM)
    assert plan_remaining(s, DAY, at(6), 0, random.Random(1)) != plan_remaining(
        s, DAY, at(6), 0, random.Random(2)
    )


def test_random_mid_day_uses_rest_of_window_with_lead():
    plan = plan_remaining(settings(per_day=4, mode=MODE_RANDOM), DAY, at(15), 1, random.Random(3))
    assert len(plan) == 3
    assert plan[0] >= at(15, 1)


def test_random_honours_gap_strictly_and_plans_only_what_fits():
    # 30 minutes with a 15-minute gap fits 3 treats (8:00, 8:15, 8:30 at the tightest).
    plan = random_times(at(8), at(8, 30), 5, random.Random(0), dt.timedelta(minutes=15))
    assert len(plan) == 3
    assert plan[-1] <= at(8, 30)
    gaps = [b - a for a, b in zip(plan, plan[1:], strict=False)]
    assert min(gaps) >= dt.timedelta(minutes=14, seconds=59)


def test_custom_gap_in_random_mode():
    s = settings(per_day=4, mode=MODE_RANDOM, min_gap_minutes=120)
    for seed in range(50):
        plan = plan_remaining(s, DAY, at(6), 0, random.Random(seed))
        gaps = [b - a for a, b in zip(plan, plan[1:], strict=False)]
        assert len(plan) == 4 and min(gaps) >= dt.timedelta(minutes=119, seconds=59)


def test_fixed_mode_with_gap_too_big_spreads_what_fits():
    # 10 hours with a 4-hour gap fits 3: 08:00, 13:00, 18:00 rather than 5 squeezed in.
    s = settings(per_day=5, min_gap_minutes=240)
    assert plan_remaining(s, DAY, at(6), 0, random.Random(1)) == [at(8), at(13), at(18)]
    assert "Only 3 of 5" in s.problem()


def test_zero_gap_allows_anything():
    s = settings(per_day=5, min_gap_minutes=0)
    assert s.capacity() == 5 and s.problem() is None


def test_replan_keeps_gap_from_last_treat():
    # A random treat went out at 12:00; changing a setting at 12:05 must not
    # plan the next one before 12:15.
    s = settings(per_day=4, mode=MODE_RANDOM, min_gap_minutes=15)
    for seed in range(50):
        plan = plan_remaining(s, DAY, at(12, 5), 1, random.Random(seed), last_given=at(12))
        assert plan[0] >= at(12, 15)
    # Fixed slots closer than the gap to the last treat are dropped.
    fixed = settings(per_day=3, min_gap_minutes=120)
    assert plan_remaining(fixed, DAY, at(12), 1, random.Random(1), last_given=at(11, 30)) == [at(18)]


@pytest.mark.parametrize(
    "kw",
    [{"enabled": False}, {"start": dt.time(18), "end": dt.time(8)}, {"per_day": 0}],
)
def test_nothing_planned_when_off_or_invalid(kw):
    assert plan_remaining(settings(**kw), DAY, at(6), 0, random.Random(1)) == []


def test_nothing_after_window_closes():
    assert plan_remaining(settings(mode=MODE_RANDOM), DAY, at(19), 0, random.Random(1)) == []


def test_invalid_window_explains_itself():
    assert "overnight" in settings(start=dt.time(20), end=dt.time(6)).problem()
    assert settings().problem() is None


def test_dst_change_day_still_inside_window():
    # 2026-11-01 is the US fall-back day; times stay inside the local window.
    day = dt.date(2026, 11, 1)
    start = dt.datetime.combine(day, dt.time(0, 30), tzinfo=TZ)
    plan = plan_remaining(
        settings(start=dt.time(0, 30), end=dt.time(4), per_day=4),
        day,
        start - dt.timedelta(minutes=10),
        0,
        random.Random(1),
    )
    assert len(plan) == 4 and plan == sorted(plan)
