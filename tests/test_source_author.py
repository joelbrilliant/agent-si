"""set_source_author corrects who wrote a source (migration 0009).

A source stored under the principal's name when someone else wrote it lets that person's words pass the evidence
gate as the principal's, so its author can be corrected, and nothing else about it. Every name and text here is
invented."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from brain.applier import Applier
from tests.conftest import context_connection, make_packet, ro_rows, source_op, upsert_op
from tests.test_truth import _apply, _err, _ok

WHY = "src-note-why"
AS_JADE = "src-email-dana-as-jade"
DANA_EMAIL = (
    "Dana Okafor to Jade, Tuesday: we should keep the seed swap on the Saturday table "
    "and bring the tins after it."
)
QUOTE = "keep the seed swap on the Saturday table"


def author_op(source_id: str, author, *, cite: str = WHY, locator: str = "author correction") -> dict:
    return {
        "op": "set_source_author",
        "row": {"source_id": source_id, "author_person_id": author},
        "source": {"source_id": cite, "locator": locator},
    }


def claim(record_id: str) -> dict:
    row = {"id": record_id, "kind": "note", "title": "Seed swap", "body": f'Jade said "{QUOTE}".'}
    return upsert_op("upsert_record", row, AS_JADE)


def author_of(instance: Path, source_id: str):
    return ro_rows(instance, "SELECT author_person_id FROM sources WHERE id = ?", (source_id,))[0][0]


@pytest.fixture
def world(instance: Path) -> Path:
    _ok(_apply(
        instance, "tester",
        source_op(WHY, "This note says why the author is corrected."),
        upsert_op("upsert_person", {"id": "per-dana-okafor", "display_name": "Dana Okafor"}, WHY, locator="seed"),
        source_op(AS_JADE, DANA_EMAIL, kind="email", author="per-jade"),
        source_op("src-email-dana", DANA_EMAIL + " Thanks.", kind="email", author="per-dana-okafor"),
    ))
    return instance


def test_a_quote_passes_under_the_wrong_author_until_the_author_is_cleared(world: Path) -> None:
    """Someone else's email stored as the principal's lets their words pass as hers.
    Clearing its author closes that route, and the gate then refuses the same sentence."""
    _ok(_apply(world, "tester", claim("rec-before")))
    _ok(_apply(world, "tester", author_op(AS_JADE, None)))
    error = _err(_apply(world, "tester", claim("rec-after")))
    assert "evidence gate" in error and "Jade" in error, error


def test_it_changes_only_the_author_and_logs_the_one_it_replaced(world: Path) -> None:
    columns = [row[1] for row in ro_rows(world, "PRAGMA table_info(sources)")]
    before = ro_rows(world, "SELECT * FROM sources WHERE id = ?", (AS_JADE,))[0]
    outcome = _ok(_apply(world, "tester", author_op(AS_JADE, "per-dana-okafor")))
    after = ro_rows(world, "SELECT * FROM sources WHERE id = ?", (AS_JADE,))[0]
    assert [name for name, old, new in zip(columns, before, after) if old != new] == ["author_person_id"]
    assert author_of(world, AS_JADE) == "per-dana-okafor"
    assert ro_rows(
        world,
        "SELECT change_id, old_author_person_id, new_author_person_id, evidence_source_id, locator, applied, changed_by "
        "FROM source_author_changes",
    ) == [(outcome.receipt["change_id"], "per-jade", "per-dana-okafor", WHY, "author correction", 1,
           f"tester:{outcome.receipt['change_id']}")]


def test_one_packet_corrects_many_sources_and_a_later_one_can_change_it_again(world: Path) -> None:
    _ok(_apply(world, "tester", author_op(AS_JADE, None), author_op("src-email-dana", None)))
    assert (author_of(world, AS_JADE), author_of(world, "src-email-dana")) == (None, None)
    _ok(_apply(world, "tester", author_op(AS_JADE, "per-dana-okafor")))
    assert ro_rows(
        world, "SELECT old_author_person_id, new_author_person_id FROM source_author_changes WHERE source_id = ? "
        "ORDER BY rowid", (AS_JADE,),
    ) == [("per-jade", None), (None, "per-dana-okafor")]


@pytest.mark.parametrize("agent", ["plain", "careful"])
def test_only_an_author_principal_agent_may_send_it_and_nothing_is_held(world: Path, agent: str) -> None:
    """careful needs approval: its packet is refused before the hold, so no approver is asked about it."""
    error = _err(_apply(world, agent, author_op(AS_JADE, None)))
    assert "author_principal = true" in error and "send the packet as that agent" in error, error
    assert author_of(world, AS_JADE) == "per-jade"
    assert ro_rows(world, "SELECT count(*) FROM source_author_changes") == [(0,)]


@pytest.mark.parametrize(
    ("op", "fix"),
    [
        (author_op("src-missing", None), "records_query on source lists the ids"),
        (author_op(AS_JADE, "per-nobody"), "add them with upsert_person first, or send author_person_id null"),
        (author_op("src-email-dana", "per-dana-okafor"), "leave this op out of the packet"),
        (author_op("src-email-dana", "per-jade"), "never makes per-jade a source's author; to record the principal's own words"),
        (author_op(AS_JADE, None, cite=AS_JADE), "the source it changes; cite a different source"),
        (author_op(AS_JADE, None, cite="src-missing"), "add it first with add_source"),
    ],
)
def test_each_refusal_names_its_fix(world: Path, op: dict, fix: str) -> None:
    error = _err(_apply(world, "tester", op))
    assert fix in error, error
    assert author_of(world, AS_JADE) == "per-jade"


def test_a_config_source_has_no_author_to_change(world: Path) -> None:
    config_ids = [row[0] for row in ro_rows(world, "SELECT id FROM sources WHERE kind = 'config' LIMIT 1")]
    assert config_ids, "brain init writes a config source"
    error = _err(_apply(world, "tester", author_op(config_ids[0], None)))
    assert "is a config source" in error and "pick a live source" in error, error


def test_the_same_source_twice_in_one_packet_is_refused_and_leaves_nothing_pending(world: Path) -> None:
    error = _err(_apply(world, "tester", author_op(AS_JADE, None), author_op(AS_JADE, "per-dana-okafor")))
    assert "changes source src-email-dana-as-jade twice; send one set_source_author op per source" in error, error
    assert author_of(world, AS_JADE) == "per-jade"
    # The refusal rolls the whole packet back, so no pending row is left to let a later update through.
    assert ro_rows(world, "SELECT count(*) FROM source_author_changes") == [(0,)]


def test_a_dry_run_gives_the_verdict_and_keeps_nothing(world: Path) -> None:
    writer = Applier(world)
    try:
        outcome = writer.apply(json.dumps(make_packet("tester", author_op(AS_JADE, None))).encode(), dry_run=True)
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert author_of(world, AS_JADE) == "per-jade"
    assert ro_rows(world, "SELECT count(*) FROM source_author_changes") == [(0,)]


def test_the_trigger_lets_through_only_the_logged_author_change(world: Path) -> None:
    con = context_connection(world)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="sources never change"):
            con.execute("UPDATE sources SET author_person_id = NULL WHERE id = ?", (AS_JADE,))
        con.execute(
            "INSERT INTO source_author_changes (change_id, source_id, old_author_person_id, new_author_person_id, "
            "evidence_source_id, locator, changed_at, changed_by) "
            "VALUES ('c-1', ?, 'per-jade', NULL, ?, 'x', '2026-10-26T00:00:00Z', 'tester:c-1')",
            (AS_JADE, WHY),
        )
        with pytest.raises(sqlite3.IntegrityError, match="sources never change"):
            con.execute("UPDATE sources SET author_person_id = NULL, content = 'other words' WHERE id = ?", (AS_JADE,))
        with pytest.raises(sqlite3.IntegrityError, match="sources never change"):
            con.execute("UPDATE sources SET author_person_id = 'per-dana-okafor' WHERE id = ?", (AS_JADE,))
        with pytest.raises(sqlite3.IntegrityError, match="sources never change"):
            con.execute("UPDATE sources SET author_person_id = NULL WHERE id = 'src-email-dana'")
        con.execute("UPDATE sources SET author_person_id = NULL WHERE id = ?", (AS_JADE,))
        con.execute("UPDATE source_author_changes SET applied = 1 WHERE change_id = 'c-1'")
        with pytest.raises(sqlite3.IntegrityError, match="never change once applied"):
            con.execute("UPDATE source_author_changes SET applied = 0 WHERE change_id = 'c-1'")
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            con.execute("DELETE FROM source_author_changes")
        with pytest.raises(sqlite3.IntegrityError, match="sources never change"):
            con.execute("UPDATE sources SET author_person_id = 'per-jade' WHERE id = ?", (AS_JADE,))
    finally:
        con.rollback()
        con.close()


def test_resending_a_corrected_source_names_its_current_author(world: Path) -> None:
    """Re-sending the original add_source (an importer re-run) must not be told to use a new id, which would store
    the same email under the principal again."""
    _ok(_apply(world, "tester", author_op(AS_JADE, None)))
    error = _err(_apply(world, "tester", source_op(AS_JADE, DANA_EMAIL, kind="email", author="per-jade")))
    assert "set_source_author changed its author_person_id to null; send author_person_id null" in error, error
    assert "use a new id" not in error
    _ok(_apply(world, "tester", source_op(AS_JADE, DANA_EMAIL, kind="email")))
