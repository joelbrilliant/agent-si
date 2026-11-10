"""Usage in real life: one content-free line per read call, and a local report.

A line holds the minute, the tool, the MCP client's name, the hub token's label, the result's bytes and rows, an
outcome enum and the server's milliseconds. A search line also says whether no hit had every word, so any word
counted (relaxed). Never a query, title, name, id, key, value, quote or path. Each line
is under 512 bytes and written in one append, so several serve processes can share a month's file.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain import store

OUTCOMES = ("hit", "empty", "unknown_key", "ambiguous_key", "disputed", "error")
KEEP_MONTHS = 13
_LABEL = re.compile(r"[^A-Za-z0-9 ._-]")
_ROW_KEYS = {"search": "hits", "records_query": "rows", "signals": "signals"}


def label(text, limit: int = 40) -> str:
    """A client name or token label as an identifier: letters, digits, space, dot, underscore and dash only."""
    return _LABEL.sub("", str(text or ""))[:limit].strip()


def outcome(tool: str, result) -> tuple[str, int]:
    """(outcome, rows) from a tool result, by its code and shape only, never its content."""
    if result.is_error:
        code = result.code or ""
        if code == "no_value":
            return "empty", 0
        if code == "unknown_key":
            return "unknown_key", 0
        if code.startswith("ambiguous"):
            return "ambiguous_key", 0
        return "error", 0
    structured = result.structured if isinstance(result.structured, dict) else {}
    if tool in _ROW_KEYS:
        rows = len(structured.get(_ROW_KEYS[tool]) or [])
        if tool == "search":
            rows += len(structured.get("sessions") or [])
    elif tool == "get":
        rows = 1 if structured.get("row") else 0
    else:
        rows = 1
    if any(structured.get(key) == "disputed" for key in ("state", "status")):
        return "disputed", rows
    return ("hit" if rows else "empty"), rows


def relaxed(tool: str, result) -> bool | None:
    """For a search, whether no hit had every word, so any word counted; None for any other tool or an error."""
    if tool != "search" or result.is_error:
        return None
    structured = result.structured if isinstance(result.structured, dict) else {}
    return structured.get("relaxed") is True


def _month_file(instance: Path, now: str) -> Path:
    return Path(instance) / "usage" / f"{now[:7]}.jsonl"


def record(instance, *, tool: str, client: str, token: str, result_bytes: int, rows: int, result: str, ms: float,
           session: str, first: bool, relaxed: bool | None = None) -> None:
    """Append one line. A failure to log never fails the read: the caller ignores OSError."""
    now = store.utc_now()
    item = {"minute": now[:16] + "Z", "tool": label(tool, 30), "client": label(client), "token": label(token),
            "bytes": int(result_bytes), "rows": int(rows), "outcome": result if result in OUTCOMES else "error",
            "ms": round(float(ms), 2), "session": label(session, 16), "first": bool(first)}
    if relaxed is not None:
        item["relaxed"] = bool(relaxed)
    line = json.dumps(item, sort_keys=True, separators=(",", ":")) + "\n"
    data = line.encode()
    if len(data) >= 512:
        return
    path = _month_file(instance, now)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def prune(instance, now: str | None = None) -> int:
    """Delete monthly files more than 13 months old."""
    now = now or store.utc_now()
    year, month = int(now[:4]), int(now[5:7])
    cutoff = (year * 12 + month - 1) - (KEEP_MONTHS - 1)  # this month and the 12 before it
    removed = 0
    for path in sorted((Path(instance) / "usage").glob("*.jsonl")):
        match = re.fullmatch(r"(\d{4})-(\d{2})\.jsonl", path.name)
        if match and int(match.group(1)) * 12 + int(match.group(2)) - 1 < cutoff:
            path.unlink()
            removed += 1
    return removed


# -- brain usage report --------------------------------------------------------------------------------------

def _lines(instance: Path, start: str, end: str) -> list[dict]:
    found = []
    for path in sorted((instance / "usage").glob("*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                item = json.loads(raw)  # trusted: a usage log line, written by brain serve and the hub
            except ValueError:
                continue
            if isinstance(item, dict) and isinstance(item.get("minute"), str) and start <= item["minute"] < end:
                found.append(item)
    return found


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))] if ordered else 0


def _writes(instance: Path, start: str, end: str, now: str) -> dict:
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        by_status = dict(con.execute(
            "SELECT status, count(*) FROM write_log WHERE decided_at >= ? AND decided_at < ? GROUP BY status",
            (start.replace("Z", ":00Z"), end.replace("Z", ":00Z"))).fetchall())
        held = con.execute("SELECT count(*), min(decided_at) FROM write_log WHERE status = 'held'").fetchone()
        reviewed = dict(con.execute(
            "SELECT CASE WHEN status = 'applied' THEN 'approved' ELSE 'refused' END, count(*) FROM write_log "
            "WHERE reviewed_at >= ? AND reviewed_at < ? GROUP BY 1",
            (start.replace("Z", ":00Z"), end.replace("Z", ":00Z"))).fetchall())
    except sqlite3.Error:
        return {"applied": 0, "held": 0, "rejected": 0, "held_now": 0, "oldest_held_hours": None,
                "approved": 0, "refused": 0}
    finally:
        con.close()
    oldest = None
    if held[1]:
        try:
            age = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(held[1], "%Y-%m-%dT%H:%M:%SZ")
            oldest = round(age.total_seconds() / 3600, 1)
        except ValueError:
            oldest = None
    return {"applied": by_status.get("applied", 0), "held": by_status.get("held", 0),
            "rejected": by_status.get("rejected", 0), "held_now": held[0], "oldest_held_hours": oldest,
            "approved": reviewed.get("approved", 0), "refused": reviewed.get("refused", 0)}


TOKEN_FIELDS = ("input", "cache_read", "cache_write", "output")


def memory_usage_db() -> Path:
    """Agent memory's session_usage database, beside its sessions.db: AGENT_SESSION_DB's folder when that's set,
    else ${XDG_DATA_HOME:-~/.local/share}/agent-memory."""
    sessions = os.environ.get("AGENT_SESSION_DB")
    if sessions:
        return Path(sessions).with_name("usage.db")
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "agent-memory" / "usage.db"


def memory_tokens(since: str):
    """Model tokens per harness from agent memory, when it's installed and has counted: read-only, counts only.
    "unknown" without it, or when a harness never recorded a field, never zero."""
    db = memory_usage_db()
    if not db.is_file():
        return "unknown"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return "unknown"
    try:
        rows = con.execute(f"SELECT harness, {', '.join(TOKEN_FIELDS)} FROM session_usage "
                           "WHERE coalesce(ended_at, started_at, '') >= ?", (since,)).fetchall()
    except sqlite3.Error:
        return "unknown"
    finally:
        con.close()
    found: dict[str, dict] = {}
    for row in rows:
        entry = found.setdefault(label(row[0], 30) or "unknown", {"sessions": 0, **{kind: None for kind in TOKEN_FIELDS}})
        entry["sessions"] += 1
        for kind, value in zip(TOKEN_FIELDS, row[1:]):
            if isinstance(value, int):
                entry[kind] = (entry[kind] or 0) + value
    return {name: {key: ("unknown" if value is None else value) for key, value in entry.items()}
            for name, entry in sorted(found.items())}


def report(instance, period: str) -> dict:
    """The week's or month's report, as a dict of counts, enums, tool and client names only (the allowlist)."""
    instance = Path(instance)
    now = store.utc_now()
    days = 7 if period == "week" else 30
    end_dt = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    start = (end_dt - timedelta(days=days)).strftime("%Y-%m-%dT%H:%MZ")
    end = (end_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%MZ")
    lines = _lines(instance, start, end)
    tools: dict[str, dict] = {}
    for item in lines:
        entry = tools.setdefault(label(item.get("tool"), 30), {"calls": 0, "hits": 0, "errors": 0, "bytes": [], "ms": []})
        entry["calls"] += 1
        if "relaxed" in item:
            entry["logged"] = entry.get("logged", 0) + 1
            entry["relaxed"] = entry.get("relaxed", 0) + (item["relaxed"] is True)
        entry["hits"] += item.get("outcome") in ("hit", "disputed")
        entry["errors"] += item.get("outcome") == "error"
        entry["bytes"].append(int(item.get("bytes") or 0))
        entry["ms"].append(float(item.get("ms") or 0))
    sessions: dict[str, dict] = {}
    for item in lines:
        key = label(item.get("session"), 16)
        if key and key not in sessions:
            sessions[key] = {"client": label(item.get("client")), "first_brief": item.get("first") is True and item.get("tool") == "brief"}
    misses = {name: sum(1 for item in lines if item.get("outcome") == name) for name in OUTCOMES if name not in ("hit", "disputed")}
    clients: dict[str, int] = {}
    for session in sessions.values():
        clients[session["client"] or "unknown"] = clients.get(session["client"] or "unknown", 0) + 1
    calls = len(lines)
    return {
        "period": period, "from": start, "to": now, "calls": calls,
        "hit_rate": round(sum(entry["hits"] for entry in tools.values()) / calls, 3) if calls else None,
        "hit_rate_by_tool": {name: round(entry["hits"] / entry["calls"], 3) for name, entry in sorted(tools.items())},
        "misses": misses,
        "disputed_reads": sum(1 for item in lines if item.get("outcome") == "disputed"),
        # Searches where no hit had every word, out of the searches logged since 1.6.3 recorded it.
        "relaxed_searches": ({"relaxed": tools["search"]["relaxed"], "of": tools["search"]["logged"]}
                             if tools.get("search", {}).get("logged") else None),
        "brief_first_rate": round(sum(s["first_brief"] for s in sessions.values()) / len(sessions), 3) if sessions else None,
        "sessions_by_client": dict(sorted(clients.items())),
        "reads_by_tool": {name: {"calls": entry["calls"], "median_bytes": int(statistics.median(entry["bytes"])),
                                 "p95_bytes": _p95(entry["bytes"]), "errors": entry["errors"],
                                 "median_ms": round(statistics.median(entry["ms"]), 2)}
                          for name, entry in sorted(tools.items())},
        "bytes": sum(sum(entry["bytes"]) for entry in tools.values()),
        "writes": _writes(instance, start, end, now),
        # Model tokens come from agent memory's session_usage, when it's installed; the brain never sees them.
        "tokens": memory_tokens((end_dt - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    }


def markdown(data: dict) -> str:
    lines = [f"# Brain usage, {data['period']} to {data['to'][:10]}", "",
             f"- Read calls: {data['calls']}" + (f", hit rate {data['hit_rate']:.0%}" if data["hit_rate"] is not None else ""),
             f"- Sessions whose first call was brief: " + (f"{data['brief_first_rate']:.0%}" if data["brief_first_rate"] is not None else "no sessions"),
             f"- Disputed reads: {data['disputed_reads']}",
             "- Searches where any word counted: " + (f"{data['relaxed_searches']['relaxed']} of {data['relaxed_searches']['of']}"
                                                      if data.get("relaxed_searches") else "not logged yet"),
             "- Misses: " + ", ".join(f"{name} {count}" for name, count in data["misses"].items()),
             f"- Held queue: {data['writes']['held_now']} now"
             + (f", oldest {data['writes']['oldest_held_hours']} hours" if data["writes"]["oldest_held_hours"] is not None else "")
             + f"; {data['writes']['approved']} approved and {data['writes']['refused']} refused in the period",
             f"- Writes: {data['writes']['applied']} applied, {data['writes']['held']} held, {data['writes']['rejected']} rejected",
             "- Model tokens: " + (data["tokens"] if isinstance(data["tokens"], str) else "; ".join(
                 f"{name} {entry['sessions']} sessions, input {entry['input']}, cache read {entry['cache_read']}, "
                 f"cache write {entry['cache_write']}, output {entry['output']}" for name, entry in data["tokens"].items())
                 or "no sessions"),
             "", "| Tool | Calls | Median bytes | p95 bytes | Errors | Median ms |", "|---|---|---|---|---|---|"]
    for name, entry in data["reads_by_tool"].items():
        lines.append(f"| {name} | {entry['calls']} | {entry['median_bytes']} | {entry['p95_bytes']} | {entry['errors']} | {entry['median_ms']} |")
    return "\n".join(lines) + "\n"
