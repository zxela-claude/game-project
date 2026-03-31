#!/usr/bin/env python3
"""
cl.py — Changelist journal (Perforce-inspired)
NAN-154: create, view, restore, and bisect changesets.

Usage:
  python3 cl.py new    --type blueprint_compile --desc "Fixed AI nav mesh"
  python3 cl.py new    --type level_load  --args '{"level":"/Game/Maps/AI_Test"}' --desc "load AI map"
  python3 cl.py list   [--status pending|done|error] [--limit N]
  python3 cl.py show   <cl-id>   [--json]
  python3 cl.py mark   <cl-id>   --status done|error
  python3 cl.py note   <cl-id>   "added note text"
  python3 cl.py restore <cl-id>  [--dry-run]    # re-submit a past CL
  python3 cl.py bisect start <good-id> <bad-id> # find breaking CL
  python3 cl.py bisect next                      # step to midpoint
  python3 cl.py bisect mark  good|bad            # label current
  python3 cl.py bisect result                    # show first bad CL

Journal lives at: ../journal/changelist.jsonl
"""

import argparse
import json
import os
import sys
import uuid
import asyncio
import subprocess
from datetime import datetime
from typing import Optional

JOURNAL = os.environ.get(
    "CL_JOURNAL",
    os.path.join(os.path.dirname(__file__), "..", "journal", "changelist.jsonl")
)
BISECT_STATE = os.environ.get(
    "CL_BISECT",
    os.path.join(os.path.dirname(__file__), "..", "journal", "bisect.json")
)

def ts():
    return datetime.utcnow().isoformat() + "Z"

def short_id() -> str:
    return "CL-" + str(uuid.uuid4())[:6].upper()

# ── Journal I/O ────────────────────────────────────────────────────────────────

def load_all() -> list[dict]:
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL) as f:
        return [json.loads(l) for l in f if l.strip()]

def save_new(entry: dict):
    os.makedirs(os.path.dirname(os.path.abspath(JOURNAL)), exist_ok=True)
    with open(JOURNAL, "a") as f:
        f.write(json.dumps(entry) + "\n")

