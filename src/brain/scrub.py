"""What a redaction writes: the tokens, the tombstone text, the column
inventory, and the scrubbing of quoted spans and of packets. Pure: no database access, so one definition
serves the op, `brain check` and the tests."""
from __future__ import annotations

import hashlib
import re

from brain import evidence_gate, schemas

REDACTED = "[redacted]"
REDACTED_URL = "https://redacted.invalid/"
TOMBSTONE_TITLE = "redacted"
TOMBSTONE_PREFIX = "redacted on "
TEXTUAL_TYPES = ("text", "enum", "url")
_QUOTE_OPEN = "\"“”'‘’"
_QUOTE_CLOSE = "\"“”'‘’"

# table -> {column: the longest value the column holds}. record_history has no limit.
SPAN_COLUMNS = {
    "projects": {"title": 120, "description": 2000, "goal": 500},
    "records": {"title": 200, "body": 100000, "summary": 1200},
    "decisions": {"title": 90, "detail": 2000},
    "actions": {"title": 90, "detail": 2000, "next_step": 500, "close_reason": 500},
    "checklist_items": {"title": 140, "detail": 2000},
    "signals": {"detail": 500},
}

# What a redaction does to every column of schemas.FIELD_CLASS. Anything not listed here is kept.
_TOKEN_COLUMNS = (
    "sources.title", "sources.content", "sources.uri", "sources.author_person_id",
    "claims.quote", "claims.value_raw", "claims.value_text",
    "facts.value_text", "facts.quote",
    "fact_history.value_text", "fact_history.quote",
    "decisions.quote",
    "threads.subject", "meetings.title",
    "write_log.packet_json", "write_log.error", "write_log.hold_reason",
    "collector_runs.error",
)
_SPAN_COLUMNS = (
    "projects.title", "projects.description", "projects.goal",
    "records.title", "records.body", "records.summary",
    "record_history.title", "record_history.body", "record_history.summary",
    "decisions.title", "decisions.detail",
    "actions.title", "actions.detail", "actions.next_step", "actions.close_reason",
    "checklist_items.title", "checklist_items.detail",
    "signals.detail", "unchecked_text.sentences",
)


