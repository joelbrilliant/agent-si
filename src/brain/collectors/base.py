from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import NoReturn
from zoneinfo import ZoneInfo

from brain.store import canonical_json


# The token a redaction leaves in a text value (brain.scrub.REDACTED; a test pins the two together). A collector keeps
# a redacted subject or title redacted instead of writing the original back.
REDACTED = "[redacted]"


class LiveDisabled(Exception):
    pass


class DocumentError(Exception):
    pass


@dataclass(frozen=True)
class LiveAdapter:
    type: str
    config_keys: tuple[str, ...]
    env_vars: tuple[str, ...]
    access: str

    def fetch(self, *args, **kwargs) -> NoReturn:
        raise LiveDisabled(f"live {self.type} collection is disabled until it is approved")


@dataclass(frozen=True)
class Option:
    kind: str
    required: bool = False
    default: object = None
    allowed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunContext:
    collector: str
    options: Mapping[str, object]
    timezone: str
    run_key: str
    now: str
    agent: str = ""  # the collector's agent, so known_facts can test its own claim
    # The five fields above keep their positions.
    instance_dir: str = ""
    allow_mass_change: bool = False
    principal_id: str = ""
    author_principal: bool = False
    marked: bool = False


@dataclass(frozen=True)
class Group:
    ops: tuple[dict, ...]
    evidence: dict | None = None


@dataclass(frozen=True)
class Plan:
    groups: tuple[Group, ...]
    items: int
    note: str = ""


@dataclass(frozen=True)
class CollectorType:
    name: str
    format: str
    ops: tuple[str, ...]
    options: Mapping[str, Option]
    document_schema: dict
    live: LiveAdapter
    known: Callable[[sqlite3.Connection, dict, RunContext], dict]
    plan: Callable[[dict, dict, RunContext], Plan]
    source_kind: str = "api"  # batch-source kind; files never emits one
    # May a new source of this type set supersedes_id directly, when its external_ref equals the
    # superseded source's? Only capture does.
    may_supersede: bool = False
    # May the type's agent have author_principal = true? Only capture.
    principal_author: bool = False


def hash_parts(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def stable_id(prefix: str, *parts: str) -> str:
    return prefix + hash_parts(*parts)[:24]


def local_date(utc: str, timezone_name: str) -> str:
    stamp = datetime.strptime(utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ensure_not_future(stamp: str, limit: str, message: str) -> None:
    if parse_utc(stamp) - parse_utc(limit) > timedelta(seconds=300):
        raise DocumentError(message)


def _stored_fact(row: dict, value_type: str):
    if value_type == "int":
        return row["value_int"]
    if value_type == "number":
        return row["value_real"]
    if value_type == "date":
        return row["value_date"]
    return row["value_text"]


def observe_facts(
    doc: dict,
    known_rows: dict,
    ctx: RunContext,
    values: Mapping[str, object],
    suffixes: tuple[str, ...],
    value_types: Mapping[str, str],
) -> tuple[Group, ...]:
    facts = known_rows.get("facts") or {}
    prefix = ctx.options["fact_prefix"]
    fetched_at = doc["fetched_at"]
    groups: list[Group] = []
    for suffix in suffixes:
        if suffix not in values:
            continue
        value = values[suffix]
        full = f"{prefix}.{suffix}"
        stored = facts.get(full)
        if stored is not None and stored["last_verified_at"] >= fetched_at:
            continue
        row: dict = {"key": full, "basis": "observed", "last_verified_at": fetched_at}
        # The value is re-sent whenever the collector's own claim isn't checked.
        changed = (
            stored is None
            or stored["status"] != "current"
            or not stored["own_checked"]
            or _stored_fact(stored, value_types[suffix]) != value
        )
        if changed:
            row["value"] = value
            row["as_of"] = local_date(fetched_at, ctx.timezone)
            # E13: the quote is the canonical evidence entry only, a contiguous run of the batch
            # source's words, and value_raw is the value as that entry writes it.
            row["quote"] = canonical_json({"fact": suffix, "value": value})
            row["value_raw"] = value if isinstance(value, str) else canonical_json(value)
        groups.append(
            Group(
                ops=(
                    {
                        "op": "upsert_fact",
                        "base": None if stored is None else stored["version"],
                        "source": {"locator": f"fact {suffix}"},
                        "row": row,
                    },
                ),
                evidence={"fact": suffix, "value": value},
            )
        )
    return tuple(groups)


def known_facts(con, prefix: str, names, agent: str = "") -> dict[str, dict]:
    keys = [f"{prefix}.{name}" for name in names]
    marks = ",".join("?" for _ in keys)
    rows = con.execute(
        "SELECT f.key, f.value_type, f.value_text, f.value_int, f.value_real, f.value_date, "
        "f.as_of, f.last_verified_at, f.version, f.status, "
        "EXISTS (SELECT 1 FROM claims WHERE key = f.key AND proposed_by = ? AND status = 'live' "
        "AND checked = 1) AS own_checked "
        "FROM facts AS f WHERE f.key IN (" + marks + ")",
        [agent, *keys],
    )
    facts = {}
    for row in rows:
        facts[row[0]] = {
            "value_type": row[1],
            "value_text": row[2],
            "value_int": row[3],
            "value_real": row[4],
            "value_date": row[5],
            "as_of": row[6],
            "last_verified_at": row[7],
            "version": row[8],
            "status": row[9],
            "own_checked": bool(row[10]),
        }
    return facts


def redacted_keys(con, prefix: str, names) -> set[str]:
    """The fact keys among prefix.<names> that have a claim a redaction rejected: a collector that stores free
    text as evidence for such a key leaves the text out from then on."""
    keys = [f"{prefix}.{name}" for name in names]
    marks = ",".join("?" for _ in keys)
    return {
        row[0]
        for row in con.execute(
            "SELECT DISTINCT key FROM claims WHERE status = 'rejected' AND resolution_locator GLOB 'redacted on *' "
            "AND key IN (" + marks + ")",
            keys,
        )
    }


def plan_note(doc: dict, *extras: str) -> str:
    notes = [item for item in extras if item]
    if doc.get("complete") is False:
        notes.append("the document is incomplete")
    return "; ".join(notes)


def utc_schema() -> dict:
    return {"type": "string", "format": "utc"}


def one_line(min_length: int, max_length: int) -> dict:
    return {
        "type": "string",
        "min_length": min_length,
        "max_length": max_length,
        "format": "single_line",
    }


def document_schema(format_name: str, properties: dict, required: tuple[str, ...]) -> dict:
    props = {
        "format": {"type": "string", "const": format_name},
        "fetched_at": utc_schema(),
        "complete": {"type": "boolean"},
        **properties,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["format", "fetched_at", "complete", *required],
        "properties": props,
    }
