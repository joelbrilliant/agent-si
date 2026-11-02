"""Calendar: the full sync, the syncToken, a cancelled event filled from the cursor, the 410
reset, and failures that hold the cursor. Every event is invented."""
from __future__ import annotations

from datetime import datetime, timedelta

from brain import store
from tests import fetch_net as fn
from tests.conftest import ro_rows
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

EVENTS = r"www\.googleapis\.com/calendar/v3/calendars/primary/events"
COLLECTORS = {"work-calendar": {"type": "calendar", "agent": "workcal", "account": "work",
                                "live_calendar_id": "primary", "live_window_days": 14}}
AGENTS = {"workcal": {"kind": "collector",
                      "operations": ["add_source", "upsert_thread", "upsert_meeting", "record_collector_run"]}}

KICKOFF = {"id": "evkickoff", "status": "confirmed", "summary": "Pilot kickoff",
           "start": {"dateTime": "2026-10-12T09:00:00+10:00"}, "end": {"dateTime": "2026-10-12T10:00:00+10:00"},
           "attendees": [{"email": "a@example.com"}, {"email": "b@example.com"}], "updated": "2026-10-08T01:02:03.456Z",
           "organizer": {"self": True}}
OFFSITE = {"id": "evoffsite", "status": "tentative", "summary": "Offsite", "start": {"date": "2026-10-15"},
           "end": {"date": "2026-10-16"}, "updated": "2026-10-07T00:00:00Z", "organizer": {"email": "sam@example.com"}}
NO_TIMES = {"id": "evbroken", "status": "confirmed", "summary": "Broken", "updated": "2026-10-07T00:00:00Z"}
FUTURE = {"id": "evfuture", "status": "confirmed", "summary": "Clock skew", "start": {"dateTime": "2026-10-20T00:00:00Z"},
          "end": {"dateTime": "2026-10-20T01:00:00Z"}, "updated": "2026-11-01T00:00:00Z"}


