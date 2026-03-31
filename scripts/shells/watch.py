#!/usr/bin/env python3
"""
watch.py — Live traffic monitor for the relay.
NAN-153

Displays every message flowing through the relay with:
  direction  sender       type                  latency   payload snippet
  ─────────────────────────────────────────────────────────────────────────
  →          agent        blueprint.set_prop    2ms       {"asset": ...}

Usage:
  python watch.py [--url ws://localhost:8765] [--filter TYPE_PREFIX]
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shells._client import RelayClient

# ANSI colours
CYAN   = "\033[96m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
DIM    = "\033[2m"
RESET  = "\033[0m"
BOLD   = "\033[1m"


def _colour_for_type(t: str) -> str:
    if t.startswith("ue.") or t.startswith("blueprint.") or t.startswith("level."):
        return CYAN
    if t.startswith("relay."):
        return DIM
    if t.startswith("build."):
        return YELLOW
    return GREEN


def _format_latency(timestamp_str: str) -> str:
    """Compute latency from relay timestamp to now in ms."""
    try:
        ts = datetime.fromisoformat(timestamp_str)
        now = datetime.now(timezone.utc)
        ms = (now - ts).total_seconds() * 1000
        return f"{ms:.0f}ms"
    except Exception:
        return "?"


def _snippet(msg: dict, max_len: int = 80) -> str:
    """Return a short payload snippet, excluding _relay meta."""
    body = {k: v for k, v in msg.items() if k != "_relay"}
    text = json.dumps(body, separators=(",", ":"))
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


def _print_header():
    print(f"{BOLD}{'DIRECTION':<10}{'SENDER':<20}{'TYPE':<35}{'LATENCY':<10}PAYLOAD{RESET}")
    print("─" * 110)


def _print_message(msg: dict, direction: str = "→"):
    relay = msg.get("_relay", {})
    sender = relay.get("sender", "?")[:18]
    msg_type = msg.get("type", "?")
    latency = _format_latency(relay.get("timestamp", "")) if relay else "?"
    colour = _colour_for_type(msg_type)
    snippet = _snippet(msg)
    print(f"{colour}{direction:<10}{sender:<20}{msg_type:<35}{latency:<10}{DIM}{snippet}{RESET}")


async def run(url: str, filter_prefix: str | None):
    print(f"{BOLD}relay watch{RESET} — connecting to {url}")
    _print_header()

    async with RelayClient("watch-shell", url=url) as client:
        async for msg in client.messages():
            msg_type = msg.get("type", "")
            if filter_prefix and not msg_type.startswith(filter_prefix):
                continue
            direction = "→" if msg.get("_relay", {}).get("sender") == "ue-head-client" else "←"
            _print_message(msg, direction)


def main():
    p = argparse.ArgumentParser(description="Relay traffic monitor")
    p.add_argument("--url", default="ws://localhost:8765", help="Relay URL")
    p.add_argument("--filter", metavar="PREFIX", help="Only show types matching prefix")
    args = p.parse_args()

    try:
        asyncio.run(run(args.url, args.filter))
    except KeyboardInterrupt:
        print(f"\n{DIM}watch stopped{RESET}")


if __name__ == "__main__":
    main()
