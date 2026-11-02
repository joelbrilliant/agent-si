"""Redaction attack tests, turned into assertions. Every name and number here is invented."""
from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from brain import backup, migrate, redact, redact_ops, scrub, store
from brain.applier import Applier, NotAllowed, Unavailable
from brain.cli import main
from tests.collector_fixtures import calendar_doc, collect, collector_instance, github_doc, gmail_doc, vanta_doc
from tests.conftest import context_connection, make_packet, ro_rows, source_op, upsert_op
from tests.phase2a import custom_instance, export, item, run_collect, sources
from tests.phase2b import (
    M, OPERATOR, PATH, REASON, REF, TEXT, bundle_members_hit, find_in, gate_conn, hot_journal, redact_now,
    redact_op, redact_packet, refuses, scan_instance, seed,
)
from tests.test_carry_forward import AGENTS, KEYS, claim
from tests.test_redact_op import KEYS2, S_TEXT, error_of, world
from tests.test_truth import Clock, _apply, _ok, clock  # noqa: F401


def db_holds(instance: Path) -> bool:
    return bool(find_in((instance / "brain.sqlite3").read_bytes()))


# -- The collectors must not write a redacted text back ---------------------------------------------

SUBJECT = f"Salary review for {M} confidential"


def test_br1_gmail_keeps_a_redacted_subject_redacted(tmp_path: Path, clock: Clock) -> None:
    clock.at("2026-10-08T00:00:00Z")
    instance = collector_instance(tmp_path, "gmail-work")
    pay = {"id": "thread-pay", "subject": SUBJECT, "messages": [
        {"at": "2026-10-02T09:00:00Z", "direction": "outbound"}, {"at": "2026-10-04T09:00:00Z", "direction": "inbound"}]}
    assert collect(instance, "gmail-work", gmail_doc(threads=[pay]))[0] == 0
    snapshot = ro_rows(instance, "SELECT id FROM sources WHERE kind = 'email'")[0][0]
    plan = redact.plan(instance, snapshot)
    assert plan["collectors"] == ["gmail-work"]
    assert "Written by collector gmail-work" in redact.render_plan(plan)
    redact.run(instance, snapshot, REASON, OPERATOR)
    assert ro_rows(instance, "SELECT subject FROM threads") == [("[redacted]",)]
    # the mailbox still holds the subject: a poll with one more thread, then a poll with a new message
    other = {"id": "thread-other", "subject": "Lunch", "messages": [{"at": "2026-10-05T09:00:00Z", "direction": "inbound"}]}
    assert collect(instance, "gmail-work", gmail_doc(threads=[pay, other]))[0] == 0
    newer = dict(pay, messages=pay["messages"] + [{"at": "2026-10-07T09:00:00Z", "direction": "inbound"}])
    clock.at("2026-10-08T03:00:00Z")
    assert collect(instance, "gmail-work", gmail_doc(fetched_at="2026-10-08T02:00:00Z", threads=[newer]))[0] == 0
    subjects = dict(ro_rows(instance, "SELECT external_id, subject FROM threads"))
    assert subjects["thread-pay"] == "[redacted]" and subjects["thread-other"] == "Lunch"
    assert ro_rows(instance, "SELECT last_inbound_at FROM threads WHERE external_id = 'thread-pay'") == [("2026-10-07T09:00:00Z",)]
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE instr(content, ?) > 0", (M,)) == [(0,)]
    # free pages hold the old text until VACUUM; the rows are what a poll controls
    redact.compact(instance / "brain.sqlite3", snapshot)
    assert not db_holds(instance)
    assert store.check_instance(instance) == []


def test_br1_calendar_keeps_a_redacted_title_redacted(tmp_path: Path, clock: Clock) -> None:
    clock.at("2026-10-08T00:00:00Z")
    instance = collector_instance(tmp_path, "work-cal")
    event = dict(calendar_doc()["events"][0], title=f"Pay review for {M}")
    assert collect(instance, "work-cal", calendar_doc(events=[event]))[0] == 0
    snapshot = ro_rows(instance, "SELECT id FROM sources WHERE instr(content, ?) > 0", (M,))[0][0]
    redact.run(instance, snapshot, REASON, OPERATOR)
    assert ro_rows(instance, "SELECT subject FROM threads") == [("[redacted]",)]
    assert ro_rows(instance, "SELECT title FROM meetings") == [("[redacted]",)]
    changed = dict(event, updated="2026-10-07T23:00:00Z", attendee_count=9)
    assert collect(instance, "work-cal", calendar_doc(events=[changed]))[0] == 0
    assert ro_rows(instance, "SELECT subject FROM threads") == [("[redacted]",)]
    assert ro_rows(instance, "SELECT title, attendee_count FROM meetings") == [("[redacted]", 9)]
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE instr(content, ?) > 0", (M,)) == [(0,)]
    assert not db_holds(instance)
    assert store.check_instance(instance) == []


