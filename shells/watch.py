#!/usr/bin/env python3
"""
watch.py — Live relay traffic monitor
Usage: python3 watch.py [--room ROOM] [--role ROLE] [--filter TYPE]
"""

import asyncio
import argparse
import json
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
from relay_client import RelayClient

COLORS = {
    "ue_host":  "\033[35m",  # magenta
    "agent":    "\033[36m",  # cyan
    "shell":    "\033[33m",  # yellow
    "discord":  "\033[34m",  # blue
    "reset":    "\033[0m",
    "dim":      "\033[2m",
    "bold":     "\033[1m",
    "green":    "\033[32m",
    "red":      "\033[31m",
}

def c(key): return COLORS.get(key, "")

def pretty_msg(msg: dict, filter_type: str):
    t = msg.get("type", "")
    if filter_type and t != filter_type:
        return
    sender = msg.get("from", {})
    role   = sender.get("role", "?")
    name   = sender.get("name", "?")
    body   = msg.get("body", msg)
    ts     = msg.get("ts", "")[:19].replace("T", " ")

    color = COLORS.get(role, "")
    print(f"{c('dim')}{ts}{c('reset')}  "
          f"{color}{name:15s}{c('reset')} "
          f"{c('bold')}{t:18s}{c('reset')} "
          f"{c('dim')}{json.dumps(body, separators=(',',':'))[:120]}{c('reset')}")

async def run(args):
    client = RelayClient(role="shell", name="watch", room=args.room)

    @client.on("*")
    async def on_any(msg):
        if msg.get("type") in ("welcome", "pong"):
            return
        pretty_msg(msg, args.filter)

    welcome = await client.connect()
    peers = welcome.get("peers", [])
    print(f"{c('green')}Connected to room '{args.room}' | {len(peers)} peers online{c('reset')}")
    print(f"{c('dim')}{'time':19s}  {'name':15s} {'type':18s} body{c('reset')}")
    print("─" * 80)

    try:
        await client.listen()
    except KeyboardInterrupt:
        pass
    finally:
        await client.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Watch relay traffic")
    parser.add_argument("--room",   default="main")
    parser.add_argument("--filter", default="", help="only show this message type")
    args = parser.parse_args()
    asyncio.run(run(args))
