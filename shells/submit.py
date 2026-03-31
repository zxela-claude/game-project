#!/usr/bin/env python3
"""
submit.py — Submit a validated changeset to the pipeline
Runs the 4-gate validator, then if all gates pass, creates a changelist entry
and sends the changeset to the UE host.

Usage:
  python3 submit.py <changelist-id>           # submit a cl from cl.py
  python3 submit.py --cmd blueprint_compile   # quick one-off command
  python3 submit.py status                    # show last submission
  python3 submit.py log [--limit N]           # submission history
"""

import asyncio
import argparse
import json
import os
import sys
import subprocess
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
from relay_client import RelayClient

SUBMIT_LOG = os.environ.get(
    "SUBMIT_LOG",
    os.path.join(os.path.dirname(__file__), "..", "journal", "submit_log.jsonl")
)
VALIDATOR  = os.path.join(os.path.dirname(__file__), "..", "validator", "validator.py")
CL_SCRIPT  = os.path.join(os.path.dirname(__file__), "..", "cl", "cl.py")

def ts():
    return datetime.utcnow().isoformat() + "Z"

def log_submit(entry: dict):
    os.makedirs(os.path.dirname(os.path.abspath(SUBMIT_LOG)), exist_ok=True)
    with open(SUBMIT_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

# ── Validation gate ────────────────────────────────────────────────────────────

def run_validator(cl_data: dict) -> tuple[bool, dict]:
    """Run validator.py and return (passed, report)."""
    try:
        result = subprocess.run(
            [sys.executable, VALIDATOR, "check", "--json", json.dumps(cl_data)],
            capture_output=True, text=True, timeout=120
        )
        report = json.loads(result.stdout) if result.stdout.strip() else {}
        return result.returncode == 0, report
    except subprocess.TimeoutExpired:
        return False, {"error": "validator timed out"}
    except Exception as e:
        return False, {"error": str(e)}

# ── Submit ─────────────────────────────────────────────────────────────────────

async def cmd_submit(args):
    # resolve changelist
    cl_data = {}
    if args.cl_id:
        result = subprocess.run(
            [sys.executable, CL_SCRIPT, "show", args.cl_id, "--json"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"error loading changelist '{args.cl_id}': {result.stderr}")
            sys.exit(1)
        cl_data = json.loads(result.stdout)
    elif args.cmd:
        cl_data = {"id": "adhoc", "type": "cmd", "cmd": args.cmd, "args": {}}
    else:
        print("provide a changelist ID or --cmd")
        sys.exit(1)

    print(f"Submitting changelist: {cl_data.get('id', '?')}")
    print(f"  type: {cl_data.get('type', '?')}")

    # run 4-gate validator
    print("\nRunning 4-gate validation...")
    passed, report = run_validator(cl_data)

    gates = report.get("gates", {})
    for gate_name, gate_result in gates.items():
        status = "✓" if gate_result.get("passed") else "✗"
        print(f"  {status} Gate {gate_name}: {gate_result.get('msg', '')}")

    if not passed:
        print("\n✗ Validation failed — not submitting")
        log_submit({"cl": cl_data.get("id"), "status": "rejected", "ts": ts(), "report": report})
        sys.exit(1)

    print("\n✓ All gates passed — submitting to UE host")

    # send to relay
    client = RelayClient(role="shell", name="submit", room=args.room)
    results = {}

    @client.on("cmd_result")
    async def on_result(msg):
        results["result"] = msg.get("body", {})

    await client.connect()
    payload = {
        "type":   "cmd",
        "cmd":    cl_data.get("cmd", "exec"),
        "cl_id":  cl_data.get("id"),
        **cl_data.get("args", {}),
    }
    await client.send(payload, to="ue_host")

    # wait up to 30s for ack
    deadline = asyncio.get_event_loop().time() + 30
    while "result" not in results and asyncio.get_event_loop().time() < deadline:
        try:
            await asyncio.wait_for(
                asyncio.ensure_future(client.ws.recv()), timeout=1.0
            )
        except asyncio.TimeoutError:
            pass

    await client.close()

    r = results.get("result", {})
    ok = r.get("ok", False)
    status = "done" if ok else "error"

    log_submit({
        "cl":    cl_data.get("id"),
        "status": status,
        "ts":    ts(),
        "result": r,
        "report": report,
    })

    if ok:
        print(f"\n✓ Submit complete: {r.get('msg', 'OK')}")
    else:
        print(f"\n✗ UE host reported error: {r.get('error', r)}")
        sys.exit(1)

def cmd_log(args):
    if not os.path.exists(SUBMIT_LOG):
        print("no submissions yet")
        return
    with open(SUBMIT_LOG) as f:
        entries = [json.loads(l) for l in f if l.strip()]
    entries = entries[-(args.limit):]
    print(f"{'cl':20s}  {'status':10s}  {'ts':20s}")
    print("─" * 55)
    for e in entries:
        print(f"{e.get('cl','?'):20s}  {e.get('status','?'):10s}  {e.get('ts','')[:19]}")

def cmd_status(args):
    if not os.path.exists(SUBMIT_LOG):
        print("no submissions yet")
        return
    with open(SUBMIT_LOG) as f:
        entries = [json.loads(l) for l in f if l.strip()]
    if not entries:
        print("no submissions yet")
        return
    last = entries[-1]
    print("Last submission:")
    print(f"  cl:     {last.get('cl', '?')}")
    print(f"  status: {last.get('status', '?')}")
    print(f"  ts:     {last.get('ts', '')[:19]}")
    print(f"  result: {json.dumps(last.get('result', {}))}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Submit validated changesets")
    sub    = parser.add_subparsers(dest="command")

    # default: submit <cl-id>
    p_sub = sub.add_parser("send", help="submit a changelist")
    p_sub.add_argument("cl_id", nargs="?", default=None)
    p_sub.add_argument("--cmd",  default=None, help="ad-hoc command (skips cl.py)")
    p_sub.add_argument("--room", default="main")

    p_log = sub.add_parser("log")
    p_log.add_argument("--limit", type=int, default=20)

    sub.add_parser("status")

    args = parser.parse_args()
    # support: submit.py <cl-id> directly (no subcommand)
    if args.command is None:
        if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
            args.cl_id  = sys.argv[1]
            args.cmd    = None
            args.room   = "main"
            asyncio.run(cmd_submit(args))
        else:
            parser.print_help()
    elif args.command == "send":   asyncio.run(cmd_submit(args))
    elif args.command == "log":    cmd_log(args)
    elif args.command == "status": cmd_status(args)
