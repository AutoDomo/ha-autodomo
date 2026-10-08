"""Fluxo de configuracao: codigo de pareamento -> escolha do que expor.

Tambem reautenticacao (ponte revogada no app: pede so' um codigo novo) e a
tela de opcoes.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTO_DOMAINS,
    CONF_ADVANCED,
    CONF_API_KEY,
    CONF_AUTH_EMULATOR_HOST,
    CONF_AUTO_ADD,
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

_CALLABLE_ERRORS = {
    "NOT_FOUND": "invalid_code",
    "DEADLINE_EXCEEDED": "expired_code",
    "INVALID_ARGUMENT": "invalid_code",
}


def _normalize_code(raw: str) -> str | None:
    code = re.sub(r"[^A-Za-z2-9]", "", raw or "").upper()
    return code if CODE_RE.match(code) else None


def _advanced_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_FUNCTIONS_URL, default=DEFAULT_FUNCTIONS_URL): str,
            vol.Optional(CONF_DATABASE_URL, default=""): str,
            vol.Optional(CONF_API_KEY, default=DEFAULT_API_KEY): str,
            vol.Optional(CONF_AUTH_EMULATOR_HOST, default=""): str,
        }
    )


def _code_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_CODE): str,
            vol.Optional(CONF_ADVANCED): section(_advanced_schema(), {"collapsed": True}),
        }
    )


def _default_entities(hass: HomeAssistant) -> list[str]:
    """Pre-selecao: tudo que se controla (AUTO_DOMAINS), nada de sensor."""
    return sorted(
        s.entity_id for s in hass.states.async_all() if s.domain in AUTO_DOMAINS and s.state != "unavailable"
    )


def _entities_schema(current: list[str], auto_add: bool) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_ENTITIES, default=current): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=list(SUPPORTED_DOMAINS), multiple=True)
            ),
            vol.Optional(CONF_AUTO_ADD, default=auto_add): selector.BooleanSelector(),
        }
    )


async def _pair(
    hass: HomeAssistant, code: str, advanced: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Troca o codigo pela identidade da ponte. (dados do entry, erro)."""
    adv = advanced or {}
    functions_url = adv.get(CONF_FUNCTIONS_URL) or DEFAULT_FUNCTIONS_URL
    database_url = adv.get(CONF_DATABASE_URL) or ""
    api_key = adv.get(CONF_API_KEY) or DEFAULT_API_KEY
    auth_emulator = adv.get(CONF_AUTH_EMULATOR_HOST) or None
    session = async_get_clientsession(hass)
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
        return None, _CALLABLE_ERRORS.get(err.status, "cannot_connect")
    except (FirebaseError, KeyError) as err:
        _LOGGER.warning("falha no pareamento: %s", err)
        return None, "cannot_connect"
    return {
        CONF_BRIDGE_ID: result["bridgeId"],
        CONF_HOME_ID: result["homeId"],
        CONF_PROJECT_ID: result.get("projectId", ""),
        CONF_DATABASE_URL: database_url or result.get("databaseURL") or DEFAULT_DATABASE_URL,
        CONF_FUNCTIONS_URL: functions_url,
        CONF_API_KEY: result.get("apiKey") or api_key,
        CONF_REFRESH_TOKEN: client.refresh_token,
        CONF_AUTH_EMULATOR_HOST: auth_emulator,
    }, None


class AutodomoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pareia o Home Assistant como ponte de uma casa do AutoDomo."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # ----- instalacao -----

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = _normalize_code(user_input[CONF_CODE])
            if code is None:
                errors[CONF_CODE] = "invalid_code"
            else:
                data, error = await _pair(self.hass, code, user_input.get(CONF_ADVANCED))
                if error:
                    errors["base"] = error
                else:
                    assert data is not None
                    await self.async_set_unique_id(data[CONF_BRIDGE_ID])
                    self._abort_if_unique_id_configured()
                    self._data = data
                    return await self.async_step_entities()
        return self.async_show_form(step_id="user", data_schema=_code_schema(), errors=errors)

    async def async_step_entities(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=f"AutoDomo ({self._data[CONF_HOME_ID][-6:]})",
                data=self._data,
                options={
                    CONF_ENTITIES: user_input.get(CONF_ENTITIES, []),
                    CONF_AUTO_ADD: user_input.get(CONF_AUTO_ADD, True),
                },
            )
        return self.async_show_form(
            step_id="entities",
            data_schema=_entities_schema(_default_entities(self.hass), True),
        )

    # ----- reautenticacao (ponte revogada / token invalido) -----

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            code = _normalize_code(user_input[CONF_CODE])
            if code is None:
                errors[CONF_CODE] = "invalid_code"
            else:
                advanced = {
                    CONF_FUNCTIONS_URL: entry.data.get(CONF_FUNCTIONS_URL),
                    CONF_DATABASE_URL: entry.data.get(CONF_DATABASE_URL),
                    CONF_API_KEY: entry.data.get(CONF_API_KEY),
                    CONF_AUTH_EMULATOR_HOST: entry.data.get(CONF_AUTH_EMULATOR_HOST),
                }
                data, error = await _pair(self.hass, code, advanced)
                if error:
                    errors["base"] = error
                else:
                    assert data is not None
                    # Ponte nova (id novo) na mesma casa: devices sao republicados
                    # no reload; se o codigo for de OUTRA casa, recusa.
                    if data[CONF_HOME_ID] != entry.data[CONF_HOME_ID]:
                        errors["base"] = "wrong_home"
                    else:
                        return self.async_update_reload_and_abort(
                            entry, data_updates=data, unique_id=data[CONF_BRIDGE_ID]
                        )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CODE): str}),
            errors=errors,
            description_placeholders={"title": entry.title},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> AutodomoOptionsFlow:
        return AutodomoOptionsFlow()


class AutodomoOptionsFlow(OptionsFlow):
    """Escolha das entidades expostas ao app."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="",
                data={
                    CONF_ENTITIES: user_input.get(CONF_ENTITIES, []),
                    CONF_AUTO_ADD: user_input.get(CONF_AUTO_ADD, False),
                },
            )
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=_entities_schema(list(opts.get(CONF_ENTITIES, [])), bool(opts.get(CONF_AUTO_ADD, False))),
        )
