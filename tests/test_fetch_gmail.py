"""Gmail: capture with the privacy rules, the historyId cursor and its 404 reset, the
threads export, and failures that hold the cursor. Every message, name and address is invented."""
from __future__ import annotations

import base64

import pytest

from brain import config as config_mod, reads, store
from tests import fetch_net as fn
from tests.conftest import connect_readonly, ro_rows
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

API = r"gmail\.googleapis\.com/gmail/v1/users/me"
COLLECTORS = {
    "work-mail": {"type": "capture", "agent": "workmail", "origin": "work-mail", "kinds": ["email"],
                  "service": "gmail", "threads": "work-mail-threads"},
    "work-mail-threads": {"type": "gmail", "agent": "workmailthreads", "account": "work", "with_subject": False},
}
AGENTS = {
    "workmail": {"kind": "collector", "operations": ["add_source", "record_collector_run"], "author_principal": True},
    "workmailthreads": {"kind": "collector", "operations": ["add_source", "upsert_thread", "record_collector_run"]},
}
MS = 1760000000000  # 2025-10-09T08:53:20Z; every message is a few minutes after it


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(mid: str, thread: str, *, labels=("INBOX",), subject="Pilot plan", sender='"Sam Example" <sam@example.com>',
            to="Alex Example <alex@example.com>", text="Hello", html=None, minute=0, attachment=None) -> dict:
    parts = [{"mimeType": "text/plain", "body": {"data": _b64(text), "size": len(text)}}] if html is None else [
        {"mimeType": "text/html", "body": {"data": _b64(html), "size": len(html)}}]
    if attachment:
        parts.append({"mimeType": "application/pdf", "filename": attachment, "body": {"attachmentId": "a1", "size": 20480}})
    headers = [{"name": "Subject", "value": subject}, {"name": "From", "value": sender}, {"name": "To", "value": to}]
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "internalDate": str(MS + minute * 60000),
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


M1 = message("m1", "t1", subject="Pilot plan, call 0412 345 678", attachment="plan alex@example.com.pdf",
             text=("Hi Alex,\n\nThe pilot can start Monday. Ring me on +61 2 9876 5432 or mail sam@example.com.\n\n"
                   "-- \nSam Example\nHead of Example\n\nOn Thu, 8 Oct 2026 at 09:00, Alex Example <alex@example.com> wrote:\n"
                   "> Can the pilot start Monday?"))
M2 = message("m2", "t1", labels=("SENT",), sender="Alex Example <alex@example.com>", to='"Sam Example" <sam@example.com>',
             subject="Re: Pilot plan", html="<p>Great, Monday it is.</p><div class=\"gmail_quote\">On Thu Sam wrote: old</div>",
             minute=5)
M3 = message("m3", "t2", labels=("DRAFT",), subject="Draft", text="Not sent yet", minute=7)
M4 = message("m4", "t3", subject="Invoice", text="Invoice attached.", minute=9)


def world(tmp_path, monkeypatch, *, listed=("m1", "m2", "m3"), history=None, store_secret=True):
    if store_secret:
        fn.secret(monkeypatch, tmp_path, "work-mail", fn.GOOGLE_SECRET)
    else:
        fn.no_secrets(monkeypatch, tmp_path)
    fake = fn.FakeNet(monkeypatch)
    fake.google_token()
    messages = {"m1": M1, "m2": M2, "m3": M3, "m4": M4}
    fake.route("GET", API + "/profile", {"emailAddress": "alex@example.com", "historyId": "1000"})
    fake.route("GET", API + "/messages", {"messages": [{"id": mid, "threadId": messages[mid]["threadId"]} for mid in listed]})
    fake.route("GET", API + r"/messages/(m\d)", lambda call: messages[call["path"].rsplit("/", 1)[1]])
    fake.route("GET", API + "/threads/t1", {"id": "t1", "messages": [
        {"id": "m1", "labelIds": ["INBOX"], "internalDate": M1["internalDate"]},
        {"id": "m2", "labelIds": ["SENT"], "internalDate": M2["internalDate"]}]})
    fake.route("GET", API + "/threads/t3", {"id": "t3", "messages": [
        {"id": "m4", "labelIds": ["INBOX"], "internalDate": M4["internalDate"]}]})
    if history is not None:
        fake.route("GET", API + "/history", history)
    return fn.instance(tmp_path, COLLECTORS, AGENTS), fake


