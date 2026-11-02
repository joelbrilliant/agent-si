"""brain-fetch check: is each capture collector ready, and what does its folder hold.

It reads every file in the folder with the real readers (run it on your own folder), changes nothing, and shares reads.intake_status with the brief."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from brain import marks, reads, store
from brain_fetch import credentials, net, readers, scan, service, state
from brain_fetch.common import Ctx, service_of

NEEDS_CREDENTIAL = False  # the documents folder needs no sign-in; the network services read their own


def _folder(raw: str) -> dict:
    if not raw:
        return {"set": False, "exists": False, "readable": False}
    path = os.path.expanduser(raw)
    exists = os.path.isdir(path)
    return {"set": True, "exists": exists, "readable": exists and os.access(path, os.R_OK | os.X_OK)}


def _files(folder: dict, raw: str, collector, gate: bool, zone: str) -> dict:
    empty = {"documents": 0, "transcripts": 0, "unsupported": 0, "unreadable": [], "io_failed": [], "skipped": 0,
             "chars": 0, "no_text": 0, "by_type": {}}
    if not folder["readable"]:
        return empty
    try:
        res = scan.scan(os.path.expanduser(raw), collector.options["origin"], {}, collector.options["kinds"], zone, gate)
    except (scan.TooMany, OSError):
        return {**empty, "unreadable": ["the folder holds more than %d files" % scan.MAX_FILES]}
    kinds = [item["kind"] for item in res.items]
    return {"documents": kinds.count("document"), "transcripts": kinds.count("meeting_transcript"),
            "unsupported": res.unsupported, "unreadable": sorted(res.unreadable), "io_failed": sorted(set(res.io_failed)), "skipped": res.skipped,
            "chars": res.chars, "no_text": res.no_text, "by_type": dict(sorted(res.by_type.items()))}


def _last_run(instance, name: str):
    try:
        con = store.connect_readonly(Path(instance) / "brain.sqlite3")
        try:
            row = con.execute(
                "SELECT started_at, status, items, error FROM collector_runs WHERE collector = ? "
                "ORDER BY started_at DESC, id DESC LIMIT 1", (name,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return None if row is None else dict(zip(("started_at", "status", "items", "error"), row))


def _credential(instance_name: str, name: str) -> str:
    if not NEEDS_CREDENTIAL:
        return "not needed"
    try:
        found = credentials.read_secret(instance_name, name)
    except credentials.SecretFileError as exc:
        return f"missing ({exc})"
    return "found" if found else f"missing ({credentials.where_it_looked(instance_name, name)})"


def check(instance, cfg, names: list[str]) -> dict:
    instance = Path(instance)
    gate = readers.transcripts_allowed(instance)
    try:
        con = store.connect_readonly(instance / "brain.sqlite3")
        try:
            intake = {entry["collector"]: entry for entry in reads.intake_status(con, store.utc_now(), cfg)}
        finally:
            con.close()
    except sqlite3.Error:
        intake = {}
    out = []
    for name in names:
        collector = cfg.collectors[name]
        if service_of(collector) != "documents":
            out.append(_service(instance, cfg, collector, intake.get(name)))
            continue
        raw = collector.options.get("live_folder", "")
        folder = _folder(raw)
        files = _files(folder, raw, collector, gate, cfg.timezone)
        cursor = state.read_cursor(instance, name)
        problems = []
        if not folder["set"]:
            problems.append("no live_folder is set")
        elif not folder["readable"]:
            problems.append("folder is missing or unreadable")
        if files["unreadable"]:
            problems.append(f"{len(files['unreadable'])} files could not be read: " + ", ".join(files["unreadable"][:5]))
        if files["io_failed"]:
            problems.append(
                f"{len(files['io_failed'])} files or folders could not be read: " + ", ".join(files["io_failed"][:5])
            )
        entry = intake.get(name)
        if entry is not None:
            problems.append(f"intake {entry['state']} since {entry['since']}")
        out.append({
            "collector": name, "type": collector.type, "folder": folder, "files": files,
            "pdf_text": readers.pypdf_available(),
            "transcripts": "on" if gate else "off: transcripts need brain redact (schema 6)",
            "credential": _credential(cfg.name, name),
            "mark_key": "present" if marks.key_path(instance).is_file() else "absent",
            "cursor": {"present": state.cursor_path(instance, name).is_file(), "files": len(cursor)},
            "last_run": _last_run(instance, name), "intake": entry, "problems": problems,
        })
    return {"ok": not any(item["problems"] for item in out), "instance": str(instance), "collectors": out}


def _service(instance: Path, cfg, collector, entry) -> dict:
    """The credential is there, one cheap read-only call accepts it, and the last run's result."""
    name, kind = collector.name, service_of(collector)
    problems, token = [], "not tested"
    try:
        secret = credentials.read_secret(cfg.name, name)
        found = "found" if secret else f"missing ({credentials.where_it_looked(cfg.name, name)})"
    except credentials.SecretFileError as exc:
        secret, found = None, f"missing ({exc})"
    if secret is None:
        problems.append(f"no credential; brain-fetch setup {kind} says where it goes")
    else:
        try:
            token = service.MODULES[kind].probe(Ctx(instance, cfg, collector, secret, {}, store.utc_now()))
        except net.NetError as exc:
            token = f"failed ({exc.error_class}: {exc})"
            problems.append(f"the service refused the credential: {exc}" if exc.error_class == "auth" else str(exc))
    if entry is not None:
        problems.append(f"intake {entry['state']} since {entry['since']}")
    return {"collector": name, "type": collector.type, "service": kind, "credential": found, "token": token,
            "mark_key": "present" if marks.key_path(instance).is_file() else "absent",
            "cursor": {"present": state.cursor_path(instance, name).is_file()},
            "last_run": _last_run(instance, name), "intake": entry, "problems": problems}
