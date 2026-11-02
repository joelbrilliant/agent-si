from __future__ import annotations

import re

SOURCE_ID = r"^src-[a-z0-9][a-z0-9._-]{0,115}$"
RECORD_ID = r"^rec-[a-z0-9][a-z0-9._-]{0,115}$"
DECISION_ID = r"^dec-[a-z0-9][a-z0-9._-]{0,115}$"
ACTION_ID = r"^act-[a-z0-9][a-z0-9._-]{0,115}$"
CHECKLIST_ID = r"^chk-[a-z0-9][a-z0-9._-]{0,115}$"
THREAD_ID = r"^thr-[a-z0-9][a-z0-9._-]{0,115}$"
MEETING_ID = r"^mtg-[a-z0-9][a-z0-9._-]{0,115}$"
LINK_ID = r"^lnk-[a-z0-9][a-z0-9._-]{0,115}$"
SIGNAL_ID = r"^sig-[a-z0-9][a-z0-9._-]{0,115}$"
RUN_ID = r"^run-[a-z0-9][a-z0-9._-]{0,115}$"
CLAIM_ID = r"^clm-[a-z0-9][a-z0-9._-]{0,115}$"
SCOPE = r"^$|^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$"
ALIAS_TARGET = r"^[a-z0-9_.]{1,119}$"
PERSON_ID = r"^per-[a-z0-9][a-z0-9-]{0,59}$"
SYSTEM_ID = r"^sys-[a-z0-9][a-z0-9-]{0,59}$"
PROJECT_ID = r"^[a-z][a-z0-9-]{1,39}$"
FACT_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
AGENT = r"^[a-z][a-z0-9]{1,39}$"
CHANGE_ID = r"^[a-z][a-z0-9]{1,39}-[a-z0-9][a-z0-9._-]{0,110}$"
ACCOUNT = r"^[a-z][a-z0-9-]{1,39}$"
SLACK_EXTERNAL_ID = r"^[A-Z0-9]{2,20}:[0-9]{10}\.[0-9]{6}$"
EXTERNAL_ID = r"^\S{1,500}$"
PRINCIPAL_NAME = r"^[^\W\d_][\w']{0,39}$"
SHA256 = r"^[0-9a-f]{64}$"

SOURCE_KINDS = (
    "user_note",
    "email",
    "slack",
    "meeting_transcript",
    "calendar",
    "document",
    "web",
    "api",
    "observation",
    "config",
)
SYSTEM_KINDS = ("saas", "infra", "internal", "device", "other")
PROJECT_KINDS = ("workstream", "project")
PROJECT_STATUSES = ("active", "parked", "done")
VALUE_TYPES = ("text", "int", "number", "date", "bool", "url", "enum")
FACT_STATUSES = ("current", "disputed", "retired")
BASIS = ("observed", "reported", "inferred")
RECORD_KINDS = ("note", "reference", "process", "learning", "gotcha", "question")
RECORD_STATUSES = ("active", "superseded", "archived")
DECISION_STATUSES = ("proposed", "in_effect", "reversed")
ACTION_STATUSES = (
    "proposed",
    "open",
    "prepared",
    "scheduled",
    "sent",
    "waiting",
    "blocked",
    "done",
    "parked",
    "dropped",
)
DUE_KINDS = ("deadline", "meeting")
CHECKLIST_STATES = (
    "not_started",
    "in_progress",
    "waiting",
    "blocked",
    "done",
    "not_applicable",
)
THREAD_CHANNELS = ("email", "slack", "calendar")
MEETING_STATUSES = ("confirmed", "tentative", "cancelled")
LINK_ROLES = ("awaiting_reply", "owes_reply", "event")
LINK_STATUSES = ("active", "closed")
SIGNAL_KINDS = ("reply", "new_message", "overdue", "stale", "drift", "conflict", "other")
SIGNAL_SUBJECTS = ("actions", "checklist_items", "decisions", "facts", "projects", "records")
SIGNAL_STATUSES = ("open", "dismissed", "resolved")
SIGNAL_BASIS = ("observed", "inferred")
RUN_STATUSES = ("ok", "partial", "error")
# Links with sources (0008): what a link says about its subject. cites is the default.
LINK_LABELS = ("cites", "status", "due", "asked", "summary")
LINK_DEFAULT_LABEL = "cites"
LINKS_MAX = 20

COLLECTOR_OPS = frozenset({"add_source", "record_collector_run"})
EXTRACTOR_OPS = frozenset({"apply_candidate"})


def _id(pattern: str) -> dict:
    return {"type": "string", "pattern": pattern}


def _text(max_length: int, *, min_length: int = 0, single_line: bool = False, no_at: bool = False) -> dict:
    schema: dict = {"type": "string", "min_length": min_length, "max_length": max_length}
    if single_line:
        schema["format"] = "single_line"
    if no_at:
        schema["no_at"] = True
    return schema


