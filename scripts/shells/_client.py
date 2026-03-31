"""
Shared WebSocket client base for relay shells.
"""
import asyncio
import json
from typing import AsyncIterator


RELAY_URL = "ws://localhost:8765"


class RelayClient:
    """Connects to the relay, sends hello, and provides message iteration."""

    def __init__(self, name: str, url: str = RELAY_URL):
        self.name = name
        self.url = url
        self._ws = None

    async def connect(self):
        import websockets
        self._ws = await websockets.connect(self.url)
        await self._ws.send(json.dumps({"type": "relay.hello", "name": self.name}))
        # Drain welcome + optional history
        while True:
            raw = await self._ws.recv()
            msg = json.loads(raw)
            if msg.get("type") == "relay.welcome":
                break
            # relay.history or relay.error — ignore history, raise on error
            if msg.get("type") == "relay.error":
                raise RuntimeError(f"Relay error: {msg.get('reason')}")
        return self

    async def send(self, msg: dict):
        await self._ws.send(json.dumps(msg))

    async def messages(self) -> AsyncIterator[dict]:
        async for raw in self._ws:
            yield json.loads(raw)

    async def close(self):
        if self._ws:
            await self._ws.close()

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *_):
        await self.close()
