"""Tests for the new pipeline components (NAN-151 to NAN-155)."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Make relay_client importable
sys.path.insert(0, str(Path(__file__).parent.parent / "relay"))
sys.path.insert(0, str(Path(__file__).parent.parent / "cl"))

# ── relay server smoke test ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_relay_server_welcome():
    """Server should accept a connection, authenticate, and send welcome."""
    import websockets
    from server import connection  # noqa: F401 — just verify importable

    # Start server on a random port
    import server as relay_server
    host, port = "127.0.0.1", 18765
    orig_token = os.environ.get("RELAY_TOKEN")
    os.environ["RELAY_TOKEN"] = "test-secret"

    server_task = None
    try:
        serve_coro = websockets.serve(relay_server.connection, host, port)
        srv = await serve_coro.__aenter__()

        async with websockets.connect(f"ws://{host}:{port}") as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "token": "test-secret",
                "room": "test",
                "role": "shell",
                "name": "pytest",
            }))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert msg["type"] == "welcome"
            assert msg["room"] == "test"
            assert "id" in msg

        await srv.__aexit__(None, None, None)
    finally:
        if orig_token is None:
            os.environ.pop("RELAY_TOKEN", None)
        else:
            os.environ["RELAY_TOKEN"] = orig_token
        # Clean up relay state between tests
        relay_server.rooms.clear()
        relay_server.clients.clear()


@pytest.mark.asyncio
async def test_relay_bad_token_rejected():
    """Server should reject connections with a wrong token."""
    import websockets
    import server as relay_server

    host, port = "127.0.0.1", 18766
    os.environ["RELAY_TOKEN"] = "correct-token"

    srv = await websockets.serve(relay_server.connection, host, port).__aenter__()
    try:
        async with websockets.connect(f"ws://{host}:{port}") as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "token": "wrong-token",
                "room": "test",
                "role": "shell",
                "name": "bad-client",
            }))
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert msg["type"] == "error"
            assert msg["code"] == "auth"
    finally:
        await srv.__aexit__(None, None, None)
        relay_server.rooms.clear()
        relay_server.clients.clear()


@pytest.mark.asyncio
async def test_relay_broadcast():
    """Messages with to='*' should be broadcast to all room members."""
    import websockets
    import server as relay_server

    host, port = "127.0.0.1", 18767
    os.environ["RELAY_TOKEN"] = "bcast-token"

    srv = await websockets.serve(relay_server.connection, host, port).__aenter__()
    try:
        async def connect(name):
            ws = await websockets.connect(f"ws://{host}:{port}")
            await ws.send(json.dumps({
                "type": "hello", "token": "bcast-token",
                "room": "bcast-room", "role": "agent", "name": name,
            }))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
            assert welcome["type"] in ("welcome", "peer_joined")
            return ws

        ws1 = await connect("Alice")
        ws2 = await connect("Bob")
        # consume peer_joined on ws1
        try:
            await asyncio.wait_for(ws1.recv(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

        # Alice broadcasts
        await ws1.send(json.dumps({"type": "cmd", "to": "*", "body": {"hello": "world"}}))

        msg = json.loads(await asyncio.wait_for(ws2.recv(), timeout=3))
        assert msg["type"] == "cmd"
        assert msg["from"]["name"] == "Alice"

        await ws1.close()
        await ws2.close()
    finally:
        await srv.__aexit__(None, None, None)
        relay_server.rooms.clear()
        relay_server.clients.clear()


# ── cl.py changelist journal ───────────────────────────────────────────────────

def test_cl_new_and_list(tmp_path):
    """cl.py new creates an entry; cl.py list shows it."""
    import subprocess
    cl = str(Path(__file__).parent.parent / "cl" / "cl.py")
    journal = str(tmp_path / "changelist.jsonl")
    env = {**os.environ, "CL_JOURNAL": journal}

    r = subprocess.run(
        [sys.executable, cl, "new", "--type", "blueprint_compile", "--desc", "test fix"],
        capture_output=True, text=True, env=env
    )
    assert r.returncode == 0
    assert "CL-" in r.stdout

    r2 = subprocess.run(
        [sys.executable, cl, "list"],
        capture_output=True, text=True, env=env
    )
    assert r2.returncode == 0
    assert "blueprint_compile" in r2.stdout
    assert "test fix" in r2.stdout


def test_cl_show_json(tmp_path):
    """cl.py show --json outputs valid JSON."""
    import subprocess
    cl = str(Path(__file__).parent.parent / "cl" / "cl.py")
    journal = str(tmp_path / "changelist.jsonl")
    env = {**os.environ, "CL_JOURNAL": journal}

    r = subprocess.run(
        [sys.executable, cl, "new", "--type", "level_load", "--args", '{"level":"/Game/Test"}'],
        capture_output=True, text=True, env=env
    )
    cl_id = r.stdout.split()[1]  # "created  CL-XXXXXX  ..."

    r2 = subprocess.run(
        [sys.executable, cl, "show", cl_id, "--json"],
        capture_output=True, text=True, env=env
    )
    assert r2.returncode == 0
    data = json.loads(r2.stdout)
    assert data["id"] == cl_id
    assert data["type"] == "level_load"
    assert data["args"]["level"] == "/Game/Test"


def test_cl_mark_and_note(tmp_path):
    """cl.py mark sets status; cl.py note appends to notes list."""
    import subprocess
    cl = str(Path(__file__).parent.parent / "cl" / "cl.py")
    journal = str(tmp_path / "changelist.jsonl")
    env = {**os.environ, "CL_JOURNAL": journal}

    r = subprocess.run(
        [sys.executable, cl, "new", "--type", "exec"],
        capture_output=True, text=True, env=env
    )
    cl_id = r.stdout.split()[1]

    subprocess.run([sys.executable, cl, "note", cl_id, "reviewed OK"], env=env)
    subprocess.run([sys.executable, cl, "mark", cl_id, "--status", "done"], env=env)

    r2 = subprocess.run(
        [sys.executable, cl, "show", cl_id, "--json"],
        capture_output=True, text=True, env=env
    )
    data = json.loads(r2.stdout)
    assert data["status"] == "done"
    assert any("reviewed OK" in n["text"] for n in data["notes"])


# ── schema.py contract registry ───────────────────────────────────────────────

def test_schema_scaffold_add_validate(tmp_path):
    """scaffold → add → validate round-trip."""
    import subprocess
    schema_cli = str(Path(__file__).parent.parent / "shells" / "schema.py")
    contracts_dir = str(tmp_path / "contracts")
    env = {**os.environ, "CONTRACTS_DIR": contracts_dir}

    # scaffold
    scaffold_file = str(tmp_path / "test_cmd.schema.json")
    subprocess.run(
        [sys.executable, schema_cli, "scaffold", "test_cmd"],
        capture_output=True, text=True, env=env, cwd=str(tmp_path)
    )
    assert Path(tmp_path / "test_cmd.schema.json").exists()

    # add
    r = subprocess.run(
        [sys.executable, schema_cli, "add", "test_cmd", scaffold_file],
        capture_output=True, text=True, env=env
    )
    assert r.returncode == 0

    # validate good data
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"id": "abc", "type": "test_cmd"}))
    r2 = subprocess.run(
        [sys.executable, schema_cli, "validate", "test_cmd", str(data_file)],
        capture_output=True, text=True, env=env
    )
    assert r2.returncode == 0
    assert "valid" in r2.stdout

    # validate bad data (missing required fields)
    bad_file = tmp_path / "bad.json"
    bad_file.write_text(json.dumps({"foo": "bar"}))
    r3 = subprocess.run(
        [sys.executable, schema_cli, "validate", "test_cmd", str(bad_file)],
        capture_output=True, text=True, env=env
    )
    assert r3.returncode != 0


# ── validator Gate 1 (schema) and Gate 3 (cpp build) ─────────────────────────

def test_validator_gate1_no_schema(tmp_path):
    """Gate 1 passes with 'skipped' when no schema registered for command."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "relay"))
    sys.path.insert(0, str(Path(__file__).parent.parent / "validator"))
    import importlib
    import validator as val_module

    orig = val_module.CONTRACTS_DIR
    val_module.CONTRACTS_DIR = str(tmp_path)
    try:
        result = val_module.gate1_schema({"cmd": "unknown_cmd", "id": "CL-TEST"})
        assert result.passed is True
        assert result.details.get("skipped") is True
    finally:
        val_module.CONTRACTS_DIR = orig


