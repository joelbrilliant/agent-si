"""The capture collector: exported text becomes sources.

A document that changes becomes a new version of its source, one that disappears gets a removal version
(only from a complete export), and a mass change is refused. No I/O: brain-fetch writes the exports.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

from brain.collectors import privacy
from brain.collectors.base import (
    CollectorType,
    DocumentError,
    Group,
    LiveAdapter,
    Option,
    Plan,
    RunContext,
    document_schema,
    ensure_not_future,
    hash_parts,
    local_date,
    one_line,
    plan_note,
)
from brain.store import canonical_json

FORMAT = "agent-brain.capture.v1"
# Ids and content hashes are stored for ever. A change to normalise, the header or scrub_text that alters
# the content of an existing item needs a new number here and a plan for the sources already stored.
CONTENT_FORMAT = 1
KINDS = ("document", "meeting_transcript", "user_note", "email", "slack")
# The conversation kinds whose text and header fields are scrubbed of addresses and phones.
SCRUBBED_KINDS = ("email", "slack")
SERVICES = ("documents", "gmail", "slack", "github")
ORIGIN = r"^[a-z][a-z0-9-]{1,39}$"
MAX_CHARS = 2_000_000
MAX_OP_BYTES = 3_670_016  # 3.5 MiB
NOTE_BYTES = "truncated at 3.5 MiB"
NOTE_CHARS = "truncated at 2,000,000 characters"
ERROR_CLASSES = ("", "auth", "network", "folder", "limit", "config", "other")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANKS = re.compile(r"\n{4,}")
_REF_CONTROLS = re.compile(r"[\x00-\x1f\x7f]")
_HEAD = 801
_REMOVED = re.compile(r"kind: [a-z_]+\nref: [^\n]+\nremoved: [0-9]{4}-[0-9]{2}-[0-9]{2}")

LIVE = LiveAdapter("capture", (), (), "Read access to one local folder, through brain-fetch")

_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ref", "kind", "title", "text"],
    "properties": {
        "ref": one_line(3, 500),
        "kind": {"type": "string", "enum": list(KINDS)},
        "title": one_line(1, 200),
        "text": {"type": "string", "min_length": 0, "max_length": 16_000_000},
        "date": {"type": "string", "format": "date"},
        "from": one_line(0, 120),
        "from_principal": {"type": "boolean"},
        "to": {"type": "array", "min_items": 0, "max_items": 20, "items": one_line(1, 120)},
        "uri": one_line(0, 2000),
        "attachments": {"type": "array", "min_items": 0, "max_items": 20, "items": one_line(1, 200)},
    },
}

SCHEMA = document_schema(
    FORMAT,
    {
        "origin": {"type": "string", "pattern": ORIGIN},
        "items": {"type": "array", "min_items": 0, "max_items": 5000, "items": _ITEM},
        "present": {"type": "array", "min_items": 0, "max_items": 20000, "items": one_line(3, 500)},
        "error": one_line(0, 300),
        "error_class": {"type": "string", "enum": list(ERROR_CLASSES)},
        "mark": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        "incremental": {"type": "boolean"},
    },
    ("origin", "items"),
)


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")  # not one-line: a document of many lines
    text = _CONTROLS.sub("", text)
    text = "\n".join(line.rstrip(" \t") for line in text.split("\n"))
    text = _BLANKS.sub("\n\n\n", text)
    return text.strip("\n")


def scrub_text(text: str, kind: str) -> str:
    """The address and phone patterns, between normalising and hashing, for conversations
    only: documents and transcripts keep their text, so their stored hashes stay valid."""
    return privacy.scrub(text) if kind in SCRUBBED_KINDS else text


def scrub_fields(item: dict) -> dict:
    """The header fields of a conversation item, scrubbed and held to their schema lengths."""
    if item["kind"] not in SCRUBBED_KINDS:
        return item
    item = dict(item)
    item["title"] = privacy.scrub(item["title"])[:200]
    if item.get("from"):
        item["from"] = privacy.scrub(item["from"])[:120]
    if item.get("to"):
        item["to"] = [privacy.scrub(name)[:120] for name in item["to"]]
    if item.get("attachments"):
        item["attachments"] = [privacy.scrub(name)[:200] for name in item["attachments"]]
    return item


def header(item: dict, note: str = "") -> str:
    kind = item["kind"]
    lines = [f"kind: {kind}"]
    if item.get("from"):
        lines.append(f"from: {item['from']}")
    if item.get("to"):
        lines.append("to: " + ", ".join(item["to"]))
    if kind != "document":
        if item.get("date"):
            lines.append(f"date: {item['date']}")
        lines.append(f"subject: {item['title']}")
    if item.get("attachments"):
        lines.append("attachments: " + ", ".join(item["attachments"]))
    lines.append(f"ref: {item['ref']}")
    if note:
        lines.append(note)
    return "\n".join(lines)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _op_bytes(row: dict) -> int:
    return len(canonical_json({"op": "add_source", "row": row}).encode())


def _row(item: dict, row_id: str, content: str, source_date: str, author, supersedes) -> dict:
    row = {
        "id": row_id,
        "kind": item["kind"],
        "title": item["title"],
        "uri": item.get("uri", ""),
        "source_date": source_date,
        "author_person_id": author,
        "content": content,
        "external_ref": item["ref"],
    }
    if supersedes is not None:
        row["supersedes_id"] = supersedes
    return row


def _content_for(item: dict, text: str, note: str) -> str:
    return header(item, note) + "\n\n" + text


def _sized(item, content, trial_id, source_date, author, supersedes) -> bool:
    return _op_bytes(_row(item, trial_id, content, source_date, author, supersedes)) <= MAX_OP_BYTES


def build_content(item: dict, trial_id: str, source_date: str, author=None, supersedes=None) -> tuple[str, bool]:
    """The stored content of an item, and whether it was cut to the size limits. trial_id,
    author and supersedes stand in for the op's own so its size is counted as it will be sent."""
    text = scrub_text(normalise(item["text"]), item["kind"])
    whole = _content_for(item, text, "")
    if len(whole) <= MAX_CHARS and _sized(item, whole, trial_id, source_date, author, supersedes):
        return whole, False
    # Two limits bind: 2,000,000 characters in the content and 3.5 MiB for the op as sent. The cut is the
    # largest character count that satisfies both, with the note that names the limit that binds.
    def bytes_fit(note: str) -> int:
        low, high = 0, min(len(text), MAX_OP_BYTES)
        while low < high:
            middle = (low + high + 1) // 2
            content = _content_for(item, text[:middle], note)
            if _sized(item, content, trial_id, source_date, author, supersedes):
                low = middle
            else:
                high = middle - 1
        return low

    k_chars = MAX_CHARS - len(header(item, NOTE_CHARS)) - 2
    if k_chars < bytes_fit(NOTE_CHARS):
        return _content_for(item, text[: max(k_chars, 0)], NOTE_CHARS), True
    k_chars = MAX_CHARS - len(header(item, NOTE_BYTES)) - 2
    cut = max(min(k_chars, bytes_fit(NOTE_BYTES)), 0)
    return _content_for(item, text[:cut], NOTE_BYTES), True


