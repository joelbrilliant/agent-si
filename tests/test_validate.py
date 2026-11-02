from __future__ import annotations

import json
import subprocess
from pathlib import Path

from brain.validate import validate_candidate, validate_command, validate_packet
from tests.conftest import make_packet, source_op, upsert_op


def _valid_packet() -> dict:
    return make_packet(
        "tester",
        source_op("src-a", "Valid source content for validation."),
        upsert_op(
            "upsert_record",
            {"id": "rec-a", "kind": "note", "title": "Title", "body": "Body text"},
            "src-a",
        ),
    )


def test_valid_packet_passes() -> None:
    assert validate_packet(_valid_packet()) == []


def test_unknown_top_level_field_is_held() -> None:
    packet = _valid_packet()
    packet["extra"] = True
    errors = validate_packet(packet)
    assert any(error["path"] == "/extra" for error in errors)


def test_change_id_prefix_must_match_agent() -> None:
    packet = _valid_packet()
    packet["change_id"] = "other-abc123def456"
    errors = validate_packet(packet)
    assert any(error["path"] == "/change_id" for error in errors)


def test_unknown_op_is_held() -> None:
    packet = _valid_packet()
    packet["ops"].append({"op": "explode", "row": {}})
    errors = validate_packet(packet)
    assert any(error["path"].endswith("/op") for error in errors)


def test_base_is_optional_but_must_be_a_version_when_sent() -> None:
    op = {"op": "upsert_person", "row": {"id": "per-a", "display_name": "A"}, "source": {"source_id": "src-a", "locator": "x"}}
    assert validate_packet(make_packet("tester", op)) == []
    assert validate_packet(make_packet("tester", {**op, "base": None})) == []
    assert validate_packet(make_packet("tester", {**op, "base": 3})) == []
    for bad in (0, "1"):
        errors = validate_packet(make_packet("tester", {**op, "base": bad}))
        assert [error["path"] for error in errors] == ["/ops/0/base"], bad


def test_unknown_row_field_is_held() -> None:
    packet = _valid_packet()
    packet["ops"][1]["row"]["surprise"] = "no"
    errors = validate_packet(packet)
    assert any(error["path"].endswith("/surprise") for error in errors)


def test_errors_carry_json_pointer_paths() -> None:
    packet = _valid_packet()
    packet["ops"][1]["row"]["surprise"] = "no"
    errors = validate_packet(packet)
    assert all(error["path"].startswith("/") or error["path"] == "" for error in errors)


def test_booleans_are_not_integers() -> None:
    packet = make_packet(
        "tester",
        source_op("src-a", "Valid source content for validation."),
        upsert_op(
            "upsert_thread",
            {
                "id": "thr-a",
                "channel": "email",
                "account": "work-mail",
                "external_id": "t1",
                "outbound_seen": 1,
            },
            "src-a",
        ),
    )
    errors = validate_packet(packet)
    assert errors


def test_decision_quote_needs_five_words() -> None:
    packet = make_packet(
        "tester",
        upsert_op(
            "upsert_decision",
            {
                "id": "dec-a",
                "title": "Decide now please",
                "quote": "too few words",
                "decided_on": "2026-10-01",
                "decided_by_person_id": "per-dana-okafor",
                "status": "proposed",
                "project_id": "general",
            },
            "src-a",
        ),
    )
    errors = validate_packet(packet)
    assert any("words" in error["message"] for error in errors)


def test_unparseable_json_is_held() -> None:
    code, payload = validate_command("packet", b"not json")
    assert code == 1
    assert payload["status"] == "held"
    assert payload["errors"][0]["path"] == ""


def test_candidate_envelope_errors_are_held() -> None:
    errors = validate_candidate({"candidate_version": 2})
    assert errors


def test_unknown_candidate_kind_is_held() -> None:
    errors = validate_candidate(
        {
            "candidate_version": 1,
            "kind": "meeting_candidate",
            "source_id": "src-a",
            "locator": "para 1",
            "quote": "at least five words copied here",
            "fields": {},
        }
    )
    assert any(error["path"] == "/kind" for error in errors)


def test_validate_command_exit_codes(tmp_path: Path) -> None:
    good = tmp_path / "ok.json"
    good.write_text(json.dumps(_valid_packet()))
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    ok = subprocess.run(
        ["brain", "validate", "packet", str(good)], capture_output=True, text=True, timeout=30
    )
    held = subprocess.run(
        ["brain", "validate", "packet", str(bad)], capture_output=True, text=True, timeout=30
    )
    usage = subprocess.run(
        ["brain", "validate"], capture_output=True, text=True, timeout=30
    )
    assert ok.returncode == 0
    assert held.returncode == 1
    assert usage.returncode == 2


def test_validator_reads_no_config_and_creates_no_files(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    validate_packet(_valid_packet())
    validate_candidate({"kind": "x"})
    assert set(tmp_path.iterdir()) == before
