"""Slack: listed channels only, bot and app posts dropped, threads once quiet for 24 hours,
edits as new versions, the threads export, and failures that hold the cursor. Everything here is invented."""
from __future__ import annotations

from datetime import datetime, timezone

from tests import fetch_net as fn
from tests.conftest import ro_rows
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

API = r"slack\.com/api/"
CHANNEL = "C0PILOT01"
COLLECTORS = {
    "work-slack": {"type": "capture", "agent": "workslack", "origin": "work-slack", "kinds": ["slack"],
                   "service": "slack", "live_channels": [CHANNEL], "threads": "work-slack-threads"},
    "work-slack-threads": {"type": "slack", "agent": "workslackthreads", "account": "work"},
}
AGENTS = {
    "workslack": {"kind": "collector", "operations": ["add_source", "record_collector_run"], "author_principal": True},
    "workslackthreads": {"kind": "collector", "operations": ["add_source", "upsert_thread", "record_collector_run"]},
}
NOW = int(datetime(2026, 10, 9, tzinfo=timezone.utc).timestamp())
PEOPLE = {"UALEX": "Alex Example", "USAM": "Sam Example"}


def ts(hours_ago: float, now: int = NOW) -> str:
    return f"{int(now - hours_ago * 3600)}.000100"


def msg(user, text, at, **extra) -> dict:
    return {"type": "message", "user": user, "text": text, "ts": at, **extra}


ROOT_QUIET, ROOT_OPEN = ts(40), ts(5)
HISTORY = [
    msg("USAM", "Hi <@UALEX>, plan is at <https://example.com/plan|plan>. Ring 0412 345 678 or <mailto:sam@example.com|sam@example.com> &amp; done",
        ts(2)),
    msg("UBOT", "Deploy finished", ts(3), bot_id="B01", subtype="bot_message"),
    msg("USAM", "<@USAM> has joined the channel", ts(4), subtype="channel_join"),
    msg("UALEX", "Pilot scope for <#C0OTHER9|general>?", ROOT_QUIET, thread_ts=ROOT_QUIET, reply_count=2,
        latest_reply=ts(30)),
    msg("USAM", "Budget question", ROOT_OPEN, thread_ts=ROOT_OPEN, reply_count=1, latest_reply=ts(1)),
    msg("UAPP", "Reminder", ts(6), app_id="A01"),
    msg("USAM", "Deck attached", ts(7), subtype="file_share", files=[{"name": "deck.pdf", "size": 40960}]),
    msg("UALEX", "I'll send the contract today <!here>", ts(8)),
]
REPLIES = {
    ROOT_QUIET: [HISTORY[3], msg("USAM", "Two sites first", ts(31), thread_ts=ROOT_QUIET),
                 msg("UBOT", "Auto reply", ts(30), thread_ts=ROOT_QUIET, bot_id="B01")],
    ROOT_OPEN: [HISTORY[4], msg("UALEX", "Up to 10k", ts(1), thread_ts=ROOT_OPEN)],
}


def world(tmp_path, monkeypatch, history=None):
    fn.secret(monkeypatch, tmp_path, "work-slack", "slack-user-token-example")
    fake = fn.FakeNet(monkeypatch)
    fake.route("GET", API + "auth.test", {"ok": True, "user_id": "UALEX", "team_id": "T01"})
    fake.route("GET", API + "conversations.info", {"ok": True, "channel": {"id": CHANNEL, "name": "pilot"}})
    fake.route("GET", API + "users.info", lambda call: {"ok": True, "user": {"name": call["params"]["user"].lower(),
                                                                            "profile": {"display_name": PEOPLE.get(call["params"]["user"], "")}}})
    fake.route("GET", API + "conversations.history", {"ok": True, "messages": history or HISTORY, "has_more": False})
    fake.route("GET", API + "conversations.replies", lambda call: {"ok": True, "messages": REPLIES[call["params"]["ts"]]})
    return fn.instance(tmp_path, COLLECTORS, AGENTS), fake