def source_id(origin: str, ref: str, sha256: str) -> str:
    return f"src-cap-{hash_parts(origin, ref)[:16]}-{sha256[:16]}"


def _fresh_id(base: str, stored: list[str]) -> str:
    if base not in stored:
        return base
    number = len(stored) + 1
    while f"{base}-r{number}" in stored:
        number += 1
    return f"{base}-r{number}"


def _removal_content(kind: str, ref: str, origin: str, day: str) -> str:
    return f"kind: {kind}\nref: {ref}\nremoved: {day}\n\nremoved from {origin} on {day}"


def _is_removal(head: str | None) -> bool:
    if head is None:
        return False
    cut = head.find("\n\n")
    return cut >= 0 and _REMOVED.fullmatch(head[:cut]) is not None


# The index sources_by_external_ref serves the range; `external_ref <> ''` is what lets SQLite use that
# partial index at all, and the unary plus keeps the planner from choosing the id index for the GLOB.
KNOWN_SQL = (
    "SELECT s.id, s.external_ref, s.title, s.kind, s.content_sha256, "
    "NOT EXISTS (SELECT 1 FROM sources AS n WHERE n.supersedes_id = s.id), "
    "CASE WHEN NOT EXISTS (SELECT 1 FROM sources AS n WHERE n.supersedes_id = s.id) "
    "THEN substr(s.content, 1, ?) END "
    "FROM sources AS s WHERE s.external_ref >= ? AND s.external_ref < ? AND s.external_ref <> '' "
    "AND +s.id GLOB 'src-cap-*'"
)