def test_first_run_captures_with_the_privacy_rules(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    code, result, err = fn.run(inst, "work-mail")
    assert (code, result["status"], result["cursor"], result["items"], err) == (0, "ok", "advanced", 2, "")
    export = fn.exports(inst, "work-mail")[-1]
    assert export["format"] == "agent-brain.capture.v1" and export["incremental"] is True and export["complete"] is True
    assert len(export["mark"]) == 64  # m2 is the principal's own, so brain-fetch signed the export
    rows = {ref: (author, content) for ref, author, content in ro_rows(
        inst, "SELECT external_ref, author_person_id, content FROM sources WHERE id GLOB 'src-cap-*'")}
    assert sorted(rows) == ["email:work-mail/m1", "email:work-mail/m2"]  # the draft is skipped
    author, content = rows["email:work-mail/m1"]
    assert author is None
    assert content == (
        "kind: email\nfrom: Sam Example\nto: Alex Example\ndate: 2025-10-09\nsubject: Pilot plan, call [phone]\n"
        "attachments: plan [email] (20 KB)\nref: email:work-mail/m1\n\n"
        "Hi Alex,\n\nThe pilot can start Monday. Ring me on [phone] or mail [email].")
    author, content = rows["email:work-mail/m2"]
    assert author == "per-alex-example" and content.endswith("\n\nGreat, Monday it is.")
    threads = ro_rows(inst, "SELECT external_id, subject, last_inbound_at, last_outbound_at, outbound_seen FROM threads "
                            "WHERE channel = 'email'")
    assert threads == [("t1", "", "2025-10-09T08:53:20Z", "2025-10-09T08:58:20Z", 1)]
    batch = ro_rows(inst, "SELECT content FROM sources WHERE id NOT GLOB 'src-cap-*' AND kind = 'email'")
    assert batch and all("Pilot" not in row[0] for row in batch)  # with_subject = false: no subject in the batch
    assert fn.state_of(inst, "work-mail")["data"] == {"history_id": "1000"}
    text = fn.written(inst)
    for secret_text in (fn.ACCESS, "refresh-example", "secret-example", "@example.com", "9876 5432", "0412"):
        assert secret_text not in text
    token_call = next(call for call in fake.calls if call["path"] == "/token")
    assert token_call["form"] == {"grant_type": "refresh_token", "client_id": "client-example",
                                  "client_secret": "secret-example", "refresh_token": "refresh-example"}
    assert all(call["headers"].get("Authorization") == f"Bearer {fn.ACCESS}" for call in fake.calls if call["host"] == "gmail.googleapis.com")
    assert all(call["timeout"] == 30 for call in fake.calls)


def test_later_runs_get_only_what_history_added(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-mail")[0] == 0
    fake.route("GET", API + "/profile", {"historyId": "1005"})
    fake.route("GET", API + "/messages", {"messages": [{"id": m, "threadId": t} for m, t in
                                                      (("m4", "t3"), ("m1", "t1"), ("m2", "t1"), ("m3", "t2"))]})
    fake.route("GET", API + "/history", lambda call: {"history": [{"messagesAdded": [{"message": {"id": "m4"}}]}]}
               if call["params"]["startHistoryId"] == "1000" else (404, {}))
    fake.calls.clear()
    clock.at("2026-10-09T01:00:00Z")
    code, result, _ = fn.run(inst, "work-mail")
    assert (code, result["items"], result["cursor"]) == (0, 1, "advanced")
    assert [path for path in fake.paths("gmail.googleapis.com") if "/messages/" in path] == ["/gmail/v1/users/me/messages/m4"]
    assert fn.state_of(inst, "work-mail")["data"] == {"history_id": "1005"}
    # A re-run with nothing new adds nothing, and gets no message in full.
    fake.route("GET", API + "/history", {"history": []})
    fake.calls.clear()
    clock.at("2026-10-09T02:00:00Z")
    before = ro_rows(inst, "SELECT count(*) FROM sources")[0][0]
    assert fn.run(inst, "work-mail")[1]["items"] == 0
    assert not [path for path in fake.paths() if "/messages/" in path]
    assert ro_rows(inst, "SELECT count(*) FROM sources")[0][0] == before  # nothing new, nothing stored


def test_a_404_history_reads_every_match_again(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-mail")[0] == 0
    captured = ro_rows(inst, "SELECT id FROM sources WHERE id GLOB 'src-cap-*' ORDER BY id")
    fake.route("GET", API + "/history", (404, {"error": {"code": 404}}))
    clock.at("2026-10-09T01:00:00Z")
    code, result, _ = fn.run(inst, "work-mail")
    assert (code, result["cursor"], result["items"]) == (0, "advanced", 2)
    assert "the history cursor was too old" in result["message"]
    assert ro_rows(inst, "SELECT id FROM sources WHERE id GLOB 'src-cap-*' ORDER BY id") == captured  # a no-op


@pytest.mark.parametrize("case", ["invalid_grant", "server", "no_secret", "bad_secret"])
def test_failures_hold_the_cursor_and_reach_the_intake_line(tmp_path, monkeypatch, clock: Clock, case: str) -> None:
    inst, fake = world(tmp_path, monkeypatch, store_secret=case != "no_secret")
    if case == "bad_secret":
        fn.secret(monkeypatch, tmp_path, "work-mail", "not json")
    if case in ("invalid_grant", "server"):
        assert fn.run(inst, "work-mail")[0] == 0
    good = fn.state_of(inst, "work-mail")
    if case == "invalid_grant":
        fake.route("POST", r"oauth2\.googleapis\.com/token", (400, {"error": "invalid_grant"}))
        cause, words = "auth", "expire after 7 days"
    elif case == "server":
        fake.route("GET", API + "/profile", (503, b"unavailable"))
        cause, words = "network", "had an error (503)"
    elif case == "no_secret":
        cause, words = "auth", "no credential"
    else:
        cause, words = "config", "isn't the JSON"
    for hour in range(1, 4):
        clock.at(f"2026-10-09T0{hour}:00:00Z")
        code, result, err = fn.run(inst, "work-mail")
        assert (code, result["status"], result["cursor"]) == (1, "error", "held")
        assert result["message"].startswith(f"{cause}: ") and words in result["message"]
    assert fn.state_of(inst, "work-mail") == good
    failed = fn.exports(inst, "work-mail")[-1]
    assert failed["complete"] is False and failed["error_class"] == cause and failed["items"] == []
    assert ro_rows(inst, "SELECT status FROM collector_runs WHERE collector = 'work-mail' ORDER BY started_at DESC LIMIT 1") == [("error",)]
    cfg = config_mod.load_config(inst / "brain.toml")
    con = connect_readonly(inst / "brain.sqlite3")
    try:
        entries = reads.intake_status(con, store.utc_now(), cfg)
    finally:
        con.close()
    assert [(e["collector"], e["state"], e["cause"]) for e in entries] == [("work-mail", "failing", cause)]
    assert fn.ACCESS not in fn.written(inst) and "refresh-example" not in fn.written(inst)


def test_no_secret_on_the_first_run_still_records_it(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch, store_secret=False)
    code, result, _ = fn.run(inst, "work-mail")
    assert (code, result["cursor"]) == (1, "held") and fn.state_of(inst, "work-mail") is None
    assert fake.calls == []  # nothing is requested without a credential


def test_a_429_waits_then_carries_on(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    answers = [(429, {}, {"Retry-After": "2"}), (429, {}, {"retry-after": "900"})]
    listing = {"messages": [{"id": "m1", "threadId": "t1"}]}
    fake.route("GET", API + "/messages", lambda call: answers.pop(0) if answers else listing)
    code, result, _ = fn.run(inst, "work-mail")
    assert (code, result["items"]) == (0, 1) and fake.slept == [2.0, 60.0]
    fake.route("GET", API + "/messages", (429, {}, {"Retry-After": "1"}))
    clock.at("2026-10-09T01:00:00Z")
    code, result, _ = fn.run(inst, "work-mail")
    assert (code, result["cursor"]) == (1, "held") and result["message"].startswith("network: ")


def test_no_query_override_uses_the_default(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake = world(tmp_path, monkeypatch)
    fn.run(inst, "work-mail")
    listing = next(call for call in fake.calls if call["path"].endswith("/messages"))
    assert listing["params"]["q"] == ("{in:inbox in:sent} -category:promotions -category:social -category:updates "
                                      "-category:forums newer_than:14d")


def test_odd_characters_never_fail_the_export(tmp_path, monkeypatch, clock: Clock) -> None:
    """R4: a control character in a one-line field once failed the export's schema on every run."""
    inst, fake = world(tmp_path, monkeypatch)
    fake.route("GET", API + "/messages", {"messages": [{"id": "m5", "threadId": "t5"}]})
    fake.route("GET", API + "/threads/t5", (404, {}))
    odd = message("m5", "t5", subject="Plan\x0bnext\r\nweek\x00", sender='"Sam\tExample\x1f" <sam@example.com>',
                  to="\x07 <kim@example.com>", attachment="notes v2.pdf")
    fake.route("GET", API + "/messages/m5", odd)
    code, result, _ = fn.run(inst, "work-mail")
    assert (code, result["items"], result["cursor"]) == (0, 1, "advanced")
    title, content = ro_rows(inst, "SELECT title, content FROM sources WHERE external_ref = 'email:work-mail/m5'")[0]
    assert title == "Plan next week"
    assert content.startswith("kind: email\nfrom: Sam Example\nto: (no name)\ndate: 2025-10-09\nsubject: Plan next week\n"
                              "attachments: notes v2.pdf (20 KB)\n")
