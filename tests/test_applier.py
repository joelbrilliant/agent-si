from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from brain.applier import Applier, Unavailable, Unreadable
from brain.init import apply_config
from tests.conftest import (
    TEST_CONFIG,
    file_sha,
    make_packet,
    ro_rows,
    seed,
    source_op,
    upsert_op,
)



# An upsert_fact with a value quotes its cited source and gives its raw value,
# so the fact tests cite a source that holds their values.
FACT_SOURCE = "src-fact-words"
FACT_WORDS = (
    "Fact notes. The window is weekly. We have 12 seats. The motto is ship it. We have 8 seats. "
    "The price is 9.5 AUD. The launch is 2026-11-01. Enabled is true. The site is https://example.test. "
    "The region is au. The region is eu. We have 3 seats. The extra is hello. The extra is there. "
    "The value is x."
)


def fact_source() -> dict:
    return source_op(FACT_SOURCE, FACT_WORDS)


def quoted(row: dict, quote: str, raw: str) -> dict:
    return {**row, "quote": quote, "value_raw": raw}

def test_stale_base_gives_conflict(apply) -> None:
    seed(apply)
    first = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-launch-review",
                    "title": "Answer Dana",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    second = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {"id": "act-launch-review", "status": "waiting"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    third = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {"id": "act-launch-review", "title": "New title"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert first.receipt["status"] == "applied"
    assert second.receipt["status"] == "applied"
    assert third.receipt["status"] == "conflict"
    assert "has moved on: base 1, now 2" in third.receipt["error"]


def test_repeated_change_id_returns_the_original_receipt(instance, apply) -> None:
    from tests.conftest import file_sha

    packet = make_packet(
        "tester",
        source_op("src-repeat", "A unique synthetic note for idempotency."),
    )
    first = apply(packet)
    digest = file_sha(instance / "brain.sqlite3")
    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    raw = json.dumps(packet).encode()
    second = apply(packet)
    shuffled = json.dumps(packet, indent=2, sort_keys=False).encode()
    # Re-serialised with different spacing; apply() uses the same apply helper which dumps again.
    third = instance  # keep linter quiet if unused later
    writer = Applier(instance)
    try:
        third = writer.apply(shuffled)
        again = writer.apply(raw)
    finally:
        writer.close()
    from brain.store import canonical_json

    assert second.receipt == first.receipt
    assert again.receipt == first.receipt
    assert third.receipt == first.receipt
    assert canonical_json(second.receipt) == canonical_json(first.receipt)
    assert second.duplicate and again.duplicate and third.duplicate
    assert second.exit_code == first.exit_code
    assert file_sha(instance / "brain.sqlite3") == digest
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count


def test_repeated_change_id_of_a_rejected_packet_returns_the_rejection(apply) -> None:
    packet = make_packet(
        "tester",
        source_op("src-a", "Dana asked whether the launch review can move to Thursday."),
        upsert_op(
            "upsert_record",
            {
                "id": "rec-bad",
                "kind": "note",
                "title": "Note",
                "body": 'Dana wrote "the launch review is cancelled for good".',
            },
            "src-a",
        ),
    )
    first = apply(packet)
    second = apply(packet)
    assert first.receipt["status"] == "rejected"
    assert second.receipt == first.receipt
    assert second.duplicate


def test_repeated_change_id_with_a_different_payload_returns_the_original_receipt(instance, apply) -> None:
    packet = make_packet(
        "tester",
        source_op("src-one", "First payload text for the change."),
        change_id="tester-samepayload1",
    )
    first = apply(packet)
    other = make_packet(
        "tester",
        source_op("src-two", "Second payload must not be written."),
        change_id="tester-samepayload1",
    )
    second = apply(other)
    assert second.receipt == first.receipt
    assert "different payload" in second.warning
    assert not ro_rows(instance, "SELECT 1 FROM sources WHERE id = 'src-two'")


def test_integrity_check_passes_after_every_commit(instance, apply, monkeypatch) -> None:
    seed(apply)
    calls: list[list[str]] = []
    original = __import__("brain.store", fromlist=["integrity_problems"]).integrity_problems

    def spy(con):
        result = original(con)
        calls.append(result)
        return result

    monkeypatch.setattr("brain.store.integrity_problems", spy)
    applied = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-int-1",
                    "title": "Integrity applied",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    rejected = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {
                    "id": "rec-int-bad",
                    "kind": "note",
                    "title": "Note",
                    "body": "Jade said we should drop the access form.",
                },
                "src-note-jade",
            ),
        )
    )
    conflict = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {"id": "act-int-1", "title": "Stale"},
                "src-note-anon",
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
                    "id": "act-held-1",
                    "title": "Needs approval",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    duplicate = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-held-1",
                    "title": "Needs approval",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
            change_id=held.receipt["change_id"],
        )
    )
    writer = Applier(instance)
    try:
        approved = writer.approve(held.receipt["change_id"], "tester")
        held2 = writer.apply(
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
                        "src-note-anon",
                    ),
                )
            ).encode()
        )
        refused = writer.refuse(held2.receipt["change_id"], "tester", "no")
    finally:
        writer.close()
    assert [item.receipt["status"] for item in (applied, rejected, conflict, held)] == [
        "applied",
        "rejected",
        "conflict",
        "held",
    ]
    assert duplicate.duplicate
    assert duplicate.integrity is None
    assert len(calls) == 7
    assert all(result == [] for result in calls)
    for outcome in (applied, rejected, conflict, held, approved, held2, refused):
        assert outcome.integrity == "ok"
    from brain.store import connect_readonly

    con = connect_readonly(instance / "brain.sqlite3")
    try:
        assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        con.close()


