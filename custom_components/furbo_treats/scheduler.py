"""Runs each camera's daily treat schedule.

Settings and today's progress are saved, so a restart keeps the same plan (a
random day is not re-rolled) and never repeats a treat already given. Treats
whose time passed while Home Assistant was down are skipped.

A double launch is a bonus second treat a set delay after a scheduled one, given
at random with the configured chance. When a double launch goes out, a triple
launch can follow the same delay after it, with its own chance. Bonus treats do
not use up the day's treats per day, but the bridge still counts them toward
its daily safety limit.

Every toss, from the schedule, a button or an action, goes through
:meth:`ScheduleManager.async_toss`, which also saves the treat photos.
"""

from __future__ import annotations

from collections.abc import Callable
import dataclasses
import datetime as dt
import logging
import random
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_time,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .api import BridgeClient, BridgeError
from .const import DOMAIN, EVENT_SCHEDULED_TREAT
from .photo_store import PhotoStore
from .planner import MODES, ScheduleSettings, plan_remaining

if TYPE_CHECKING:
    from .coordinator import FurboStatusCoordinator

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
RETRY_DELAY = dt.timedelta(minutes=2)
BONUS_NAMES = {2: "double", 3: "triple"}
# Settings that don't affect when the day's treats happen, so changing them
# never re-plans the day.
NON_PLAN_FIELDS = (
    "double_enabled",
    "double_chance",
    "triple_enabled",
    "triple_chance",
    "bonus_delay_seconds",
    "photos_enabled",
    "photo_1s",
    "photo_2s",
    "photo_3s",
    "photo_retention_days",
)
# Failures worth one more try: the bridge reports that the camera or Furbo was
# unreachable, so nothing went out. Not "connection": when the bridge's answer
# is lost the treat may well have been tossed, and a retry would double it.
RETRYABLE = {"camera", "upstream"}

_FIELDS = {f.name: f for f in dataclasses.fields(ScheduleSettings)}


def _settings_to_dict(settings: ScheduleSettings) -> dict[str, Any]:
    data = dataclasses.asdict(settings)
    data["start"] = settings.start.isoformat()
    data["end"] = settings.end.isoformat()
    return data


def _settings_from_dict(data: dict[str, Any] | None) -> ScheduleSettings:
    """Stored settings; anything missing or unreadable falls back to its default."""
    default = ScheduleSettings()
    values: dict[str, Any] = {}
    for name, raw in (data or {}).items():
        if name not in _FIELDS:
            continue
        try:
            if name in ("start", "end"):
                values[name] = dt.time.fromisoformat(raw)
            elif name == "mode":
                if raw in MODES:
                    values[name] = raw
            elif isinstance(getattr(default, name), bool):
                values[name] = bool(raw)
            else:
                values[name] = int(raw)
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring unreadable stored schedule setting %s", name)
    return dataclasses.replace(default, **values)


def _parse_time(value: Any) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