def test_validator_gate3_no_ubt_log(tmp_path):
    """Gate 3 passes with 'skipped' when no UBT log exists."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "relay"))
    sys.path.insert(0, str(Path(__file__).parent.parent / "validator"))
    import validator as val_module

    orig = val_module.UBT_LOG
    val_module.UBT_LOG = str(tmp_path / "nonexistent.log")
    try:
        result = val_module.gate3_cpp_build({})
        assert result.passed is True
        assert result.details.get("skipped") is True
    finally:
        val_module.UBT_LOG = orig


def test_validator_gate3_clean_log(tmp_path):
    """Gate 3 passes when UBT log has no error lines."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "relay"))
    sys.path.insert(0, str(Path(__file__).parent.parent / "validator"))
    import validator as val_module

    log_file = tmp_path / "ubt.log"
    log_file.write_text("Build successful\nWarning: deprecated API\nAll targets built\n")

    orig = val_module.UBT_LOG
    val_module.UBT_LOG = str(log_file)
    try:
        result = val_module.gate3_cpp_build({})
        assert result.passed is True
    finally:
        val_module.UBT_LOG = orig


def test_validator_gate3_error_log(tmp_path):
    """Gate 3 fails when UBT log contains error lines."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "relay"))
    sys.path.insert(0, str(Path(__file__).parent.parent / "validator"))
    import validator as val_module

    log_file = tmp_path / "ubt.log"
    log_file.write_text("error C2065: undeclared identifier\nBuild failed\n")

    orig = val_module.UBT_LOG
    val_module.UBT_LOG = str(log_file)
    try:
        result = val_module.gate3_cpp_build({})
        assert result.passed is False
        assert len(result.details.get("errors", [])) > 0
    finally:
        val_module.UBT_LOG = orig
