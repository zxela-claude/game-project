#!/usr/bin/env python3
"""
submit.py — Manual command composer for the relay.
NAN-153

Composes and sends a single command to the relay with optional --dry-run.

Usage:
  python submit.py blueprint.set_property --payload '{"asset": "BP_Hero", "property": "Health", "value": 100}'
  python submit.py level.place_actor --payload '{"class": "BP_Enemy", "x": 0, "y": 0, "z": 0}'
  python submit.py blueprint.compile --dry-run
  python submit.py --interactive

Interactive mode:
  Prompts for type and JSON payload at the terminal. Ctrl+C to quit.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shells._client import RelayClient

GREEN  = "\033[92m"
YELLOW = "\033[93m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"


async def _send_command(url: str, msg_type: str, payload: dict, dry_run: bool):
    msg = {"type": msg_type, **payload}
    if dry_run:
        print(f"{YELLOW}[DRY RUN]{RESET} Would send:")
        print(json.dumps(msg, indent=2))
        return

    async with RelayClient("submit-shell", url=url) as client:
        await client.send(msg)
        print(f"{GREEN}Sent:{RESET} {msg_type}")
        print(json.dumps(msg, indent=2))

        # Wait briefly for any immediate response
        try:
            msg_back = await asyncio.wait_for(client._ws.recv(), timeout=2.0)
            resp = json.loads(msg_back)
            r_type = resp.get("type", "?")
            if not r_type.startswith("relay.history"):
                print(f"\n{DIM}Response: {r_type}{RESET}")
                print(json.dumps(resp, indent=2))
        except asyncio.TimeoutError:
            pass


async def _interactive(url: str, dry_run: bool):
    print(f"{BOLD}relay submit (interactive){RESET} — {url}")
    print("Enter message type and JSON payload. Ctrl+C to quit.\n")

    while True:
        try:
            msg_type = input("type> ").strip()
            if not msg_type:
                continue
            raw_payload = input("payload (JSON, or blank for {})> ").strip()
            payload = json.loads(raw_payload) if raw_payload else {}
        except (KeyboardInterrupt, EOFError):
            print("\nsubmit stopped")
            break
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
            continue

        await _send_command(url, msg_type, payload, dry_run)
        print()


def main():
    p = argparse.ArgumentParser(description="Manual command composer")
    p.add_argument("type", nargs="?", help="Message type (e.g. blueprint.set_property)")
    p.add_argument("--payload", default="{}", help="JSON payload string")
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--dry-run", action="store_true", help="Print command without sending")
    p.add_argument("--interactive", "-i", action="store_true", help="Interactive mode")
    args = p.parse_args()

    if args.interactive or not args.type:
        asyncio.run(_interactive(args.url, args.dry_run))
        return

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as e:
        print(f"Invalid --payload JSON: {e}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(_send_command(args.url, args.type, payload, args.dry_run))


if __name__ == "__main__":
    main()