def _opt(schema: dict) -> dict:
    merged = dict(schema)
    typ = merged.get("type")
    if isinstance(typ, list):
        if "null" not in typ:
            merged["type"] = [*typ, "null"]
    else:
        merged["type"] = [typ, "null"]
    return merged


def _enum(values, nullable: bool = False) -> dict:
    schema: dict = {"type": "string", "enum": list(values)}
    if nullable:
        return _opt(schema)
    return schema


UTC = {"type": "string", "format": "utc"}
DATE = {"type": "string", "format": "date"}
BOOL = {"type": "boolean"}
INT = {"type": "integer"}

INSERT_ENVELOPE = {
    "type": "object",
    "required": ["op", "row"],
    "properties": {
        "op": {"type": "string"},
        "row": {"type": "object"},
    },
}

# base is optional: an op without one creates the row when it doesn't exist, and is a conflict naming the
# current version when it does. "base": null means the same as leaving it out.
UPSERT_ENVELOPE = {
    "type": "object",
    "required": ["op", "row", "source"],
    "properties": {
        "op": {"type": "string"},
        "row": {"type": "object"},
        "source": {
            "type": "object",
            "required": ["source_id", "locator"],
            "properties": {
                "source_id": _id(SOURCE_ID),
                "locator": _text(200, min_length=1, single_line=True),
            },
        },
        "base": {"type": ["integer", "null"], "minimum": 1},
    },
}

# A locator with a non-whitespace character. validate.check full-matches a pattern, so "has a non-space
# character" is written whole.
_LOCATOR = {**_text(200, min_length=1, single_line=True), "pattern": r"(?s).*\S.*"}

# Links with sources (0008): the upsert envelope plus an optional list of further cited sources. The row's own
# source stays its primary citation; each item becomes a row of links. An op whose envelope is this one takes
# links, so LINK_OPS below is derived from it.
LINKED_ENVELOPE = {
    **UPSERT_ENVELOPE,
    "properties": {
        **UPSERT_ENVELOPE["properties"],
        "sources": {
            "type": "array",
            "min_items": 1,
            "max_items": LINKS_MAX,
            "unique_items": True,
            "items": {
                "type": "object",
                "required": ["source_id", "locator"],
                "properties": {
                    "source_id": _id(SOURCE_ID),
                    "locator": _LOCATOR,
                    "label": _enum(LINK_LABELS),
                },
            },
        },
    },
}

# add_claim, resolve_fact, set_aliases and verify_claim: a row and a cited source, and no base (ruling
# R1). Their locators land in columns whose CHECK trims, so a whitespace-only locator is refused here
#.
CLAIM_ENVELOPE = {
    "type": "object",
    "required": ["op", "row", "source"],
    "properties": {
        "op": {"type": "string"},
        "row": {"type": "object"},
        "source": {
            "type": "object",
            "required": ["source_id", "locator"],
            "properties": {
                "source_id": _id(SOURCE_ID),
                "locator": _LOCATOR,
            },
        },
    },
}

PACKET = {
    "type": "object",
    "required": ["packet_version", "change_id", "agent", "intent", "ops"],
    "properties": {
        "packet_version": {"type": "integer", "const": 1},
        "change_id": {"type": "string", "pattern": CHANGE_ID},
        "agent": {"type": "string", "pattern": AGENT},
        "intent": _text(500, min_length=1, single_line=True),
        "ops": {"type": "array", "min_items": 1, "max_items": 500, "items": {"type": "object"}},
    },
}

CANDIDATE_ENVELOPE = {
    "type": "object",
    "required": ["candidate_version", "kind", "source_id", "locator", "quote", "fields"],
    "properties": {
        "candidate_version": {"type": "integer"},
        "kind": {"type": "string", "pattern": r"^[a-z][a-z_]{2,39}_candidate$"},
        "source_id": _id(SOURCE_ID),
        # A derived op's locator lands in a column whose CHECK trims, so, as CLAIM_ENVELOPE's,
        # a whitespace-only locator is refused here.
        "locator": _LOCATOR,
        "quote": {**_text(2000, min_length=1), "min_words": 5},
        "fields": {"type": "object"},
    },
}

DAY_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PAST_DAY = r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|today|yesterday|" + "|".join(DAY_WORDS) + r")$"
FUTURE_DAY = r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|today|tomorrow|" + "|".join(DAY_WORDS) + r")$"
SLUG = r"^[a-z][a-z0-9-]{1,39}$"
TOPIC = r"^[a-z][a-z0-9-]{1,59}$"
CANDIDATE_ACTION_STATUSES = tuple(s for s in ACTION_STATUSES if s not in ("done", "dropped"))
CANDIDATE_BASIS = ("reported", "inferred")

