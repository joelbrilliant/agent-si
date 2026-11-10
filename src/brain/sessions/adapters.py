"""Read-only readers for the session stores agent tools keep on this machine: Claude Code, Codex, Grok and Hermes.

Each reader lists a tool's sessions (discover) and returns one session's dialogue (load): the user's and the
assistant's text, in order, without tool calls, tool output or the context a tool writes for itself. Nothing here
writes, and no model is involved. Ported from agent-memory 0.2.1 (session_adapters.py).
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from brain import ingress
from brain.text import single_line

HARNESSES = ("claude", "codex", "grok", "hermes")
# Hermes' own roles for what a person and the assistant said. Bound as parameters, never written into its SQL.
DIALOGUE_ROLES = ("user", "assistant")
# One line of a transcript, or a small summary file. A longer line is skipped, as an unreadable one is.
MAX_LINE_BYTES = ingress.MAX_DOCUMENT_BYTES
TITLE_CHARS = 120

# The surfaces a person talks to Hermes through. A cron run or a delegated task is not a conversation.
HUMAN_HERMES_SOURCES = (
    "acp", "api_server", "cli", "desktop", "discord", "signal", "slack", "teams", "telegram", "tui", "webhook",
    "weixin", "whatsapp",
)
CODEX_BOOTSTRAP_PREFIXES = (
    "# AGENTS.md instructions",
    "<app-context>",
    "<apps_instructions>",
    "<collaboration_mode>",
    "<environment_context>",
    "<permissions instructions>",
    "<plugins_instructions>",
    "<recommended_plugins>",
    "<skills_instructions>",
)
GENERATED_MESSAGE_PREFIXES = (
    "[CONTEXT COMPACTION",
    "[Context compaction",
    "[System note:",
    "[Your active task list was preserved across context compression]",
    "[IMPORTANT: Background process",
    "You've reached the maximum number of tool-calling iterations allowed.",
    "Review the conversation above and update the skill library",
    "Review the conversation above and consider saving to memory",
    "Review the conversation above and update two things:",
)
GENERATED_USER_ENVELOPE_PREFIX = "<user_info"
ACTIVE_TASK_LIST_MARKER = "[Your active task list was preserved across context compression]"
# "User" turns a tool writes for itself, which no person typed. On the machine these were measured on
# (10 November 2026) they were about a third of Claude Code's and Codex's user turns.
HARNESS_USER_PREFIXES = (
    "<task-notification>",  # Claude Code: a background task finished
    "<side-sessions-event>",  # Claude Code: another session's status
    "<ci-monitor-event",  # Claude Code: a watched pull request changed
    "Warning: The maximum number of unified exec processes",  # Codex: too many open processes
    "The following is the Codex agent history whose request action you are assessing",  # Codex: an approval review
)
# Claude Code puts its reminders in the same turn as what the person typed, ahead of it.
_LEADING_REMINDERS = re.compile(r"\A(?:\s*<system-reminder>.*?</system-reminder>)+\s*", re.DOTALL)
# A reader's number goes up when it starts returning messages it used to leave out. The record then reads that
# tool's sessions again once, changed or not, and adds what it lacks (record.sync). A reader not named is at 1.
READER_VERSIONS = {"claude": 2}  # 2: messages typed while the assistant was working


@dataclass(frozen=True)
class SessionRoots:
    """Where each tool keeps its sessions. hermes_profiles are extra Hermes homes, as (name, state.db) pairs."""

    claude_root: Path
    codex_root: Path
    grok_root: Path
    hermes_db: Path
    hermes_profiles: tuple[tuple[str, Path], ...] = ()

    @classmethod
    def default(cls, hermes_profiles: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None):
        """The tools' own default folders under the home folder. An environment variable moves one of them."""
        env = os.environ if env is None else env
        home = Path.home()
        hermes_home = Path(env.get("HERMES_HOME") or home / ".hermes")
        return cls(
            claude_root=Path(env.get("CLAUDE_PROJECTS_ROOT") or home / ".claude" / "projects"),
            codex_root=Path(env.get("CODEX_SESSIONS_ROOT") or home / ".codex" / "sessions"),
            grok_root=Path(env.get("GROK_SESSIONS_ROOT") or home / ".grok" / "sessions"),
            hermes_db=Path(env.get("HERMES_SESSION_DB") or hermes_home / "state.db"),
            hermes_profiles=tuple(
                (name, Path(path).expanduser()) for name, path in sorted((hermes_profiles or {}).items())
            ),
        )


@dataclass(frozen=True)
class SessionDescriptor:
    harness: str
    session_id: str
    source_path: str
    fingerprint: str
    title: str
    cwd: str
    started_at: float
    updated_at: float
    native_session_id: str = ""


