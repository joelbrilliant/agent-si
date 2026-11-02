"""Hub tokens and the audit table, in <instance>/hub.sqlite3, written only by the hub.

A token is abt_<12-character public id>_<43-character secret>, shown once and stored as a SHA-256 only. It names one
agent from brain.toml, a scope (read, or read write) and a label. Every request checks that it exists, isn't revoked
or expired, and that its agent still exists with the same kind.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain import advice, config as config_mod, store
from brain.text import single_line

LIFETIME = timedelta(days=365)
AUDIT_DAYS = 400
IP_DAYS = 90
ACTIONS = ("apply", "extract", "approve", "refuse", "refused_at_edge", "auth_failed", "token_issued", "token_revoked",
           "backup", "restore", "update", "redact")
_TOKEN = re.compile(r"abt_([0-9a-f]{12})_([A-Za-z0-9_-]{43})")
SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL,
  scope TEXT NOT NULL CHECK (scope IN ('read', 'read write')), label TEXT NOT NULL, issued_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, revoked_at TEXT, folders TEXT) STRICT;
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, at TEXT NOT NULL, token_id TEXT, agent TEXT,
  action TEXT NOT NULL, change_id TEXT, body_sha256 TEXT, http_status INTEGER, exit_code INTEGER,
  detail TEXT NOT NULL DEFAULT '', client_ip TEXT) STRICT;
"""


class TokenError(Exception):
    pass


@dataclass(frozen=True)
class Token:
    id: str
    agent: str
    kind: str
    scope: str
    label: str
    expires_at: str
    # None for a token that reads the whole brain, else the folder ids it is limited to (1.12.0).
    folders: tuple[str, ...] | None = None

    @property
    def writes(self) -> bool:
        return self.scope == "read write"


