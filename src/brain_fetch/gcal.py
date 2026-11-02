"""Google Calendar: a fetcher for the existing calendar collector. No text is captured.
Scope: calendar.readonly.

The cursor is the syncToken, plus the times, title and attendee count of each event seen in the window, so a
cancelled event (which an incremental sync returns without times) can still be written. A 410 means the token
expired: it's dropped with the event map and the run does a full sync.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from brain import schemas
from brain_fetch import google, net
from brain.collectors.privacy import scrub
from brain_fetch.common import CALENDAR, Pulled, line

API = "https://www.googleapis.com/calendar/v3/calendars/{}/events"
MAX_PAGES = 40
AHEAD = timedelta(days=60)  # timeMax of a full sync: bounds the expansion of open-ended recurring events
RESYNC = timedelta(days=7)  # a full sync at least this often, so meetings beyond AHEAD come into view
MAX_EVENTS = 5000
_STATUSES = schemas.MEETING_STATUSES


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value) -> tuple[str, bool] | None:
    """(UTC time, all day). A date-only start or end is midnight UTC, as the collector requires."""
    if not isinstance(value, dict):
        return None
    try:
        if isinstance(value.get("dateTime"), str):
            parsed = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return None
            return _stamp(parsed), False
        if isinstance(value.get("date"), str):
            return datetime.strptime(value["date"], "%Y-%m-%d").strftime("%Y-%m-%dT00:00:00Z"), True
    except ValueError:
        return None
    return None


def _updated(value, fetched: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        stamp = _stamp(parsed) if parsed.tzinfo is not None else fetched
    except ValueError:
        stamp = fetched
    return min(stamp, fetched)


def _event(item: dict, known: dict, fetched: str) -> dict | None:
    event_id = item.get("id")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 500 or any(c.isspace() for c in event_id):
        return None
    status = item.get("status") if item.get("status") in _STATUSES else "confirmed"
    start, end = _when(item.get("start")), _when(item.get("end"))
    remembered = known.get(event_id) if isinstance(known.get(event_id), dict) else None
    if start is None or end is None:
        if status != "cancelled" or remembered is None:
            return None  # a cancelled event we never saw, or an event without readable times
        start, end = (remembered["start"], remembered["all_day"]), (remembered["end"], remembered["all_day"])
        title, attendees = remembered["title"], remembered["attendee_count"]
    else:
        title = line(scrub(line(item.get("summary"), 600)), 300)
        attendees = min(len(item.get("attendees") or []), 10000)
    all_day = start[1] and end[1]
    begin, finish = start[0], max(end[0], start[0])
    if all_day != start[1]:
        return None  # one end all-day and the other timed: the collector can't hold it
    event = {"id": event_id, "title": title, "start": begin, "end": finish, "all_day": all_day, "status": status,
             "attendee_count": attendees, "updated": _updated(item.get("updated"), fetched),
             "updated_by_self": net.field(item, "organizer", "self") is True}
    known[event_id] = {"start": begin, "end": finish, "all_day": all_day, "title": title, "attendee_count": attendees}
    return event


def _list(token: str, url: str, params: dict) -> tuple[int, list, str]:
    items, page = [], ""
    for _ in range(MAX_PAGES):
        status, payload = net.request("GET", url, token=token, params={**params, **({"pageToken": page} if page else {})})
        if status in (404, 410):
            return status, [], ""
        items.extend(item for item in net.field(payload, "items", default=[]) or [] if isinstance(item, dict))
        page = net.field(payload, "nextPageToken", default="")
        if not isinstance(page, str) or not page:
            sync = net.field(payload, "nextSyncToken", default="")
            return status, items, sync if isinstance(sync, str) else ""
    raise net.NetError(f"the calendar answer ran past {MAX_PAGES} pages", "limit")


def pull(ctx) -> Pulled:
    token = google.access_token(ctx.secret)
    url = API.format(net.quote(ctx.options["live_calendar_id"], safe=""))
    fetched = datetime.strptime(ctx.fetched, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    window = timedelta(days=ctx.options.get("live_window_days") or 14)
    sync = ctx.cursor.get("sync_token") if isinstance(ctx.cursor.get("sync_token"), str) else ""
    full_at = ctx.cursor.get("full_at") if isinstance(ctx.cursor.get("full_at"), str) else ""
    weekly = bool(sync) and (not full_at or full_at <= _stamp(fetched - RESYNC))
    if weekly:
        sync = ""
    # The event map is this collector's own memory of event times, kept across a full sync, so a cancelled
    # event can still be written; old entries are pruned by end time below.
    known = dict(ctx.cursor["events"]) if isinstance(ctx.cursor.get("events"), dict) else {}
    base = {"singleEvents": "true", "maxResults": 250}
    # showDeleted on a full sync, so a cancellation made just before it isn't lost (an incremental sync always
    # includes deleted events).
    full = {**base, "timeMin": _stamp(fetched - window), "timeMax": _stamp(fetched + AHEAD), "showDeleted": "true"}
    status, items, next_sync = _list(token, url, {**base, "syncToken": sync} if sync else full)
    reset = False
    if status == 410 and sync:
        reset, sync = True, ""
        status, items, next_sync = _list(token, url, full)
    if status == 404:
        raise net.NetError("the calendar wasn't found; check live_calendar_id", "config", status)
    if status == 410:
        raise net.NetError("Google answered 410 to a full calendar sync", "network", status)
    events = {}
    for item in items:
        event = _event(item, known, ctx.fetched)
        if event is not None:
            events[event["id"]] = event
    cutoff = _stamp(fetched - window)
    known = {key: value for key, value in known.items() if isinstance(value, dict) and value.get("end", "") >= cutoff}
    rows = sorted(events.values(), key=lambda event: event["id"])[:MAX_EVENTS]
    doc = {"format": CALENDAR, "fetched_at": ctx.fetched, "complete": bool(next_sync), "account": ctx.options["account"],
           "outbound_visible": True, "events": rows}
    cursor = {"sync_token": next_sync, "events": known, "full_at": full_at if sync else ctx.fetched}
    pulled = Pulled(cursor=cursor, complete=bool(next_sync), items=len(rows))
    pulled.docs.append((ctx.collector.name, doc))
    notes = ["the sync token had expired, so the window was read again" if reset else "",
             "Google gave no sync token; the next run reads the window again" if not next_sync else ""]
    pulled.note = "; ".join(filter(None, notes))
    return pulled


def probe(ctx) -> str:
    token = google.access_token(ctx.secret)
    url = API.format(net.quote(ctx.options["live_calendar_id"], safe=""))
    status, _ = net.request("GET", url, token=token, params={"maxResults": 1})
    if status == 404:
        raise net.NetError("the calendar wasn't found; check live_calendar_id", "config", status)
    return "ok"
