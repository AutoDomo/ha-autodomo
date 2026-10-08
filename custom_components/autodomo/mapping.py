"""Traducao entre entidades do Home Assistant e o modelo do AutoDomo.

Contrato: AutoDomo/autodomo-cloud docs/modelo-dados.md. `kind` = dominio do
HA (input_boolean vira `switch`); `caps` descreve o que o dispositivo faz;
`state` leva so' o que o app precisa mostrar.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import ATTR_SUPPORTED_FEATURES, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

# Estados de cover/lock sairam de homeassistant.const (HA 2025+; viraram
# CoverState/LockState nas plataformas) - literais evitam depender da versao.
COVER_STATES = ("open", "closed", "opening", "closing")
STATE_LOCKED = "locked"

# Chaves do RTDB nao aceitam . # $ [ ] /
def device_id_for(entity_id: str) -> str:
    return entity_id.replace(".", "_")


def entity_id_for(device_id: str) -> str | None:
    """Inverso de device_id_for (so' o primeiro '_' e' o ponto do dominio)."""
    if "_" not in device_id:
        return None
    domain, rest = device_id.split("_", 1)
    return f"{domain}.{rest}"


def kind_for(domain: str) -> str | None:
    if domain == "input_boolean":
        return "switch"
    if domain in ("light", "switch", "cover", "sensor", "binary_sensor", "climate", "lock", "fan", "scene", "button"):
        return domain
    return None


def _available(state: State) -> bool:
    return state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def device_payload(state: State, room: str | None) -> dict[str, Any] | None:
    """Documento `devices/{id}` para a entidade (None se o dominio nao e' suportado)."""
    domain = state.domain
    kind = kind_for(domain)
    if kind is None:
        return None
    attrs = state.attributes
    caps: dict[str, Any] = {}
    features = int(attrs.get(ATTR_SUPPORTED_FEATURES, 0) or 0)

    if kind == "light":
        modes = set(attrs.get("supported_color_modes") or [])
        caps["onoff"] = True
        if modes & {"brightness", "hs", "rgb", "rgbw", "rgbww", "xy", "color_temp", "white"}:
            caps["brightness"] = True
        if modes & {"hs", "rgb", "rgbw", "rgbww", "xy"}:
            caps["color_rgb"] = True
        if "color_temp" in modes:
            caps["color_temp"] = True
            if attrs.get("min_color_temp_kelvin"):
                caps["color_temp_min_k"] = attrs["min_color_temp_kelvin"]
            if attrs.get("max_color_temp_kelvin"):
                caps["color_temp_max_k"] = attrs["max_color_temp_kelvin"]
    elif kind == "switch":
        caps["onoff"] = True
    elif kind == "cover":
        caps["open_close"] = bool(features & 3)  # OPEN | CLOSE
        caps["stop"] = bool(features & 8)
        caps["position"] = bool(features & 4)
        caps["tilt"] = bool(features & 128)
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]
    elif kind == "sensor":
        if attrs.get("unit_of_measurement"):
            caps["unit"] = attrs["unit_of_measurement"]
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]
        if attrs.get("state_class"):
            caps["state_class"] = attrs["state_class"]
    elif kind == "binary_sensor":
        if attrs.get("device_class"):
            caps["device_class"] = attrs["device_class"]
    elif kind == "climate":
        caps["modes"] = list(attrs.get("hvac_modes") or [])
        caps["target_temp"] = True
        if attrs.get("min_temp") is not None:
            caps["min_temp"] = attrs["min_temp"]
        if attrs.get("max_temp") is not None:
            caps["max_temp"] = attrs["max_temp"]
        if attrs.get("target_temp_step"):
            caps["temp_step"] = attrs["target_temp_step"]
        if attrs.get("fan_modes"):
            caps["fan_modes"] = list(attrs["fan_modes"])
    elif kind == "lock":
        caps["lock_unlock"] = True
    elif kind == "fan":
        caps["onoff"] = True
        caps["percentage"] = bool(features & 1)
    elif kind == "scene":
        caps["activate"] = True
    elif kind == "button":
        caps["press"] = True

    payload: dict[str, Any] = {
        "kind": kind,
        "name": state.name,
        "caps": caps or {"onoff": True},
        "source": {"entity_id": state.entity_id, "domain": domain},
    }
    if room:
        payload["room"] = room
    return payload


