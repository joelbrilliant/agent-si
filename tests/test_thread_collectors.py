from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from brain import store
from brain.cli import main
from brain.collectors.base import stable_id
from tests.collector_fixtures import (
    block_network,
    calendar_doc,
    collect,
    collector_instance,
    gmail_doc,
    slack_doc,
)
from tests.conftest import NOW, call, make_packet, read_instance, ro_rows, source_op, upsert_op

ASK = "C0ABC1234:1791400000.000100"
OWE = "C0ABC1234:1791400100.000200"
_MESSAGE_OPS = ["add_source", "upsert_thread", "record_collector_run"]


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch) -> None:
    monkeypatch.setattr(store, "utc_now", lambda: NOW)


def _apply(instance: Path, packet: dict) -> dict:
    path = instance.parent / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["apply", "--instance", str(instance), str(path)])
    assert code == 0, (stdout.getvalue(), stderr.getvalue())
    return json.loads(stdout.getvalue())


def _signals(instance: Path) -> dict:
    result = call(instance, "signals")
    assert not result.is_error, result.text
    return result.structured


def _run_error(instance: Path) -> str:
    return ro_rows(instance, "SELECT error FROM collector_runs ORDER BY rowid DESC LIMIT 1")[0][0]


def _thread(instance: Path, external_id: str) -> tuple:
    return ro_rows(
        instance,
        "SELECT id, outbound_seen, subject, last_inbound_at, last_outbound_at, version "
        "FROM threads WHERE external_id = ?",
        (external_id,),
    )[0]


def _state(instance: Path, link_id: str) -> str:
    return ro_rows(
        instance,
        "SELECT reply_state FROM thread_link_state WHERE link_id = ?",
        (link_id,),
    )[0][0]


def _mail(external_id: str, messages: list[tuple[str, str]], subject: str | None) -> dict:
    item = {
        "id": external_id,
        "messages": [{"at": at, "direction": direction} for at, direction in messages],
    }
    if subject is not None:
        item["subject"] = subject
    return item


def _add_collector(instance: Path, name: str, type_name: str, agent: str, account: str, ops: list[str]) -> None:
    extra = (
        f"\n[agents.{agent}]\n"
        'kind = "collector"\n'
        f"operations = {json.dumps(ops)}\n"
        'approval = "none"\n'
        "approvers = []\n"
        "author_principal = false\n"
        f"\n[collectors.{name}]\n"
        f'type = "{type_name}"\n'
        f'agent = "{agent}"\n'
        f'account = "{account}"\n'
    )
    path = instance / "brain.toml"
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")


def _link_actions(instance: Path, ask_thread: str, owe_thread: str) -> None:
    _apply(
        instance,
        make_packet(
            "plain",
            source_op("src-scenario", "Scenario notes for two linked actions."),
            upsert_op(
                "upsert_action",
                {
                    "id": "act-ask",
                    "title": "Ask follow-up",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": NOW,
                },
                "src-scenario",
            ),
            upsert_op(
                "upsert_action",
                {
                    "id": "act-owe",
                    "title": "Send the note",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": NOW,
                },
                "src-scenario",
            ),
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-ask",
                    "action_id": "act-ask",
                    "thread_id": ask_thread,
                    "role": "awaiting_reply",
                    "since": "2026-10-02T09:00:00Z",
                },
                "src-scenario",
            ),
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-owe",
                    "action_id": "act-owe",
                    "thread_id": owe_thread,
                    "role": "owes_reply",
                    "since": "2026-10-03T09:00:00Z",
                },
                "src-scenario",
            ),
        ),
    )


def _message_doc(kind: str, fetched_at: str, threads: list[dict]) -> dict:
    builder = gmail_doc if kind == "gmail" else slack_doc
    return builder(fetched_at=fetched_at, outbound_visible=True, threads=threads)


