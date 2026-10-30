"""Saves treat photos into Home Assistant's media folder and keeps the latest.

Photos land in ``<media>/furbo_treats/<camera>/<YYYY-MM-DD>/HH-MM-SS_+1s.jpg``,
where they show up under Media in the sidebar. Days older than the retention
setting are deleted once a day.

No Home Assistant imports: the file work is plain Python, run in an executor
by the caller, so it can be tested on its own.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
import logging
from pathlib import Path
import re
import shutil
from typing import Any

_LOGGER = logging.getLogger(__name__)

FOLDER = "furbo_treats"
OFFSETS = (1.0, 2.0)


def slug(text: str) -> str:
    """A folder-safe version of a camera name."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "camera"


def offset_label(offset: float) -> str:
    return f"+{offset:g}s"


@dataclass
class SavedPhoto:
    offset: float
    path: str  # on disk
    url: str  # e.g. /media/local/furbo_treats/... for notifications

    def as_dict(self) -> dict[str, Any]:
        return {"offset": self.offset, "path": self.path, "url": self.url}


class PhotoStore:
    """Writes photos under one media folder and remembers each camera's latest."""

    def __init__(self, media_root: Path, media_name: str = "local") -> None:
        self.root = media_root / FOLDER
        self.media_name = media_name
        # camera id -> offset -> (jpeg bytes, when the treat was tossed)
        self.latest: dict[str, dict[float, tuple[bytes, dt.datetime]]] = {}
        self._listeners: list[Callable[[str], None]] = []

    def add_listener(self, update: Callable[[str], None]) -> Callable[[], None]:
        self._listeners.append(update)
        return lambda: self._listeners.remove(update)

    def notify(self, camera_id: str) -> None:
        """Tell the image entities a camera has new photos. Call on the event loop."""
        for update in list(self._listeners):
            update(camera_id)

    def save(
        self, camera_id: str, camera_name: str, tossed_at: dt.datetime, photos: list[dict[str, Any]]
    ) -> list[SavedPhoto]:
        """Write one toss's photos to disk and remember them. Blocking."""
        day_dir = self.root / slug(camera_name) / tossed_at.date().isoformat()
        saved: list[SavedPhoto] = []
        for photo in photos:
            try:
                offset = float(photo["offset"])
                jpeg = base64.b64decode(photo["jpeg"], validate=True)
            except (KeyError, TypeError, ValueError, binascii.Error):
                _LOGGER.warning("Skipping an unreadable treat photo from the bridge")
                continue
            name = f"{tossed_at:%H-%M-%S}_{offset_label(offset)}.jpg"
            try:
                day_dir.mkdir(parents=True, exist_ok=True)
                (day_dir / name).write_bytes(jpeg)
            except OSError as err:
                _LOGGER.warning("Could not save a treat photo to %s: %s", day_dir, err)
                continue
            self.latest.setdefault(camera_id, {})[offset] = (jpeg, tossed_at)
            relative = (day_dir / name).relative_to(self.root.parent).as_posix()
            saved.append(SavedPhoto(offset, str(day_dir / name), f"/media/{self.media_name}/{relative}"))
        return saved

    def load_latest(self, camera_id: str, camera_name: str, tz: dt.tzinfo) -> None:
        """Pick up the newest saved photo for each offset, e.g. after a restart. Blocking."""
        camera_dir = self.root / slug(camera_name)
        if not camera_dir.is_dir():
            return
        days = sorted((d for d in camera_dir.iterdir() if d.is_dir()), reverse=True)
        for offset in OFFSETS:
            if offset in self.latest.get(camera_id, {}):
                continue
            suffix = f"_{offset_label(offset)}.jpg"
            for day in days:
                files = sorted(f for f in day.iterdir() if f.name.endswith(suffix))
                if not files:
                    continue
                newest = files[-1]
                try:
                    when = dt.datetime.strptime(f"{day.name} {newest.name[:8]}", "%Y-%m-%d %H-%M-%S").replace(
                        tzinfo=tz
                    )
                    self.latest.setdefault(camera_id, {})[offset] = (newest.read_bytes(), when)
                except (ValueError, OSError):
                    pass
                break

    def cleanup(self, camera_name: str, keep_days: int, today: dt.date) -> int:
        """Delete this camera's day folders older than ``keep_days``. Blocking.

        Only folders named like a date are touched. Returns how many went.
        """
        camera_dir = self.root / slug(camera_name)
        if keep_days < 1 or not camera_dir.is_dir():
            return 0
        oldest_kept = today - dt.timedelta(days=keep_days - 1)
        removed = 0
        for folder in camera_dir.iterdir():
            try:
                day = dt.date.fromisoformat(folder.name)
            except ValueError:
                continue
            if folder.is_dir() and day < oldest_kept:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