CANDIDATE_KINDS: dict[str, dict] = {
    "decision_candidate": {
        "type": "object",
        "required": ["title", "decided_on", "decided_by", "status", "workstream"],
        "properties": {
            "title": _text(90, min_length=1, single_line=True),
            "decided_on": {"type": "string", "pattern": PAST_DAY},
            "decided_by": _id(PERSON_ID),
            "status": _enum(DECISION_STATUSES),
            "workstream": _id(SLUG),
            "supersedes": _opt(_id(DECISION_ID)),
            "detail": _text(2000),
        },
    },
    "action_candidate": {
        "type": "object",
        "required": ["title", "status", "status_basis", "workstream"],
        "properties": {
            "title": _text(90, min_length=1, single_line=True),
            "status": _enum(CANDIDATE_ACTION_STATUSES),
            "status_basis": _enum(CANDIDATE_BASIS),
            "workstream": _id(SLUG),
            "lead": _opt(_id(PERSON_ID)),
            "waiting_on": _opt(_id(PERSON_ID)),
            "due_date": _opt({"type": "string", "pattern": FUTURE_DAY}),
            "due_kind": _enum(DUE_KINDS, nullable=True),
            "asked_on": _opt({"type": "string", "pattern": PAST_DAY}),
            "detail": _text(2000),
            "next_step": _text(500),
            "thread": _opt({
                "type": "object",
                "required": ["channel", "account", "external_id", "role"],
                "properties": {
                    "channel": _enum(THREAD_CHANNELS),
                    "account": {"type": "string", "pattern": ACCOUNT},
                    "external_id": {"type": "string", "pattern": EXTERNAL_ID, "min_length": 1, "max_length": 500},
                    "role": _enum(LINK_ROLES),
                },
            }),
        },
    },
    "claim_candidate": {
        "type": "object",
        "required": ["key", "value_raw"],
        "properties": {
            "key": _text(200, min_length=3, single_line=True),  # a key, or alias words
            "value_raw": _text(500, min_length=1, single_line=True),
            "scope": {"type": "string", "pattern": SCOPE},
            "supersedes": {
                "type": "array",
                "min_items": 1,
                "max_items": 20,
                "unique_items": True,
                "items": _id(CLAIM_ID),
            },
        },
    },
}
# Each candidate kind's current format.
CANDIDATE_VERSIONS = {"decision_candidate": 1, "action_candidate": 1, "claim_candidate": 2}

ALIASES = {
    "type": "array",
    "max_items": 20,
    "items": _text(120, min_length=1, single_line=True, no_at=True),
}

PERSON_LIST = {
    "type": "array",
    "max_items": 20,
    "unique_items": True,
    "items": _id(PERSON_ID),
}

# A legacy_import agent's original times on an insert (0008, part D). Any other agent is refused.
ORIGINAL_TIMES = {"created_at": UTC, "updated_at": UTC}

ALLOWED_VALUES = {
    "type": "array",
    "min_items": 1,
    "max_items": 50,
    "items": _text(200, min_length=1),
}


def _op(
    *,
    table: str,
    key: str,
    envelope: dict,
    row: dict,
    required_create: tuple[str, ...],
    gate_fields: tuple[str, ...] = (),
    scope: str | None = None,
    identity: tuple[str, ...] = (),
    utc_fields: tuple[str, ...] = (),
    json_fields: tuple[str, ...] = (),
    bool_fields: tuple[str, ...] = (),
    kind: str = "upsert",
) -> dict:
    return {
        "table": table,
        "key": key,
        "kind": kind,
        "envelope": envelope,
        "row": row,
        "required_create": required_create,
        "gate_fields": gate_fields,
        "scope": scope,
        "identity": identity,
        "utc_fields": utc_fields,
        "json_fields": json_fields,
        "bool_fields": bool_fields,
    }


COMMAND_OPS = {
    "rebuild_search": {
        "table": "search_index",
        "kind": "command",
        "envelope": {"type": "object", "required": ["op"], "properties": {"op": {"type": "string"}}},
    },
    "redact_source": {
        "table": "sources",
        "kind": "command",
        "envelope": {
            "type": "object",
            "required": ["op", "source_id", "reason", "operator"],
            "properties": {
                "op": {"type": "string"},
                "source_id": _id(SOURCE_ID),
                "reason": {**_text(200, min_length=3, single_line=True), "pattern": r"^[^\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff@]{3,200}$"},
                "operator": {**_text(60, min_length=1, single_line=True), "pattern": r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,59}$"},
            },
        },
    },
    "apply_candidate": {
        "table": None,
        "kind": "command",
        "envelope": {
            "type": "object",
            "required": ["op", "candidate"],
            "properties": {"op": {"type": "string"}, "candidate": {"type": "object"}},
        },
    },
}

