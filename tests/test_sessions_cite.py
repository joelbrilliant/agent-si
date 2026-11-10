"""A fact can quote a past session, and only what the session really says.

brain sessions excerpt prints an add_source op holding messages word for word. The applier checks it against the
session record, so an edited excerpt is refused, and then the evidence gate treats it like any source: a real quote
passes and a made-up one is refused. An excerpt has no author, so it never lets an agent put words in the
principal's mouth. Every transcript is invented.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from brain import advice, cli
from brain.config import load_config
from brain.sessions import cite, record
from brain.sessions.adapters import SessionRoots
from tests.conftest import TEST_CONFIG, make_packet, ro_rows, upsert_op
from tests.test_sessions_record import claude_row, jsonl

SESSION = "claude:claude-kettle"


@pytest.fixture
def recorded(instance: Path, tmp_path: Path) -> Path:
    """The test brain with one invented session in its record."""
    root = tmp_path / "claude-projects"
    jsonl(root / "-work-office" / "claude-kettle.jsonl", [
        claude_row("k1", "user", "The office kettle has scale again, what did we decide about descaling it",
                   1_800_000_000, session="claude-kettle"),
        claude_row("k2", "assistant", "Descale the kettle with citric acid on the first Monday of each month.",
                   1_800_000_060, session="claude-kettle"),
        claude_row("k3", "user", "Good, and please keep the vinegar well away from that kettle.", 1_800_000_120,
                   session="claude-kettle"),
    ])
    roots = SessionRoots(claude_root=root, codex_root=tmp_path / "x", grok_root=tmp_path / "g",
                         hermes_db=tmp_path / "h.db")
    assert record.sync(record.path_for(instance), roots).new_messages == 3
    return instance


def excerpt(instance: Path, first: int = 2, last: int = 3, **kwargs) -> dict:
    return cite.excerpt_op(instance, load_config(instance / "brain.toml"), SESSION, first, last, **kwargs)


def note(body: str, source_id: str) -> dict:
    return upsert_op("upsert_record", {"id": "rec-kettle-care", "kind": "note", "title": "Kettle care", "body": body},
                     source_id)


def test_an_excerpt_is_the_sessions_words_and_a_real_quote_passes_the_gate(recorded, apply) -> None:
    op = excerpt(recorded)
    row = op["row"]
    assert row["content"] == (
        "[2] assistant 2027-01-15 08:01\nDescale the kettle with citric acid on the first Monday of each month.\n\n"
        "[3] user 2027-01-15 08:02\nGood, and please keep the vinegar well away from that kettle.\n\n"
    )
    assert row["external_ref"] == "session:claude:claude-kettle#2-3" and row["kind"] == "document"
    assert row["id"] == cite.source_id(SESSION, 2, 3) and row["id"].startswith("src-session-")
    assert row["title"].startswith("Session claude 2027-01-15: The office kettle has scale again")
    assert row["source_date"] == "2027-01-15" and "author_person_id" not in row
    outcome = apply(make_packet("tester", op, note(
        'The agreed routine is to "descale the kettle with citric acid on the first Monday of each month".', row["id"])))
    assert outcome.receipt["status"] == "applied", outcome.receipt
    stored = ro_rows(recorded, "SELECT kind, author_person_id, external_ref, content FROM sources WHERE id = ?",
                     (row["id"],))
    assert stored == [("document", None, row["external_ref"], row["content"])]
    # The same excerpt sent again later is the source it already is.
    again = apply(make_packet("tester", op, upsert_op(
        "upsert_record", {"id": "rec-kettle-vinegar", "kind": "note", "title": "No vinegar",
                          "body": 'The request was to "keep the vinegar well away from that kettle".'}, row["id"])))
    assert again.receipt["status"] == "applied", again.receipt


def test_a_made_up_quote_is_refused(recorded, apply) -> None:
    op = excerpt(recorded)
    outcome = apply(make_packet("tester", op, note(
        'The agreed routine is to "descale the kettle with vinegar every single Friday".', op["row"]["id"])))
    assert outcome.receipt["status"] == "rejected"
    assert f"is not in the cited source {op['row']['id']}" in outcome.receipt["error"]
    assert ro_rows(recorded, "SELECT count(*) FROM sources WHERE id = ?", (op["row"]["id"],)) == [(0,)]


def test_an_edited_excerpt_is_refused(recorded, apply) -> None:
    op = excerpt(recorded)
    edited = copy.deepcopy(op)
    edited["row"]["content"] = edited["row"]["content"].replace("citric acid", "vinegar")
    outcome = apply(make_packet("tester", edited, note(
        'The agreed routine is to "descale the kettle with vinegar on the first Monday".', op["row"]["id"])))
    assert outcome.receipt["status"] == "rejected"
    error = outcome.receipt["error"]
    assert "is not messages 2 to 3 of claude:claude-kettle as the session record holds them" in error
    assert "brain sessions excerpt claude:claude-kettle --from 2 --to 3" in error
    # Trimming or adding a line is editing too.
    for content in (op["row"]["content"].strip(), op["row"]["content"] + "[4] user 2027-01-15 08:03\nAnd more.\n\n"):
        changed = copy.deepcopy(op)
        changed["row"]["content"] = content
        assert apply(make_packet("tester", changed)).receipt["status"] == "rejected"


def test_an_excerpt_has_no_author_and_one_shape(recorded, apply) -> None:
    op = excerpt(recorded)
    for change in ({"author_person_id": "per-jade"}, {"kind": "user_note"}):
        shaped = copy.deepcopy(op)
        shaped["row"].update(change)
        outcome = apply(make_packet("tester", shaped))
        assert outcome.receipt["status"] == "rejected", change
        assert "a session excerpt is a source of kind document with no author_person_id" in outcome.receipt["error"]
    for ref in ("session:claude:claude-kettle", "session:claude:claude-kettle#3-", "session:kettle#1-2"):
        odd = copy.deepcopy(op)
        odd["row"]["external_ref"] = ref
        outcome = apply(make_packet("tester", odd))
        assert outcome.receipt["status"] == "rejected", ref
        assert "session:<session id>#<first>-<last>" in outcome.receipt["error"] or "is not a session id" in \
            outcome.receipt["error"]
    missing = copy.deepcopy(op)
    missing["row"]["external_ref"] = "session:claude:no-such-session#1-2"
    outcome = apply(make_packet("tester", missing))
    assert outcome.receipt["status"] == "rejected" and "has no session claude:no-such-session" in outcome.receipt["error"]
    past = copy.deepcopy(op)
    past["row"]["external_ref"] = "session:claude:claude-kettle#3-9"
    outcome = apply(make_packet("tester", past))
    assert outcome.receipt["status"] == "rejected" and "the session has 3 messages" in outcome.receipt["error"]


def test_a_session_quote_does_not_put_words_in_the_principals_mouth(recorded, apply) -> None:
    op = excerpt(recorded)
    outcome = apply(make_packet("tester", op, note(
        'Jade said "please keep the vinegar well away from that kettle".', op["row"]["id"])))
    assert outcome.receipt["status"] == "rejected"
    assert "Jade" in outcome.receipt["error"] and "per-jade" in outcome.receipt["error"]
    neutral = apply(make_packet("tester", op, note(
        'The session asked to "please keep the vinegar well away from that kettle".', op["row"]["id"])))
    assert neutral.receipt["status"] == "applied", neutral.receipt


def test_the_excerpt_stays_after_the_session_is_forgotten(recorded, apply) -> None:
    op = excerpt(recorded)
    assert apply(make_packet("tester", op)).receipt["status"] == "applied"
    assert record.forget(record.path_for(recorded), "claude", "claude-kettle") == 3
    later = apply(make_packet("tester", note(
        'The routine was to "descale the kettle with citric acid on the first Monday of each month".',
        op["row"]["id"])))
    assert later.receipt["status"] == "applied", later.receipt
    with pytest.raises(cite.CiteError, match="has no session claude:claude-kettle"):
        excerpt(recorded)


def test_excerpt_limits_name_their_fix(recorded) -> None:
    with pytest.raises(cite.CiteError, match="from must not be after to"):
        excerpt(recorded, 3, 2)
    with pytest.raises(cite.CiteError, match="the session has 3 messages"):
        excerpt(recorded, 1, 4)
    assert "at most 50 messages" in advice.session_range_error(1, 60, cite.MAX_MESSAGES)
    assert "take fewer messages" in advice.session_range_error(1, 2, cite.MAX_MESSAGES, too_long=True)
    assert excerpt(recorded, 1, 1, source="src-my-own-id")["row"]["id"] == "src-my-own-id"
    (recorded / "brain.toml").write_text(TEST_CONFIG + "\n[sessions]\nrecord = false\n", encoding="utf-8")
    with pytest.raises(cite.CiteError, match="session recording is off"):
        excerpt(recorded)


def test_recording_off_refuses_an_excerpt_at_the_applier(recorded, tmp_path) -> None:
    from brain.applier import Applier

    op = excerpt(recorded)
    (recorded / "brain.toml").write_text(TEST_CONFIG + "\n[sessions]\nrecord = false\n", encoding="utf-8")
    writer = Applier(recorded)
    try:
        outcome = writer.apply(json.dumps(make_packet("tester", op)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "rejected" and "session recording is off" in outcome.receipt["error"]


def test_brain_sessions_excerpt_prints_the_op_and_validate_gives_the_appliers_verdict(recorded, tmp_path, capsys) -> None:
    assert cli.main(["sessions", "excerpt", SESSION, "--from", "2", "--to", "3", "--instance", str(recorded)]) == 0
    op = json.loads(capsys.readouterr().out)
    assert op == excerpt(recorded)
    good = tmp_path / "good.json"
    good.write_text(json.dumps(make_packet("tester", op)), encoding="utf-8")
    assert cli.main(["validate", "packet", str(good), "--instance", str(recorded)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "applied"
    op["row"]["content"] = op["row"]["content"].replace("Monday", "Friday")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(make_packet("tester", op)), encoding="utf-8")
    assert cli.main(["validate", "packet", str(bad), "--instance", str(recorded)]) == 1
    assert "as the session record holds them" in json.loads(capsys.readouterr().out)["error"]
    assert ro_rows(recorded, "SELECT count(*) FROM sources WHERE id LIKE 'src-session-%'") == [(0,)]
    assert cli.main(["sessions", "excerpt", SESSION, "--from", "2", "--to", "9", "--instance", str(recorded)]) == 2
    assert "the session has 3 messages" in capsys.readouterr().err
    assert cli.main(["sessions", "excerpt", "kettle", "--from", "1", "--to", "1", "--instance", str(recorded)]) == 2
    assert "is not a session id" in capsys.readouterr().err
    # search shows a session with a message number on its end, and the commands take the id that way too.
    assert cli.main(["sessions", "excerpt", SESSION + "#2", "--from", "2", "--to", "3", "--instance", str(recorded)]) == 0
    assert json.loads(capsys.readouterr().out) == excerpt(recorded)
    assert cli.main(["sessions", "forget", SESSION + "#2", "--instance", str(recorded)]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "forgotten", "session": SESSION, "messages_removed": 3}
