#!/usr/bin/env python3
"""
queue.py — Validation queue monitor.
NAN-153

Shows the gate-level pass/fail status for every command that flows through
the relay. Validation results are published as relay messages of type
"validator.result".

  ID       TYPE                    G1  G2  G3  G4  RESULT
  ──────────────────────────────────────────────────────────
  ab12cd   blueprint.set_property  ✓   ✓   ✓   ✓   PASS
  ef34gh   blueprint.compile       ✓   ✗   —   —   FAIL(gate2)

If the validator service is not running, commands show as UNVALIDATED.

Usage:
  python queue.py [--url ws://localhost:8765] [--tail N]
"""

import argparse
import asyncio
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shells._client import RelayClient

# ANSI
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
DIM    = "\033[2m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

PASS_SYM = f"{GREEN}✓{RESET}"
FAIL_SYM = f"{RED}✗{RESET}"
SKIP_SYM = f"{DIM}—{RESET}"
PEND_SYM = f"{YELLOW}?{RESET}"


def _gate_sym(result: dict | None, gate: int) -> str:
    if result is None:
        return PEND_SYM
    gates = result.get("gates", {})
    g = gates.get(f"gate{gate}")
    if g is None:
        return SKIP_SYM
    return PASS_SYM if g.get("pass") else FAIL_SYM


def _overall(result: dict | None) -> str:
    if result is None:
        return f"{YELLOW}PENDING{RESET}"
    passed = result.get("pass", False)
    if passed:
        return f"{GREEN}PASS{RESET}"
    failed_gate = result.get("failed_gate")
    if failed_gate:
        return f"{RED}FAIL(gate{failed_gate}){RESET}"
    return f"{RED}FAIL{RESET}"


class QueueMonitor:
    def __init__(self, tail: int):
        # command_id → {type, sender, result}
        self._entries: dict[str, dict] = {}
        self._order: deque[str] = deque(maxlen=tail)
        self._tail = tail

    def _on_command(self, msg: dict):
        relay = msg.get("_relay", {})
        cid = relay.get("id", "?")
        self._entries[cid] = {
            "short_id": cid[:6],
            "type": msg.get("type", "?"),
            "sender": relay.get("sender", "?"),
            "result": None,
        }
        if cid not in self._order:
            self._order.append(cid)

    def _on_result(self, msg: dict):
        cid = msg.get("command_id")
        if cid and cid in self._entries:
            self._entries[cid]["result"] = msg

    def ingest(self, msg: dict):
        t = msg.get("type", "")
        if t == "validator.result":
            self._on_result(msg)
        elif not t.startswith("relay.") and "_relay" in msg:
            self._on_command(msg)

    def render(self):
        import os
        # Clear screen
        print("\033[2J\033[H", end="")
        print(f"{BOLD}relay queue{RESET} — last {self._tail} commands\n")
        print(f"{BOLD}{'ID':<8}{'TYPE':<35}{'SENDER':<18}{'G1':<5}{'G2':<5}{'G3':<5}{'G4':<5}RESULT{RESET}")
        print("─" * 100)
        for cid in list(self._order):
            e = self._entries.get(cid)
            if not e:
                continue
            r = e["result"]
            g = [_gate_sym(r, i) for i in range(1, 5)]
            print(
                f"{e['short_id']:<8}{e['type']:<35}{e['sender'][:16]:<18}"
                f"{g[0]:<14}{g[1]:<14}{g[2]:<14}{g[3]:<14}{_overall(r)}"
            )


async def run(url: str, tail: int):
    print(f"Connecting to {url}…")
    monitor = QueueMonitor(tail)

    async with RelayClient("queue-shell", url=url) as client:
        async for msg in client.messages():
            monitor.ingest(msg)
            monitor.render()


def main():
    p = argparse.ArgumentParser(description="Validation queue monitor")
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--tail", type=int, default=20, help="Number of recent commands to show")
    args = p.parse_args()
    try:
        asyncio.run(run(args.url, args.tail))
    except KeyboardInterrupt:
        print("\nqueue stopped")


if __name__ == "__main__":
    main()
