"""One search over the brain and past sessions, and get on a session.

search keeps the brain's hits as they were and adds a short list of past sessions beside them. The record is read
only on the machine that holds it: a folder token and a hub connection never see it. Every transcript is invented.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import advice, reads, usage
from brain.sessions import asking_session, record
from tests.conftest import NOW, READ_CONFIG, call, read_instance
from tests.test_sessions_record import claude_row, codex_row, iso, jsonl

DAY = "2027-01-15"  # 1_800_000_000 in UTC, the read fixture's time zone


@pytest.fixture
def brain(tmp_path: Path, monkeypatch) -> Path:
    """The read fixture's brain, with three invented sessions in the folders the tools would keep them in."""
    claude = tmp_path / "claude-projects"
    codex = tmp_path / "codex-sessions"
    jsonl(claude / "-work-office" / "claude-kettle.jsonl", [
        claude_row("k1", "user", "The office kettle has scale again, what did we decide about descaling it",
                   1_800_000_000, session="claude-kettle"),
        claude_row("k2", "assistant", "Descale the kettle with citric acid on the first Monday of each month.",
                   1_800_000_060, session="claude-kettle"),
        claude_row("k3", "user", "Good, and keep the vinegar away from it.", 1_800_000_120, session="claude-kettle"),
        claude_row("k4", "assistant", "Noted: citric acid only, no vinegar near the kettle.", 1_800_000_180,
                   session="claude-kettle"),
    ])
    jsonl(claude / "-work-office" / "claude-badges.jsonl", [
        claude_row("b1", "user", "badgecanary which printer makes the new visitor badges", 1_800_090_000,
                   session="claude-badges"),
    ])
    jsonl(codex / "2027" / "rollout-codex-kettle.jsonl", [
        {"type": "session_meta", "payload": {"id": "codex-kettle", "timestamp": iso(1_800_200_000), "cwd": "/work"}},
        codex_row("x1", "user", "kettle descaling reminder script, please", 1_800_200_001),
        codex_row("x2", "assistant", "The kettle reminder script runs from cron each month.", 1_800_200_002),
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(claude))
    monkeypatch.setenv("CODEX_SESSIONS_ROOT", str(codex))
    return read_instance(tmp_path)


def local(instance: Path, tool: str, asking=None, **arguments):
    return reads.run_tool(instance, tool, arguments, now=NOW, local=True, asking=asking)


def session_of(place: str) -> str:
    """The session an id from search belongs to: the id without the message number on its end."""
    return place.partition("#")[0]


def test_search_adds_past_sessions_beside_the_brains_hits_and_leaves_the_hits_alone(brain) -> None:
    plain = call(brain, "search", query="kettle")  # not on this machine: the brain alone
    assert "sessions" not in plain.structured and not record.path_for(brain).exists()
    both = local(brain, "search", query="kettle")
    assert both.structured["hits"] == plain.structured["hits"]
    assert "rec-kettle" in [hit["id"] for hit in both.structured["hits"]]
    sessions = both.structured["sessions"]
    assert sorted(session_of(item["id"]) for item in sessions) == ["claude:claude-kettle", "codex:codex-kettle"]
    first = next(item for item in sessions if session_of(item["id"]) == "claude:claude-kettle")
    assert first["title"].startswith("The office kettle has scale again") and first["date"] == DAY
    assert len(first["title"]) <= 80
    # Beside brain hits a session is a lead: its one best line.
    assert len(first["excerpts"]) == 1 and set(first["excerpts"][0]) == {"n", "role", "text", "at"}
    # Its id ends in that line's message number, so get on the id opens the session there.
    assert first["id"] == f"claude:claude-kettle#{first['excerpts'][0]['n']}"
    assert first["excerpts"][0]["at"].startswith(DAY + " ")
    assert record.path_for(brain).is_file()
    # Words only the sessions hold: no brain hit, so the sessions are the answer and come as deep as a search of
    # sessions only.
    only = local(brain, "search", query="badgecanary printer")
    assert only.structured["hits"] == [] and [s["id"] for s in only.structured["sessions"]] == ["claude:claude-badges#1"]
    alone = local(brain, "search", query="vinegar Monday descaling")
    assert alone.structured["hits"] == []
    kettle = next(item for item in alone.structured["sessions"] if session_of(item["id"]) == "claude:claude-kettle")
    assert len(kettle["excerpts"]) == 3 and kettle["id"] == f"claude:claude-kettle#{kettle['excerpts'][0]['n']}"
    # Words nothing holds: the result has no sessions key at all, as before the record existed.
    assert "sessions" not in local(brain, "search", query="zzzunseen").structured


def test_a_search_of_sessions_only_goes_deeper(brain) -> None:
    result = local(brain, "search", query="kettle vinegar", entities=["session"])
    assert result.structured["hits"] == [] and result.structured["match"] == '"kettle" OR "vinegar"'
    kettle = result.structured["sessions"][0]
    assert session_of(kettle["id"]) == "claude:claude-kettle" and len(kettle["excerpts"]) == 3
    assert len(local(brain, "search", query="kettle", entities=["session"], limit=1).structured["sessions"]) == 1
    mixed = local(brain, "search", query="kettle", entities=["record", "session"])
    assert {hit["entity"] for hit in mixed.structured["hits"]} == {"record"} and mixed.structured["sessions"]
    without = local(brain, "search", query="kettle", entities=["record"])
    assert without.structured["hits"] == mixed.structured["hits"] and "sessions" not in without.structured
    assert "sessions" not in local(brain, "search", query="kettle", under="ops").structured


def test_search_leaves_out_the_session_that_is_asking(brain) -> None:
    asked = local(brain, "search", ("claude", "claude-kettle"), query="kettle")
    assert [session_of(item["id"]) for item in asked.structured["sessions"]] == ["codex:codex-kettle"]
    assert asking_session({"CLAUDE_CODE_SESSION_ID": " abc "}) == ("claude", "abc")
    assert asking_session({}) is None and asking_session({"CLAUDE_CODE_SESSION_ID": ""}) is None


def test_search_by_date_and_the_period_listing(brain) -> None:
    dated = local(brain, "search", query="kettle", since=DAY, until=DAY)
    assert [session_of(item["id"]) for item in dated.structured["sessions"]] == ["claude:claude-kettle"]
    assert all(hit["date"] == DAY for hit in dated.structured["hits"])
    listing = local(brain, "search", since="2027-01-16", until="2027-01-18")
    assert listing.structured["query"] == "" and listing.structured["hits"] == []
    # A period's sessions open at their first message in it.
    assert [item["id"] for item in listing.structured["sessions"]] == ["codex:codex-kettle#1", "claude:claude-badges#1"]
    assert listing.structured["warnings"] == []
    fallback = local(brain, "search", query="vinegar", since="2027-01-16", until="2027-01-18")
    assert [item["id"] for item in fallback.structured["sessions"]] == ["codex:codex-kettle#1", "claude:claude-badges#1"]
    assert fallback.structured["warnings"] == [
        "No session in those dates had the words; these are that period's sessions, newest first."]
    for arguments in ({"since": "15 January"}, {"query": "kettle", "until": "2027-13-01"}):
        bad = local(brain, "search", **arguments)
        assert bad.code == "invalid_arguments" and advice.date_error() in bad.text
    empty = local(brain, "search")
    assert empty.code == "bad_query" and advice.search_needs_words_error() in empty.text
    assert local(brain, "search", query="***").code == "bad_query"


def test_dates_filter_the_brains_hits_by_each_rows_own_date(brain) -> None:
    every = call(brain, "search", query="kettle", limit=25).structured["hits"]
    days = sorted({hit["date"] for hit in every if hit["date"]})
    assert len(days) >= 2, days
    day = days[0]
    kept = call(brain, "search", query="kettle", limit=25, since=day, until=day).structured["hits"]
    assert kept == [hit for hit in every if hit["date"] == day]
    after = call(brain, "search", query="kettle", limit=25, since=days[1]).structured["hits"]
    assert after == [hit for hit in every if hit["date"] and hit["date"] >= days[1]]
    paged = call(brain, "search", query="kettle", limit=1, offset=1, since=days[1]).structured
    assert paged["hits"] == after[1:2]


def test_get_opens_a_session_a_page_at_a_time(brain) -> None:
    hit = local(brain, "search", query="vinegar kettle", entities=["session"]).structured["sessions"][0]
    assert hit["id"] == f"claude:claude-kettle#{hit['excerpts'][0]['n']}"
    # The id search gave opens the session at that line. Here the messages before it are short, so they come too.
    page = local(brain, "get", entity="session", id=hit["id"])
    row = page.structured["row"]
    assert page.structured["entity"] == "session" and page.structured["id"] == "claude:claude-kettle"
    assert (row["tool"], row["messages"], row["cwd"]) == ("claude", 4, "/work/garden")
    assert row["started_at"] == DAY + " 08:00" and row["content"].startswith("[1] user " + DAY + " 08:00\n")
    assert "[3] user " + DAY + " 08:02\nGood, and keep the vinegar away from it.\n\n" in row["content"]
    pages = page.structured["pages"]["content"]
    assert pages["offset"] == 0 and pages["returned"] == pages["total"] == len(row["content"])
    assert pages["next_offset"] is None
    # The session's own id opens it at its start, and an offset is where a page starts, whatever the id ends in.
    assert local(brain, "get", entity="session", id="claude:claude-kettle").structured["row"] == row
    fourth = row["content"].index("[4] assistant")
    for session in ("claude:claude-kettle", "claude:claude-kettle#1"):
        later = local(brain, "get", entity="session", id=session, offset=fourth)
        assert later.structured["row"]["content"].startswith("[4] assistant " + DAY + " 08:03\nNoted: citric acid only")
    past = local(brain, "get", entity="session", id="claude:claude-kettle#9")
    assert past.code == "not_found"
    assert "session claude:claude-kettle has 4 messages and no message 9" in past.text
    assert "claude:claude-kettle#1 to claude:claude-kettle#4" in past.text
    missing = local(brain, "get", entity="session", id="claude:no-such-session")
    assert missing.code == "not_found" and "brain sessions sync" in missing.text
    assert local(brain, "get", entity="session", id="claude:no-such-session#2").code == "not_found"
    bad = local(brain, "get", entity="session", id="kettle")
    assert bad.code == "bad_id" and "is not a session id" in bad.text


def test_a_long_session_opens_at_the_line_in_a_small_page(brain, tmp_path) -> None:
    """A page is 3,000 characters, and a session opened at a message doesn't spend it on a long message before."""
    long_answer = "Ledger notes. " + "The petty cash ledger is balanced monthly. " * 120
    jsonl(tmp_path / "claude-projects" / "-work-office" / "claude-ledger.jsonl", [
        claude_row("l1", "user", "How do we keep the petty cash ledger", 1_800_300_000, session="claude-ledger"),
        claude_row("l2", "assistant", long_answer, 1_800_300_060, session="claude-ledger"),
        claude_row("l3", "user", "ledgercanary who signs it off each quarter", 1_800_300_120, session="claude-ledger"),
        claude_row("l4", "assistant", "The office manager signs the ledger off. " * 100, 1_800_300_180,
                   session="claude-ledger"),
        claude_row("l5", "user", "Thanks.", 1_800_300_240, session="claude-ledger"),
    ])
    hit = local(brain, "search", query="ledgercanary quarter", entities=["session"]).structured["sessions"][0]
    assert hit["id"] == "claude:claude-ledger#3"
    page = local(brain, "get", entity="session", id=hit["id"])
    content, pages = page.structured["row"]["content"], page.structured["pages"]["content"]
    assert content.startswith("[3] user 2027-01-18 19:22\nledgercanary who signs it off each quarter\n\n[4] assistant ")
    assert len(content) == record.PAGE_CHARS == pages["returned"] == 3000
    assert pages["offset"] > len(long_answer) and pages["next_offset"] == pages["offset"] + 3000
    assert pages["total"] > 9000
    # The next page carries on from there, and the page before is one offset away.
    rest = local(brain, "get", entity="session", id="claude:claude-ledger", offset=pages["next_offset"])
    assert (content + rest.structured["row"]["content"]).endswith("[5] user 2027-01-18 19:24\nThanks.\n\n")
    # A short message before the line comes with it: message 5 opens with the end of nothing, message 2 with 1.
    second = local(brain, "get", entity="session", id="claude:claude-ledger#2").structured
    assert second["pages"]["content"]["offset"] == 0 and second["row"]["content"].startswith("[1] user ")
    start = local(brain, "get", entity="session", id="claude:claude-ledger").structured
    assert start["row"]["content"] == second["row"]["content"]
    assert len(json.dumps(page.structured)) < 3600


def test_only_this_machine_reads_the_record(brain) -> None:
    local(brain, "search", query="kettle")
    # A hub connection: the same call without local.
    assert "sessions" not in call(brain, "search", query="kettle").structured
    for arguments in ({"entity": "session", "id": "claude:claude-kettle"},):
        refused = call(brain, "get", **arguments)
        assert refused.code == "unavailable" and advice.sessions_local_error() in refused.text
    assert call(brain, "search", query="kettle", entities=["session"]).code == "unavailable"
    # A folder token, even on this machine.
    scoped = reads.run_tool(brain, "search", {"query": "kettle"}, now=NOW, folders=("ops",), local=True)
    assert "sessions" not in scoped.structured
    token = reads.run_tool(brain, "get", {"entity": "session", "id": "claude:claude-kettle"}, now=NOW,
                           folders=("ops",), local=True)
    assert token.code == "folder_token" and "past sessions" in token.text


def test_recording_off_keeps_sessions_out(brain) -> None:
    (brain / "brain.toml").write_text(READ_CONFIG + "\n[sessions]\nrecord = false\n", encoding="utf-8")
    result = local(brain, "search", query="kettle")
    assert "sessions" not in result.structured and result.structured["hits"]
    assert not record.path_for(brain).exists()
    for refused in (local(brain, "search", query="kettle", entities=["session"]),
                    local(brain, "get", entity="session", id="claude:claude-kettle")):
        assert refused.code == "unavailable" and advice.sessions_off_error() in refused.text


def test_a_record_that_cannot_be_refreshed_still_answers(brain, monkeypatch) -> None:
    local(brain, "search", query="kettle")

    def broken(*_args, **_kwargs):
        raise OSError("disk says no")

    monkeypatch.setattr(reads.sessions_mod, "refresh", broken)
    result = local(brain, "search", query="kettle")
    assert sorted(session_of(item["id"]) for item in result.structured["sessions"]) == ["claude:claude-kettle",
                                                                                         "codex:codex-kettle"]
    assert result.structured["warnings"] == [
        "The session record couldn't be refreshed (OSError); its sessions may be behind."]
    assert result.structured["hits"]


def test_the_usage_log_counts_a_searchs_sessions(brain) -> None:
    result = local(brain, "search", query="badgecanary")
    assert usage.outcome("search", result) == ("hit", 1)
    assert usage.outcome("search", local(brain, "search", query="zzzunseen")) == ("empty", 0)


def test_brain_read_serves_sessions_from_the_command_line(brain, capsys, monkeypatch) -> None:
    from brain import cli

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-kettle")
    assert cli.main(["read", "search", "--instance", str(brain), "--args", json.dumps({"query": "kettle"})]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in printed["sessions"]] == ["codex:codex-kettle#1"]
    # get takes that id as it came, and opens the session at the line.
    assert cli.main(["read", "get", "--instance", str(brain), "--args",
                     json.dumps({"entity": "session", "id": "codex:codex-kettle#1"})]) == 0
    assert json.loads(capsys.readouterr().out)["row"]["content"].startswith("[1] user ")


# -- the pointer, and the record built when a person first connects ----------------------------------------------


def test_the_handshake_points_at_past_sessions_only_when_they_are_recorded(brain) -> None:
    from brain import server

    said = server._instructions(brain, "demo", now=NOW)
    assert server.SESSIONS_RULE in said and "don't keep memory notes in files" in said
    assert server.SESSIONS_RULE not in server._instructions(brain, "demo", now=NOW, folders=("ops",))
    (brain / "brain.toml").write_text(READ_CONFIG + "\n[sessions]\nrecord = false\n", encoding="utf-8")
    assert server.SESSIONS_RULE not in server._instructions(brain, "demo", now=NOW)


def test_the_summary_says_what_was_recorded_and_how_to_stop(brain) -> None:
    from brain import sessions
    from brain.config import load_config

    found = sessions.summary(brain, load_config(brain / "brain.toml"))
    assert found["sessions"] == {"claude": 2, "codex": 1} and found["messages"] == found["new_messages"] == 7
    line = sessions.summary_line(found)
    assert line.startswith("Past sessions: 3 recorded (7 messages from claude, codex), in ")
    for needle in ("They stay on this machine.", "record = false under [sessions] in brain.toml",
                   "brain sessions forget <session id>"):
        assert needle in line
    assert "none found yet" in sessions.summary_line({"record": True, "sessions": {}, "messages": 0, "file": "x"})
    assert "record = true under [sessions]" in sessions.summary_line({"record": False})
    assert "brain sessions sync" in sessions.summary_line({"record": True, "error": "OSError"})
    (brain / "brain.toml").write_text(READ_CONFIG + "\n[sessions]\nrecord = false\n", encoding="utf-8")
    assert sessions.summary(brain, load_config(brain / "brain.toml")) == {"record": False}