def test_br1_github_stores_no_free_text_and_recovers(tmp_path: Path, clock: Clock) -> None:
    clock.at("2026-10-08T00:00:00Z")
    instance = collector_instance(tmp_path, "repo")
    doc = github_doc(pushes=[{"branch": "main", "head_sha": "a" * 40, "at": "2026-10-07T00:30:00Z"}],
                     runs=[{"id": 9001, "branch": "main", "head_sha": "a" * 40, "status": "completed",
                            "conclusion": "success", "updated_at": "2026-10-07T02:00:00Z"},
                           {"id": 9002, "branch": f"fix/{M}", "head_sha": "b" * 40, "status": "completed",
                            "conclusion": "failure", "updated_at": "2026-10-07T01:00:00Z"}])
    assert collect(instance, "repo", doc)[0] == 0
    # nothing a branch name or a sha carries is ever written to a source
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE instr(content, ?) > 0", (M,)) == [(0,)]
    assert not db_holds(instance)
    snapshot = ro_rows(instance, "SELECT id FROM sources WHERE kind = 'api'")[0][0]
    redact.run(instance, snapshot, REASON, OPERATOR)
    assert ro_rows(instance, "SELECT status FROM facts WHERE key = 'eng.repo.ci_state'") == [("retired",)]
    clock.at("2026-10-08T02:00:00Z")
    assert collect(instance, "repo", dict(doc, fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    assert ro_rows(instance, "SELECT status, value_text FROM facts WHERE key = 'eng.repo.ci_state'") == [("current", "success")]
    assert not db_holds(instance)
    assert store.check_instance(instance) == []


def test_br1_vanta_does_not_store_test_names_again(tmp_path: Path, clock: Clock) -> None:
    clock.at("2026-10-08T00:00:00Z")
    instance = collector_instance(tmp_path, "compliance")
    failing = [{"id": "tst-0001", "name": f"Offboarding for {M} done", "status": "NEEDS_ATTENTION", "due": "2026-10-20",
                "link": "https://app.vanta.example/tests/tst-0001", "controls": ["AC-1"]}]
    doc = vanta_doc(failing_tests=failing)
    assert collect(instance, "compliance", doc)[0] == 0
    snapshot = ro_rows(instance, "SELECT id FROM sources WHERE instr(content, ?) > 0", (M,))[0][0]
    redact.run(instance, snapshot, REASON, OPERATOR)
    assert ro_rows(instance, "SELECT count(*) FROM facts WHERE status = 'retired'")[0][0] == 5
    clock.at("2026-10-08T02:00:00Z")
    assert collect(instance, "compliance", dict(doc, fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    # the facts come back with their counts, and the names stay out
    assert ro_rows(instance, "SELECT status, value_int FROM facts WHERE key = 'vendor.vanta.failing_tests'") == [("current", 1)]
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE instr(content, 'Offboarding') > 0") == [(0,)]
    assert not db_holds(instance)
    assert store.check_instance(instance) == []


def test_br1_vanta_stores_names_before_any_redaction(tmp_path: Path, clock: Clock) -> None:
    """The control: the names are evidence until a snapshot of the fact is redacted."""
    clock.at("2026-10-08T00:00:00Z")
    instance = collector_instance(tmp_path, "compliance")
    assert collect(instance, "compliance", vanta_doc())[0] == 0
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE instr(content, 'MFA on for every account') > 0") == [(1,)]


def test_br1_the_collectors_token_is_the_scrub_token() -> None:
    from brain.collectors import base

    assert base.REDACTED == scrub.REDACTED


# -- ANALYZE, the pre-flight and the committed line --------------------------------------------------

def test_br2_analyze_does_not_block_check_backup_restore_or_redact(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    instance, sid = fixture.instance, fixture.source_id
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.execute("ANALYZE")
    con.commit()
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_schema WHERE name LIKE 'sqlite_stat%'")}
    con.close()
    assert "sqlite_stat1" in names
    assert store.check_instance(instance) == []
    made = backup.backup(instance, tmp_path / "out")
    bundle = Path(made["bundle"])
    restored = tmp_path / "restored"
    assert backup.restore(bundle, restored)["status"] == "restored"  # the bundle holds no planner statistics
    assert not [r for r in ro_rows(restored, "SELECT name FROM sqlite_schema WHERE name LIKE 'sqlite_stat%'")]
    assert store.check_instance(restored) == []
    redact.run(instance, sid, REASON, OPERATOR)
    assert scan_instance(instance) == {}
    assert store.check_instance(instance) == []
    # a bundle that carries statistics is still refused by restore
    from tests.test_backup import _tamper, refused

    hostile = _tamper(tmp_path, bundle, "stat", lambda c: c.execute("ANALYZE"))
    refused(hostile, tmp_path / "t-stat", "restore refused: bundle schema differs from the engine's: table sqlite_stat1 is not in the engine's schema")


def test_br2_a_foreign_object_is_refused_before_anything_commits(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.execute("CREATE TABLE loot (x TEXT)")
    con.commit()
    con.close()
    before = sorted(p.name for p in (instance / "backups").iterdir())
    with pytest.raises(NotAllowed) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value).startswith("redact refused: brain check reports: schema: table loot is not in the engine's schema; ")
    assert not (instance / "REDACTING").exists()
    assert ro_rows(instance, "SELECT count(*) FROM write_log WHERE change_id LIKE 'brain-redact-%'") == [(0,)]
    assert ro_rows(instance, "SELECT title FROM sources WHERE id = ?", (sid,)) != [("redacted",)]
    assert sorted(p.name for p in (instance / "backups").iterdir()) == before
    # a leftover copy of a redacted source and an unfinished redaction do not block the rerun that cleans them
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.execute("DROP TABLE loot")
    con.commit()
    con.close()
    redact.run(instance, sid, REASON, OPERATOR)


def test_br2_a_failed_bundle_step_says_the_redaction_committed(tmp_path: Path, clock: Clock, monkeypatch, capsys) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    real = backup.backup

    def refuse(*args, **kwargs):
        raise backup.Refused("backup refused: brain check found problems: schema: something odd")

    monkeypatch.setattr(redact.backup, "backup", refuse)
    with pytest.raises(Unavailable) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == f"redaction of {sid} is committed; run brain redact {sid} again"
    assert "backup refused: brain check found problems: schema: something odd" in capsys.readouterr().err
    assert (instance / "REDACTING").exists()
    monkeypatch.setattr(redact.backup, "backup", real)
    redact.run(instance, sid, REASON, OPERATOR)
    assert not (instance / "REDACTING").exists() and scan_instance(instance) == {}


# -- Threads and meetings edited by another source ---------------------------------------------------

def test_br3_a_thread_and_a_meeting_edited_by_another_source_lose_the_text(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    thread = _apply(instance, "meta", upsert_op("upsert_thread", {"id": "thr-vault", "last_inbound_at": "2026-10-08T00:00:00Z"}, "src-other", locator="thread t-18f2", base=1))
    meeting = _apply(instance, "tester", upsert_op("upsert_meeting", {"id": "mtg-vault", "status": "tentative"}, "src-other", base=1))
    assert thread.receipt["status"] == "applied" and meeting.receipt["status"] == "applied"
    assert ro_rows(instance, "SELECT source_id FROM threads WHERE id = 'thr-vault'") == [("src-other",)]
    assert ro_rows(instance, "SELECT source_id FROM meetings WHERE id = 'mtg-vault'") == [("src-other",)]
    plan = redact.plan(instance, fixture.source_id)
    assert plan["rows"]["meetings"] == 1
    result = redact_now(fixture)
    assert ro_rows(instance, "SELECT subject FROM threads WHERE id = 'thr-vault'") == [("[redacted]",)]
    assert ro_rows(instance, "SELECT title FROM meetings WHERE id = 'mtg-vault'") == [("[redacted]",)]
    assert result["rows"]["meetings"] == 1
    assert scan_instance(instance) == {}
    assert store.check_instance(instance) == []


# -- A redacted text under another id ----------------------------------------------------------------

def test_br4_a_redacted_text_is_refused_under_any_id(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    original = ro_rows(instance, "SELECT content FROM sources WHERE id = ?", (sid,))[0][0]  # the stored text, header and all
    assert M in original
    redact.run(instance, sid, REASON, OPERATOR)
    copy = _apply(instance, "tester", {"op": "add_source", "row": {
        "id": "src-copy", "kind": "document", "title": f"Copy {M}", "source_date": "2026-10-01", "content": original, "uri": f"https://x/{M}"}})
    assert copy.receipt["status"] == "rejected"
    assert error_of(copy) == f"op 0 (add_source): this text was redacted (source {sid})"
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id = 'src-copy'") == [(0,)]
    stored = ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (copy.receipt["change_id"],))[0][0]
    assert M not in stored and json.loads(stored)["redacted_ops"] == [0]
    # a whole batch is refused, and a text that was never redacted, or that differs by a character, is not
    batch = _apply(instance, "tester", source_op("src-fine", "A note nobody redacted.", kind="document"),
                   {"op": "add_source", "row": {"id": "src-copy2", "kind": "document", "title": "t", "source_date": "2026-10-01", "content": original}})
    assert batch.receipt["status"] == "rejected" and "this text was redacted" in error_of(batch)
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id = 'src-fine'") == [(0,)]
    assert _apply(instance, "tester", source_op("src-fine", "A note nobody redacted.", kind="document")).receipt["status"] == "applied"
    assert _apply(instance, "tester", source_op("src-near", original + " ", kind="document")).receipt["status"] == "applied"
    # the re-sent original (same id, same hash) is still the no-op, and `brain check` and a backup stay clean
    again = _apply(instance, "tester", {"op": "add_source", "row": {
        "id": sid, "kind": "document", "title": "Notes", "source_date": "2026-10-07", "content": original, "external_ref": REF}})
    assert again.receipt["status"] == "applied"
    assert store.check_instance(instance) == []
    for outcome in (copy, batch, again):
        assert M not in ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (outcome.receipt["change_id"],))[0][0]
    # the limit of a hash: a copy that differs by one character is a different text, and is accepted
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id = 'src-near'") == [(1,)]


def test_br4_an_agents_own_source_called_redacted_is_not_a_tombstone(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    _ok(_apply(instance, "tester", source_op("src-forge", "Some forged words.", title="redacted", kind="document")))
    copy = _apply(instance, "tester", source_op("src-forge2", "Some forged words.", kind="document"))
    assert copy.receipt["status"] == "applied"


def test_br4_the_refusal_is_one_the_check_accepts(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    redact_op(instance, "src-s")
    refused_copy = _apply(instance, "tester", source_op("src-again", S_TEXT, kind="document"))
    assert error_of(refused_copy) == "op 0 (add_source): this text was redacted (source src-s)"
    assert store.check_instance(instance) == []


# -- Unquoted copies are listed -----------------------------------------------------------------------

def test_br5_unquoted_verbatim_copies_are_listed_not_removed(tmp_path: Path, clock: Clock, capsys) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    for rid, body in (("rec-bt", f"The contact is `{M}` per the doc."), ("rec-gu", f"The contact is «{M}» per the doc."),
                      ("rec-nq", f"Per the doc: Contact {M} for the vault.")):
        assert _apply(instance, "tester", upsert_op("upsert_record", {"id": rid, "kind": "note", "title": "t " + rid, "body": body}, sid)).receipt["status"] == "applied"
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-fine", "kind": "note", "title": "Fine", "body": "The vault is audited every year by the team."}, "src-other")))
    plan = redact.plan(instance, sid)
    assert plan["residual"] == {"records": ["rec-bt", "rec-gu", "rec-nq"]}
    text = redact.render_plan(plan)
    assert "Still holds the text without quote marks after the scrub (listed, not removed): records rec-bt, rec-gu, rec-nq." in text
    assert M not in text
    capsys.readouterr()
    result = redact_now(fixture)
    assert result["residual"] == {"records": ["rec-bt", "rec-gu", "rec-nq"]}
    err = capsys.readouterr().err
    assert "rows still hold the removed text without quote marks (not removed): records rec-bt, rec-gu, rec-nq." in err
    # reported only: the rows keep the text, and the person deals with them
    assert [r[0] for r in ro_rows(instance, "SELECT id FROM records WHERE instr(body, ?) > 0 ORDER BY id", (M,))] == ["rec-bt", "rec-gu", "rec-nq"]
    assert store.check_instance(instance) == []
    # a rerun has no text left to compare with, and says nothing
    assert redact.run(instance, sid, REASON, OPERATOR)["residual"] == {}


def test_br5_a_clean_fixture_lists_nothing(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    assert redact.plan(fixture.instance, fixture.source_id)["residual"] == {}
    assert redact_now(fixture)["residual"] == {}


# -- Many claims on one key -------------------------------------------------------------------------------

def many_claims(instance: Path, n: int) -> None:
    con = context_connection(instance)
    now = "2026-10-01T00:00:00Z"
    text = "Some document text. " * 50
    con.execute("PRAGMA foreign_keys = ON")
    # the insert trigger rebuilds the key's search document per claim (quadratic): load without it, rebuild once
    trigger = con.execute("SELECT sql FROM sqlite_schema WHERE name = 'search_claims_ai'").fetchone()[0]
    con.execute("DROP TRIGGER search_claims_ai")
    con.execute(
        "INSERT INTO sources (id, kind, title, source_date, content, content_sha256, captured_at, captured_by) "
        "VALUES ('src-one', 'document', 'one', '2026-09-01', ?, ?, ?, 'bulk:x')", (text, store.sha256_text(text), now))
    con.execute("INSERT INTO fact_keys (key, value_type, source_id, locator, version, created_at, updated_at, updated_by) "
                "VALUES ('many.motto', 'text', 'src-one', 'x', 1, ?, ?, 'bulk:x')", (now, now))
    for j in range(n):
        con.execute(
            "INSERT INTO claims (id, key, scope, value_type, value_text, value_raw, status, basis, as_of, source_id, locator, quote, "
            "checked, supersedes, proposed_by, created_at, created_by, updated_at, updated_by) "
            "VALUES (?, 'many.motto', ?, 'text', ?, ?, 'superseded', 'reported', '2026-09-01', 'src-one', 'p1', ?, 1, '[]', 'bulk', ?, 'bulk:x', ?, 'bulk:x')",
            (f"clm-many-{j}", f"s{j}", f"value {j}", f"value {j}", ("quote text number %d " % j) * 5, now, now))
    con.execute(trigger)
    store.rebuild_search(con)
    con.commit()
    con.close()


def test_br6_ten_thousand_claims_on_one_key_redact_in_seconds(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    many_claims(instance, 10000)
    fresh = custom_instance(tmp_path / "fresh", agents=AGENTS, fact_keys=KEYS)
    original = ro_rows(fresh, "SELECT sql FROM sqlite_schema WHERE name = 'search_claims_au'")
    started = time.perf_counter()
    outcome = redact_op(instance, "src-one")
    elapsed = time.perf_counter() - started
    print(f"10,000 claims on one key: {elapsed:.2f} s")
    assert outcome.receipt["status"] == "applied" and outcome.integrity == "ok"
    assert elapsed < 10, elapsed
    assert sum(1 for t in outcome.receipt["targets"] if t["table"] == "claims") == 10000
    assert ro_rows(instance, "SELECT sql FROM sqlite_schema WHERE name = 'search_claims_au'") == original  # stored SQL restored
    assert store.check_instance(instance) == []


def search_rows(instance: Path):
    return ro_rows(instance, "SELECT rowid, entity, ref_id, status, title, body FROM search_index ORDER BY rowid")


def test_br6_the_one_refresh_leaves_the_same_search_index_as_the_trigger(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    results = []
    for name, swap in (("trigger", 10**9), ("swap", 0)):
        instance = custom_instance(tmp_path / name, agents=AGENTS, fact_keys=KEYS)
        many_claims(instance, 40)
        monkeypatch.setattr(redact_ops, "TRIGGER_SWAP_MIN", swap)
        assert redact_op(instance, "src-one").receipt["status"] == "applied"
        results.append(search_rows(instance))
        assert store.check_instance(instance) == []
    assert results[0] == results[1]


def test_br6_a_failed_redaction_puts_the_trigger_back(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    many_claims(instance, 30)
    original = ro_rows(instance, "SELECT sql FROM sqlite_schema WHERE name = 'search_claims_au'")
    monkeypatch.setattr(redact_ops, "TRIGGER_SWAP_MIN", 5)
    monkeypatch.setattr(redact_ops.store, "redaction_problems", lambda con, **kwargs: ["forced"])
    outcome = redact_op(instance, "src-one")
    assert outcome.receipt["status"] == "rejected" and "the scrub would leave text behind: forced" in error_of(outcome)
    assert ro_rows(instance, "SELECT sql FROM sqlite_schema WHERE name = 'search_claims_au'") == original
    assert ro_rows(instance, "SELECT count(*) FROM claims WHERE quote = '[redacted]'") == [(0,)]
    # the next writer opens the store and a claim update still re-indexes its fact
    monkeypatch.undo()
    assert store.check_instance(instance) == []
    _ok(_apply(instance, "tester", source_op("src-two", "The Pro plan includes 25 seats.", kind="document")))
    claim(instance, "bot", "clm-after", "demo.seats", "25", "The Pro plan includes 25 seats", "src-two")
    assert store.check_instance(instance) == []


# -- The reason may not repeat the title or the uri ------------------------------------------------------------

def test_br7_the_reason_may_not_repeat_the_title_or_the_uri(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    row = {"id": "src-t", "kind": "document", "title": "Quarterly redundancy plan for Pat Example", "source_date": "2026-10-01",
           "author_person_id": None, "content": "Some unrelated body text that has nothing to say.",
           "uri": "https://files.example.com/hr/pat-example-redundancy-letter.pdf"}
    _ok(_apply(instance, "tester", {"op": "add_source", "row": row}))
    for n, (reason, span) in enumerate((
        ("remove quarterly redundancy plan for Pat Example", "characters 8 to 24"),
        ("remove https://files.example.com/hr/pat-example-redundancy-letter.pdf", "characters 8 to 27"),
    ), start=1):
        outcome = redact_op(instance, "src-t", reason=reason, n=n)
        assert error_of(outcome) == f"op 0 (redact_source): the reason repeats text from source src-t ({span} of the reason)"
        assert "Pat" not in error_of(outcome)
    assert ro_rows(instance, "SELECT title FROM sources WHERE id = 'src-t'") == [("Quarterly redundancy plan for Pat Example",)]
    assert redact_op(instance, "src-t", reason="privacy request 2026-11-02", n=3).receipt["status"] == "applied"


# -- The disposition covers the live schema ----------------------------------------------------------------------

def undecided(con) -> list[str]:
    missing = []
    for (table,) in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if table.startswith("search_index"):
            continue
        for _cid, column, ctype, *_rest in con.execute(f"PRAGMA table_info({table})"):
            if ctype.upper() in ("TEXT", "BLOB", "ANY", "") and f"{table}.{column}" not in scrub.DISPOSITION:
                missing.append(f"{table}.{column}")
    return missing


def test_br8_every_text_column_of_the_live_schema_has_a_decision(tmp_path: Path) -> None:
    instance = custom_instance(tmp_path)
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        assert undecided(con) == [], "add every new text column to scrub._KEPT_COLUMNS, _TOKEN_COLUMNS or _SPAN_COLUMNS"
        # the check sees a column a later migration adds, whatever FIELD_CLASS says
        con.execute("ALTER TABLE records ADD COLUMN note_extra TEXT NOT NULL DEFAULT ''")
        assert undecided(con) == ["records.note_extra"]
        con.execute("ALTER TABLE records ADD COLUMN blob_extra BLOB")
        assert undecided(con) == ["records.note_extra", "records.blob_extra"]
    finally:
        con.close()


def test_br8_no_column_is_typed_beyond_text_integer_and_real(tmp_path: Path) -> None:
    instance = custom_instance(tmp_path)
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        types = set()
        for (table,) in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
            if table.startswith("search_index"):
                continue
            types |= {row[2].upper() for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()
    assert types <= {"TEXT", "INTEGER", "REAL"}, types


# -- Further redaction checks ------------------------------------------------------------------------------------------------------

def test_br10_an_old_store_is_refused_before_the_plan(tmp_path: Path, capsys) -> None:
    instance = custom_instance(tmp_path)
    old = tmp_path / "v5"
    old.mkdir()
    migrate.migrate(old / "brain.sqlite3", create=True, upto=5)
    for name in ("brain.toml", "ROUTER.md"):
        (old / name).write_bytes((instance / name).read_bytes())
    code = main(["redact", "src-a", "--reason", REASON, "--instance", str(old), "--yes"])
    captured = capsys.readouterr()
    assert code == 5 and captured.out == ""
    assert captured.err == f"brain: store is at version 5, engine expects {migrate.latest_version()}; run brain migrate\n"
    assert not (old / "REDACTING").exists()


def test_br11_check_with_a_hot_journal_prints_no_read_errors(tmp_path: Path, clock: Clock, capsys) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    hot_journal(fixture.instance / "brain.sqlite3")
    code = main(["check", "--instance", str(fixture.instance)])
    captured = capsys.readouterr()
    assert code == 1
    assert "could not read" not in captured.err
    assert "store has a hot journal: run brain redact <id> again, or any brain write, to recover it" in json.loads(captured.out)["problems"]


# -- the reviewer's attacks, as assertions -----------------------------------------------------------------------------

def counts(instance: Path) -> dict[str, int]:
    return {t: ro_rows(instance, f"SELECT count(*) FROM {t}")[0][0]
            for t in ("sources", "claims", "facts", "fact_history", "write_log", "signals", "records", "record_history", "collector_runs")}


def test_attack_twice_and_partly(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    redact_now(fixture)
    first = counts(instance)
    snapshot = ro_rows(instance, "SELECT id, title, content FROM sources ORDER BY id")
    second = redact_now(fixture, reason="second request 2026-11-03")
    assert not any(second["rows"][t] for t in ("claims", "facts", "fact_history", "signals", "sources", "records"))
    after = counts(instance)
    assert {k: after[k] - first[k] for k in first} == {**{k: 0 for k in first}, "write_log": 1}
    assert ro_rows(instance, "SELECT id, title, content FROM sources ORDER BY id") == snapshot
    # a changed document captured after the redaction supersedes the tombstone; redacting the new head rewrites only it
    clock.at("2026-10-12T01:00:00Z")
    code, payload, err = run_collect(instance, export([item(PATH, TEXT + " Added line with new content.", date="2026-10-11")], fetched_at="2026-10-12T00:00:00Z"))
    assert code == 0, (payload, err)
    ids = [r[0] for r in sources(instance, REF)]
    third = redact.run(instance, ids[-1], "third request 2026-11-04", OPERATOR)
    assert third["rows"]["sources"] == 1 and third["rows"]["write_log"] <= 2
    assert redact.run(instance, ids[0], "fourth request 2026-11-05", OPERATOR)["rows"]["sources"] == 0
    assert store.check_instance(instance) == []
    assert scan_instance(instance) == {}
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]


def test_attack_the_gate_open_probes(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    con = gate_conn(instance, sid)
    for message, sql, params in (
        ("sources never change", "UPDATE sources SET title='redacted', content=(SELECT tombstone FROM redaction_gate), uri='', author_person_id=NULL, captured_by='x' WHERE id=?", (sid,)),
        ("sources never change", "UPDATE sources SET title='redacted', content=(SELECT tombstone FROM redaction_gate), uri='', author_person_id=NULL WHERE id='src-other'", ()),
        ("claims never change", "UPDATE claims SET value_text='[redacted]' WHERE id='clm-seats'", ()),
        ("claims never change", "UPDATE claims SET value_text='[redacted]' WHERE id='clm-site'", ()),
        ("claims never change", "UPDATE claims SET value_text='https://redacted.invalid/' WHERE id='clm-seats'", ()),
        ("claims never change", "UPDATE claims SET value_text='https://redacted.invalid/' WHERE id='clm-motto'", ()),
        ("claims never change", "UPDATE claims SET quote='[redacted]' WHERE id='clm-seats2'", ()),
        ("claims never change", "UPDATE claims SET quote='' WHERE id='clm-seats'", ()),
        ("claims never change", "UPDATE claims SET value_raw=NULL WHERE id='clm-seats'", ()),
        ("CHECK constraint failed", "UPDATE claims SET quote='[redacted]', status='rejected' WHERE id='clm-seats'", ()),
        ("append-only", "UPDATE fact_history SET quote='' WHERE source_id=?", (sid,)),
        ("append-only", "UPDATE fact_history SET value_text='[redacted]' WHERE source_id=? AND value_type='int'", (sid,)),
        ("append-only", "UPDATE record_history SET kind='decision' WHERE record_id='rec-vault'", ()),
        ("append-only", "UPDATE collector_runs SET error='' WHERE instr(error, ?) > 0", (sid,)),
    ):
        assert message in refuses(con, sql, params), sql
    for column, value in (("status", "'rejected'"), ("hold_reason", "'x'"), ("applied_seq", "applied_seq+1"), ("targets_json", "'[]'")):
        assert "decided change is final" in refuses(
            con, f"UPDATE write_log SET {column}={value} WHERE status='applied' AND instr(packet_json, ?) > 0", (sid,))
    assert "identity columns never change" in refuses(con, "UPDATE write_log SET packet_json='{}' WHERE status='held'")
    con.execute("ROLLBACK")
    con.close()


def test_attack_a_forged_tombstone_is_not_one(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    redact_now(fixture)
    forged = _apply(instance, "tester", source_op("src-forge", "redacted on 2026-10-12 by Alex Example: privacy", title="redacted", kind="document"))
    assert forged.receipt["status"] == "applied"
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        assert store.tombstone_ids(con) == [fixture.source_id]
    finally:
        con.close()
    assert store.check_instance(instance) == []


def test_attack_an_unchecked_note_edited_by_another_source(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    rec = {"id": "rec-legacy", "body": f'Alex said "Contact {M} for the vault" but "this other quote is not in the source at all". Edited by audit.'}
    assert _apply(instance, "importer", upsert_op("upsert_record", rec, "src-other", base=1)).receipt["status"] == "applied"
    redact_now(fixture)
    assert M not in ro_rows(instance, "SELECT sentences FROM unchecked_text")[0][0]
    assert M not in ro_rows(instance, "SELECT body FROM records WHERE id = 'rec-legacy'")[0][0]
    assert scan_instance(instance) == {}
    assert store.check_instance(instance) == []


def test_attack_no_other_encoding_of_the_marker_survives(tmp_path: Path, clock: Clock) -> None:
    import base64
    import io
    import tarfile
    import urllib.parse

    fixture = seed(tmp_path, clock)
    redact_now(fixture)
    blob = b""
    for path in fixture.instance.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".tar.gz"):
            with tarfile.open(path, "r:gz") as tar:
                for member in tar:
                    blob += tar.extractfile(member).read()
        else:
            blob += path.read_bytes()
    low = blob.lower()
    for sentence in ("Contact Zq7MarkerAlexExampleSecret0412555019xYzW for the vault",
                     "https://example.com/Zq7MarkerAlexExampleSecret0412555019xYzW/start"):
        for i in range(len(sentence) - 13):
            assert sentence[i : i + 14].lower().encode() not in low, sentence[i : i + 14]
    for form in (M.encode("utf-16-le"), base64.b64encode(M.encode()), urllib.parse.quote(M).encode(), M.encode().hex().encode()):
        assert form not in blob and form.lower() not in low


def test_attack_no_column_holds_a_fragment_unique_to_the_source(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    redact_now(fixture)
    con = sqlite3.connect(f"file:{fixture.instance / 'brain.sqlite3'}?mode=ro", uri=True)
    try:
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'search_index_%'").fetchall():
            for column in [r[1] for r in con.execute(f"PRAGMA table_info({table})")]:
                for fragment in ("costs 49 aud a seat", "nothing else matters here", "jade said", "docs live at", "contact zq7", "example.com/zq7"):
                    n = con.execute(f"SELECT count(*) FROM {table} WHERE instr(lower(cast({column} AS text)), ?) > 0", (fragment,)).fetchone()[0]
                    assert n == 0, (table, column, fragment)
    finally:
        con.close()


def hold_write_lock(db: Path, seconds: float):
    code = textwrap.dedent(
        f"""
        import sqlite3, time
        con = sqlite3.connect({str(db)!r}, isolation_level=None)
        con.execute("BEGIN IMMEDIATE")
        print("locked", flush=True)
        time.sleep({seconds})
        con.execute("ROLLBACK")
        """
    )
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    return process


def test_attack_a_running_backup_blocks_redact_but_not_a_write(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    instance = fixture.instance
    handle = backup.open_lock(instance)
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    try:
        with pytest.raises(Unavailable) as err:
            redact_now(fixture)
        assert str(err.value) == "a backup or another redaction is running; try again when it finishes"
        assert not (instance / "REDACTING").exists() and ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
        assert _apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-z", "kind": "note", "title": "t", "body": "b"}, "src-other")).receipt["status"] == "applied"
    finally:
        handle.close()


def test_attack_a_busy_writer_for_a_few_seconds_only_delays_the_redaction(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    holder = hold_write_lock(fixture.instance / "brain.sqlite3", 2)
    started = time.time()
    result = redact_now(fixture)
    holder.wait()
    assert result["status"] == "redacted" and time.time() - started >= 1.5
    assert scan_instance(fixture.instance) == {} and store.check_instance(fixture.instance) == []


def test_attack_a_writer_holding_the_store_past_the_timeout_leaves_nothing_behind(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    instance = fixture.instance
    real = redact._write_marker
    state = {}

    def marker_then_writer(*args, **kwargs):
        real(*args, **kwargs)
        state["holder"] = hold_write_lock(instance / "brain.sqlite3", 12)  # the transaction cannot begin for 10 s

    monkeypatch.setattr(redact, "_write_marker", marker_then_writer)
    with pytest.raises(Unavailable):
        redact_now(fixture)
    state["holder"].wait()
    assert not (instance / "REDACTING").exists() and ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
    assert ro_rows(instance, "SELECT title FROM sources WHERE id = ?", (fixture.source_id,)) != [("redacted",)]
    assert store.check_instance(instance) == []
    monkeypatch.undo()
    assert redact_now(fixture)["status"] == "redacted"


def test_attack_a_writer_between_the_commit_and_vacuum(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    """A writer takes the store right before VACUUM: the compaction's busy wait is shortened here, so the retry is what
    gets it through."""
    fixture = seed(tmp_path, clock, bundles=False)
    instance = fixture.instance
    real_vacuum, real_open = redact._vacuum, redact._open_compact
    state = {"sleeps": []}

    def vacuum(con, source_id):
        state["holder"] = hold_write_lock(instance / "brain.sqlite3", 1.5)
        real_vacuum(con, source_id)

    def quick(db):
        con = sqlite3.connect(str(db), isolation_level=None, timeout=0.2)
        con.execute("PRAGMA temp_store = MEMORY")
        return con

    monkeypatch.setattr(redact, "_vacuum", vacuum)
    monkeypatch.setattr(redact, "_open_compact", quick)
    monkeypatch.setattr(redact, "_sleep", lambda seconds: (state["sleeps"].append(seconds), time.sleep(0.5)))
    result = redact_now(fixture)
    state["holder"].wait()
    assert result["status"] == "redacted" and state["sleeps"] and scan_instance(instance) == {}


def test_attack_restoring_an_older_bundle_is_the_documented_limit(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    older = sorted(fixture.outside.iterdir())[0]
    redact_now(fixture)
    restored = tmp_path / "restored"
    assert backup.restore(older, restored)["status"] == "restored"
    assert ro_rows(restored, "SELECT count(*) FROM sources WHERE title = 'redacted'") == [(0,)]
    assert find_in((restored / "brain.sqlite3").read_bytes())
    assert store.check_instance(restored) == []
    redact.run(restored, fixture.source_id, REASON, OPERATOR)  # the runbook's answer: run the command again
    assert scan_instance(restored) == {}


def test_attack_the_plan_and_the_receipt_agree_outside_the_derived_rows(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    plan = redact.plan(fixture.instance, fixture.source_id)
    result = redact_now(fixture)
    for table, count in plan["rows"].items():
        if table not in ("facts", "fact_history"):
            assert result["rows"][table] == count, table
