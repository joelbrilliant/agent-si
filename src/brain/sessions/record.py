"""The session record: sessions.sqlite3 beside the brain, holding what was said in past agent sessions, word for word.

Code files it, never a model. The readers in brain.sessions.adapters list each tool's sessions, and sync adds the
messages the record doesn't hold yet. A message is added once, under its own key, and never rewritten, so the record
keeps a session after the tool deletes its copy. forget removes one session and stops it coming back.

The record is not the brain: it has no change packets, no write log and no evidence gate, and it never opens
brain.sqlite3. A fact reaches the brain from here only as an excerpt source the applier checks (brain.sessions.cite).
Search is SQLite FTS5 over one row per message. Ported from agent-memory 0.2.1 (session_continuity.py).
"""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # Windows: SQLite's own lock is the only one
    fcntl = None

from brain.sessions import adapters
from brain.sessions.adapters import SessionDescriptor, SessionRoots
from brain.text import single_line

FILE_NAME = "sessions.sqlite3"
LOCK_NAME = ".sessions.lock"
# Stored for ever: a message's place in its session's text depends on RENDER_FORMAT. A change to render_message
# that alters a length needs a new number here and a plan for the starts already stored.
RENDER_FORMAT = 1
MAX_MESSAGE_CHARS = 32_000
MAX_EXCERPTS = 3
EXCERPT_CHARS = 600
# A page of a session is small, because an agent pays for every character a read returns: 3,000 characters where a
# record body or a source pages at 8,000. A page opened at a message spends at most LEAD_CHARS on what came just
# before it, so the message itself and what followed get the rest.
PAGE_CHARS = 3000
LEAD_CHARS = 600
NO_TIME = "no time"

# A message that looks like it holds a secret is never stored.
SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"xox[bap]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?im)(?:^|\s)(?:export\s+)?[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\s*=\s*\S{16,}"),
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  harness     TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  source_path TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  cwd         TEXT NOT NULL DEFAULT '',
  started_at  REAL NOT NULL DEFAULT 0,
  updated_at  REAL NOT NULL DEFAULT 0,
  messages    INTEGER NOT NULL DEFAULT 0,
  chars       INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (harness, session_id)
) STRICT;
CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY,
  harness     TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  n           INTEGER NOT NULL,
  start       INTEGER NOT NULL,
  message_key TEXT NOT NULL,
  role        TEXT NOT NULL,
  content     TEXT NOT NULL,
  timestamp   REAL NOT NULL DEFAULT 0,
  UNIQUE (harness, session_id, message_key),
  UNIQUE (harness, session_id, n)
) STRICT;
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content, content='messages', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
CREATE INDEX IF NOT EXISTS messages_by_time ON messages(timestamp);
CREATE INDEX IF NOT EXISTS sessions_by_update ON sessions(updated_at DESC);
CREATE TABLE IF NOT EXISTS forgotten (
  harness      TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  forgotten_at TEXT NOT NULL,
  PRIMARY KEY (harness, session_id)
) STRICT;
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
) STRICT;
"""


@dataclass(frozen=True)
class Excerpt:
    n: int
    role: str
    timestamp: float
    text: str
    start: int = 0


@dataclass(frozen=True)
class Hit:
    harness: str
    session_id: str
    title: str
    cwd: str
    started_at: float
    updated_at: float
    score: float
    excerpts: tuple[Excerpt, ...]

    @property
    def id(self) -> str:
        return session_ref(self.harness, self.session_id)


@dataclass(frozen=True)
class SyncResult:
    sessions: dict[str, int] = field(default_factory=dict)
    new_sessions: int = 0
    new_messages: int = 0
    skipped_secret: int = 0
    seconds: float = 0.0
    errors: tuple[dict, ...] = ()
    busy: bool = False  # another process was syncing, so this call changed nothing


def path_for(instance_dir) -> Path:
    return Path(instance_dir) / FILE_NAME


def session_ref(harness: str, session_id: str) -> str:
    """A session's id as tools show it: the tool's name, a colon and the tool's own session id."""
    return f"{harness}:{session_id}"


