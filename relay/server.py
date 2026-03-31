#!/usr/bin/env python3
"""
NAN-151 — Game Dev Relay Server
WebSocket hub :8765 — replaces UE single-port 6776 with multi-user command bus.

Rooms:   each UE project gets its own room (default: "main")
Roles:   ue_host | agent | shell | discord
Routing: commands tagged with {to: "ue_host"} are forwarded to UE only
         commands tagged with {to: "*"} are broadcast to the room
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import websockets
from websockets import ServerConnection as WebSocketServerProtocol

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("relay")

# ── State ──────────────────────────────────────────────────────────────────────

rooms: dict[str, dict[str, WebSocketServerProtocol]] = {}   # room → {client_id → ws}
clients: dict[str, dict] = {}                                # client_id → {room, role, name, ws}
message_log: list[dict] = []                                 # in-memory journal (also written to disk)

JOURNAL_PATH = os.environ.get("RELAY_JOURNAL", "../journal/relay_log.jsonl")
AUTH_TOKEN   = os.environ.get("RELAY_TOKEN", "dev-token-change-me")

# ── Helpers ────────────────────────────────────────────────────────────────────

def ts() -> str:
    return datetime.utcnow().isoformat() + "Z"

def log_message(entry: dict):
    entry["_ts"] = ts()
    message_log.append(entry)
    try:
        os.makedirs(os.path.dirname(os.path.abspath(JOURNAL_PATH)), exist_ok=True)
        with open(JOURNAL_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        log.warning(f"journal write failed: {e}")

async def send_json(ws: WebSocketServerProtocol, payload: dict):
    try:
        await ws.send(json.dumps(payload))
    except Exception:
        pass

async def broadcast(room: str, payload: dict, exclude: Optional[str] = None):
    if room not in rooms:
        return
    dead = []
    for cid, ws in list(rooms[room].items()):
        if cid == exclude:
            continue
        try:
            await ws.send(json.dumps(payload))
        except Exception:
            dead.append(cid)
    for cid in dead:
        await remove_client(cid)

async def route_to(target_role: str, room: str, payload: dict, exclude: Optional[str] = None):
    """Send only to clients in room whose role matches target_role."""
    for cid, info in list(clients.items()):
        if info["room"] == room and info["role"] == target_role and cid != exclude:
            await send_json(info["ws"], payload)

async def remove_client(cid: str):
    info = clients.pop(cid, None)
    if not info:
        return
    room = info["room"]
    if room in rooms:
        rooms[room].pop(cid, None)
        if not rooms[room]:
            del rooms[room]
    log.info(f"client left: {info['name']} ({info['role']}) room={room}")
    await broadcast(room, {
        "type": "peer_left",
        "id": cid,
        "name": info["name"],
        "role": info["role"],
    }, exclude=cid)

# ── Handshake ──────────────────────────────────────────────────────────────────

async def handshake(ws: WebSocketServerProtocol) -> Optional[dict]:
    """
    Client must send within 5s:
    {
      "type": "hello",
      "token": "<RELAY_TOKEN>",
      "room":  "main",          // optional, default "main"
      "role":  "agent",         // ue_host | agent | shell | discord
      "name":  "LevelDesign"    // display name
    }
    """
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        msg = json.loads(raw)
    except asyncio.TimeoutError:
        await send_json(ws, {"type": "error", "code": "timeout", "msg": "no hello within 5s"})
        return None
    except Exception as e:
        await send_json(ws, {"type": "error", "code": "parse", "msg": str(e)})
        return None

    if msg.get("type") != "hello":
        await send_json(ws, {"type": "error", "code": "proto", "msg": "expected hello"})
        return None

    if msg.get("token") != AUTH_TOKEN:
        await send_json(ws, {"type": "error", "code": "auth", "msg": "bad token"})
        return None

    role = msg.get("role", "shell")
    if role not in ("ue_host", "agent", "shell", "discord"):
        await send_json(ws, {"type": "error", "code": "role", "msg": f"unknown role: {role}"})
        return None

    return {
        "id":   str(uuid.uuid4())[:8],
        "room": msg.get("room", "main"),
        "role": role,
        "name": msg.get("name", role),
        "ws":   ws,
    }

# ── Message handling ───────────────────────────────────────────────────────────

async def handle_message(cid: str, raw: str):
    info = clients[cid]
    try:
        msg = json.loads(raw)
    except Exception:
        await send_json(info["ws"], {"type": "error", "code": "parse", "msg": "invalid json"})
        return

    msg_type = msg.get("type", "cmd")

    # ── ping ──
    if msg_type == "ping":
        await send_json(info["ws"], {"type": "pong", "ts": ts()})
        return

    # ── peers: list who's in the room ──
    if msg_type == "peers":
        peers = [
            {"id": c, "name": v["name"], "role": v["role"]}
            for c, v in clients.items()
            if v["room"] == info["room"]
        ]
        await send_json(info["ws"], {"type": "peers", "peers": peers})
        return

    # ── cmd / any other message: route it ──
    envelope = {
        "type":  msg_type,
        "from":  {"id": cid, "name": info["name"], "role": info["role"]},
        "room":  info["room"],
        "ts":    ts(),
        "body":  msg.get("body", msg),
    }
    log_message(envelope)

    to = msg.get("to", "*")

    if to == "*":
        await broadcast(info["room"], envelope, exclude=cid)
    elif to in ("ue_host", "agent", "shell", "discord"):
        await route_to(to, info["room"], envelope, exclude=None)
    else:
        # direct by client id
        target = clients.get(to)
        if target:
            await send_json(target["ws"], envelope)
        else:
            await send_json(info["ws"], {"type": "error", "code": "no_target", "msg": f"'{to}' not found"})

# ── Connection loop ────────────────────────────────────────────────────────────

async def connection(ws: WebSocketServerProtocol):
    info = await handshake(ws)
    if not info:
        await ws.close()
        return

    cid  = info["id"]
    room = info["room"]

    clients[cid] = info
    rooms.setdefault(room, {})[cid] = ws

    log.info(f"client joined: {info['name']} ({info['role']}) room={room} id={cid}")

    # ack
    peers = [
        {"id": c, "name": v["name"], "role": v["role"]}
        for c, v in clients.items()
        if v["room"] == room and c != cid
    ]
    await send_json(ws, {
        "type":  "welcome",
        "id":    cid,
        "room":  room,
        "peers": peers,
        "ts":    ts(),
    })

    # notify others
    await broadcast(room, {
        "type": "peer_joined",
        "id":   cid,
        "name": info["name"],
        "role": info["role"],
    }, exclude=cid)

    try:
        async for raw in ws:
            await handle_message(cid, raw)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await remove_client(cid)

# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    host = os.environ.get("RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("RELAY_PORT", "8765"))

    log.info(f"Relay server starting on ws://{host}:{port}")
    log.info(f"Auth token: {'(from env)' if 'RELAY_TOKEN' in os.environ else '*** USING DEFAULT dev-token — SET RELAY_TOKEN ENV ***'}")

    async with websockets.serve(connection, host, port):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