# Columns a redaction keeps. Every column of schemas.FIELD_CLASS is in exactly one of the three
# lists, so a new column cannot be added without a decision (test 20). Why the text-bearing ones stay:
#   sources.external_ref, id, captured_by, kind: the re-fetch no-op and the audit need them (the ref names the item);
#   claims.key (the registry), locator (200 characters typed by the sender, never read by the gate), scope (a label
#     that can be a word from the source, and part of the claim's identity), the number and date values;
#   facts.value_date, every locator and write_log.intent (free text written by the sender);
#   checklist_items.section and external_ref (labels an agent typed), threads.external_id (an id, not words);
#   people, systems, fact_keys, aliases, note_meta, search_keys, thread_links, action_people,
#     checklist_item_people and the redaction tables (entities and config, not copies of a source);
#   links (0008): pointers to a source with a locator and a label, never its words, kept as every locator is.
_KEPT_COLUMNS = {
    "action_people": (
        "action_id", "role", "person_id",
    ),
    "actions": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "status", "status_basis",
        "project_id", "owner_person_id", "waiting_on_person_id", "due_date", "due_kind", "asked_on",
        "blocked_by", "record_id", "last_verified_at", "closed_at",
    ),
    "aliases": (
        "kind", "alias", "target",
    ),
    "checklist_item_people": (
        "item_id", "role", "person_id",
    ),
    "checklist_items": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "project_id", "state",
        "state_basis", "owner_person_id", "waiting_on_person_id", "land_by", "action_id", "external_ref",
        "last_verified_at", "section",
    ),
    "claims": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "value_type", "proposed_by",
        "created_by", "value_date", "key", "scope", "status", "basis", "as_of", "supersedes", "superseded_by",
        "resolution_source_id", "resolution_locator",
    ),
    "collector_runs": (
        "id", "collector", "started_at", "finished_at", "status", "input_sha256", "recorded_at", "recorded_by",
    ),
    "decisions": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "decided_on",
        "decided_by_person_id", "status", "governs_from", "project_id", "supersedes_id",
    ),
    "fact_history": (
        "key", "change", "value_type", "value_date", "status", "basis", "as_of", "last_verified_at",
        "source_id", "locator", "recorded_at", "recorded_by",
    ),
    "fact_keys": (
        "key", "value_type", "unit", "allowed_values", "description", "source_id", "locator", "created_at",
        "updated_at", "updated_by", "value_words",
    ),
    "facts": (
        "key", "source_id", "locator", "created_at", "updated_at", "updated_by", "value_type", "value_date",
        "status", "basis", "as_of", "last_verified_at",
    ),
    "meetings": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "thread_id", "starts_at",
        "ends_at", "status", "project_id", "transcript_source_id",
    ),
    "links": (
        "subject_table", "subject_id", "label", "source_id", "locator", "created_at", "created_by",
    ),
    "note_meta": (
        "entity", "ref_id", "field", "written_by", "written_at",
    ),
    "people": (
        "id", "display_name", "aliases", "org", "role", "source_id", "locator", "created_at", "updated_at",
        "updated_by",
    ),
    "projects": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "kind", "goal_date", "status",
        "parent_id",
    ),
    "record_history": (
        "record_id", "change", "kind", "status", "project_id", "source_id", "locator", "recorded_at",
        "recorded_by",
    ),
    "records": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "kind", "status", "project_id",
    ),
    "redaction_gate": (
        "source_id", "redacted_on", "tombstone",
    ),
    "redaction_packets": (
        "change_id",
    ),
    "source_author_changes": (
        "change_id", "source_id", "old_author_person_id", "new_author_person_id", "evidence_source_id", "locator", "changed_at", "changed_by",
    ),
    "search_keys": (
        "entity", "ref_id",
    ),
    "signals": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "kind", "subject_table",
        "subject_id", "thread_id", "status", "basis", "resolved_at",
    ),
    "sources": (
        "id", "kind", "source_date", "content_sha256", "external_ref", "captured_at", "captured_by",
        "supersedes_id",
    ),
    "systems": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "kind", "owner_person_id",
        "name", "vendor",
    ),
    "thread_links": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "action_id",
        "checklist_item_id", "thread_id", "role", "since", "seen_through", "status",
    ),
    "threads": (
        "id", "source_id", "locator", "created_at", "updated_at", "updated_by", "channel", "account",
        "external_id", "last_inbound_at", "last_outbound_at",
    ),
    "unchecked_text": (
        "entity", "ref_id", "source_id", "created_at", "created_by", "updated_at", "updated_by",
    ),
    "write_log": (
        "change_id", "agent", "intent", "status", "decided_at", "reviewed_by", "reviewed_at", "payload_sha256",
        "targets_json", "approved_via",
    ),
}


def _disposition() -> dict[str, str]:
    out = {f"{table}.{column}": "kept" for table, columns in _KEPT_COLUMNS.items() for column in columns}
    for key in _TOKEN_COLUMNS:
        out[key] = "token"
    for key in _SPAN_COLUMNS:
        out[key] = "spans"
    return out


DISPOSITION: dict[str, str] = _disposition()

# Ops with their own rule in scrub_packet, so the inventory does not compute their fields. set_source_author
# carries only ids (the source and the person), never a source's words, so a redaction leaves it as it was.
_OWN_RULE = frozenset({"add_source", "apply_candidate", "record_collector_run", "redact_source", "rebuild_search",
                       "set_source_author"})


def _packet_fields() -> dict[str, tuple[tuple[str, ...], tuple[str, ...]]]:
    """op -> (fields replaced whole, fields whose quoted spans are scrubbed), computed from DISPOSITION and
    the ops' row schemas."""
    out: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
    for name, spec in {**schemas.OPS, **schemas.COMMAND_OPS}.items():
        tokens: list[str] = []
        spans: list[str] = []
        row = spec.get("row")
        properties = row.get("properties", {}) if isinstance(row, dict) else {}
        if name not in _OWN_RULE:
            for field in properties:
                kind = DISPOSITION.get(f"{spec['table']}.{field}")
                if kind == "token":
                    tokens.append(field)
                elif kind == "spans":
                    spans.append(field)
            if name == "upsert_fact" and "value_raw" in properties:
                tokens.append("value_raw")
        out[name] = (tuple(sorted(tokens)), tuple(sorted(spans)))
    return out


PACKET_FIELDS = _packet_fields()


def value_token(value_type: str) -> str | None:
    if value_type in ("text", "enum"):
        return REDACTED
    if value_type == "url":
        return REDACTED_URL
    return None


def tombstone_text(date: str, operator: str, reason: str) -> str:
    return f"{TOMBSTONE_PREFIX}{date} by {operator}: {reason}"


def redaction_error(date: str) -> str:
    return f"{TOMBSTONE_PREFIX}{date}"