def update_entry(cl_id: str, updates: dict) -> bool:
    entries = load_all()
    found = False
    for e in entries:
        if e.get("id") == cl_id:
            e.update(updates)
            found = True
    if not found:
        return False
    with open(JOURNAL, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return True

def find_entry(cl_id: str) -> Optional[dict]:
    for e in load_all():
        if e.get("id") == cl_id:
            return e
    return None

# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_new(args):
    extra = {}
    if args.args:
        try:
            extra = json.loads(args.args)
        except json.JSONDecodeError as e:
            print(f"invalid --args JSON: {e}")
            sys.exit(1)

    entry = {
        "id":          short_id(),
        "type":        args.type,
        "cmd":         args.type,
        "description": args.desc or "",
        "status":      "pending",
        "args":        extra,
        "notes":       [],
        "created":     ts(),
        "updated":     ts(),
    }
    save_new(entry)
    print(f"created  {entry['id']}  {args.type}  \"{entry['description']}\"")
    return entry["id"]

def cmd_list(args):
    entries = load_all()
    if args.status:
        entries = [e for e in entries if e.get("status") == args.status]
    entries = entries[-(args.limit):]
    if not entries:
        print("no changelists found")
        return
    print(f"{'id':10s}  {'status':8s}  {'type':25s}  {'description':35s}  {'created':19s}")
    print("─" * 105)
    for e in entries:
        print(f"{e['id']:10s}  {e.get('status','?'):8s}  {e.get('type','?'):25s}  "
              f"{e.get('description','')[:35]:35s}  {e.get('created','')[:19]}")

def cmd_show(args):
    e = find_entry(args.cl_id)
    if not e:
        print(f"changelist '{args.cl_id}' not found")
        sys.exit(1)
    if args.json:
        print(json.dumps(e, indent=2))
        return
    print(f"ID:          {e['id']}")
    print(f"Type:        {e['type']}")
    print(f"Status:      {e['status']}")
    print(f"Description: {e.get('description','')}")
    print(f"Args:        {json.dumps(e.get('args',{}))}")
    print(f"Created:     {e['created'][:19]}")
    print(f"Updated:     {e.get('updated','')[:19]}")
    if e.get("notes"):
        print("Notes:")
        for n in e["notes"]:
            print(f"  [{n['ts'][:19]}] {n['text']}")

def cmd_mark(args):
    ok = update_entry(args.cl_id, {"status": args.status, "updated": ts()})
    if ok:
        print(f"{args.cl_id} → {args.status}")
    else:
        print(f"changelist '{args.cl_id}' not found")
        sys.exit(1)

def cmd_note(args):
    e = find_entry(args.cl_id)
    if not e:
        print(f"changelist '{args.cl_id}' not found")
        sys.exit(1)
    notes = e.get("notes", [])
    notes.append({"ts": ts(), "text": args.text})
    update_entry(args.cl_id, {"notes": notes, "updated": ts()})
    print(f"note added to {args.cl_id}")

async def _send_to_relay(entry: dict, room: str):
    """Re-submit a CL to the relay → UE host."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "relay"))
    from relay_client import RelayClient

    client  = RelayClient(role="shell", name="cl-restore", room=room)
    results = {}

    @client.on("cmd_result")
    async def on_result(msg):
        results["r"] = msg.get("body", {})

    await client.connect()
    payload = {"type": "cmd", "cmd": entry.get("cmd"), "cl_id": entry["id"], **entry.get("args", {})}
    await client.send(payload, to="ue_host")

    deadline = asyncio.get_event_loop().time() + 30
    while "r" not in results and asyncio.get_event_loop().time() < deadline:
        try:
            await asyncio.wait_for(asyncio.ensure_future(client.ws.recv()), timeout=1.0)
        except asyncio.TimeoutError:
            pass
    await client.close()
    return results.get("r", {})

def cmd_restore(args):
    e = find_entry(args.cl_id)
    if not e:
        print(f"changelist '{args.cl_id}' not found")
        sys.exit(1)

    print(f"Restore {e['id']}: {e['type']} — \"{e.get('description','')}\"")
    print(f"  args: {json.dumps(e.get('args', {}))}")

    if args.dry_run:
        print("[dry-run] would send to UE host")
        return

    confirm = input("Send to UE host? [y/N] ").strip().lower()
    if confirm != "y":
        print("aborted")
        return

    result = asyncio.run(_send_to_relay(e, room=args.room))
    if result.get("ok"):
        update_entry(e["id"], {"status": "restored", "updated": ts(),
                               "notes": e.get("notes", []) + [{"ts": ts(), "text": "restored via cl.py"}]})
        print(f"✓ restore complete: {result.get('msg','OK')}")
    else:
        print(f"✗ restore error: {result.get('error', result)}")

# ── Bisect ─────────────────────────────────────────────────────────────────────

def load_bisect() -> dict:
    if not os.path.exists(BISECT_STATE):
        return {}
    with open(BISECT_STATE) as f:
        return json.load(f)

def save_bisect(state: dict):
    os.makedirs(os.path.dirname(os.path.abspath(BISECT_STATE)), exist_ok=True)
    with open(BISECT_STATE, "w") as f:
        json.dump(state, f, indent=2)

def cmd_bisect(args):
    if args.bisect_cmd == "start":
        # collect all CL ids between good and bad (chronological order)
        all_cls  = load_all()
        ids      = [e["id"] for e in all_cls]
        try:
            gi = ids.index(args.good)
            bi = ids.index(args.bad)
        except ValueError as e:
            print(f"CL not found: {e}")
            sys.exit(1)
        if gi >= bi:
            print("good must come before bad chronologically")
            sys.exit(1)
        candidates = ids[gi:bi+1]
        state = {
            "good":       args.good,
            "bad":        args.bad,
            "candidates": candidates,
            "labels":     {args.good: "good", args.bad: "bad"},
            "current":    None,
        }
        save_bisect(state)
        mid = candidates[len(candidates)//2]
        state["current"] = mid
        save_bisect(state)
        print(f"bisect started: {len(candidates)} changesets between {args.good} and {args.bad}")
        print(f"Test:  {mid}")
        _show_cl_brief(mid)

    elif args.bisect_cmd == "next":
        state = load_bisect()
        if not state:
            print("no bisect in progress — run: cl.py bisect start <good> <bad>")
            sys.exit(1)
        cands   = state["candidates"]
        labels  = state["labels"]
        unlabeled = [c for c in cands if c not in labels]
        if not unlabeled:
            print("all candidates labelled — run: cl.py bisect result")
            return
        mid = unlabeled[len(unlabeled)//2]
        state["current"] = mid
        save_bisect(state)
        print(f"Test:  {mid}")
        _show_cl_brief(mid)

    elif args.bisect_cmd == "mark":
        state = load_bisect()
        if not state or not state.get("current"):
            print("no current bisect step")
            sys.exit(1)
        state["labels"][state["current"]] = args.label
        # prune candidates
        cands  = state["candidates"]
        labels = state["labels"]
        if args.label == "good":
            # discard everything up to and including current
            idx = cands.index(state["current"])
            state["candidates"] = cands[idx+1:]
        else:
            idx = cands.index(state["current"])
            state["candidates"] = cands[:idx+1]
        state["current"] = None
        save_bisect(state)
        remaining = [c for c in state["candidates"] if c not in labels]
        print(f"marked {state['labels'][list(state['labels'].keys())[-1]]} — {len(remaining)} candidates remaining")
        if not remaining:
            print("bisect complete — run: cl.py bisect result")

    elif args.bisect_cmd == "result":
        state = load_bisect()
        labels = state.get("labels", {})
        bads   = [c for c in state.get("candidates", []) if labels.get(c) == "bad"]
        first_bad = bads[0] if bads else state.get("bad")
        print(f"First bad changelist: {first_bad}")
        _show_cl_brief(first_bad)
        os.remove(BISECT_STATE)
        print("bisect state cleared")

def _show_cl_brief(cl_id: str):
    e = find_entry(cl_id)
    if e:
        print(f"  type: {e['type']}  desc: {e.get('description','')}  created: {e['created'][:19]}")

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Changelist journal")
    sub    = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new")
    p_new.add_argument("--type", required=True, help="command type, e.g. blueprint_compile")
    p_new.add_argument("--desc", default="", help="description")
    p_new.add_argument("--args", default=None, help="JSON extra args")

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--limit",  type=int, default=50)

    p_show = sub.add_parser("show")
    p_show.add_argument("cl_id")
    p_show.add_argument("--json", action="store_true")

    p_mark = sub.add_parser("mark")
    p_mark.add_argument("cl_id")
    p_mark.add_argument("--status", required=True, choices=["pending","done","error","restored"])

    p_note = sub.add_parser("note")
    p_note.add_argument("cl_id")
    p_note.add_argument("text")

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("cl_id")
    p_restore.add_argument("--room",    default="main")
    p_restore.add_argument("--dry-run", action="store_true")

    p_bisect = sub.add_parser("bisect")
    bs = p_bisect.add_subparsers(dest="bisect_cmd", required=True)
    p_bs = bs.add_parser("start")
    p_bs.add_argument("good")
    p_bs.add_argument("bad")
    bs.add_parser("next")
    p_bm = bs.add_parser("mark")
    p_bm.add_argument("label", choices=["good", "bad"])
    bs.add_parser("result")

    args = parser.parse_args()
    {
        "new":     cmd_new,
        "list":    cmd_list,
        "show":    cmd_show,
        "mark":    cmd_mark,
        "note":    cmd_note,
        "restore": cmd_restore,
        "bisect":  cmd_bisect,
    }[args.command](args)
