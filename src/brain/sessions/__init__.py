"""Past agent sessions as memory: the record (brain.sessions.record) and the readers that fill it
(brain.sessions.adapters). Recording is on unless brain.toml says `[sessions] record = false`."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from brain.sessions import record
from brain.sessions.adapters import SessionRoots


def roots_for(config) -> SessionRoots:
    """Where this brain's sessions come from: the tools' default folders, plus the Hermes profiles brain.toml names."""
    return SessionRoots.default(hermes_profiles=dict(config.sessions.hermes_profiles))


def refresh(instance_dir, config) -> record.SyncResult | None:
    """Sync the record for this brain, or None when recording is off."""
    if not config.sessions.record:
        return None
    return record.sync(record.path_for(Path(instance_dir)), roots_for(config))


def asking_session(env) -> tuple[str, str] | None:
    """The session this process was started from, where the tool says so, so a search can leave out the
    conversation it is asked in. Claude Code gives the servers it starts CLAUDE_CODE_SESSION_ID. A tool that names
    no session leaves this None, and its live session is searched like any other."""
    claude = (env.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    return ("claude", claude) if claude else None


def summary(instance_dir, config) -> dict:
    """Sync the record and say what it holds, in counts only: for setup and connect to show. Recording off, or a
    record that can't be written, is said plainly and never stops the caller."""
    if not config.sessions.record:
        return {"record": False}
    db = record.path_for(Path(instance_dir))
    try:
        result = refresh(instance_dir, config)
    except (OSError, sqlite3.Error) as exc:
        return {"record": True, "error": type(exc).__name__}
    held = record.status(db)
    return {
        "record": True,
        "file": str(db),
        "sessions": held["sessions"],
        "messages": held["messages"],
        "new_messages": result.new_messages,
        "seconds": round(result.seconds, 1),
    }


def summary_line(found: dict) -> str:
    """One line for a person: what was recorded, that it stays here, and how to turn it off or remove a session."""
    if not found.get("record"):
        return ("Past sessions: not recorded. To record them, set record = true under [sessions] in brain.toml, "
                "then run brain sessions sync.")
    if found.get("error"):
        return f"Past sessions: the record couldn't be written ({found['error']}). Run brain sessions sync to try again."
    control = ("To stop, set record = false under [sessions] in brain.toml. To remove one, run brain sessions "
               "forget <session id>.")
    count = sum(found["sessions"].values())
    if not count:
        return ("Past sessions: none found yet. What you and your agent tools say is recorded on this machine "
                "from now on. " + control)
    tools = ", ".join(sorted(found["sessions"]))
    return (f"Past sessions: {count:,} recorded ({found['messages']:,} messages from {tools}), in "
            f"{found['file']}. They stay on this machine. " + control)
