"""
UE Python Bootstrap — one-time paste
NAN-152

Paste this into UE Editor → Tools → Execute Python Script (or run via -ExecCmds).

Connects UE to the relay server at ws://HOST:8765.
Registers as 'ue-head-client'.
Listens for type:command messages and executes via unreal.* Python API.
Auto-reconnects if relay goes down.
"""

import asyncio
import json
import os
import threading
import time

# ── Config ────────────────────────────────────────────────────────────────────
RELAY_HOST = os.environ.get("RELAY_HOST", "localhost")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8765"))
RELAY_URL = f"ws://{RELAY_HOST}:{RELAY_PORT}"
RECONNECT_DELAY = 3  # seconds between reconnect attempts

# ── UE imports (available inside UE Python environment) ───────────────────────
try:
    import unreal
    # Verify it's a real UE Python environment, not a stub package
    HAS_UNREAL = hasattr(unreal, "EditorLevelLibrary")
    if not HAS_UNREAL:
        print("[ue_bootstrap] WARNING: 'unreal' module found but no UE APIs — running in stub mode")
except ImportError:
    HAS_UNREAL = False
    print("[ue_bootstrap] WARNING: 'unreal' module not found — running in stub mode")


# ── Command handlers ──────────────────────────────────────────────────────────

def handle_blueprint_set_property(payload: dict) -> dict:
    if not HAS_UNREAL:
        return {"status": "stub", "payload": payload}
    asset_path = payload["asset_path"]
    prop_name = payload["property"]
    value = payload["value"]
    with unreal.ScopedEditorTransaction("set_property") as t:
        asset = unreal.load_object(None, asset_path)
        if asset is None:
            return {"status": "error", "reason": f"asset not found: {asset_path}"}
        unreal.SystemLibrary.set_object_property(asset, prop_name, value)
    return {"status": "ok"}


def handle_level_place_actor(payload: dict) -> dict:
    if not HAS_UNREAL:
        return {"status": "stub", "payload": payload}
    actor_class_path = payload["actor_class"]
    location = payload.get("location", {"x": 0, "y": 0, "z": 0})
    actor_class = unreal.load_class(None, actor_class_path)
    if actor_class is None:
        return {"status": "error", "reason": f"class not found: {actor_class_path}"}
    loc = unreal.Vector(location["x"], location["y"], location["z"])
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(actor_class, loc)
    return {"status": "ok", "actor_name": actor.get_name() if actor else None}


def handle_level_delete_actor(payload: dict) -> dict:
    if not HAS_UNREAL:
        return {"status": "stub", "payload": payload}
    actor_name = payload["actor_name"]
    actors = unreal.EditorLevelLibrary.get_all_level_actors()
    for actor in actors:
        if actor.get_name() == actor_name:
            unreal.EditorLevelLibrary.destroy_actor(actor)
            return {"status": "ok"}
    return {"status": "error", "reason": f"actor not found: {actor_name}"}


def handle_level_save(_payload: dict) -> dict:
    if not HAS_UNREAL:
        return {"status": "stub"}
    unreal.EditorLevelLibrary.save_current_level()
    return {"status": "ok"}


def handle_build_run(payload: dict) -> dict:
    """Trigger a build — runs asynchronously in UE."""
    if not HAS_UNREAL:
        return {"status": "stub"}
    target = payload.get("target", "development")
    # Kick off build; actual result comes back via webhook/event
    unreal.AutomationLibrary.run_automation_tests(target)
    return {"status": "started", "target": target}


HANDLERS = {
    "blueprint.set_property": handle_blueprint_set_property,
    "level.place_actor": handle_level_place_actor,
    "level.delete_actor": handle_level_delete_actor,
    "level.save": handle_level_save,
    "build.run": handle_build_run,
}


def dispatch(msg: dict) -> dict:
    msg_type = msg.get("type", "")
    handler = HANDLERS.get(msg_type)
    if handler is None:
        return {"status": "unhandled", "type": msg_type}
    try:
        return handler(msg.get("payload", {}))
    except Exception as e:
        return {"status": "error", "reason": str(e)}


# ── WebSocket client ──────────────────────────────────────────────────────────

async def _run_client():
    while True:
        try:
            import websockets
            async with websockets.connect(RELAY_URL) as ws:
                # Handshake
                await ws.send(json.dumps({"type": "relay.hello", "name": "ue-head-client"}))
                welcome = json.loads(await ws.recv())
                print(f"[ue_bootstrap] Connected to relay — client_id={welcome.get('client_id')}")

                async for raw in ws:
                    msg = json.loads(raw)
                    msg_type = msg.get("type", "")

                    # Skip non-command relay traffic
                    if msg_type in ("relay.history", "relay.welcome", "relay.error"):
                        continue

                    print(f"[ue_bootstrap] Received: {msg_type}")
                    result = dispatch(msg)

                    relay_id = msg.get("_relay", {}).get("id")
                    await ws.send(json.dumps({
                        "type": "ue.command_result",
                        "original_id": relay_id,
                        "original_type": msg_type,
                        "result": result,
                    }))

        except Exception as e:
            print(f"[ue_bootstrap] Disconnected ({e}), retrying in {RECONNECT_DELAY}s…")
            await asyncio.sleep(RECONNECT_DELAY)


def start_bootstrap():
    """Entry point — start relay client in a background thread so UE stays responsive."""
    def _thread():
        asyncio.run(_run_client())

    t = threading.Thread(target=_thread, daemon=True, name="relay-client")
    t.start()
    print(f"[ue_bootstrap] Relay client started → {RELAY_URL}")
    return t


# ── Run when pasted/executed in UE ───────────────────────────────────────────
if __name__ == "__main__":
    # Standalone test mode
    asyncio.run(_run_client())
else:
    # UE paste mode — start in background
    start_bootstrap()
