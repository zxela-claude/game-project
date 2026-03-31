#!/usr/bin/env python3
"""
Shared relay client — import this in shells, agents, and the UE bootstrap.
"""

import asyncio
import json
import os
import websockets

RELAY_URL   = os.environ.get("RELAY_URL",   "ws://localhost:8765")
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "dev-token-change-me")


class RelayClient:
    def __init__(self, role: str, name: str, room: str = "main"):
        self.role  = role
        self.name  = name
        self.room  = room
        self.ws    = None
        self.id    = None
        self._handlers: dict[str, list] = {}

    def on(self, msg_type: str):
        """Decorator: @client.on("cmd") async def handler(msg): ..."""
        def decorator(fn):
            self._handlers.setdefault(msg_type, []).append(fn)
            return fn
        return decorator

    async def connect(self):
        self.ws = await websockets.connect(RELAY_URL)
        await self.ws.send(json.dumps({
            "type":  "hello",
            "token": RELAY_TOKEN,
            "room":  self.room,
            "role":  self.role,
            "name":  self.name,
        }))
        welcome = json.loads(await self.ws.recv())
        if welcome.get("type") != "welcome":
            raise RuntimeError(f"handshake failed: {welcome}")
        self.id = welcome["id"]
        return welcome

    async def send(self, body: dict, to: str = "*"):
        await self.ws.send(json.dumps({"to": to, "body": body, **body}))

    async def listen(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            t = msg.get("type", "")
            for handler in self._handlers.get(t, []) + self._handlers.get("*", []):
                await handler(msg)

    async def run(self):
        await self.connect()
        await self.listen()

    async def close(self):
        if self.ws:
            await self.ws.close()
