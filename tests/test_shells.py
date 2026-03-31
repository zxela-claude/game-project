"""
Tests for the 5 command shells (NAN-153).
These tests run without a live relay — they test shell logic directly.
"""

import asyncio
import json
import sys
import tempfile
import time
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure scripts/ is on path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ── _client.py ────────────────────────────────────────────────────────────────

class TestRelayClient:
    """Basic structural tests for RelayClient."""

    def test_import(self):
        from shells._client import RelayClient
        c = RelayClient("test-shell", url="ws://localhost:8765")
        assert c.name == "test-shell"
        assert c.url == "ws://localhost:8765"


# ── watch.py ─────────────────────────────────────────────────────────────────

class TestWatch:
    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from shells import watch
        self.watch = watch

    def test_colour_for_type_ue(self):
        assert self.watch._colour_for_type("ue.cmd") == self.watch.CYAN

    def test_colour_for_type_blueprint(self):
        assert self.watch._colour_for_type("blueprint.set_property") == self.watch.CYAN

    def test_colour_for_type_relay(self):
        assert self.watch._colour_for_type("relay.error") == self.watch.DIM

    def test_colour_for_type_build(self):
        assert self.watch._colour_for_type("build.run") == self.watch.YELLOW

    def test_colour_for_type_other(self):
        assert self.watch._colour_for_type("schema.updated") == self.watch.GREEN

    def test_format_latency_returns_string(self):
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        result = self.watch._format_latency(ts)
        assert result.endswith("ms")

    def test_format_latency_bad_input(self):
        assert self.watch._format_latency("not-a-date") == "?"

    def test_snippet_truncates(self):
        msg = {"type": "blueprint.set_property", "data": "x" * 200}
        result = self.watch._snippet(msg)
        assert len(result) <= 85  # 80 + ellipsis

    def test_snippet_excludes_relay_meta(self):
        msg = {"type": "x", "_relay": {"id": "secret"}, "payload": 1}
        result = self.watch._snippet(msg)
        assert "secret" not in result
        assert "payload" in result


# ── queue.py ──────────────────────────────────────────────────────────────────

class TestQueueMonitor:
    def setup_method(self):
        from shells.queue import QueueMonitor
        self.QueueMonitor = QueueMonitor

    def test_pending_initially(self):
        m = self.QueueMonitor(tail=10)
        assert len(m._entries) == 0

    def test_ingest_command(self):
        m = self.QueueMonitor(tail=10)
        msg = {
            "type": "blueprint.set_property",
            "_relay": {"id": "abc123", "sender": "agent"},
        }
        m.ingest(msg)
        assert "abc123" in m._entries
        assert m._entries["abc123"]["type"] == "blueprint.set_property"
        assert m._entries["abc123"]["result"] is None

    def test_ingest_result_updates_entry(self):
        m = self.QueueMonitor(tail=10)
        msg = {
            "type": "blueprint.set_property",
            "_relay": {"id": "abc123", "sender": "agent"},
        }
        m.ingest(msg)
        result = {
            "type": "validator.result",
            "command_id": "abc123",
            "pass": True,
            "gates": {"gate1": {"pass": True}, "gate2": {"pass": True}},
        }
        m.ingest(result)
        assert m._entries["abc123"]["result"]["pass"] is True

    def test_relay_messages_ignored(self):
        m = self.QueueMonitor(tail=10)
        msg = {"type": "relay.welcome", "client_id": "x"}
        m.ingest(msg)
        assert len(m._entries) == 0

    def test_tail_limit(self):
        m = self.QueueMonitor(tail=3)
        for i in range(5):
            m.ingest({"type": "blueprint.x", "_relay": {"id": f"id{i}", "sender": "a"}})
        assert len(m._order) == 3

    def test_gate_sym_pass(self):
        from shells.queue import _gate_sym, PASS_SYM
        result = {"pass": True, "gates": {"gate1": {"pass": True}}}
        assert _gate_sym(result, 1) == PASS_SYM

    def test_gate_sym_fail(self):
        from shells.queue import _gate_sym, FAIL_SYM
        result = {"pass": False, "gates": {"gate1": {"pass": False}}}
        assert _gate_sym(result, 1) == FAIL_SYM

    def test_gate_sym_skip(self):
        from shells.queue import _gate_sym, SKIP_SYM
        result = {"pass": True, "gates": {}}
        assert _gate_sym(result, 2) == SKIP_SYM

    def test_gate_sym_pending(self):
        from shells.queue import _gate_sym, PEND_SYM
        assert _gate_sym(None, 1) == PEND_SYM

    def test_overall_pass(self):
        from shells.queue import _overall
        assert "PASS" in _overall({"pass": True})

    def test_overall_fail_with_gate(self):
        from shells.queue import _overall
        s = _overall({"pass": False, "failed_gate": 2})
        assert "FAIL" in s and "gate2" in s

    def test_overall_pending(self):
        from shells.queue import _overall
        assert "PENDING" in _overall(None)


# ── schema.py ─────────────────────────────────────────────────────────────────

