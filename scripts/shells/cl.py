#!/usr/bin/env python3
"""
cl.py — Changelist journal + restore shell.
NAN-156

Reads the relay's session journal (sessions/YYYY-MM-DD.jsonl) and provides
bisect-capable navigation, tagging, and restore dispatch.

Sub-commands:
  list    [--date YYYY-MM-DD] [--type PREFIX] [--sender NAME] [--n N]
                       List journal entries (newest first)
  show    <id>         Show a single entry in full
  mark-good <id>       Tag an entry as a known-good state checkpoint
  mark-bad  <id>       Tag an entry as a known-bad state checkpoint
  bisect               Show midpoint between marked good/bad; repeat to narrow
  restore <id>         Emit restore command to relay (dispatches to UE)
  marks                List current bisect marks

Usage:
  python cl.py list --n 20
  python cl.py list --type blueprint. --date 2026-03-31
  python cl.py show abc-1234-...
  python cl.py mark-good abc-1234-...
  python cl.py mark-bad  def-5678-...
  python cl.py bisect
  python cl.py restore abc-1234-... [--url ws://localhost:8765] [--dry-run]
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SESSIONS_DIR = ROOT / "sessions"
MARKS_FILE = SESSIONS_DIR / "bisect-marks.json"

# ANSI colour helpers
RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RED = "\033[31m"
MAGENTA = "\033[35m"


def _c(code: str, text: str) -> bool:
    """Colour text if stdout is a tty."""
    if sys.stdout.isatty():
        return f"{code}{text}{RESET}"
    return text


# ── Journal loading ────────────────────────────────────────────────────────────

def _journal_files(date_filter: str | None = None) -> list[Path]:
    """Return sorted list of .jsonl journal files, optionally filtered by date."""
    if not SESSIONS_DIR.exists():
        return []
    if date_filter:
        path = SESSIONS_DIR / f"{date_filter}.jsonl"
        return [path] if path.exists() else []
    return sorted(SESSIONS_DIR.glob("*.jsonl"))


def _load_entries(
    date_filter: str | None = None,
    type_prefix: str | None = None,
    sender: str | None = None,
) -> list[dict]:
    """Load all journal entries across files, applying optional filters."""
    entries = []
    for f in _journal_files(date_filter):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if type_prefix and not entry.get("type", "").startswith(type_prefix):
                continue
            rel = entry.get("_relay", {})
            if sender and rel.get("sender", "") != sender:
                continue
            entries.append(entry)
    return entries


def _find_entry(entry_id: str) -> dict | None:
    """Find a single journal entry by its _relay.id across all journal files."""
    for entry in _load_entries():
        if entry.get("_relay", {}).get("id") == entry_id:
            return entry
    return None


# ── Bisect marks ───────────────────────────────────────────────────────────────

def _load_marks() -> dict:
    """Load bisect marks from disk (returns {"good": id|None, "bad": id|None})."""
    if MARKS_FILE.exists():
        try:
            return json.loads(MARKS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"good": None, "bad": None}


def _save_marks(marks: dict):
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    MARKS_FILE.write_text(json.dumps(marks, indent=2) + "\n", encoding="utf-8")


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _short_id(entry_id: str) -> str:
    return entry_id[:8] if len(entry_id) >= 8 else entry_id


def _format_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return iso or "?"


def _restore_label(strategy: str) -> str:
    mapping = {
        "reversible": _c(GREEN, "REV"),
        "snapshot": _c(CYAN, "SNAP"),
        "none": _c(DIM, "N/A"),
    }
    return mapping.get(strategy, strategy)


def _type_colour(msg_type: str) -> str:
    if msg_type.startswith(("ue.", "blueprint.", "level.")):
        return _c(CYAN, msg_type)
    if msg_type.startswith("build."):
        return _c(YELLOW, msg_type)
    if msg_type.startswith("relay."):
        return _c(DIM, msg_type)
    return _c(GREEN, msg_type)


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_list(args):
    entries = _load_entries(
        date_filter=args.date,
        type_prefix=args.type,
        sender=args.sender,
    )
    # Newest first
    entries = list(reversed(entries))
    if args.n:
        entries = entries[: args.n]

    marks = _load_marks()
    good_id = marks.get("good")
    bad_id = marks.get("bad")

    if not entries:
        print("No journal entries found.")
        return

    header = f"{'ID':8}  {'Timestamp':19}  {'Restore':4}  {'Sender':16}  Type"
    print(_c(BOLD, header))
    print("─" * 75)

    for e in entries:
        rel = e.get("_relay", {})
        eid = rel.get("id", "?")
        short = _short_id(eid)
        ts = _format_ts(rel.get("timestamp", ""))
        restore = _restore_label(rel.get("restore", "none"))
        sender = (rel.get("sender", "?"))[:16]
        msg_type = _type_colour(e.get("type", "?"))

        tag = ""
        if eid == good_id:
            tag = _c(GREEN, " ← GOOD")
        elif eid == bad_id:
            tag = _c(RED, " ← BAD")

        print(f"{short}  {ts}  {restore}  {sender:<16}  {msg_type}{tag}")


def cmd_show(args):
    entry = _find_entry(args.id)
    if entry is None:
        print(f"Entry not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(entry, indent=2))


def cmd_mark_good(args):
    entry = _find_entry(args.id)
    if entry is None:
        print(f"Entry not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    marks = _load_marks()
    marks["good"] = args.id
    _save_marks(marks)
    print(_c(GREEN, f"Marked as GOOD: {_short_id(args.id)}"))


def cmd_mark_bad(args):
    entry = _find_entry(args.id)
    if entry is None:
        print(f"Entry not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    marks = _load_marks()
    marks["bad"] = args.id
    _save_marks(marks)
    print(_c(RED, f"Marked as BAD: {_short_id(args.id)}"))


def cmd_marks(args):
    marks = _load_marks()
    good_id = marks.get("good")
    bad_id = marks.get("bad")

    if not good_id and not bad_id:
        print("No bisect marks set.")
        print("  Use: cl.py mark-good <id>")
        print("       cl.py mark-bad  <id>")
        return

    if good_id:
        entry = _find_entry(good_id)
        if entry:
            rel = entry.get("_relay", {})
            print(f"{_c(GREEN, 'GOOD')}  {_short_id(good_id)}  {_format_ts(rel.get('timestamp',''))}  {entry.get('type','?')}")
        else:
            print(f"{_c(GREEN, 'GOOD')}  {_short_id(good_id)}  (entry not found in journal)")
    else:
        print(f"{_c(DIM, 'GOOD')}  (not set)")

    if bad_id:
        entry = _find_entry(bad_id)
        if entry:
            rel = entry.get("_relay", {})
            print(f"{_c(RED, 'BAD ')}  {_short_id(bad_id)}  {_format_ts(rel.get('timestamp',''))}  {entry.get('type','?')}")
        else:
            print(f"{_c(RED, 'BAD ')}  {_short_id(bad_id)}  (entry not found in journal)")
    else:
        print(f"{_c(DIM, 'BAD ')}  (not set)")


def cmd_bisect(args):
    """Binary search between marked-good and marked-bad to find the breaking entry."""
    marks = _load_marks()
    good_id = marks.get("good")
    bad_id = marks.get("bad")

    if not good_id or not bad_id:
        print("Bisect requires both a GOOD and BAD mark.", file=sys.stderr)
        print("  cl.py mark-good <id>", file=sys.stderr)
        print("  cl.py mark-bad  <id>", file=sys.stderr)
        sys.exit(1)

    all_entries = _load_entries()
    if not all_entries:
        print("No journal entries found.", file=sys.stderr)
        sys.exit(1)

    # Build chronological index
    ids = [e.get("_relay", {}).get("id") for e in all_entries]

    try:
        good_idx = ids.index(good_id)
    except ValueError:
        print(f"GOOD entry not found in journal: {good_id}", file=sys.stderr)
        sys.exit(1)

    try:
        bad_idx = ids.index(bad_id)
    except ValueError:
        print(f"BAD entry not found in journal: {bad_id}", file=sys.stderr)
        sys.exit(1)

    if good_idx >= bad_idx:
        print(
            f"GOOD ({_short_id(good_id)}) must come before BAD ({_short_id(bad_id)}) chronologically.",
            file=sys.stderr,
        )
        sys.exit(1)

    gap = bad_idx - good_idx - 1
    if gap == 0:
        print(_c(BOLD, "Bisect complete — the breaking change is the entry immediately after GOOD:"))
        entry = all_entries[bad_idx]
        rel = entry.get("_relay", {})
        print(f"  id:       {rel.get('id')}")
        print(f"  time:     {_format_ts(rel.get('timestamp', ''))}")
        print(f"  type:     {entry.get('type', '?')}")
        print(f"  sender:   {rel.get('sender', '?')}")
        print(f"\n  To restore to just before this: cl.py restore {good_id}")
        return

    mid_idx = good_idx + 1 + gap // 2
    mid_entry = all_entries[mid_idx]
    mid_rel = mid_entry.get("_relay", {})
    mid_id = mid_rel.get("id", "?")

    print(_c(BOLD, "Bisect — test this entry:"))
    print(f"  id:       {mid_id}")
    print(f"  time:     {_format_ts(mid_rel.get('timestamp', ''))}")
    print(f"  type:     {mid_entry.get('type', '?')}")
    print(f"  sender:   {mid_rel.get('sender', '?')}")
    print(f"  restore:  {mid_rel.get('restore', 'none')}")
    print(f"\n  Progress: {good_idx + 1} — [{mid_idx}] — {bad_idx}  ({gap} entries remaining)")
    print(f"\n  If this entry is GOOD: cl.py mark-good {mid_id}")
    print(f"  If this entry is BAD:  cl.py mark-bad  {mid_id}")


async def _send_restore(url: str, entry: dict, dry_run: bool):
    """Send a restore command to the relay."""
    rel = entry.get("_relay", {})
    entry_id = rel.get("id", "?")
    restore_type = rel.get("restore", "none")

    restore_msg = {
        "type": "changelist.restore",
        "target_id": entry_id,
        "target_type": entry.get("type"),
        "target_timestamp": rel.get("timestamp"),
        "restore_strategy": restore_type,
        "original": entry,
    }

    if dry_run:
        print(_c(YELLOW, "DRY RUN — restore command (not sent):"))
        print(json.dumps(restore_msg, indent=2))
        return

    if restore_type == "none":
        print(
            _c(YELLOW, f"Warning: entry {_short_id(entry_id)} has no restore strategy (type=none).")
        )
        print("Sending restore command anyway — UE handler may reject it.")

    try:
        import websockets

        async with websockets.connect(url) as ws:
            # Handshake
            await ws.send(json.dumps({"type": "relay.hello", "name": "cl-restore"}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                msg = json.loads(raw)
                if msg.get("type") == "relay.welcome":
                    break
                if msg.get("type") == "relay.error":
                    print(f"Relay error: {msg.get('reason')}", file=sys.stderr)
                    sys.exit(1)

            await ws.send(json.dumps(restore_msg))
            print(_c(GREEN, f"Restore dispatched for entry {_short_id(entry_id)}"))
            print(f"  strategy: {restore_type}")
            print(f"  type:     {entry.get('type', '?')}")
            print(f"  time:     {_format_ts(rel.get('timestamp', ''))}")
    except OSError as exc:
        print(f"Could not connect to relay at {url}: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_restore(args):
    entry = _find_entry(args.id)
    if entry is None:
        print(f"Entry not found: {args.id}", file=sys.stderr)
        sys.exit(1)
    asyncio.run(_send_restore(args.url, entry, args.dry_run))


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cl.py",
        description="Changelist journal + restore shell (NAN-156)",
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # list
    ls = sub.add_parser("list", help="List journal entries (newest first)")
    ls.add_argument("--date", metavar="YYYY-MM-DD", help="Filter to a specific day's journal")
    ls.add_argument("--type", metavar="PREFIX", help="Filter by message type prefix")
    ls.add_argument("--sender", metavar="NAME", help="Filter by sender name")
    ls.add_argument("--n", type=int, default=40, metavar="N", help="Max entries to show (default 40)")
    ls.set_defaults(func=cmd_list)

    # show
    sh = sub.add_parser("show", help="Show a single journal entry in full")
    sh.add_argument("id", help="Entry ID (full UUID or prefix)")
    sh.set_defaults(func=cmd_show)

    # mark-good
    mg = sub.add_parser("mark-good", help="Tag an entry as a known-good checkpoint")
    mg.add_argument("id", help="Entry ID")
    mg.set_defaults(func=cmd_mark_good)

    # mark-bad
    mb = sub.add_parser("mark-bad", help="Tag an entry as a known-bad checkpoint")
    mb.add_argument("id", help="Entry ID")
    mb.set_defaults(func=cmd_mark_bad)

    # marks
    mk = sub.add_parser("marks", help="Show current bisect marks")
    mk.set_defaults(func=cmd_marks)

    # bisect
    bs = sub.add_parser("bisect", help="Show midpoint between marked good/bad")
    bs.set_defaults(func=cmd_bisect)

    # restore
    rs = sub.add_parser("restore", help="Emit a restore command to the relay")
    rs.add_argument("id", help="Entry ID to restore to")
    rs.add_argument(
        "--url", default="ws://localhost:8765", metavar="URL",
        help="Relay WebSocket URL (default: ws://localhost:8765)"
    )
    rs.add_argument("--dry-run", action="store_true", help="Print restore command without sending")
    rs.set_defaults(func=cmd_restore)

    return p


def main():
    parser = _build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
