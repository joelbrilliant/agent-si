from __future__ import annotations

from brain.collectors.base import (
    REDACTED,
    CollectorType,
    DocumentError,
    Group,
    LiveAdapter,
    Option,
    Plan,
    RunContext,
    document_schema,
    ensure_not_future,
    one_line,
    plan_note,
    stable_id,
    utc_schema,
)

FORMAT = "agent-brain.calendar.v1"
_LABEL = r"^[a-z][a-z0-9-]{1,39}$"
_EVENT_ID = r"^\S{1,500}$"
_MIDNIGHT = "T00:00:00Z"
_KNOWN = (
    "SELECT t.id, t.external_id, t.subject, t.last_inbound_at, t.last_outbound_at, "
    "t.outbound_seen, t.version, "
    "m.id, m.title, m.starts_at, m.ends_at, m.all_day, m.status, m.attendee_count, m.version "
    "FROM threads AS t LEFT JOIN meetings AS m ON m.thread_id = t.id "
    "WHERE t.channel = 'calendar' AND t.account = ? "
    "ORDER BY t.external_id"
)

LIVE = LiveAdapter(
    type="calendar",
    config_keys=(),
    env_vars=("BRAIN_<NAME>_TOKEN_FILE",),
    access="Read-only access to events on one calendar",
)

_EVENT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "title",
        "start",
        "end",
        "all_day",
        "status",
        "attendee_count",
        "updated",
        "updated_by_self",
    ],
    "properties": {
        "id": {"type": "string", "pattern": _EVENT_ID},
        "title": one_line(0, 300),
        "start": utc_schema(),
        "end": utc_schema(),
        "all_day": {"type": "boolean"},
        "status": {"type": "string", "enum": ["confirmed", "tentative", "cancelled"]},
        "attendee_count": {"type": "integer", "minimum": 0, "maximum": 10000},
        "updated": utc_schema(),
        "updated_by_self": {"type": "boolean"},
    },
}

SCHEMA = document_schema(
    FORMAT,
    {
        "account": {"type": "string", "pattern": _LABEL},
        "outbound_visible": {"type": "boolean"},
        "events": {"type": "array", "min_items": 0, "max_items": 5000, "items": _EVENT},
        # Brain-fetch's failed export, so the run is recorded as an error with its cause.
        "error": one_line(0, 300),
        "error_class": {"type": "string", "enum": ["", "auth", "network", "folder", "limit", "config", "other"]},
    },
    ("account", "outbound_visible", "events"),
)


def _stored_title(title: str) -> str:
    return title if title else "(no title)"


def _latest(thread: dict) -> str | None:
    times = [item for item in (thread["last_inbound_at"], thread["last_outbound_at"]) if item]
    return max(times) if times else None


def _check(doc: dict) -> None:
    seen: set[str] = set()
    visible = doc["outbound_visible"]
    for event in doc["events"]:
        event_id = event["id"]
        if event_id in seen:
            raise DocumentError(f"duplicate event id {event_id}")
        seen.add(event_id)
        if event["end"] < event["start"]:
            raise DocumentError(f"event {event_id} ends before it starts")
        if event["all_day"] and (
            not str(event["start"]).endswith(_MIDNIGHT) or not str(event["end"]).endswith(_MIDNIGHT)
        ):
            raise DocumentError(f"event {event_id}: all-day times must be midnight UTC")
        if event["updated_by_self"] and not visible:
            raise DocumentError(f"event {event_id}: updated_by_self needs outbound_visible")
        ensure_not_future(
            event["updated"],
            doc["fetched_at"],
            f"event {event_id} updated is in the future",
        )


def known(con, doc: dict, ctx: RunContext) -> dict:
    rows = con.execute(_KNOWN, (ctx.options["account"],))
    threads = {}
    for row in rows:
        meeting = None
        if row[7] is not None:
            meeting = {
                "id": row[7],
                "title": row[8],
                "starts_at": row[9],
                "ends_at": row[10],
                "all_day": row[11],
                "status": row[12],
                "attendee_count": row[13],
                "version": row[14],
            }
        threads[row[1]] = {
            "id": row[0],
            "subject": row[2],
            "last_inbound_at": row[3],
            "last_outbound_at": row[4],
            "outbound_seen": row[5],
            "version": row[6],
            "meeting": meeting,
        }
    return {"threads": threads}


