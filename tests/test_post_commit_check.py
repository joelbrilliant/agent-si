from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from brain import store
from brain.applier import Applier, Outcome
from brain.init import init_instance, search_rebuild
from brain.store import connect_reader
from tests.conftest import (
    TEST_CONFIG,
    context_connection,
    make_packet,
    ro_rows,
    source_op,
    upsert_op,
)


def test_small_store_runs_the_full_check_after_every_commit(instance, apply, monkeypatch) -> None:
    calls: list[list[str]] = []
    original = store.integrity_problems

    def spy(con):
        result = original(con)
        calls.append(result)
        return result

    monkeypatch.setattr(store, "integrity_problems", spy)
    applied = apply(make_packet("tester", source_op("src-full", "A source that applies.")))
    rejected = apply(make_packet("nobody", source_op("src-nope", "An unknown agent.")))
    conflict = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-full",
                    "title": "Full check",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-full",
                base=99,
            ),
        )
    )
    held = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-held",
                    "title": "Needs approval",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-full",
            ),
        )
    )
    writer = Applier(instance)
    try:
        approved = writer.approve(held.receipt["change_id"], "tester")
        held_again = writer.apply(
            json.dumps(
                make_packet(
                    "careful",
                    upsert_op(
                        "upsert_action",
                        {
                            "id": "act-held-2",
                            "title": "Second hold",
                            "status": "open",
                            "status_basis": "observed",
                            "project_id": "general",
                            "last_verified_at": "2026-10-01T00:00:00Z",
                        },
                        "src-full",
                    ),
                )
            ).encode()
        )
        refused = writer.refuse(held_again.receipt["change_id"], "tester", "no")
    finally:
        writer.close()
    decisions = (applied, rejected, conflict, held, approved, held_again, refused)
    assert [item.receipt["status"] for item in decisions] == [
        "applied",
        "rejected",
        "conflict",
        "held",
        "applied",
        "held",
        "rejected",
    ]
    for outcome in decisions:
        assert outcome.check == "full"
        assert outcome.check_ms >= 0
    assert len(calls) == len(decisions)


def test_large_store_runs_the_targeted_check_between_full_checks(instance, apply, monkeypatch) -> None:
    monkeypatch.setattr("brain.applier.FULL_CHECK_MAX_BYTES", 0)
    monkeypatch.setattr("brain.applier.FULL_CHECK_EVERY", 5)
    calls: list[list[str]] = []
    original = store.integrity_problems

    def spy(con):
        result = original(con)
        calls.append(result)
        return result

    monkeypatch.setattr(store, "integrity_problems", spy)
    outcomes = [
        apply(make_packet("tester", source_op(f"src-cadence-{i}", f"Cadence source {i}.")))
        for i in range(12)
    ]
    full = 0
    for outcome in outcomes:
        seq = _rowid(instance, outcome.receipt["change_id"])
        expect = "full" if seq % 5 == 0 else "targeted"
        assert outcome.check == expect
        if expect == "full":
            full += 1
    assert full >= 2
    assert { _rowid(instance, item.receipt["change_id"]) for item in outcomes if item.check == "full" } >= {5, 10}
    assert len(calls) == full


def test_cadence_uses_the_decisions_own_rowid(instance, apply, monkeypatch) -> None:
    monkeypatch.setattr("brain.applier.FULL_CHECK_MAX_BYTES", 0)
    monkeypatch.setattr("brain.applier.FULL_CHECK_EVERY", 5)
    calls: list[list[str]] = []
    original_check = store.integrity_problems

    def spy(con):
        result = original_check(con)
        calls.append(result)
        return result

    monkeypatch.setattr(store, "integrity_problems", spy)
    original = Applier._finish
    state: dict = {"fired": False, "interleaved": None}

    def wrapped(self, change_id: str):
        if not state["fired"]:
            seq = self.con.execute(
                "SELECT rowid FROM write_log WHERE change_id = ?", (change_id,)
            ).fetchone()[0]
            if seq % 5 == 0:
                state["fired"] = True
                other = Applier(instance)
                try:
                    state["interleaved"] = other.apply(
                        json.dumps(
                            make_packet(
                                "tester",
                                source_op("src-interleave", "Committed between the decision and its check."),
                            )
                        ).encode()
                    )
                finally:
                    other.close()
        return original(self, change_id)

    monkeypatch.setattr(Applier, "_finish", wrapped)
    due = None
    for i in range(8):
        outcome = apply(make_packet("tester", source_op(f"src-own-{i}", f"Own rowid source {i}.")))
        if state["fired"]:
            due = outcome
            break
    assert due is not None
    assert state["interleaved"] is not None
    due_row = _rowid(instance, due.receipt["change_id"])
    other_row = _rowid(instance, state["interleaved"].receipt["change_id"])
    newest = ro_rows(instance, "SELECT max(rowid) FROM write_log")[0][0]
    assert due_row % 5 == 0
    assert newest > due_row
    assert due.check == "full"
    assert state["interleaved"].check == "targeted"
    assert other_row != due_row
    assert len(calls) == 1