def test_failed_post_commit_check_pauses_the_store(instance, apply, monkeypatch) -> None:
    monkeypatch.setattr("brain.store.integrity_problems", lambda con: ["row 1 missing from index x"])
    first = apply(
        make_packet(
            "tester",
            source_op("src-pause", "A valid source used to trip the pause."),
        )
    )
    assert first.receipt["status"] == "applied"
    assert first.exit_code == 5
    assert (instance / "PAUSED").is_file()
    assert first.receipt["change_id"] in (instance / "PAUSED").read_text()
    monkeypatch.undo()
    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    with pytest.raises(Unavailable):
        apply(make_packet("tester", source_op("src-after-pause", "Must not apply while paused.")))
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count
    import subprocess

    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
    )
    assert check.returncode == 1
    assert "paused" in check.stdout.lower() or "paused" in check.stderr.lower()


def test_base_null_for_an_existing_row_gives_conflict(apply) -> None:
    seed(apply)
    first = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_person",
                {"id": "per-extra", "display_name": "Extra Person"},
                "src-note-anon",
            ),
        )
    )
    second = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_person",
                {"id": "per-extra", "display_name": "Other"},
                "src-note-anon",
            ),
        )
    )
    assert first.receipt["status"] == "applied"
    assert second.receipt["status"] == "conflict"
    assert "already exists" in second.receipt["error"]


