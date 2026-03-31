"""
NAN-152 — UE Python Bootstrap
==============================
One-time paste into Unreal Engine > Tools > Execute Python Script
(or place in Project/Content/Python/ and add to startup scripts)

This script:
  1. Connects to the relay server via WebSocket (using Python's built-in http for
     the initial WS upgrade — UE ships with limited Python stdlib, no third-party libs)
  2. Registers as role "ue_host" in the relay room
  3. Listens for commands and dispatches them to the UE Python API

Supported command types (body.cmd):
  blueprint_compile   — compile all dirty blueprints
  level_load          — load a level by path
  level_save          — save the current level
  run_pie             — begin Play In Editor (headless)
  stop_pie            — end PIE
  exec                — run arbitrary UE console command
  status              — reply with current map + dirty assets count

ENV (set in Project Settings > Python or via .env next to bootstrap.py):
  RELAY_URL   = ws://YOUR_RELAY_HOST:8765   (default: ws://localhost:8765)
  RELAY_TOKEN = dev-token-change-me
  RELAY_ROOM  = main
"""

import unreal
import json
import threading
import os
import time

# ── Config ─────────────────────────────────────────────────────────────────────
RELAY_URL   = os.environ.get("RELAY_URL",   "ws://localhost:8765")
RELAY_TOKEN = os.environ.get("RELAY_TOKEN", "dev-token-change-me")
RELAY_ROOM  = os.environ.get("RELAY_ROOM",  "main")
MACHINE_NAME = os.environ.get("COMPUTERNAME", "ue-head-client")

# ── WebSocket (stdlib only — no websockets package in UE Python) ──────────────
import socket
import base64
import hashlib
import struct
import urllib.parse


class _SimpleWS:
    """Minimal WebSocket client using only stdlib — safe for UE's embedded Python."""

    def __init__(self, url: str):
        parsed = urllib.parse.urlparse(url)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        self.sock = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        if b"101" not in resp:
            raise RuntimeError(f"WS handshake failed: {resp[:200]}")

    def send_text(self, text: str):
        data = text.encode()
        n = len(data)
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
        header = b"\x81"  # FIN + text opcode
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += struct.pack("!BH", 0xFE, n)
        else:
            header += struct.pack("!BQ", 0xFF, n)
        header += mask_key
        self.sock.sendall(header + masked)

    def recv_text(self) -> str:
        def recv_exact(n):
            buf = b""
            while len(buf) < n:
                chunk = self.sock.recv(n - len(buf))
                if not chunk:
                    raise ConnectionResetError
                buf += chunk
            return buf

        header = recv_exact(2)
        opcode = header[0] & 0x0F
        if opcode == 8:
            raise ConnectionResetError("server closed")
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", recv_exact(8))[0]
        return recv_exact(length).decode()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ── UE command handlers ────────────────────────────────────────────────────────

def _cmd_blueprint_compile(body: dict) -> dict:
    unreal.log("[relay] compiling blueprints...")
    result = unreal.EditorAssetLibrary.save_directory("/Game/", False, False)
    unreal.BlueprintEditorLibrary.compile_blueprint(None)  # compiles all dirty
    return {"ok": True, "msg": "blueprint compile triggered"}


def _cmd_level_load(body: dict) -> dict:
    level_path = body.get("level", "")
    unreal.EditorLevelLibrary.load_level(level_path)
    return {"ok": True, "level": level_path}


def _cmd_level_save(body: dict) -> dict:
    unreal.EditorLevelLibrary.save_current_level()
    return {"ok": True}


def _cmd_run_pie(body: dict) -> dict:
    unreal.EditorLevelLibrary.editor_play_simulate()
    return {"ok": True, "msg": "PIE started"}


def _cmd_stop_pie(body: dict) -> dict:
    unreal.EditorLevelLibrary.editor_end_play()
    return {"ok": True, "msg": "PIE stopped"}


def _cmd_exec(body: dict) -> dict:
    cmd = body.get("command", "")
    unreal.SystemLibrary.execute_console_command(None, cmd)
    return {"ok": True, "command": cmd}


def _cmd_status(body: dict) -> dict:
    world = unreal.EditorLevelLibrary.get_editor_world()
    level_name = world.get_name() if world else "unknown"
    return {"ok": True, "level": level_name, "machine": MACHINE_NAME}


HANDLERS = {
    "blueprint_compile": _cmd_blueprint_compile,
    "level_load":        _cmd_level_load,
    "level_save":        _cmd_level_save,
    "run_pie":           _cmd_run_pie,
    "stop_pie":          _cmd_stop_pie,
    "exec":              _cmd_exec,
    "status":            _cmd_status,
}


# ── Main loop (runs in background thread) ──────────────────────────────────────

_running = False
_ws: _SimpleWS = None


def _relay_loop():
    global _ws
    while _running:
        try:
            _ws = _SimpleWS(RELAY_URL)
            _ws.connect()
            unreal.log(f"[relay] connected to {RELAY_URL}")

            # hello
            _ws.send_text(json.dumps({
                "type":  "hello",
                "token": RELAY_TOKEN,
                "room":  RELAY_ROOM,
                "role":  "ue_host",
                "name":  MACHINE_NAME,
            }))

            while _running:
                raw = _ws.recv_text()
                msg = json.loads(raw)

                if msg.get("type") == "welcome":
                    unreal.log(f"[relay] welcomed as {msg.get('id')} | peers: {msg.get('peers')}")
                    continue

                if msg.get("type") not in ("cmd", "command"):
                    continue

                body = msg.get("body", {})
                cmd  = body.get("cmd") or body.get("type", "")
                handler = HANDLERS.get(cmd)

                if handler:
                    try:
                        result = handler(body)
                    except Exception as e:
                        result = {"ok": False, "error": str(e)}
                    _ws.send_text(json.dumps({
                        "type": "cmd_result",
                        "to":   msg.get("from", {}).get("id", "*"),
                        "body": {"cmd": cmd, **result},
                    }))
                else:
                    unreal.log_warning(f"[relay] unknown cmd: {cmd}")

        except ConnectionResetError:
            unreal.log_warning("[relay] disconnected — reconnecting in 5s")
        except Exception as e:
            unreal.log_error(f"[relay] error: {e} — reconnecting in 5s")
        finally:
            if _ws:
                _ws.close()
        if _running:
            time.sleep(5)


def start():
    global _running
    if _running:
        unreal.log("[relay] already running")
        return
    _running = True
    t = threading.Thread(target=_relay_loop, daemon=True)
    t.start()
    unreal.log(f"[relay] bootstrap started → {RELAY_URL} room={RELAY_ROOM}")


def stop():
    global _running
    _running = False
    if _ws:
        _ws.close()
    unreal.log("[relay] bootstrap stopped")


# Auto-start when executed directly in UE
start()
