"""
Tests for cl.py — changelist journal + restore shell (NAN-156).
Runs without a live relay.
"""

import json
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_entry(entry_id: str, msg_type: str, sender: str = "agent", restore: str = "reversible"):
    return {
        "type": msg_type,
        "data": {"asset": "BP_Hero"},
        "_relay": {
            "id": entry_id,
            "sender": sender,
            "sender_id": "cid-1",
            "timestamp": "2026-03-31T10:00:00+00:00",
            "restore": restore,
        },
    }


def _write_journal(sessions_dir: Path, filename: str, entries: list[dict]):
    f = sessions_dir / filename
    f.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


# ── Journal loading ────────────────────────────────────────────────────────────

class TestLoadEntries:
    def test_load_basic(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("id-1", "blueprint.set_property"),
            _make_entry("id-2", "level.place_actor"),
        ])
        entries = cl._load_entries()
        assert len(entries) == 2

    def test_filter_by_type(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("id-1", "blueprint.set_property"),
            _make_entry("id-2", "level.place_actor"),
            _make_entry("id-3", "blueprint.compile"),
        ])
        entries = cl._load_entries(type_prefix="blueprint.")
        assert len(entries) == 2
        assert all(e["type"].startswith("blueprint.") for e in entries)

    def test_filter_by_sender(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("id-1", "blueprint.x", sender="agent"),
            _make_entry("id-2", "level.y", sender="vscode"),
        ])
        entries = cl._load_entries(sender="agent")
        assert len(entries) == 1
        assert entries[0]["_relay"]["sender"] == "agent"

    def test_filter_by_date(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-30.jsonl", [_make_entry("id-old", "level.x")])
        _write_journal(tmp_path, "2026-03-31.jsonl", [_make_entry("id-new", "level.y")])

        entries = cl._load_entries(date_filter="2026-03-31")
        assert len(entries) == 1
        assert entries[0]["_relay"]["id"] == "id-new"

    def test_skips_invalid_json(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        f = tmp_path / "2026-03-31.jsonl"
        f.write_text('{"type":"ok","_relay":{"id":"id-ok"}}\nnot json\n', encoding="utf-8")
        entries = cl._load_entries()
        assert len(entries) == 1

    def test_empty_sessions_dir(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path / "nonexistent")
        entries = cl._load_entries()
        assert entries == []

    def test_multi_file_load(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-30.jsonl", [_make_entry("id-a", "ue.cmd")])
        _write_journal(tmp_path, "2026-03-31.jsonl", [_make_entry("id-b", "ue.cmd")])
        entries = cl._load_entries()
        assert len(entries) == 2


class TestFindEntry:
    def test_find_existing(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        target = _make_entry("abc-1234", "blueprint.compile")
        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("other-id", "level.x"),
            target,
        ])
        result = cl._find_entry("abc-1234")
        assert result is not None
        assert result["_relay"]["id"] == "abc-1234"

    def test_find_missing(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [_make_entry("id-1", "level.x")])
        assert cl._find_entry("no-such-id") is None


# ── Bisect marks ───────────────────────────────────────────────────────────────

class TestBisectMarks:
    def test_load_empty(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")
        marks = cl._load_marks()
        assert marks == {"good": None, "bad": None}

    def test_save_and_load(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        cl._save_marks({"good": "aaa", "bad": "bbb"})
        marks = cl._load_marks()
        assert marks["good"] == "aaa"
        assert marks["bad"] == "bbb"

    def test_load_corrupted_returns_empty(self, tmp_path, monkeypatch):
        import shells.cl as cl
        marks_file = tmp_path / "bisect-marks.json"
        marks_file.write_text("not json", encoding="utf-8")
        monkeypatch.setattr(cl, "MARKS_FILE", marks_file)
        marks = cl._load_marks()
        assert marks == {"good": None, "bad": None}


# ── cmd_list ───────────────────────────────────────────────────────────────────

class TestCmdList:
    def test_list_prints_entries(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("id-1", "blueprint.set_property"),
            _make_entry("id-2", "level.place_actor"),
        ])
        args = MagicMock(date=None, type=None, sender=None, n=10)
        cl.cmd_list(args)
        out = capsys.readouterr().out
        assert "id-1" in out or "id-1"[:8] in out
        assert "blueprint.set_property" in out

    def test_list_no_entries(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        args = MagicMock(date=None, type=None, sender=None, n=10)
        cl.cmd_list(args)
        out = capsys.readouterr().out
        assert "No journal entries" in out

    def test_list_n_limit(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        entries = [_make_entry(f"id-{i}", "blueprint.x") for i in range(10)]
        _write_journal(tmp_path, "2026-03-31.jsonl", entries)

        args = MagicMock(date=None, type=None, sender=None, n=3)
        cl.cmd_list(args)
        out = capsys.readouterr().out
        # 3 data rows + header + separator
        lines = [l for l in out.strip().splitlines() if l.strip() and "─" not in l and "ID" not in l]
        assert len(lines) == 3

    def test_list_marks_shown(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        marks_file = tmp_path / "bisect-marks.json"
        monkeypatch.setattr(cl, "MARKS_FILE", marks_file)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("abc-1234-abcd-efgh", "blueprint.x"),
        ])
        cl._save_marks({"good": "abc-1234-abcd-efgh", "bad": None})

        args = MagicMock(date=None, type=None, sender=None, n=10)
        cl.cmd_list(args)
        out = capsys.readouterr().out
        assert "GOOD" in out


# ── cmd_show ───────────────────────────────────────────────────────────────────

class TestCmdShow:
    def test_show_entry(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("show-id-123", "blueprint.compile"),
        ])
        args = MagicMock(id="show-id-123")
        cl.cmd_show(args)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["_relay"]["id"] == "show-id-123"

    def test_show_missing_exits(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        args = MagicMock(id="no-such-id")
        with pytest.raises(SystemExit):
            cl.cmd_show(args)


# ── cmd_mark_good / cmd_mark_bad ───────────────────────────────────────────────

class TestMarkCommands:
    def test_mark_good(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        _write_journal(tmp_path, "2026-03-31.jsonl", [_make_entry("good-id", "level.x")])
        args = MagicMock(id="good-id")
        cl.cmd_mark_good(args)

        marks = cl._load_marks()
        assert marks["good"] == "good-id"

    def test_mark_bad(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        _write_journal(tmp_path, "2026-03-31.jsonl", [_make_entry("bad-id", "level.x")])
        args = MagicMock(id="bad-id")
        cl.cmd_mark_bad(args)

        marks = cl._load_marks()
        assert marks["bad"] == "bad-id"

    def test_mark_good_missing_entry_exits(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        args = MagicMock(id="no-entry")
        with pytest.raises(SystemExit):
            cl.cmd_mark_good(args)

    def test_mark_bad_missing_entry_exits(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        args = MagicMock(id="no-entry")
        with pytest.raises(SystemExit):
            cl.cmd_mark_bad(args)


# ── cmd_marks ──────────────────────────────────────────────────────────────────

class TestCmdMarks:
    def test_marks_none_set(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        args = MagicMock()
        cl.cmd_marks(args)
        out = capsys.readouterr().out
        assert "No bisect marks" in out

    def test_marks_shows_both(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("gid-1", "level.x"),
            _make_entry("bid-2", "level.y"),
        ])
        cl._save_marks({"good": "gid-1", "bad": "bid-2"})

        args = MagicMock()
        cl.cmd_marks(args)
        out = capsys.readouterr().out
        assert "GOOD" in out
        assert "BAD" in out


# ── cmd_bisect ─────────────────────────────────────────────────────────────────

class TestCmdBisect:
    def _setup_journal(self, tmp_path, n_entries=10):
        import shells.cl as cl
        entries = [_make_entry(f"entry-{i:04d}", "blueprint.x") for i in range(n_entries)]
        _write_journal(tmp_path, "2026-03-31.jsonl", entries)
        return entries

    def test_bisect_no_marks_exits(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        args = MagicMock()
        with pytest.raises(SystemExit):
            cl.cmd_bisect(args)

    def test_bisect_shows_midpoint(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        entries = self._setup_journal(tmp_path, n_entries=10)
        # good=0, bad=9 → midpoint should be around 4 or 5
        cl._save_marks({"good": "entry-0000", "bad": "entry-0009"})

        args = MagicMock()
        cl.cmd_bisect(args)
        out = capsys.readouterr().out
        assert "entry-" in out
        assert "Progress" in out

    def test_bisect_adjacent_entries(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        entries = self._setup_journal(tmp_path, n_entries=3)
        # good=0, bad=1 → gap=0, done
        cl._save_marks({"good": "entry-0000", "bad": "entry-0001"})

        args = MagicMock()
        cl.cmd_bisect(args)
        out = capsys.readouterr().out
        assert "complete" in out.lower()

    def test_bisect_good_after_bad_exits(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)
        monkeypatch.setattr(cl, "MARKS_FILE", tmp_path / "bisect-marks.json")

        entries = self._setup_journal(tmp_path, n_entries=5)
        # reversed: good=4, bad=0
        cl._save_marks({"good": "entry-0004", "bad": "entry-0000"})

        args = MagicMock()
        with pytest.raises(SystemExit):
            cl.cmd_bisect(args)


# ── cmd_restore ────────────────────────────────────────────────────────────────

class TestCmdRestore:
    def test_restore_dry_run(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("restore-id-xyz", "blueprint.compile", restore="snapshot"),
        ])
        args = MagicMock(id="restore-id-xyz", url="ws://localhost:8765", dry_run=True)
        cl.cmd_restore(args)
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "changelist.restore" in out
        assert "restore-id-xyz" in out

    def test_restore_dry_run_none_strategy(self, tmp_path, monkeypatch, capsys):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("norestore-id", "relay.welcome", restore="none"),
        ])
        args = MagicMock(id="norestore-id", url="ws://localhost:8765", dry_run=True)
        cl.cmd_restore(args)
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_restore_entry_not_found_exits(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        args = MagicMock(id="ghost-id", url="ws://localhost:8765", dry_run=False)
        with pytest.raises(SystemExit):
            cl.cmd_restore(args)

    def test_restore_live_connection_refused(self, tmp_path, monkeypatch):
        import shells.cl as cl
        monkeypatch.setattr(cl, "SESSIONS_DIR", tmp_path)

        _write_journal(tmp_path, "2026-03-31.jsonl", [
            _make_entry("live-id", "blueprint.compile", restore="snapshot"),
        ])
        args = MagicMock(id="live-id", url="ws://localhost:19999", dry_run=False)
        with pytest.raises(SystemExit):
            cl.cmd_restore(args)
