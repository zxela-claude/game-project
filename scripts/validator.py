#!/usr/bin/env python3
"""
validator.py — 4-Gate Validation Service
NAN-157

Listens on the relay for commands and runs them through 4 validation gates
before allowing them to reach UE. Publishes validator.result messages.

Gates:
  Gate 1 — Schema:    Command payload matches registered contract in /contracts/
  Gate 2 — Blueprint: Blueprint types and references are valid (UE headless check)
  Gate 3 — C++ Build: No C++ build violations introduced (UBT dry-run check)
  Gate 4 — Smoke:     Smoke sanity — level loads, backend ping, Blueprint executes

A command passes if ALL applicable gates pass. If any gate fails, validation
stops and the failure gate + reason are reported.

validator.result message format:
{
  "type": "validator.result",
  "command_id": "<relay id>",
  "command_type": "<command type>",
  "pass": true/false,
  "failed_gate": 1/2/3/4/null,
  "gates": {
    "gate1": {"pass": true/false, "reason": "..."},
    "gate2": {"pass": true/false, "reason": "..."},
    ...
  },
  "ts": "<iso timestamp>"
}

Usage:
  python validator.py [--url ws://localhost:8765] [--dry-run]

  --dry-run: log what would be validated but don't connect to relay
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = ROOT / "contracts"
CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [validator] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("validator")


# ── Gate skip rules ────────────────────────────────────────────────────────────

# Commands that skip Gate 2 (no Blueprint involvement)
SKIP_BLUEPRINT_GATE = {
    "relay.hello", "relay.ping", "relay.history",
    "schema.updated", "validator.result",
    "watch.subscribe", "record.start", "record.stop",
}

# Commands that skip Gate 3 (no C++ build impact)
SKIP_CPP_GATE = {
    *SKIP_BLUEPRINT_GATE,
    "blueprint.set_property",
    "level.move_actor",
    "level.set_property",
    "level.place_actor",
    "level.delete_actor",
}

# Commands that skip Gate 4 (no smoke test needed)
SKIP_SMOKE_GATE = {
    *SKIP_BLUEPRINT_GATE,
    "blueprint.set_property",
    "level.move_actor",
    "level.set_property",
}

# Commands that are relay-internal and should not be validated at all
RELAY_INTERNAL = {
    "relay.hello", "relay.welcome", "relay.error",
    "relay.ping", "relay.pong", "relay.history",
    "validator.result",
}


# ── Gate implementations ───────────────────────────────────────────────────────

def _load_contract(command_type: str) -> Optional[dict]:
    """Load a contract for the given command type if one exists."""
    # Contract names follow the pattern: command type with dots replaced by underscores
    name = command_type.replace(".", "_") + ".json"
    path = CONTRACTS_DIR / name
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    return None


def gate1_schema(msg: dict) -> dict:
    """
    Gate 1: Schema validation.
    - If a contract exists for this command type, validate the payload against it.
    - If no contract exists, pass with a note (contract-optional by design).
    - If contract exists but payload is malformed, fail.
    """
    cmd_type = msg.get("type", "")
    contract = _load_contract(cmd_type)

    if contract is None:
        return {"pass": True, "reason": "no contract registered — skipped"}

    # Validate required fields
    required = contract.get("required", [])
    missing = [f for f in required if f not in msg]
    if missing:
        return {
            "pass": False,
            "reason": f"missing required fields: {', '.join(missing)}",
        }

    # Validate field types
    field_types = contract.get("fields", {})
    for field, expected_type in field_types.items():
        if field in msg:
            val = msg[field]
            type_map = {
                "string": str,
                "number": (int, float),
                "boolean": bool,
                "object": dict,
                "array": list,
            }
            py_type = type_map.get(expected_type)
            if py_type and not isinstance(val, py_type):
                return {
                    "pass": False,
                    "reason": f"field '{field}' expected {expected_type}, got {type(val).__name__}",
                }

    return {"pass": True, "reason": "schema ok"}


def gate2_blueprint(msg: dict) -> dict:
    """
    Gate 2: Blueprint compile check.
    Validates that Blueprint-affecting commands reference valid actor/component types.
    In production this triggers a UE headless compile; here we do structural checks.
    """
    cmd_type = msg.get("type", "")

    if cmd_type in SKIP_BLUEPRINT_GATE:
        return {"pass": True, "reason": "skipped — not a Blueprint command"}

    # For blueprint commands, require a target field
    if cmd_type.startswith("blueprint."):
        target = msg.get("target") or msg.get("actor") or msg.get("blueprint")
        if not target:
            return {
                "pass": False,
                "reason": "Blueprint command missing 'target', 'actor', or 'blueprint' field",
            }
        # Actor class names must be non-empty strings
        if not isinstance(target, str) or not target.strip():
            return {"pass": False, "reason": "Blueprint target must be a non-empty string"}

    # For level commands that reference blueprints
    if cmd_type.startswith("level."):
        actor_class = msg.get("actor_class")
        if actor_class is not None:
            if not isinstance(actor_class, str) or not actor_class.strip():
                return {"pass": False, "reason": "actor_class must be a non-empty string"}

    return {"pass": True, "reason": "blueprint structure ok"}


def gate3_cpp_build(msg: dict) -> dict:
    """
    Gate 3: C++ build check (UnrealBuildTool dry-run).
    Checks that commands introducing new C++ types or modifying build targets
    don't introduce known build violations.
    In production this runs UBT --dry-run; here we check build metadata.
    """
    cmd_type = msg.get("type", "")

    if cmd_type in SKIP_CPP_GATE:
        return {"pass": True, "reason": "skipped — no C++ impact"}

    # For build commands, validate build target
    if cmd_type == "build.run":
        target = msg.get("target")
        config = msg.get("config", "Development")
        valid_configs = {"Debug", "Development", "Shipping", "Test", "DebugGame"}
        if config not in valid_configs:
            return {
                "pass": False,
                "reason": f"unknown build config '{config}', expected one of {valid_configs}",
            }
        if not target:
            return {"pass": False, "reason": "build.run missing 'target' field"}

    # For contract updates that might add new C++ types
    if cmd_type == "contract.update":
        new_type = msg.get("cpp_type")
        if new_type and not isinstance(new_type, str):
            return {"pass": False, "reason": "cpp_type must be a string"}

    # blueprint.compile triggers a full C++ check
    if cmd_type == "blueprint.compile":
        blueprint = msg.get("blueprint") or msg.get("target")
        if not blueprint:
            return {"pass": False, "reason": "blueprint.compile missing 'blueprint' or 'target' field"}

    return {"pass": True, "reason": "cpp build check ok"}


def gate4_smoke(msg: dict) -> dict:
    """
    Gate 4: Smoke test (PIE headless).
    Validates that the command will not break the smoke test suite:
    level loads, backend ping, Blueprint executes.
    In production this runs a headless PIE session; here we check semantic constraints.
    """
    cmd_type = msg.get("type", "")

    if cmd_type in SKIP_SMOKE_GATE:
        return {"pass": True, "reason": "skipped — low-risk command"}

    # level.save: must have a valid level name
    if cmd_type == "level.save":
        level = msg.get("level")
        if not level or not isinstance(level, str):
            return {"pass": False, "reason": "level.save requires a 'level' string field"}
        if "/" not in level and not level.startswith("/Game"):
            return {
                "pass": False,
                "reason": f"level path '{level}' should start with '/Game/' (UE content path)",
            }

    # schema.migrate: must have a version
    if cmd_type == "schema.migrate":
        version = msg.get("to_version")
        if version is None:
            return {"pass": False, "reason": "schema.migrate requires 'to_version' field"}

    return {"pass": True, "reason": "smoke check ok"}


# ── Validation orchestrator ────────────────────────────────────────────────────

GateResult = dict  # {"pass": bool, "reason": str}


def validate(msg: dict) -> dict:
    """
    Run all 4 gates for a message. Returns a validator.result dict.
    Stops at the first failed gate.
    """
    relay = msg.get("_relay", {})
    command_id = relay.get("id", "unknown")
    command_type = msg.get("type", "unknown")
    ts = datetime.now(timezone.utc).isoformat()

    gates: dict[str, GateResult] = {}

    gate_fns = [
        ("gate1", gate1_schema),
        ("gate2", gate2_blueprint),
        ("gate3", gate3_cpp_build),
        ("gate4", gate4_smoke),
    ]

    failed_gate: Optional[int] = None

    for gate_key, gate_fn in gate_fns:
        result = gate_fn(msg)
        gates[gate_key] = result
        if not result["pass"]:
            gate_num = int(gate_key[-1])
            failed_gate = gate_num
            # Fill remaining gates as skipped
            for i in range(gate_num + 1, 5):
                gates[f"gate{i}"] = {"pass": False, "reason": "skipped — earlier gate failed"}
            break

    passed = failed_gate is None

    return {
        "type": "validator.result",
        "command_id": command_id,
        "command_type": command_type,
        "pass": passed,
        "failed_gate": failed_gate,
        "gates": gates,
        "ts": ts,
    }


# ── Service ────────────────────────────────────────────────────────────────────

class ValidatorService:
    """Connects to the relay and validates every incoming command."""

    def __init__(self, url: str):
        self.url = url

    async def run(self):
        from shells._client import RelayClient

        log.info(f"Connecting to relay at {self.url}")
        async with RelayClient("validator-service", url=self.url) as client:
            log.info("Connected — listening for commands")
            async for msg in client.messages():
                msg_type = msg.get("type", "")

                # Skip relay-internal messages and our own results
                if msg_type in RELAY_INTERNAL:
                    continue

                # Skip messages we sent (avoid loops)
                relay_meta = msg.get("_relay", {})
                if relay_meta.get("sender") == "validator-service":
                    continue

                result = validate(msg)
                status = "PASS" if result["pass"] else f"FAIL(gate{result['failed_gate']})"
                log.info(f"  {msg_type:<40} → {status}")

                await client.send(result)


async def _run_service(url: str):
    svc = ValidatorService(url)
    await svc.run()


def main():
    p = argparse.ArgumentParser(description="4-Gate Validator Service")
    p.add_argument("--url", default="ws://localhost:8765", help="Relay WebSocket URL")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate a sample command and exit without connecting",
    )
    args = p.parse_args()

    if args.dry_run:
        sample = {
            "type": "blueprint.set_property",
            "target": "BP_PlayerCharacter",
            "property": "MaxWalkSpeed",
            "value": 600,
            "_relay": {"id": "dryrun-001", "sender": "test"},
        }
        result = validate(sample)
        print(json.dumps(result, indent=2))
        return

    try:
        asyncio.run(_run_service(args.url))
    except KeyboardInterrupt:
        print("\nvalidator stopped")


if __name__ == "__main__":
    main()
