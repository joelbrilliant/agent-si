"""`brain validate packet --instance` gives the verdict `brain apply` gives: the check is the applier's own decision
with a rollback in place of the commit, so the two cannot disagree."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path

import pytest

from brain import store
from brain.applier import Applier, Unreadable
from brain.cli import main
from brain.init import init_instance
from tests.conftest import config_with, make_packet, ro_rows, source_op, upsert_op

AVERY = {"person_id": "per-avery", "display_name": "Avery", "names": ["Avery"]}
ATTRIBUTED_NOTE = "Next: open the stall once Avery approves the plan."
EVIDENCE_REFUSAL = 'attributes words or a decision to Avery'
NOTE_CAP_REFUSAL = "body is a note and is at most 500 characters"


@pytest.fixture
def avery(tmp_path: Path) -> Path:
    return Path(init_instance("demo", tmp_path, config_text=config_with(principal=AVERY))["instance"])


def _record_packet(body: str, *, agent: str = "tester", change_id: str | None = None) -> dict:
    return make_packet(
        agent,
        source_op("src-dr", "Notes from the working party. Nothing here about approvals."),
        upsert_op("upsert_record", {"id": "rec-dr", "kind": "note", "title": "Status note", "body": body}, "src-dr"),
        change_id=change_id,
    )


def _bytes(packet: dict) -> bytes:
    return json.dumps(packet).encode()


def _snapshot(instance: Path) -> dict:
    """Everything a dry run could leave behind: the database bytes, the folder's files and the change counts."""
    return {
        "db": hashlib.sha256((instance / "brain.sqlite3").read_bytes()).hexdigest(),
        "files": sorted(path.name for path in instance.iterdir()),
        "log": ro_rows(instance, "SELECT count(*) FROM write_log")[0][0],
        "records": ro_rows(instance, "SELECT count(*) FROM records")[0][0],
        "sources": ro_rows(instance, "SELECT count(*) FROM sources")[0][0],
    }


def _verdict(outcome) -> tuple:
    receipt = outcome.receipt
    return receipt["status"], receipt["error"], receipt["targets"], outcome.exit_code


def _dry_then_real(instance: Path, packet: dict):
    raw = _bytes(packet)
    before = _snapshot(instance)
    applier = Applier(instance)
    try:
        dry = applier.apply(raw, dry_run=True)
        assert _snapshot(instance) == before, "a dry run left something behind"
        real = applier.apply(raw)
    finally:
        applier.close()
    return dry, real


@pytest.mark.parametrize(
    "body, message",
    [(ATTRIBUTED_NOTE, EVIDENCE_REFUSAL), ("x" * 501, NOTE_CAP_REFUSAL)],
    ids=["evidence-gate-attribution", "note-cap-500"],
)
def test_a_refused_packet_is_refused_by_the_dry_run_with_the_same_message(avery, body, message) -> None:
    dry, real = _dry_then_real(avery, _record_packet(body))
    assert dry.receipt["status"] == "rejected" and dry.exit_code == 1 and dry.dry_run
    assert message in dry.receipt["error"]
    assert _verdict(dry) == _verdict(real)
    assert not real.dry_run


def test_a_clean_packet_is_applied_by_a_dry_run_without_using_up_its_change_id(avery) -> None:
    packet = _record_packet("A short clean note.")
    dry, real = _dry_then_real(avery, packet)
    assert dry.receipt["status"] == "applied" and dry.exit_code == 0 and dry.dry_run
    assert dry.receipt["targets"], "the dry run names what apply would write"
    assert _verdict(dry) == _verdict(real)
    assert real.receipt["status"] == "applied" and not real.duplicate
    # The same change_id was free, so the receipt carries the sequence number the dry run predicted.
    assert dry.receipt["applied_seq"] == real.receipt["applied_seq"]
    assert dry.receipt["payload_sha256"] == real.receipt["payload_sha256"]
    assert ro_rows(avery, "SELECT body FROM records WHERE id = 'rec-dr'") == [("A short clean note.",)]


def test_a_duplicate_change_id_reports_the_stored_receipt_and_changes_nothing(avery) -> None:
    packet = _record_packet("A short clean note.")
    applier = Applier(avery)
    try:
        first = applier.apply(_bytes(packet))
        before = _snapshot(avery)
        dry = applier.apply(_bytes(packet), dry_run=True)
        again = applier.apply(_bytes(packet))
        different = applier.apply(_bytes(_record_packet("Another body.", change_id=packet["change_id"])), dry_run=True)
    finally:
        applier.close()
    assert dry.duplicate and again.duplicate
    assert dry.receipt == again.receipt == first.receipt
    assert dry.exit_code == again.exit_code == 0
    assert "different payload" in different.warning and different.receipt == first.receipt
    assert _snapshot(avery) == before


