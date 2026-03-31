#!/usr/bin/env python3
"""
record.py — Live relay recorder
Connects to the relay and writes every message to a JSONL session file.
These recordings feed the changelist journal (cl.py).

Usage:
  python3 record.py start [--session NAME] [--room ROOM]
  python3 record.py list
  python3 record.py show  <session>
  python3 record.py replay <session> [--dry-run]
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
from relay_client import RelayClient

RECORDINGS_DIR = os.environ.get(
    "RECORDINGS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "journal", "recordings")
)

def ts():
    return datetime.utcnow().isoformat() + "Z"

def session_path(name: str) -> str:
    return os.path.join(RECORDINGS_DIR, f"{name}.jsonl")

# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_start(args):
    os.makedirs(RECORDINGS_DIR, exist_ok=True)

    session_name = args.session or datetime.utcnow().strftime("session_%Y%m%d_%H%M%S")
    out_path     = session_path(session_name)

    client = RelayClient(role="shell", name="recorder", room=args.room)
    count  = [0]

    @client.on("*")
    async def on_any(msg):
        if msg.get("type") in ("welcome", "pong", "peers"):
            return
        with open(out_path, "a") as f:
            f.write(json.dumps(msg) + "\n")
        count[0] += 1
        print(f"\r  recorded: {count[0]:4d} messages  [{msg.get('type','?'):20s}]  {session_name}", end="", flush=True)

    welcome = await client.connect()
    print(f"Recording room '{args.room}' → {out_path}")
    print("Ctrl+C to stop\n")

    try:
        await client.listen()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await client.close()
        print(f"\n\nSession '{session_name}' saved — {count[0]} messages")

def cmd_list(args):
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".jsonl"))
    if not files:
        print("no recordings found")
        return
    print(f"{'session':35s}  {'messages':8s}  {'size':8s}")
    print("─" * 60)
    for f in files:
        name = f.replace(".jsonl", "")
        path = session_path(name)
        lines = sum(1 for _ in open(path))
        size  = os.path.getsize(path)
        print(f"{name:35s}  {lines:8d}  {size:6d}B")

def cmd_show(args):
    path = session_path(args.session)
    if not os.path.exists(path):
        print(f"session '{args.session}' not found")
        sys.exit(1)
    with open(path) as f:
        for line in f:
            msg = json.loads(line)
            sender = msg.get("from", {})
            body   = msg.get("body", {})
            print(f"{msg.get('_ts','')[:19]}  "
                  f"{sender.get('name','?'):15s} "
                  f"{msg.get('type','?'):18s} "
                  f"{json.dumps(body, separators=(',',':'))[:100]}")

async def cmd_replay(args):
    """Replay a recorded session back to the relay (for testing/restore)."""
    path = session_path(args.session)
    if not os.path.exists(path):
        print(f"session '{args.session}' not found")
        sys.exit(1)

    with open(path) as f:
        messages = [json.loads(l) for l in f if l.strip()]

    if args.dry_run:
        print(f"[dry-run] would replay {len(messages)} messages from '{args.session}'")
        for msg in messages[:10]:
            print(f"  {msg.get('_ts','')[:19]}  {msg.get('type','?')}  {json.dumps(msg.get('body',{}))[:80]}")
        if len(messages) > 10:
            print(f"  ... and {len(messages)-10} more")
        return

    client = RelayClient(role="shell", name="replay", room=args.room)
    await client.connect()
    for i, msg in enumerate(messages):
        body = msg.get("body", msg)
        to   = msg.get("from", {}).get("role", "*")
        await client.send(body, to=to)
        print(f"\r  replayed {i+1}/{len(messages)}", end="", flush=True)
    await client.close()
    print(f"\ndone — {len(messages)} messages replayed")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Relay recorder")
    sub    = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("--session", default=None, help="session name (default: timestamp)")
    p_start.add_argument("--room",    default="main")

    sub.add_parser("list")

    p_show = sub.add_parser("show")
    p_show.add_argument("session")

    p_replay = sub.add_parser("replay")
    p_replay.add_argument("session")
    p_replay.add_argument("--room",    default="main")
    p_replay.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if   args.command == "start":  asyncio.run(cmd_start(args))
    elif args.command == "list":   cmd_list(args)
    elif args.command == "show":   cmd_show(args)
    elif args.command == "replay": asyncio.run(cmd_replay(args))