class TestSchema:
    def test_list_empty(self, tmp_path, monkeypatch):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)
        captured = StringIO()
        with patch("sys.stdout", captured):
            schema_mod.cmd_list(MagicMock())
        assert "No contracts" in captured.getvalue()

    def test_add_and_list(self, tmp_path, monkeypatch):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)

        # Create a source JSON file
        src = tmp_path / "input.json"
        src.write_text('{"version": 1}')

        args = MagicMock()
        args.name = "test_contract"
        args.file = str(src)
        schema_mod.cmd_add(args)

        dest = tmp_path / "test_contract.json"
        assert dest.exists()
        assert json.loads(dest.read_text()) == {"version": 1}

    def test_add_invalid_json_exits(self, tmp_path, monkeypatch):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)

        src = tmp_path / "bad.json"
        src.write_text("not json {{")
        args = MagicMock()
        args.name = "bad"
        args.file = str(src)
        with pytest.raises(SystemExit):
            schema_mod.cmd_add(args)

    def test_show_contract(self, tmp_path, monkeypatch, capsys):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)

        (tmp_path / "my_contract.json").write_text('{"x": 1}')
        args = MagicMock()
        args.name = "my_contract"
        schema_mod.cmd_show(args)
        out = capsys.readouterr().out
        assert '"x"' in out

    def test_show_missing_exits(self, tmp_path, monkeypatch):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)
        args = MagicMock()
        args.name = "nonexistent"
        with pytest.raises(SystemExit):
            schema_mod.cmd_show(args)

    def test_remove_contract(self, tmp_path, monkeypatch, capsys):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)

        (tmp_path / "del_me.json").write_text('{"x": 1}')
        args = MagicMock()
        args.name = "del_me"
        schema_mod.cmd_remove(args)
        assert not (tmp_path / "del_me.json").exists()

    def test_remove_missing_exits(self, tmp_path, monkeypatch):
        import shells.schema as schema_mod
        monkeypatch.setattr(schema_mod, "CONTRACTS_DIR", tmp_path)
        args = MagicMock()
        args.name = "nope"
        with pytest.raises(SystemExit):
            schema_mod.cmd_remove(args)


# ── record.py ─────────────────────────────────────────────────────────────────

class TestRecord:
    def test_load_messages_basic(self, tmp_path):
        f = tmp_path / "rec.jsonl"
        f.write_text(
            '{"type":"blueprint.set_property","_relay":{"sender":"agent"}}\n'
            '{"type":"relay.welcome"}\n'
            '{"type":"blueprint.compile","_relay":{"sender":"agent"}}\n'
        )
        import shells.record as record_mod
        msgs = record_mod._load_messages(f, None)
        assert len(msgs) == 3

    def test_load_messages_filter(self, tmp_path):
        f = tmp_path / "rec.jsonl"
        f.write_text(
            '{"type":"blueprint.set_property"}\n'
            '{"type":"relay.welcome"}\n'
            '{"type":"blueprint.compile"}\n'
        )
        import shells.record as record_mod
        msgs = record_mod._load_messages(f, "blueprint.")
        assert len(msgs) == 2
        assert all(m["type"].startswith("blueprint.") for m in msgs)

    def test_load_messages_skips_invalid_json(self, tmp_path):
        f = tmp_path / "rec.jsonl"
        f.write_text('{"type":"ok"}\nnot json\n{"type":"ok2"}\n')
        import shells.record as record_mod
        msgs = record_mod._load_messages(f, None)
        assert len(msgs) == 2

    def test_export_csv(self, tmp_path, capsys):
        f = tmp_path / "rec.jsonl"
        f.write_text('{"type":"blueprint.x","_relay":{"sender":"agent","timestamp":"2026-01-01T00:00:00+00:00"}}\n')
        import shells.record as record_mod
        args = MagicMock()
        args.file = str(f)
        args.filter = None
        args.format = "csv"
        record_mod.cmd_export(args)
        out = capsys.readouterr().out
        assert "blueprint.x" in out
        assert "timestamp" in out  # header

    def test_stats(self, tmp_path, capsys):
        f = tmp_path / "rec.jsonl"
        f.write_text(
            '{"type":"blueprint.x","_relay":{"sender":"agent"}}\n'
            '{"type":"blueprint.x","_relay":{"sender":"agent"}}\n'
            '{"type":"level.y","_relay":{"sender":"vscode"}}\n'
        )
        import shells.record as record_mod
        args = MagicMock()
        args.file = str(f)
        record_mod.cmd_stats(args)
        out = capsys.readouterr().out
        assert "3" in out
        assert "blueprint.x" in out

    def test_stats_empty(self, tmp_path, capsys):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        import shells.record as record_mod
        args = MagicMock()
        args.file = str(f)
        record_mod.cmd_stats(args)
        out = capsys.readouterr().out
        assert "No messages" in out


# ── submit.py ─────────────────────────────────────────────────────────────────

class TestSubmit:
    @pytest.mark.asyncio
    async def test_dry_run(self, capsys):
        import shells.submit as submit_mod
        await submit_mod._send_command(
            url="ws://localhost:8765",
            msg_type="blueprint.set_property",
            payload={"asset": "BP_Hero", "property": "Health", "value": 100},
            dry_run=True,
        )
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "blueprint.set_property" in out
        assert "BP_Hero" in out

    @pytest.mark.asyncio
    async def test_dry_run_no_connection(self):
        """Dry run should not attempt any network connection."""
        import shells.submit as submit_mod
        with patch("websockets.connect", side_effect=ConnectionRefusedError("should not connect")):
            # Should not raise — dry run exits before connecting
            await submit_mod._send_command(
                url="ws://localhost:1",
                msg_type="test.cmd",
                payload={},
                dry_run=True,
            )