def test_a_conflict_and_a_held_packet_get_apply_exit_codes_from_the_dry_run(avery) -> None:
    applier = Applier(avery)
    try:
        applier.apply(_bytes(make_packet(
            "tester",
            source_op("src-p", "People notes."),
            upsert_op("upsert_person", {"id": "per-extra", "display_name": "Extra Person"}, "src-p"),
        )))
    finally:
        applier.close()
    clash = make_packet("tester", upsert_op("upsert_person", {"id": "per-extra", "display_name": "Other"}, "src-p"))
    waits = _record_packet("A short clean note.", agent="careful")
    for packet, status, code in ((clash, "conflict", 3), (waits, "held", 4)):
        dry, real = _dry_then_real(avery, packet)
        assert (dry.receipt["status"], dry.exit_code) == (status, code)
        assert _verdict(dry) == _verdict(real)
        assert dry.warning == real.warning


def test_a_dry_run_runs_no_integrity_check_and_writes_no_pause_file(avery, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("a dry run must not run the post-commit check")

    monkeypatch.setattr(store, "integrity_problems", forbidden)
    applier = Applier(avery)
    try:
        before = _snapshot(avery)
        assert applier.apply(_bytes(_record_packet("A short clean note.")), dry_run=True).exit_code == 0
        assert _snapshot(avery) == before
    finally:
        applier.close()
    assert not (avery / "PAUSED").exists()


def test_an_unreadable_packet_raises_the_same_error_on_a_dry_run(avery) -> None:
    applier = Applier(avery)
    try:
        with pytest.raises(Unreadable) as real:
            applier.apply(b"{")
        with pytest.raises(Unreadable) as dry:
            applier.apply(b"{", dry_run=True)
    finally:
        applier.close()
    assert str(dry.value) == str(real.value)


# -- brain validate packet FILE --instance DIR ------------------------------------------------------------------


def _cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(part) for part in argv])
    return code, out.getvalue(), err.getvalue()


def _file(directory: Path, packet: dict) -> Path:
    path = directory / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path


def test_validate_with_an_instance_gives_the_verdict_apply_gives(avery, tmp_path) -> None:
    cases = (
        (_record_packet(ATTRIBUTED_NOTE), "rejected", 1, EVIDENCE_REFUSAL),
        (_record_packet("x" * 501), "rejected", 1, NOTE_CAP_REFUSAL),
        (_record_packet("A short clean note."), "applied", 0, ""),
        (_record_packet("A short clean note.", agent="careful"), "held", 4, ""),
    )
    for packet, status, code, message in cases:
        path = _file(tmp_path, packet)
        before = _snapshot(avery)
        got, out, _err = _cli("validate", "packet", path, "--instance", avery)
        report = json.loads(out)
        assert (report["status"], got) == (status, code), out
        assert message in report["error"]
        assert report["errors"] == ([{"path": "", "message": report["error"]}] if report["error"] else [])
        assert report["kind"] == "packet" and report["dry_run"] is True and report["change_id"] == packet["change_id"]
        assert _snapshot(avery) == before
        applied, real, _ = _cli("apply", path, "--instance", avery)
        assert applied == got
        assert json.loads(real)["status"] == status
        assert json.loads(real)["error"] == report["error"]
        assert json.loads(real)["targets"] == report["targets"]


def test_validate_with_an_instance_reports_a_duplicate_and_an_unreadable_packet(avery, tmp_path) -> None:
    path = _file(tmp_path, _record_packet("A short clean note."))
    assert _cli("apply", path, "--instance", avery)[0] == 0
    code, out, _ = _cli("validate", "packet", path, "--instance", avery)
    assert code == 0 and json.loads(out)["duplicate"] is True
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    code, out, _ = _cli("validate", "packet", bad, "--instance", avery)
    report = json.loads(out)
    assert code == 2 and report["status"] == "unreadable" and report["error"] == "packet is not valid JSON"


def test_validate_with_an_instance_refuses_a_paused_instance_as_apply_does(avery, tmp_path) -> None:
    (avery / "PAUSED").write_text("{}\n", encoding="utf-8")
    path = _file(tmp_path, _record_packet("A short clean note."))
    code, out, err = _cli("validate", "packet", path, "--instance", avery)
    assert code == 5 and out == "" and "paused" in err
    assert code == _cli("apply", path, "--instance", avery)[0]


def test_validate_with_an_instance_is_for_packets_only(avery, tmp_path) -> None:
    path = tmp_path / "candidate.json"
    path.write_text("{}", encoding="utf-8")
    code, _out, err = _cli("validate", "candidate", path, "--instance", avery)
    assert code == 2 and "packet" in err


def test_validate_without_an_instance_says_it_checked_the_shape_only(avery, tmp_path) -> None:
    path = _file(tmp_path, _record_packet(ATTRIBUTED_NOTE))
    code, out, _ = _cli("validate", "packet", path)
    report = json.loads(out)
    assert code == 0 and report["status"] == "valid"
    assert "shape only" in report["checked"] and "--instance" in report["checked"]
    assert "dry_run" not in report