def is_tombstone(row) -> bool:
    """A real tombstone: the title, the prefix, and a stored hash that is not the hash of its own text
. An agent's own source titled `redacted` is hashed by the applier and never passes."""
    content = row["content"]
    return (
        row["title"] == TOMBSTONE_TITLE
        and isinstance(content, str)
        and content.startswith(TOMBSTONE_PREFIX)
        and row["content_sha256"] != hashlib.sha256(content.encode()).hexdigest()
    )


def source_key(content: str) -> str:
    return evidence_gate.match_key(content)


def squeeze(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


# -- source ids named in a text ---------------------------------------------------------------------

_SOURCE_TOKEN = re.compile(r"(?<![a-z0-9._-])src-[a-z0-9][a-z0-9._-]*")


def named_sources(text) -> set[str]:
    """The source ids a text names: each maximal run of `[a-z0-9._-]` that starts `src-` and is not preceded
    by one of those characters, and the same run with its closing full stops removed, one at a time."""
    found: set[str] = set()
    for token in _SOURCE_TOKEN.findall(text or ""):
        found.add(token)
        while token.endswith("."):
            token = token[:-1]
            found.add(token)
    return found


def names_source(text, source_id: str) -> bool:
    return source_id in named_sources(text)


# -- quoted spans -----------------------------------------------------------------------------------

def _spans(text: str, keys) -> list[str]:
    found: dict[str, None] = {}
    for sentence in evidence_gate.sentences(text):
        for span in evidence_gate.quoted_spans(sentence):
            key = evidence_gate.match_key(span)
            if key and any(evidence_gate.contains(source, key) for source in keys):
                found[span.strip()] = None
    return sorted((span for span in found if span), key=len, reverse=True)


def scrub_spans(text, keys, limit: int | None = None):
    """Replace each quoted span of `text` that one of the sources (`keys` are their match_keys) contains
    with the token. A result longer than `limit` and than the original becomes the token."""
    if not isinstance(text, str) or not text:
        return text
    keys = list(keys)
    out = text
    for span in _spans(text, keys):
        words = span.split()
        pattern = (
            "(?<=[" + re.escape(_QUOTE_OPEN) + "])(\\s*)"
            + r"\s+".join(re.escape(word) for word in words)
            + "(\\s*)(?=[" + re.escape(_QUOTE_CLOSE) + "])"
        )
        out = re.sub(pattern, lambda m: m.group(1) + REDACTED + m.group(2), out)
    if limit is not None and len(out) > limit and len(out) > len(text):
        return REDACTED
    return out


# -- packets ----------------------------------------------------------------------------------------

def op_source(op) -> str | None:
    """The one source an op cites: its envelope `source.source_id`, the id of an `add_source` row, or an
    `apply_candidate`'s `candidate.source_id`."""
    if not isinstance(op, dict):
        return None
    name = op.get("op")
    if name == "add_source":
        row = op.get("row")
        found = row.get("id") if isinstance(row, dict) else None
    elif name == "apply_candidate":
        cand = op.get("candidate")
        found = cand.get("source_id") if isinstance(cand, dict) else None
    else:
        source = op.get("source")
        found = source.get("source_id") if isinstance(source, dict) else None
    return found if isinstance(found, str) else None


def op_row(op):
    """(table, id) of the row an upsert op writes, or None."""
    if not isinstance(op, dict):
        return None
    spec = schemas.OPS.get(op.get("op"))
    row = op.get("row")
    if spec is None or spec["kind"] != "upsert" or not isinstance(row, dict):
        return None
    return (spec["table"], str(row.get(spec["key"])))


def _scrub_written(op: dict, keys: list) -> dict:
    """An op that cites another source but wrote a row the redaction changed: spans with every chain key,
    and the quote when a chain key contains it. Nothing else changes."""
    out = dict(op)
    _tokens, spans = PACKET_FIELDS.get(op["op"], ((), ()))
    row = dict(op["row"])
    for field in spans:
        if isinstance(row.get(field), str):
            row[field] = scrub_spans(row[field], keys)
    quote = row.get("quote")
    if isinstance(quote, str) and quote != REDACTED:
        qkey = evidence_gate.match_key(quote)
        if qkey and any(evidence_gate.contains(k, qkey) for k in keys):
            row["quote"] = REDACTED
    out["row"] = row
    return out


def _scrub_op(op: dict, key, types: dict[str, str], whole: bool) -> dict:
    out = dict(op)
    name = op["op"]
    if name == "add_source":
        row = dict(op["row"])
        row["content"], row["title"] = REDACTED, TOMBSTONE_TITLE
        if "uri" in row:
            row["uri"] = ""
        out["row"] = row
        return out
    if name == "apply_candidate":
        cand = dict(op["candidate"])
        if "quote" in cand:
            cand["quote"] = REDACTED
        fields = dict(cand.get("fields") or {})
        if "value_raw" in fields:
            fields["value_raw"] = REDACTED
        for field in ("title", "detail", "next_step"):
            if isinstance(fields.get(field), str):
                if whole:
                    fields[field] = REDACTED
                elif key is not None:
                    fields[field] = scrub_spans(fields[field], [key])
        cand["fields"] = fields
        out["candidate"] = cand
        return out
    tokens, spans = PACKET_FIELDS.get(name, ((), ()))
    row = dict(op.get("row") or {})
    for field in tokens:
        if field in row:
            row[field] = REDACTED
    for field in spans:
        if isinstance(row.get(field), str):
            if whole:
                row[field] = REDACTED
            elif key is not None:
                row[field] = scrub_spans(row[field], [key])
    if name == "upsert_fact" and isinstance(row.get("value"), str):
        known = types.get(row.get("key"))
        token = value_token(known or "")
        if token is None and whole and known is None:
            # A refused packet may name a key the store does not have: its type is unknown, so the
            # text goes (the value never became state).
            token = REDACTED
        if token is not None:
            row["value"] = token
    out["row"] = row
    return out


def scrub_packet(packet, keys: dict[str, str], types: dict[str, str], whole: bool = False, date: str = "",
                 cited=None, rows=()):
    """The packet with every op that cites a chain source scrubbed, and the indexes of the ops it changed
. `cited` is every id of the chain; `keys` maps the ids whose text is still there to their
    match_key; `types` maps a fact key to its value type; `rows` is the set of (table, id) of rows the
    redaction changed in its note tables."""
    ops = packet.get("ops") if isinstance(packet, dict) else None
    if not isinstance(ops, list):
        return packet, []
    cited = set(keys) if cited is None else set(cited)
    every_key = list(keys.values())
    touched: list[int] = []
    new_ops = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            new_ops.append(op)
            continue
        sid = op_source(op)
        if sid in cited:
            changed = _scrub_op(op, keys.get(sid), types, whole)
        elif op.get("op") == "record_collector_run" and isinstance(op.get("row"), dict) and any(
            names_source(str(op["row"].get("error", "")), other) for other in cited
        ):
            changed = dict(op)
            changed["row"] = {**op["row"], "error": redaction_error(date)}
        elif rows and op_row(op) in rows:
            changed = _scrub_written(op, every_key)
        else:
            changed = op
        new_ops.append(changed)
        if changed != op:
            touched.append(index)
    if not touched:
        return packet, []
    result = dict(packet)
    result["ops"] = new_ops
    existing = packet.get("redacted_ops")
    result["redacted_ops"] = sorted(set(touched) | set(existing if isinstance(existing, list) else ()))
    return result, touched


def packet_problems(packet, source_ids, whole: bool = False) -> list[str]:
    """The check form of scrub_packet: the ops that cite one of `source_ids` and still hold a non-token value
    in a token field, and, when `whole` (the packet did not apply), in a span field. The span fields of an
    applied packet cannot be checked once the source text is gone."""
    problems: list[str] = []
    ops = packet.get("ops") if isinstance(packet, dict) else None
    if not isinstance(ops, list):
        return problems
    for index, op in enumerate(ops):
        if not isinstance(op, dict) or op_source(op) not in source_ids:
            continue
        name = op.get("op")
        if name == "add_source":
            row = op.get("row") or {}
            if row.get("content") != REDACTED or row.get("title") != TOMBSTONE_TITLE or row.get("uri", "") != "":
                problems.append(f"op {index}")
            continue
        if name == "apply_candidate":
            cand = op.get("candidate") or {}
            fields = cand.get("fields") or {}
            bad = cand.get("quote", REDACTED) != REDACTED or fields.get("value_raw", REDACTED) != REDACTED
            if whole:
                bad = bad or any(
                    fields.get(f, REDACTED) != REDACTED for f in ("title", "detail", "next_step") if isinstance(fields.get(f), str)
                )
            if bad:
                problems.append(f"op {index}")
            continue
        tokens, spans = PACKET_FIELDS.get(name, ((), ()))
        row = op.get("row") or {}
        required = tokens + (spans if whole else ())
        if any(field in row and row[field] != REDACTED for field in required):
            problems.append(f"op {index}")
    return problems
