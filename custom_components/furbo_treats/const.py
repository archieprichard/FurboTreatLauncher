"""Constants for Furbo Treat Launcher."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "furbo_treats"
MANUFACTURER: Final = "Tomofun"

CONF_API_TOKEN: Final = "api_token"
CONF_CODE: Final = "code"

DEFAULT_HOST: Final = "127.0.0.1"  # the add-on shares Home Assistant's host network
DEFAULT_PORT: Final = 8792

STATUS_INTERVAL: Final = timedelta(seconds=60)

SERVICE_TOSS_TREAT: Final = "toss_treat"
ATTR_COUNT: Final = "count"

# Fired after every scheduled treat, so an automation can notify on failures.
EVENT_SCHEDULED_TREAT: Final = f"{DOMAIN}_scheduled_treat"

MODELS: Final = {
    "FB0030": "Furbo 360° Dog Camera",
    "FB0035": "Furbo 360° Dog Camera",
    "FBC0030": "Furbo 360° Cat Camera",
    "FBC0035": "Furbo 360° Cat Camera",
    "MC0020": "Furbo Mini 360°",
    "MC0030": "Furbo Mini 360°",
}
