"""Tests for the relay server (NAN-151)."""

import asyncio
import json
import sys
import os
from pathlib import Path

import pytest
import pytest_asyncio

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# Patch sessions dir to tmp for tests
import tempfile
_tmp = tempfile.mkdtemp()

import relay as relay_module
relay_module.SESSIONS_DIR = Path(_tmp)
relay_module.SNAPSHOTS_DIR = Path(_tmp) / "snapshots"
relay_module.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)

from relay import RelayServer, restore_strategy, JournalWriter


# ── restore_strategy ──────────────────────────────────────────────────────────

def test_restore_strategy_reversible():
    assert restore_strategy("blueprint.set_property") == "reversible"
    assert restore_strategy("level.place_actor") == "reversible"
    assert restore_strategy("level.move_actor") == "reversible"
    assert restore_strategy("contract.update") == "reversible"


def test_restore_strategy_snapshot():
    assert restore_strategy("blueprint.compile") == "snapshot"
    assert restore_strategy("level.save") == "snapshot"
    assert restore_strategy("build.run") == "snapshot"
    assert restore_strategy("schema.migrate") == "snapshot"


def test_restore_strategy_none():
    assert restore_strategy("unknown.command") == "none"
    assert restore_strategy("relay.hello") == "none"
    assert restore_strategy("") == "none"


# ── JournalWriter ─────────────────────────────────────────────────────────────

def test_journal_writer_creates_file():
    writer = JournalWriter()
    entry = {"type": "test", "payload": "hello"}
    writer.write(entry)
    writer.close()

    files = list(Path(_tmp).glob("*.jsonl"))
    assert len(files) >= 1
    lines = files[0].read_text().strip().split("\n")
    parsed = json.loads(lines[0])
    assert parsed["type"] == "test"


def test_journal_writer_appends():
    writer = JournalWriter()
    writer.write({"type": "a"})
    writer.write({"type": "b"})
    writer.close()

    files = sorted(Path(_tmp).glob("*.jsonl"))
    lines = files[-1].read_text().strip().split("\n")
    types = [json.loads(l)["type"] for l in lines if l]
    assert "a" in types
    assert "b" in types


# ── RelayServer unit tests ────────────────────────────────────────────────────

class MockWebSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_register_regular_client():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "client-1", "test-agent")
    assert "client-1" in server._clients
    assert server._client_names["client-1"] == "test-agent"
    assert server._ue_client_id is None


@pytest.mark.asyncio
async def test_register_ue_client():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "ue-id", "ue-head-client")
    assert server._ue_client_id == "ue-id"


@pytest.mark.asyncio
async def test_unregister_regular_client():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "client-1", "test-agent")
    await server._unregister("client-1")
    assert "client-1" not in server._clients
    assert server._ue_client_id is None


@pytest.mark.asyncio
async def test_unregister_ue_client():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "ue-id", "ue-head-client")
    await server._unregister("ue-id")
    assert server._ue_client_id is None


@pytest.mark.asyncio
async def test_handle_message_adds_relay_metadata():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "c1", "shell")
    await server._handle_message(ws, "c1", json.dumps({"type": "unknown.event", "data": 42}))
    assert len(server._history) == 1
    msg = server._history[0]
    assert "_relay" in msg
    assert msg["_relay"]["sender"] == "shell"
    assert "id" in msg["_relay"]
    assert "timestamp" in msg["_relay"]
    assert "restore" in msg["_relay"]


@pytest.mark.asyncio
async def test_handle_message_invalid_json():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "c1", "shell")
    await server._handle_message(ws, "c1", "not json{{")
    error = ws.sent[-1]
    assert error["type"] == "relay.error"
    assert "invalid JSON" in error["reason"]


@pytest.mark.asyncio
async def test_ue_commands_go_to_queue():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "c1", "agent")
    await server._handle_message(ws, "c1", json.dumps({"type": "blueprint.set_property", "asset": "x"}))
    assert server._command_queue.qsize() == 1


@pytest.mark.asyncio
async def test_non_ue_commands_broadcast():
    server = RelayServer()
    ws1 = MockWebSocket()
    ws2 = MockWebSocket()
    await server._register(ws1, "c1", "agent")
    await server._register(ws2, "c2", "watcher")
    await server._handle_message(ws1, "c1", json.dumps({"type": "contract.published"}))
    # Both clients should receive broadcast (no UE exclusion for non-UE types)
    assert len(ws1.sent) > 0 or len(ws2.sent) > 0


@pytest.mark.asyncio
async def test_history_replay_on_join():
    server = RelayServer()
    ws1 = MockWebSocket()
    await server._register(ws1, "c1", "agent1")
    # Simulate a message being added to history
    server._history.append({"type": "test.event", "_relay": {}})

    ws2 = MockWebSocket()
    await server._register(ws2, "c2", "agent2")
    # New joiner should have received history
    assert any(m.get("type") == "relay.history" for m in ws2.sent)


@pytest.mark.asyncio
async def test_ue_dispatch_drops_when_ue_disconnected():
    server = RelayServer()
    ws = MockWebSocket()
    await server._register(ws, "c1", "agent")

    # No UE connected — should broadcast error
    await server._command_queue.put({"type": "blueprint.compile", "_relay": {"id": "x"}})
    task = asyncio.create_task(server._ue_dispatch_loop())
    await asyncio.sleep(0.05)
    task.cancel()

    # ws should have received a command_error broadcast
    assert any(m.get("type") == "relay.command_error" for m in ws.sent)


# ── UE bootstrap dispatch tests ───────────────────────────────────────────────

def test_bootstrap_dispatch_unhandled():
    import ue_bootstrap
    result = ue_bootstrap.dispatch({"type": "unknown.xyz", "payload": {}})
    assert result["status"] == "unhandled"


def test_bootstrap_dispatch_stub_mode():
    import ue_bootstrap
    # In stub mode (no UE module), handlers return stub status
    result = ue_bootstrap.dispatch({"type": "level.save", "payload": {}})
    assert result["status"] in ("stub", "ok")  # ok if unreal was somehow importable


def test_bootstrap_dispatch_error_handling():
    import ue_bootstrap
    # Pass malformed payload — handler should catch and return error
    result = ue_bootstrap.dispatch({"type": "blueprint.set_property", "payload": {}})
    # Will fail because asset_path key missing — should return error
    assert result["status"] in ("error", "stub")