class TreatScheduler:
    """One camera's schedule: settings, today's plan, and the next timer."""

    def __init__(
        self,
        hass: HomeAssistant,
        manager: ScheduleManager,
        camera_id: str,
        camera_name: str,
        stored: dict[str, Any],
    ) -> None:
        self.hass = hass
        self._manager = manager
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.settings = _settings_from_dict(stored.get("settings"))
        self.day: str = stored.get("day", "")
        self.given: int = int(stored.get("given", 0))
        self.doubles: int = int(stored.get("doubles", 0))
        self.triples: int = int(stored.get("triples", 0))
        self.last_given = _parse_time(stored.get("last_given"))
        self._saved_plan = [t for t in map(_parse_time, stored.get("plan", [])) if t]
        self._saved_key: str | None = stored.get("settings_key")
        self.plan: list[dt.datetime] = []
        self.last_result: str | None = stored.get("last_result")
        self._retried: set[str] = set()
        self._rng = random.SystemRandom()
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._bonus_timers: list[CALLBACK_TYPE] = []
        self._in_flight_since: dt.datetime | None = None
        self._listeners: list[Callable[[], None]] = []

    # --- persistence and listeners -------------------------------------------

    def _plan_key(self) -> str:
        """The settings that decide the plan; the bonus launch ones do not."""
        data = _settings_to_dict(self.settings)
        for name in NON_PLAN_FIELDS:
            data.pop(name)
        return repr(sorted(data.items()))

    def as_dict(self) -> dict[str, Any]:
        return {
            "settings": _settings_to_dict(self.settings),
            "settings_key": self._plan_key(),
            "day": self.day,
            "given": self.given,
            "doubles": self.doubles,
            "triples": self.triples,
            "last_given": self.last_given.isoformat() if self.last_given else None,
            "plan": [t.isoformat() for t in self.plan],
            "last_result": self.last_result,
        }

    @callback
    def add_listener(self, update: Callable[[], None]) -> CALLBACK_TYPE:
        self._listeners.append(update)

        @callback
        def remove() -> None:
            self._listeners.remove(update)

        return remove

    @callback
    def _changed(self) -> None:
        self._manager.async_save()
        for update in list(self._listeners):
            update()

    # --- planning ------------------------------------------------------------

    @property
    def next_treat(self) -> dt.datetime | None:
        return self.plan[0] if self.plan else None

    @property
    def problem(self) -> str | None:
        return self.settings.problem() if self.settings.enabled else None

    @callback
    def async_start(self) -> None:
        """Pick up today's saved plan, or make a new one."""
        now = dt_util.now()
        today = now.date().isoformat()
        if self.day != today:
            self._reset_day(today)
            self._replan(now)
        elif self._saved_key == self._plan_key() and self.settings.enabled:
            # Same settings as before the restart: keep the times already chosen.
            self.plan = [t for t in self._saved_plan if t > now]
        else:
            self._replan(now)
        self._arm()
        self._changed()

    @callback
    def update_settings(self, **changes: Any) -> None:
        """Apply a change from one of the schedule entities."""
        before = self._plan_key()
        self.settings = dataclasses.replace(self.settings, **changes)
        if self._plan_key() != before:
            if self.problem:
                _LOGGER.warning("%s treat schedule: %s", self.camera_name, self.problem)
            self._replan(dt_util.now())
            self._arm()
        if "photo_retention_days" in changes:
            self._manager.clean_old_photos(dt_util.now().date())  # now, not at midnight
        self._changed()

    @callback
    def new_day(self, now: dt.datetime) -> None:
        """Start a fresh day just after midnight."""
        self._reset_day(now.date().isoformat())
        self._replan(now)
        self._arm()
        self._changed()

    def _reset_day(self, today: str) -> None:
        self.day, self.given, self.doubles, self.triples = today, 0, 0, 0
        self._retried.clear()

    def _replan(self, now: dt.datetime) -> None:
        # A treat being tossed right now counts as given, so a settings change
        # during that toss cannot plan an extra one or one too close behind it.
        given, last = self.given, self.last_given
        if self._in_flight_since is not None:
            given += 1
            last = max(last, self._in_flight_since) if last else self._in_flight_since
        self.plan = plan_remaining(self.settings, now.date(), now, given, self._rng, last)
        if self.plan:
            _LOGGER.info(
                "%s: %d treat(s) planned today at %s",
                self.camera_name,
                len(self.plan),
                ", ".join(t.strftime("%H:%M") for t in self.plan),
            )

    @callback
    def _arm(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        if self.plan:
            self._unsub_timer = async_track_point_in_time(self.hass, self._fire, self.plan[0])

    @callback
    def async_stop(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
        for unsub in self._bonus_timers:
            unsub()
        self._bonus_timers.clear()

    # --- tossing -------------------------------------------------------------

    async def _fire(self, now: dt.datetime) -> None:
        now = dt_util.as_local(now)  # the timer hands over UTC
        self._unsub_timer = None
        if not self.plan:
            return
        due = self.plan.pop(0)
        result: dict[str, Any] = {
            "camera": self.camera_name,
            "device_id": self.camera_id,
            "scheduled_for": due.isoformat(),
            "double_launch": False,
        }
        self._in_flight_since = now
        try:
            tossed = await self._manager.async_toss(self.camera_id)
        except Exception as caught:
            err = caught if isinstance(caught, BridgeError) else BridgeError(str(caught), "unexpected")
            if not isinstance(caught, BridgeError):
                _LOGGER.exception("%s: unexpected error tossing a treat", self.camera_name)
            self._in_flight_since = None
            result.update(success=False, error=str(err), kind=err.kind)
            self.last_result = f"Failed at {now:%H:%M}: {err}"
            _LOGGER.warning("%s scheduled treat failed: %s", self.camera_name, err)
            window_end = dt.datetime.combine(now.date(), self.settings.end, tzinfo=now.tzinfo)
            retry_at = (now + RETRY_DELAY).replace(microsecond=0)
            # A slot that is itself a retry is never retried again.
            if err.kind in RETRYABLE and due.isoformat() not in self._retried and retry_at <= window_end:
                self._retried.add(retry_at.isoformat())
                self.plan.insert(0, retry_at)
                result["retry_at"] = retry_at.isoformat()
        else:
            self._in_flight_since = None
            self.given += 1
            self.last_given = now
            result["success"] = True
            result["photos"] = tossed["photo_urls"]
            self.last_result = f"Tossed at {now:%H:%M}"
            _LOGGER.info(
                "%s: scheduled treat %d of %d tossed",
                self.camera_name,
                self.given,
                self.settings.per_day,
            )
            self._maybe_bonus(2, tossed["tossed_at"] or now, result)
        self.hass.bus.async_fire(EVENT_SCHEDULED_TREAT, result)
        self._arm()
        self._changed()
        await self._manager.coordinator.async_request_refresh()

    def _maybe_bonus(self, shot: int, tossed_at: dt.datetime, result: dict[str, Any]) -> None:
        """Roll for bonus shot ``shot`` (2 = double, 3 = triple) and arm it on a hit.

        The delay counts from ``tossed_at``, the moment the previous treat left,
        not from when its photos were ready.
        """
        name = BONUS_NAMES[shot]
        enabled = getattr(self.settings, f"{name}_enabled")
        chance = getattr(self.settings, f"{name}_chance")
        if not enabled or self._rng.random() * 100 >= chance:
            return
        delay = max(1, self.settings.bonus_delay_seconds)
        elapsed = (dt_util.now() - tossed_at).total_seconds()
        wait = max(1.0, delay - max(0.0, elapsed))
        result[f"{name}_launch_at"] = (tossed_at + dt.timedelta(seconds=delay)).isoformat()

        async def fire(_when: dt.datetime) -> None:
            if unsub in self._bonus_timers:
                self._bonus_timers.remove(unsub)
            await self._fire_bonus(shot)

        unsub = async_call_later(self.hass, wait, fire)
        self._bonus_timers.append(unsub)

    async def _fire_bonus(self, shot: int) -> None:
        name = BONUS_NAMES[shot]
        now = dt_util.now()
        result: dict[str, Any] = {
            "camera": self.camera_name,
            "device_id": self.camera_id,
            "double_launch": shot == 2,
            "triple_launch": shot == 3,
        }
        try:
            tossed = await self._manager.async_toss(self.camera_id)
        except Exception as err:  # noqa: BLE001 - reported as a failed bonus, never raised
            result.update(success=False, error=str(err), kind=getattr(err, "kind", "unexpected"))
            _LOGGER.warning("%s %s launch failed: %s", self.camera_name, name, err)
        else:
            if shot == 2:
                self.doubles += 1
            else:
                self.triples += 1
            result["success"] = True
            result["photos"] = tossed["photo_urls"]
            self.last_result = f"{name.capitalize()} launch at {now:%H:%M}"
            _LOGGER.info("%s: %s launch!", self.camera_name, name)
            if shot == 2:
                self._maybe_bonus(3, tossed["tossed_at"] or now, result)
        self.hass.bus.async_fire(EVENT_SCHEDULED_TREAT, result)
        self._changed()
        await self._manager.coordinator.async_request_refresh()


class ScheduleManager:
    """All of one config entry's schedules, saved together."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        client: BridgeClient,
        coordinator: FurboStatusCoordinator,
        photos: PhotoStore | None = None,
    ) -> None:
        self.hass = hass
        self.client = client
        self.coordinator = coordinator
        self.photos = photos
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}")
        self.schedulers: dict[str, TreatScheduler] = {}
        self._unsub_midnight: CALLBACK_TYPE | None = None

    async def async_load(self, cameras: list[dict[str, Any]]) -> None:
        stored = await self._store.async_load() or {}
        for camera in cameras:
            camera_id = str(camera["id"])
            self.schedulers[camera_id] = TreatScheduler(
                self.hass, self, camera_id, camera.get("name") or camera_id, stored.get(camera_id, {})
            )
        for scheduler in self.schedulers.values():
            scheduler.async_start()
        self._unsub_midnight = async_track_time_change(self.hass, self._midnight, hour=0, minute=0, second=5)
        self.clean_old_photos(dt_util.now().date())

    @callback
    def _midnight(self, now: dt.datetime) -> None:
        local = dt_util.as_local(now)
        for scheduler in self.schedulers.values():
            scheduler.new_day(local)
        self.clean_old_photos(local.date())

    @callback
    def clean_old_photos(self, today: dt.date) -> None:
        if self.photos is None:
            return
        for s in self.schedulers.values():
            self.hass.async_add_executor_job(
                self.photos.cleanup, s.camera_name, s.settings.photo_retention_days, today
            )

    async def async_toss(self, camera_id: str, count: int = 1) -> dict[str, Any]:
        """Toss treats, with photos when the camera's photo switch is on, and save them.

        Every toss goes through here: scheduled, double, triple, buttons and the
        action. The result gains ``photo_urls`` (for notifications) and
        ``tossed_at`` (when the last treat left, local time; None if unknown).
        Raises BridgeError when the toss itself fails.
        """
        scheduler = self.schedulers.get(camera_id)
        offsets = scheduler.settings.photo_offsets() if (self.photos and scheduler) else []
        if offsets:
            result = await self.client.toss(camera_id, count, photos=True, photo_offsets=offsets)
        else:
            result = await self.client.toss(camera_id, count, photos=False)
        urls: list[str] = []
        tossed_at: dt.datetime | None = None
        for toss in result.get("tosses") or []:
            stamp = toss.get("tossed_at")
            if isinstance(stamp, (int, float)):
                tossed_at = dt_util.as_local(dt_util.utc_from_timestamp(stamp))
            pictures = toss.pop("photos", None)  # base64 JPEGs: too big to pass on
            if pictures and self.photos is not None:
                when = tossed_at or dt_util.now()
                name = scheduler.camera_name if scheduler else camera_id
                saved = await self.hass.async_add_executor_job(
                    self.photos.save, camera_id, name, when, pictures
                )
                toss["photo_files"] = [p.as_dict() for p in saved]
                urls.extend(p.url for p in saved)
                if saved:
                    self.photos.notify(camera_id)
            if toss.get("photo_error"):
                _LOGGER.warning("Treat photo not taken: %s", toss["photo_error"])
        result["photo_urls"] = urls
        result["tossed_at"] = tossed_at
        return result

    @callback
    def async_save(self) -> None:
        self._store.async_delay_save(lambda: {cid: s.as_dict() for cid, s in self.schedulers.items()}, 1.0)

    @callback
    def async_unload(self) -> None:
        if self._unsub_midnight:
            self._unsub_midnight()
            self._unsub_midnight = None
        for scheduler in self.schedulers.values():
            scheduler.async_stop()
