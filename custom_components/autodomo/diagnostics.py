"""Diagnostico (Configuracoes > Integracoes > AutoDomo > Baixar diagnostico)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_API_KEY, CONF_REFRESH_TOKEN

TO_REDACT = {CONF_REFRESH_TOKEN, CONF_API_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    bridge = getattr(entry, "runtime_data", None)
    status: dict[str, Any] = {}
    if bridge is not None:
        status = {
            "home_id": bridge.home_id,
            "bridge_id": bridge.bridge_id,
            "auth_failed": bridge.auth_failed,
            "entities_selected": sorted(bridge.entity_ids),
            "entities_published": sorted(bridge._published),  # noqa: SLF001
            "entities_missing": sorted(e for e in bridge.entity_ids if hass.states.get(e) is None),
            "auto_add": bridge.auto_add,
        }
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "bridge": status,
    }