def _prepare_reply(tmp_path: Path, kind: str) -> tuple[Path, dict]:
    if kind == "gmail":
        collector, account, prefix, ask_id, owe_id, subject = (
            "gmail-work",
            "work",
            "thr-email-",
            "thread-ask",
            "thread-owe",
            "Question",
        )
        owe_subject = "Follow up"
    else:
        collector, account, prefix, ask_id, owe_id, subject = (
            "slack-team",
            "team",
            "thr-slack-",
            ASK,
            OWE,
            None,
        )
        owe_subject = None
    instance = collector_instance(tmp_path, collector)
    ask_thread = stable_id(prefix, account, ask_id)
    owe_thread = stable_id(prefix, account, owe_id)
    doc1 = _message_doc(
        kind,
        "2026-10-07T00:00:00Z",
        [
            _mail(ask_id, [("2026-10-02T09:00:00Z", "outbound")], subject),
            _mail(owe_id, [("2026-10-03T09:00:00Z", "inbound")], owe_subject),
        ],
    )
    code, payload, err = collect(instance, collector, doc1)
    assert code == 0, (payload, err)
    assert payload["status"] == "ok"
    assert payload["items"] == 2
    assert _thread(instance, ask_id)[:2] == (ask_thread, 1)
    assert _thread(instance, owe_id)[:2] == (owe_thread, 1)
    if kind == "slack":
        assert _thread(instance, ask_id)[2] == ""
        assert _thread(instance, owe_id)[2] == ""
    _link_actions(instance, ask_thread, owe_thread)
    assert _state(instance, "lnk-ask") == "waiting"
    assert _state(instance, "lnk-owe") == "waiting"
    owed = [item for item in _signals(instance)["signals"] if item["subject"]["id"] == "act-owe"]
    assert [item["kind"] for item in owed] == ["owes_reply"]
    doc2 = _message_doc(
        kind,
        "2026-10-07T12:00:00Z",
        [
            _mail(
                ask_id,
                [("2026-10-02T09:00:00Z", "outbound"), ("2026-10-05T09:00:00Z", "inbound")],
                subject,
            ),
            _mail(
                owe_id,
                [("2026-10-03T09:00:00Z", "inbound"), ("2026-10-06T09:00:00Z", "outbound")],
                owe_subject,
            ),
        ],
    )
    return instance, doc2


def _finish_reply(instance: Path, collector: str, agent: str, doc2: dict) -> None:
    code, payload, err = collect(instance, collector, doc2)
    assert code == 0, (payload, err)
    assert payload["status"] == "ok"
    assert _state(instance, "lnk-ask") == "replied"
    assert _state(instance, "lnk-owe") == "replied"
    signals = _signals(instance)["signals"]
    assert [item["kind"] for item in signals if item["subject"]["id"] == "act-ask"] == ["reply"]
    assert [item for item in signals if item["subject"]["id"] == "act-owe"] == []
    got = call(instance, "get", entity="action", id="act-ask")
    assert not got.is_error, got.text
    assert got.structured["related"]["thread_links"]["items"][0]["reply_state"] == "replied"
    logged = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    code, again, err = collect(instance, collector, doc2)
    assert code == 0, (again, err)
    assert again["duplicate"] is True
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == logged
    agents = {row[0] for row in ro_rows(instance, "SELECT agent FROM write_log")}
    assert agents - {"brain"} == {agent, "plain"}


def test_acceptance_7_gmail_reply_moves_a_linked_action_to_replied(tmp_path: Path) -> None:
    with block_network():
        instance, doc2 = _prepare_reply(tmp_path, "gmail")
        _finish_reply(instance, "gmail-work", "gmailwork", doc2)


def test_acceptance_7_slack_reply_moves_a_linked_action_to_replied(tmp_path: Path) -> None:
    with block_network():
        instance, doc2 = _prepare_reply(tmp_path, "slack")
        _finish_reply(instance, "slack-team", "slackteam", doc2)


def test_message_in_another_thread_never_marks_replied(tmp_path: Path) -> None:
    with block_network():
        instance, _doc2 = _prepare_reply(tmp_path, "gmail")
        doc = gmail_doc(
            fetched_at="2026-10-07T12:00:00Z",
            outbound_visible=True,
            threads=[
                _mail("thread-ask", [("2026-10-02T09:00:00Z", "outbound")], "Question"),
                _mail("thread-elsewhere", [("2026-10-05T09:00:00Z", "inbound")], "Elsewhere"),
            ],
        )
        code, payload, err = collect(instance, "gmail-work", doc)
        assert code == 0, (payload, err)
        assert _state(instance, "lnk-ask") == "waiting"


