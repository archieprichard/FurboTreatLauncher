"""Setup: point at the bridge add-on, then enter Furbo's emailed code if asked."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import BridgeAuthError, BridgeClient, BridgeConnectionError, BridgeError
from .const import CONF_API_TOKEN, CONF_CODE, DEFAULT_HOST, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_NEW_CODE = "send_new_code"
# Bridge error kind -> (form field, error key) on the code step.
CODE_ERRORS = {
    "bad_code": (CONF_CODE, "invalid_code"),
    "bad_request": (CONF_CODE, "invalid_code"),
    "conflict": ("base", "code_expired"),
}
PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


class FurboTreatsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Furbo Treat Launcher."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def _client(self, data: Mapping[str, Any]) -> BridgeClient:
        return BridgeClient(
            async_get_clientsession(self.hass), data[CONF_HOST], data[CONF_PORT], data[CONF_API_TOKEN]
        )

    async def _check(self, data: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Fetch the bridge status, or return form errors."""
        try:
            return await self._client(data).status(), {}
        except BridgeAuthError:
            return None, {CONF_API_TOKEN: "invalid_auth"}
        except BridgeConnectionError:
            return None, {"base": "cannot_connect"}
        except BridgeError:
            _LOGGER.exception("Unexpected bridge response")
            return None, {"base": "unknown"}

    @staticmethod
    def _login_errors(status: dict[str, Any]) -> dict[str, str]:
        """Form errors for a bridge that is not ready or waiting for a code."""
        login = status.get("login")
        if login in ("ready", "code_required"):
            return {}
        if login == "signing_in":
            return {"base": "signing_in"}
        return {"base": "furbo_login_failed"}

    async def _proceed(self, status: dict[str, Any]) -> ConfigFlowResult:
        """Continue once the bridge is ready, or asking for Furbo's code."""
        if status.get("login") == "code_required":
            return await self.async_step_code()
        return self._finish()

    def _finish(self) -> ConfigFlowResult:
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=self._data)
        return self.async_create_entry(title="Furbo", data=self._data)

    # --- first setup -----------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None:
            data = {**user_input, CONF_HOST: user_input[CONF_HOST].strip()}
            await self.async_set_unique_id(f"{data[CONF_HOST]}:{data[CONF_PORT]}")
            self._abort_if_unique_id_configured()
            status, errors = await self._check(data)
            if status is not None:
                self._data = data
                errors = self._login_errors(status)
                if not errors:
                    return await self._proceed(status)
                placeholders["error"] = status.get("login_error") or ""

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_API_TOKEN): PASSWORD,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    # --- the emailed code ----------------------------------------------------------

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client = self._client(self._data)
            try:
                if user_input.get(CONF_NEW_CODE):
                    await client.resend_code()
                    errors["base"] = "code_resent"
                elif not user_input.get(CONF_CODE):
                    errors[CONF_CODE] = "invalid_code"
                else:
                    await client.submit_code(str(user_input[CONF_CODE]).strip())
                    return self._finish()
            except BridgeConnectionError:
                errors["base"] = "cannot_connect"
            except BridgeError as err:
                field, error = CODE_ERRORS.get(err.kind, ("base", "unknown"))
                errors[field] = error
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema(
                {vol.Optional(CONF_CODE): str, vol.Optional(CONF_NEW_CODE, default=False): BooleanSelector()}
            ),
            errors=errors,
        )

    # --- reauthentication ----------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The bridge refused our token, or Furbo wants a new emailed code."""
        self._data = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"error": ""}
        if user_input is not None and user_input.get(CONF_API_TOKEN):
            self._data[CONF_API_TOKEN] = user_input[CONF_API_TOKEN]
        status, errors = await self._check(self._data)
        if status is not None:
            errors = self._login_errors(status)
            if not errors:
                return await self._proceed(status)
            placeholders["error"] = status.get("login_error") or ""
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Optional(CONF_API_TOKEN): PASSWORD}),
            errors=errors,
            description_placeholders=placeholders,
        )