def captured(inst) -> dict:
    return {ref: (author, content) for ref, author, content in ro_rows(
        inst, "SELECT external_ref, author_person_id, content FROM sources WHERE id GLOB 'src-cap-*' "
              "AND NOT EXISTS (SELECT 1 FROM sources AS n WHERE n.supersedes_id = sources.id)")}


def ref(at: str) -> str:
    return f"slack:work-slack/{CHANNEL}/{at}"


def test_first_run_keeps_people_and_drops_bots(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    code, result, err = fn.run(inst, "work-slack")
    assert (code, result["status"], result["items"], result["cursor"], err) == (0, "ok", 4, "advanced", "")
    rows = captured(inst)
    assert sorted(rows) == sorted([ref(ts(2)), ref(ROOT_QUIET), ref(ts(7)), ref(ts(8))])
    author, content = rows[ref(ts(2))]
    assert author is None
    assert content == (
        f"kind: slack\nfrom: Sam Example\ndate: 2026-10-08\nsubject: #pilot: Hi @Alex Example, plan is at https://example.com/plan. Ring\nref: {ref(ts(2))}\n\n"
        "Hi @Alex Example, plan is at https://example.com/plan. Ring [phone] or [email] & done")
    author, content = rows[ref(ROOT_QUIET)]
    assert author is None  # Sam replied, so the thread isn't the principal's alone
    assert content.endswith("Pilot scope for #general?\n\nReplies:\n[17:00] Sam Example: Two sites first")
    assert "Auto reply" not in content
    assert rows[ref(ts(8))][0] == "per-alex-example" and rows[ref(ts(8))][1].endswith("today @here")
    assert "attachments: deck.pdf (40 KB)" in rows[ref(ts(7))][1]
    every = "\n".join(content for _, content in rows.values())
    for dropped in ("Deploy finished", "has joined", "Reminder", "Budget question", "UALEX", "USAM"):
        assert dropped not in every
    data = fn.state_of(inst, "work-slack")["data"]
    assert data == {"channels": {CHANNEL: ts(2)}, "open": {f"{CHANNEL}/{ROOT_OPEN}": ts(1)}}
    history = next(call for call in fake.calls if call["path"].endswith("conversations.history"))
    assert float(history["params"]["oldest"]) == NOW - 14 * 86400
    # Only the listed channel is ever requested.
    assert {call["params"].get("channel") for call in fake.calls if "channel" in call["params"]} == {CHANNEL}
    threads = ro_rows(inst, "SELECT external_id, last_inbound_at, last_outbound_at FROM threads WHERE channel = 'slack' ORDER BY external_id")
    assert (f"{CHANNEL}:{ROOT_QUIET}", "2026-10-07T17:00:00Z", "2026-10-07T08:00:00Z") in threads
    assert len(threads) == 4
    assert "slack-user-token-example" not in fn.written(inst)
    assert all(call["headers"]["Authorization"] == "Bearer slack-user-token-example" for call in fake.calls)


def test_threads_wait_until_quiet_and_edits_give_versions(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-slack")[0] == 0
    later = NOW + 25 * 3600
    clock.at("2026-10-10T01:00:00Z")
    edited = msg("USAM", "Hi <@UALEX>, the plan moved to Tuesday", ts(2), edited={"user": "USAM", "ts": ts(1, later)})
    fake.route("GET", API + "conversations.history", {"ok": True, "messages": [edited, msg("USAM", "New one", ts(1, later))]})
    fake.calls.clear()
    code, result, _ = fn.run(inst, "work-slack")
    assert (code, result["cursor"]) == (0, "advanced")
    history = next(call for call in fake.calls if call["path"].endswith("conversations.history"))
    assert float(history["params"]["oldest"]) == float(ts(2)) - 86400  # the cursor less a day, to catch edits
    rows = captured(inst)
    assert rows[ref(ROOT_OPEN)][1].endswith("Budget question\n\nReplies:\n[23:00] Alex Example: Up to 10k")
    assert rows[ref(ts(2))][1].endswith("Hi @Alex Example, the plan moved to Tuesday")
    chain = ro_rows(inst, "SELECT count(*), count(supersedes_id) FROM sources WHERE external_ref = ?", (ref(ts(2)),))
    assert chain == [(2, 1)]
    assert fn.state_of(inst, "work-slack")["data"]["open"] == {}
    assert ref(ts(1, later)).rsplit("/", 1)[1] == fn.state_of(inst, "work-slack")["data"]["channels"][CHANNEL]


def test_a_reply_inside_the_day_keeps_the_thread_waiting(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-slack")[0] == 0
    clock.at("2026-10-10T01:00:00Z")
    later = NOW + 25 * 3600
    REPLIES_LATER = [HISTORY[4], msg("UALEX", "Up to 10k", ts(1)), msg("USAM", "Agreed", ts(2, later))]
    fake.route("GET", API + "conversations.replies", {"ok": True, "messages": REPLIES_LATER})
    fake.route("GET", API + "conversations.history", {"ok": True, "messages": []})
    assert fn.run(inst, "work-slack")[0] == 0
    assert ref(ROOT_OPEN) not in captured(inst)
    assert fn.state_of(inst, "work-slack")["data"]["open"] == {f"{CHANNEL}/{ROOT_OPEN}": ts(2, later)}


def test_dms_only_when_asked(tmp_path, monkeypatch, clock: Clock) -> None:
    collectors = {**COLLECTORS, "work-slack": {**COLLECTORS["work-slack"], "include_dms": True}}
    fn.secret(monkeypatch, tmp_path, "work-slack", "slack-user-token-example")
    fake = fn.FakeNet(monkeypatch)
    fake.route("GET", API + "auth.test", {"ok": True, "user_id": "UALEX"})
    fake.route("GET", API + "conversations.info", {"ok": False, "error": "missing_scope"})
    fake.route("GET", API + "users.info", {"ok": True, "user": {"profile": {"real_name": "Sam Example"}}})
    fake.route("GET", API + "conversations.list", {"ok": True, "channels": [{"id": "D0SAM0001", "user": "USAM"}]})
    fake.route("GET", API + "conversations.history", lambda call: {"ok": True, "messages": [
        msg("USAM", f"Hello in {call['params']['channel']}", ts(2))]})
    inst = fn.instance(tmp_path, collectors, AGENTS)
    code, result, _ = fn.run(inst, "work-slack")
    assert (code, result["items"]) == (0, 2)
    titles = sorted(row[0] for row in ro_rows(inst, "SELECT title FROM sources WHERE id GLOB 'src-cap-*'"))
    assert titles == [f"#{CHANNEL}: Hello in {CHANNEL}", "#dm-Sam Example: Hello in D0SAM0001"]
    listing = next(call for call in fake.calls if call["path"].endswith("conversations.list"))
    assert listing["params"]["types"] == "im"


def test_failures_hold_the_cursor(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-slack")[0] == 0
    good = fn.state_of(inst, "work-slack")
    for answer, cause in (({"ok": False, "error": "invalid_auth"}, "auth"), ({"ok": False, "error": "ratelimited"}, "network"),
                          ((500, b"oops"), "network")):
        fake.route("GET", API + "auth.test", answer)
        clock.at("2026-10-09T01:00:00Z")
        code, result, _ = fn.run(inst, "work-slack")
        assert (code, result["cursor"]) == (1, "held") and result["message"].startswith(cause + ": ")
    fake.route("GET", API + "auth.test", {"ok": True, "user_id": "UALEX"})
    fake.route("GET", API + "conversations.history", {"ok": False, "error": "channel_not_found"})
    code, result, _ = fn.run(inst, "work-slack")
    assert (code, result["message"]) == (1, "config: Slack refused conversations.history (channel_not_found)")
    assert fn.state_of(inst, "work-slack") == good
