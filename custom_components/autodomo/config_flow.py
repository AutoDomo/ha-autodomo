"""Fluxo de configuracao: pareamento por codigo + escolha de entidades."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_API_KEY,
    CONF_AUTH_EMULATOR_HOST,
    CONF_BRIDGE_ID,
    CONF_CODE,
    CONF_DATABASE_URL,
    CONF_ENTITIES,
    CONF_FUNCTIONS_URL,
    CONF_HOME_ID,
    CONF_PROJECT_ID,
    CONF_REFRESH_TOKEN,
    DEFAULT_API_KEY,
    DEFAULT_DATABASE_URL,
    DEFAULT_FUNCTIONS_URL,
    DOMAIN,
    SUPPORTED_DOMAINS,
)
from .firebase import CallableError, FirebaseClient, FirebaseError, call_function

_LOGGER = logging.getLogger(__name__)

CODE_RE = re.compile(r"^[A-Z2-9]{8}$")


def _normalize_code(raw: str) -> str | None:
    code = re.sub(r"[^A-Za-z2-9]", "", raw).upper()
    return code if CODE_RE.match(code) else None


class AutodomoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pareia o Home Assistant como ponte de uma casa do AutoDomo."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = _normalize_code(user_input[CONF_CODE])
            if code is None:
                errors[CONF_CODE] = "invalid_code"
            else:
                functions_url = user_input.get(CONF_FUNCTIONS_URL) or DEFAULT_FUNCTIONS_URL
                database_url = user_input.get(CONF_DATABASE_URL) or ""
                api_key = user_input.get(CONF_API_KEY) or DEFAULT_API_KEY
                auth_emulator = user_input.get(CONF_AUTH_EMULATOR_HOST) or None
                session = async_get_clientsession(self.hass)
                try:
                    result = await call_function(
                        session,
                        functions_url,
                        "claimBridge",
                        {"code": code, "info": {"type": "homeassistant", "version": HA_VERSION, "platform": "ha"}},
                    )
                    client = FirebaseClient(
                        session,
                        api_key=result.get("apiKey") or api_key,
                        database_url=database_url or result.get("databaseURL") or DEFAULT_DATABASE_URL,
                        auth_emulator_host=auth_emulator,
                    )
                    await client.sign_in_with_custom_token(result["customToken"])
                except CallableError as err:
                    _LOGGER.debug("claimBridge recusou: %s", err)
                    errors["base"] = {
                        "NOT_FOUND": "invalid_code",
                        "DEADLINE_EXCEEDED": "expired_code",
                        "INVALID_ARGUMENT": "invalid_code",
                    }.get(err.status, "cannot_connect")
                except (FirebaseError, KeyError) as err:
                    _LOGGER.warning("falha no pareamento: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    bridge_id = result["bridgeId"]
                    await self.async_set_unique_id(bridge_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"AutoDomo ({result['homeId'][-6:]})",
                        data={
                            CONF_BRIDGE_ID: bridge_id,
                            CONF_HOME_ID: result["homeId"],
                            CONF_PROJECT_ID: result.get("projectId", ""),
                            CONF_DATABASE_URL: database_url or result.get("databaseURL") or DEFAULT_DATABASE_URL,
                            CONF_FUNCTIONS_URL: functions_url,
                            CONF_API_KEY: result.get("apiKey") or api_key,
                            CONF_REFRESH_TOKEN: client.refresh_token,
                            CONF_AUTH_EMULATOR_HOST: auth_emulator,
                        },
                        options={CONF_ENTITIES: []},
                    )

        schema: dict[Any, Any] = {vol.Required(CONF_CODE): str}
        if self.show_advanced_options:
            schema.update(
                {
                    vol.Optional(CONF_FUNCTIONS_URL, default=DEFAULT_FUNCTIONS_URL): str,
                    vol.Optional(CONF_DATABASE_URL, default=""): str,
                    vol.Optional(CONF_API_KEY, default=DEFAULT_API_KEY): str,
                    vol.Optional(CONF_AUTH_EMULATOR_HOST, default=""): str,
                }
            )
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AutodomoOptionsFlow:
        return AutodomoOptionsFlow()


class AutodomoOptionsFlow(OptionsFlow):
    """Escolha das entidades expostas ao app."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data={CONF_ENTITIES: user_input.get(CONF_ENTITIES, [])})

        current = self.config_entry.options.get(CONF_ENTITIES, [])
        schema = vol.Schema(
            {
                vol.Optional(CONF_ENTITIES, default=current): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=list(SUPPORTED_DOMAINS), multiple=True)
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
