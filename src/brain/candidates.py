from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from brain.collectors.base import stable_id
from brain.config import Config
from brain.evidence_gate import contains, match_key
from brain.store import canonical_json
from brain.strict import candidate_note_errors, date_rule, day_date, parse_raw, value_in_quote
from brain.truth import key_parts, resolve, words
from brain.units import UnitError

_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_CLAIM_QUOTE_MAX = 500


@dataclass(frozen=True)
class Plan:
    kind: str
    ops: tuple[dict, ...]
    errors: tuple[dict, ...]
    appliable: bool


def change_id(agent: str, candidate: dict) -> str:
    return f"{agent}-{hashlib.sha256(canonical_json(candidate).encode()).hexdigest()[:24]}"


def plan(con: sqlite3.Connection, config: Config, candidate: dict, decided_at: str, *, agent: str = "") -> Plan:
    """The candidate's derived ops. agent is the extractor, part of a claim's id."""
    return PLANNERS[candidate["kind"]](con, config, candidate, decided_at, agent=agent)


def _finish(kind: str, errors: list[dict], ops: list[dict], *, appliable: bool) -> Plan:
    if errors:
        return Plan(kind, (), tuple(errors), appliable)
    return Plan(kind, tuple(ops), (), appliable)


def _source_row(con: sqlite3.Connection, source_id: str):
    return con.execute(
        "SELECT content, source_date, title FROM sources WHERE id = ?",
        (source_id,),
    ).fetchone()


def _open_candidate(con: sqlite3.Connection, candidate: dict) -> tuple[object | None, list[dict]]:
    errors: list[dict] = []
    source_id = candidate["source_id"]
    row = _source_row(con, source_id)
    if row is None:
        errors.append({"path": "/source_id", "message": f"source {source_id} doesn't exist"})
        return None, errors
    if not contains(match_key(row["content"]), match_key(candidate["quote"])):
        errors.append({"path": "/quote", "message": f"quote is not in source {source_id}"})
    return row, errors


def _person_error(con: sqlite3.Connection, path: str, person_id: str | None) -> dict | None:
    if not person_id:
        return None
    found = con.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone()
    if found is None:
        return {"path": path, "message": f"person {person_id} doesn't exist"}
    return None


def _workstream_error(config: Config, workstream: str) -> dict | None:
    if any(item.id == workstream for item in config.workstreams):
        return None
    return {
        "path": "/fields/workstream",
        "message": f"{workstream} is not a workstream of this instance",
    }


def _resolve_day(field: str, value, source, *, forward: bool) -> tuple[str | None, dict | None]:
    """A candidate's date: a day word resolves as strict.day_date says,
    and an ISO date passes in exactly strict's cases, with its message order."""
    if not isinstance(value, str) or source is None:
        return None, None
    pointer = f"/fields/{field}"
    if _ISO_DAY.fullmatch(value):
        tail = date_rule(field, value, source, forward=forward)
        if tail:
            return None, {"path": pointer, "message": tail}
        return value, None
    if not contains(match_key(source["content"]), value):
        return None, {"path": pointer, "message": f"{field} {value} isn't in the source"}
    resolved = day_date(value, source["source_date"], forward=forward)
    if resolved is None:
        return None, {"path": pointer, "message": f"{field} {value} isn't in the source"}
    return resolved.isoformat(), None


def _source_ref(candidate: dict) -> dict:
    return {"source_id": candidate["source_id"], "locator": candidate["locator"]}


