from __future__ import annotations

from brain.schemas import EXTERNAL_ID, SLACK_EXTERNAL_ID
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

_LABEL = r"^[a-z][a-z0-9-]{1,39}$"
_MESSAGE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["at", "direction"],
    "properties": {
        "at": utc_schema(),
        "direction": {"type": "string", "enum": ["inbound", "outbound"]},
    },
}
_KNOWN = (
    "SELECT id, external_id, subject, last_inbound_at, last_outbound_at, "
    "outbound_seen, version FROM threads WHERE channel = ? AND account = ? "
    "ORDER BY external_id"
)
_OPS = ("add_source", "upsert_thread", "record_collector_run")
_ACCOUNT = Option("label", required=True)


def _thread_schema(id_pattern: str, with_subject: bool) -> dict:
    properties = {
        "id": {"type": "string", "pattern": id_pattern},
        "messages": {
            "type": "array",
            "min_items": 1,
            "max_items": 1000,
            "items": _MESSAGE,
        },
    }
    required = ["id", "messages"]
    if with_subject:
        properties["subject"] = one_line(0, 300)
        required.append("subject")
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


def _schema(format_name: str, id_pattern: str, with_subject: bool) -> dict:
    return document_schema(
        format_name,
        {
            "account": {"type": "string", "pattern": _LABEL},
            "outbound_visible": {"type": "boolean"},
            "threads": {
                "type": "array",
                "min_items": 0,
                "max_items": 5000,
                "items": _thread_schema(id_pattern, with_subject),
            },
        },
        ("account", "outbound_visible", "threads"),
    )


def _latest(messages: list, direction: str) -> str | None:
    times = [item["at"] for item in messages if item["direction"] == direction]
    return max(times) if times else None


def _later(stored: str | None, observed: str | None) -> str | None:
    if observed is None:
        return None
    if stored is None or observed > stored:
        return observed
    return None


def _check(doc: dict) -> None:
    seen: set[str] = set()
    visible = doc["outbound_visible"]
    for thread in doc["threads"]:
        external_id = thread["id"]
        if external_id in seen:
            raise DocumentError(f"duplicate thread id {external_id}")
        seen.add(external_id)
        for message in thread["messages"]:
            if message["direction"] == "outbound" and not visible:
                raise DocumentError(
                    f"thread {external_id}: outbound message but outbound_visible is false"
                )
            ensure_not_future(
                message["at"],
                doc["fetched_at"],
                f"thread {external_id} message at is in the future",
            )


def _locator(external_id: str) -> str:
    return f"thread {external_id[:180]}"


def _evidence(thread: dict, with_subject: bool, inbound, outbound, visible: bool) -> dict:
    evidence = {
        "thread": thread["id"],
        "inbound": inbound,
        "outbound": outbound,
        "outbound_visible": visible,
    }
    if with_subject:
        evidence = {
            "thread": thread["id"],
            "subject": thread["subject"],
            "inbound": inbound,
            "outbound": outbound,
            "outbound_visible": visible,
        }
    return evidence


def _build(name: str, format_name: str, prefix: str, channel: str, source_kind: str, with_subject: bool, live: LiveAdapter,
           options: dict):
    def known(con, doc: dict, ctx: RunContext) -> dict:
        rows = con.execute(_KNOWN, (channel, ctx.options["account"]))
        threads = {}
        for row in rows:
            threads[row[1]] = {
                "id": row[0],
                "subject": row[2],
                "last_inbound_at": row[3],
                "last_outbound_at": row[4],
                "outbound_seen": row[5],
                "version": row[6],
            }
        return {"threads": threads}

    def plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
        ensure_not_future(doc["fetched_at"], ctx.now, "fetched_at is in the future")
        account = ctx.options["account"]
        if doc["account"] != account:
            raise DocumentError(
                f"document account {doc['account']} doesn't match collector account {account}"
            )
        _check(doc)
        stored = known_rows.get("threads") or {}
        visible = bool(doc["outbound_visible"])
        # With_subject = false keeps subjects out of the thread row and the batch source,
        # because with capture on a subject lives only in its capture source.
        subjects = with_subject and ctx.options.get("with_subject", True) is not False
        groups: list[Group] = []
        for thread in sorted(doc["threads"], key=lambda item: item["id"]):
            external_id = thread["id"]
            inbound = _latest(thread["messages"], "inbound")
            outbound = _latest(thread["messages"], "outbound")
            current = stored.get(external_id)
            if current is not None and current["subject"] == REDACTED:
                # A redacted subject stays redacted: the mailbox still holds the original, but neither the
                # thread row nor the evidence snapshot may carry it again.
                thread = {**thread, "subject": REDACTED}
            locator = _locator(external_id)
            if current is None:
                row = {
                    "id": stable_id(prefix, account, external_id),
                    "channel": channel,
                    "account": account,
                    "external_id": external_id,
                    "subject": thread["subject"] if subjects else "",
                    "outbound_seen": visible,
                }
                if inbound is not None:
                    row["last_inbound_at"] = inbound
                if outbound is not None:
                    row["last_outbound_at"] = outbound
                op = {
                    "op": "upsert_thread",
                    "base": None,
                    "source": {"locator": locator},
                    "row": row,
                }
            else:
                row = {"id": current["id"]}
                moved_in = _later(current["last_inbound_at"], inbound)
                moved_out = _later(current["last_outbound_at"], outbound)
                if moved_in is not None:
                    row["last_inbound_at"] = moved_in
                if moved_out is not None:
                    row["last_outbound_at"] = moved_out
                if visible and not current["outbound_seen"]:
                    row["outbound_seen"] = True
                if subjects and thread["subject"] and thread["subject"] != current["subject"]:
                    row["subject"] = thread["subject"]
                if len(row) == 1:
                    continue
                op = {
                    "op": "upsert_thread",
                    "base": current["version"],
                    "source": {"locator": locator},
                    "row": row,
                }
            groups.append(
                Group(
                    ops=(op,),
                    evidence=_evidence(thread, subjects, inbound, outbound, visible),
                )
            )
        return Plan(tuple(groups), len(doc["threads"]), plan_note(doc))

    return CollectorType(
        name=name,
        format=format_name,
        ops=_OPS,
        options=options,
        document_schema=_schema(format_name, EXTERNAL_ID if with_subject else SLACK_EXTERNAL_ID, with_subject),
        live=live,
        known=known,
        plan=plan,
        source_kind=source_kind,
    )


GMAIL = _build(
    "gmail",
    "agent-brain.gmail.v1",
    "thr-email-",
    "email",
    "email",
    True,
    LiveAdapter(
        type="gmail",
        config_keys=("live_query", "live_max_threads"),
        env_vars=("BRAIN_<NAME>_TOKEN_FILE",),
        access="Read-only metadata for one mailbox, headers and labels, no bodies",
    ),
    {"account": _ACCOUNT, "with_subject": Option("bool", default=True)},
)
SLACK = _build(
    "slack",
    "agent-brain.slack.v1",
    "thr-slack-",
    "slack",
    "slack",
    False,
    LiveAdapter(
        type="slack",
        config_keys=("live_channels", "live_max_threads"),
        env_vars=("BRAIN_<NAME>_TOKEN",),
        access="A user token with read-only history for the listed channels",
    ),
    {"account": _ACCOUNT},
)
