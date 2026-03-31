#!/usr/bin/env python3
"""
schema.py — Contract/schema registry
Define, list, validate, and assign data structures for the pipeline.

Usage:
  python3 schema.py list
  python3 schema.py add    <name> <json-schema-file>
  python3 schema.py show   <name>
  python3 schema.py remove <name>
  python3 schema.py validate <name> <data-json-file>
  python3 schema.py assign   <name> --to agent|ue_host|all

Schemas live in ../contracts/ as <name>.schema.json
"""

import argparse
import json
import os
import sys
from datetime import datetime

CONTRACTS_DIR = os.environ.get(
    "CONTRACTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "contracts")
)

def ts():
    return datetime.utcnow().isoformat() + "Z"

def schema_path(name: str) -> str:
    return os.path.join(CONTRACTS_DIR, f"{name}.schema.json")

def load_schema(name: str) -> dict:
    p = schema_path(name)
    if not os.path.exists(p):
        print(f"schema '{name}' not found", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        return json.load(f)

# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_list(args):
    os.makedirs(CONTRACTS_DIR, exist_ok=True)
    files = [f for f in os.listdir(CONTRACTS_DIR) if f.endswith(".schema.json")]
    if not files:
        print("no schemas registered")
        return
    print(f"{'name':30s}  {'version':8s}  {'assigned_to':20s}  {'updated':20s}")
    print("─" * 85)
    for f in sorted(files):
        name = f.replace(".schema.json", "")
        s = load_schema(name)
        meta = s.get("_meta", {})
        print(f"{name:30s}  {meta.get('version','—'):8s}  "
              f"{','.join(meta.get('assigned_to',[])) or '—':20s}  "
              f"{meta.get('updated','—')[:19]:20s}")

def cmd_add(args):
    os.makedirs(CONTRACTS_DIR, exist_ok=True)
    if not os.path.exists(args.file):
        print(f"file not found: {args.file}", file=sys.stderr)
        sys.exit(1)
    with open(args.file) as f:
        schema = json.load(f)
    schema.setdefault("_meta", {})
    schema["_meta"]["name"]    = args.name
    schema["_meta"]["updated"] = ts()
    schema["_meta"].setdefault("version", "1.0.0")
    with open(schema_path(args.name), "w") as f:
        json.dump(schema, f, indent=2)
    print(f"registered schema '{args.name}'")

def cmd_show(args):
    s = load_schema(args.name)
    print(json.dumps(s, indent=2))

def cmd_remove(args):
    p = schema_path(args.name)
    if not os.path.exists(p):
        print(f"schema '{args.name}' not found")
        return
    os.remove(p)
    print(f"removed '{args.name}'")

def cmd_validate(args):
    """Basic JSON Schema validation (subset: type, required, properties)."""
    schema = load_schema(args.name)
    if not os.path.exists(args.data):
        print(f"data file not found: {args.data}", file=sys.stderr)
        sys.exit(1)
    with open(args.data) as f:
        data = json.load(f)

    errors = _validate(schema, data, path="$")
    if not errors:
        print(f"✓ data is valid against '{args.name}'")
    else:
        print(f"✗ {len(errors)} validation error(s):")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)

def _validate(schema: dict, data, path: str) -> list:
    errors = []
    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "object":  dict,
            "array":   list,
            "string":  str,
            "number":  (int, float),
            "integer": int,
            "boolean": bool,
        }
        expected = type_map.get(expected_type)
        if expected and not isinstance(data, expected):
            errors.append(f"{path}: expected {expected_type}, got {type(data).__name__}")
            return errors  # can't go deeper

    if isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                errors.append(f"{path}.{req}: required field missing")
        props = schema.get("properties", {})
        for k, sub in props.items():
            if k in data:
                errors.extend(_validate(sub, data[k], f"{path}.{k}"))

    if isinstance(data, list):
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                errors.extend(_validate(items_schema, item, f"{path}[{i}]"))

    return errors

def cmd_assign(args):
    s = load_schema(args.name)
    s.setdefault("_meta", {})
    assigned = set(s["_meta"].get("assigned_to", []))
    targets = ["ue_host", "agent", "shell"] if args.to == "all" else [args.to]
    assigned.update(targets)
    s["_meta"]["assigned_to"] = sorted(assigned)
    s["_meta"]["updated"]     = ts()
    with open(schema_path(args.name), "w") as f:
        json.dump(s, f, indent=2)
    print(f"schema '{args.name}' assigned to: {', '.join(sorted(assigned))}")

def cmd_scaffold(args):
    """Create a starter schema file."""
    scaffold = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": args.name,
        "type": "object",
        "_meta": {"name": args.name, "version": "1.0.0", "assigned_to": [], "updated": ts()},
        "required": ["id", "type"],
        "properties": {
            "id":   {"type": "string", "description": "unique identifier"},
            "type": {"type": "string", "description": "message/object type"},
        },
        "additionalProperties": True,
    }
    out = f"{args.name}.schema.json"
    with open(out, "w") as f:
        json.dump(scaffold, f, indent=2)
    print(f"scaffolded → {out}  (edit then: schema.py add {args.name} {out})")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Schema/contract registry")
    sub    = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list")

    p_add = sub.add_parser("add")
    p_add.add_argument("name")
    p_add.add_argument("file")

    p_show = sub.add_parser("show")
    p_show.add_argument("name")

    p_rm = sub.add_parser("remove")
    p_rm.add_argument("name")

    p_val = sub.add_parser("validate")
    p_val.add_argument("name")
    p_val.add_argument("data")

    p_assign = sub.add_parser("assign")
    p_assign.add_argument("name")
    p_assign.add_argument("--to", required=True, choices=["ue_host", "agent", "shell", "all"])

    p_scaffold = sub.add_parser("scaffold")
    p_scaffold.add_argument("name")

    args = parser.parse_args()
    {
        "list":     cmd_list,
        "add":      cmd_add,
        "show":     cmd_show,
        "remove":   cmd_remove,
        "validate": cmd_validate,
        "assign":   cmd_assign,
        "scaffold": cmd_scaffold,
    }[args.command](args)
