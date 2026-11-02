"""The principal's own lines in a thread or transcript count as hers, and nobody else's do.

The 1.8.0 author fix left every meeting transcript and mixed email or Slack thread without an author,
because several people speak in them. That closed the old hole, where anyone's line passed as the principal's, but
it also refused a quote of her own line. A speaker-lines excerpt holds only her lines, each copied exactly, as a
source she wrote with external_ref speaker-lines:<thread id>, and the gate reads it whenever the thread is cited.
Every name and text here is invented."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.applier import Applier
from tests.conftest import make_packet, ro_rows, source_op, upsert_op
from tests.test_truth import _apply, _err, _ok

THREAD = "src-meeting-seed-swap"
LINES = "src-meeting-seed-swap-jade-lines"
TRANSCRIPT = (
    "Garden working party notes.\n"
    "Jade: We should keep the seed swap on the Saturday table.\n"
    "Dana Okafor: No, move the seed swap to Sunday after the gate closes.\n"
    "Jade: Fine then, we can bring the tins after it."
)
HER_LINES = "We should keep the seed swap on the Saturday table.\n\nFine then, we can bring the tins after it."


def lines_op(content: str = HER_LINES, *, sid: str = LINES, of: str = THREAD, author="per-jade") -> dict:
    op = source_op(sid, content, kind="meeting_transcript", author=author, title="Jade's lines: working party")
    op["row"]["external_ref"] = "speaker-lines:" + of
    return op


def said(record_id: str, quote: str, *, cite: str = THREAD) -> dict:
    row = {"id": record_id, "kind": "note", "title": "Seed swap", "body": f'Jade said "{quote}".'}
    return upsert_op("upsert_record", row, cite)


@pytest.fixture
def call(instance: Path) -> Path:
    _ok(_apply(
        instance, "tester",
        source_op("src-note-why", "Jade's own lines from the working party, copied exactly."),
        upsert_op("upsert_person", {"id": "per-dana-okafor", "display_name": "Dana Okafor"}, "src-note-why",
                  locator="seed"),
        source_op(THREAD, TRANSCRIPT, kind="meeting_transcript"),
    ))
    return instance


def test_her_own_line_passes_once_her_lines_are_saved(call: Path) -> None:
    """A transcript with no author refuses a quote of her own line until her lines are saved."""
    _err(_apply(call, "tester", said("rec-before", "keep the seed swap on the Saturday table")))
    _ok(_apply(call, "tester", lines_op()))
    _ok(_apply(call, "tester", said("rec-after", "keep the seed swap on the Saturday table")))


def test_another_speakers_line_in_the_same_thread_still_fails(call: Path) -> None:
    _ok(_apply(call, "tester", lines_op()))
    error = _err(_apply(call, "tester", said("rec-dana", "move the seed swap to Sunday after the gate")))
    assert "Jade" in error and "per-jade" in error


def test_a_quote_cannot_join_two_of_her_lines_across_someone_else(call: Path) -> None:
    """Read as one run of words, the excerpt would hold "the Saturday table Fine then we can bring", which she never
    said: Dana spoke between those lines."""
    _ok(_apply(call, "tester", lines_op()))
    _err(_apply(call, "tester", said("rec-joined", "on the Saturday table. Fine then, we can bring")))
    _ok(_apply(call, "tester", said("rec-second", "Fine then, we can bring the tins after it")))


def test_citing_her_lines_directly_reads_them_one_line_at_a_time(call: Path) -> None:
    _ok(_apply(call, "tester", lines_op()))
    _ok(_apply(call, "tester", said("rec-direct", "keep the seed swap on the Saturday table", cite=LINES)))
    _err(_apply(call, "tester", said("rec-direct-joined", "on the Saturday table. Fine then, we can bring", cite=LINES)))


def test_a_decision_she_made_can_quote_her_line_from_the_thread(call: Path) -> None:
    decision = {"id": "dec-seed-swap-saturday", "title": "Seed swap stays on Saturday",
                "quote": "We should keep the seed swap on the Saturday table", "decided_on": "2026-10-01",
                "decided_by_person_id": "per-jade", "status": "in_effect", "project_id": "general"}
    _err(_apply(call, "tester", upsert_op("upsert_decision", decision, THREAD)))
    _ok(_apply(call, "tester", lines_op()))
    _ok(_apply(call, "tester", upsert_op("upsert_decision", decision, THREAD)))


def test_a_line_must_be_copied_exactly_from_the_thread(call: Path) -> None:
    error = _err(_apply(call, "tester", lines_op(HER_LINES + "\n\nAnd we should invite the neighbours too.")))
    assert THREAD in error and "exactly" in error and "blank line" in error
    error = _err(_apply(call, "tester", lines_op("We should keep the seed swap on Saturday.")))
    assert THREAD in error


def test_her_lines_must_come_from_a_stored_thread_that_isnt_itself_lines(call: Path) -> None:
    error = _err(_apply(call, "tester", lines_op(of="src-meeting-nowhere")))
    assert "src-meeting-nowhere" in error and "add_source" in error
    _ok(_apply(call, "tester", lines_op()))
    error = _err(_apply(call, "tester", lines_op(sid="src-lines-of-lines", of=LINES)))
    assert LINES in error


def test_only_an_author_principal_agent_saves_lines_in_her_name(call: Path) -> None:
    error = _err(_apply(call, "plain", lines_op()))
    assert "may not add a source authored by the principal" in error


def test_redacting_the_thread_redacts_her_lines_with_it(call: Path) -> None:
    _ok(_apply(call, "tester", lines_op()))
    writer = Applier(call, allow_reserved_agent=True)
    try:
        packet = make_packet("brain", {"op": "redact_source", "source_id": THREAD, "reason": "test removal",
                                       "operator": "tester"})
        _ok(writer.apply(json.dumps(packet).encode()))
    finally:
        writer.close()
    rows = dict(ro_rows(call, "SELECT id, content FROM sources WHERE id IN (?, ?)", (THREAD, LINES)))
    assert all("Saturday" not in text for text in rows.values()), rows
    assert ro_rows(call, "SELECT author_person_id FROM sources WHERE id = ?", (LINES,)) == [(None,)]


def test_the_excerpt_lookup_uses_the_partial_index(call: Path) -> None:
    """Without `external_ref <> ''` SQLite can't use the partial index and reads every source's text per cited source
    (found by review: 960 lookups took 11 seconds on 3,000 sources, inside the write lock)."""
    _ok(_apply(call, "tester", lines_op()))
    writer = Applier(call)
    seen: list[str] = []
    writer.con.set_trace_callback(seen.append)
    try:
        packet = make_packet("tester", said("rec-plan", "keep the seed swap on the Saturday table"))
        _ok(writer.apply(json.dumps(packet).encode()))
    finally:
        writer.con.set_trace_callback(None)
        writer.close()
    lookups = [sql for sql in seen if "speaker-lines:" in sql and sql.lstrip().upper().startswith("SELECT")]
    assert lookups
    for sql in lookups:
        plan = " ".join(str(row[-1]) for row in ro_rows(call, "EXPLAIN QUERY PLAN " + sql))
        assert "sources_by_external_ref" in plan, plan


def test_a_line_must_be_one_line_in_whole_words(call: Path) -> None:
    """A paragraph of two transcript lines would carry the next speaker's line into her lines, and a cut mid-word
    would make a quote she never said."""
    two = "We should keep the seed swap on the Saturday table.\nDana Okafor: No, move the seed swap to Sunday after"
    assert THREAD in _err(_apply(call, "tester", lines_op(two)))
    assert THREAD in _err(_apply(call, "tester", lines_op("ove the seed swap to Sunday after the gate")))
    assert THREAD in _err(_apply(call, "tester", lines_op("We should keep the seed swap on the Satur")))


def test_a_fact_quote_cannot_join_two_of_her_lines(call: Path) -> None:
    thread = "src-meeting-region"
    _ok(_apply(call, "tester",
               source_op(thread, "Planning call.\nJade: Our region for the pilot is au for now.\nDana Okafor: Then we "
                                 "need the Sydney rack.\nJade: Seats stay at forty this quarter.", kind="meeting_transcript"),
               lines_op("Our region for the pilot is au for now.\n\nSeats stay at forty this quarter.",
                        sid="src-meeting-region-jade-lines", of=thread)))

    def fact(quote: str) -> dict:
        row = {"key": "demo.region", "value": "au", "value_raw": "au", "quote": quote, "basis": "observed",
               "as_of": "2026-10-01", "last_verified_at": "2026-10-01T00:00:00Z"}
        return upsert_op("upsert_fact", row, "src-meeting-region-jade-lines")

    def claim(cid: str, quote: str) -> dict:
        row = {"id": cid, "key": "demo.region", "value_raw": "au", "basis": "reported", "quote": quote}
        return {"op": "add_claim", "row": row, "source": {"source_id": "src-meeting-region-jade-lines", "locator": "p1"}}

    joined = "region for the pilot is au for now. Seats stay"
    assert "quote is not in source" in _err(_apply(call, "tester", fact(joined)))
    assert "quote is not in source" in _err(_apply(call, "tester", claim("clm-region-joined", joined)))
    _ok(_apply(call, "tester", fact("Our region for the pilot is au for now")))
    _ok(_apply(call, "tester", claim("clm-region-line", "Our region for the pilot is au for now")))


def test_redacting_her_lines_alone_leaves_the_thread_and_a_later_redaction_takes_both(call: Path) -> None:
    from brain import store

    _ok(_apply(call, "tester", lines_op()))

    def redact(source_id: str) -> None:
        writer = Applier(call, allow_reserved_agent=True)
        try:
            packet = make_packet("brain", {"op": "redact_source", "source_id": source_id, "reason": "test removal",
                                           "operator": "tester"})
            _ok(writer.apply(json.dumps(packet).encode()))
        finally:
            writer.close()

    redact(LINES)
    rows = dict(ro_rows(call, "SELECT id, content FROM sources WHERE id IN (?, ?)", (THREAD, LINES)))
    assert "Saturday" in rows[THREAD] and "Saturday" not in rows[LINES]
    redact(THREAD)
    rows = dict(ro_rows(call, "SELECT id, content FROM sources WHERE id IN (?, ?)", (THREAD, LINES)))
    assert all("Saturday" not in text for text in rows.values())
    con = store.connect_readonly(call / "brain.sqlite3")
    try:
        assert store.redaction_problems(con) == []
    finally:
        con.close()
