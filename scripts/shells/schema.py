#!/usr/bin/env python3
"""
schema.py — Contract registry CRUD with file watcher.
NAN-153

Manages JSON contract files in /contracts/. Supports listing, showing,
adding, removing, and watching for filesystem changes.

Sub-commands:
  list                     List all contracts
  show <name>              Print a contract
  add <name> <file.json>   Register a new contract (copies file to /contracts/)
  remove <name>            Delete a contract
  watch                    Watch /contracts/ for changes and broadcast via relay

Usage:
  python schema.py list
  python schema.py show blueprint_interface
  python schema.py add my_contract ./my_contract.json
  python schema.py remove my_contract
  python schema.py watch [--url ws://localhost:8765]
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CONTRACTS_DIR = ROOT / "contracts"
CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _list_contracts() -> list[Path]:
    return sorted(CONTRACTS_DIR.glob("*.json"))


def _contract_path(name: str) -> Path:
    if not name.endswith(".json"):
        name = name + ".json"
    return CONTRACTS_DIR / name


# ── Sub-commands ──────────────────────────────────────────────────────────────

def cmd_list(args):
    files = _list_contracts()
    if not files:
        print("No contracts registered.")
        return
    print(f"{'NAME':<40} {'SIZE':>8}  MODIFIED")
    print("─" * 70)
    for f in files:
        stat = f.stat()
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        print(f"{f.stem:<40} {stat.st_size:>8}  {mtime}")


def cmd_show(args):
    path = _contract_path(args.name)
    if not path.exists():
        print(f"Contract not found: {args.name}", file=sys.stderr)
        sys.exit(1)
    text = path.read_text()
    try:
        obj = json.loads(text)
        print(json.dumps(obj, indent=2))
    except json.JSONDecodeError:
        print(text)


def cmd_add(args):
    src = Path(args.file)
    if not src.exists():
        print(f"Source file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    # Validate JSON
    try:
        json.loads(src.read_text())
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    dest = _contract_path(args.name)
    shutil.copy2(src, dest)
    print(f"Registered contract: {args.name} → {dest}")


def cmd_remove(args):
    path = _contract_path(args.name)
    if not path.exists():
        print(f"Contract not found: {args.name}", file=sys.stderr)
        sys.exit(1)
    path.unlink()
    print(f"Removed contract: {args.name}")


async def _watch_loop(url: str):
    """Poll /contracts/ for changes and publish schema.updated events."""
    from shells._client import RelayClient

    # Snapshot current state: name → mtime
    def _snapshot() -> dict[str, float]:
        return {f.stem: f.stat().st_mtime for f in _list_contracts()}

    previous = _snapshot()
    print(f"Watching {CONTRACTS_DIR} — connecting to {url}")

    async with RelayClient("schema-shell", url=url) as client:
        print("Connected. Watching for contract changes…")
        while True:
            await asyncio.sleep(1)
            current = _snapshot()
            added   = set(current) - set(previous)
            removed = set(previous) - set(current)
            changed = {k for k in current if k in previous and current[k] != previous[k]}
            for name in sorted(added):
                print(f"  + added:   {name}")
                await client.send({"type": "schema.updated", "action": "add", "name": name})
            for name in sorted(removed):
                print(f"  - removed: {name}")
                await client.send({"type": "schema.updated", "action": "remove", "name": name})
            for name in sorted(changed):
                print(f"  ~ changed: {name}")
                await client.send({"type": "schema.updated", "action": "change", "name": name})
            previous = current


def cmd_watch(args):
    try:
        asyncio.run(_watch_loop(args.url))
    except KeyboardInterrupt:
        print("\nschema watch stopped")


# ── Argument parser ───────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Contract registry CRUD")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List contracts")

    sp_show = sub.add_parser("show", help="Print a contract")
    sp_show.add_argument("name")

    sp_add = sub.add_parser("add", help="Register a contract")
    sp_add.add_argument("name")
    sp_add.add_argument("file")

    sp_rm = sub.add_parser("remove", help="Remove a contract")
    sp_rm.add_argument("name")

    sp_watch = sub.add_parser("watch", help="Watch /contracts/ for changes")
    sp_watch.add_argument("--url", default="ws://localhost:8765")

    args = p.parse_args()
    dispatch = {"list": cmd_list, "show": cmd_show, "add": cmd_add,
                "remove": cmd_remove, "watch": cmd_watch}
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
