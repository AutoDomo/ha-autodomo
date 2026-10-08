"""Integracao AutoDomo: o Home Assistant como ponte de uma casa do app."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bridge import AutodomoBridge
from .const import (
    CONF_API_KEY,
    CONF_AUTH_EMULATOR_HOST,
    CONF_BRIDGE_ID,
    CONF_DATABASE_URL,
    CONF_ENTITIES,
    CONF_HOME_ID,
    CONF_REFRESH_TOKEN,
)
from .firebase import FirebaseAuthError, FirebaseClient, FirebaseError

_LOGGER = logging.getLogger(__name__)

type AutodomoConfigEntry = ConfigEntry[AutodomoBridge]


async def async_setup_entry(hass: HomeAssistant, entry: AutodomoConfigEntry) -> bool:
    client = FirebaseClient(
        async_get_clientsession(hass),
        api_key=entry.data[CONF_API_KEY],
        database_url=entry.data[CONF_DATABASE_URL],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        auth_emulator_host=entry.data.get(CONF_AUTH_EMULATOR_HOST) or None,
    )
    try:
        await client.refresh()
    except FirebaseAuthError as err:
        raise ConfigEntryAuthFailed(f"ponte revogada ou token expirado: {err}") from err
    except FirebaseError as err:
        raise ConfigEntryNotReady(f"sem acesso ao AutoDomo: {err}") from err

    bridge = AutodomoBridge(
        hass,
        client,
        home_id=entry.data[CONF_HOME_ID],
        bridge_id=entry.data[CONF_BRIDGE_ID],
        entity_ids=set(entry.options.get(CONF_ENTITIES, [])),
    )
    try:
        await bridge.async_start()
    except FirebaseAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except FirebaseError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = bridge
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AutodomoConfigEntry) -> bool:
    await entry.runtime_data.async_stop()
    return True


async def _async_options_updated(hass: HomeAssistant, entry: AutodomoConfigEntry) -> None:
    """Entidades mudaram: recarrega a ponte (republica devices e apaga os que sairam)."""
    await hass.config_entries.async_reload(entry.entry_id)
