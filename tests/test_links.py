"""Links with sources.

A record, action, checklist item or decision cites its primary source in the envelope's `source` and any others in
`sources`. Each item becomes a row of links with a label. The evidence gate and the date rule read every cited source,
a record has a summary, and a legacy import may keep a row's original times.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from brain import advice, migrate, schemas, store
from brain.applier import Applier, link_target_id
from brain.init import init_instance
from tests.conftest import context_connection, make_packet, render_config, ro_rows, source_op, upsert_op

AGENTS = {
    "tester": {"operations": "*", "author_principal": True},
    "plain": {"operations": "*"},
    "importer": {"operations": "*", "legacy_import": True},
}
JADE = "per-jade"


@pytest.fixture
def inst(tmp_path: Path) -> Path:
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(agents=AGENTS))["instance"])
    _ok(_apply(instance, "tester",
               source_op("src-a", "Descale the kettle every month. The filter lasts a year.", date="2026-10-01"),
               source_op("src-b", "Order the new filters on Friday. The supplier is Kettle Co.", date="2026-10-02"),
               source_op("src-jade", "I want the team to descale the kettle every single month from now on.",
                         author=JADE, date="2026-10-03"),
               source_op("src-c", "Chat notes from the kitchen meeting.", date="2026-10-04")))
    return instance


def _apply(instance: Path, agent: str, *ops, dry_run: bool = False):
    writer = Applier(instance)
    try:
        return writer.apply(json.dumps(make_packet(agent, *ops)).encode(), dry_run=dry_run)
    finally:
        writer.close()


def _ok(outcome):
    assert outcome.receipt["status"] == "applied", outcome.receipt["error"]
    return outcome


def _refused(outcome) -> str:
    assert outcome.receipt["status"] != "applied", outcome.receipt
    return outcome.receipt["error"]


def _linked(op: dict, *items) -> dict:
    return {**op, "sources": [dict(item) for item in items]}


def _record(row_id="rec-kettle", **fields) -> dict:
    return {"id": row_id, "kind": "note", "title": "Kettle care", "body": "Descale it.", **fields}


def _links(instance: Path, subject_id: str) -> list[tuple]:
    return ro_rows(instance, "SELECT subject_table, label, source_id, locator FROM links WHERE subject_id = ? "
                             "ORDER BY label, source_id", (subject_id,))


def _version(instance: Path, table: str, row_id: str) -> int:
    return ro_rows(instance, f"SELECT version FROM {table} WHERE id = ?", (row_id,))[0][0]


# -- A. the schema -------------------------------------------------------------------------------------------------


def test_the_links_table_follows_its_ops_and_its_labels(tmp_path: Path) -> None:
    db = tmp_path / "brain.sqlite3"
    migrate.migrate(db, create=True)
    con = sqlite3.connect(db)
    try:
        sql = con.execute("SELECT sql FROM sqlite_schema WHERE name = 'links'").fetchone()[0]
        columns = [row[1] for row in con.execute("PRAGMA table_info(links)")]
        indexes = {row[1] for row in con.execute("PRAGMA index_list(links)")}
        history = [row[1] for row in con.execute("PRAGMA table_info(record_history)")]
        records = [row[1] for row in con.execute("PRAGMA table_info(records)")]
    finally:
        con.close()
    subjects = re.search(r"subject_table\s+IN\s*\(([^)]*)\)", sql).group(1)
    labels = re.search(r"label\s+IN\s*\(([^)]*)\)", sql).group(1)
    assert set(re.findall(r"'([^']*)'", subjects)) == set(schemas.LINK_TABLES), "links.subject_table vs LINK_OPS' tables"
    assert set(re.findall(r"'([^']*)'", labels)) == set(schemas.LINK_LABELS), "links.label vs schemas.LINK_LABELS"
    assert schemas.LINK_DEFAULT_LABEL in schemas.LINK_LABELS
    assert columns == ["subject_table", "subject_id", "label", "source_id", "locator", "created_at", "created_by"]
    assert "links_by_source" in indexes
    assert "summary" in records and "summary" in history
    assert {name for name in schemas.LINK_OPS} == {"upsert_record", "upsert_action", "upsert_checklist_item",
                                                    "upsert_decision"}


def test_links_are_append_only_and_name_a_row_that_exists(inst: Path) -> None:
    _ok(_apply(inst, "tester", _linked(upsert_op("upsert_record", _record(), "src-a"),
                                       {"source_id": "src-b", "locator": "p1"})))
    con = context_connection(inst)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("UPDATE links SET locator = 'p2'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM links")
        with pytest.raises(sqlite3.IntegrityError, match="does not exist"):
            con.execute("INSERT INTO links VALUES ('records', 'rec-none', 'cites', 'src-a', 'p1', "
                        "'2026-10-08T00:00:00Z', 'tester:x')")
    finally:
        con.close()


def test_each_record_version_keeps_its_summary(inst: Path) -> None:
    _ok(_apply(inst, "tester", upsert_op("upsert_record", _record(summary="Monthly descaling."), "src-a")))
    _ok(_apply(inst, "tester", upsert_op("upsert_record", {"id": "rec-kettle", "summary": "Descale monthly."},
                                         "src-a", base=1)))
    assert ro_rows(inst, "SELECT version, summary FROM record_history WHERE record_id = 'rec-kettle' "
                         "ORDER BY version") == [(1, "Monthly descaling."), (2, "Descale monthly.")]
    error = _refused(_apply(inst, "tester", upsert_op("upsert_record", {"id": "rec-kettle", "summary": "x" * 1201},
                                                      "src-a", base=2)))
    assert "at most 1200" in error


# -- B. the write path ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name,row", [
    ("upsert_record", _record()),
    ("upsert_action", {"id": "act-filters", "title": "Order filters", "status": "open", "status_basis": "reported",
                       "project_id": "general", "last_verified_at": "2026-10-05T00:00:00Z"}),
    ("upsert_checklist_item", {"id": "chk-filters", "project_id": "general", "title": "Order filters",
                               "state": "not_started", "state_basis": "reported",
                               "last_verified_at": "2026-10-05T00:00:00Z"}),
    ("upsert_decision", {"id": "dec-descale", "title": "Descale monthly", "quote": "Descale the kettle every month",
                         "decided_on": "2026-10-01", "decided_by_person_id": "per-jade-agent", "status": "in_effect",
                         "project_id": "general"}),
])
def test_every_linked_op_writes_its_links_and_names_them_in_the_receipt(inst: Path, name: str, row: dict) -> None:
    ops = []
    if name == "upsert_decision":
        ops.append(upsert_op("upsert_person", {"id": "per-jade-agent", "display_name": "Kim"}, "src-a"))
    ops.append(_linked(upsert_op(name, row, "src-a"), {"source_id": "src-b", "locator": "p2"},
                       {"source_id": "src-c", "locator": "line 1", "label": "status"}))
    receipt = _ok(_apply(inst, "tester", *ops)).receipt
    table = schemas.OPS[name]["table"]
    assert _links(inst, row["id"]) == [(table, "cites", "src-b", "p2"), (table, "status", "src-c", "line 1")]
    links = [item for item in receipt["targets"] if item["table"] == "links"]
    assert links == [
        {"table": "links", "id": link_target_id(table, row["id"], "cites", "src-b"), "action": "insert"},
        {"table": "links", "id": link_target_id(table, row["id"], "status", "src-c"), "action": "insert"},
    ]
    # The row's own source stays its primary citation.
    assert ro_rows(inst, f"SELECT source_id FROM {table} WHERE id = ?", (row["id"],)) == [("src-a",)]


def test_an_existing_link_is_left_alone_and_a_links_only_upsert_keeps_the_version(inst: Path) -> None:
    _ok(_apply(inst, "tester", _linked(upsert_op("upsert_record", _record(), "src-a"),
                                       {"source_id": "src-b", "locator": "p2"})))
    created = ro_rows(inst, "SELECT created_at, created_by FROM links")
    # A links-only upsert: the id plus sources. It needs base, and the row and its version stay as they were.
    no_base = _apply(inst, "tester", _linked(upsert_op("upsert_record", {"id": "rec-kettle"}, "src-a"),
                                             {"source_id": "src-c", "locator": "p9"}))
    assert no_base.receipt["status"] == "conflict" and '"base": 1' in no_base.receipt["error"]
    before = ro_rows(inst, "SELECT * FROM records WHERE id = 'rec-kettle'")
    receipt = _ok(_apply(inst, "tester", _linked(upsert_op("upsert_record", {"id": "rec-kettle"}, "src-a", base=1),
                                                 {"source_id": "src-b", "locator": "elsewhere"},
                                                 {"source_id": "src-c", "locator": "p9"}))).receipt
    assert ro_rows(inst, "SELECT * FROM records WHERE id = 'rec-kettle'") == before
    assert _version(inst, "records", "rec-kettle") == 1
    assert receipt["targets"] == [
        {"table": "links", "id": link_target_id("records", "rec-kettle", "cites", "src-c"), "action": "insert"}]
    # The first link kept its locator and stamp; a re-sent identical op changes nothing.
    assert _links(inst, "rec-kettle") == [("records", "cites", "src-b", "p2"), ("records", "cites", "src-c", "p9")]
    assert ro_rows(inst, "SELECT created_at, created_by FROM links WHERE source_id = 'src-b'") == created
    again = _ok(_apply(inst, "tester", _linked(upsert_op("upsert_record", {"id": "rec-kettle"}, "src-a", base=1),
                                               {"source_id": "src-c", "locator": "p9"}))).receipt
    assert again["targets"] == []


@pytest.mark.parametrize("name,row", [
    ("upsert_action", {"id": "act-filters", "title": "Order filters", "status": "open", "status_basis": "reported",
                       "project_id": "general", "last_verified_at": "2026-10-05T00:00:00Z"}),
    ("upsert_checklist_item", {"id": "chk-filters", "project_id": "general", "title": "Order filters",
                               "state": "not_started", "state_basis": "reported",
                               "last_verified_at": "2026-10-05T00:00:00Z"}),
])
def test_a_links_only_upsert_leaves_an_action_or_checklist_item_as_it_was(inst: Path, name: str, row: dict) -> None:
    # Resolving people adds a key to the row, so a links-only upsert on an action must not rewrite the row or
    # bump its version, or the importer's next base would be refused.
    table = schemas.OPS[name]["table"]
    _ok(_apply(inst, "tester", upsert_op(name, row, "src-a")))
    before = ro_rows(inst, f"SELECT * FROM {table} WHERE id = ?", (row["id"],))
    receipt = _ok(_apply(inst, "tester", _linked(upsert_op(name, {"id": row["id"]}, "src-a", base=1),
                                                 {"source_id": "src-b", "locator": "p2"}))).receipt
    assert ro_rows(inst, f"SELECT * FROM {table} WHERE id = ?", (row["id"],)) == before
    assert _version(inst, table, row["id"]) == 1
    assert receipt["targets"] == [
        {"table": "links", "id": link_target_id(table, row["id"], "cites", "src-b"), "action": "insert"}]


def test_a_missing_linked_source_is_refused_with_its_fix(inst: Path) -> None:
    error = _refused(_apply(inst, "tester", _linked(upsert_op("upsert_record", _record(), "src-a"),
                                                    {"source_id": "src-none", "locator": "p1"})))
    assert advice.link_source_missing_error("src-none") in error
    assert "add_source" in error and not ro_rows(inst, "SELECT 1 FROM records")
    # A source added earlier in the same packet can be linked.
    _ok(_apply(inst, "tester", source_op("src-new", "Fresh notes."),
               _linked(upsert_op("upsert_record", _record(), "src-a"), {"source_id": "src-new", "locator": "p1"})))


def test_the_envelope_checks_sources(inst: Path) -> None:
    from brain import validate

    def errors(op: dict) -> list[dict]:
        return validate.validate_packet(make_packet("tester", op))

    base = upsert_op("upsert_record", _record(), "src-a")
    too_many = [{"source_id": f"src-s{i}", "locator": "p"} for i in range(schemas.LINKS_MAX + 1)]
    assert any(item["path"] == "/ops/0/sources" and str(schemas.LINKS_MAX) in item["message"]
               for item in errors({**base, "sources": too_many}))
    assert any(item["path"] == "/ops/0/sources/0/label" and "cites" in item["message"]
               for item in errors(_linked(base, {"source_id": "src-b", "locator": "p", "label": "quote"})))
    assert any(item["path"] == "/ops/0/sources/0/locator"
               for item in errors(_linked(base, {"source_id": "src-b", "locator": " "})))
    # An op that takes no links refuses the property and names what it allows.
    person = _linked(upsert_op("upsert_person", {"id": "per-kim", "display_name": "Kim"}, "src-a"),
                     {"source_id": "src-b", "locator": "p"})
    assert any(item["path"] == "/ops/0/sources" and "Allowed:" in item["message"] for item in errors(person))


def test_the_dry_run_gives_the_same_verdict_and_keeps_nothing(inst: Path) -> None:
    op = _linked(upsert_op("upsert_record", _record(summary='Says "the filter lasts a year".'), "src-a"),
                 {"source_id": "src-b", "locator": "p2"})
    rehearsal = _apply(inst, "tester", op, dry_run=True)
    assert rehearsal.receipt["status"] == "applied" and rehearsal.dry_run
    assert any(item["table"] == "links" for item in rehearsal.receipt["targets"])
    assert ro_rows(inst, "SELECT count(*) FROM links") == [(0,)]
    bad = _linked(op, {"source_id": "src-none", "locator": "p"})
    assert _apply(inst, "tester", bad, dry_run=True).receipt["error"] == _apply(inst, "tester", bad).receipt["error"]


# -- C. the evidence gate and the date rule ------------------------------------------------------------------------


def test_a_quote_passes_when_any_cited_source_holds_it(inst: Path) -> None:
    row = _record(body='The supplier said "order the new filters on Friday".')
    one = _refused(_apply(inst, "plain", upsert_op("upsert_record", row, "src-a")))
    assert "is not in the cited source src-a" in one
    _ok(_apply(inst, "plain", _linked(upsert_op("upsert_record", row, "src-a"), {"source_id": "src-b", "locator": "p2"})))
    # Not in any: the refusal names every source checked.
    other = _record("rec-other", body='Notes say "buy a new kettle soon".')
    error = _refused(_apply(inst, "plain", _linked(upsert_op("upsert_record", other, "src-a"),
                                                   {"source_id": "src-b", "locator": "p2"})))
    assert "not in any cited source (checked src-a, src-b)" in error and "sources" in error


def test_the_summary_is_gated(inst: Path) -> None:
    error = _refused(_apply(inst, "plain", upsert_op("upsert_record", _record(summary='It says "replace weekly now".'),
                                                     "src-a")))
    assert "evidence gate" in error and "replace weekly now" in error


def test_an_attribution_needs_five_words_from_a_cited_source_the_principal_wrote(inst: Path) -> None:
    said = _record(body='Jade wants the team "to descale the kettle every single month".')
    alone = _refused(_apply(inst, "plain", upsert_op("upsert_record", said, "src-a")))
    assert "is not in the cited source src-a" in alone
    # The same words, held by a source Jade wrote that the op also cites, pass.
    _ok(_apply(inst, "plain", _linked(upsert_op("upsert_record", said, "src-a"),
                                      {"source_id": "src-jade", "locator": "line 1"})))
    # Five words that only a source someone else wrote holds do not, and the refusal names what it checked.
    other = _record("rec-other", body='Jade said "descale the kettle every month".')
    error = _refused(_apply(inst, "plain", _linked(upsert_op("upsert_record", other, "src-a"),
                                                   {"source_id": "src-b", "locator": "p2"})))
    assert "attributes words or a decision to Jade" in error and "Checked src-a, src-b." in error


def test_a_decision_quote_comes_from_a_cited_source_and_the_principal_needs_their_own(inst: Path) -> None:
    decision = {"id": "dec-descale", "title": "Descale monthly", "decided_on": "2026-10-03", "status": "in_effect",
                "project_id": "general", "decided_by_person_id": JADE,
                "quote": "descale the kettle every single month"}
    error = _refused(_apply(inst, "plain", _linked(upsert_op("upsert_decision", {**decision, "decided_on": "2026-10-01"},
                                                             "src-a"), {"source_id": "src-b", "locator": "p2"})))
    assert "decision quote is not in any cited source (checked src-a, src-b)" in error
    _ok(_apply(inst, "plain", _linked(upsert_op("upsert_decision", decision, "src-a"),
                                      {"source_id": "src-jade", "locator": "line 1"})))
    # A quote that only a source someone else wrote holds is refused for a decision by the principal.
    other = {**decision, "id": "dec-other", "quote": "Descale the kettle every month", "decided_on": "2026-10-01"}
    error = _refused(_apply(inst, "plain", _linked(upsert_op("upsert_decision", other, "src-c"),
                                                   {"source_id": "src-a", "locator": "p1"})))
    assert "needs its quote from a source authored by per-jade" in error and "src-a" in error


def test_a_state_date_passes_when_any_cited_source_allows_it(inst: Path) -> None:
    action = {"id": "act-filters", "title": "Order filters", "status": "open", "status_basis": "reported",
              "project_id": "general", "last_verified_at": "2026-10-05T00:00:00Z", "due_date": "2026-10-02",
              "due_kind": "deadline"}
    one = _refused(_apply(inst, "plain", upsert_op("upsert_action", action, "src-c")))
    assert "due_date 2026-10-02 is neither the source date nor in the source" in one
    two = {**action, "due_date": "2026-12-25"}
    error = _refused(_apply(inst, "plain", _linked(upsert_op("upsert_action", two, "src-c"),
                                                   {"source_id": "src-a", "locator": "p1"})))
    assert advice.date_not_in_sources_error("due_date", "2026-12-25", ["src-c", "src-a"]) in error
    _ok(_apply(inst, "plain", _linked(upsert_op("upsert_action", action, "src-c"),
                                      {"source_id": "src-b", "locator": "p2", "label": "due"})))


def test_a_legacy_import_still_flags_rather_than_refuses(inst: Path) -> None:
    row = _record(body='Notes say "buy a new kettle soon".')
    _ok(_apply(inst, "importer", _linked(upsert_op("upsert_record", row, "src-a"),
                                         {"source_id": "src-b", "locator": "p2"})))
    assert ro_rows(inst, "SELECT entity, ref_id FROM unchecked_text") == [("record", "rec-kettle")]


def test_a_link_to_a_redacted_source_is_refused(inst: Path) -> None:
    writer = Applier(inst, allow_reserved_agent=True)
    try:
        packet = make_packet("brain", {"op": "redact_source", "source_id": "src-c", "reason": "test removal",
                                       "operator": "tester"})
        _ok(writer.apply(json.dumps(packet).encode()))
    finally:
        writer.close()
    error = _refused(_apply(inst, "tester", _linked(upsert_op("upsert_record", _record(), "src-a"),
                                                    {"source_id": "src-c", "locator": "p1"})))
    assert "source src-c is redacted" in error


def test_a_redaction_scrubs_a_row_that_cites_the_source_through_a_link(inst: Path) -> None:
    row = _record(body='The supplier said "order the new filters on Friday".',
                  summary='Filters: "order the new filters on Friday".')
    _ok(_apply(inst, "tester", _linked(upsert_op("upsert_record", row, "src-a"), {"source_id": "src-b", "locator": "p2"})))
    writer = Applier(inst, allow_reserved_agent=True)
    try:
        packet = make_packet("brain", {"op": "redact_source", "source_id": "src-b", "reason": "test removal",
                                       "operator": "tester"})
        _ok(writer.apply(json.dumps(packet).encode()))
    finally:
        writer.close()
    body, summary = ro_rows(inst, "SELECT body, summary FROM records WHERE id = 'rec-kettle'")[0]
    assert "[redacted]" in body and "Friday" not in body
    assert "[redacted]" in summary and "Friday" not in summary
    assert all("Friday" not in text for (text,) in ro_rows(inst, "SELECT body || summary FROM record_history"))
    # The link stays: it is a pointer to the tombstone, never the source's words.
    assert _links(inst, "rec-kettle") == [("records", "cites", "src-b", "p2")]
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        assert store.redaction_problems(con) == []
    finally:
        con.close()


# -- D. original times on a legacy import --------------------------------------------------------------------------

_TIMES = {"created_at": "2025-03-01T09:00:00Z", "updated_at": "2025-04-01T10:30:00Z"}


@pytest.mark.parametrize("name,row", [
    ("upsert_record", _record()),
    ("upsert_action", {"id": "act-filters", "title": "Order filters", "status": "open", "status_basis": "reported",
                       "project_id": "general", "last_verified_at": "2026-10-05T00:00:00Z"}),
    ("upsert_checklist_item", {"id": "chk-filters", "project_id": "general", "title": "Order filters",
                               "state": "not_started", "state_basis": "reported",
                               "last_verified_at": "2026-10-05T00:00:00Z"}),
    ("upsert_decision", {"id": "dec-descale", "title": "Descale monthly", "quote": "Descale the kettle every month",
                         "decided_on": "2026-10-01", "decided_by_person_id": "per-kim", "status": "in_effect",
                         "project_id": "general"}),
])
def test_a_legacy_import_keeps_original_times_and_no_one_else_may(inst: Path, name: str, row: dict) -> None:
    table = schemas.OPS[name]["table"]
    person = upsert_op("upsert_person", {"id": "per-kim", "display_name": "Kim"}, "src-a")
    _ok(_apply(inst, "tester", person))
    error = _refused(_apply(inst, "plain", upsert_op(name, {**row, **_TIMES}, "src-a")))
    assert advice.original_times_agent_error("created_at", "plain") in error
    _ok(_apply(inst, "importer", upsert_op(name, {**row, **_TIMES}, "src-a")))
    assert ro_rows(inst, f"SELECT created_at, updated_at FROM {table} WHERE id = ?", (row["id"],)) == [
        (_TIMES["created_at"], _TIMES["updated_at"])]
    # An update keeps the applier's own stamps: original times are for an insert only.
    error = _refused(_apply(inst, "importer", upsert_op(name, {"id": row["id"], **_TIMES}, "src-a", base=1)))
    assert advice.original_times_update_error("created_at") in error


def test_original_times_are_ordered_and_not_after_the_apply_time(inst: Path) -> None:
    backwards = {"created_at": "2025-05-01T00:00:00Z", "updated_at": "2025-04-01T00:00:00Z"}
    error = _refused(_apply(inst, "importer", upsert_op("upsert_record", _record(**backwards), "src-a")))
    assert advice.original_times_order_error(backwards["created_at"], backwards["updated_at"]) in error
    error = _refused(_apply(inst, "importer", upsert_op("upsert_record", _record(created_at="2099-01-01T00:00:00Z"),
                                                        "src-a")))
    assert "created_at 2099-01-01T00:00:00Z is after the apply time" in error
    error = _refused(_apply(inst, "importer", upsert_op("upsert_record", _record(created_at="2025-05-01"), "src-a")))
    assert "UTC timestamp" in error
    # One time alone stands for both.
    _ok(_apply(inst, "importer", upsert_op("upsert_record", _record(created_at="2025-05-01T00:00:00Z"), "src-a")))
    assert ro_rows(inst, "SELECT created_at, updated_at FROM records") == [("2025-05-01T00:00:00Z",) * 2]
