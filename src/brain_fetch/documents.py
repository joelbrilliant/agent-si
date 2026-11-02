"""The documents folder run: scan, write exports, collect them, move the cursor."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from brain import collect, config as config_mod, store
from brain.applier import Unavailable, Unreadable
from brain_fetch import readers, scan, state

CHUNK_ITEMS = 5000
CHUNK_BYTES = 24 * 1024 * 1024
FORMAT = "agent-brain.capture.v1"


def result_for(name: str, status: str, message: str = "", **extra) -> dict:
    files = {"changed": 0, "unchanged": 0, "unreadable": 0, "skipped": 0}
    files.update(extra.pop("files", {}))
    return {"collector": name, "status": status, "files": files, "exports": 0, "cursor": "held",
            "pruned": 0, "message": message, **extra}


def _doc(origin, fetched, items, present, complete, error="", error_class="") -> dict:
    doc = {"format": FORMAT, "fetched_at": fetched, "complete": complete, "origin": origin,
           "items": items, "present": present}
    if error:
        doc["error"], doc["error_class"] = error, error_class
    return doc


def _chunks(items: list) -> list[list]:
    chunks, current, size = [], [], 0
    for item in items:
        weight = len(store.canonical_json(item).encode())
        if current and (len(current) >= CHUNK_ITEMS or size + weight > CHUNK_BYTES):
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += weight
    chunks.append(current)
    return chunks


def _edits(res, cursor: dict, origin: str) -> int:
    """Files in the cursor whose hash changed: the run-level mass-change guard."""
    count = 0
    for item in res.items:
        path = item["ref"].split(f":{origin}/", 1)[1]
        old = cursor.get(path)
        if isinstance(old, dict) and old.get("reader") != "failed" and old.get("sha256") != res.entries[path]["sha256"]:
            count += 1
    return count


def _error_text(instance, run_id) -> str:
    try:
        con = store.connect_readonly(Path(instance) / "brain.sqlite3")
        try:
            row = con.execute("SELECT error FROM collector_runs WHERE id = ?", (run_id,)).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return ""
    return row[0] if row else ""


def run(instance, name: str, *, rescan: bool = False, allow_mass_change: bool = False) -> dict:
    instance = Path(instance)
    cfg = config_mod.load_config(instance / "brain.toml")
    try:
        with state.run_lock(instance, name):
            return _locked(instance, cfg, cfg.collectors[name], rescan, allow_mass_change)
    except state.Busy as exc:
        return result_for(name, "busy", str(exc))


def _locked(instance: Path, cfg, collector, rescan: bool, allow: bool) -> dict:
    name, options = collector.name, collector.options
    if not options.get("live_folder"):
        return result_for(name, "skipped", "no live_folder is set")
    folder, origin = os.path.expanduser(options["live_folder"]), options["origin"]
    cursor = {} if rescan else state.read_cursor(instance, name)
    fetched = store.utc_now()
    res, error = scan.Result(), None
    try:
        os.scandir(folder).close()
        res = scan.scan(folder, origin, cursor, options["kinds"], cfg.timezone, readers.transcripts_allowed(instance))
    except scan.TooMany:
        error = ("the folder holds more than %d files" % scan.MAX_FILES, "limit")
    except OSError:
        error = ("folder is missing or unreadable", "folder")
    chunks = [[]] if error else _chunks(res.items)
    if len(chunks) > 1 and not allow:
        moved = _edits(res, cursor, origin)
        if moved > 20 or (moved >= 5 and moved * 4 > len(cursor)):
            return result_for(
                name, "error",
                f"mass change: {moved} documents would change; rerun with --allow-mass-change if this is expected",
            )
    present = sorted(set(res.present))
    docs = []
    carried: list[str] = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        if error:
            docs.append(_doc(origin, fetched, [], [], False, *error))
            continue
        docs.append(_doc(origin, fetched, chunk, sorted(set(present + carried)) if last else [],
                         last and not res.io_failure))
        carried += [item["ref"] for item in chunk]
    inbox = state.inbox_dir(instance, name)
    paths = []
    try:
        for doc in docs:
            path = state.export_path(inbox, fetched.replace("-", "").replace(":", ""))
            state.write_export(path, doc)
            paths.append(path)
    except (state.InboxFull, OSError) as exc:
        return result_for(name, "error", str(exc), exports=len(paths))
    files = {"changed": res.changed, "unchanged": res.unchanged, "unreadable": len(res.unreadable),
             "skipped": res.skipped}
    codes, last_result = [], None
    for path in paths:
        try:
            last_result = collect.run(instance, name, path, allow_mass_change=allow)
        except Unavailable as exc:
            return result_for(name, "unavailable", str(exc), files=files, exports=len(paths))
        except Unreadable as exc:
            return result_for(name, "error", str(exc), files=files, exports=len(paths))
        codes.append(last_result.exit_code)
        if last_result.exit_code != 0:
            break
    payload = last_result.payload or {}
    ok = last_result.exit_code == 0
    status = payload.get("status") if ok else "error"
    message = "" if ok else _error_text(instance, payload.get("run_id")) or f"collect exited {last_result.exit_code}"
    if res.unreadable:
        message = "; ".join(filter(None, [message, f"{len(res.unreadable)} files could not be read"]))
    if res.io_failure:
        message = "; ".join(filter(None, [message, "some files or folders could not be read; the next run retries"]))
    advanced = ok and len(codes) == len(paths) and docs[-1]["complete"]
    if advanced:
        state.write_cursor(instance, name, res.entries)
    return result_for(name, status or "error", message, files=files, exports=len(paths),
                      cursor="advanced" if advanced else "held", pruned=state.prune_inbox(inbox))
