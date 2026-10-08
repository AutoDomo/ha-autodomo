"""Cliente minimo do Firebase (Auth REST + Realtime Database REST/SSE).

Sem SDK: o Home Assistant ja' traz aiohttp, e a ponte so' precisa de
PUT/PATCH/DELETE/GET e de um stream de eventos. Funciona contra a nuvem e
contra o emulador (database_url com ?ns=..., auth_emulator_host).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

TOKEN_REFRESH_MARGIN_S = 300


class FirebaseError(Exception):
    """Erro de comunicacao/autenticacao com o Firebase."""


class FirebaseAuthError(FirebaseError):
    """Token recusado: a ponte foi revogada ou o refresh token expirou."""


class CallableError(FirebaseError):
    """Cloud Function devolveu erro (status = codigo gRPC em texto)."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message


async def call_function(
    session: aiohttp.ClientSession, functions_url: str, name: str, data: dict[str, Any], id_token: str | None = None
) -> dict[str, Any]:
    """Chama uma Cloud Function onCall (protocolo: {"data": ...} -> {"result": ...})."""
    headers = {"Content-Type": "application/json"}
    if id_token:
        headers["Authorization"] = f"Bearer {id_token}"
    try:
        async with session.post(
            f"{functions_url.rstrip('/')}/{name}", json={"data": data}, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise FirebaseError(f"falha ao chamar {name}: {err}") from err
    if "error" in body:
        err = body["error"] or {}
        raise CallableError(str(err.get("status", "UNKNOWN")), str(err.get("message", "")))
    return body.get("result") or {}


class FirebaseClient:
    """Sessao autenticada da ponte no Realtime Database."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        database_url: str,
        refresh_token: str | None = None,
        auth_emulator_host: str | None = None,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._auth_emulator_host = auth_emulator_host
        self.refresh_token = refresh_token
        self.uid: str | None = None
        self._id_token: str | None = None
        self._id_token_exp = 0.0
        self._refresh_lock = asyncio.Lock()

        parts = urlsplit(database_url)
        self._db_base = f"{parts.scheme}://{parts.netloc}"
        self._db_params = dict(parse_qsl(parts.query))

    # ----- Auth -----

    def _auth_url(self, service: str, path: str) -> str:
        if self._auth_emulator_host:
            return f"http://{self._auth_emulator_host}/{service}/v1/{path}?key={self._api_key}"
        return f"https://{service}/v1/{path}?key={self._api_key}"

    async def sign_in_with_custom_token(self, custom_token: str) -> None:
        """Troca o custom token (do claimBridge) por ID token + refresh token."""
        url = self._auth_url("identitytoolkit.googleapis.com", "accounts:signInWithCustomToken")
        try:
            async with self._session.post(
                url, json={"token": custom_token, "returnSecureToken": True}, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    raise FirebaseAuthError(f"signInWithCustomToken: {body.get('error', {}).get('message', resp.status)}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FirebaseError(f"signInWithCustomToken: {err}") from err
        self._store_tokens(body["idToken"], body["refreshToken"], int(body.get("expiresIn", 3600)))

    async def refresh(self) -> None:
        """Renova o ID token com o refresh token."""
        if not self.refresh_token:
            raise FirebaseAuthError("sem refresh token")
        url = self._auth_url("securetoken.googleapis.com", "token")
        try:
            async with self._session.post(
                url,
                data={"grant_type": "refresh_token", "refresh_token": self.refresh_token},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status != 200:
                    msg = body.get("error", {}).get("message", str(resp.status))
                    if msg in ("TOKEN_EXPIRED", "USER_DISABLED", "USER_NOT_FOUND", "INVALID_REFRESH_TOKEN"):
                        raise FirebaseAuthError(f"refresh recusado: {msg}")
                    raise FirebaseError(f"refresh: {msg}")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FirebaseError(f"refresh: {err}") from err
        self._store_tokens(body["id_token"], body["refresh_token"], int(body.get("expires_in", 3600)))

    def _store_tokens(self, id_token: str, refresh_token: str, expires_in: int) -> None:
        self._id_token = id_token
        self.refresh_token = refresh_token
        self._id_token_exp = time.time() + expires_in
        try:
            payload = id_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            import base64  # noqa: PLC0415

            self.uid = json.loads(base64.urlsafe_b64decode(payload)).get("user_id")
        except Exception:  # noqa: BLE001 - so' informativo
            self.uid = None

    async def token(self) -> str:
        """ID token valido (renova se estiver pra vencer)."""
        if self._id_token and time.time() < self._id_token_exp - TOKEN_REFRESH_MARGIN_S:
            return self._id_token
        async with self._refresh_lock:
            if self._id_token and time.time() < self._id_token_exp - TOKEN_REFRESH_MARGIN_S:
                return self._id_token
            await self.refresh()
            assert self._id_token is not None
            return self._id_token

    # ----- RTDB REST -----

    async def _db_url(self, path: str) -> tuple[str, dict[str, str]]:
        params = dict(self._db_params)
        params["auth"] = await self.token()
        return f"{self._db_base}/{path.strip('/')}.json", params

    async def _request(self, method: str, path: str, data: Any = None) -> Any:
        url, params = await self._db_url(path)
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                data=None if data is None else json.dumps(data),
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 401:
                    raise FirebaseAuthError("RTDB: nao autorizado (ponte revogada?)")
                if resp.status >= 400:
                    text = await resp.text()
                    raise FirebaseError(f"RTDB {method} {path}: {resp.status} {text[:200]}")
                if method == "DELETE":
                    return None
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FirebaseError(f"RTDB {method} {path}: {err}") from err

    async def get(self, path: str) -> Any:
        return await self._request("GET", path)

    async def put(self, path: str, data: Any) -> None:
        await self._request("PUT", path, data)

    async def patch(self, path: str, data: dict[str, Any]) -> None:
        await self._request("PATCH", path, data)

    async def delete(self, path: str) -> None:
        await self._request("DELETE", path)

    # ----- RTDB streaming (SSE) -----

    async def stream(
        self,
        path: str,
        on_event: Callable[[str, str, Any], Awaitable[None]],
    ) -> None:
        """Escuta `path` por SSE ate' ser cancelado ou o servidor encerrar.

        on_event(event, subpath, data) para `put`/`patch`. Levanta
        FirebaseAuthError em `auth_revoked`/401 e FirebaseError em `cancel`
        ou queda de conexao - quem chama decide o backoff.
        """
        url, params = await self._db_url(path)
        headers = {"Accept": "text/event-stream"}
        try:
            async with self._session.get(
                url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=None, sock_read=90)
            ) as resp:
                if resp.status == 401:
                    raise FirebaseAuthError("stream: nao autorizado")
                if resp.status >= 400:
                    raise FirebaseError(f"stream {path}: {resp.status}")
                event: str | None = None
                data_lines: list[str] = []
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if event is not None:
                            await self._dispatch(event, "\n".join(data_lines), on_event)
                        event, data_lines = None, []
                        continue
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                    # comentarios (":") e outros campos sao ignorados
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise FirebaseError(f"stream {path}: {err}") from err
        raise FirebaseError(f"stream {path}: conexao encerrada pelo servidor")

    async def _dispatch(
        self, event: str, data: str, on_event: Callable[[str, str, Any], Awaitable[None]]
    ) -> None:
        if event == "keep-alive":
            return
        if event == "auth_revoked":
            raise FirebaseAuthError("stream: auth_revoked")
        if event == "cancel":
            raise FirebaseError(f"stream: cancelado pelo servidor ({data[:100]})")
        if event in ("put", "patch"):
            try:
                payload = json.loads(data) if data else {}
            except json.JSONDecodeError:
                _LOGGER.debug("evento %s com payload invalido: %s", event, data[:100])
                return
            await on_event(event, payload.get("path", "/"), payload.get("data"))
