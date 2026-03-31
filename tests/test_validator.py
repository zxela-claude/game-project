"""
Tests for the 4-Gate Validator service (NAN-157).
All tests run without a live relay.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from validator import (
    CONTRACTS_DIR,
    gate1_schema,
    gate2_blueprint,
    gate3_cpp_build,
    gate4_smoke,
    validate,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _relay_wrap(msg: dict, sender: str = "test", id_: str = "abc123") -> dict:
    return {**msg, "_relay": {"id": id_, "sender": sender}}


def _write_contract(name: str, content: dict):
    """Write a temporary contract file. Returns path."""
    path = CONTRACTS_DIR / f"{name}.json"
    path.write_text(json.dumps(content))
    return path


def _remove_contract(name: str):
    path = CONTRACTS_DIR / f"{name}.json"
    if path.exists():
        path.unlink()


# ── Gate 1: Schema ─────────────────────────────────────────────────────────────

class TestGate1Schema:
    def test_no_contract_passes(self):
        _remove_contract("unknown_command")
        msg = {"type": "unknown.command"}
        r = gate1_schema(msg)
        assert r["pass"] is True
        assert "no contract" in r["reason"]

    def test_valid_payload_passes(self):
        _write_contract("blueprint_set_property", {
            "required": ["target", "property", "value"],
            "fields": {"target": "string", "value": "number"},
        })
        msg = {"type": "blueprint.set_property", "target": "BP_Actor", "property": "Speed", "value": 300}
        r = gate1_schema(msg)
        assert r["pass"] is True
        _remove_contract("blueprint_set_property")

    def test_missing_required_field_fails(self):
        _write_contract("blueprint_set_property", {
            "required": ["target", "property", "value"],
        })
        msg = {"type": "blueprint.set_property", "target": "BP_Actor"}
        r = gate1_schema(msg)
        assert r["pass"] is False
        assert "property" in r["reason"] or "value" in r["reason"]
        _remove_contract("blueprint_set_property")

    def test_wrong_field_type_fails(self):
        _write_contract("blueprint_set_property", {
            "required": ["target"],
            "fields": {"target": "string"},
        })
        msg = {"type": "blueprint.set_property", "target": 123}
        r = gate1_schema(msg)
        assert r["pass"] is False
        assert "target" in r["reason"]
        _remove_contract("blueprint_set_property")

    def test_array_type_validation(self):
        _write_contract("test_array", {
            "required": ["items"],
            "fields": {"items": "array"},
        })
        msg = {"type": "test.array", "items": [1, 2, 3]}
        r = gate1_schema(msg)
        assert r["pass"] is True
        _remove_contract("test_array")

    def test_boolean_type_validation_fails(self):
        _write_contract("test_bool", {
            "required": ["flag"],
            "fields": {"flag": "boolean"},
        })
        msg = {"type": "test.bool", "flag": "yes"}
        r = gate1_schema(msg)
        assert r["pass"] is False
        _remove_contract("test_bool")


# ── Gate 2: Blueprint ─────────────────────────────────────────────────────────

class TestGate2Blueprint:
    def test_relay_internal_skipped(self):
        msg = {"type": "relay.hello"}
        r = gate2_blueprint(msg)
        assert r["pass"] is True
        assert "skipped" in r["reason"]

    def test_blueprint_command_with_target_passes(self):
        msg = {"type": "blueprint.set_property", "target": "BP_Enemy", "property": "Speed"}
        r = gate2_blueprint(msg)
        assert r["pass"] is True

    def test_blueprint_command_missing_target_fails(self):
        msg = {"type": "blueprint.set_property", "property": "Speed", "value": 100}
        r = gate2_blueprint(msg)
        assert r["pass"] is False
        assert "target" in r["reason"] or "actor" in r["reason"]

    def test_blueprint_command_empty_target_fails(self):
        msg = {"type": "blueprint.add_node", "target": "   "}
        r = gate2_blueprint(msg)
        assert r["pass"] is False

    def test_blueprint_compile_with_blueprint_field(self):
        msg = {"type": "blueprint.compile", "blueprint": "BP_HUD"}
        r = gate2_blueprint(msg)
        assert r["pass"] is True

    def test_level_place_actor_with_valid_class(self):
        msg = {"type": "level.place_actor", "actor_class": "BP_Tree", "location": [0, 0, 0]}
        r = gate2_blueprint(msg)
        assert r["pass"] is True

    def test_level_place_actor_with_invalid_class_fails(self):
        msg = {"type": "level.place_actor", "actor_class": ""}
        r = gate2_blueprint(msg)
        assert r["pass"] is False

    def test_schema_updated_skipped(self):
        msg = {"type": "schema.updated", "action": "add"}
        r = gate2_blueprint(msg)
        assert r["pass"] is True
        assert "skipped" in r["reason"]

    def test_non_blueprint_command_passes(self):
        msg = {"type": "level.move_actor", "actor": "SomeActor"}
        r = gate2_blueprint(msg)
        assert r["pass"] is True


# ── Gate 3: C++ Build ─────────────────────────────────────────────────────────

class TestGate3CppBuild:
    def test_low_risk_command_skipped(self):
        msg = {"type": "blueprint.set_property"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is True
        assert "skipped" in r["reason"]

    def test_build_run_valid(self):
        msg = {"type": "build.run", "target": "MyGame", "config": "Development"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is True

    def test_build_run_invalid_config_fails(self):
        msg = {"type": "build.run", "target": "MyGame", "config": "Banana"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is False
        assert "config" in r["reason"]

    def test_build_run_missing_target_fails(self):
        msg = {"type": "build.run", "config": "Development"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is False
        assert "target" in r["reason"]

    def test_build_run_all_valid_configs(self):
        for config in ["Debug", "Development", "Shipping", "Test", "DebugGame"]:
            msg = {"type": "build.run", "target": "MyGame", "config": config}
            r = gate3_cpp_build(msg)
            assert r["pass"] is True, f"config {config} should pass"

    def test_blueprint_compile_with_target_passes(self):
        msg = {"type": "blueprint.compile", "blueprint": "BP_Character"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is True

    def test_blueprint_compile_missing_blueprint_fails(self):
        msg = {"type": "blueprint.compile"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is False

    def test_contract_update_with_valid_cpp_type(self):
        msg = {"type": "contract.update", "name": "PlayerState", "cpp_type": "AMyPlayerState"}
        r = gate3_cpp_build(msg)
        assert r["pass"] is True

    def test_contract_update_with_invalid_cpp_type_fails(self):
        msg = {"type": "contract.update", "cpp_type": 123}
        r = gate3_cpp_build(msg)
        assert r["pass"] is False


# ── Gate 4: Smoke ─────────────────────────────────────────────────────────────

class TestGate4Smoke:
    def test_low_risk_command_skipped(self):
        msg = {"type": "level.move_actor"}
        r = gate4_smoke(msg)
        assert r["pass"] is True
        assert "skipped" in r["reason"]

    def test_level_save_valid_path_passes(self):
        msg = {"type": "level.save", "level": "/Game/Maps/MainLevel"}
        r = gate4_smoke(msg)
        assert r["pass"] is True

    def test_level_save_missing_level_fails(self):
        msg = {"type": "level.save"}
        r = gate4_smoke(msg)
        assert r["pass"] is False
        assert "level" in r["reason"]

    def test_level_save_non_game_path_warns(self):
        msg = {"type": "level.save", "level": "SomeLevel"}
        r = gate4_smoke(msg)
        assert r["pass"] is False
        assert "/Game" in r["reason"]

    def test_schema_migrate_with_version_passes(self):
        msg = {"type": "schema.migrate", "to_version": "2"}
        r = gate4_smoke(msg)
        assert r["pass"] is True

    def test_schema_migrate_missing_version_fails(self):
        msg = {"type": "schema.migrate"}
        r = gate4_smoke(msg)
        assert r["pass"] is False
        assert "to_version" in r["reason"]

    def test_build_run_passes_smoke(self):
        msg = {"type": "build.run", "target": "MyGame"}
        r = gate4_smoke(msg)
        assert r["pass"] is True


# ── Full validate() orchestration ─────────────────────────────────────────────

class TestValidateOrchestration:
    def test_clean_command_passes_all_gates(self):
        msg = _relay_wrap({
            "type": "blueprint.set_property",
            "target": "BP_Hero",
            "property": "Health",
            "value": 100,
        })
        result = validate(msg)
        assert result["pass"] is True
        assert result["failed_gate"] is None
        assert len(result["gates"]) == 4
        for key in ["gate1", "gate2", "gate3", "gate4"]:
            assert key in result["gates"]

    def test_result_has_required_fields(self):
        msg = _relay_wrap({"type": "relay.hello"})
        result = validate(msg)
        assert "type" in result
        assert result["type"] == "validator.result"
        assert "command_id" in result
        assert "command_type" in result
        assert "pass" in result
        assert "gates" in result
        assert "ts" in result

    def test_command_id_from_relay_meta(self):
        msg = _relay_wrap({"type": "blueprint.set_property", "target": "BP"}, id_="xyz999")
        result = validate(msg)
        assert result["command_id"] == "xyz999"

    def test_gate1_failure_stops_at_gate1(self):
        # Write contract that will fail
        _write_contract("blueprint_compile", {
            "required": ["blueprint", "force_recompile"],
        })
        msg = _relay_wrap({"type": "blueprint.compile", "blueprint": "BP_A"})
        result = validate(msg)
        assert result["failed_gate"] == 1
        assert result["gates"]["gate1"]["pass"] is False
        assert result["gates"]["gate2"]["pass"] is False  # skipped
        assert "skipped" in result["gates"]["gate2"]["reason"]
        _remove_contract("blueprint_compile")

    def test_gate2_failure_stops_at_gate2(self):
        # No contract for this type, so gate1 passes
        # Missing 'target' triggers gate2 failure
        msg = _relay_wrap({"type": "blueprint.set_property", "property": "X"})
        result = validate(msg)
        assert result["failed_gate"] == 2
        assert result["gates"]["gate1"]["pass"] is True
        assert result["gates"]["gate2"]["pass"] is False
        assert result["gates"]["gate3"]["pass"] is False  # skipped
        assert result["gates"]["gate4"]["pass"] is False  # skipped

    def test_gate3_failure_stops_at_gate3(self):
        msg = _relay_wrap({
            "type": "build.run",
            "target": "MyGame",
            "config": "INVALID_CONFIG",
        })
        result = validate(msg)
        assert result["failed_gate"] == 3
        assert result["gates"]["gate1"]["pass"] is True
        assert result["gates"]["gate2"]["pass"] is True
        assert result["gates"]["gate3"]["pass"] is False

    def test_gate4_failure_stops_at_gate4(self):
        msg = _relay_wrap({"type": "level.save"})  # missing 'level' field
        result = validate(msg)
        assert result["failed_gate"] == 4
        assert result["gates"]["gate1"]["pass"] is True
        assert result["gates"]["gate4"]["pass"] is False

    def test_schema_migrate_all_gates(self):
        msg = _relay_wrap({"type": "schema.migrate", "to_version": "3"})
        result = validate(msg)
        assert result["pass"] is True

    def test_relay_internal_passes_cleanly(self):
        msg = _relay_wrap({"type": "relay.hello"})
        result = validate(msg)
        assert result["pass"] is True
        assert result["command_type"] == "relay.hello"

    def test_unknown_command_passes_no_contract(self):
        msg = _relay_wrap({"type": "custom.do_something", "data": "hello"})
        result = validate(msg)
        assert result["pass"] is True


# ── ValidatorService structure ────────────────────────────────────────────────

class TestValidatorServiceStructure:
    def test_import(self):
        from validator import ValidatorService
        svc = ValidatorService("ws://localhost:8765")
        assert svc.url == "ws://localhost:8765"

    def test_dry_run_produces_valid_result(self):
        """Simulate the --dry-run sample command."""
        msg = _relay_wrap({
            "type": "blueprint.set_property",
            "target": "BP_PlayerCharacter",
            "property": "MaxWalkSpeed",
            "value": 600,
        }, id_="dryrun-001")
        result = validate(msg)
        assert result["pass"] is True
        assert result["command_id"] == "dryrun-001"