OPS = {
    "add_source": _op(
        table="sources",
        key="id",
        kind="insert",
        envelope=INSERT_ENVELOPE,
        required_create=("id", "kind", "title", "source_date", "content"),
        identity=("id",),
        row={
            "type": "object",
            "properties": {
                "id": _id(SOURCE_ID),
                "kind": _enum(SOURCE_KINDS),
                "title": _text(200, min_length=1, single_line=True),
                "uri": _text(2000, min_length=0),
                "source_date": DATE,
                "author_person_id": _opt(_id(PERSON_ID)),
                "content": _text(2_000_000, min_length=1),
                "external_ref": _text(500, min_length=0),
                "supersedes_id": _opt(_id(SOURCE_ID)),
            },
        },
    ),
    "upsert_person": _op(
        table="people",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("display_name",),
        identity=("id",),
        json_fields=("aliases",),
        row={
            "type": "object",
            "properties": {
                "id": _id(PERSON_ID),
                "display_name": _text(120, min_length=1, single_line=True, no_at=True),
                "aliases": ALIASES,
                "org": _text(120, min_length=0, no_at=True),
                "role": _text(120, min_length=0, no_at=True),
            },
        },
    ),
    "upsert_system": _op(
        table="systems",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("name", "kind"),
        identity=("id",),
        row={
            "type": "object",
            "properties": {
                "id": _id(SYSTEM_ID),
                "name": _text(120, min_length=1),
                "kind": _enum(SYSTEM_KINDS),
                "vendor": _text(120, min_length=0),
                "owner_person_id": _opt(_id(PERSON_ID)),
            },
        },
    ),
    "upsert_project": _op(
        table="projects",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("kind", "title"),
        gate_fields=("title", "description", "goal"),
        scope="id",
        identity=("id",),
        row={
            "type": "object",
            "properties": {
                "id": _id(PROJECT_ID),
                "kind": _enum(PROJECT_KINDS),
                "title": _text(120, min_length=1),
                "description": _text(2000, min_length=0),
                "goal": _text(500, min_length=0),
                "goal_date": _opt(DATE),
                "status": _enum(PROJECT_STATUSES),
                "sort_order": INT,
                "parent_id": _opt(_id(PROJECT_ID)),
            },
        },
    ),
    "upsert_fact_key": _op(
        table="fact_keys",
        key="key",
        envelope=UPSERT_ENVELOPE,
        required_create=("value_type",),
        identity=("key",),
        json_fields=("allowed_values", "value_words"),
        row={
            "type": "object",
            "properties": {
                "key": {"type": "string", "pattern": FACT_KEY, "min_length": 3, "max_length": 120},
                "value_type": _enum(VALUE_TYPES),
                "unit": _text(40, min_length=0),
                "allowed_values": _opt(ALLOWED_VALUES),
                "stale_after_days": _opt({"type": "integer", "minimum": 1, "maximum": 3650}),
                "description": _text(500, min_length=0),
                "value_words": _opt({"type": "object"}),
            },
        },
    ),
    "upsert_fact": _op(
        table="facts",
        key="key",
        envelope=UPSERT_ENVELOPE,
        required_create=("value", "basis", "as_of", "last_verified_at"),
        scope="fact",
        identity=("key",),
        utc_fields=("last_verified_at",),
        row={
            "type": "object",
            "properties": {
                "key": {"type": "string", "pattern": FACT_KEY, "min_length": 3, "max_length": 120},
                "value": {"type": ["string", "integer", "number", "boolean"]},
                "status": _enum(FACT_STATUSES),
                "basis": _enum(BASIS),
                "as_of": DATE,
                "last_verified_at": UTC,
                "quote": {**_text(500, min_length=1), "min_words": 3},
                "value_raw": _text(500, min_length=1, single_line=True),
            },
        },
    ),
    "add_claim": _op(
        table="claims",
        key="id",
        kind="claim",
        envelope=CLAIM_ENVELOPE,
        required_create=("id", "key", "value_raw", "basis", "quote"),
        scope="fact",
        row={
            "type": "object",
            "properties": {
                "id": _id(CLAIM_ID),
                "key": {"type": "string", "pattern": FACT_KEY, "min_length": 3, "max_length": 120},
                "scope": {"type": "string", "pattern": SCOPE},
                "value_raw": _text(500, min_length=1, single_line=True),
                "planned": BOOL,
                "basis": _enum(BASIS),
                "as_of": DATE,
                "quote": {**_text(500, min_length=1), "min_words": 3},
                "supersedes": {
                    "type": "array",
                    "min_items": 1,
                    "max_items": 20,
                    "unique_items": True,
                    "items": _id(CLAIM_ID),
                },
                # A legacy_import agent's fields.
                "status": _enum(("live", "planned", "superseded", "rejected")),
                "superseded_by": _id(CLAIM_ID),
                "resolution_source_id": _id(SOURCE_ID),
                "resolution_locator": _text(200, min_length=1, single_line=True),
            },
        },
    ),
    "verify_claim": _op(
        table="claims",
        key="claim_id",
        kind="verify",
        envelope=CLAIM_ENVELOPE,
        required_create=(),
        scope="fact",
        row={
            "type": "object",
            "required": ["claim_id", "quote"],
            "properties": {
                "claim_id": _id(CLAIM_ID),
                "quote": {**_text(500, min_length=1), "min_words": 3},
            },
        },
    ),
    "resolve_fact": _op(
        table="claims",
        key="key",
        kind="resolve",
        envelope=CLAIM_ENVELOPE,
        required_create=(),
        scope="fact",
        row={
            "type": "object",
            "required": ["key", "scope", "keep", "reject", "quote"],
            "properties": {
                "key": {"type": "string", "pattern": FACT_KEY, "min_length": 3, "max_length": 120},
                "scope": {"type": "string", "pattern": SCOPE},
                "keep": {
                    "type": "array",
                    "min_items": 1,
                    "max_items": 40,
                    "unique_items": True,
                    "items": _id(CLAIM_ID),
                },
                "reject": {
                    "type": "array",
                    "min_items": 0,
                    "max_items": 40,
                    "unique_items": True,
                    "items": _id(CLAIM_ID),
                },
                "quote": {**_text(500, min_length=1), "min_words": 3},
            },
        },
    ),
    "set_aliases": _op(
        table="aliases",
        key=None,
        kind="aliases",
        envelope=CLAIM_ENVELOPE,
        required_create=(),
        row={
            "type": "object",
            "required": ["aliases"],
            "properties": {
                "aliases": {
                    "type": "array",
                    "min_items": 0,
                    "max_items": 5000,
                    "items": {
                        "type": "object",
                        "required": ["kind", "alias", "target"],
                        "properties": {
                            "kind": _enum(("entity", "attribute")),
                            "alias": _text(80, min_length=1, single_line=True),
                            "target": {"type": "string", "pattern": ALIAS_TARGET},
                        },
                    },
                },
            },
        },
    ),
    # 1.8.0: correct who wrote a source. Only an author_principal agent may send it, and it cites the
    # source that says why (migration 0009).
    "set_source_author": _op(
        table="sources",
        key=None,
        kind="author",
        envelope=CLAIM_ENVELOPE,
        required_create=(),
        row={
            "type": "object",
            "required": ["source_id", "author_person_id"],
            "properties": {
                "source_id": _id(SOURCE_ID),
                "author_person_id": {"type": ["string", "null"], "pattern": PERSON_ID},
            },
        },
    ),
    "upsert_record": _op(
        table="records",
        key="id",
        envelope=LINKED_ENVELOPE,
        required_create=("kind", "title", "body"),
        gate_fields=("title", "body", "summary"),
        scope="project_id",
        identity=("id",),
        row={
            "type": "object",
            "properties": {
                "id": _id(RECORD_ID),
                "kind": _enum(RECORD_KINDS),
                "title": _text(200, min_length=1, single_line=True),
                "body": _text(100000, min_length=1),
                "summary": _text(1200, min_length=0),
                "status": _enum(RECORD_STATUSES),
                "project_id": _opt(_id(PROJECT_ID)),
                **ORIGINAL_TIMES,
            },
        },
    ),
    "upsert_decision": _op(
        table="decisions",
        key="id",
        envelope=LINKED_ENVELOPE,
        required_create=(
            "title",
            "quote",
            "decided_on",
            "decided_by_person_id",
            "status",
            "project_id",
        ),
        gate_fields=("title", "detail"),
        scope="project_id",
        identity=("id",),
        row={
            "type": "object",
            "properties": {
                "id": _id(DECISION_ID),
                "title": _text(90, min_length=1, single_line=True),
                "detail": _text(2000, min_length=0),
                "quote": {**_text(2000, min_length=1), "min_words": 5},
                "decided_on": DATE,
                "decided_by_person_id": _id(PERSON_ID),
                "status": _enum(DECISION_STATUSES),
                "governs_from": _opt(DATE),
                "project_id": _id(PROJECT_ID),
                "supersedes_id": _opt(_id(DECISION_ID)),
                **ORIGINAL_TIMES,
            },
        },
    ),
    "upsert_action": _op(
        table="actions",
        key="id",
        envelope=LINKED_ENVELOPE,
        required_create=("title", "status", "status_basis", "project_id", "last_verified_at"),
        gate_fields=("title", "detail", "next_step", "close_reason"),
        scope="project_id",
        identity=("id",),
        utc_fields=("last_verified_at", "closed_at"),
        row={
            "type": "object",
            "properties": {
                "id": _id(ACTION_ID),
                "title": _text(90, min_length=1, single_line=True),
                "detail": _text(2000, min_length=0),
                "next_step": _text(500, min_length=0),
                "status": _enum(ACTION_STATUSES),
                "status_basis": _enum(BASIS),
                "project_id": _id(PROJECT_ID),
                "owner_person_id": _opt(_id(PERSON_ID)),
                "waiting_on_person_id": _opt(_id(PERSON_ID)),
                "owner_person_ids": PERSON_LIST,
                "waiting_on_person_ids": PERSON_LIST,
                "due_date": _opt(DATE),
                "due_kind": _enum(DUE_KINDS, nullable=True),
                "asked_on": _opt(DATE),
                "blocked_by": _opt(_id(ACTION_ID)),
                "record_id": _opt(_id(RECORD_ID)),
                "last_verified_at": UTC,
                "closed_at": _opt(UTC),
                "close_reason": _text(500, min_length=0),
                **ORIGINAL_TIMES,
            },
        },
    ),
    "upsert_checklist_item": _op(
        table="checklist_items",
        key="id",
        envelope=LINKED_ENVELOPE,
        required_create=("project_id", "title", "state", "state_basis", "last_verified_at"),
        gate_fields=("title", "detail"),
        scope="project_id",
        identity=("id",),
        utc_fields=("last_verified_at",),
        row={
            "type": "object",
            "properties": {
                "id": _id(CHECKLIST_ID),
                "project_id": _id(PROJECT_ID),
                "section": _text(80, min_length=0, single_line=True),
                "title": _text(140, min_length=1, single_line=True),
                "detail": _text(2000, min_length=0),
                "state": _enum(CHECKLIST_STATES),
                "state_basis": _enum(BASIS),
                "owner_person_id": _opt(_id(PERSON_ID)),
                "waiting_on_person_id": _opt(_id(PERSON_ID)),
                "owner_person_ids": PERSON_LIST,
                "waiting_on_person_ids": PERSON_LIST,
                "land_by": _opt(DATE),
                "action_id": _opt(_id(ACTION_ID)),
                "external_ref": _text(200, min_length=0),
                "sort_order": INT,
                "last_verified_at": UTC,
                **ORIGINAL_TIMES,
            },
        },
    ),
    "upsert_thread": _op(
        table="threads",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("channel", "account", "external_id"),
        identity=("id", "channel", "account", "external_id"),
        utc_fields=("last_inbound_at", "last_outbound_at"),
        bool_fields=("outbound_seen",),
        row={
            "type": "object",
            "properties": {
                "id": _id(THREAD_ID),
                "channel": _enum(THREAD_CHANNELS),
                "account": {"type": "string", "pattern": ACCOUNT},
                "external_id": {"type": "string", "pattern": EXTERNAL_ID, "min_length": 1, "max_length": 500},
                "subject": _text(300, min_length=0, single_line=True),
                "last_inbound_at": _opt(UTC),
                "last_outbound_at": _opt(UTC),
                "outbound_seen": BOOL,
            },
        },
    ),
    "upsert_thread_link": _op(
        table="thread_links",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("thread_id", "role", "since"),
        identity=("id", "action_id", "checklist_item_id", "thread_id"),
        utc_fields=("since", "seen_through"),
        row={
            "type": "object",
            "properties": {
                "id": _id(LINK_ID),
                "action_id": _opt(_id(ACTION_ID)),
                "checklist_item_id": _opt(_id(CHECKLIST_ID)),
                "thread_id": _id(THREAD_ID),
                "role": _enum(LINK_ROLES),
                "since": UTC,
                "seen_through": _opt(UTC),
                "status": _enum(LINK_STATUSES),
            },
        },
    ),
    "upsert_signal": _op(
        table="signals",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("kind", "subject_table", "subject_id", "basis"),
        gate_fields=("detail",),
        identity=("id", "kind", "subject_table", "subject_id", "thread_id"),
        utc_fields=("resolved_at",),
        row={
            "type": "object",
            "properties": {
                "id": _id(SIGNAL_ID),
                "kind": _enum(SIGNAL_KINDS),
                "subject_table": _enum(SIGNAL_SUBJECTS),
                "subject_id": _text(120, min_length=1),
                "thread_id": _opt(_id(THREAD_ID)),
                "detail": _text(500, min_length=0),
                "status": _enum(SIGNAL_STATUSES),
                "basis": _enum(SIGNAL_BASIS),
                "resolved_at": _opt(UTC),
            },
        },
    ),
    "upsert_meeting": _op(
        table="meetings",
        key="id",
        envelope=UPSERT_ENVELOPE,
        required_create=("thread_id", "title", "starts_at", "ends_at", "status"),
        scope="project_id",
        identity=("id", "thread_id"),
        bool_fields=("all_day",),
        row={
            "type": "object",
            "properties": {
                "id": _id(MEETING_ID),
                "thread_id": _id(THREAD_ID),
                "title": _text(300, min_length=1, single_line=True),
                "starts_at": UTC,
                "ends_at": UTC,
                "all_day": BOOL,
                "status": _enum(MEETING_STATUSES),
                "attendee_count": {"type": "integer", "minimum": 0, "maximum": 10000},
                "project_id": _opt(_id(PROJECT_ID)),
                "transcript_source_id": _opt(_id(SOURCE_ID)),
            },
        },
    ),
    "record_collector_run": _op(
        table="collector_runs",
        key="id",
        kind="insert",
        envelope=INSERT_ENVELOPE,
        required_create=("id", "collector", "started_at", "finished_at", "status"),
        identity=("id",),
        utc_fields=("started_at", "finished_at"),
        row={
            "type": "object",
            "properties": {
                "id": _id(RUN_ID),
                "collector": {"type": "string", "pattern": ACCOUNT},
                "started_at": UTC,
                "finished_at": UTC,
                "status": _enum(RUN_STATUSES),
                "items": {"type": "integer", "minimum": 0},
                "input_sha256": {"type": "string", "pattern": r"^$|^[0-9a-f]{64}$"},
                "error": _text(2000, min_length=0),
            },
        },
    ),
}