# A transcript's path and the descriptor already held for it.
Known = Mapping[str, SessionDescriptor] | None


@dataclass(frozen=True)
class RawMessage:
    message_key: str
    role: str
    content: str
    timestamp: float


def parse_timestamp(value) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def file_fingerprint(path: Path, extra_path: Path | None = None) -> str:
    parts = []
    for candidate in (path, extra_path):
        if candidate is None:
            continue
        try:
            info = candidate.stat()
        except OSError:
            parts.append("missing")
            continue
        parts.append(f"{info.st_mtime_ns}:{info.st_size}")
    return "|".join(parts)


def read_json_lines(path: Path, needs: tuple[bytes, ...] = ()) -> Iterable[tuple[int, dict]]:
    """Each line of a transcript that is a JSON object, with its line number. A line that isn't is skipped. With
    needs, so is a line that lacks one of those byte strings, before it is parsed: most of a transcript's bytes are
    tool output, and a row can't have a type its line never spells."""
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            if len(line) > MAX_LINE_BYTES or any(need not in line for need in needs):
                continue
            try:
                row = ingress.loads(line, what="session line")
            except ingress.IngressError:
                continue
            if isinstance(row, dict):
                yield line_number, row


def text_parts(content, allowed_types: set[str]) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    parts = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in allowed_types:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text)
    return parts


def clean_message_content(role: str, content) -> str:
    """The message without what a tool wrote for itself. '' when nothing a person or the assistant said is left."""
    if not isinstance(content, str):
        return ""
    cleaned = content.strip()
    if not cleaned or cleaned.startswith(GENERATED_MESSAGE_PREFIXES):
        return ""
    if role == "user" and cleaned.lower().startswith(GENERATED_USER_ENVELOPE_PREFIX):
        return ""
    if role == "user":
        cleaned = _LEADING_REMINDERS.sub("", cleaned)
        if not cleaned or cleaned.startswith(HARNESS_USER_PREFIXES) or cleaned.startswith("<system-reminder>"):
            return ""  # a reminder left unclosed is the tool's own text to its end
        marker_at = cleaned.find(ACTIVE_TASK_LIST_MARKER)
        if marker_at >= 0 and (marker_at == 0 or cleaned[marker_at - 1] == "\n"):  # not one-line: finds a line start
            cleaned = cleaned[:marker_at].rstrip()
    return cleaned


def _joined(parts: list[str]) -> str:
    return "\n".join(part for part in parts if part.strip()).strip()  # not one-line: a message keeps its lines


# -- Hermes ----------------------------------------------------------------------------------------------------


def discover_hermes(path: Path, known: Known = None) -> list[SessionDescriptor]:
    """Hermes sessions a person took part in. Older state.db files carry the surface (cli, telegram and so on) in
    sessions.source. Current Hermes writes source 'local' and the surface in a created_source column, so when that
    column exists the query also takes source = 'local' rows whose created_source is a human surface."""
    if not path.is_file():
        raise FileNotFoundError(path)
    marks = ",".join("?" for _ in HUMAN_HERMES_SOURCES)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(sessions)")}
        if "created_source" in columns:
            surface = "COALESCE(NULLIF(s.created_source, ''), s.source)"
            where = f"(s.source IN ({marks}) OR (s.source = 'local' AND s.created_source IN ({marks})))"
            params = HUMAN_HERMES_SOURCES + HUMAN_HERMES_SOURCES
        else:
            surface = "s.source"
            where = f"s.source IN ({marks})"
            params = HUMAN_HERMES_SOURCES
        rows = con.execute(
            f"SELECT s.id, {surface}, COALESCE(s.title, ''), COALESCE(s.cwd, ''), s.started_at, "
            "COALESCE(MAX(m.timestamp), s.ended_at, s.started_at), COALESCE(MAX(m.id), 0), COUNT(m.id) "
            "FROM sessions s JOIN messages m ON m.session_id = s.id "
            f"WHERE {where} AND m.role IN (?, ?) GROUP BY s.id",
            (*params, *DIALOGUE_ROLES),
        ).fetchall()
    finally:
        con.close()
    return [
        SessionDescriptor(
            harness="hermes",
            session_id=str(session_id),
            source_path=str(path),
            fingerprint=f"{max_message_id}:{message_count}",
            title=title or f"{source} session",
            cwd=cwd or "",
            started_at=float(started_at or 0),
            updated_at=float(updated_at or 0),
        )
        for session_id, source, title, cwd, started_at, updated_at, max_message_id, message_count in rows
    ]