def parse_ref(ref: str) -> tuple[str, str] | None:
    harness, colon, session_id = str(ref or "").partition(":")
    if not colon or harness not in adapters.HARNESSES or not session_id:
        return None
    return harness, session_id


_PLACE = re.compile(r"\A(.+)#([1-9][0-9]{0,6})\Z", re.DOTALL)


def place_ref(harness: str, session_id: str, n: int) -> str:
    """A message's place as search shows it: the session's id, a hash and the message's number. get opens the
    session there."""
    return f"{session_ref(harness, session_id)}#{int(n)}"


def parse_place(ref: str) -> tuple[str, str, int | None] | None:
    """A session id with or without a message number on the end: (tool, session id, number or None)."""
    text = str(ref or "")
    placed = _PLACE.match(text)
    parsed = parse_ref(placed.group(1) if placed else text)
    if parsed is None:
        return None
    return parsed[0], parsed[1], int(placed.group(2)) if placed else None


# -- the file --------------------------------------------------------------------------------------------------


def _private(path: Path) -> None:
    """Owner-only access for the record and its WAL files: it holds private conversations."""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        with contextlib.suppress(OSError):
            if candidate.exists():
                candidate.chmod(0o600)


def connect(db: Path) -> sqlite3.Connection:
    """The record opened for writing, created owner-only with its tables when it is missing."""
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    os.close(os.open(db, os.O_CREAT | os.O_RDWR, 0o600))
    con = sqlite3.connect(db, timeout=5.0)
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(_SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('render_format', ?)", (str(RENDER_FORMAT),))
    con.commit()
    _private(db)
    return con


def connect_readonly(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"{Path(db).resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
    con.execute("PRAGMA busy_timeout = 5000")
    con.execute("PRAGMA query_only = ON")
    return con


@contextlib.contextmanager
def _sync_lock(db: Path):
    """True while this process holds the sync lock, False when another process has it."""
    if fcntl is None:
        yield True
        return
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    handle = open(Path(db).parent / LOCK_NAME, "a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()


# -- text ------------------------------------------------------------------------------------------------------


def has_secret(content: str) -> bool:
    return any(pattern.search(content) for pattern in SECRET_PATTERNS)


def clock(timestamp: float, zone: str) -> str:
    """A stored time as a local clock time in the brain's time zone, or NO_TIME when the tool recorded none."""
    if not timestamp or timestamp <= 0:
        return NO_TIME
    try:
        return datetime.fromtimestamp(float(timestamp), ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return NO_TIME


def local_day(timestamp: float, zone: str) -> str:
    stamp = clock(timestamp, zone)
    return "" if stamp == NO_TIME else stamp[:10]


def rendered_length(n: int, role: str, timestamp: float, content: str) -> int:
    """How many characters render_message gives. The time is 16 characters or NO_TIME in every zone, so the length,
    and with it every message's start, doesn't depend on the zone."""
    stamp_chars = 16 if timestamp and timestamp > 0 else len(NO_TIME)
    return len(f"[{n}] {role} ") + stamp_chars + 1 + len(content) + 2


def render_message(n: int, role: str, timestamp: float, content: str, zone: str) -> str:
    """One message as a session's text shows it: a header line with its number, who spoke and when, then the
    words, then a blank line."""
    stamp = clock(timestamp, zone)
    if stamp == NO_TIME and timestamp and timestamp > 0:
        stamp = "0000-00-00 00:00"  # a time no calendar holds still takes its 16 characters
    return f"[{n}] {role} {stamp}\n{content}\n\n"


def day_bounds(since: str | None, until: str | None, zone: str) -> tuple[float | None, float | None]:
    """Local YYYY-MM-DD days as (the first second of since, the first second after until), both days included.
    ValueError for anything that isn't such a date."""
    tz = ZoneInfo(zone)

    def start(value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=tz)

    first = start(since).timestamp() if since else None
    after = (start(until) + timedelta(days=1)).timestamp() if until else None
    return first, after


def fts_terms(query: str) -> list[str]:
    terms: list[str] = []
    for word in re.findall(r"[a-z0-9][a-z0-9_-]{1,}", str(query or "").lower()):
        if word not in terms:
            terms.append(word)
    return terms[:20]


# -- sync ------------------------------------------------------------------------------------------------------


def _append(con: sqlite3.Connection, descriptor: SessionDescriptor, raw: list, stamp: str) -> tuple[int, int, bool]:
    """Add the messages of one session the record doesn't hold yet. Returns (added, skipped as secrets, new)."""
    harness, session_id = descriptor.harness, descriptor.session_id
    row = con.execute(
        "SELECT messages, chars FROM sessions WHERE harness = ? AND session_id = ?", (harness, session_id)
    ).fetchone()
    count, chars = (row[0], row[1]) if row else (0, 0)
    held = {
        key for (key,) in con.execute(
            "SELECT message_key FROM messages WHERE harness = ? AND session_id = ?", (harness, session_id)
        )
    }
    added = skipped = 0
    newest = 0.0
    for message in raw:
        if message.message_key in held:
            continue
        content = message.content.replace("\x00", "").strip()
        if not content:
            continue
        if has_secret(content):
            skipped += 1
            continue
        content = content[:MAX_MESSAGE_CHARS]
        count += 1
        con.execute(
            "INSERT INTO messages (harness, session_id, n, start, message_key, role, content, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (harness, session_id, count, chars, message.message_key, message.role, content, message.timestamp),
        )
        chars += rendered_length(count, message.role, message.timestamp, content)
        held.add(message.message_key)
        newest = max(newest, message.timestamp)
        added += 1
    updated_at = max(descriptor.updated_at, newest)
    con.execute(
        "INSERT INTO sessions (harness, session_id, source_path, fingerprint, title, cwd, started_at, updated_at, "
        "messages, chars) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (harness, session_id) DO UPDATE SET source_path = excluded.source_path, "
        "fingerprint = excluded.fingerprint, title = excluded.title, cwd = excluded.cwd, "
        "started_at = CASE WHEN sessions.started_at > 0 THEN sessions.started_at ELSE excluded.started_at END, "
        "updated_at = max(sessions.updated_at, excluded.updated_at), messages = excluded.messages, "
        "chars = excluded.chars",
        (harness, session_id, descriptor.source_path, stamp, descriptor.title, descriptor.cwd,
         descriptor.started_at, updated_at, count, chars),
    )
    return added, skipped, row is None


def sync(db: Path, roots: SessionRoots) -> SyncResult:
    """Add what the tools' session stores hold and the record doesn't. Sessions whose store hasn't changed since the
    last sync are skipped by a fingerprint, so a sync with nothing new only lists the folders. When a tool's reader
    has come to return more than it did, that tool's sessions are read again once, changed or not, and the messages
    the record lacks are added after the ones it holds. Nothing is ever removed or renumbered here: a session the
    tool has deleted stays in the record."""
    began = time.monotonic()
    db = Path(db)
    descriptors, errors = adapters.discover_sources(roots, _known(db))
    newest: dict[tuple[str, str], SessionDescriptor] = {}
    for descriptor in descriptors:
        key = (descriptor.harness, descriptor.session_id)
        if key not in newest or descriptor.updated_at >= newest[key].updated_at:
            newest[key] = descriptor  # one id in two files: the file written last holds the fullest copy
    counts: dict[str, int] = {}
    for harness, _ in newest:
        counts[harness] = counts.get(harness, 0) + 1
    if not newest and not db.is_file():
        return SyncResult(errors=tuple(errors), seconds=time.monotonic() - began)
    with _sync_lock(db) as held:
        if not held:
            return SyncResult(sessions=dict(sorted(counts.items())), busy=True, errors=tuple(errors),
                              seconds=time.monotonic() - began)
        con = connect(db)
        try:
            known = {(row[0], row[1]): row[2] for row in con.execute(
                "SELECT harness, session_id, fingerprint FROM sessions")}
            forgotten = {(row[0], row[1]) for row in con.execute("SELECT harness, session_id FROM forgotten")}
            behind = _readers_behind(con)
            new_sessions = new_messages = skipped_secret = 0
            con.execute("BEGIN IMMEDIATE")
            try:
                for key in sorted(newest):
                    descriptor = newest[key]
                    stamp = f"{RENDER_FORMAT}:{descriptor.fingerprint}"
                    if key in forgotten or (known.get(key) == stamp and descriptor.harness not in behind):
                        continue
                    try:
                        raw = adapters.load_messages(descriptor)
                    except (OSError, sqlite3.Error) as exc:
                        # One unreadable session is listed and left as it is; nothing already recorded is touched.
                        errors.append({"harness": descriptor.harness, "profile": "default",
                                       "path": descriptor.source_path, "error": type(exc).__name__})
                        continue
                    added, skipped, new = _append(con, descriptor, raw, stamp)
                    new_messages += added
                    skipped_secret += skipped
                    new_sessions += 1 if new and added else 0
                elapsed = time.monotonic() - began
                # A tool with a store or a session that couldn't be read keeps its old number, so the next sync
                # reads its sessions again.
                unread = {error["harness"] for error in errors}
                read_again = [(_READER_KEY + harness, str(version)) for harness, version in sorted(behind.items())
                              if harness not in unread]
                for name, value in (("last_sync_at", _utc_now()), ("last_sync_seconds", f"{elapsed:.2f}"),
                                    *read_again):
                    con.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                                "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (name, value))
                con.commit()
            except BaseException:
                con.rollback()
                raise
        finally:
            con.close()
            _private(db)
    return SyncResult(
        sessions=dict(sorted(counts.items())),
        new_sessions=new_sessions,
        new_messages=new_messages,
        skipped_secret=skipped_secret,
        seconds=time.monotonic() - began,
        errors=tuple(errors),
    )


def _known(db: Path) -> dict[str, SessionDescriptor]:
    """What the record holds for each transcript file, so discovery needn't open a file that hasn't changed. Hermes
    keeps every session in one database, so its sessions are never looked up by path."""
    if not db.is_file():
        return {}
    marker = f"{RENDER_FORMAT}:"
    try:
        con = connect_readonly(db)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            "SELECT harness, session_id, source_path, fingerprint, title, cwd, started_at, updated_at FROM sessions "
            "WHERE harness <> ? AND fingerprint LIKE ?", ("hermes", marker + "%")
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {
        row[2]: SessionDescriptor(harness=row[0], session_id=row[1], source_path=row[2],
                                  fingerprint=row[3][len(marker):], title=row[4], cwd=row[5],
                                  started_at=float(row[6] or 0), updated_at=float(row[7] or 0))
        for row in rows
    }


_READER_KEY = "reader:"


def _readers_behind(con: sqlite3.Connection) -> dict[str, int]:
    """The tools whose reader now returns more than it did when the record last read their sessions, each with its
    reader's number now. The record keeps the number it last read with in meta; an engine from before readers had
    numbers leaves that row alone, so two engine versions on one record don't undo each other."""
    held = dict(con.execute("SELECT key, value FROM meta WHERE key LIKE ?", (_READER_KEY + "%",)))
    behind = {}
    for harness, version in adapters.READER_VERSIONS.items():
        try:
            read_with = int(held.get(_READER_KEY + harness, 1))
        except ValueError:
            read_with = 1
        if read_with < version:
            behind[harness] = version
    return behind


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- search ----------------------------------------------------------------------------------------------------


def _not_session(exclude: tuple[str, str] | None) -> tuple[str, list]:
    """Leave out the session that is asking. A Hermes profile's session is stored as <profile>:<id>, so the id alone
    matches too."""
    if not exclude:
        return "", []
    harness, session_id = exclude
    return (
        " AND NOT (m.harness = ? AND (m.session_id = ? OR substr(m.session_id, instr(m.session_id, ':') + 1) = ?))",
        [harness, session_id, session_id],
    )


def _window(since: float | None, before: float | None) -> tuple[str, list]:
    sql, params = "", []
    if since is not None or before is not None:
        sql += " AND m.timestamp > 0"  # a message with no time can't be placed, so a date filter leaves it out
    if since is not None:
        sql += " AND m.timestamp >= ?"
        params.append(since)
    if before is not None:
        sql += " AND m.timestamp < ?"
        params.append(before)
    return sql, params


def _match_rows(con, expression: str, fetch: int, exclude, since, before) -> list:
    not_sql, not_params = _not_session(exclude)
    window_sql, window_params = _window(since, before)
    return con.execute(
        "SELECT m.harness, m.session_id, s.title, s.cwd, s.started_at, s.updated_at, m.role, m.timestamp, "
        "bm25(messages_fts) AS score, snippet(messages_fts, 0, '', '', ' ... ', 40), m.n, m.start "
        "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
        "JOIN sessions s ON s.harness = m.harness AND s.session_id = m.session_id "
        "WHERE messages_fts MATCH ?" + not_sql + window_sql +
        " ORDER BY bm25(messages_fts), m.timestamp DESC LIMIT ?",
        (expression, *not_params, *window_params, fetch),
    ).fetchall()


def search(
    db: Path,
    query: str,
    *,
    limit: int = 5,
    excerpts: int = MAX_EXCERPTS,
    excerpt_chars: int = EXCERPT_CHARS,
    exclude: tuple[str, str] | None = None,
    since: float | None = None,
    before: float | None = None,
    all_terms_first: bool = False,
) -> list[Hit]:
    """Sessions holding the query's words, best first, each with its best messages as excerpts. Every word counts
    and bm25 puts messages with more and rarer words first. all_terms_first keeps messages holding every word and
    widens only when none do. No sync here: the caller decides when the record is refreshed."""
    db = Path(db)
    terms = fts_terms(query)
    if not terms or not db.is_file():
        return []
    quoted = ['"%s"' % term.replace('"', '""') for term in terms]
    fetch = max(limit * 12, 24)
    con = connect_readonly(db)
    try:
        rows = _match_rows(con, " ".join(quoted), fetch, exclude, since, before) if all_terms_first else []
        if not rows and (not all_terms_first or len(terms) > 1):
            rows = _match_rows(con, " OR ".join(quoted), fetch, exclude, since, before)
        return _grouped(rows, limit, excerpts, excerpt_chars)
    finally:
        con.close()


def _grouped(rows: list, limit: int, excerpts: int, excerpt_chars: int = EXCERPT_CHARS) -> list[Hit]:
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        group = groups.setdefault((row[0], row[1]), {"meta": row[:6], "score": row[8], "excerpts": []})
        group["score"] = min(group["score"], row[8])
        if len(group["excerpts"]) < excerpts:
            group["excerpts"].append(
                Excerpt(n=row[10], role=row[6], timestamp=float(row[7] or 0),
                        text=single_line(row[9], max_chars=excerpt_chars, ellipsis=" ..."), start=row[11])
            )
    hits = [
        Hit(harness=g["meta"][0], session_id=g["meta"][1], title=g["meta"][2], cwd=g["meta"][3],
            started_at=float(g["meta"][4] or 0), updated_at=float(g["meta"][5] or 0), score=float(g["score"]),
            excerpts=tuple(g["excerpts"]))
        for g in groups.values()
    ]
    hits.sort(key=lambda hit: (round(hit.score, 2), -hit.updated_at))
    # A tool can keep one conversation under two session ids after a resume or a fork. Both stay in the record,
    # but a result doesn't spend two places on the same words.
    distinct, seen = [], set()
    for hit in hits:
        same = tuple((excerpt.role, excerpt.text.lower()) for excerpt in hit.excerpts)
        if same and same in seen:
            continue
        seen.add(same)
        distinct.append(hit)
    return distinct[: max(1, limit)]


def period(db: Path, since: float | None, before: float | None, *, limit: int = 5,
           exclude: tuple[str, str] | None = None, excerpt_chars: int = EXCERPT_CHARS) -> list[Hit]:
    """The sessions with a message inside the times, newest first, each with its first message there as the
    excerpt. For a dated question with no words to match, such as what was worked on yesterday."""
    db = Path(db)
    if not db.is_file() or (since is None and before is None):
        return []
    not_sql, not_params = _not_session(exclude)
    window_sql, window_params = _window(since, before)
    con = connect_readonly(db)
    try:
        rows = con.execute(
            "SELECT m.harness, m.session_id, s.title, s.cwd, s.started_at, MAX(m.timestamp), MIN(m.n) "
            "FROM messages m JOIN sessions s ON s.harness = m.harness AND s.session_id = m.session_id "
            "WHERE 1 = 1" + not_sql + window_sql +
            " GROUP BY m.harness, m.session_id ORDER BY MAX(m.timestamp) DESC, m.harness, m.session_id LIMIT ?",
            (*not_params, *window_params, max(1, limit)),
        ).fetchall()
        hits = []
        for harness, session_id, title, cwd, started_at, last_at, first_n in rows:
            role, text, at, start = con.execute(
                "SELECT role, content, timestamp, start FROM messages WHERE harness = ? AND session_id = ? AND n = ?",
                (harness, session_id, first_n),
            ).fetchone()
            hits.append(
                Hit(harness=harness, session_id=session_id, title=title, cwd=cwd, started_at=float(started_at or 0),
                    updated_at=float(last_at or 0), score=0.0,
                    excerpts=(Excerpt(n=first_n, role=role, timestamp=float(at or 0),
                                      text=single_line(text, max_chars=excerpt_chars, ellipsis=" ..."), start=start),))
            )
        return hits
    finally:
        con.close()


# -- read ------------------------------------------------------------------------------------------------------


def session_row(con: sqlite3.Connection, harness: str, session_id: str) -> dict | None:
    row = con.execute(
        "SELECT harness, session_id, title, cwd, started_at, updated_at, messages, chars, source_path "
        "FROM sessions WHERE harness = ? AND session_id = ?",
        (harness, session_id),
    ).fetchone()
    if row is None or not row[6]:
        return None
    names = ("harness", "session_id", "title", "cwd", "started_at", "updated_at", "messages", "chars", "source_path")
    return dict(zip(names, row, strict=True))


class NoSuchMessage(LookupError):
    """The session is in the record and has no message with that number."""

    def __init__(self, n: int, messages: int) -> None:
        super().__init__(f"no message {n}")
        self.n = n
        self.messages = messages


def message_start(con: sqlite3.Connection, harness: str, session_id: str, n: int) -> int | None:
    """Where to open a session to read message n: at the message, or at the earliest message before it that
    starts within LEAD_CHARS of it, so a short question comes with its answer and a long one doesn't use the
    page up. None when the session has no message n."""
    row = con.execute(
        "SELECT start FROM messages WHERE harness = ? AND session_id = ? AND n = ?", (harness, session_id, n)
    ).fetchone()
    if row is None:
        return None
    at = int(row[0])
    earlier = con.execute(
        "SELECT MIN(start) FROM messages WHERE harness = ? AND session_id = ? AND n < ? AND start >= ?",
        (harness, session_id, n, at - LEAD_CHARS),
    ).fetchone()
    return int(earlier[0]) if earlier and earlier[0] is not None else at


def read(db: Path, harness: str, session_id: str, zone: str, *, offset: int = 0, at: int | None = None,
         max_chars: int = PAGE_CHARS) -> dict | None:
    """One page of a session's text: each message under a header line with its number, who spoke and when. The
    page starts at a character offset, or with at (a message number) and no offset, where message_start puts that
    message. None when the record doesn't hold the session, and NoSuchMessage when it holds no such message."""
    db = Path(db)
    if not db.is_file():
        return None
    offset = max(0, int(offset))
    con = connect_readonly(db)
    try:
        session = session_row(con, harness, session_id)
        if session is None:
            return None
        if at is not None and not offset:
            opened = message_start(con, harness, session_id, int(at))
            if opened is None:
                raise NoSuchMessage(int(at), int(session["messages"]))
            offset = opened
        first = con.execute(
            "SELECT n FROM messages WHERE harness = ? AND session_id = ? AND start <= ? ORDER BY start DESC LIMIT 1",
            (harness, session_id, offset),
        ).fetchone()
        parts: list[str] = []
        gathered = 0
        skip = 0
        numbers: list[int] = []
        if first is not None:
            for n, start, role, timestamp, content in con.execute(
                "SELECT n, start, role, timestamp, content FROM messages WHERE harness = ? AND session_id = ? "
                "AND n >= ? ORDER BY n",
                (harness, session_id, first[0]),
            ):
                text = render_message(n, role, timestamp, content, zone)
                if not parts:
                    skip = offset - start
                parts.append(text)
                numbers.append(n)
                gathered += len(text)
                if gathered - skip >= max_chars:
                    break
        text = "".join(parts)[skip : skip + max_chars]
    finally:
        con.close()
    end = offset + len(text)
    return {
        **session,
        "text": text,
        "offset": offset,
        "returned": len(text),
        "next_offset": end if end < session["chars"] else None,
        "first_n": numbers[0] if numbers and text else None,
        "last_n": numbers[-1] if numbers and text else None,
    }


def messages_between(db: Path, harness: str, session_id: str, first: int, last: int) -> list[tuple]:
    """Messages first to last of a session as (n, role, timestamp, content), for an excerpt."""
    db = Path(db)
    if not db.is_file():
        return []
    con = connect_readonly(db)
    try:
        return con.execute(
            "SELECT n, role, timestamp, content FROM messages WHERE harness = ? AND session_id = ? "
            "AND n BETWEEN ? AND ? ORDER BY n",
            (harness, session_id, first, last),
        ).fetchall()
    finally:
        con.close()


# -- forget and status -----------------------------------------------------------------------------------------


def forget(db: Path, harness: str, session_id: str) -> int:
    """Remove one session's messages from the record and keep it out of later syncs. Returns how many messages
    went. The session is listed as forgotten whether or not the record held it, so one that is still in the tool's
    own store doesn't come back."""
    db = Path(db)
    con = connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        removed = con.execute(
            "DELETE FROM messages WHERE harness = ? AND session_id = ?", (harness, session_id)
        ).rowcount
        con.execute("DELETE FROM sessions WHERE harness = ? AND session_id = ?", (harness, session_id))
        con.execute(
            "INSERT OR REPLACE INTO forgotten (harness, session_id, forgotten_at) VALUES (?, ?, ?)",
            (harness, session_id, _utc_now()),
        )
        con.commit()
        return max(0, removed)
    finally:
        con.close()
        _private(db)


def status(db: Path) -> dict:
    """What the record holds: sessions and messages per tool, the last sync and the file's size. No content."""
    db = Path(db)
    if not db.is_file():
        return {"exists": False, "sessions": {}, "messages": 0, "forgotten": 0, "bytes": 0,
                "last_sync_at": None, "oldest": None, "newest": None}
    con = connect_readonly(db)
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "sessions" not in tables:
            return {"exists": False, "sessions": {}, "messages": 0, "forgotten": 0, "bytes": db.stat().st_size,
                    "last_sync_at": None, "oldest": None, "newest": None}
        sessions = dict(con.execute(
            "SELECT harness, count(*) FROM sessions WHERE messages > 0 GROUP BY harness ORDER BY harness"))
        messages = con.execute("SELECT count(*) FROM messages").fetchone()[0]
        forgotten = con.execute("SELECT count(*) FROM forgotten").fetchone()[0]
        meta = dict(con.execute("SELECT key, value FROM meta"))
        oldest, newest = con.execute(
            "SELECT min(timestamp), max(timestamp) FROM messages WHERE timestamp > 0").fetchone()
    finally:
        con.close()
    return {"exists": True, "sessions": sessions, "messages": messages, "forgotten": forgotten,
            "bytes": db.stat().st_size, "last_sync_at": meta.get("last_sync_at"), "oldest": oldest, "newest": newest}