@pytest.mark.parametrize("kind", ["gmail", "slack", "calendar"])
@pytest.mark.parametrize(
    "field",
    ["from", "to", "sender", "attendees", "email", "user", "name"],
)
def test_documents_carry_no_people(tmp_path: Path, kind: str, field: str) -> None:
    collector, builder, key = {
        "gmail": ("gmail-work", gmail_doc, "threads"),
        "slack": ("slack-team", slack_doc, "threads"),
        "calendar": ("work-cal", calendar_doc, "events"),
    }[kind]
    doc = builder()
    item = dict(doc[key][0])
    item[field] = "x"
    doc[key][0] = item
    instance = collector_instance(tmp_path, collector)
    code, payload, err = collect(instance, collector, doc)
    assert code == 1, (payload, err)
    assert payload["status"] == "error"
    assert "is not allowed" in _run_error(instance)


def test_outbound_visibility_sets_outbound_seen_and_clears_unknown(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    _add_collector(instance, "gmail-work", "gmail", "gmailwork", "work", _MESSAGE_OPS)
    assert _signals(instance)["unknown_outbound"] == 1
    doc = gmail_doc(
        fetched_at="2026-10-07T00:00:00Z",
        outbound_visible=True,
        threads=[
            {
                "id": "thread-hidden",
                "subject": "Access form",
                "messages": [{"at": "2026-10-01T09:00:00Z", "direction": "outbound"}],
            }
        ],
    )
    code, payload, err = collect(instance, "gmail-work", doc)
    assert code == 0, (payload, err)
    assert _thread(instance, "thread-hidden")[:2] == ("thr-hidden", 1)
    assert ro_rows(
        instance,
        "SELECT count(*) FROM threads WHERE external_id = 'thread-hidden'",
    )[0][0] == 1
    assert _state(instance, "lnk-hidden") == "replied"
    assert _signals(instance)["unknown_outbound"] == 0


def test_outbound_invisible_documents_keep_unknown(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    _add_collector(instance, "gmail-work", "gmail", "gmailwork", "work", _MESSAGE_OPS)
    hidden = {
        "id": "thread-hidden",
        "subject": "Access form",
        "messages": [{"at": "2026-10-01T09:00:00Z", "direction": "outbound"}],
    }
    refused = gmail_doc(
        fetched_at="2026-10-07T00:00:00Z",
        outbound_visible=False,
        threads=[hidden],
    )
    code, payload, err = collect(instance, "gmail-work", refused)
    assert code == 1, (payload, err)
    assert payload["status"] == "error"
    assert _run_error(instance) == (
        "thread thread-hidden: outbound message but outbound_visible is false"
    )
    assert _thread(instance, "thread-hidden")[1] == 0
    assert _signals(instance)["unknown_outbound"] == 1
    moved = gmail_doc(
        fetched_at="2026-10-07T06:00:00Z",
        outbound_visible=False,
        threads=[
            {
                "id": "thread-hidden",
                "subject": "Access form",
                "messages": [{"at": "2026-10-04T09:00:00Z", "direction": "inbound"}],
            }
        ],
    )
    code, payload, err = collect(instance, "gmail-work", moved)
    assert code == 0, (payload, err)
    row = _thread(instance, "thread-hidden")
    assert row[0] == "thr-hidden"
    assert row[1] == 0
    assert row[3] == "2026-10-04T09:00:00Z"
    assert _signals(instance)["unknown_outbound"] == 1


def test_thread_times_never_move_backwards(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    first = gmail_doc(
        fetched_at="2026-10-07T08:00:00Z",
        threads=[
            _mail(
                "thread-time",
                [("2026-10-04T09:00:00Z", "inbound"), ("2026-10-05T09:00:00Z", "outbound")],
                "Times",
            )
        ],
    )
    code, payload, err = collect(instance, "gmail-work", first)
    assert code == 0, (payload, err)
    original = _thread(instance, "thread-time")
    assert original[1] == 1
    older = gmail_doc(
        fetched_at="2026-10-07T10:00:00Z",
        threads=[
            _mail(
                "thread-time",
                [("2026-10-01T09:00:00Z", "inbound"), ("2026-10-02T09:00:00Z", "outbound")],
                "Times",
            )
        ],
    )
    code, payload, err = collect(instance, "gmail-work", older)
    assert code == 0, (payload, err)
    assert _thread(instance, "thread-time") == original
    invisible = gmail_doc(
        fetched_at="2026-10-07T12:00:00Z",
        outbound_visible=False,
        threads=[
            _mail("thread-time", [("2026-10-06T09:00:00Z", "inbound")], "Times"),
        ],
    )
    code, payload, err = collect(instance, "gmail-work", invisible)
    assert code == 0, (payload, err)
    moved = _thread(instance, "thread-time")
    assert moved[1] == 1
    assert moved[3] == "2026-10-06T09:00:00Z"
    assert moved[4] == original[4]


def test_rerunning_a_fixture_changes_nothing(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    doc = gmail_doc(fetched_at="2026-10-07T00:00:00Z")
    code, first, err = collect(instance, "gmail-work", doc)
    assert code == 0, (first, err)
    logged = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    version = _thread(instance, "thread-review")[5]
    code, again, err = collect(instance, "gmail-work", doc)
    assert code == 0, (again, err)
    assert again["duplicate"] is True
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == logged
    later = gmail_doc(fetched_at="2026-10-07T12:00:00Z")
    code, payload, err = collect(instance, "gmail-work", later)
    assert code == 0, (payload, err)
    assert payload["status"] == "ok"
    assert payload["duplicate"] is False
    assert [item["table"] for item in payload["receipt"]["targets"]] == ["collector_runs"]
    assert _thread(instance, "thread-review")[5] == version


def test_new_threads_get_deterministic_ids(tmp_path: Path) -> None:
    found = []
    for name in ("one", "two"):
        instance = collector_instance(tmp_path / name, "gmail-work", "slack-team")
        code, payload, err = collect(instance, "gmail-work", gmail_doc())
        assert code == 0, (payload, err)
        code, payload, err = collect(instance, "slack-team", slack_doc())
        assert code == 0, (payload, err)
        found.append(
            ro_rows(
                instance,
                "SELECT channel, external_id, id FROM threads ORDER BY channel, external_id",
            )
        )
    assert found[0] == found[1]
    rows = {(channel, external): thread_id for channel, external, thread_id in found[0]}
    assert rows[("email", "thread-review")] == stable_id("thr-email-", "work", "thread-review")
    assert rows[("slack", ASK)] == stable_id("thr-slack-", "team", ASK)


def test_existing_thread_is_matched_by_channel_account_and_external_id(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    _apply(
        instance,
        make_packet(
            "tester",
            source_op("src-review", "A thread created before collection."),
            upsert_op(
                "upsert_thread",
                {
                    "id": "thr-review",
                    "channel": "email",
                    "account": "work",
                    "external_id": "thread-review",
                    "subject": "Old subject",
                    "last_inbound_at": "2026-10-01T09:00:00Z",
                    "outbound_seen": False,
                },
                "src-review",
            ),
        ),
    )
    doc = gmail_doc(
        fetched_at="2026-10-07T00:00:00Z",
        threads=[
            _mail("thread-review", [("2026-10-06T09:00:00Z", "inbound")], "Updated subject"),
        ],
    )
    code, payload, err = collect(instance, "gmail-work", doc)
    assert code == 0, (payload, err)
    assert _thread(instance, "thread-review")[:3] == ("thr-review", 1, "Updated subject")
    _add_collector(instance, "gmail-other", "gmail", "gmailother", "other", _MESSAGE_OPS)
    other = gmail_doc(
        fetched_at="2026-10-07T01:00:00Z",
        account="other",
        threads=[
            _mail("thread-review", [("2026-10-06T09:00:00Z", "inbound")], "Other account"),
        ],
    )
    code, payload, err = collect(instance, "gmail-other", other)
    assert code == 0, (payload, err)
    rows = ro_rows(
        instance,
        "SELECT account, id FROM threads WHERE external_id = 'thread-review' ORDER BY account",
    )
    assert rows == [
        ("other", stable_id("thr-email-", "other", "thread-review")),
        ("work", "thr-review"),
    ]


def _event(event_id: str = "evt-board-prep", **overrides) -> dict:
    event = {
        "id": event_id,
        "title": "Board prep",
        "start": "2026-10-09T01:00:00Z",
        "end": "2026-10-09T02:00:00Z",
        "all_day": False,
        "status": "confirmed",
        "attendee_count": 4,
        "updated": "2026-10-07T03:00:00Z",
        "updated_by_self": False,
    }
    event.update(overrides)
    return event


def _link_event(instance: Path, thread_id: str) -> None:
    _apply(
        instance,
        make_packet(
            "plain",
            source_op("src-event", "A slot linked to a calendar thread."),
            upsert_op(
                "upsert_action",
                {
                    "id": "act-event",
                    "title": "Hold the slot",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": NOW,
                },
                "src-event",
            ),
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-event",
                    "action_id": "act-event",
                    "thread_id": thread_id,
                    "role": "event",
                    "since": "2026-10-07T03:00:00Z",
                },
                "src-event",
            ),
        ),
    )


def _last_packet(instance: Path, agent: str) -> dict:
    raw = ro_rows(
        instance,
        "SELECT packet_json FROM write_log WHERE agent = ? ORDER BY rowid DESC LIMIT 1",
        (agent,),
    )[0][0]
    return json.loads(raw)


def test_calendar_creates_meetings_and_event_threads(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "work-cal")
    away = _event(
        "evt-away",
        title="",
        start="2026-10-10T00:00:00Z",
        end="2026-10-11T00:00:00Z",
        all_day=True,
        attendee_count=0,
        updated="2026-10-07T04:00:00Z",
    )
    code, payload, err = collect(instance, "work-cal", calendar_doc(events=[_event(), away]))
    assert code == 0, (payload, err)
    thread_id = stable_id("thr-calendar-", "work-cal", "evt-board-prep")
    meeting_id = stable_id("mtg-", "work-cal", "evt-board-prep")
    assert ro_rows(
        instance,
        "SELECT id, channel, account, external_id, subject, last_inbound_at, last_outbound_at, "
        "outbound_seen FROM threads WHERE external_id = 'evt-board-prep'",
    ) == [
        (
            thread_id,
            "calendar",
            "work-cal",
            "evt-board-prep",
            "Board prep",
            "2026-10-07T03:00:00Z",
            None,
            1,
        )
    ]
    assert ro_rows(
        instance,
        "SELECT id, thread_id, title, starts_at, ends_at, all_day, status, attendee_count, "
        "project_id, transcript_source_id FROM meetings WHERE thread_id = ?",
        (thread_id,),
    ) == [
        (
            meeting_id,
            thread_id,
            "Board prep",
            "2026-10-09T01:00:00Z",
            "2026-10-09T02:00:00Z",
            0,
            "confirmed",
            4,
            None,
            None,
        )
    ]
    away_thread = stable_id("thr-calendar-", "work-cal", "evt-away")
    assert ro_rows(
        instance,
        "SELECT id, title, starts_at, ends_at, all_day FROM meetings WHERE thread_id = ?",
        (away_thread,),
    ) == [
        (
            stable_id("mtg-", "work-cal", "evt-away"),
            "(no title)",
            "2026-10-10T00:00:00Z",
            "2026-10-11T00:00:00Z",
            1,
        )
    ]
    bad = calendar_doc(
        fetched_at="2026-10-07T08:00:00Z",
        events=[
            _event(
                "evt-bad",
                all_day=True,
                start="2026-10-10T01:00:00Z",
                end="2026-10-11T00:00:00Z",
                updated="2026-10-07T05:00:00Z",
            )
        ],
    )
    code, payload, err = collect(instance, "work-cal", bad)
    assert code == 1, (payload, err)
    assert payload["status"] == "error"
    assert _run_error(instance) == "event evt-bad: all-day times must be midnight UTC"


def test_calendar_update_by_someone_else_raises_new_message_on_event_links(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "work-cal")
    code, payload, err = collect(instance, "work-cal", calendar_doc())
    assert code == 0, (payload, err)
    thread_id = stable_id("thr-calendar-", "work-cal", "evt-board-prep")
    _link_event(instance, thread_id)
    assert ro_rows(
        instance,
        "SELECT new_message FROM thread_link_state WHERE link_id = 'lnk-event'",
    ) == [(0,)]
    later = calendar_doc(
        fetched_at="2026-10-07T12:00:00Z",
        events=[_event(updated="2026-10-07T06:00:00Z")],
    )
    code, payload, err = collect(instance, "work-cal", later)
    assert code == 0, (payload, err)
    assert _thread(instance, "evt-board-prep")[3:5] == ("2026-10-07T06:00:00Z", None)
    assert ro_rows(
        instance,
        "SELECT new_message FROM thread_link_state WHERE link_id = 'lnk-event'",
    ) == [(1,)]
    news = [item for item in _signals(instance)["signals"] if item["kind"] == "new_message"]
    assert len(news) == 1
    assert news[0]["thread_id"] == thread_id
    assert news[0]["subject"]["id"] == "act-event"


def test_calendar_update_by_self_is_outbound(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "work-cal")
    code, payload, err = collect(instance, "work-cal", calendar_doc())
    assert code == 0, (payload, err)
    thread_id = stable_id("thr-calendar-", "work-cal", "evt-board-prep")
    _link_event(instance, thread_id)
    later = calendar_doc(
        fetched_at="2026-10-07T12:00:00Z",
        events=[_event(updated="2026-10-07T06:00:00Z", updated_by_self=True)],
    )
    code, payload, err = collect(instance, "work-cal", later)
    assert code == 0, (payload, err)
    row = _thread(instance, "evt-board-prep")
    assert row[3:5] == ("2026-10-07T03:00:00Z", "2026-10-07T06:00:00Z")
    assert ro_rows(
        instance,
        "SELECT new_message FROM thread_link_state WHERE link_id = 'lnk-event'",
    ) == [(0,)]
    assert _signals(instance)["counts"]["new_message"] == 0
    packet = _last_packet(instance, "workcal")
    thread_ops = [op for op in packet["ops"] if op["op"] == "upsert_thread"]
    assert len(thread_ops) == 1
    assert thread_ops[0]["row"] == {"id": thread_id, "last_outbound_at": "2026-10-07T06:00:00Z"}
    assert [op for op in packet["ops"] if op["op"] == "upsert_meeting"] == []


def test_cancelled_event_updates_meeting_status(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "work-cal")
    code, payload, err = collect(instance, "work-cal", calendar_doc())
    assert code == 0, (payload, err)
    meeting_id = stable_id("mtg-", "work-cal", "evt-board-prep")
    later = calendar_doc(
        fetched_at="2026-10-07T12:00:00Z",
        events=[_event(status="cancelled", updated="2026-10-07T06:00:00Z")],
    )
    code, payload, err = collect(instance, "work-cal", later)
    assert code == 0, (payload, err)
    assert ro_rows(instance, "SELECT status, version FROM meetings WHERE id = ?", (meeting_id,)) == [
        ("cancelled", 2)
    ]
    packet = _last_packet(instance, "workcal")
    meeting_ops = [op for op in packet["ops"] if op["op"] == "upsert_meeting"]
    assert len(meeting_ops) == 1
    assert meeting_ops[0]["base"] == 1
    assert meeting_ops[0]["row"] == {"id": meeting_id, "status": "cancelled"}


def test_older_event_snapshot_is_ignored(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "work-cal")
    code, payload, err = collect(instance, "work-cal", calendar_doc())
    assert code == 0, (payload, err)
    before = ro_rows(
        instance,
        "SELECT subject, version FROM threads WHERE external_id = 'evt-board-prep'",
    )
    same = calendar_doc(
        fetched_at="2026-10-07T12:00:00Z",
        events=[_event(title="Changed title")],
    )
    code, payload, err = collect(instance, "work-cal", same)
    assert code == 0, (payload, err)
    assert ro_rows(
        instance,
        "SELECT subject, version FROM threads WHERE external_id = 'evt-board-prep'",
    ) == before
    assert ro_rows(instance, "SELECT version FROM meetings") == [(1,)]
    older = calendar_doc(
        fetched_at="2026-10-07T13:00:00Z",
        events=[_event(title="Older title", updated="2026-10-07T01:00:00Z")],
    )
    code, payload, err = collect(instance, "work-cal", older)
    assert code == 0, (payload, err)
    assert ro_rows(
        instance,
        "SELECT subject, version FROM threads WHERE external_id = 'evt-board-prep'",
    ) == before
    assert ro_rows(instance, "SELECT version FROM meetings") == [(1,)]