def state_payload(state: State, ts_ms: int) -> dict[str, Any]:
    """Documento `state/{id}`: pequeno, so' o que o app mostra."""
    kind = kind_for(state.domain) or state.domain
    attrs = state.attributes
    out: dict[str, Any] = {"ts": ts_ms, "available": _available(state)}

    if kind in ("light", "switch", "fan", "binary_sensor"):
        out["on"] = state.state == STATE_ON
    if kind == "light":
        if attrs.get("brightness") is not None:
            out["brightness"] = int(attrs["brightness"])
        if attrs.get("rgb_color"):
            out["rgb"] = [int(c) for c in attrs["rgb_color"]]
        if attrs.get("color_temp_kelvin"):
            out["color_temp"] = int(attrs["color_temp_kelvin"])
    elif kind == "cover":
        out["state"] = state.state if state.state in COVER_STATES else "unknown"
        if attrs.get("current_position") is not None:
            out["position"] = int(attrs["current_position"])
        if attrs.get("current_tilt_position") is not None:
            out["tilt"] = int(attrs["current_tilt_position"])
    elif kind == "sensor":
        value: Any = state.state
        try:
            value = float(state.state)
            if value.is_integer():
                value = int(value)
        except (TypeError, ValueError):
            pass
        out["value"] = value
        if attrs.get("unit_of_measurement"):
            out["unit"] = attrs["unit_of_measurement"]
    elif kind == "climate":
        out["mode"] = state.state
        if attrs.get("temperature") is not None:
            out["target_temp"] = attrs["temperature"]
        if attrs.get("current_temperature") is not None:
            out["current_temp"] = attrs["current_temperature"]
        if attrs.get("fan_mode"):
            out["fan"] = attrs["fan_mode"]
        if attrs.get("hvac_action"):
            out["action"] = attrs["hvac_action"]
    elif kind == "lock":
        out["locked"] = state.state == STATE_LOCKED
        out["state"] = state.state
    elif kind == "fan":
        if attrs.get("percentage") is not None:
            out["percentage"] = int(attrs["percentage"])
    return out


def service_for_command(domain: str, action: str, params: Any) -> tuple[str, str, dict[str, Any]] | None:
    """(dominio, servico, dados extras) para um comando do app, ou None se invalido."""
    p = params if isinstance(params, dict) else {}
    kind = kind_for(domain)
    if kind is None:
        return None

    if action in ("turn_on", "turn_off", "toggle") and kind in ("light", "switch", "fan"):
        return domain, action, {}
    if kind == "light":
        if action == "set_brightness" and "brightness" in p:
            return domain, "turn_on", {"brightness": max(0, min(255, int(p["brightness"])))}
        if action == "set_color" and isinstance(p.get("rgb"), list) and len(p["rgb"]) == 3:
            return domain, "turn_on", {"rgb_color": [max(0, min(255, int(c))) for c in p["rgb"]]}
        if action == "set_color_temp" and "kelvin" in p:
            return domain, "turn_on", {"color_temp_kelvin": int(p["kelvin"])}
    elif kind == "cover":
        if action == "open":
            return domain, "open_cover", {}
        if action == "close":
            return domain, "close_cover", {}
        if action == "stop":
            return domain, "stop_cover", {}
        if action == "set_position" and "position" in p:
            return domain, "set_cover_position", {"position": max(0, min(100, int(p["position"])))}
        if action == "set_tilt" and "tilt" in p:
            return domain, "set_cover_tilt_position", {"tilt_position": max(0, min(100, int(p["tilt"])))}
    elif kind == "climate":
        if action == "set_temperature" and "temperature" in p:
            return domain, "set_temperature", {"temperature": float(p["temperature"])}
        if action == "set_mode" and isinstance(p.get("mode"), str):
            return domain, "set_hvac_mode", {"hvac_mode": p["mode"]}
        if action == "set_fan_mode" and isinstance(p.get("fan"), str):
            return domain, "set_fan_mode", {"fan_mode": p["fan"]}
        if action in ("turn_on", "turn_off"):
            return domain, action, {}
    elif kind == "lock":
        if action in ("lock", "unlock"):
            return domain, action, {}
    elif kind == "fan":
        if action == "set_percentage" and "percentage" in p:
            return domain, "set_percentage", {"percentage": max(0, min(100, int(p["percentage"])))}
    elif kind == "scene" and action == "activate":
        return domain, "turn_on", {}
    elif kind == "button" and action == "press":
        return domain, "press", {}
    return None