def load_hermes(descriptor: SessionDescriptor) -> list[RawMessage]:
    con = sqlite3.connect(f"file:{descriptor.source_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT id, role, content, timestamp FROM messages WHERE session_id = ? "
            "AND role IN (?, ?) AND content IS NOT NULL ORDER BY timestamp, id",
            (descriptor.native_session_id or descriptor.session_id, *DIALOGUE_ROLES),
        ).fetchall()
    finally:
        con.close()
    messages = []
    for message_id, role, content, timestamp in rows:
        content = clean_message_content(role, content)
        if content:
            messages.append(RawMessage(str(message_id), role, content, float(timestamp or 0)))
    return messages


# -- Claude Code -----------------------------------------------------------------------------------------------


def _claude_dialogue(row: dict) -> bool:
    return row.get("type") in ("user", "assistant") and not (
        row.get("isSidechain") is True or row.get("isCompactSummary") is True or row.get("isMeta") is True
    )


def _claude_typed_mid_turn(row: dict):
    """What a person typed while the assistant was still working, or None. Claude Code hands such a message to the
    model part way through the turn and writes it as an attachment row, never as a user row. The same kind of row
    carries finished tasks and other sessions' messages, so only one Claude Code marks as a person's is taken. A
    row that names no origin (older Claude Code versions, and some of the tool's own events since) is left out."""
    attachment = row.get("attachment")
    if row.get("type") != "attachment" or row.get("isSidechain") is True or not isinstance(attachment, dict):
        return None
    if attachment.get("type") != "queued_command" or attachment.get("commandMode") != "prompt":
        return None
    if attachment.get("isMeta") is True:
        return None
    origin = attachment.get("origin")
    if (isinstance(origin, dict) and origin.get("kind") == "human") or attachment.get("humanTurn") is True:
        return attachment.get("prompt")
    return None


def _unchanged(known: Known, path: Path, fingerprint: str) -> SessionDescriptor | None:
    """The descriptor the record already holds for this file, when the file hasn't changed since."""
    prior = (known or {}).get(str(path))
    if prior is None or prior.fingerprint != fingerprint:
        return None
    return replace(prior, updated_at=max(prior.updated_at, path.stat().st_mtime))


def discover_claude(root: Path, known: Known = None) -> list[SessionDescriptor]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    descriptors = []
    for path in sorted(root.rglob("*.jsonl")):
        if "subagents" in path.parts:
            continue
        same = _unchanged(known, path, file_fingerprint(path))
        if same is not None:
            descriptors.append(same)
            continue
        found = None
        for _, row in read_json_lines(path):
            if _claude_dialogue(row):
                found = row
                break
        if found is None:
            continue
        title = ""
        message = found.get("message")
        if found.get("type") == "user" and isinstance(message, dict) and isinstance(message.get("content"), str):
            title = single_line(message["content"], max_chars=TITLE_CHARS)
        descriptors.append(
            SessionDescriptor(
                harness="claude",
                session_id=str(found.get("sessionId") or path.stem),
                source_path=str(path),
                fingerprint=file_fingerprint(path),
                title=title or path.stem,
                cwd=str(found.get("cwd") or ""),
                started_at=parse_timestamp(found.get("timestamp")),
                updated_at=path.stat().st_mtime,
            )
        )
    return descriptors


def load_claude(descriptor: SessionDescriptor) -> list[RawMessage]:
    messages = []
    for line_number, row in read_json_lines(Path(descriptor.source_path)):
        if _claude_dialogue(row):
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role") or row.get("type")
            if role not in ("user", "assistant"):
                continue
            said = message.get("content")
        else:
            role, said = "user", _claude_typed_mid_turn(row)
            if said is None:
                continue
        content = clean_message_content(role, _joined(text_parts(said, {"text"})))
        if content:
            messages.append(
                RawMessage(str(row.get("uuid") or line_number), role, content, parse_timestamp(row.get("timestamp")))
            )
    return messages


# -- Codex -----------------------------------------------------------------------------------------------------


def discover_codex(root: Path, known: Known = None) -> list[SessionDescriptor]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    descriptors = []
    for path in sorted(root.rglob("*.jsonl")):
        same = _unchanged(known, path, file_fingerprint(path))
        if same is not None:
            descriptors.append(same)
            continue
        meta = None
        for _, row in read_json_lines(path):
            if row.get("type") == "session_meta":
                if isinstance(row.get("payload"), dict):
                    meta = row["payload"]
                break
        if not meta:
            continue
        descriptors.append(
            SessionDescriptor(
                harness="codex",
                session_id=str(meta.get("id") or meta.get("session_id") or path.stem),
                source_path=str(path),
                fingerprint=file_fingerprint(path),
                title=path.stem,
                cwd=str(meta.get("cwd") or ""),
                started_at=parse_timestamp(meta.get("timestamp")),
                updated_at=path.stat().st_mtime,
            )
        )
    return descriptors