def _meeting_row(event: dict, title: str, meeting_id: str, thread_id: str, current: dict | None) -> dict | None:
    if current is None:
        return {
            "id": meeting_id,
            "thread_id": thread_id,
            "title": title,
            "starts_at": event["start"],
            "ends_at": event["end"],
            "all_day": bool(event["all_day"]),
            "status": event["status"],
            "attendee_count": event["attendee_count"],
        }
    row = {"id": current["id"]}
    if title != current["title"]:
        row["title"] = title
    if event["start"] != current["starts_at"]:
        row["starts_at"] = event["start"]
    if event["end"] != current["ends_at"]:
        row["ends_at"] = event["end"]
    if bool(event["all_day"]) != bool(current["all_day"]):
        row["all_day"] = bool(event["all_day"])
    if event["status"] != current["status"]:
        row["status"] = event["status"]
    if event["attendee_count"] != current["attendee_count"]:
        row["attendee_count"] = event["attendee_count"]
    if len(row) == 1:
        return None
    return row


def plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    ensure_not_future(doc["fetched_at"], ctx.now, "fetched_at is in the future")
    account = ctx.options["account"]
    if doc["account"] != account:
        raise DocumentError(
            f"document account {doc['account']} doesn't match collector account {account}"
        )
    if doc.get("error"):
        raise DocumentError(f"{doc.get('error_class') or 'other'}: {doc['error']}")
    _check(doc)
    stored = known_rows.get("threads") or {}
    visible = bool(doc["outbound_visible"])
    groups: list[Group] = []
    for event in sorted(doc["events"], key=lambda item: item["id"]):
        event_id = event["id"]
        current = stored.get(event_id)
        if current is not None:
            latest = _latest(current)
            if latest is not None and event["updated"] <= latest:
                continue
        title = _stored_title(event["title"])
        evidence = dict(event)
        if current is not None and (
            current["subject"] == REDACTED or (current["meeting"] is not None and current["meeting"]["title"] == REDACTED)
        ):
            # A redacted title stays redacted in the thread, the meeting and the evidence snapshot.
            title = REDACTED
            evidence["title"] = REDACTED
        locator = f"event {event_id[:180]}"
        if current is None:
            thread_id = stable_id("thr-calendar-", account, event_id)
            thread_row = {
                "id": thread_id,
                "channel": "calendar",
                "account": account,
                "external_id": event_id,
                "subject": title,
                "outbound_seen": visible,
            }
            if event["updated_by_self"]:
                thread_row["last_outbound_at"] = event["updated"]
            else:
                thread_row["last_inbound_at"] = event["updated"]
            thread_op = {
                "op": "upsert_thread",
                "base": None,
                "source": {"locator": locator},
                "row": thread_row,
            }
            meeting_base = None
        else:
            thread_id = current["id"]
            thread_row = {"id": thread_id}
            if title != current["subject"]:
                thread_row["subject"] = title
            when = "last_outbound_at" if event["updated_by_self"] else "last_inbound_at"
            thread_row[when] = event["updated"]
            if visible and not current["outbound_seen"]:
                thread_row["outbound_seen"] = True
            thread_op = {
                "op": "upsert_thread",
                "base": current["version"],
                "source": {"locator": locator},
                "row": thread_row,
            }
            meeting_base = None if current["meeting"] is None else current["meeting"]["version"]
        meeting_id = stable_id("mtg-", account, event_id)
        meeting_row = _meeting_row(
            event,
            title,
            meeting_id,
            thread_id,
            None if current is None else current["meeting"],
        )
        ops = [thread_op]
        if meeting_row is not None:
            ops.append(
                {
                    "op": "upsert_meeting",
                    "base": meeting_base,
                    "source": {"locator": locator},
                    "row": meeting_row,
                }
            )
        groups.append(Group(ops=tuple(ops), evidence=evidence))
    return Plan(tuple(groups), len(doc["events"]), plan_note(doc))


TYPE = CollectorType(
    name="calendar",
    format=FORMAT,
    ops=("add_source", "upsert_thread", "upsert_meeting", "record_collector_run"),
    # Brain-fetch feeds this collector directly, so its two fetch settings are options.
    options={
        "account": Option("label", required=True),
        "live_calendar_id": Option("query", default=""),
        "live_window_days": Option("days", default=14),
    },
    document_schema=SCHEMA,
    live=LIVE,
    known=known,
    plan=plan,
    source_kind="calendar",
)
