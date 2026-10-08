"""Teste de fumaca do cliente Firebase contra o emulador do autodomo-cloud.

Nao precisa do Home Assistant. Em outro terminal, em autodomo-cloud:
    firebase emulators:start --only auth,database,functions
Depois:
    pip install aiohttp
    python tests/emulator_smoke.py

Fluxo: cria uma casa + codigo de pareamento direto no RTDB (como a Function
faria), chama claimBridge no emulador, troca o custom token, escreve
devices/state/heartbeat, cria um comando como "usuario" e confere que o
stream da ponte recebe e consegue apagar.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components", "autodomo"))
from firebase import FirebaseClient, call_function  # noqa: E402  (import direto do modulo, sem HA)

PROJECT = "autodomo-cloud"
NS = f"{PROJECT}-default-rtdb"
DB_HOST = os.environ.get("FIREBASE_DATABASE_EMULATOR_HOST", "127.0.0.1:9000")
AUTH_HOST = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
FUNCS = os.environ.get("FUNCTIONS_URL", f"http://127.0.0.1:5001/{PROJECT}/us-central1")
DB_URL = f"http://{DB_HOST}/?ns={NS}"
API_KEY = "fake-api-key"


async def admin_put(session: aiohttp.ClientSession, path: str, data) -> None:
    # No emulador, "Bearer owner" da' acesso total (so' pra montar o cenario).
    async with session.put(
        f"http://{DB_HOST}/{path}.json", params={"ns": NS}, data=json.dumps(data), headers={"Authorization": "Bearer owner"}
    ) as r:
        r.raise_for_status()


def ok(msg: str) -> None:
    print(f"  ok  {msg}")


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        home = "h_smoke"
        owner = "u_owner"
        code = "ABCDEFGH"
        await admin_put(session, f"homes/{home}", {
            "meta": {"name": "Casa teste", "createdBy": owner, "createdAt": 1},
            "members": {owner: {"role": "owner", "addedBy": owner, "addedAt": 1}},
        })
        await admin_put(session, f"pairings/{code}", {
            "homeId": home, "name": "HA teste", "createdBy": owner, "expiresAt": int(time.time() * 1000) + 600_000,
        })
        ok("cenario montado (casa + pairing)")

        res = await call_function(session, FUNCS, "claimBridge", {"code": "abcd-efgh", "info": {"type": "homeassistant", "version": "test"}})
        bridge_id = res["bridgeId"]
        assert res["homeId"] == home, res
        ok(f"claimBridge -> bridgeId={bridge_id}")

        try:
            await call_function(session, FUNCS, "claimBridge", {"code": code})
            raise AssertionError("segundo claim deveria falhar")
        except Exception as err:  # noqa: BLE001
            ok(f"segundo claim recusado ({str(err)[:60]})")

        client = FirebaseClient(session, api_key=API_KEY, database_url=DB_URL, auth_emulator_host=AUTH_HOST)
        await client.sign_in_with_custom_token(res["customToken"])
        ok(f"custom token trocado, uid={client.uid}")

        did = "light_sala"
        await client.patch(f"homes/{home}/devices", {did: {"bridge": bridge_id, "kind": "light", "name": "Sala", "caps": {"onoff": True}, "source": {"entity_id": "light.sala"}}})
        await client.put(f"homes/{home}/state/{did}", {"on": False, "ts": int(time.time() * 1000)})
        await client.patch(f"homes/{home}/bridges/{bridge_id}", {"online": True, "lastSeen": int(time.time() * 1000), "info": {"type": "homeassistant", "version": "test"}})
        ok("devices/state/heartbeat escritos pela ponte")

        try:
            await client.put(f"homes/{home}/members/x", {"role": "admin"})
            raise AssertionError("ponte nao pode escrever em members")
        except Exception:  # noqa: BLE001
            ok("ponte barrada em members")

        got: list[tuple[str, str, object]] = []
        received = asyncio.Event()

        async def on_event(event: str, path: str, data: object) -> None:
            got.append((event, path, data))
            if data is not None and path not in ("", "/"):
                received.set()
            if data is not None and path in ("", "/") and isinstance(data, dict) and data:
                received.set()

        stream_task = asyncio.create_task(client.stream(f"commands/{home}/{bridge_id}", on_event))
        await asyncio.sleep(1.0)

        # "usuario" cria comando (admin no emulador, simulando o app)
        cmd = {"deviceId": did, "action": "turn_on", "by": owner, "ts": int(time.time() * 1000)}
        await admin_put(session, f"commands/{home}/{bridge_id}/c1", cmd)
        await asyncio.wait_for(received.wait(), 10)
        ok(f"stream recebeu comando: {got[-1][1]} {got[-1][2]}")

        await client.delete(f"commands/{home}/{bridge_id}/c1")
        left = await client.get(f"commands/{home}/{bridge_id}")
        assert not left, left
        ok("ponte apagou o comando")

        stream_task.cancel()
        try:
            await stream_task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

        await client.refresh()
        ok("refresh token funcionou")
        print("\nTUDO OK")


if __name__ == "__main__":
    asyncio.run(main())
