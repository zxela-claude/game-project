# Game Dev Pipeline

Multi-agent Unreal Engine 5 development pipeline.
Discord = prompts only. Code lives in VSCode + Visual Studio.

## Quick start

```bash
# 1. Start the relay server (critical path — start this first)
cd relay
pip install websockets
RELAY_TOKEN=your-secret python3 server.py

# 2. Paste ue/bootstrap.py into UE > Tools > Execute Python Script
#    (set RELAY_URL and RELAY_TOKEN env vars first, or edit the defaults)

# 3. Watch traffic
cd shells
python3 watch.py

# 4. Run the validator service (optional — enables auto-gate on submit)
cd validator
python3 validator.py serve
```

## Directory layout

```
relay/          WebSocket hub :8765 — multi-user command bus
  server.py       relay server
  relay_client.py shared client library

ue/
  bootstrap.py    paste once into UE Python — connects to relay

shells/           5 operator command shells
  watch.py        live traffic monitor
  queue.py        job queue — push/drain/status
  schema.py       contract registry — define/validate/assign schemas
  record.py       session recorder — replay for restore
  submit.py       submit validated changesets

cl/
  cl.py           changelist journal — new/list/show/mark/restore/bisect

validator/
  validator.py    4-gate validator — schema / blueprint / C++ / smoke

contracts/        registered JSON schemas (name.schema.json)
journal/          runtime data — changelist.jsonl, queue.jsonl, recordings/
```

## Command cheat-sheet

### Relay
```bash
RELAY_TOKEN=x python3 relay/server.py          # start server
```

### Watch
```bash
python3 shells/watch.py                        # all traffic
python3 shells/watch.py --filter cmd           # commands only
```

### Queue
```bash
python3 shells/queue.py push --cmd blueprint_compile
python3 shells/queue.py push --cmd level_load --args '{"level":"/Game/Maps/Main"}'
python3 shells/queue.py status
python3 shells/queue.py drain
```

### Schema
```bash
python3 shells/schema.py scaffold my_command   # generate starter schema
python3 shells/schema.py add my_command my_command.schema.json
python3 shells/schema.py list
python3 shells/schema.py validate my_command data.json
python3 shells/schema.py assign my_command --to all
```

### Record
```bash
python3 shells/record.py start --session pre-patch
python3 shells/record.py list
python3 shells/record.py show pre-patch
python3 shells/record.py replay pre-patch --dry-run
```

### Submit
```bash
python3 shells/submit.py CL-ABCD12            # submit + validate
python3 shells/submit.py log
```

### Changelist journal (cl.py)
```bash
python3 cl/cl.py new --type blueprint_compile --desc "Nav mesh fix"
python3 cl/cl.py list
python3 cl/cl.py show CL-ABCD12
python3 cl/cl.py mark CL-ABCD12 --status done
python3 cl/cl.py note CL-ABCD12 "tested on AI_Test map — OK"
python3 cl/cl.py restore CL-ABCD12           # re-send to UE host
python3 cl/cl.py bisect start CL-GOOD CL-BAD  # find breaking change
python3 cl/cl.py bisect next
python3 cl/cl.py bisect mark good
python3 cl/cl.py bisect result
```

### 4-Gate Validator
```bash
python3 validator/validator.py check --cl-id CL-ABCD12
python3 validator/validator.py run-gate 1 --cl-id CL-ABCD12
python3 validator/validator.py serve   # run as relay service
```

## 4 Gates

| Gate | Name | What it checks |
|------|------|----------------|
| 1 | Schema | payload matches registered contract |
| 2 | Blueprint Compile | UE host compiles all dirty blueprints, reports errors |
| 3 | C++ Build Check | scans last UnrealBuildTool log for errors |
| 4 | Smoke Test | runs PIE headless for N seconds, checks for crashes |

Gate 4 only runs if gates 1–3 pass (it's expensive).

## Environment

Copy `.env.example` → `.env` and set:
- `RELAY_TOKEN` — shared secret between server and all clients
- `RELAY_URL` — `ws://YOUR_SERVER_IP:8765`
