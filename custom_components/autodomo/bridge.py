"""A ponte: sincroniza entidades escolhidas com o AutoDomo e executa comandos."""

from __future__ import annotations

import asyncio
from functools import partial
import logging
import random
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED, __version__ as HA_VERSION
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.event import EventStateChangedData, async_call_later, async_track_state_change_event

from .const import (
    AUTO_DOMAINS,
    COMMAND_MAX_AGE_S,
    DOMAIN,
    HEARTBEAT_INTERVAL_S,
    ISSUE_MISSING_ENTITY,
    STATE_DEBOUNCE_S,
    STREAM_RETRY_MAX_S,
    STREAM_RETRY_MIN_S,
)
from .firebase import FirebaseAuthError, FirebaseClient, FirebaseError
from .mapping import device_id_for, device_payload, entity_id_for, service_for_command, state_payload

_LOGGER = logging.getLogger(__name__)


def _now_ms() -> int:
    return int(time.time() * 1000)


class AutodomoBridge:
    """Uma instancia por config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: FirebaseClient,
        *,
        home_id: str,
        bridge_id: str,
        entity_ids: set[str],
        auto_add: bool,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.home_id = home_id
        self.bridge_id = bridge_id
        self.selected = set(entity_ids)  # escolha explicita (opcoes)
        self.auto_add = auto_add
        self.entity_ids = set(entity_ids)  # efetivo: selecionadas + auto (se ligado)
        self.auth_failed = False

        self._published: set[str] = set()  # entidades cujo devices/{id} ja' foi escrito
        self._unsub: list[CALLBACK_TYPE] = []
        self._debounce: dict[str, CALLBACK_TYPE] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._stopped = asyncio.Event()

    # ----- ciclo de vida -----

    async def async_start(self) -> None:
        if self.auto_add:
            self.entity_ids |= {s.entity_id for s in self.hass.states.async_all() if s.domain in AUTO_DOMAINS}
        await self._publish_devices()
        await self._publish_all_states()
        self._check_missing_entities()
        # Assina ANTES de qualquer espera: nada escapa entre a foto e o listener.
        if self.auto_add:
            self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_any_state_changed))
        else:
            self._unsub.append(
                async_track_state_change_event(self.hass, list(self.entity_ids), self._on_state_changed)
            )
        self._tasks.append(self.hass.async_create_background_task(self._command_loop(), "autodomo_commands"))
        self._tasks.append(self.hass.async_create_background_task(self._heartbeat_loop(), "autodomo_heartbeat"))
        _LOGGER.info(
            "ponte %s da casa %s ativa com %d entidades (auto: %s)",
            self.bridge_id,
            self.home_id,
            len(self.entity_ids),
            self.auto_add,
        )

    async def async_stop(self) -> None:
        self._stopped.set()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        for cancel in self._debounce.values():
            cancel()
        self._debounce.clear()
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if not self.auth_failed:
            try:
                await self.client.patch(self._bridge_path(), {"online": False, "lastSeen": _now_ms()})
            except FirebaseError as err:
                _LOGGER.debug("nao deu pra marcar offline: %s", err)

    # ----- caminhos -----

    def _bridge_path(self) -> str:
        return f"homes/{self.home_id}/bridges/{self.bridge_id}"

    def _devices_path(self) -> str:
        return f"homes/{self.home_id}/devices"

    def _state_path(self, device_id: str) -> str:
        return f"homes/{self.home_id}/state/{device_id}"

    def _commands_path(self) -> str:
        return f"commands/{self.home_id}/{self.bridge_id}"

    # ----- devices -----

    def _room_for(self, entity_id: str) -> str | None:
        ent_reg = er.async_get(self.hass)
        entry = ent_reg.async_get(entity_id)
        if entry is None:
            return None
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else None

    async def _publish_device(self, entity_id: str, state: Any) -> bool:
        payload = device_payload(state, self._room_for(entity_id))
        if payload is None:
            _LOGGER.warning("entidade %s de dominio nao suportado - ignorada", entity_id)
            return False
        payload["bridge"] = self.bridge_id
        await self.client.put(f"{self._devices_path()}/{device_id_for(entity_id)}", payload)
        self._published.add(entity_id)
        _LOGGER.debug("dispositivo %s publicado", entity_id)
        return True

    async def _publish_devices(self) -> None:
        """Escreve devices/* desta ponte e apaga os que sairam da selecao."""
        updates: dict[str, Any] = {}
        wanted: set[str] = set()
        for entity_id in sorted(self.entity_ids):
            did = device_id_for(entity_id)
            wanted.add(did)  # mesmo sem estado ainda: nao e' pra apagar
            state = self.hass.states.get(entity_id)
            if state is None:
                # Entidades de MQTT/discovery podem aparecer depois do boot -
                # publica quando o primeiro estado chegar.
                _LOGGER.debug("entidade %s ainda nao existe - publica quando aparecer", entity_id)
                continue
            payload = device_payload(state, self._room_for(entity_id))
            if payload is None:
                _LOGGER.warning("entidade %s de dominio nao suportado - ignorada", entity_id)
                continue
            payload["bridge"] = self.bridge_id
            updates[did] = payload
            self._published.add(entity_id)

        # Devices desta ponte que ja' estao no servidor e nao foram mais escolhidos.
        try:
            existing = await self.client.get(
                self._devices_path(), query={"orderBy": '"bridge"', "equalTo": f'"{self.bridge_id}"'}
            )
        except FirebaseError as err:
            _LOGGER.debug("nao deu pra listar devices existentes: %s", err)
            existing = None
        stale = [did for did in (existing or {}) if did not in wanted] if isinstance(existing, dict) else []

        if updates:
            await self.client.patch(self._devices_path(), updates)
        for did in stale:
            # state antes do device: a regra de state exige que o device exista
            await self.client.delete(self._state_path(did))
            await self.client.delete(f"{self._devices_path()}/{did}")
        if stale:
            _LOGGER.info("%d dispositivo(s) removido(s) do app", len(stale))

    async def _publish_all_states(self) -> None:
        """Estado inicial de tudo que ja' existe. Entidade que apareceu entre o
        _publish_devices e aqui ganha o device agora - state/{id} sem
        devices/{id} e' negado pelas regras e derruba o PATCH todo."""
        updates: dict[str, Any] = {}
        ts = _now_ms()
        for entity_id in self.entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if entity_id not in self._published and not await self._publish_device(entity_id, state):
                continue
            updates[device_id_for(entity_id)] = state_payload(state, ts)
        if updates:
            await self.client.patch(f"homes/{self.home_id}/state", updates)

    async def _reconcile_unpublished(self) -> None:
        """Entidade escolhida com estado mas sem device (janela entre a foto
        inicial e o listener, ou que estava indisponivel) - publica agora."""
        for entity_id in self.entity_ids - self._published:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            if await self._publish_device(entity_id, state):
                await self.client.put(self._state_path(device_id_for(entity_id)), state_payload(state, _now_ms()))

    # ----- reparos: entidade escolhida que nao existe mais -----

    @callback
    def _check_missing_entities(self) -> None:
        ent_reg = er.async_get(self.hass)
        for entity_id in self.selected:
            issue_id = f"{ISSUE_MISSING_ENTITY}_{self.entry.entry_id}_{entity_id}"
            if self.hass.states.get(entity_id) is None and ent_reg.async_get(entity_id) is None:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key=ISSUE_MISSING_ENTITY,
                    translation_placeholders={"entity_id": entity_id},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    # ----- state -----

    @callback
    def _on_any_state_changed(self, event: Event[EventStateChangedData]) -> None:
        """Modo auto: tudo dos AUTO_DOMAINS entra sozinho; o resto so' se escolhido."""
        entity_id = event.data["entity_id"]
        if entity_id not in self.entity_ids:
            if event.data.get("old_state") is None and entity_id.split(".", 1)[0] in AUTO_DOMAINS:
                self.entity_ids.add(entity_id)
                _LOGGER.info("entidade nova %s exposta automaticamente", entity_id)
            else:
                return
        self._on_state_changed(event)

    @callback
    def _on_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if event.data.get("new_state") is None:
            return
        # Debounce por entidade: sensores que mudam varias vezes por segundo
        # viram uma escrita a cada 300 ms.
        if cancel := self._debounce.pop(entity_id, None):
            cancel()
        # Corrotina (via partial): o HA agenda no loop. Funcao comum iria pro executor.
        self._debounce[entity_id] = async_call_later(
            self.hass, STATE_DEBOUNCE_S, partial(self._push_state_later, entity_id)
        )

    async def _push_state_later(self, entity_id: str, _now: Any) -> None:
        await self._push_state(entity_id)

    async def _push_state(self, entity_id: str) -> None:
        self._debounce.pop(entity_id, None)
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        try:
            if entity_id not in self._published and not await self._publish_device(entity_id, state):
                return
            await self.client.put(self._state_path(device_id_for(entity_id)), state_payload(state, _now_ms()))
        except FirebaseAuthError as err:
            self._on_auth_failed(err)
        except FirebaseError as err:
            _LOGGER.warning("falha ao publicar estado de %s: %s", entity_id, err)

    # ----- comandos -----

    async def _command_loop(self) -> None:
        delay = STREAM_RETRY_MIN_S
        while not self._stopped.is_set():
            try:
                await self.client.stream(self._commands_path(), self._on_command_event)
                delay = STREAM_RETRY_MIN_S
            except FirebaseAuthError as err:
                self._on_auth_failed(err)
                return
            except FirebaseError as err:
                _LOGGER.debug("stream de comandos caiu (%s), reconectando em %ss", err, delay)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("erro inesperado no stream de comandos")
            await asyncio.sleep(delay + random.uniform(0, 1))
            delay = min(delay * 2, STREAM_RETRY_MAX_S)

    async def _on_command_event(self, event: str, path: str, data: Any) -> None:
        if data is None:
            return  # remocao (nossa propria, depois de executar)
        if path in ("", "/"):
            # Carga inicial: tudo que estava na fila.
            if isinstance(data, dict):
                for cmd_id, cmd in list(data.items()):
                    await self._handle_command(cmd_id, cmd)
            return
        parts = path.strip("/").split("/")
        if len(parts) == 1 and isinstance(data, dict):
            await self._handle_command(parts[0], data)
        # patch em campo de um comando existente: ignorado (comando e' imutavel)

    async def _handle_command(self, cmd_id: str, cmd: Any) -> None:
        try:
            if not isinstance(cmd, dict):
                return
            ts = int(cmd.get("ts") or 0)
            if ts and _now_ms() - ts > COMMAND_MAX_AGE_S * 1000:
                _LOGGER.debug("comando %s velho (%ds) - descartado", cmd_id, (_now_ms() - ts) // 1000)
                return
            entity_id = entity_id_for(str(cmd.get("deviceId", "")))
            if entity_id is None or entity_id not in self.entity_ids:
                _LOGGER.warning("comando %s para entidade nao exposta: %s", cmd_id, cmd.get("deviceId"))
                return
            mapped = service_for_command(entity_id.split(".", 1)[0], str(cmd.get("action", "")), cmd.get("params"))
            if mapped is None:
                _LOGGER.warning("comando %s invalido para %s: %s", cmd_id, entity_id, cmd.get("action"))
                return
            domain, service, data = mapped
            _LOGGER.debug("comando %s: %s.%s %s -> %s", cmd_id, domain, service, data, entity_id)
            await self.hass.services.async_call(domain, service, {"entity_id": entity_id, **data}, blocking=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("falha ao executar comando %s", cmd_id)
        finally:
            try:
                await self.client.delete(f"{self._commands_path()}/{cmd_id}")
            except FirebaseError as err:
                _LOGGER.debug("nao deu pra apagar comando %s: %s", cmd_id, err)

    # ----- presenca -----

    async def _heartbeat_loop(self) -> None:
        info = {"type": "homeassistant", "version": HA_VERSION, "platform": "ha"}
        first = True
        while not self._stopped.is_set():
            try:
                payload: dict[str, Any] = {"online": True, "lastSeen": _now_ms()}
                if first:
                    payload["info"] = info
                await self.client.patch(self._bridge_path(), payload)
                first = False
                await self._reconcile_unpublished()
                self._check_missing_entities()
            except FirebaseAuthError as err:
                self._on_auth_failed(err)
                return
            except FirebaseError as err:
                _LOGGER.debug("heartbeat falhou: %s", err)
            except asyncio.CancelledError:
                raise
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)

    def _on_auth_failed(self, err: Exception | None = None) -> None:
        if self.auth_failed:
            return
        self.auth_failed = True
        _LOGGER.warning(
            "acesso negado pelo AutoDomo (%s) - ponte revogada no app? Pedindo reconexao (um codigo novo).", err
        )
        # Aparece em Configuracoes > Integracoes como "Reautenticar".
        self.entry.async_start_reauth(self.hass)