def world(tmp_path, monkeypatch):
    fn.secret(monkeypatch, tmp_path, "work-calendar", fn.GOOGLE_SECRET)
    fake = fn.FakeNet(monkeypatch)
    fake.google_token()

    def events(call):
        params = call["params"]
        if "syncToken" in params:
            return answers[params["syncToken"]]
        now = datetime.strptime(store.utc_now(), "%Y-%m-%dT%H:%M:%SZ")
        assert params["timeMin"] == (now - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert params["timeMax"] == (now + timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert (params["singleEvents"], params["showDeleted"]) == ("true", "true")
        if not params.get("pageToken"):
            fulls.append(store.utc_now())
        if params.get("pageToken") == "p2":
            return {"items": [NO_TIMES, FUTURE], "nextSyncToken": "sync-1"}
        return {"items": [KICKOFF, OFFSITE], "nextPageToken": "p2"}

    answers: dict = {}
    fulls: list = []
    fake.route("GET", EVENTS, events)
    fake.fulls = fulls
    return fn.instance(tmp_path, COLLECTORS, AGENTS), fake, answers


def meetings(inst) -> dict:
    return {row[0]: row[1:] for row in ro_rows(
        inst, "SELECT t.external_id, m.title, m.starts_at, m.ends_at, m.all_day, m.status, m.attendee_count, "
              "t.last_inbound_at, t.last_outbound_at FROM meetings AS m JOIN threads AS t ON t.id = m.thread_id")}


def test_full_then_incremental_then_410(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, answers = world(tmp_path, monkeypatch)
    code, result, err = fn.run(inst, "work-calendar")
    assert (code, result["status"], result["items"], result["cursor"], err) == (0, "ok", 3, "advanced", "")
    rows = meetings(inst)
    assert rows["evkickoff"] == ("Pilot kickoff", "2026-10-11T23:00:00Z", "2026-10-12T00:00:00Z", 0, "confirmed", 2,
                                 None, "2026-10-08T01:02:03Z")
    assert rows["evoffsite"] == ("Offsite", "2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z", 1, "tentative", 0,
                                 "2026-10-07T00:00:00Z", None)
    assert rows["evfuture"][6] == "2026-10-09T00:00:00Z"  # an update stamp after fetched_at is clamped
    assert "evbroken" not in rows
    data = fn.state_of(inst, "work-calendar")["data"]
    assert data["sync_token"] == "sync-1" and sorted(data["events"]) == ["evfuture", "evkickoff", "evoffsite"]
    # The incremental sync returns a cancelled event without times: the cursor supplies them.
    answers["sync-1"] = {"items": [{"id": "evkickoff", "status": "cancelled"}, {"id": "evunknown", "status": "cancelled"}],
                         "nextSyncToken": "sync-2"}
    clock.at("2026-10-09T01:00:00Z")
    fake.calls.clear()
    code, result, _ = fn.run(inst, "work-calendar")
    assert (code, result["items"]) == (0, 1)
    assert meetings(inst)["evkickoff"][4] == "cancelled" and "evunknown" not in meetings(inst)
    events_call = next(call for call in fake.calls if call["host"] == "www.googleapis.com")
    assert events_call["params"] == {"singleEvents": "true", "maxResults": "250", "syncToken": "sync-1"}
    # An expired token (410) drops the token and the map and reads the window again.
    answers["sync-2"] = (410, {"error": {"code": 410, "message": "Sync token is no longer valid"}})
    clock.at("2026-10-09T02:00:00Z")
    code, result, _ = fn.run(inst, "work-calendar")
    assert (code, result["cursor"]) == (0, "advanced")
    assert "the sync token had expired" in result["message"]
    assert fn.state_of(inst, "work-calendar")["data"]["sync_token"] == "sync-1"
    assert "access-example-token" not in fn.written(inst) and "refresh-example" not in fn.written(inst)


def test_failures_hold_the_cursor(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, answers = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-calendar")[0] == 0
    good = fn.state_of(inst, "work-calendar")
    for answer, cause in (((401, {}), "auth"), ((404, {}), "config"), ((502, b"bad gateway"), "network")):
        answers["sync-1"] = answer
        clock.at("2026-10-09T01:00:00Z")
        code, result, _ = fn.run(inst, "work-calendar")
        assert (code, result["cursor"]) == (1, "held") and result["message"].startswith(cause + ": ")
        failed = fn.exports(inst, "work-calendar")[-1]
        assert failed["complete"] is False and failed["error_class"] == cause and failed["events"] == []
    assert fn.state_of(inst, "work-calendar") == good
    last = ro_rows(inst, "SELECT status, error FROM collector_runs WHERE collector = 'work-calendar' "
                         "ORDER BY started_at DESC, rowid DESC LIMIT 1")
    assert last[0][0] == "error" and last[0][1].startswith("network: ")


def test_a_calendar_without_an_id_isnt_run(tmp_path, monkeypatch, clock: Clock) -> None:
    fn.no_secrets(monkeypatch, tmp_path)
    inst = fn.instance(tmp_path, {"work-calendar": {"type": "calendar", "agent": "workcal", "account": "work"}}, AGENTS)
    code, payload, _ = fn.bf("run", "--instance", str(inst))
    assert (code, payload) == (0, {"collectors": []})
    code, _, err = fn.bf("run", "--instance", str(inst), "work-calendar")
    assert (code, err.strip()) == (2, "brain-fetch: collector work-calendar has no live_calendar_id")


def test_a_full_sync_at_least_weekly(tmp_path, monkeypatch, clock: Clock) -> None:
    """R1: the full sync is bounded at 60 days ahead, so it's redone weekly to bring later meetings into view.
    The event map survives it, so a cancellation can still be written."""
    inst, fake, answers = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-calendar")[0] == 0
    answers["sync-1"] = {"items": [], "nextSyncToken": "sync-1"}
    for stamp in ("2026-10-12T00:00:00Z", "2026-10-15T23:59:59Z"):
        clock.at(stamp)
        assert fn.run(inst, "work-calendar")[1]["cursor"] == "advanced"
    assert fake.fulls == ["2026-10-09T00:00:00Z"]
    clock.at("2026-10-16T00:00:00Z")
    assert fn.run(inst, "work-calendar")[0] == 0
    assert fake.fulls == ["2026-10-09T00:00:00Z", "2026-10-16T00:00:00Z"]
    data = fn.state_of(inst, "work-calendar")["data"]
    assert data["full_at"] == "2026-10-16T00:00:00Z" and "evkickoff" in data["events"]


def test_calendar_titles_are_scrubbed_and_one_line(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, answers = world(tmp_path, monkeypatch)
    KICKOFF_ODD = {**KICKOFF, "id": "evodd", "summary": "Call Sam\n0412 345 678\x0b sam@example.com"}
    fake.route("GET", EVENTS, {"items": [KICKOFF_ODD], "nextSyncToken": "sync-9"})
    assert fn.run(inst, "work-calendar")[0] == 0
    assert meetings(inst)["evodd"][0] == "Call Sam [phone] [email]"