def load_codex(descriptor: SessionDescriptor) -> list[RawMessage]:
    messages = []
    for line_number, row in read_json_lines(Path(descriptor.source_path), (b'"response_item"', b'"message"')):
        payload = row.get("payload")
        if row.get("type") != "response_item" or not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant") or (role == "assistant" and payload.get("phase") == "commentary"):
            continue
        parts = text_parts(payload.get("content"), {"input_text"} if role == "user" else {"output_text"})
        if role == "user":
            parts = [part for part in parts if not part.lstrip().startswith(CODEX_BOOTSTRAP_PREFIXES)]
        content = clean_message_content(role, _joined(parts))
        if content:
            messages.append(
                RawMessage(str(payload.get("id") or line_number), role, content, parse_timestamp(row.get("timestamp")))
            )
    return messages


# -- Grok ------------------------------------------------------------------------------------------------------


def discover_grok(root: Path, known: Known = None) -> list[SessionDescriptor]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    descriptors = []
    for path in sorted(root.rglob("chat_history.jsonl")):
        summary_path = path.parent / "summary.json"
        same = _unchanged(known, path, file_fingerprint(path, summary_path))
        if same is not None:
            descriptors.append(same)
            continue
        summary: dict = {}
        try:
            loaded = ingress.load_file(summary_path, limit=MAX_LINE_BYTES, what="session summary")
            if isinstance(loaded, dict):
                summary = loaded
        except (OSError, ingress.IngressError):
            pass
        session_id = path.parent.name
        updated_at = parse_timestamp(summary.get("last_active_at")) or parse_timestamp(summary.get("updated_at"))
        descriptors.append(
            SessionDescriptor(
                harness="grok",
                session_id=session_id,
                source_path=str(path),
                fingerprint=file_fingerprint(path, summary_path),
                title=single_line(summary.get("generated_title") or session_id, max_chars=TITLE_CHARS),
                cwd=str(summary.get("git_root_dir") or "").rstrip("/"),
                started_at=parse_timestamp(summary.get("created_at")),
                updated_at=updated_at or path.stat().st_mtime,
            )
        )
    return descriptors


def load_grok(descriptor: SessionDescriptor) -> list[RawMessage]:
    messages = []
    for line_number, row in read_json_lines(Path(descriptor.source_path)):
        role = row.get("type")
        if role not in ("user", "assistant") or (role == "user" and row.get("synthetic_reason")):
            continue
        content = clean_message_content(role, _joined(text_parts(row.get("content"), {"text"})))
        if content:
            # Grok stamps no message, so each takes its session's start plus its line number, which keeps the order.
            messages.append(
                RawMessage(str(line_number), role, content, descriptor.started_at + line_number / 1000.0)
            )
    return messages


# -- all four --------------------------------------------------------------------------------------------------

_DISCOVER = {"claude": discover_claude, "codex": discover_codex, "grok": discover_grok, "hermes": discover_hermes}
_LOAD = {"claude": load_claude, "codex": load_codex, "grok": load_grok, "hermes": load_hermes}


def discover_sources(roots: SessionRoots, known: Known = None) -> tuple[list[SessionDescriptor], list[dict]]:
    """Every session found, and one entry per folder or file that couldn't be read. A tool that isn't installed has
    no folder: that is listed too, and the caller decides whether it matters. known maps a transcript's path to the
    descriptor the record already holds, so a file that hasn't changed is listed without being opened."""
    descriptors: list[SessionDescriptor] = []
    errors: list[dict] = []
    sources = [
        ("claude", roots.claude_root, ""),
        ("codex", roots.codex_root, ""),
        ("grok", roots.grok_root, ""),
        ("hermes", roots.hermes_db, ""),
        *(("hermes", path, profile) for profile, path in roots.hermes_profiles),
    ]
    for harness, root, profile in sources:
        try:
            rows = _DISCOVER[harness](Path(root), known)
        except (OSError, sqlite3.Error) as exc:
            errors.append({"harness": harness, "profile": profile or "default", "path": str(root),
                           "error": type(exc).__name__})
            continue
        if profile:
            rows = [replace(row, session_id=f"{profile}:{row.session_id}", native_session_id=row.session_id)
                    for row in rows]
        descriptors.extend(rows)
    return descriptors, errors


def load_messages(descriptor: SessionDescriptor) -> list[RawMessage]:
    return _LOAD[descriptor.harness](descriptor)