def test_base_for_a_missing_row_gives_conflict(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_person",
                {"id": "per-missing", "display_name": "Missing"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert outcome.receipt["status"] == "conflict"
    assert "doesn't exist" in outcome.receipt["error"]


def test_packet_is_atomic_when_a_later_op_fails(instance, apply) -> None:
    seed(apply)
    before = ro_rows(instance, "SELECT count(*) FROM people")[0][0]
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_person",
                {"id": "per-atomic", "display_name": "Atomic"},
                "src-note-anon",
            ),
            upsert_op(
                "upsert_record",
                {
                    "id": "rec-atomic",
                    "kind": "note",
                    "title": "Note",
                    "body": 'Jade said we should drop the access form.',
                },
                "src-note-jade",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert ro_rows(instance, "SELECT count(*) FROM people")[0][0] == before
    assert not ro_rows(instance, "SELECT 1 FROM people WHERE id = 'per-atomic'")


def test_later_ops_see_earlier_ops_in_the_same_packet(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            source_op("src-same-packet", "Same packet source text is visible immediately."),
            upsert_op(
                "upsert_record",
                {
                    "id": "rec-same",
                    "kind": "note",
                    "title": "Note",
                    "body": "Same packet source text is visible immediately.",
                },
                "src-same-packet",
            ),
            upsert_op(
                "upsert_record",
                {"id": "rec-same", "title": "Updated note"},
                "src-same-packet",
                base=99,
            ),
        )
    )
    assert outcome.receipt["status"] == "applied"


def test_unknown_agent_is_rejected(apply) -> None:
    outcome = apply(make_packet("ghost", source_op("src-x", "Unknown agent text.")))
    assert outcome.receipt["status"] == "rejected"


def test_reserved_agent_is_rejected_through_apply(apply) -> None:
    outcome = apply(
        make_packet("brain", source_op("src-reserved", "Reserved agent must not apply."))
    )
    assert outcome.receipt["status"] == "rejected"
    assert "reserved" in outcome.receipt["error"]


def test_op_outside_agent_policy_is_rejected(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "collector",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-nope",
                    "title": "Nope",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "not allowed" in outcome.receipt["error"]


def test_project_scope_is_enforced(apply) -> None:
    seed(apply)
    outside = apply(
        make_packet(
            "scoped",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-ops",
                    "title": "Ops action",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "ops",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    inside = apply(
        make_packet(
            "scoped",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-general",
                    "title": "General action",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    moved = apply(
        make_packet(
            "scoped",
            upsert_op(
                "upsert_action",
                {"id": "act-general", "project_id": "ops"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert outside.receipt["status"] == "rejected"
    assert inside.receipt["status"] == "applied"
    assert moved.receipt["status"] == "rejected"


def test_fact_prefix_scope_is_enforced(apply) -> None:
    seed(apply)
    bad = apply(
        make_packet(
            "scoped",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": "ops.window",
                        "value": "weekly",
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "The window is weekly",
                    "weekly",
                ),
                FACT_SOURCE,
            ),
        )
    )
    good = apply(
        make_packet(
            "scoped",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": "demo.seats",
                        "value": 12,
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "We have 12 seats",
                    "12",
                ),
                FACT_SOURCE,
            ),
        )
    )
    assert bad.receipt["status"] == "rejected"
    assert good.receipt["status"] == "applied"


def test_configured_agents_cannot_create_or_change_workstreams(apply) -> None:
    seed(apply)
    create = apply(
        make_packet(
            "tester",
            source_op("src-ws", "Workstream attempt."),
            upsert_op(
                "upsert_project",
                {"id": "extra-ws", "kind": "workstream", "title": "Extra"},
                "src-ws",
            ),
        )
    )
    change = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_project",
                {"id": "general", "title": "Changed"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert create.receipt["status"] == "rejected"
    assert change.receipt["status"] == "rejected"


def test_held_packet_is_not_applied_until_approved(instance, apply) -> None:
    seed(apply)
    held = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-hold",
                    "title": "Hold me",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    assert held.receipt["status"] == "held"
    assert not ro_rows(instance, "SELECT 1 FROM actions WHERE id = 'act-hold'")
    writer = Applier(instance)
    try:
        approved = writer.approve(held.receipt["change_id"], "tester")
    finally:
        writer.close()
    assert approved.receipt["status"] == "applied"
    assert ro_rows(instance, "SELECT 1 FROM actions WHERE id = 'act-hold'")


def test_approval_by_a_non_approver_changes_nothing(instance, apply) -> None:
    seed(apply)
    held = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-hold-2",
                    "title": "Hold me",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    writer = Applier(instance)
    try:
        from brain.applier import NotAllowed

        with pytest.raises(NotAllowed):
            writer.approve(held.receipt["change_id"], "plain")
    finally:
        writer.close()
    assert held.receipt["status"] == "held"
    assert not ro_rows(instance, "SELECT 1 FROM actions WHERE id = 'act-hold-2'")


def test_approval_after_the_base_moved_gives_conflict(instance, apply) -> None:
    seed(apply)
    created = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-race",
                    "title": "Race",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    held = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {"id": "act-race", "title": "Held update"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {"id": "act-race", "title": "Moved first"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    writer = Applier(instance)
    try:
        outcome = writer.approve(held.receipt["change_id"], "tester")
    finally:
        writer.close()
    assert created.receipt["status"] == "applied"
    assert outcome.receipt["status"] == "conflict"


def test_refusal_rejects_a_held_packet(instance, apply) -> None:
    seed(apply)
    held = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-refuse",
                    "title": "Refuse me",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    writer = Applier(instance)
    try:
        outcome = writer.refuse(held.receipt["change_id"], "tester", "no thanks")
    finally:
        writer.close()
    assert outcome.receipt["status"] == "rejected"
    assert "refused by tester" in outcome.receipt["error"]


@pytest.mark.parametrize(
    "key,value,column,quote,raw",
    [
        ("demo.motto", "ship it", "value_text", "The motto is ship it", "ship it"),
        ("demo.seats", 8, "value_int", "We have 8 seats", "8"),
        ("demo.price", 9.5, "value_real", "The price is 9.5 AUD", "9.5"),
        ("demo.launch", "2026-11-01", "value_date", "The launch is 2026-11-01", "2026-11-01"),
        ("demo.enabled", True, "value_int", "Enabled is true", "true"),
        ("demo.site", "https://example.test", "value_text", "The site is https://example.test", "https://example.test"),
        ("demo.region", "au", "value_text", "The region is au", "au"),
    ],
)
def test_fact_values_map_to_one_typed_column(apply, instance, key, value, column, quote, raw) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": key,
                        "value": value,
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    quote,
                    raw,
                ),
                FACT_SOURCE,
            ),
        )
    )
    assert outcome.receipt["status"] == "applied"
    row = ro_rows(
        instance,
        "SELECT value_text, value_int, value_real, value_date FROM facts WHERE key = ?",
        (key,),
    )[0]
    filled = sum(part is not None for part in row)
    assert filled == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("demo.seats", True),
        ("demo.seats", "8"),
        ("demo.enabled", 1),
        ("demo.motto", 12),
        ("demo.launch", "not-a-date"),
    ],
)
def test_fact_value_of_the_wrong_json_type_is_rejected(apply, key, value) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": key,
                        "value": value,
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "We have 8 seats",
                    "8",
                ),
                FACT_SOURCE,
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "value_raw" not in outcome.receipt["error"] and "quote" not in outcome.receipt["error"]


def test_enum_fact_outside_allowed_values_is_rejected(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": "demo.region",
                        "value": "eu",
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "The region is eu",
                    "eu",
                ),
                FACT_SOURCE,
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == "op 1 (upsert_fact): value is not in allowed_values"


def test_unknown_fact_key_is_rejected(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": "demo.unknown",
                        "value": "x",
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "The value is x",
                    "x",
                ),
                FACT_SOURCE,
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == "op 1 (upsert_fact): unknown fact key demo.unknown"


def test_fact_key_type_cannot_change_while_a_fact_uses_it(instance, apply) -> None:
    seed(apply)
    written = apply(
        make_packet(
            "tester",
            fact_source(),
            upsert_op(
                "upsert_fact",
                quoted(
                    {
                        "key": "demo.seats",
                        "value": 3,
                        "basis": "observed",
                        "as_of": "2026-10-01",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "We have 3 seats",
                    "3",
                ),
                FACT_SOURCE,
            ),
        )
    )
    assert written.receipt["status"] == "applied", written.receipt
    text = TEST_CONFIG.replace('key = "demo.seats"\nvalue_type = "int"', 'key = "demo.seats"\nvalue_type = "text"')
    (instance / "brain.toml").write_text(text)
    outcome = apply_config(instance)
    assert outcome is not None
    assert outcome.receipt["status"] == "rejected"


def test_identical_source_readd_is_a_noop_and_a_different_one_is_rejected(instance, apply) -> None:
    seed(apply)
    same = apply(
        make_packet(
            "tester",
            source_op(
                "src-note-anon",
                "We will cut the access form from the onboarding pack this week.",
            ),
        )
    )
    different = apply(
        make_packet(
            "tester",
            source_op("src-note-anon", "Different content for the same id."),
        )
    )
    assert same.receipt["status"] == "applied"
    assert same.receipt["targets"] == []
    assert different.receipt["status"] == "rejected"
    assert "sources never change" in different.receipt["error"]


def test_identity_columns_cannot_change(apply) -> None:
    seed(apply)
    apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread",
                {
                    "id": "thr-one",
                    "channel": "email",
                    "account": "work-mail",
                    "external_id": "thread-1",
                },
                "src-note-anon",
            ),
        )
    )
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_thread",
                {"id": "thr-one", "channel": "slack"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "cannot change" in outcome.receipt["error"]


def test_future_timestamps_are_rejected(apply, monkeypatch) -> None:
    monkeypatch.setattr("brain.store.utc_now", lambda: "2026-10-01T00:00:00Z")
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-future",
                    "title": "Future",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:10:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"


def test_every_upsert_op_creates_and_updates(instance, apply) -> None:
    seed(apply)
    writer = Applier(instance, allow_reserved_agent=True)
    try:
        key = writer.apply(
            json.dumps(
                make_packet(
                    "brain",
                    upsert_op(
                        "upsert_fact_key",
                        {"key": "demo.extra", "value_type": "text"},
                        "src-note-anon",
                    ),
                )
            ).encode()
        )
        project = writer.apply(
            json.dumps(
                make_packet(
                    "brain",
                    upsert_op(
                        "upsert_project",
                        {"id": "side-project", "kind": "project", "title": "Side"},
                        "src-note-anon",
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert key.receipt["status"] == "applied"
    assert project.receipt["status"] == "applied"
    cases = [
        (
            "upsert_person",
            {"id": "per-casey", "display_name": "Casey"},
            {"display_name": "Casey Two"},
        ),
        (
            "upsert_system",
            {"id": "sys-mail", "name": "Mail", "kind": "saas"},
            {"vendor": "Acme"},
        ),
        (
            "upsert_record",
            {"id": "rec-up", "kind": "note", "title": "T", "body": "Body text"},
            {"title": "T2"},
        ),
        (
            "upsert_decision",
            {
                "id": "dec-up",
                "title": "Decide this now please",
                "quote": "move the launch review to Thursday afternoon",
                "decided_on": "2026-10-01",
                "decided_by_person_id": "per-dana-okafor",
                "status": "proposed",
                "project_id": "general",
            },
            {"status": "in_effect"},
        ),
        (
            "upsert_action",
            {
                "id": "act-up",
                "title": "Do the thing",
                "status": "open",
                "status_basis": "observed",
                "project_id": "general",
                "last_verified_at": "2026-10-01T00:00:00Z",
            },
            {"title": "Do it later"},
        ),
        (
            "upsert_checklist_item",
            {
                "id": "chk-up",
                "project_id": "general",
                "title": "Check item",
                "state": "not_started",
                "state_basis": "observed",
                "last_verified_at": "2026-10-01T00:00:00Z",
            },
            {"state": "in_progress"},
        ),
        (
            "upsert_thread",
            {
                "id": "thr-up",
                "channel": "email",
                "account": "work-mail",
                "external_id": "ext-1",
            },
            {"subject": "Hello"},
        ),
        (
            "upsert_thread_link",
            {
                "id": "lnk-up",
                "action_id": "act-up",
                "thread_id": "thr-up",
                "role": "awaiting_reply",
                "since": "2026-10-01T00:00:00Z",
            },
            {"status": "closed"},
        ),
        (
            "upsert_signal",
            {
                "id": "sig-up",
                "kind": "overdue",
                "subject_table": "actions",
                "subject_id": "act-up",
                "basis": "observed",
            },
            {"status": "dismissed", "resolved_at": "2026-10-01T00:00:00Z"},
        ),
        (
            "upsert_fact",
            {
                "key": "demo.extra",
                "value": "hello",
                "basis": "observed",
                "as_of": "2026-10-01",
                "last_verified_at": "2026-10-01T00:00:00Z",
                "quote": "The extra is hello",
                "value_raw": "hello",
            },
            {
                "value": "there",
                "basis": "observed",
                "as_of": "2026-10-02",
                "last_verified_at": "2026-10-01T00:00:00Z",
                "quote": "The extra is there",
                "value_raw": "there",
            },
        ),
    ]
    assert apply(make_packet("tester", fact_source())).receipt["status"] == "applied"
    for op_name, create_row, update_row in cases:
        cited = FACT_SOURCE if op_name == "upsert_fact" else "src-note-anon"
        created = apply(
            make_packet("tester", upsert_op(op_name, create_row, cited if "src-email-dana" not in op_name else "src-email-dana"))
        )
        if op_name == "upsert_decision":
            created = apply(
                make_packet("tester", upsert_op(op_name, create_row, "src-email-dana"))
            )
        assert created.receipt["status"] == "applied", created.receipt
        key_field = "key" if "key" in create_row else "id"
        updated = apply(
            make_packet(
                "tester",
                upsert_op(
                    op_name,
                    {key_field: create_row[key_field], **update_row},
                    "src-email-dana" if op_name == "upsert_decision" else cited,
                    base=1,
                ),
            )
        )
        assert updated.receipt["status"] == "applied", updated.receipt


def test_collector_runs_are_insert_only(apply) -> None:
    seed(apply)
    first = apply(
        make_packet(
            "collector",
            {
                "op": "record_collector_run",
                "row": {
                    "id": "run-one",
                    "collector": "mail-box",
                    "started_at": "2026-10-01T00:00:00Z",
                    "finished_at": "2026-10-01T00:01:00Z",
                    "status": "ok",
                },
            },
        )
    )
    second = apply(
        make_packet(
            "collector",
            {
                "op": "record_collector_run",
                "row": {
                    "id": "run-one",
                    "collector": "mail-box",
                    "started_at": "2026-10-01T00:00:00Z",
                    "finished_at": "2026-10-01T00:01:00Z",
                    "status": "ok",
                },
            },
        )
    )
    assert first.receipt["status"] == "applied"
    assert second.receipt["status"] == "rejected"
    assert "append-only" in second.receipt["error"]


def test_receipt_has_exactly_the_documented_keys(apply) -> None:
    outcome = apply(make_packet("tester", source_op("src-keys", "Receipt key source.")))
    assert set(outcome.receipt) == {
        "receipt_version",
        "change_id",
        "agent",
        "status",
        "decided_at",
        "applied_seq",
        "reviewed_by",
        "reviewed_at",
        "payload_sha256",
        "error",
        "targets",
    }


def test_payload_sha256_is_the_sha_of_the_stored_packet_json(instance, apply) -> None:
    from brain.store import sha256_text

    outcome = apply(make_packet("tester", source_op("src-hash", "Hash me.")))
    stored = ro_rows(
        instance, "SELECT packet_json, payload_sha256 FROM write_log WHERE change_id = ?",
        (outcome.receipt["change_id"],),
    )[0]
    assert sha256_text(stored[0]) == stored[1] == outcome.receipt["payload_sha256"]


@pytest.mark.parametrize(
    "kind",
    ["not-json", "NaN", "array", "no-change-id", "bad-change-id", "too-large"],
)
def test_unreadable_input_writes_nothing(instance, applier, kind) -> None:
    raw = {
        "not-json": b"not-json",
        "NaN": b"NaN",
        "array": b"[1]",
        "no-change-id": b'{"agent": "tester"}',
        "bad-change-id": b'{"change_id": "BAD", "agent": "tester"}',
        "too-large": b"{" + b"x" * (5 * 1024 * 1024 + 10),
    }[kind]
    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    with pytest.raises(Unreadable):
        applier.apply(raw)
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count


def test_store_version_mismatch_is_unavailable(instance) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("PRAGMA user_version = 99")
    finally:
        con.close()
    writer = Applier(instance)
    try:
        with pytest.raises(Unavailable):
            writer.apply(json.dumps(make_packet("tester", source_op("src-v", "x" * 8))).encode())
    finally:
        writer.close()


def test_concurrent_appliers_serialise_without_losing_writes(instance) -> None:
    errors = []
    receipts = []

    def worker(prefix: str) -> None:
        writer = Applier(instance)
        try:
            for i in range(20):
                packet = make_packet(
                    "tester",
                    source_op(f"src-{prefix}-{i}", f"Concurrent source {prefix} {i} text."),
                )
                receipts.append(writer.apply(json.dumps(packet).encode()))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            writer.close()

    threads = [threading.Thread(target=worker, args=("a",)), threading.Thread(target=worker, args=("b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(receipts) == 40
    assert all(item.receipt["status"] == "applied" for item in receipts)
    seqs = sorted(row[0] for row in ro_rows(instance, "SELECT applied_seq FROM write_log WHERE applied_seq IS NOT NULL"))
    assert seqs == list(range(1, 42))


def test_unhandled_command_op_is_not_applied(instance, monkeypatch) -> None:
    from brain import applier as applier_mod
    from brain import schemas

    command_ops = dict(schemas.COMMAND_OPS)
    command_ops["poke_index"] = {
        "table": "search_index",
        "kind": "command",
        "envelope": {
            "type": "object",
            "required": ["op"],
            "properties": {"op": {"type": "string"}},
        },
    }
    monkeypatch.setattr(schemas, "COMMAND_OPS", command_ops)
    monkeypatch.setattr(applier_mod, "_BRAIN_OPS", applier_mod._BRAIN_OPS | {"poke_index"})
    packet = make_packet("brain", {"op": "poke_index"}, intent="unhandled command")
    digest = file_sha(instance / "brain.sqlite3")
    applied = ro_rows(instance, "SELECT count(*) FROM write_log WHERE status = 'applied'")[0][0]
    writer = Applier(instance, allow_reserved_agent=True)
    try:
        with pytest.raises(RuntimeError, match=r"op 0 \(poke_index\): unhandled command op"):
            writer.apply(json.dumps(packet).encode())
    finally:
        writer.close()
    assert file_sha(instance / "brain.sqlite3") == digest
    assert (
        ro_rows(
            instance,
            "SELECT status FROM write_log WHERE change_id = ?",
            (packet["change_id"],),
        )
        == []
    )
    assert ro_rows(instance, "SELECT count(*) FROM write_log WHERE status = 'applied'")[0][0] == applied
