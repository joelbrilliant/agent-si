"""The session record: what past agent sessions said, filed word for word by code.

Every transcript here is invented, in the shape each tool writes: Claude Code and Codex JSONL, Grok's folder per
session, and Hermes' state.db. The record adds each message once and keeps it, so a session survives the tool deleting
its copy, and forget is the only way one leaves.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brain import cli
from brain.config import ConfigError, _parse_config
from brain.init import init_instance
from brain.sessions import adapters, record
from brain.sessions.adapters import SessionRoots
from tests.conftest import TEST_CONFIG

ZONE = "Australia/Brisbane"  # UTC+10 all year


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def jsonl(path: Path, rows: list[dict], mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as handle:
        handle.writelines(json.dumps(row) + "\n" for row in rows)


def claude_row(uuid: str, role: str, text, at: float, session: str = "claude-garden", **extra) -> dict:
    return {"type": role, "uuid": uuid, "sessionId": session, "cwd": "/work/garden", "timestamp": iso(at),
            "message": {"role": role, "content": text}, **extra}


def codex_row(ident: str, role: str, text: str, at: float, **payload) -> dict:
    kind = "input_text" if role == "user" else "output_text"
    return {"type": "response_item", "timestamp": iso(at),
            "payload": {"type": "message", "id": ident, "role": role, "content": [{"type": kind, "text": text}],
                        **payload}}


def write_hermes(path: Path, *, created_source: bool = False) -> None:
    con = sqlite3.connect(path)
    extra = ", created_source TEXT" if created_source else ""
    con.executescript(
        f"CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL, ended_at REAL, "
        f"title TEXT, cwd TEXT{extra});"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL, "
        "content TEXT, timestamp REAL NOT NULL);"
    )
    if created_source:
        con.execute("INSERT INTO sessions VALUES ('hermes-beds', 'local', 5000, NULL, 'Raised beds', '/work', 'cli')")
        con.execute("INSERT INTO sessions VALUES ('hermes-cron', 'local', 5001, NULL, 'Nightly', '/work', 'cron')")
    else:
        con.execute("INSERT INTO sessions VALUES ('hermes-beds', 'telegram', 5000, NULL, 'Raised beds', '/work')")
        con.execute("INSERT INTO sessions VALUES ('hermes-cron', 'cron', 5001, NULL, 'Nightly', '/work')")
    con.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        [
            ("hermes-beds", "user", "hermescanary the raised beds need cedar boards\n\n"
             "[Your active task list was preserved across context compression]\n- [ ] generatedsuffix (pending)", 5002),
            ("hermes-beds", "assistant", "Cedar boards last about fifteen years outdoors.", 5003),
            ("hermes-beds", "tool", "toolnoise should never be recorded", 5004),
            ("hermes-beds", "user", "[System note: generatednote]", 5005),
            ("hermes-cron", "user", "croncanary nightly compost reading", 5006),
        ],
    )
    con.commit()
    con.close()


@pytest.fixture
def stores(tmp_path: Path) -> SessionRoots:
    claude = tmp_path / "claude-projects"
    codex = tmp_path / "codex-sessions"
    grok = tmp_path / "grok-sessions"
    jsonl(claude / "-work-garden" / "claude-garden.jsonl", [
        {"type": "summary", "summary": "not dialogue"},
        claude_row("c1", "user", "claudecanary which tomato variety suits a cool veranda", 1_800_000_000),
        claude_row("c2", "assistant", [{"type": "text", "text": "A cherry tomato such as Sweet Million."},
                                       {"type": "tool_use", "name": "Bash", "input": {"command": "toolnoise"}}],
                   1_800_000_060),
        claude_row("c3", "user", [{"type": "tool_result", "content": "toolnoise output"}], 1_800_000_061),
        claude_row("c4", "user", "metanoise from the harness", 1_800_000_062, isMeta=True),
        claude_row("c5", "assistant", "sidechainnoise from a helper", 1_800_000_063, isSidechain=True),
        claude_row("c6", "user", "Plant them after the last frost then.", 1_800_000_120),
    ])
    jsonl(claude / "-work-garden" / "claude-garden" / "subagents" / "agent-1.jsonl", [
        claude_row("s1", "user", "subagentnoise prompt", 1_800_000_010, session="claude-garden"),
    ])
    jsonl(codex / "2027" / "rollout-codex-shed.jsonl", [
        {"type": "session_meta", "payload": {"id": "codex-shed", "timestamp": iso(1_800_100_000), "cwd": "/work/shed"}},
        codex_row("x1", "user", "<environment_context>bootstrapnoise</environment_context>", 1_800_100_001),
        codex_row("x2", "user", "codexcanary the shed roof needs new flashing", 1_800_100_002),
        codex_row("x3", "assistant", "commentarynoise thinking aloud", 1_800_100_003, phase="commentary"),
        codex_row("x4", "assistant", "Use lead-free flashing along the ridge.", 1_800_100_004),
    ])
    jsonl(grok / "grok-pond" / "chat_history.jsonl", [
        {"type": "user", "content": [{"type": "text", "text": "grokcanary how deep should the pond be"}]},
        {"type": "user", "content": [{"type": "text", "text": "syntheticnoise"}], "synthetic_reason": "resume"},
        {"type": "assistant", "content": [{"type": "text", "text": "Sixty centimetres keeps fish through winter."}]},
    ])
    (grok / "grok-pond" / "summary.json").write_text(json.dumps(
        {"created_at": iso(1_800_200_000), "last_active_at": iso(1_800_200_500), "generated_title": "Pond depth",
         "git_root_dir": "/work/pond/"}), encoding="utf-8")
    write_hermes(tmp_path / "hermes-state.db")
    return SessionRoots(claude_root=claude, codex_root=codex, grok_root=grok, hermes_db=tmp_path / "hermes-state.db")


def all_messages(db: Path) -> list[tuple]:
    con = record.connect_readonly(db)
    try:
        return con.execute("SELECT harness, session_id, n, role, content FROM messages ORDER BY harness, session_id, n"
                           ).fetchall()
    finally:
        con.close()


def test_sync_records_the_dialogue_of_all_four_tools_and_nothing_a_tool_wrote_for_itself(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    result = record.sync(db, stores)
    assert result.sessions == {"claude": 1, "codex": 1, "grok": 1, "hermes": 1}
    assert (result.new_sessions, result.new_messages, result.busy, result.errors) == (4, 9, False, ())
    rows = all_messages(db)
    assert [(h, n, role) for h, _, n, role, _ in rows] == [
        ("claude", 1, "user"), ("claude", 2, "assistant"), ("claude", 3, "user"),
        ("codex", 1, "user"), ("codex", 2, "assistant"),
        ("grok", 1, "user"), ("grok", 2, "assistant"),
        ("hermes", 1, "user"), ("hermes", 2, "assistant"),
    ]
    text = "\n".join(row[4] for row in rows)
    for noise in ("toolnoise", "metanoise", "sidechainnoise", "subagentnoise", "bootstrapnoise", "commentarynoise",
                  "syntheticnoise", "generatedsuffix", "generatednote", "croncanary"):
        assert noise not in text
    assert rows[7][4] == "hermescanary the raised beds need cedar boards"
    assert rows[1][4] == "A cherry tomato such as Sweet Million."


def test_turns_a_tool_writes_for_itself_are_left_out(stores, tmp_path) -> None:
    reminder = "<system-reminder>\nLinked sessions now: none.\n</system-reminder>"
    jsonl(stores.claude_root / "-work-garden" / "claude-noise.jsonl", [
        claude_row("n1", "user", "<task-notification>\n<task-id>abc</task-id> tasknoise finished</task-notification>",
                   1_800_700_000, session="claude-noise"),
        claude_row("n2", "user", "<side-sessions-event>sidenoise</side-sessions-event>", 1_800_700_010,
                   session="claude-noise"),
        claude_row("n3", "user", '<ci-monitor-event pr="1">cinoise</ci-monitor-event>', 1_800_700_020,
                   session="claude-noise"),
        claude_row("n4", "user", reminder + "\n" + reminder + "\nkeptcanary prune the plum tree in July",
                   1_800_700_030, session="claude-noise"),
        claude_row("n5", "user", reminder, 1_800_700_040, session="claude-noise"),
        claude_row("n6", "user", "<system-reminder>\nremindernoise with no end", 1_800_700_050, session="claude-noise"),
        claude_row("n7", "assistant", "July it is. <system-reminder> is just a word here.", 1_800_700_060,
                   session="claude-noise"),
        claude_row("n8", "user", "A note that mentions <task-notification> part way through stays.", 1_800_700_070,
                   session="claude-noise"),
    ])
    jsonl(stores.codex_root / "2027" / "rollout-codex-noise.jsonl", [
        {"type": "session_meta", "payload": {"id": "codex-noise", "timestamp": iso(1_800_700_000), "cwd": "/work"}},
        codex_row("w1", "user", "Warning: The maximum number of unified exec processes you can keep open is 60 and "
                  "you currently have 60 processes open. warnnoise", 1_800_700_001),
        codex_row("w2", "user", "The following is the Codex agent history whose request action you are assessing. "
                  "reviewnoise", 1_800_700_002),
        codex_row("w3", "user", "keptcanary and the quince too", 1_800_700_003),
    ])
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    kept = [(row[1], row[3], row[4]) for row in all_messages(db) if row[1] in ("claude-noise", "codex-noise")]
    assert kept == [
        ("claude-noise", "user", "keptcanary prune the plum tree in July"),
        ("claude-noise", "assistant", "July it is. <system-reminder> is just a word here."),
        ("claude-noise", "user", "A note that mentions <task-notification> part way through stays."),
        ("codex-noise", "user", "keptcanary and the quince too"),
    ]


def queued_row(uuid: str, prompt, at: float, session: str, **attachment) -> dict:
    """What Claude Code writes when a message reaches the model part way through a turn: an attachment row, never
    a user row. A None in attachment leaves that key out, as older Claude Code versions do."""
    fields = {"type": "queued_command", "commandMode": "prompt", "prompt": prompt, "origin": {"kind": "human"},
              "humanTurn": True, "timestamp": iso(at), **attachment}
    return {"type": "attachment", "uuid": uuid, "sessionId": session, "cwd": "/work/garden", "timestamp": iso(at),
            "isSidechain": False, "attachment": {key: value for key, value in fields.items() if value is not None}}


def test_a_message_typed_while_the_assistant_is_working_is_recorded_in_its_place(stores, tmp_path) -> None:
    reminder = "<system-reminder>\nremindernoise\n</system-reminder>"
    picture = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aaaa"}}
    s = "claude-midturn"
    jsonl(stores.claude_root / "-work-garden" / "claude-midturn.jsonl", [
        claude_row("m1", "user", "Prune the plum tree.", 1_800_800_000, session=s),
        claude_row("m2", "assistant", "Starting on the lower branches.", 1_800_800_010, session=s),
        queued_row("m3", "midturncanary leave the top branch alone", 1_800_800_020, s),
        queued_row("m4", [picture, {"type": "text", "text": "listcanary this is the branch"}], 1_800_800_030, s),
        queued_row("m5", reminder + "\nremindedcanary and sweep up after", 1_800_800_040, s),
        queued_row("m6", "oldercanary typed in a Claude Code that set no humanTurn", 1_800_800_050, s, humanTurn=None),
        # Not a person's: a finished task, another session, the tool's own event, and a row that names no origin.
        queued_row("m7", "<task-notification>\ntasknoise finished</task-notification>", 1_800_800_060, s,
                   commandMode="task-notification", humanTurn=None,
                   origin={"kind": "task-notification", "producer": "session-task"}),
        queued_row("m8", "<agent-message>peernoise from another session</agent-message>", 1_800_800_070, s,
                   humanTurn=None, isMeta=True, origin={"kind": "peer", "from": "another session"}),
        queued_row("m9", '<ci-monitor-event pr="1">cinoise</ci-monitor-event>', 1_800_800_080, s, humanTurn=None,
                   origin=None),
        queued_row("m10", "unnamednoise with no origin", 1_800_800_090, s, humanTurn=None, origin=None),
        # A person's, with nothing to record: a picture alone, a reminder alone, and one typed to a helper.
        queued_row("m11", [picture], 1_800_800_100, s),
        queued_row("m12", reminder, 1_800_800_110, s),
        {**queued_row("m13", "sidechainnoise typed to a helper", 1_800_800_120, s), "isSidechain": True},
        {"type": "attachment", "uuid": "m14", "sessionId": s, "timestamp": iso(1_800_800_130),
         "attachment": {"type": "hook_additional_context", "content": ["hooknoise notes a hook added"]}},
        claude_row("m15", "assistant", "Left the top branch and swept up.", 1_800_800_200, session=s),
    ])
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    kept = [(row[2], row[3], row[4]) for row in all_messages(db) if row[1] == s]
    assert kept == [
        (1, "user", "Prune the plum tree."),
        (2, "assistant", "Starting on the lower branches."),
        (3, "user", "midturncanary leave the top branch alone"),
        (4, "user", "listcanary this is the branch"),
        (5, "user", "remindedcanary and sweep up after"),
        (6, "user", "oldercanary typed in a Claude Code that set no humanTurn"),
        (7, "assistant", "Left the top branch and swept up."),
    ]
    hit = record.search(db, "midturncanary")[0]
    assert (hit.id, hit.excerpts[0].n, hit.excerpts[0].role) == (f"claude:{s}", 3, "user")
    assert hit.excerpts[0].timestamp == 1_800_800_020


def earlier_reader(monkeypatch) -> None:
    """The Claude Code reader as it was before it read messages typed mid-turn, with no reader number to keep."""
    monkeypatch.setattr(adapters, "_claude_typed_mid_turn", lambda row: None)
    monkeypatch.setattr(adapters, "READER_VERSIONS", {})


def numbered(db: Path, session: str) -> list[tuple]:
    con = record.connect_readonly(db)
    try:
        return con.execute("SELECT n, start, message_key, role, content, timestamp FROM messages "
                           "WHERE session_id = ? ORDER BY n", (session,)).fetchall()
    finally:
        con.close()


def write_earlier_session(stores) -> str:
    s = "claude-earlier"
    jsonl(stores.claude_root / "-work-garden" / "claude-earlier.jsonl", [
        claude_row("e1", "user", "Net the fig tree.", 1_800_900_000, session=s),
        claude_row("e2", "assistant", "Netting the lower half first.", 1_800_900_010, session=s),
        queued_row("e3", "backfillcanary use the green net", 1_800_900_020, s),
        claude_row("e4", "assistant", "Green net is on.", 1_800_900_030, session=s),
    ])
    return s


def test_a_session_filed_by_the_earlier_reader_is_read_again_once_and_keeps_its_numbers(
    stores, tmp_path, monkeypatch
) -> None:
    s = write_earlier_session(stores)
    db = tmp_path / "brain" / record.FILE_NAME
    with monkeypatch.context() as earlier:
        earlier_reader(earlier)
        record.sync(db, stores)
    before = numbered(db, s)
    assert [row[4] for row in before] == ["Net the fig tree.", "Netting the lower half first.", "Green net is on."]

    again = record.sync(db, stores)  # the file hasn't changed; the reader has
    assert (again.new_sessions, again.new_messages) == (0, 1)
    after = numbered(db, s)
    assert after[:3] == before  # no number, place or word already filed has moved
    n, start, key, role, content, at = after[3]
    assert (n, key, role, content) == (4, "e3", "user", "backfillcanary use the green net")
    assert at == 1_800_900_020 < after[2][5]  # it keeps its own time, which is before the message filed ahead of it
    assert start == before[2][1] + record.rendered_length(*[before[2][i] for i in (0, 3, 5, 4)])

    def never(descriptor):
        raise AssertionError(f"read again: {descriptor.session_id}")

    monkeypatch.setattr(adapters, "load_messages", never)
    third = record.sync(db, stores)
    assert (third.new_sessions, third.new_messages, third.errors) == (0, 0, ())


def test_a_tool_whose_sessions_could_not_be_read_is_read_again_at_the_next_sync(stores, tmp_path, monkeypatch) -> None:
    s = write_earlier_session(stores)
    db = tmp_path / "brain" / record.FILE_NAME
    with monkeypatch.context() as earlier:
        earlier_reader(earlier)
        record.sync(db, stores)

    def unreadable(descriptor):
        if descriptor.session_id == s:
            raise PermissionError(descriptor.source_path)
        return real_load(descriptor)

    real_load = adapters.load_messages
    with monkeypatch.context() as failing:
        failing.setattr(adapters, "load_messages", unreadable)
        failed = record.sync(db, stores)
    assert [(error["harness"], error["error"]) for error in failed.errors] == [("claude", "PermissionError")]
    assert "backfillcanary" not in " ".join(row[4] for row in numbered(db, s))

    retried = record.sync(db, stores)
    assert (retried.new_messages, retried.errors) == (1, ())
    assert numbered(db, s)[3][4] == "backfillcanary use the green net"


def test_a_second_sync_adds_nothing_and_new_lines_take_the_next_numbers(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    again = record.sync(db, stores)
    assert (again.new_sessions, again.new_messages) == (0, 0)
    path = stores.claude_root / "-work-garden" / "claude-garden.jsonl"
    jsonl(path, [claude_row("c7", "assistant", "Mid October is safe on that veranda.", 1_800_000_180)], mode="a")
    os.utime(path, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))
    third = record.sync(db, stores)
    assert (third.new_sessions, third.new_messages) == (0, 1)
    claude = [row for row in all_messages(db) if row[0] == "claude"]
    assert [row[2] for row in claude] == [1, 2, 3, 4]
    assert claude[3][4] == "Mid October is safe on that veranda."


def test_a_session_stays_in_the_record_after_the_tool_deletes_it(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    (stores.claude_root / "-work-garden" / "claude-garden.jsonl").unlink()
    result = record.sync(db, stores)
    assert result.sessions.get("claude") is None
    hits = record.search(db, "claudecanary")
    assert [hit.id for hit in hits] == ["claude:claude-garden"]
    assert record.status(db)["sessions"]["claude"] == 1


def test_forget_removes_a_session_and_it_does_not_come_back(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    assert record.forget(db, "codex", "codex-shed") == 2
    assert record.search(db, "codexcanary") == []
    path = stores.codex_root / "2027" / "rollout-codex-shed.jsonl"
    jsonl(path, [codex_row("x5", "user", "codexcanary and the gutter too", 1_800_100_005)], mode="a")
    os.utime(path, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))
    record.sync(db, stores)
    assert record.search(db, "codexcanary") == []
    status = record.status(db)
    assert status["forgotten"] == 1 and "codex" not in status["sessions"]
    assert record.forget(db, "codex", "never-seen") == 0


def test_a_message_that_looks_like_a_secret_is_never_stored(stores, tmp_path) -> None:
    path = stores.claude_root / "-work-garden" / "claude-garden.jsonl"
    jsonl(path, [
        claude_row("k1", "user", "secretcanary here is the key sk-ant-" + "a1B2" * 8, 1_800_000_200),
        claude_row("k2", "user", "secretcanary export GARDEN_API_KEY=" + "z9" * 12, 1_800_000_201),
        claude_row("k3", "assistant", "secretcanary I won't repeat that key.", 1_800_000_202),
    ], mode="a")
    db = tmp_path / "brain" / record.FILE_NAME
    result = record.sync(db, stores)
    assert result.skipped_secret == 2
    kept = [row[4] for row in all_messages(db) if "secretcanary" in row[4]]
    assert kept == ["secretcanary I won't repeat that key."]


def test_the_record_is_readable_by_its_owner_only(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_search_counts_any_word_and_can_keep_every_word_first(stores, tmp_path) -> None:
    jsonl(stores.claude_root / "-work-orchard" / "claude-orchard.jsonl", [
        claude_row("o1", "user", "The orchard ladder is aluminium.", 1_800_300_000, session="claude-orchard"),
        claude_row("o2", "assistant", "orchard ladder pruning schedule: prune the ladder side first, then the "
                   "orchard rows, ladder again in spring.", 1_800_300_060, session="claude-orchard"),
        claude_row("o3", "user", "Where did the quince ladder go?", 1_800_300_120, session="claude-orchard"),
    ])
    jsonl(stores.claude_root / "-work-orchard" / "claude-quince.jsonl", [
        claude_row("q1", "user", "The quince tree fruits in autumn.", 1_800_400_000, session="claude-quince"),
    ])
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    any_word = record.search(db, "quince pruning schedule")
    assert {hit.session_id for hit in any_word} == {"claude-orchard", "claude-quince"}
    every_word = record.search(db, "quince ladder", all_terms_first=True)
    assert [hit.session_id for hit in every_word] == ["claude-orchard"]
    assert [excerpt.n for excerpt in every_word[0].excerpts] == [3]
    orchard = next(hit for hit in any_word if hit.session_id == "claude-orchard")
    assert orchard.title == "The orchard ladder is aluminium." and orchard.id == "claude:claude-orchard"
    assert all(len(excerpt.text) <= record.EXCERPT_CHARS and "\n" not in excerpt.text for excerpt in orchard.excerpts)
    assert record.search(db, "quince", limit=1, excerpts=1)[0].excerpts.__len__() == 1
    assert record.search(db, "?!") == [] and record.search(tmp_path / "none.sqlite3", "quince") == []


def test_search_leaves_out_the_asking_session_and_keeps_to_the_dates(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    assert [hit.id for hit in record.search(db, "claudecanary tomato")] == ["claude:claude-garden"]
    assert record.search(db, "claudecanary tomato", exclude=("claude", "claude-garden")) == []
    assert record.search(db, "claudecanary", exclude=("codex", "claude-garden")) != []
    day = "2027-01-15"  # 1_800_000_000 is 15 January 2027, 18:00 in Brisbane
    since, before = record.day_bounds(day, day, ZONE)
    assert [hit.id for hit in record.search(db, "claudecanary", since=since, before=before)] == ["claude:claude-garden"]
    later, _ = record.day_bounds("2027-01-16", None, ZONE)
    assert record.search(db, "claudecanary", since=later) == []
    _, earlier = record.day_bounds(None, "2027-01-14", ZONE)
    assert record.search(db, "claudecanary", before=earlier) == []
    with pytest.raises(ValueError):
        record.day_bounds("15 January", None, ZONE)


def test_two_ids_for_one_conversation_take_one_place(stores, tmp_path) -> None:
    for name in ("fork-a", "fork-b"):
        jsonl(stores.claude_root / "-work-forks" / f"{name}.jsonl", [
            claude_row("f1", "user", "forkcanary the same words in both copies", 1_800_500_000, session=name),
        ])
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    assert len(record.search(db, "forkcanary")) == 1
    assert record.status(db)["sessions"]["claude"] == 3


def test_period_lists_a_days_sessions_newest_first(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    since, before = record.day_bounds("2027-01-15", "2027-01-18", ZONE)
    hits = record.period(db, since, before)
    assert [hit.id for hit in hits] == ["grok:grok-pond", "codex:codex-shed", "claude:claude-garden"]
    assert hits[2].excerpts[0].n == 1 and hits[2].excerpts[0].text.startswith("claudecanary")
    assert [hit.id for hit in record.period(db, since, before, exclude=("grok", "grok-pond"), limit=1)] == [
        "codex:codex-shed"]
    assert record.period(db, None, None) == []


def test_read_pages_a_session_as_text_with_a_header_for_each_message(stores, tmp_path) -> None:
    long_text = "longcanary " + "mulch " * 4000
    jsonl(stores.claude_root / "-work-garden" / "claude-garden.jsonl", [
        claude_row("c8", "assistant", long_text, 1_800_000_240),
        claude_row("c9", "user", "Thanks, that covers the mulch.", 1_800_000_300),
    ], mode="a")
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    first = record.read(db, "claude", "claude-garden", ZONE)
    assert first["text"].startswith("[1] user 2027-01-15 18:00\nclaudecanary which tomato variety suits a cool veranda\n\n"
                                    "[2] assistant 2027-01-15 18:01\nA cherry tomato such as Sweet Million.\n\n")
    assert (first["messages"], first["offset"], first["returned"]) == (5, 0, record.PAGE_CHARS)
    assert (first["first_n"], first["last_n"]) == (1, 4)
    pages, offset = [first["text"]], first["next_offset"]
    while offset is not None:
        page = record.read(db, "claude", "claude-garden", ZONE, offset=offset)
        pages.append(page["text"])
        offset = page["next_offset"]
    whole = "".join(pages)
    assert len(whole) == first["chars"] and len(pages) == -(-first["chars"] // record.PAGE_CHARS) == 9
    assert whole.endswith("[5] user 2027-01-15 18:05\nThanks, that covers the mulch.\n\n")
    assert long_text.strip() in whole
    # A hit's start is where its message begins. Opened at a message, a page takes the short messages just
    # before it and never a long one, so the message asked for is always on the page.
    hit = record.search(db, "longcanary")[0]
    assert whole[hit.excerpts[0].start:].startswith("[4] assistant 2027-01-15 18:04\nlongcanary mulch")
    con = record.connect_readonly(db)
    try:
        assert record.message_start(con, "claude", "claude-garden", 4) == 0  # three short messages lead up to it
        assert record.message_start(con, "claude", "claude-garden", 1) == 0
        fifth = record.message_start(con, "claude", "claude-garden", 5)
        assert whole[fifth:].startswith("[5] user 2027-01-15 18:05\nThanks")  # the long message 4 is left out
        assert record.message_start(con, "claude", "claude-garden", 6) is None
    finally:
        con.close()
    at_five = record.read(db, "claude", "claude-garden", ZONE, at=5)
    assert at_five["offset"] == fifth and at_five["text"] == whole[fifth:] and at_five["next_offset"] is None
    assert record.read(db, "claude", "claude-garden", ZONE, at=4)["text"] == first["text"]
    assert record.read(db, "claude", "claude-garden", ZONE, at=5, offset=7)["text"] == whole[7:7 + record.PAGE_CHARS]
    with pytest.raises(record.NoSuchMessage) as missing:
        record.read(db, "claude", "claude-garden", ZONE, at=6)
    assert (missing.value.n, missing.value.messages) == (6, 5)
    assert record.parse_place("claude:claude-garden#5") == ("claude", "claude-garden", 5)
    assert record.parse_place("claude:claude-garden") == ("claude", "claude-garden", None)
    assert record.parse_place("hermes:side:20270115#12") == ("hermes", "side:20270115", 12)
    assert record.parse_place("garden#5") is None and record.parse_place("") is None
    assert record.place_ref("claude", "claude-garden", 5) == "claude:claude-garden#5"
    assert record.read(db, "claude", "no-such-session", ZONE) is None
    assert record.read(db, "claude", "claude-garden", ZONE, offset=10**9)["text"] == ""
    grok = record.read(db, "grok", "grok-pond", ZONE)
    assert grok["title"] == "Pond depth" and grok["cwd"] == "/work/pond"
    assert record.messages_between(db, "claude", "claude-garden", 2, 3) == [
        (2, "assistant", 1_800_000_060.0, "A cherry tomato such as Sweet Million."),
        (3, "user", 1_800_000_120.0, "Plant them after the last frost then."),
    ]


def test_a_message_with_no_time_keeps_its_place(tmp_path) -> None:
    roots = SessionRoots(claude_root=tmp_path / "c", codex_root=tmp_path / "x", grok_root=tmp_path / "g",
                         hermes_db=tmp_path / "h.db")
    row = claude_row("n1", "user", "notimecanary first", 1_800_000_000)
    del row["timestamp"]
    jsonl(roots.claude_root / "p" / "s.jsonl", [row, claude_row("n2", "assistant", "second", 1_800_000_060)])
    db = tmp_path / "brain" / record.FILE_NAME
    result = record.sync(db, roots)
    assert sorted(error["harness"] for error in result.errors) == ["codex", "grok", "hermes"]
    assert {error["error"] for error in result.errors} == {"FileNotFoundError"}
    page = record.read(db, "claude", "claude-garden", ZONE)
    assert page["text"] == "[1] user no time\nnotimecanary first\n\n[2] assistant 2027-01-15 18:01\nsecond\n\n"
    assert page["chars"] == len(page["text"]) and page["next_offset"] is None
    since, _ = record.day_bounds("2027-01-01", None, ZONE)
    assert [e.n for e in record.search(db, "notimecanary second", since=since)[0].excerpts] == [2]


def test_sync_is_skipped_while_another_process_holds_the_lock(stores, tmp_path) -> None:
    fcntl = pytest.importorskip("fcntl")
    db = tmp_path / "brain" / record.FILE_NAME
    db.parent.mkdir()
    with open(db.parent / record.LOCK_NAME, "a+b") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        busy = record.sync(db, stores)
    assert busy.busy and busy.new_messages == 0 and not db.exists()
    assert record.sync(db, stores).new_messages == 9


def test_an_unreadable_store_is_listed_and_the_others_still_sync(stores, tmp_path) -> None:
    db = tmp_path / "brain" / record.FILE_NAME
    record.sync(db, stores)
    stores.hermes_db.write_bytes(b"not a database at all, " * 100)
    jsonl(stores.claude_root / "-work-garden" / "claude-late.jsonl", [
        claude_row("l1", "user", "latecanary one more question", 1_800_600_000, session="claude-late"),
    ])
    result = record.sync(db, stores)
    assert [(error["harness"], error["error"]) for error in result.errors] == [("hermes", "DatabaseError")]
    assert result.new_messages == 1
    assert [hit.id for hit in record.search(db, "hermescanary")] == ["hermes:hermes-beds"]


def test_hermes_profiles_and_the_current_hermes_layout(tmp_path) -> None:
    write_hermes(tmp_path / "default.db", created_source=True)
    write_hermes(tmp_path / "side.db")
    roots = SessionRoots(claude_root=tmp_path / "c", codex_root=tmp_path / "x", grok_root=tmp_path / "g",
                         hermes_db=tmp_path / "default.db", hermes_profiles=(("side", tmp_path / "side.db"),))
    db = tmp_path / "brain" / record.FILE_NAME
    result = record.sync(db, roots)
    assert result.sessions == {"hermes": 2} and result.new_messages == 4
    # The two profiles hold the same words here, so a search gives them one place; both are in the record.
    assert len(record.search(db, "hermescanary")) == 1 and record.status(db)["sessions"] == {"hermes": 2}
    assert record.parse_ref("hermes:side:hermes-beds") == ("hermes", "side:hermes-beds")
    assert [hit.id for hit in record.search(db, "hermescanary", exclude=("hermes", "hermes-beds"))] == []
    assert record.parse_ref("claude") is None and record.parse_ref("notes:abc") is None
    page = record.read(db, "hermes", "side:hermes-beds", ZONE)
    assert page["messages"] == 2 and "croncanary" not in page["text"]


def test_default_roots_follow_the_environment(tmp_path) -> None:
    roots = SessionRoots.default(
        hermes_profiles={"side": "~/side/state.db"},
        env={"CLAUDE_PROJECTS_ROOT": str(tmp_path / "c"), "HERMES_HOME": str(tmp_path / "h")},
    )
    assert roots.claude_root == tmp_path / "c" and roots.hermes_db == tmp_path / "h" / "state.db"
    assert roots.codex_root == Path.home() / ".codex" / "sessions"
    assert roots.hermes_profiles == (("side", Path.home() / "side" / "state.db"),)
    assert adapters.HARNESSES == ("claude", "codex", "grok", "hermes")


def test_the_sessions_table_in_brain_toml() -> None:
    assert _parse_config(TEST_CONFIG.encode()).sessions.record is True
    off = _parse_config((TEST_CONFIG + '\n[sessions]\nrecord = false\nhermes_profiles = { side = "~/side/state.db" }\n'
                         ).encode())
    assert off.sessions.record is False and off.sessions.hermes_profiles == (("side", "~/side/state.db"),)
    for bad, needle in (
        ("[sessions]\nrecrod = true\n", "unknown key sessions.recrod"),
        ('[sessions]\nrecord = "yes"\n', "sessions.record must be true or false"),
        ('[sessions]\nhermes_profiles = { default = "x" }\n', "profile name"),
        ("[sessions]\nhermes_profiles = { side = 3 }\n", "path to that profile's state.db"),
    ):
        with pytest.raises(ConfigError, match=needle):
            _parse_config((TEST_CONFIG + "\n" + bad).encode())


def test_brain_sessions_status_sync_and_forget(stores, tmp_path, monkeypatch, capsys) -> None:
    (tmp_path / "home").mkdir()
    instance = Path(init_instance("demo", tmp_path / "home", config_text=TEST_CONFIG)["instance"])
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(stores.claude_root))
    monkeypatch.setenv("CODEX_SESSIONS_ROOT", str(stores.codex_root))
    monkeypatch.setenv("GROK_SESSIONS_ROOT", str(tmp_path / "no-grok-here"))
    monkeypatch.setenv("HERMES_SESSION_DB", str(stores.hermes_db))

    def run(*argv) -> tuple[int, dict]:
        code = cli.main(["sessions", *argv, "--instance", str(instance)])
        return code, json.loads(capsys.readouterr().out)

    code, before = run("status")
    assert code == 0 and before["record"] is True and before["exists"] is False
    code, synced = run("sync")
    assert code == 0 and synced["status"] == "synced" and synced["new_messages"] == 7
    assert synced["no_store_found"] == ["grok"] and synced["errors"] == []
    code, after = run("status")
    assert after["sessions"] == {"claude": 1, "codex": 1, "hermes": 1} and after["messages"] == 7
    assert after["file"] == str(instance / record.FILE_NAME) and after["last_sync_at"]
    code, gone = run("forget", "hermes:hermes-beds")
    assert code == 0 and gone == {"status": "forgotten", "session": "hermes:hermes-beds", "messages_removed": 2}
    assert cli.main(["sessions", "forget", "beds", "--instance", str(instance)]) == 2
    assert "is not a session id" in capsys.readouterr().err
    (instance / "brain.toml").write_text(TEST_CONFIG + "\n[sessions]\nrecord = false\n", encoding="utf-8")
    code, off = run("sync")
    assert code == 1 and off["status"] == "off" and "record = true" in off["message"]
