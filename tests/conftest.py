"""Test setup for the integration's logic.

When Home Assistant is installed, the real package is imported. When it is not
(as in a bare CI or sandbox), the package __init__ is skipped and minimal
stand-ins are registered for the few Home Assistant and aiohttp pieces the
planner and scheduler use: a controllable clock, recorded timers, and an
in-memory store. That is enough to drive the scheduling logic end to end.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import sys
import types
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PKG = "custom_components.furbo_treats"
TZ = ZoneInfo("America/New_York")

try:
    import homeassistant  # noqa: F401

    HAVE_HA = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_HA = False


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class FakeClock:
    now = dt.datetime(2026, 10, 29, 6, 0, tzinfo=TZ)


class FakeTimers:
    """Every timer the code under test arms, so a test can fire them."""

    points: list[list] = []  # [when, action, cancelled]
    later: list[list] = []  # [delay, action, cancelled]
    daily: list = []

    @classmethod
    def reset(cls) -> None:
        cls.points, cls.later, cls.daily = [], [], []


def _track_point(hass, action, when):
    entry = [when, action, False]
    FakeTimers.points.append(entry)

    def cancel():
        entry[2] = True

    return cancel


def _call_later(hass, delay, action):
    entry = [delay, action, False]
    FakeTimers.later.append(entry)

    def cancel():
        entry[2] = True

    return cancel


def _track_time_change(hass, action, **kw):
    FakeTimers.daily.append(action)
    return lambda: None


class FakeStore:
    saved: dict = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return FakeStore.saved.get(self.key)

    def async_delay_save(self, data_func, delay=0):
        FakeStore.saved[self.key] = data_func()


if not HAVE_HA:
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        (PKG, ROOT / "custom_components" / "furbo_treats"),
    ):
        _module(name, __path__=[str(path)])
    _module("homeassistant", __path__=[])
    _module(
        "homeassistant.core",
        CALLBACK_TYPE=object,
        HomeAssistant=object,
        callback=lambda func: func,
    )
    _module("homeassistant.helpers", __path__=[])
    _module(
        "homeassistant.helpers.event",
        async_track_point_in_time=_track_point,
        async_call_later=_call_later,
        async_track_time_change=_track_time_change,
    )
    _module("homeassistant.helpers.storage", Store=FakeStore)
    _module("homeassistant.util", __path__=[])
    _module(
        "homeassistant.util.dt",
        now=lambda: FakeClock.now,
        as_local=lambda value: value.astimezone(TZ),
    )
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        _module(
            "aiohttp",
            ClientTimeout=lambda **kw: kw,
            ClientError=type("ClientError", (Exception,), {}),
            ClientSession=object,
        )
else:
    sys.path.insert(0, str(ROOT))