REDACTED_SQL = (
    "SELECT external_ref, content_sha256, content, title FROM sources WHERE title = 'redacted' "
    "AND external_ref >= ? AND external_ref < ? AND +id GLOB 'src-cap-*'"
)


def known(con, doc: dict, ctx: RunContext) -> dict:
    origin = ctx.options["origin"]
    current: dict[str, dict] = {}
    ids: dict[str, list[str]] = {}
    for kind in dict.fromkeys(ctx.options["kinds"]):
        rows = con.execute(KNOWN_SQL, (_HEAD, f"{kind}:{origin}/", f"{kind}:{origin}0"))
        for row_id, ref, title, row_kind, sha, is_current, head in rows:
            ids.setdefault(ref, []).append(row_id)
            if is_current:
                current[ref] = {
                    "id": row_id,
                    "title": title,
                    "sha": sha,
                    "removed": _is_removal(head),
                    "kind": row_kind,
                }
    redacted: dict[str, set[str]] = {}
    for kind in dict.fromkeys(ctx.options["kinds"]):
        for ref, sha, content, title in con.execute(REDACTED_SQL, (f"{kind}:{origin}/", f"{kind}:{origin}0")):
            if title == "redacted" and content.startswith("redacted on ") and sha != _sha(content):
                redacted.setdefault(ref, set()).add(sha)
    return {"current": current, "ids": ids, "redacted": redacted}


def _under(ref: str, origin: str, kinds) -> bool:
    return any(ref.startswith(f"{kind}:{origin}/") and len(ref) > len(f"{kind}:{origin}/") for kind in kinds)


def _check_item(item: dict, origin: str, kinds, seen: set[str]) -> None:
    ref, kind = item["ref"], item["kind"]
    if kind not in kinds:
        raise DocumentError(f"{ref}: kind {kind} is not collected here")
    prefix = f"{kind}:{origin}/"
    if not ref.startswith(prefix) or len(ref) == len(prefix) or _REF_CONTROLS.search(ref):
        raise DocumentError(f"{ref}: ref must start {prefix}")
    if any(segment in {"", ".", ".."} for segment in ref[len(prefix):].split("/")):
        raise DocumentError(f"{ref}: ref has an unsafe path")
    if ref in seen:
        raise DocumentError(f"duplicate ref {ref}")
    seen.add(ref)


def _guard(moved: int, live: int, ctx: RunContext) -> None:
    if ctx.allow_mass_change:
        return
    if moved > 20 or (moved >= 5 and moved * 4 > live):
        raise DocumentError(
            f"mass change: {moved} documents would change; rerun with --allow-mass-change if this is expected"
        )


def plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    ensure_not_future(doc["fetched_at"], ctx.now, "fetched_at is in the future")
    origin, kinds = ctx.options["origin"], tuple(ctx.options["kinds"])
    if doc["origin"] != origin:
        raise DocumentError(f"document origin {doc['origin']} doesn't match collector origin {origin}")
    if doc.get("error"):
        raise DocumentError(f"{doc.get('error_class') or 'other'}: {doc['error']}")
    current = known_rows.get("current") or {}
    stored_ids = known_rows.get("ids") or {}
    items = sorted((scrub_fields(item) for item in doc["items"]), key=lambda item: item["ref"])
    seen: set[str] = set()
    for item in items:
        _check_item(item, origin, kinds, seen)
    present = sorted(set(doc.get("present") or ()))
    for ref in present:
        if ref in seen:
            raise DocumentError(f"{ref} is both an item and listed as unchanged")
        if not _under(ref, origin, kinds):
            raise DocumentError(f"{ref}: ref is not under this collector's origin and kinds")
        version = current.get(ref)
        if (version is None or version["removed"]) and ref in (known_rows.get("redacted") or {}):
            continue
        if version is None or version["removed"]:
            raise DocumentError(
                f"{ref} is listed as unchanged but the store has no version of it; rerun with --rescan"
            )
    day = local_date(doc["fetched_at"], ctx.timezone)
    allowed = ctx.marked and bool(ctx.principal_id) and ctx.author_principal
    groups: list[Group] = []
    moved = truncated = left_out = 0
    ignored = False
    for item in items:
        ref = item["ref"]
        source_date = item.get("date") or day
        claims_principal = item.get("from_principal") is True
        if claims_principal and not allowed:
            ignored = True
        author = ctx.principal_id if claims_principal and allowed else None
        version = current.get(ref)
        supersedes = None if version is None else version["id"]
        stored = stored_ids.get(ref, [])
        trial = source_id(origin, ref, "0" * 64)
        content, cut = build_content(item, trial, source_date, author, supersedes)
        sha = _sha(content)
        if cut:
            truncated += 1
        if version is not None and not version["removed"] and version["sha"] == sha:
            continue
        if sha in (known_rows.get("redacted") or {}).get(ref, ()):
            left_out += 1
            continue
        row_id = _fresh_id(source_id(origin, ref, sha), stored)
        if len(row_id) != len(trial):
            # A returning text gets an -r<k> suffix: size the op again with an id of that length.
            content, cut = build_content(item, row_id, source_date, author, supersedes)
            sha = _sha(content)
            row_id = _fresh_id(source_id(origin, ref, sha), stored)
        if version is not None and not version["removed"]:
            moved += 1
        groups.append(Group(ops=({"op": "add_source", "row": _row(item, row_id, content, source_date, author, supersedes)},)))
    removals: list[tuple[str, dict]] = []
    if doc["complete"] and not doc.get("incremental"):
        # An incremental export lists only what is new, so it never removes anything.
        listed = seen | set(present)
        for ref in sorted(current):
            version = current[ref]
            if ref in listed or version["removed"] or not _under(ref, origin, kinds):
                continue
            removals.append((ref, version))
        moved += len(removals)
        for ref, version in removals:
            content = _removal_content(version["kind"], ref, origin, day)
            row_id = _fresh_id(source_id(origin, ref, _sha(content)), stored_ids.get(ref, []))
            row = {
                "id": row_id,
                "kind": version["kind"],
                "title": version["title"],
                "uri": "",
                "source_date": day,
                "author_person_id": None,
                "content": content,
                "external_ref": ref,
                "supersedes_id": version["id"],
            }
            groups.append(Group(ops=({"op": "add_source", "row": row},)))
    live = sum(1 for version in current.values() if not version["removed"])
    _guard(moved, live, ctx)
    notes = []
    if truncated:
        notes.append(f"{truncated} item{'' if truncated == 1 else 's'} truncated at the size limit")
    if left_out:
        notes.append(f"{left_out} item{'' if left_out == 1 else 's'} match{'es' if left_out == 1 else ''} redacted text and {'was' if left_out == 1 else 'were'} left out")
    if ignored:
        notes.append(
            "from_principal ignored: the export is not marked by brain-fetch or the agent may not author "
            "as the principal"
        )
    return Plan(tuple(groups), len(items) + len(present), plan_note(doc, *notes))


TYPE = CollectorType(
    name="capture",
    format=FORMAT,
    ops=("add_source", "record_collector_run"),
    options={
        "origin": Option("label", required=True),
        "kinds": Option("strings", default=["document"], allowed=KINDS),
        "live_folder": Option("path", default=""),
        # What brain-fetch pulls for this collector. The engine plans the export the same way whatever
        # the service; config checks the combination (config._check_service_settings).
        "service": Option("enum", default="documents", allowed=SERVICES),
        "live_query": Option("query", default=""),
        "live_channels": Option("channels", default=[]),
        "include_dms": Option("bool", default=False),
        "repo": Option("repo", default=""),
        "threads": Option("label", default=""),
    },
    document_schema=SCHEMA,
    live=LIVE,
    known=known,
    plan=plan,
    source_kind="document",
    may_supersede=True,
    principal_author=True,
)