def _plan_decision(
    con: sqlite3.Connection, config: Config, candidate: dict, _decided_at: str, *, agent: str = ""
) -> Plan:
    fields = candidate["fields"]
    source, errors = _open_candidate(con, candidate)
    title = fields["title"]
    row_id = stable_id("dec-", candidate["source_id"], match_key(title))
    if con.execute("SELECT 1 FROM decisions WHERE id = ?", (row_id,)).fetchone() is not None:
        errors.append(
            {
                "path": "/fields/title",
                "message": f"decision {row_id} already exists for this source and title",
            }
        )
    decided_on, date_error = _resolve_day("decided_on", fields.get("decided_on"), source, forward=False)
    if date_error is not None:
        errors.append(date_error)
    person_error = _person_error(con, "/fields/decided_by", fields.get("decided_by"))
    if person_error is not None:
        errors.append(person_error)
    workstream = fields["workstream"]
    workstream_error = _workstream_error(config, workstream)
    if workstream_error is not None:
        errors.append(workstream_error)
    supersedes = fields.get("supersedes")
    if supersedes:
        existing = con.execute(
            "SELECT project_id FROM decisions WHERE id = ?",
            (supersedes,),
        ).fetchone()
        if existing is None:
            errors.append(
                {"path": "/fields/supersedes", "message": f"decision {supersedes} doesn't exist"}
            )
        elif existing["project_id"] != workstream:
            errors.append(
                {
                    "path": "/fields/supersedes",
                    "message": (
                        f"decision {supersedes} is in workstream {existing['project_id']}, "
                        f"not {workstream}"
                    ),
                }
            )
    errors.extend(candidate_note_errors(fields, ("detail",), config.writes))
    if errors:
        return _finish("decision_candidate", errors, [], appliable=True)
    return _finish(
        "decision_candidate",
        [],
        [
            {
                "op": "upsert_decision",
                "base": None,
                "source": _source_ref(candidate),
                "row": {
                    "id": row_id,
                    "title": title,
                    "detail": fields.get("detail") or "",
                    "quote": candidate["quote"],
                    "decided_on": decided_on,
                    "decided_by_person_id": fields["decided_by"],
                    "status": fields["status"],
                    "project_id": workstream,
                    "supersedes_id": supersedes or None,
                },
            }
        ],
        appliable=True,
    )


def _since(stored, role: str) -> tuple[dict | None, str | None]:
    outbound = stored["last_outbound_at"]
    inbound = stored["last_inbound_at"]
    thread_id = stored["id"]
    if role == "awaiting_reply":
        if not outbound:
            return {
                "path": "/fields/thread",
                "message": f"thread {thread_id} has no outbound message for an awaiting_reply link",
            }, None
        return None, outbound
    if role == "owes_reply":
        if not inbound:
            return {
                "path": "/fields/thread",
                "message": f"thread {thread_id} has no inbound message for an owes_reply link",
            }, None
        return None, inbound
    stamps = [item for item in (outbound, inbound) if item]
    if not stamps:
        return {
            "path": "/fields/thread",
            "message": f"thread {thread_id} has no message for an event link",
        }, None
    return None, max(stamps)


def _plan_action(
    con: sqlite3.Connection, config: Config, candidate: dict, decided_at: str, *, agent: str = ""
) -> Plan:
    fields = candidate["fields"]
    source, errors = _open_candidate(con, candidate)
    title = fields["title"]
    row_id = stable_id("act-", candidate["source_id"], match_key(title))
    if con.execute("SELECT 1 FROM actions WHERE id = ?", (row_id,)).fetchone() is not None:
        errors.append(
            {
                "path": "/fields/title",
                "message": f"action {row_id} already exists for this source and title",
            }
        )
    workstream = fields["workstream"]
    workstream_error = _workstream_error(config, workstream)
    if workstream_error is not None:
        errors.append(workstream_error)
    for path, person_id in (
        ("/fields/lead", fields.get("lead")),
        ("/fields/waiting_on", fields.get("waiting_on")),
    ):
        person_error = _person_error(con, path, person_id)
        if person_error is not None:
            errors.append(person_error)
    due_date, due_error = _resolve_day("due_date", fields.get("due_date"), source, forward=True)
    if due_error is not None:
        errors.append(due_error)
    asked_on, asked_error = _resolve_day("asked_on", fields.get("asked_on"), source, forward=False)
    if asked_error is not None:
        errors.append(asked_error)
    link = None
    thread = fields.get("thread")
    if thread:
        stored = con.execute(
            "SELECT id, last_outbound_at, last_inbound_at FROM threads "
            "WHERE channel = ? AND account = ? AND external_id = ?",
            (thread["channel"], thread["account"], thread["external_id"]),
        ).fetchone()
        if stored is None:
            errors.append(
                {
                    "path": "/fields/thread",
                    "message": (
                        f"thread {thread['channel']} {thread['account']} {thread['external_id']} "
                        "isn't in the store"
                    ),
                }
            )
        else:
            since_error, since = _since(stored, thread["role"])
            if since_error is not None:
                errors.append(since_error)
            else:
                link = {
                    "op": "upsert_thread_link",
                    "base": None,
                    "source": _source_ref(candidate),
                    "row": {
                        "id": stable_id("lnk-", row_id, stored["id"]),
                        "action_id": row_id,
                        "thread_id": stored["id"],
                        "role": thread["role"],
                        "since": since,
                    },
                }
    errors.extend(candidate_note_errors(fields, ("detail", "next_step"), config.writes))
    if errors:
        return _finish("action_candidate", errors, [], appliable=True)
    ops = [
        {
            "op": "upsert_action",
            "base": None,
            "source": _source_ref(candidate),
            "row": {
                "id": row_id,
                "title": title,
                "detail": fields.get("detail") or "",
                "next_step": fields.get("next_step") or "",
                "status": fields["status"],
                "status_basis": fields["status_basis"],
                "project_id": workstream,
                "owner_person_id": fields.get("lead") or None,
                "waiting_on_person_id": fields.get("waiting_on") or None,
                "due_date": due_date,
                "due_kind": fields.get("due_kind"),
                "asked_on": asked_on,
                "last_verified_at": decided_at,
            },
        }
    ]
    if link is not None:
        ops.append(link)
    return _finish("action_candidate", [], ops, appliable=True)


