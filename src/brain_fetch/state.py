"""Cursors, the inbox and the run lock."""
from __future__ import annotations

import contextlib
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only
    fcntl = None

from brain import ingress, store
from brain.applier import Unavailable

CURSOR_LIMIT = 16 * 1024 * 1024
KEEP_DAYS = 14
_EXPORT_NAME = re.compile(r"([0-9]{8}T[0-9]{6}Z)-([0-9]{2})\.json")


class InboxFull(Exception):
    pass


class Busy(Unavailable):
    def __init__(self, name: str):
        super().__init__(f"collector {name} is already running; try again when it finishes")
        self.collector = name


def fetch_dir(instance) -> Path:
    path = Path(instance) / "fetch"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def inbox_dir(instance, collector: str) -> Path:
    parent = fetch_dir(instance) / "inbox"
    parent.mkdir(mode=0o700, exist_ok=True)
    path = parent / collector
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def cursor_path(instance, collector: str) -> Path:
    return Path(instance) / "fetch" / f"{collector}.state.json"


def read_cursor(instance, collector: str) -> dict:
    """The path-to-entry map of the last good run; empty when the cursor is missing, unreadable, over 16 MiB
    or not this collector's."""
    path = cursor_path(instance, collector)
    try:
        data = ingress.load_file(path, limit=CURSOR_LIMIT, what="cursor")
    except (OSError, ingress.IngressError):
        return {}
    if not isinstance(data, dict) or data.get("format") != 1 or data.get("collector") != collector:
        return {}
    files = data.get("files")
    if not isinstance(files, dict):
        return {}
    # An entry that doesn't have the shape this code writes is dropped, so the next run reads that file again.
    return {path: entry for path, entry in files.items() if isinstance(path, str) and _entry_ok(entry)}


def _entry_ok(entry) -> bool:
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("sha256"), str)
        and isinstance(entry.get("reader"), str)
        and all(type(entry.get(key)) is int for key in ("size", "mtime_ns", "ctime_ns"))
        and type(entry.get("stored", False)) is bool
    )


def _atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def write_cursor(instance, collector: str, files: dict) -> None:
    fetch_dir(instance)
    body = {"format": 1, "collector": collector, "updated_at": store.utc_now(), "files": files}
    _atomic_write(cursor_path(instance, collector), store.canonical_json(body))


def export_path(inbox: Path, stamp: str) -> Path:
    """The first free <stamp>-NN.json, NN from 01; more than 99 in one second is refused."""
    for number in range(1, 100):
        path = inbox / f"{stamp}-{number:02d}.json"
        if not path.exists():
            return path
    raise InboxFull("inbox is full for this second")


def write_export(path: Path, doc: dict) -> None:
    _atomic_write(path, store.canonical_json(doc))


def prune_inbox(inbox: Path) -> int:
    """Delete exports whose name stamp is more than 14 days before store.utc_now(). Other names stay."""
    now = datetime.strptime(store.utc_now(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    removed = 0
    for path in sorted(inbox.iterdir()):
        match = _EXPORT_NAME.fullmatch(path.name)
        if match is None or not path.is_file() or path.is_symlink():
            continue
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if (now - stamp).total_seconds() > KEEP_DAYS * 86400:
            path.unlink()
            removed += 1
    return removed


@contextlib.contextmanager
def run_lock(instance, collector: str):
    """fetch/.lock-<collector>, held for one run. This is separate from collect's own lock."""
    if fcntl is None:
        raise Unavailable("brain-fetch needs fcntl file locks, which this platform doesn't have")
    handle = os.fdopen(os.open(fetch_dir(instance) / f".lock-{collector}", os.O_RDWR | os.O_CREAT, 0o600), "a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Busy(collector) from exc
        yield
    finally:
        handle.close()


def transcripts_allowed(instance) -> bool:
    """True only when the store holds redaction_gate (migration 0006)."""
    db = Path(instance) / "brain.sqlite3"
    if not db.is_file():
        return False
    con = store.connect_readonly(db)
    try:
        return con.execute("PRAGMA user_version").fetchone()[0] >= 6 and con.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'redaction_gate'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        con.close()


def read_state(instance, collector: str, service: str) -> dict:
    """A network service's cursor: the data of the last good run, or empty when the file is
    missing, unreadable, over 16 MiB, or another collector's, service's or format's."""
    path = cursor_path(instance, collector)
    try:
        data = ingress.load_file(path, limit=CURSOR_LIMIT, what="cursor")
    except (OSError, ingress.IngressError):
        return {}
    if not isinstance(data, dict) or data.get("format") != 2 or data.get("collector") != collector:
        return {}
    if data.get("service") != service or not isinstance(data.get("data"), dict):
        return {}
    return data["data"]


def write_state(instance, collector: str, service: str, data: dict) -> None:
    fetch_dir(instance)
    body = {"format": 2, "collector": collector, "service": service, "updated_at": store.utc_now(), "data": data}
    _atomic_write(cursor_path(instance, collector), store.canonical_json(body))
