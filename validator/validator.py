#!/usr/bin/env python3
"""
validator.py — 4-Gate Validation Service (NAN-155)

Gate 1: Schema   — validate payload against registered contract
Gate 2: Blueprint Compile — ask UE host to compile blueprints, check result
Gate 3: C++ Build Check   — run UnrealBuildTool dry-run (or check last build log)
Gate 4: Smoke Test        — run PIE headless for N seconds, check for crashes

Usage (standalone):
  python3 validator.py check --cl-id CL-ABCD12
  python3 validator.py check --json '{"cmd":"blueprint_compile"}'
  python3 validator.py run-gate <1|2|3|4> [--cl-id CL-ABCD12]

Usage (service — listens on relay and validates incoming submits):
  python3 validator.py serve [--room ROOM]
"""

import asyncio
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))

CONTRACTS_DIR = os.environ.get(
    "CONTRACTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "contracts")
)
UBT_LOG = os.environ.get(
    "UBT_LOG",
    os.path.join(os.path.dirname(__file__), "..", "journal", "ubt_last.log")
)
PIE_TIMEOUT = int(os.environ.get("PIE_TIMEOUT", "10"))  # seconds to run PIE smoke test

def ts():
    return datetime.utcnow().isoformat() + "Z"

# ── Gate results ───────────────────────────────────────────────────────────────

class GateResult:
    def __init__(self, name: str, passed: bool, msg: str, details: dict = None):
        self.name    = name
        self.passed  = passed
        self.msg     = msg
        self.details = details or {}

    def to_dict(self):
        return {
            "name":    self.name,
            "passed":  self.passed,
            "msg":     self.msg,
            "details": self.details,
        }

# ── Gate 1: Schema validation ──────────────────────────────────────────────────

def gate1_schema(cl_data: dict) -> GateResult:
    """Check that cl_data matches its registered contract (if one exists)."""
    cmd = cl_data.get("cmd") or cl_data.get("type", "")
    schema_file = os.path.join(CONTRACTS_DIR, f"{cmd}.schema.json")

    if not os.path.exists(schema_file):
        # no schema registered — pass with warning
        return GateResult("1_schema", True, f"no schema for '{cmd}' — skipped", {"skipped": True})

    with open(schema_file) as f:
        schema = json.load(f)

    errors = _validate_schema(schema, cl_data, "$")
    if errors:
        return GateResult("1_schema", False, f"{len(errors)} schema error(s)", {"errors": errors})
    return GateResult("1_schema", True, "schema valid")

def _validate_schema(schema: dict, data, path: str) -> list:
    errors = []
    expected_type = schema.get("type")
    if expected_type:
        type_map = {"object": dict, "array": list, "string": str,
                    "number": (int, float), "integer": int, "boolean": bool}
        exp = type_map.get(expected_type)
        if exp and not isinstance(data, exp):
            errors.append(f"{path}: expected {expected_type}, got {type(data).__name__}")
            return errors
    if isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"{path}.{req}: required field missing")
        for k, sub in schema.get("properties", {}).items():
            if k in data:
                errors.extend(_validate_schema(sub, data[k], f"{path}.{k}"))
    return errors

# ── Gate 2: Blueprint compile ──────────────────────────────────────────────────