def connect(instance) -> sqlite3.Connection:
    con = sqlite3.connect(str(Path(instance) / "hub.sqlite3"), isolation_level=None, timeout=10.0)
    con.executescript(SCHEMA)
    # A hub made before 1.12.0 has no folders column; its tokens stay whole-brain (NULL).
    if "folders" not in {row[1] for row in con.execute("PRAGMA table_info(tokens)")}:
        try:
            con.execute("ALTER TABLE tokens ADD COLUMN folders TEXT")
        except sqlite3.OperationalError:
            if "folders" not in {row[1] for row in con.execute("PRAGMA table_info(tokens)")}:  # another request added it first
                raise
    return con


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.strptime(store.utc_now(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _folders_of(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(json.loads(raw))  # trusted: tokens.folders, written by issue


def issue(instance, agent: str, scope: str, label: str, folders=None) -> str:
    """A new token, returned once. The reserved agent brain never gets one. With folders (ids that exist), it reads only
    those folders and what is filed in them, and it can't write."""
    cfg = config_mod.load_config(Path(instance) / "brain.toml")
    if agent == "brain":
        raise TokenError("the reserved agent brain never gets a token")
    if agent not in cfg.agents:
        raise TokenError(f"no agent {agent} in brain.toml")
    if scope not in ("read", "read write"):
        raise TokenError('scope must be "read" or "read write"')
    label = single_line(label, max_chars=120)
    if not label:
        raise TokenError("a token needs a label, such as \"Alex Example laptop, Claude Code\"")
    stored = None
    if folders:
        if scope != "read":
            raise TokenError(advice.folder_write_error())
        folders = list(dict.fromkeys(folders))
        con = store.connect_readonly(Path(instance) / "brain.sqlite3")
        try:
            for folder in folders:
                if con.execute("SELECT 1 FROM projects WHERE id = ?", (folder,)).fetchone() is None:
                    raise TokenError(advice.folder_missing_error(folder))
        finally:
            con.close()
        stored = json.dumps(folders)
    public = secrets.token_hex(6)
    secret = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    token = f"abt_{public}_{secret}"
    con = connect(instance)
    try:
        con.execute("INSERT INTO tokens (id, sha256, agent, kind, scope, label, issued_at, expires_at, folders) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (public, _sha(token), agent, cfg.agents[agent].kind, scope, label, store.utc_now(),
                     _stamp(_now() + LIFETIME), stored))
    finally:
        con.close()
    audit(instance, "token_issued", token_id="shell", agent=agent,
          detail=f"scope {scope}" + (f"; folders {', '.join(folders)}" if stored else ""))
    return token


def revoke(instance, token_id: str) -> bool:
    con = connect(instance)
    try:
        done = con.execute("UPDATE tokens SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (store.utc_now(), token_id))
    finally:
        con.close()
    if done.rowcount:
        audit(instance, "token_revoked", token_id="shell", detail=f"token {token_id}")
    return bool(done.rowcount)


def listing(instance) -> list[dict]:
    con = connect(instance)
    try:
        rows = con.execute("SELECT id, agent, scope, label, issued_at, expires_at, revoked_at, folders FROM tokens "
                           "ORDER BY issued_at, id")
        keys = ("id", "agent", "scope", "label", "issued_at", "expires_at", "revoked_at")
        return [{**dict(zip(keys, row)), "folders": None if row[7] is None else list(_folders_of(row[7]))} for row in rows]
    finally:
        con.close()


def check(instance, header: str | None, cfg) -> Token | None:
    """The token an Authorization header carries, or None. Bearer only; a query-string token is never read."""
    if not header or not header.startswith("Bearer "):
        return None
    match = _TOKEN.fullmatch(header[len("Bearer "):].strip())
    if match is None:
        return None
    token = match.group(0)
    con = connect(instance)
    try:
        row = con.execute("SELECT id, sha256, agent, kind, scope, label, expires_at, revoked_at, folders FROM tokens WHERE id = ?",
                          (match.group(1),)).fetchone()
    finally:
        con.close()
    if row is None or not hmac.compare_digest(row[1], _sha(token)) or row[7] is not None or row[6] <= store.utc_now():
        return None
    agent = cfg.agents.get(row[2])
    if agent is None or agent.kind != row[3]:
        return None
    return Token(row[0], row[2], row[3], row[4], row[5], row[6], _folders_of(row[8]))


def audit(instance, action: str, *, token_id: str | None = None, agent: str | None = None, change_id: str | None = None,
          body_sha256: str | None = None, http_status: int | None = None, exit_code: int | None = None, detail: str = "",
          client_ip: str | None = None) -> None:
    if action not in ACTIONS:
        raise ValueError(action)
    con = connect(instance)
    try:
        con.execute("INSERT INTO audit (at, token_id, agent, action, change_id, body_sha256, http_status, exit_code, detail, "
                    "client_ip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (store.utc_now(), token_id, agent, action, change_id, body_sha256, http_status, exit_code,
                     single_line(detail, max_chars=300), client_ip))
    finally:
        con.close()


def prune(instance) -> None:
    """Audit rows kept 400 days; client addresses cleared after 90."""
    now = _now()
    con = connect(instance)
    try:
        con.execute("DELETE FROM audit WHERE at < ?", (_stamp(now - timedelta(days=AUDIT_DAYS)),))
        con.execute("UPDATE audit SET client_ip = NULL WHERE at < ? AND client_ip IS NOT NULL", (_stamp(now - timedelta(days=IP_DAYS)),))
    finally:
        con.close()


def audit_rows(instance, since: str, agent: str | None = None) -> list[dict]:
    con = connect(instance)
    try:
        sql = "SELECT at, token_id, agent, action, change_id, http_status, exit_code, detail FROM audit WHERE at >= ?"
        params: list = [since]
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        rows = con.execute(sql + " ORDER BY id", params).fetchall()
    finally:
        con.close()
    keys = ("at", "token_id", "agent", "action", "change_id", "http_status", "exit_code", "detail")
    return [dict(zip(keys, row)) for row in rows]
