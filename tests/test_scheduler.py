"""The schedule runner: firing treats, retries, double launches and restarts."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from .conftest import HAVE_HA, TZ, FakeClock, FakeStore, FakeTimers

pytestmark = pytest.mark.skipif(HAVE_HA, reason="runs against stand-ins; use HA's harness instead")

if not HAVE_HA:
    from custom_components.furbo_treats.api import BridgeError
    from custom_components.furbo_treats.const import EVENT_SCHEDULED_TREAT
    from custom_components.furbo_treats.scheduler import ScheduleManager


def at(hour: int, minute: int = 0, second: int = 0, day: int = 29) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, second, tzinfo=TZ)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, name, data):
        self.events.append((name, data))


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()


class FakeClient:
    def __init__(self) -> None:
        self.tosses = 0
        self.errors: list[BridgeError] = []

    async def toss(self, camera_id, count=1):
        if self.errors:
            raise self.errors.pop(0)
        self.tosses += 1
        return {"results": ["acknowledged"]}


class FakeCoordinator:
    async def async_request_refresh(self):
        pass


@pytest.fixture(autouse=True)
def _reset():
    FakeTimers.reset()
    FakeStore.saved = {}
    FakeClock.now = at(6)


def setup(**settings):
    hass, client = FakeHass(), FakeClient()
    manager = ScheduleManager(hass, "entry", client, FakeCoordinator())
    asyncio.run(manager.async_load([{"id": "cam", "name": "Living room"}]))
    scheduler = manager.schedulers["cam"]
    if settings:
        scheduler.update_settings(**settings)
    return hass, client, manager, scheduler


def armed() -> list[dt.datetime]:
    return [when for when, _, cancelled in FakeTimers.points if not cancelled]


def fire_next(at_time: dt.datetime | None = None) -> None:
    entry = next(e for e in FakeTimers.points if not e[2])
    entry[2] = True
    FakeClock.now = at_time or entry[0]
    asyncio.run(entry[1](FakeClock.now.astimezone(dt.UTC)))


def test_off_by_default_and_turning_on_plans_the_day():
    _, _, _, s = setup()
    assert s.plan == [] and armed() == []
    s.update_settings(enabled=True)
    assert s.plan == [at(8), at(13), at(18)]
    assert armed() == [at(8)]


def test_firing_tosses_counts_and_arms_the_next():
    hass, client, _, s = setup(enabled=True)
    fire_next()
    assert client.tosses == 1 and s.given == 1 and s.last_given == at(8)
    assert armed() == [at(13)]
    name, data = hass.bus.events[-1]
    assert name == EVENT_SCHEDULED_TREAT and data["success"] is True
    assert FakeStore.saved["furbo_treats.entry"]["cam"]["given"] == 1


def test_restart_keeps_random_times_and_progress():
    _, _, _, s = setup(enabled=True, mode="random", per_day=4)
    first_plan = list(s.plan)
    fire_next()
    remaining = list(s.plan)
    assert remaining == first_plan[1:]

    # Home Assistant restarts a bit later the same day.
    FakeTimers.reset()
    FakeClock.now = first_plan[0] + dt.timedelta(minutes=1)
    _, _, _, again = setup()
    assert again.given == 1
    assert again.plan == remaining  # not re-rolled
    assert armed() == remaining[:1]


def test_treat_missed_while_down_is_skipped_not_dumped():
    _, client, _, s = setup(enabled=True)
    FakeTimers.reset()
    FakeClock.now = at(14)  # back up after the 08:00 and 13:00 slots
    _, client2, _, again = setup()
    assert again.plan == [at(18)]
    assert client2.tosses == 0


def test_failed_treat_retried_once_two_minutes_later():
    hass, client, _, s = setup(enabled=True)
    client.errors = [BridgeError("camera offline", "camera"), BridgeError("still offline", "camera")]
    fire_next()
    assert s.given == 0
    assert armed() == [at(8, 2)]
    assert hass.bus.events[-1][1]["retry_at"] == at(8, 2).isoformat()
    fire_next()  # the retry fails too: no third attempt
    assert armed() == [at(13)]
    fire_next()
    assert s.given == 1


def test_daily_limit_refusal_is_not_retried():
    _, client, _, s = setup(enabled=True)
    client.errors = [BridgeError("Daily limit reached", "rate_limited")]
    fire_next()
    assert armed() == [at(13)]


def test_double_launch_fires_thirty_seconds_later_without_using_the_count():
    hass, client, _, s = setup(enabled=True, double_enabled=True, double_chance=100)
    fire_next()
    assert [d for d, _, c in FakeTimers.later if not c] == [30]
    delay_entry = FakeTimers.later[0]
    FakeClock.now = at(8, 0, 30)
    asyncio.run(delay_entry[1](FakeClock.now))
    assert client.tosses == 2
    assert s.given == 1 and s.doubles == 1
    assert hass.bus.events[-1][1]["double_launch"] is True


def test_double_launch_off_or_unlucky():
    _, _, _, s = setup(enabled=True, double_enabled=False, double_chance=100)
    fire_next()
    assert FakeTimers.later == []
    s.update_settings(double_enabled=True, double_chance=1)
    s._rng.random = lambda: 0.5  # a 50 roll misses a 1% chance
    fire_next()
    assert FakeTimers.later == []


def fire_bonus(at_time: dt.datetime) -> None:
    entry = next(e for e in FakeTimers.later if not e[2])
    entry[2] = True
    FakeClock.now = at_time
    asyncio.run(entry[1](at_time))


def test_triple_launch_follows_a_double():
    hass, client, _, s = setup(
        enabled=True, double_enabled=True, double_chance=100, triple_enabled=True, triple_chance=100
    )
    fire_next()
    fire_bonus(at(8, 0, 30))
    assert len([e for e in FakeTimers.later if not e[2]]) == 1  # the triple is armed
    fire_bonus(at(8, 1, 0))
    assert client.tosses == 3
    assert (s.given, s.doubles, s.triples) == (1, 1, 1)
    assert hass.bus.events[-1][1]["triple_launch"] is True
    assert FakeTimers.later[-1][2]  # nothing further after a triple


def test_triple_needs_a_double_first():
    _, client, _, s = setup(enabled=True, double_enabled=False, triple_enabled=True, triple_chance=100)
    fire_next()
    assert FakeTimers.later == []
    assert client.tosses == 1


def test_failed_double_never_rolls_a_triple():
    _, client, _, s = setup(
        enabled=True, double_enabled=True, double_chance=100, triple_enabled=True, triple_chance=100
    )
    fire_next()
    client.errors = [BridgeError("camera offline", "camera")]
    fire_bonus(at(8, 0, 30))
    assert [e for e in FakeTimers.later if not e[2]] == []
    assert (s.doubles, s.triples) == (0, 0)


def test_triple_off_stops_at_double():
    _, client, _, s = setup(
        enabled=True, double_enabled=True, double_chance=100, triple_enabled=False, triple_chance=100
    )
    fire_next()
    fire_bonus(at(8, 0, 30))
    assert [e for e in FakeTimers.later if not e[2]] == []
    assert client.tosses == 2


def test_changing_double_settings_does_not_replan():
    _, _, _, s = setup(enabled=True, mode="random")
    plan = list(s.plan)
    s.update_settings(double_enabled=True, double_chance=50)
    assert s.plan == plan


def test_changing_count_mid_day_keeps_what_was_given():
    _, _, _, s = setup(enabled=True, per_day=3)
    fire_next()  # 08:00
    FakeClock.now = at(9)
    s.update_settings(per_day=2)
    # Fixed slots for 2 a day are 08:00 and 18:00; 08:00 is done, 18:00 to go.
    assert s.plan == [at(18)]


def test_new_day_resets():
    _, _, manager, s = setup(enabled=True, double_enabled=True, double_chance=100)
    fire_next()
    FakeClock.now = at(0, 0, 5, day=30)
    FakeTimers.daily[0](FakeClock.now)
    assert s.given == 0 and s.doubles == 0
    assert s.plan[0] == at(8, day=30)


def test_unload_cancels_timers():
    _, _, manager, s = setup(enabled=True)
    manager.async_unload()
    assert armed() == []


# --- regressions from the bug check ------------------------------------------


def test_lost_answer_is_not_retried_because_the_treat_may_have_gone_out():
    from custom_components.furbo_treats.api import BridgeConnectionError

    _, client, _, s = setup(enabled=True)
    client.errors = [BridgeConnectionError("timed out")]
    fire_next()
    assert armed() == [at(13)]  # no retry at 08:02


def test_settings_change_during_a_toss_does_not_add_an_extra_treat():
    _, client, _, s = setup(enabled=True, per_day=3, mode="random", min_gap_minutes=15)
    original = client.toss

    async def slow_toss(camera_id, count=1):
        # While the 1st treat is in flight, the person nudges the window.
        s.update_settings(end=dt.time(18, 30))
        return await original(camera_id, count)

    client.toss = slow_toss
    fire_next()
    assert s.given == 1
    assert len(s.plan) == 2  # 3 per day, 1 given: not 3 more
    assert s.plan[0] >= s.last_given + dt.timedelta(minutes=15)


def test_unload_cancels_pending_bonus_launches():
    _, _, manager, s = setup(enabled=True, double_enabled=True, double_chance=100)
    fire_next()
    assert [e for e in FakeTimers.later if not e[2]]
    manager.async_unload()
    assert not [e for e in FakeTimers.later if not e[2]]


def test_unexpected_error_does_not_stall_the_rest_of_the_day():
    _, client, _, s = setup(enabled=True)
    client.errors = [RuntimeError("boom")]
    fire_next()
    assert armed() == [at(13)]  # no retry (outcome unknown), but the day goes on
    fire_next()
    assert s.given == 1
