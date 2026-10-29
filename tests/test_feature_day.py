"""Feature check: whole simulated days with every schedule feature switched on.

Drives the real scheduler through complete days, firing every timer it arms in
time order, and checks the resulting treat timeline against what was asked for:
a set number of treats per day, inside the window, fixed or random, at least the
minimum gap apart, with chance-based double launches 30 s after a treat and
triple launches 30 s after a double.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from .conftest import HAVE_HA, TZ, FakeClock, FakeStore, FakeTimers

pytestmark = pytest.mark.skipif(HAVE_HA, reason="runs against stand-ins")

if not HAVE_HA:
    from custom_components.furbo_treats.scheduler import ScheduleManager

    from .test_scheduler import FakeClient, FakeCoordinator, FakeHass


def run_day(day: int, **settings) -> list[tuple[dt.datetime, str]]:
    """Simulate one day from 06:00; return (time, kind) for every treat tossed."""
    FakeTimers.reset()
    FakeStore.saved = {}
    FakeClock.now = dt.datetime(2026, 10, day, 6, 0, tzinfo=TZ)
    hass, client = FakeHass(), FakeClient()
    manager = ScheduleManager(hass, "entry", client, FakeCoordinator())
    asyncio.run(manager.async_load([{"id": "cam", "name": "Cam"}]))
    manager.schedulers["cam"].update_settings(**settings)

    due_later: dict[int, dt.datetime] = {}  # id(timer entry) -> when it fires
    fired: list[dt.datetime] = []
    while True:
        for entry in FakeTimers.later:
            due_later.setdefault(id(entry), FakeClock.now + dt.timedelta(seconds=entry[0]))
        pending = [(e[0], e, "point") for e in FakeTimers.points if not e[2]]
        pending += [(due_later[id(e)], e, "later") for e in FakeTimers.later if not e[2]]
        if not pending:
            break
        when, entry, _ = min(pending, key=lambda p: p[0])
        entry[2] = True
        FakeClock.now = when
        asyncio.run(entry[1](when))
        fired.append(when)

    # Every timer fire tosses once and fires exactly one event, in order.
    assert len(fired) == len(hass.bus.events) == client.tosses
    timeline = []
    for when, (_, data) in zip(fired, hass.bus.events, strict=True):
        assert data["success"]
        kind = "triple" if data.get("triple_launch") else "double" if data["double_launch"] else "main"
        timeline.append((when, kind))
    return timeline


ALL_ON = {
    "enabled": True,
    "per_day": 5,
    "start": dt.time(8),
    "end": dt.time(18),
    "min_gap_minutes": 60,
    "double_enabled": True,
    "double_chance": 50,
    "triple_enabled": True,
    "triple_chance": 50,
}


def check_day(timeline, per_day, start, end, gap):
    mains = [t for t, kind in timeline if kind == "main"]
    assert len(mains) == per_day, "treats per day"
    assert all(start <= t.time() <= end for t in mains), "inside the window"
    gaps = [b - a for a, b in zip(mains, mains[1:], strict=False)]
    assert all(g >= dt.timedelta(minutes=gap) for g in gaps), "minimum gap"
    previous = None
    for t, kind in timeline:
        if kind == "double":
            assert previous[1] == "main" and t - previous[0] == dt.timedelta(seconds=30)
        if kind == "triple":
            assert previous[1] == "double" and t - previous[0] == dt.timedelta(seconds=30)
        previous = (t, kind)


def test_random_days_with_everything_on():
    doubles = triples = days = 0
    plans = set()
    for day in range(1, 29):
        timeline = run_day(day, mode="random", **ALL_ON)
        check_day(timeline, 5, dt.time(8), dt.time(18), 60)
        kinds = [k for _, k in timeline]
        doubles += kinds.count("double")
        triples += kinds.count("triple")
        days += 1
        plans.add(tuple(t.time() for t, k in timeline if k == "main"))
    assert len(plans) == days, "random mode picks new times every day"
    # 140 treats at 50 %: about 70 doubles, and about half of those go on to a triple.
    assert 40 <= doubles <= 100
    assert 0.25 * doubles <= triples <= 0.75 * doubles


def test_fixed_days_with_everything_on():
    for day in range(1, 8):
        timeline = run_day(day, mode="fixed", **ALL_ON)
        check_day(timeline, 5, dt.time(8), dt.time(18), 60)
        mains = [t.time() for t, k in timeline if k == "main"]
        assert mains == [dt.time(8), dt.time(10, 30), dt.time(13), dt.time(15, 30), dt.time(18)]


def test_bonuses_off_means_exactly_the_count():
    timeline = run_day(3, mode="random", **{**ALL_ON, "double_enabled": False})
    assert [k for _, k in timeline] == ["main"] * 5


def test_triple_on_but_double_off_never_triples():
    timeline = run_day(3, mode="random", **{**ALL_ON, "double_enabled": False, "triple_chance": 100})
    assert "triple" not in [k for _, k in timeline]


def test_always_double_and_triple():
    timeline = run_day(3, mode="fixed", **{**ALL_ON, "double_chance": 100, "triple_chance": 100})
    assert [k for _, k in timeline] == ["main", "double", "triple"] * 5
