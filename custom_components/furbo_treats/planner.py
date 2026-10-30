"""Works out when the day's scheduled treats happen.

Pure Python with no Home Assistant imports, so the timing rules can be tested
on their own. All datetimes are timezone-aware.

Fixed mode spreads the day's treats evenly across the window, first at the
start and last at the end (one treat goes in the middle). Random mode picks new
times every day.

The minimum gap between treats is honoured strictly in both modes. When the
window cannot fit the requested number of treats with that gap, only as many as
fit are planned, and :meth:`ScheduleSettings.problem` says so.

Treats whose time has already passed are skipped, never made up later, so a
restart or a settings change cannot dump several treats at once.
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import math
import random

MODE_FIXED = "fixed"
MODE_RANDOM = "random"
MODES = (MODE_FIXED, MODE_RANDOM)

# A random treat is never planned sooner than this, so changing a setting does
# not toss one the same instant.
RANDOM_LEAD = dt.timedelta(minutes=1)


@dataclass(frozen=True)
class ScheduleSettings:
    """What the person has configured."""

    enabled: bool = False
    per_day: int = 3
    start: dt.time = dt.time(8, 0)
    end: dt.time = dt.time(18, 0)
    mode: str = MODE_FIXED
    min_gap_minutes: int = 15
    double_enabled: bool = False
    double_chance: int = 25  # percent
    # Rolled only after a double launch went out.
    triple_enabled: bool = False
    triple_chance: int = 25  # percent
    # Seconds from a treat to its double launch, and from a double to its triple.
    bonus_delay_seconds: int = 30

    @property
    def min_gap(self) -> dt.timedelta:
        return dt.timedelta(minutes=max(0, self.min_gap_minutes))

    def window_length(self) -> dt.timedelta:
        today = dt.date(2000, 1, 3)  # any day without a clock change
        return dt.datetime.combine(today, self.end) - dt.datetime.combine(today, self.start)

    def capacity(self) -> int:
        """How many treats fit in the window with the minimum gap."""
        return fit_count(self.window_length(), self.min_gap, self.per_day)

    def problem(self) -> str | None:
        """What stops this schedule from working as configured, or None."""
        if self.end <= self.start:
            return "The schedule window must end after it starts (overnight windows are not supported)."
        if self.per_day < 1:
            return "Treats per day must be at least 1."
        fits = self.capacity()
        if fits < self.per_day:
            return (
                f"Only {fits} of {self.per_day} treats fit between "
                f"{self.start:%H:%M} and {self.end:%H:%M} with a {self.min_gap_minutes}-minute gap."
            )
        return None

    def window_invalid(self) -> bool:
        return self.end <= self.start or self.per_day < 1


def fit_count(span: dt.timedelta, gap: dt.timedelta, wanted: int) -> int:
    """The most of ``wanted`` treats that fit in ``span`` at least ``gap`` apart."""
    if wanted <= 0 or span < dt.timedelta(0):
        return 0
    if gap <= dt.timedelta(0):
        return wanted
    return min(wanted, math.floor(span / gap) + 1)


def window(day: dt.date, settings: ScheduleSettings, tz: dt.tzinfo) -> tuple[dt.datetime, dt.datetime]:
    """The window's start and end on a given day, in local time."""
    return (
        dt.datetime.combine(day, settings.start, tzinfo=tz),
        dt.datetime.combine(day, settings.end, tzinfo=tz),
    )


def fixed_times(start: dt.datetime, end: dt.datetime, count: int) -> list[dt.datetime]:
    """``count`` evenly spaced times from ``start`` to ``end`` inclusive."""
    if count <= 0:
        return []
    if count == 1:
        return [_round(start + (end - start) / 2)]
    step = (end - start) / (count - 1)
    return [_round(start + step * i) for i in range(count)]


def random_times(
    start: dt.datetime,
    end: dt.datetime,
    count: int,
    rng: random.Random,
    min_gap: dt.timedelta,
) -> list[dt.datetime]:
    """Up to ``count`` random times in [start, end], at least ``min_gap`` apart.

    Uniform over all spacings that respect the gap: draw the points in a span
    shortened by the total gap, sort them, then push each one out by its share
    of the gaps. Plans fewer than ``count`` when they cannot all fit.
    """
    count = fit_count(end - start, min_gap, count)
    if count <= 0:
        return []
    gap = min_gap.total_seconds()
    free = (end - start).total_seconds() - gap * (count - 1)
    offsets = sorted(rng.uniform(0, free) for _ in range(count))
    return [_round(start + dt.timedelta(seconds=o + gap * i)) for i, o in enumerate(offsets)]


def plan_remaining(
    settings: ScheduleSettings,
    day: dt.date,
    now: dt.datetime,
    given: int,
    rng: random.Random,
    last_given: dt.datetime | None = None,
) -> list[dt.datetime]:
    """The treat times still to come today.

    ``given`` is how many scheduled treats have already gone out today, and
    ``last_given`` when the latest one did; only the rest are planned, and none
    closer to the last one than the minimum gap.
    """
    if not settings.enabled or settings.window_invalid():
        return []
    remaining = settings.per_day - given
    if remaining <= 0:
        return []
    start, end = window(day, settings, now.tzinfo or dt.UTC)
    earliest = now
    if last_given is not None and last_given.date() == day:
        earliest = max(earliest, last_given + settings.min_gap - dt.timedelta(seconds=1))
    if earliest >= end:
        return []
    if settings.mode == MODE_RANDOM:
        lead = max(start, now + RANDOM_LEAD, earliest + dt.timedelta(seconds=1))
        return random_times(lead, end, remaining, rng, settings.min_gap)
    slots = fixed_times(start, end, fit_count(end - start, settings.min_gap, settings.per_day))
    return [t for t in slots if t > earliest][:remaining]


def _round(value: dt.datetime) -> dt.datetime:
    """Whole seconds: nobody needs a treat at 13:00:00.417."""
    return value.replace(microsecond=0)
