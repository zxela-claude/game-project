#!/usr/bin/env python3
"""
Relay Server — WebSocket hub :8765
NAN-151

The backbone of the UE game dev infrastructure.
- Accepts connections from shells, agents, VSCode, and UE bootstrap
- Serializes commands to UE (prevents multi-user collision)
- Broadcasts results to all connected clients
- Replays command history to new joiners on connect
- Writes journal entry before forwarding every command
- Determines restore strategy per command type
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection as WebSocketServerProtocol

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "sessions"
SNAPSHOTS_DIR = ROOT / "sessions" / "snapshots"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

# ── Config ────────────────────────────────────────────────────────────────────
HOST = os.environ.get("RELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("RELAY_PORT", "8765"))
MAX_JOURNAL_REPLAY = 500  # max entries replayed to new joiners

# ── Restore strategies ────────────────────────────────────────────────────────
# Commands that can be reversed by replaying an inverse operation
REVERSIBLE_TYPES = {
    "blueprint.set_property",
    "blueprint.add_node",
    "blueprint.delete_node",
    "level.place_actor",
    "level.delete_actor",
    "level.move_actor",
    "level.set_property",
    "contract.update",
}

# Commands that require a pre-command snapshot
SNAPSHOT_TYPES = {
    "blueprint.compile",
    "level.save",
    "build.run",
    "schema.migrate",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("relay")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def restore_strategy(msg_type: str) -> str:
    if msg_type in REVERSIBLE_TYPES:
        return "reversible"
    if msg_type in SNAPSHOT_TYPES:
        return "snapshot"
    return "none"


class JournalWriter:
    """Writes journal entries to /sessions/YYYY-MM-DD.jsonl"""

    def __init__(self):
        self._file: Any = None
        self._date: str = ""

    def _open(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._date:
            if self._file:
                self._file.close()
            self._date = today
            path = SESSIONS_DIR / f"{today}.jsonl"
            self._file = open(path, "a", buffering=1)

    def write(self, entry: dict):
        self._open()
        self._file.write(json.dumps(entry) + "\n")

    def close(self):
        if self._file:
            self._file.close()


class RelayServer:
    def __init__(self):
        self._clients: dict[str, WebSocketServerProtocol] = {}  # client_id → ws
        self._client_names: dict[str, str] = {}                 # client_id → name
        self._ue_client_id: str | None = None                    # which client is UE
        self._command_queue: asyncio.Queue = asyncio.Queue()     # UE-bound commands
        self._journal = JournalWriter()
        self._history: list[dict] = []                           # replay buffer

    # ── Connection lifecycle ───────────────────────────────────────────────────

    async def _register(self, ws: WebSocketServerProtocol, client_id: str, name: str):
        self._clients[client_id] = ws
        self._client_names[client_id] = name
        if name == "ue-head-client":
            self._ue_client_id = client_id
            log.info(f"UE head client connected: {client_id}")
        else:
            log.info(f"Client connected: name={name} id={client_id}")

        # Replay recent history to new joiner
        replay = self._history[-MAX_JOURNAL_REPLAY:]
        if replay:
            await ws.send(json.dumps({"type": "relay.history", "entries": replay}))

    async def _unregister(self, client_id: str):
        name = self._client_names.pop(client_id, client_id)
        self._clients.pop(client_id, None)
        if self._ue_client_id == client_id:
            self._ue_client_id = None
            log.warning("UE head client disconnected")
        else:
            log.info(f"Client disconnected: name={name} id={client_id}")

    # ── Message routing ────────────────────────────────────────────────────────

    async def _handle_message(self, ws: WebSocketServerProtocol, client_id: str, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await ws.send(json.dumps({"type": "relay.error", "reason": "invalid JSON"}))
            return

        msg_type = msg.get("type", "")
        sender = self._client_names.get(client_id, client_id)

        # Enrich message
        msg["_relay"] = {
            "id": str(uuid.uuid4()),
            "sender": sender,
            "sender_id": client_id,
            "timestamp": utcnow(),
            "restore": restore_strategy(msg_type),
        }

        # Journal entry written BEFORE forwarding
        self._journal.write(msg)
        self._history.append(msg)

        if msg_type.startswith("ue.") or msg_type.startswith("blueprint.") or \
                msg_type.startswith("level.") or msg_type.startswith("build."):
            # Route to UE via serialized queue
            await self._enqueue_for_ue(msg)
        else:
            # Broadcast to all
            await self._broadcast(msg, exclude=None)

    async def _enqueue_for_ue(self, msg: dict):
        await self._command_queue.put(msg)

    async def _ue_dispatch_loop(self):
        """Serializes commands to UE — one at a time, in order."""
        while True:
            msg = await self._command_queue.get()
            ue_id = self._ue_client_id
            if ue_id and ue_id in self._clients:
                try:
                    await self._clients[ue_id].send(json.dumps(msg))
                    log.info(f"→ UE: type={msg.get('type')} id={msg['_relay']['id']}")
                except Exception as e:
                    log.warning(f"Failed to send to UE: {e}")
                    await self._broadcast_error(msg, f"UE send failed: {e}")
            else:
                log.warning(f"UE not connected; dropping command type={msg.get('type')}")
                await self._broadcast_error(msg, "UE not connected")
            self._command_queue.task_done()

    async def _broadcast(self, msg: dict, exclude: str | None = None):
        payload = json.dumps(msg)
        targets = [
            ws for cid, ws in self._clients.items() if cid != exclude
        ]
        if targets:
            await asyncio.gather(*[ws.send(payload) for ws in targets], return_exceptions=True)

    async def _broadcast_error(self, original: dict, reason: str):
        await self._broadcast({
            "type": "relay.command_error",
            "reason": reason,
            "original_id": original.get("_relay", {}).get("id"),
            "original_type": original.get("type"),
        })

    # ── WebSocket handler ──────────────────────────────────────────────────────

    async def handler(self, ws: WebSocketServerProtocol):
        client_id = str(uuid.uuid4())

        # Expect a hello message as first message
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            hello = json.loads(raw)
            if hello.get("type") != "relay.hello":
                await ws.send(json.dumps({"type": "relay.error", "reason": "expected relay.hello"}))
                return
            name = hello.get("name", "unknown")
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await ws.send(json.dumps({"type": "relay.error", "reason": "hello timeout or invalid JSON"}))
            return

        await self._register(ws, client_id, name)
        await ws.send(json.dumps({
            "type": "relay.welcome",
            "client_id": client_id,
            "server_time": utcnow(),
        }))

        try:
            async for raw in ws:
                await self._handle_message(ws, client_id, raw)
        except websockets.ConnectionClosedOK:
            pass
        except websockets.ConnectionClosedError as e:
            log.warning(f"Connection closed with error: {e}")
        finally:
            await self._unregister(client_id)

    # ── Main entry ─────────────────────────────────────────────────────────────

    async def serve(self):
        dispatch_task = asyncio.create_task(self._ue_dispatch_loop())
        log.info(f"Relay server starting on ws://{HOST}:{PORT}")
        try:
            async with websockets.serve(self.handler, HOST, PORT):
                log.info(f"Relay server ready — waiting for connections")
                await asyncio.Future()  # run forever
        finally:
            dispatch_task.cancel()
            self._journal.close()


def main():
    relay = RelayServer()
    try:
        asyncio.run(relay.serve())
    except KeyboardInterrupt:
        log.info("Relay server stopped")


if __name__ == "__main__":
    main()
