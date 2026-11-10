"""A session as evidence: an excerpt of the record that the brain stores as a source.

brain sessions excerpt prints a ready add_source op holding messages N to M of a session, word for word, with
external_ref `session:<session id>#N-M`. The applier checks every such source against the record before it stores
it, so its content is what was said and nothing else. From then on it is an ordinary source: the evidence gate
finds a real quote in it and refuses a made-up one, and it stays if the session is later forgotten.

It has no author. A user turn can be a pasted email, a system notice or another agent's message, so the record
alone can't say the principal wrote it.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from brain import advice, store
from brain.sessions import record
from brain.text import single_line

PREFIX = "session:"
KIND = "document"
MAX_MESSAGES = 50
MAX_CHARS = 2_000_000  # the most a source's content holds
_REF = re.compile(r"session:(?P<session>[a-z]+:.+)#(?P<first>[1-9][0-9]{0,8})-(?P<last>[1-9][0-9]{0,8})")


class CiteError(Exception):
    pass


def is_excerpt(row: dict) -> bool:
    return str(row.get("external_ref") or "").startswith(PREFIX)


def external_ref(session: str, first: int, last: int) -> str:
    return f"{PREFIX}{session}#{first}-{last}"


def source_id(session: str, first: int, last: int) -> str:
    return f"src-session-{hashlib.sha256(session.encode()).hexdigest()[:12]}-{first}-{last}"


def _rows(instance_dir: Path, session: str, first: int, last: int) -> tuple[dict, list[tuple]]:
    """The session and its messages first to last, or a CiteError that names the fix."""
    ref = record.parse_ref(session)
    if ref is None:
        raise CiteError(advice.session_ref_error(session))
    if first > last or last - first + 1 > MAX_MESSAGES:
        raise CiteError(advice.session_range_error(first, last, MAX_MESSAGES))
    db = record.path_for(instance_dir)
    found = None
    if db.is_file():
        con = record.connect_readonly(db)
        try:
            found = record.session_row(con, *ref)
        finally:
            con.close()
    if found is None:
        raise CiteError(advice.session_missing_error(session))
    if last > found["messages"]:
        raise CiteError(advice.session_range_error(first, last, MAX_MESSAGES, held=found["messages"]))
    return found, record.messages_between(db, *ref, first, last)


def render(rows: list[tuple], zone: str) -> str:
    return "".join(record.render_message(n, role, timestamp, content, zone) for n, role, timestamp, content in rows)


def excerpt_op(instance_dir, config, session: str, first: int, last: int, source: str | None = None) -> dict:
    """The add_source op for messages first to last of a session, as the applier will accept it."""
    if not config.sessions.record:
        raise CiteError(advice.sessions_off_error())
    found, rows = _rows(Path(instance_dir), session, first, last)
    zone = config.timezone
    content = render(rows, zone)
    if len(content) > MAX_CHARS:
        raise CiteError(advice.session_range_error(first, last, MAX_MESSAGES, too_long=True))
    day = next((record.local_day(timestamp, zone) for _n, _role, timestamp, _text in rows
                if record.local_day(timestamp, zone)), "")
    day = day or record.local_day(found["updated_at"], zone) or store.local_today(zone)
    title = single_line(f"Session {found['harness']} {day}: {found['title']}", max_chars=200, ellipsis="...")
    return {
        "op": "add_source",
        "row": {
            "id": source or source_id(session, first, last),
            "kind": KIND,
            "title": title,
            "source_date": day,
            "content": content,
            "external_ref": external_ref(session, first, last),
        },
    }


def check(instance_dir, config, row: dict) -> str | None:
    """Why the applier refuses this session excerpt, or None when it is exactly what the record holds."""
    ref = str(row.get("external_ref") or "")
    match = _REF.fullmatch(ref)
    if match is None:
        return advice.session_excerpt_ref_error(ref)
    session, first, last = match["session"], int(match["first"]), int(match["last"])
    hint = advice.session_excerpt_hint(session, first, last)
    if not config.sessions.record:
        return advice.sessions_off_error()
    if row.get("kind") != KIND or row.get("author_person_id") is not None or row.get("supersedes_id") is not None:
        return advice.session_excerpt_shape_error(KIND, hint)
    try:
        _found, rows = _rows(Path(instance_dir), session, first, last)
    except CiteError as exc:
        return str(exc)
    if row.get("content") != render(rows, config.timezone):
        return advice.session_excerpt_content_error(session, first, last, hint)
    return None
