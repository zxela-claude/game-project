#!/usr/bin/env python3
"""
record.py — Session logger with replay, export, and filter.
NAN-153

Connects to the relay and captures every message to a local JSONL file.
Can also replay a recorded file back to the relay.

Sub-commands:
  start  [--output FILE]           Connect and record to FILE (default: recording.jsonl)
  replay <file> [--url URL]        Replay a recording back into the relay
  export <file> [--format json|csv] [--filter PREFIX]  Export/filter a recording
  stats  <file>                    Print statistics about a recording

Usage:
  python record.py start --output session.jsonl
  python record.py replay session.jsonl
  python record.py export session.jsonl --filter blueprint. --format json
  python record.py stats session.jsonl
"""

import argparse
import asyncio
import csv
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Sub-commands ──────────────────────────────────────────────────────────────

async def _start(output: Path, url: str):
    from shells._client import RelayClient

    print(f"Recording to {output} — connecting to {url}")
    count = 0

    with output.open("a", buffering=1) as f:
        async with RelayClient("record-shell", url=url) as client:
            print("Connected. Recording messages (Ctrl+C to stop)…")
            async for msg in client.messages():
                line = json.dumps(msg)
                f.write(line + "\n")
                count += 1
                msg_type = msg.get("type", "?")
                sender = msg.get("_relay", {}).get("sender", "?")
                print(f"  [{count}] {sender:<18} {msg_type}")

    print(f"\nRecorded {count} messages to {output}")


def cmd_start(args):
    output = Path(args.output)
    try:
        asyncio.run(_start(output, args.url))
    except KeyboardInterrupt:
        print("\nrecord stopped")


async def _replay(source: Path, url: str, delay: float):
    from shells._client import RelayClient

    lines = [l.strip() for l in source.read_text().splitlines() if l.strip()]
    print(f"Replaying {len(lines)} messages from {source} → {url}")

    async with RelayClient("record-replay", url=url) as client:
        for i, line in enumerate(lines, 1):
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Strip relay meta — relay will add fresh meta on receipt
            msg.pop("_relay", None)
            msg_type = msg.get("type", "?")
            if msg_type.startswith("relay."):
                continue  # skip relay system messages
            await client.send(msg)
            print(f"  [{i}/{len(lines)}] {msg_type}")
            if delay > 0:
                await asyncio.sleep(delay)

    print("Replay complete.")


def cmd_replay(args):
    source = Path(args.file)
    if not source.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_replay(source, args.url, args.delay))


def _load_messages(source: Path, filter_prefix: str | None) -> list[dict]:
    msgs = []
    for line in source.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if filter_prefix and not msg.get("type", "").startswith(filter_prefix):
            continue
        msgs.append(msg)
    return msgs


def cmd_export(args):
    source = Path(args.file)
    if not source.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    msgs = _load_messages(source, args.filter)
    if args.format == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(["timestamp", "sender", "type", "payload"])
        for m in msgs:
            relay = m.get("_relay", {})
            payload = json.dumps({k: v for k, v in m.items() if k != "_relay"})
            writer.writerow([relay.get("timestamp", ""), relay.get("sender", ""), m.get("type", ""), payload])
    else:
        print(json.dumps(msgs, indent=2))


def cmd_stats(args):
    source = Path(args.file)
    if not source.exists():
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    msgs = _load_messages(source, None)
    if not msgs:
        print("No messages found.")
        return

    from collections import Counter
    types = Counter(m.get("type", "?") for m in msgs)
    senders = Counter(m.get("_relay", {}).get("sender", "?") for m in msgs)

    print(f"Total messages: {len(msgs)}")
    print(f"\nTop types:")
    for t, n in types.most_common(10):
        print(f"  {n:>5}  {t}")
    print(f"\nSenders:")
    for s, n in senders.most_common():
        print(f"  {n:>5}  {s}")


# ── Argument parser ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Session logger and replayer")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp_start = sub.add_parser("start", help="Record relay traffic")
    sp_start.add_argument("--output", default="recording.jsonl")
    sp_start.add_argument("--url", default="ws://localhost:8765")

    sp_replay = sub.add_parser("replay", help="Replay a recording")
    sp_replay.add_argument("file")
    sp_replay.add_argument("--url", default="ws://localhost:8765")
    sp_replay.add_argument("--delay", type=float, default=0.1, help="Seconds between messages")

    sp_export = sub.add_parser("export", help="Export/filter a recording")
    sp_export.add_argument("file")
    sp_export.add_argument("--filter", metavar="PREFIX")
    sp_export.add_argument("--format", choices=["json", "csv"], default="json")

    sp_stats = sub.add_parser("stats", help="Show recording statistics")
    sp_stats.add_argument("file")

    args = p.parse_args()
    dispatch = {"start": cmd_start, "replay": cmd_replay,
                "export": cmd_export, "stats": cmd_stats}
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