PEOPLE_LISTS = {
    # table -> (list table, list key column)
    "actions": ("action_people", "action_id"),
    "checklist_items": ("checklist_item_people", "item_id"),
}
PEOPLE_ROLES = (
    # role, scalar column, list field
    ("owner", "owner_person_id", "owner_person_ids"),
    ("waiting_on", "waiting_on_person_id", "waiting_on_person_ids"),
)
# The ops that take a sources list, and the tables a link can name: derived from LINKED_ENVELOPE, so a new
# linked op is a link subject by construction (tests/test_links.py holds them to the links CHECK).
LINK_OPS = frozenset(name for name, spec in OPS.items() if spec["envelope"] is LINKED_ENVELOPE)
LINK_TABLES = tuple(sorted({OPS[name]["table"] for name in LINK_OPS}))
# The ops whose row may carry a legacy import's original created_at and updated_at.
ORIGINAL_TIME_OPS = frozenset(
    name for name, spec in OPS.items() if all(field in spec["row"]["properties"] for field in ORIGINAL_TIMES)
)
UPSERT_TABLES = {
    spec["table"]: spec["key"]
    for spec in OPS.values()
    if spec["kind"] == "upsert"
}

SUBJECT_KEY = {
    "actions": "id",
    "checklist_items": "id",
    "decisions": "id",
    "facts": "key",
    "projects": "id",
    "records": "id",
}