def test_targeted_check_still_pauses_on_a_missing_guard_and_search_drift(
    instance, apply, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("brain.applier.FULL_CHECK_MAX_BYTES", 0)
    parent = tmp_path / "drift"
    parent.mkdir()
    other = Path(init_instance("demo", parent, config_text=TEST_CONFIG)["instance"])
    original = Applier._finish

    def drop_guard(self, change_id: str):
        con = context_connection(instance)
        try:
            con.execute("DROP TRIGGER guard_sources_update")
        finally:
            con.close()
        return original(self, change_id)

    monkeypatch.setattr(Applier, "_finish", drop_guard)
    paused = apply(make_packet("tester", source_op("src-drop-guard", "Drops a guard after commit.")))
    assert paused.check == "targeted"
    assert paused.exit_code == 5
    assert "missing guard guard_sources_update" in (instance / "PAUSED").read_text()

    def tamper(self, change_id: str):
        self.con.execute("UPDATE search_index SET title = 'tampered' WHERE ref_id = 'rec-drift'")
        return original(self, change_id)

    monkeypatch.setattr(Applier, "_finish", tamper)
    writer = Applier(other)
    try:
        drifted = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    source_op("src-drift", "A source cited by the drifted record."),
                    upsert_op(
                        "upsert_record",
                        {
                            "id": "rec-drift",
                            "kind": "note",
                            "title": "Before",
                            "body": "Body text for the drift check.",
                        },
                        "src-drift",
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert drifted.check == "targeted"
    assert drifted.exit_code == 5
    assert "search index out of sync for record rec-drift" in (other / "PAUSED").read_text()


def test_rebuild_and_brain_check_always_run_the_full_check(instance, monkeypatch) -> None:
    monkeypatch.setattr("brain.applier.FULL_CHECK_MAX_BYTES", 0)
    rebuilt = search_rebuild(instance, "manual")
    assert rebuilt.check == "full"
    calls: list[int] = []
    original = store.integrity_problems

    def spy(con):
        calls.append(1)
        return original(con)

    monkeypatch.setattr(store, "integrity_problems", spy)
    assert store.check_instance(instance) == []
    assert len(calls) == 1


def test_full_check_finds_damage_on_its_cadence(instance, apply, monkeypatch) -> None:
    monkeypatch.setattr("brain.applier.FULL_CHECK_MAX_BYTES", 0)
    monkeypatch.setattr("brain.applier.FULL_CHECK_EVERY", 3)
    monkeypatch.setattr(store, "integrity_problems", lambda con: ["synthetic damage"])
    seen: list[Outcome] = []
    for i in range(6):
        outcome = apply(make_packet("tester", source_op(f"src-damage-{i}", f"Damage cadence {i}.")))
        seen.append(outcome)
        if outcome.exit_code == 5:
            break
    paused = seen[-1]
    assert paused.exit_code == 5
    assert _rowid(instance, paused.receipt["change_id"]) % 3 == 0
    assert seen[:-1]
    for outcome in seen[:-1]:
        assert outcome.exit_code != 5
        assert _rowid(instance, outcome.receipt["change_id"]) % 3 != 0


def test_post_commit_check_stays_within_budget(instance, apply, monkeypatch) -> None:
    from brain import applier as applier_mod

    content = "x" * 450_000
    for batch in range(4):
        ops = [
            source_op(f"src-bulk-{batch}-{i}", content)
            for i in range(10)
        ]
        outcome = apply(make_packet("tester", *ops, intent=f"bulk {batch}"))
        assert outcome.receipt["status"] == "applied", outcome.receipt
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        size = con.execute("PRAGMA page_count").fetchone()[0] * con.execute("PRAGMA page_size").fetchone()[0]
    finally:
        con.close()
    assert size > applier_mod.FULL_CHECK_MAX_BYTES
    targeted = apply(make_packet("tester", source_op("src-targeted", "A small targeted commit.")))
    assert targeted.check == "targeted"
    assert targeted.check_ms < 100
    monkeypatch.setattr(applier_mod, "FULL_CHECK_EVERY", 1)
    full = apply(make_packet("tester", source_op("src-full-budget", "A full check on the large store.")))
    assert full.check == "full"
    assert full.check_ms < applier_mod.CHECK_BUDGET_MS


def test_slow_check_is_reported_on_stderr(instance, tmp_path, monkeypatch, capsys) -> None:
    from brain import cli
    from brain.applier import receipt_from_row

    monkeypatch.setattr("brain.applier.CHECK_BUDGET_MS", -1)
    packet = make_packet("tester", source_op("src-slow", "Reports the budget note."))
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    code = cli.main(["apply", str(path), "--instance", str(instance)])
    captured = capsys.readouterr()
    assert code == 0
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT * FROM write_log WHERE change_id = ?", (packet["change_id"],)
        ).fetchone()
    finally:
        con.close()
    receipt = receipt_from_row(row)
    assert captured.out == json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n"
    assert re.fullmatch(
        r"brain: post-commit check took \d+ ms \(budget 1000 ms\); "
        r"run brain check when the store is idle\n",
        captured.err,
    )


def test_read_only_check_never_takes_the_write_lock(instance, monkeypatch) -> None:
    db = instance / "brain.sqlite3"
    reader = connect_reader(db)
    traced: list[str] = []
    reader.set_trace_callback(traced.append)
    try:
        assert store.integrity_problems(reader) == []
    finally:
        reader.close()
    assert not any(statement.lstrip().upper().startswith("BEGIN") for statement in traced)

    statements: list[str] = []
    original = store.connect_readonly

    def wrapped(path):
        con = original(path)
        con.set_trace_callback(statements.append)
        return con

    monkeypatch.setattr(store, "connect_readonly", wrapped)
    assert store.check_instance(instance) == []
    assert not any("BEGIN IMMEDIATE" in statement.upper() for statement in statements)

    holder = sqlite3.connect(db, timeout=5)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert store.check_instance(instance) == []
        assert holder.in_transaction
        probe = sqlite3.connect(db, timeout=0.2)
        try:
            with pytest.raises(sqlite3.OperationalError):
                probe.execute("BEGIN IMMEDIATE")
        finally:
            probe.close()
    finally:
        holder.rollback()
        holder.close()


def _rowid(instance: Path, change_id: str) -> int:
    return ro_rows(instance, "SELECT rowid FROM write_log WHERE change_id = ?", (change_id,))[0][0]