def _names(con: sqlite3.Connection, kind: str, target: str) -> list[str]:
    """A target's names in normal form: its implicit name, then its configured aliases."""
    found = [words(target)]
    for (alias,) in con.execute(
        "SELECT alias FROM aliases WHERE kind = ? AND target = ? ORDER BY alias", (kind, target)
    ):
        if alias not in found:
            found.append(alias)
    return found


def _plan_claim(
    con: sqlite3.Connection, config: Config, candidate: dict, _decided_at: str, *, agent: str = ""
) -> Plan:
    """A version 2 claim candidate, checked by C1 to C6."""
    fields = candidate["fields"]
    source, errors = _open_candidate(con, candidate)
    quote = candidate["quote"]
    raw = fields["value_raw"]
    scope = fields.get("scope", "")
    if len(quote) > _CLAIM_QUOTE_MAX:
        errors.append({"path": "/quote", "message": f"a claim's quote is at most {_CLAIM_QUOTE_MAX} characters"})
    text = fields["key"]
    keys = resolve(con, text)
    if not keys:
        errors.append({"path": "/fields/key", "message": f"unknown fact key {text}"})
    elif len(keys) > 1:
        errors.append({"path": "/fields/key", "message": f"{text} matches more than one fact key: {', '.join(keys)}"})
    if len(keys) == 1 and source is not None:
        key = keys[0]
        registry = con.execute("SELECT * FROM fact_keys WHERE key = ?", (key,)).fetchone()
        try:
            values = parse_raw(registry, raw, source["source_date"])
        except UnitError as exc:
            errors.append({"path": "/fields/value_raw", "message": exc.tail})
        else:
            if not value_in_quote(registry, raw, values, quote, source["source_date"]):
                errors.append({"path": "/fields/value_raw", "message": f"value {raw} is not in the quote"})
        entity, attribute = key_parts(key)
        quote_words = words(quote)
        title_words = words(source["title"])
        if not any(contains(quote_words, name) for name in _names(con, "attribute", attribute) if name):
            errors.append(
                {
                    "path": "/quote",
                    "message": f"the quote doesn't name {words(attribute)} or one of its aliases",
                }
            )
        if not any(
            contains(quote_words, name) or contains(title_words, name)
            for name in _names(con, "entity", entity)
            if name
        ):
            errors.append(
                {
                    "path": "/quote",
                    "message": (
                        f"neither the quote nor the source title names {words(entity)} "
                        "or one of its aliases"
                    ),
                }
            )
        if scope and not (
            contains(match_key(quote), match_key(scope)) or contains(match_key(source["title"]), match_key(scope))
        ):
            errors.append(
                {"path": "/fields/scope", "message": f"scope {scope} is in neither the quote nor the source title"}
            )
    if errors:
        return _finish("claim_candidate", errors, [], appliable=True)
    row: dict = {
        # The extractor is part of the id, so two extractors' identical candidates are two claims.
        "id": stable_id("clm-", agent, candidate["source_id"], candidate["locator"], keys[0], scope, raw, quote),
        "key": keys[0],
    }
    if "scope" in fields:
        row["scope"] = scope
    row.update({"value_raw": raw, "basis": "reported", "quote": quote})
    if "supersedes" in fields:
        row["supersedes"] = list(fields["supersedes"])
    return _finish(
        "claim_candidate",
        [],
        [{"op": "add_claim", "row": row, "source": _source_ref(candidate)}],
        appliable=True,
    )


PLANNERS: dict[str, Callable[..., Plan]] = {
    "decision_candidate": _plan_decision,
    "action_candidate": _plan_action,
    "claim_candidate": _plan_claim,
}