async def gate2_blueprint(cl_data: dict, room: str = "main") -> GateResult:
    """Ask UE host to compile blueprints and report back."""
    try:
        from relay_client import RelayClient
    except ImportError:
        return GateResult("2_blueprint", False, "relay_client not found — is relay running?")

    result = {}
    client = RelayClient(role="shell", name="validator", room=room)

    @client.on("cmd_result")
    async def on_result(msg):
        body = msg.get("body", {})
        if body.get("cmd") == "blueprint_compile":
            result["r"] = body

    try:
        await client.connect()
        await client.send({"type": "cmd", "cmd": "blueprint_compile"}, to="ue_host")

        deadline = asyncio.get_event_loop().time() + 30
        while "r" not in result and asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.wait_for(asyncio.ensure_future(client.ws.recv()), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        await client.close()
    except Exception as e:
        return GateResult("2_blueprint", False, f"relay error: {e}")

    r = result.get("r")
    if not r:
        return GateResult("2_blueprint", False, "no response from UE host (timeout)")
    if r.get("ok"):
        return GateResult("2_blueprint", True, "blueprints compiled OK")
    return GateResult("2_blueprint", False, r.get("error", "blueprint compile failed"), r)

# ── Gate 3: C++ build check ────────────────────────────────────────────────────

def gate3_cpp_build(cl_data: dict) -> GateResult:
    """
    Check the last UnrealBuildTool output for errors.
    In CI/head-client mode, could run UBT --check. Here we scan the log.
    """
    if not os.path.exists(UBT_LOG):
        return GateResult("3_cpp_build", True, "no UBT log found — skipped", {"skipped": True})

    try:
        with open(UBT_LOG) as f:
            content = f.read()
    except Exception as e:
        return GateResult("3_cpp_build", False, f"could not read UBT log: {e}")

    errors = [l for l in content.splitlines()
              if "error " in l.lower() and not l.strip().startswith("//")]
    warnings = [l for l in content.splitlines() if "warning " in l.lower()]

    if errors:
        return GateResult(
            "3_cpp_build", False,
            f"{len(errors)} build error(s) in UBT log",
            {"errors": errors[:10], "warning_count": len(warnings)}
        )
    return GateResult(
        "3_cpp_build", True,
        f"build clean ({len(warnings)} warnings)",
        {"warning_count": len(warnings)}
    )

# ── Gate 4: Smoke test ─────────────────────────────────────────────────────────

async def gate4_smoke(cl_data: dict, room: str = "main") -> GateResult:
    """
    Trigger PIE on the UE host, wait PIE_TIMEOUT seconds, then stop it.
    A crash / error message = fail.
    """
    try:
        from relay_client import RelayClient
    except ImportError:
        return GateResult("4_smoke", False, "relay_client not found")

    events = []
    client = RelayClient(role="shell", name="smoke-test", room=room)

    @client.on("*")
    async def capture(msg):
        events.append(msg)

    try:
        await client.connect()

        # start PIE
        await client.send({"type": "cmd", "cmd": "run_pie"}, to="ue_host")
        await asyncio.sleep(PIE_TIMEOUT)

        # stop PIE
        await client.send({"type": "cmd", "cmd": "stop_pie"}, to="ue_host")
        await asyncio.sleep(2)

        await client.close()
    except Exception as e:
        return GateResult("4_smoke", False, f"relay error during smoke: {e}")

    # scan events for crash/error indicators
    crash_keywords = ["crash", "fatal", "unhandled exception", "access violation"]
    crashes = [
        e for e in events
        if any(kw in json.dumps(e).lower() for kw in crash_keywords)
    ]

    if crashes:
        return GateResult("4_smoke", False, f"crash detected during PIE smoke test",
                          {"crash_events": crashes[:3]})
    return GateResult("4_smoke", True, f"PIE ran {PIE_TIMEOUT}s without crash")

# ── Full validation run ────────────────────────────────────────────────────────

async def run_all_gates(cl_data: dict, room: str = "main") -> dict:
    gates = {}
    passed_all = True

    # Gate 1 (sync)
    g1 = gate1_schema(cl_data)
    gates[g1.name] = g1.to_dict()
    if not g1.passed:
        passed_all = False

    # Gate 2 (async, needs relay)
    g2 = await gate2_blueprint(cl_data, room)
    gates[g2.name] = g2.to_dict()
    if not g2.passed:
        passed_all = False

    # Gate 3 (sync)
    g3 = gate3_cpp_build(cl_data)
    gates[g3.name] = g3.to_dict()
    if not g3.passed:
        passed_all = False

    # Gate 4 only runs if gates 1-3 pass (expensive)
    if passed_all:
        g4 = await gate4_smoke(cl_data, room)
        gates[g4.name] = g4.to_dict()
        if not g4.passed:
            passed_all = False
    else:
        gates["4_smoke"] = {"name": "4_smoke", "passed": None, "msg": "skipped (earlier gate failed)", "details": {}}

    return {
        "passed": passed_all,
        "ts":     ts(),
        "gates":  gates,
        "cl":     cl_data.get("id", "adhoc"),
    }

# ── Service mode ───────────────────────────────────────────────────────────────

async def serve_mode(room: str):
    from relay_client import RelayClient

    svc = RelayClient(role="shell", name="validator-svc", room=room)

    @svc.on("cmd")
    async def on_cmd(msg):
        body = msg.get("body", {})
        if body.get("validate") or body.get("cmd") == "validate":
            print(f"[validator] validating {body.get('cl_id','?')}...")
            report = await run_all_gates(body, room)
            verdict = "✓ PASS" if report["passed"] else "✗ FAIL"
            print(f"[validator] {verdict}  gates: "
                  + "  ".join(f"{k}={'P' if v['passed'] else 'F'}" for k, v in report["gates"].items()))
            # reply to sender
            sender_id = msg.get("from", {}).get("id", "*")
            await svc.send({"type": "validation_result", **report}, to=sender_id)

    welcome = await svc.connect()
    print(f"[validator] service running in room '{room}' | id={welcome['id']}")
    await svc.listen()

# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="4-Gate Validator")
    sub    = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="run all gates on a CL or raw JSON")
    p_check.add_argument("--cl-id", default=None)
    p_check.add_argument("--json",  default=None, help="raw JSON CL data")
    p_check.add_argument("--room",  default="main")
    p_check.add_argument("--output-json", action="store_true")

    p_gate = sub.add_parser("run-gate", help="run a single gate")
    p_gate.add_argument("gate", choices=["1","2","3","4"])
    p_gate.add_argument("--cl-id", default=None)
    p_gate.add_argument("--room",  default="main")

    p_serve = sub.add_parser("serve", help="run as relay service")
    p_serve.add_argument("--room", default="main")

    args = parser.parse_args()

    if args.command == "serve":
        asyncio.run(serve_mode(args.room))

    elif args.command == "check":
        if args.json:
            cl_data = json.loads(args.json)
        elif args.cl_id:
            cl_script = os.path.join(os.path.dirname(__file__), "..", "cl", "cl.py")
            r = subprocess.run([sys.executable, cl_script, "show", args.cl_id, "--json"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                print(f"CL not found: {args.cl_id}")
                sys.exit(1)
            cl_data = json.loads(r.stdout)
        else:
            print("provide --cl-id or --json")
            sys.exit(1)

        report = asyncio.run(run_all_gates(cl_data, args.room))

        if args.output_json:
            print(json.dumps(report))
            sys.exit(0 if report["passed"] else 1)

        print(f"\n4-Gate Validation Report — {report['cl']}")
        print("─" * 50)
        for name, g in report["gates"].items():
            status = "✓" if g["passed"] else ("—" if g["passed"] is None else "✗")
            print(f"  {status} {name:15s}  {g['msg']}")
        print("─" * 50)
        verdict = "✓ ALL GATES PASSED" if report["passed"] else "✗ VALIDATION FAILED"
        print(f"\n  {verdict}\n")
        sys.exit(0 if report["passed"] else 1)

    elif args.command == "run-gate":
        cl_data = {}
        if args.cl_id:
            cl_script = os.path.join(os.path.dirname(__file__), "..", "cl", "cl.py")
            r = subprocess.run([sys.executable, cl_script, "show", args.cl_id, "--json"],
                               capture_output=True, text=True)
            if r.returncode == 0:
                cl_data = json.loads(r.stdout)

        async def run_gate():
            g_map = {
                "1": lambda: gate1_schema(cl_data),
                "2": lambda: gate2_blueprint(cl_data, args.room),
                "3": lambda: gate3_cpp_build(cl_data),
                "4": lambda: gate4_smoke(cl_data, args.room),
            }
            fn = g_map[args.gate]
            result = await fn() if asyncio.iscoroutinefunction(fn) else fn()
            if asyncio.iscoroutine(result):
                result = await result
            status = "✓" if result.passed else "✗"
            print(f"{status} Gate {args.gate}: {result.msg}")
            if result.details:
                print(f"  details: {json.dumps(result.details, indent=2)}")
            sys.exit(0 if result.passed else 1)

        asyncio.run(run_gate())