# -- Field classes ------------------------------------------------------------
# Every text column of every table is a fact, state, label, note, or evidence (or an
# "evidence or system") field. tests/test_strict.py keeps the map complete.
FIELD_CLASSES = ("fact", "state", "label", "note", "evidence")
_EVIDENCE_TABLES = {
    "sources": (
        "id", "kind", "title", "uri", "source_date", "author_person_id", "content", "content_sha256",
        "external_ref", "captured_at", "captured_by", "supersedes_id",
    ),
    "people": (
        "id", "display_name", "aliases", "org", "role", "source_id", "locator", "created_at", "updated_at",
        "updated_by",
    ),
    "write_log": (
        "change_id", "agent", "intent", "status", "decided_at", "reviewed_by", "reviewed_at", "payload_sha256",
        "error", "packet_json", "targets_json", "approved_via", "hold_reason",
    ),
    "collector_runs": (
        "id", "collector", "started_at", "finished_at", "status", "input_sha256", "error", "recorded_at",
        "recorded_by",
    ),
    "fact_history": (
        "key", "change", "value_type", "value_text", "value_date", "status", "basis", "as_of", "last_verified_at",
        "source_id", "locator", "recorded_at", "recorded_by", "quote",
    ),
    "record_history": (
        "record_id", "change", "kind", "title", "body", "status", "project_id", "source_id", "locator",
        "recorded_at", "recorded_by", "summary",
    ),
    "links": ("subject_table", "subject_id", "label", "source_id", "locator", "created_at", "created_by"),
    "search_keys": ("entity", "ref_id"),
    "unchecked_text": ("entity", "ref_id", "sentences", "source_id", "created_at", "created_by", "updated_at", "updated_by"),
    "note_meta": ("entity", "ref_id", "field", "written_by", "written_at"),
    "redaction_gate": ("source_id", "redacted_on", "tombstone"),
    "redaction_packets": ("change_id",),
    "source_author_changes": ("change_id", "source_id", "old_author_person_id", "new_author_person_id", "evidence_source_id", "locator", "changed_at", "changed_by"),
    "aliases": ("kind", "alias", "target"),
    "fact_keys": (
        "key", "value_type", "unit", "allowed_values", "description", "source_id", "locator", "created_at",
        "updated_at", "updated_by", "value_words",
    ),
}
_STAMPS = ("source_id", "locator", "created_at", "updated_at", "updated_by")
# table: (key column, fact, state, label, note, also evidence)
_CLASSED_TABLES = {
    "actions": (
        "id",
        (),
        (
            "status", "status_basis", "project_id", "owner_person_id", "waiting_on_person_id", "due_date",
            "due_kind", "asked_on", "blocked_by", "record_id", "last_verified_at", "closed_at",
        ),
        ("title",),
        ("detail", "next_step", "close_reason"),
        (),
    ),
    "checklist_items": (
        "id",
        (),
        (
            "project_id", "state", "state_basis", "owner_person_id", "waiting_on_person_id", "land_by",
            "action_id", "external_ref", "last_verified_at",
        ),
        ("title", "section"),
        ("detail",),
        (),
    ),
    "projects": ("id", (), ("kind", "goal_date", "status", "parent_id"), ("title",), ("description", "goal"), ()),
    "records": ("id", (), ("kind", "status", "project_id"), ("title", "summary"), ("body",), ()),
    "decisions": (
        "id",
        ("title", "quote"),
        ("decided_on", "decided_by_person_id", "status", "governs_from", "project_id", "supersedes_id"),
        (),
        ("detail",),
        (),
    ),
    "systems": ("id", (), ("kind", "owner_person_id"), ("name", "vendor"), (), ()),
    "threads": (
        "id",
        (),
        ("channel", "account", "external_id", "last_inbound_at", "last_outbound_at"),
        (),
        (),
        ("subject",),
    ),
    "thread_links": (
        "id",
        (),
        ("action_id", "checklist_item_id", "thread_id", "role", "since", "seen_through", "status"),
        (),
        (),
        (),
    ),
    "meetings": (
        "id",
        (),
        ("thread_id", "starts_at", "ends_at", "status", "project_id", "transcript_source_id"),
        ("title",),
        (),
        (),
    ),
    "signals": (
        "id",
        (),
        ("kind", "subject_table", "subject_id", "thread_id", "status", "basis", "resolved_at"),
        (),
        (),
        ("detail",),
    ),
    "facts": (
        "key",
        ("value_text", "value_date", "quote"),
        ("status", "basis", "as_of", "last_verified_at"),
        (),
        (),
        ("value_type",),
    ),
    "claims": (
        "id",
        ("value_text", "value_date", "value_raw", "quote"),
        (
            "key", "scope", "status", "basis", "as_of", "supersedes", "superseded_by", "resolution_source_id",
            "resolution_locator",
        ),
        (),
        (),
        ("value_type", "proposed_by", "created_by"),
    ),
}
_STATE_TABLES = {
    "action_people": ("action_id", "role", "person_id"),
    "checklist_item_people": ("item_id", "role", "person_id"),
}


