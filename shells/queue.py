#!/usr/bin/env python3
"""
queue.py — Job queue: enqueue commands, view status, drain results
Usage:
  python3 queue.py push   --cmd blueprint_compile
  python3 queue.py push   --cmd level_load --args '{"level":"/Game/Maps/MainMap"}'
  python3 queue.py push   --cmd exec       --args '{"command":"stat fps"}'
  python3 queue.py status
  python3 queue.py drain  [--timeout 30]
  python3 queue.py clear
"""

import asyncio
import argparse
import json
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
from relay_client import RelayClient

QUEUE_FILE = os.environ.get("QUEUE_FILE", os.path.join(os.path.dirname(__file__), "..", "journal", "queue.jsonl"))

def ts():
    return datetime.utcnow().isoformat() + "Z"

def load_queue():
    if not os.path.exists(QUEUE_FILE):
        return []
    with open(QUEUE_FILE) as f:
        return [json.loads(l) for l in f if l.strip()]

def save_entry(entry):
    os.makedirs(os.path.dirname(os.path.abspath(QUEUE_FILE)), exist_ok=True)
    with open(QUEUE_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")

def update_queue(entries):
    os.makedirs(os.path.dirname(os.path.abspath(QUEUE_FILE)), exist_ok=True)
    with open(QUEUE_FILE, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_push(args):
    extra = {}
    if args.args:
        extra = json.loads(args.args)
    entry = {
        "id":      str(uuid.uuid4())[:8],
        "status":  "pending",
        "cmd":     args.cmd,
        "room":    args.room,
        "created": ts(),
        **extra,
    }
    save_entry(entry)
    print(f"queued  [{entry['id']}]  {args.cmd}")

def cmd_status(args):
    entries = load_queue()
    if not entries:
        print("queue is empty")
        return
    counts = {}
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print(f"{'id':8s}  {'status':10s}  {'cmd':25s}  {'created':20s}")
    print("─" * 70)
    for e in entries:
        print(f"{e['id']:8s}  {e['status']:10s}  {e['cmd']:25s}  {e['created'][:19]}")
    print(f"\n  total: {len(entries)}  " + "  ".join(f"{k}: {v}" for k, v in counts.items()))

def cmd_clear(args):
    entries = load_queue()
    done    = [e for e in entries if e["status"] != "done"]
    update_queue(done)
    print(f"cleared {len(entries) - len(done)} completed jobs")

async def cmd_drain(args):
    """Send all pending jobs to UE and collect results."""
    entries = load_queue()
    pending = [e for e in entries if e["status"] == "pending"]
    if not pending:
        print("no pending jobs")
        return

    results = {}
    client = RelayClient(role="shell", name="queue", room=args.room)

    @client.on("cmd_result")
    async def on_result(msg):
        body = msg.get("body", {})
        jid  = body.get("job_id")
        if jid:
            results[jid] = body

    await client.connect()
    print(f"draining {len(pending)} jobs → ue_host ...")

    for job in pending:
        payload = {**job, "type": "cmd", "to": "ue_host", "job_id": job["id"]}
        await client.send(payload, to="ue_host")
        job["status"] = "sent"
        print(f"  → [{job['id']}] {job['cmd']}")

    # wait for results
    deadline = asyncio.get_event_loop().time() + args.timeout
    async def listen_with_timeout():
        while asyncio.get_event_loop().time() < deadline:
            if len(results) >= len(pending):
                break
            try:
                await asyncio.wait_for(client.ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    await listen_with_timeout()
    await client.close()

    for job in pending:
        r = results.get(job["id"], {})
        job["status"] = "done" if r.get("ok") else "error"
        job["result"] = r
        print(f"  ← [{job['id']}] {job['cmd']} → {'OK' if r.get('ok') else 'ERR'}")

    update_queue([e if e not in pending else next((j for j in pending if j["id"] == e["id"]), e)
                  for e in entries])

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Job queue for relay commands")
    sub    = parser.add_subparsers(dest="command", required=True)

    p_push = sub.add_parser("push")
    p_push.add_argument("--cmd",  required=True)
    p_push.add_argument("--args", default=None, help="JSON string of extra fields")
    p_push.add_argument("--room", default="main")

    p_status = sub.add_parser("status")

    p_drain = sub.add_parser("drain")
    p_drain.add_argument("--timeout", type=int, default=30)
    p_drain.add_argument("--room", default="main")

    p_clear = sub.add_parser("clear")

    args = parser.parse_args()
    if   args.command == "push":   cmd_push(args)
    elif args.command == "status": cmd_status(args)
    elif args.command == "drain":  asyncio.run(cmd_drain(args))
    elif args.command == "clear":  cmd_clear(args)