def _field_class_map() -> dict[str, str]:
    classes: dict[str, str] = {}
    for table, columns in _EVIDENCE_TABLES.items():
        for column in columns:
            classes[f"{table}.{column}"] = "evidence"
    for table, (key, fact, state, label, note, extra) in _CLASSED_TABLES.items():
        for column in (key, *_STAMPS, *extra):
            classes[f"{table}.{column}"] = "evidence"
        for name, columns in (("fact", fact), ("state", state), ("label", label), ("note", note)):
            for column in columns:
                classes[f"{table}.{column}"] = name
    for table, columns in _STATE_TABLES.items():
        for column in columns:
            classes[f"{table}.{column}"] = "state"
    return classes


FIELD_CLASS: dict[str, str] = _field_class_map()
# Each op's note fields, in row-schema order. .get matters: a row holds properties that aren't
# columns, such as owner_person_ids.
NOTE_FIELDS: dict[str, tuple[str, ...]] = {
    name: fields
    for name, spec in OPS.items()
    if (
        fields := tuple(
            field
            for field in spec["row"]["properties"]
            if spec["table"] is not None and FIELD_CLASS.get(f"{spec['table']}.{field}") == "note"
        )
    )
}
# The read tools' entity names and the tables they read, so a message can say "records_query on project".
ENTITY_TABLE = {
    "action": "actions",
    "checklist_item": "checklist_items",
    "decision": "decisions",
    "record": "records",
    "project": "projects",
    "person": "people",
    "system": "systems",
    "thread": "threads",
    "thread_link": "thread_links",
    "meeting": "meetings",
    "signal": "signals",
    "source": "sources",
}
# Each note op's entity, from its table, so an op with note fields has one by construction.
NOTE_ENTITY = {
    name: next(entity for entity, table in ENTITY_TABLE.items() if table == OPS[name]["table"])
    for name in NOTE_FIELDS
}

# -- The Held line ---------------------------------------------------
# Anchored, so a free-text key quoted inside another message can't make the line name a key.
HELD_CONFLICT = re.compile(
    r"^op [0-9.]+ \(add_claim\): would (?:open a|widen the) conflict on "
    r"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+) \("
)


def held_conflict_reason(label: str, verb: str, key: str, bucket_label: str) -> str:
    """A verified candidate's conflict hold. verb is open or widen."""
    middle = "would open a conflict" if verb == "open" else "would widen the conflict"
    return f"op {label} (add_claim): {middle} on {key} ({bucket_label})"
