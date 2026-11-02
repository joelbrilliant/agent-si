"""brain redact: the question, the lock and the marker, the transaction, then the
steps after the commit: optimise the search index, VACUUM, delete the export files and replace the bundles.

Every stop is recovered by running the same command again."""
from __future__ import annotations

import errno
import fcntl
import getpass
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from brain import backup, ingress, redact_ops, schemas, scrub, store
from brain.applier import Applier, NotAllowed, Unavailable, Unreadable

VACUUM_ATTEMPTS = 6
MEMORY_CEILING = 2 * 1024 * 1024 * 1024
HEADROOM = 64 * 1024 * 1024
EXPORT_READ_MAX = 256 * 1024 * 1024
MARKER = backup.MARKER_NAME
_ROW_TABLES = (
    "sources", "claims", "facts", "fact_history", "decisions", "records", "record_history", "actions",
    "checklist_items", "projects", "signals", "unchecked_text", "threads", "meetings", "write_log", "collector_runs",
)
_SOURCE_ID = re.compile(schemas.SOURCE_ID)
_ENVELOPE = schemas.COMMAND_OPS["redact_source"]["envelope"]["properties"]
_REASON = re.compile(_ENVELOPE["reason"]["pattern"])
_OPERATOR = re.compile(_ENVELOPE["operator"]["pattern"])
_EXPORT_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]{2}\.json")
_COLLECTOR_DIR = re.compile(r"[a-z][a-z0-9-]{1,39}")
_OP_PREFIX = re.compile(r"^op [0-9]+ \([a-z_]+\): ")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _warn(text: str) -> None:
    print(f"brain: {text}", file=sys.stderr)


# -- arguments ------------------------------------------------------------------------------------

def check_arguments(source_id: str, reason: str, operator, backups) -> tuple[str, list[Path]]:
    """The validated operator and the extra bundle folders (absolute, sorted, no repeats)."""
    if not isinstance(source_id, str) or _SOURCE_ID.fullmatch(source_id) is None:
        raise Unreadable("source id is invalid")
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        raise Unreadable("--reason must be 3 to 200 characters on one line, with no @ and no control characters")
    if operator is None:
        try:
            operator = getpass.getuser()
        except Exception:  # getpass raises OSError, KeyError or ImportError depending on the platform
            operator = "unknown"
        if _OPERATOR.fullmatch(operator) is None:
            operator = "unknown"
    elif _OPERATOR.fullmatch(operator) is None:
        raise Unreadable(
            "--operator must be 1 to 60 letters, digits, spaces, dots, hyphens or underscores, "
            "starting with a letter or digit"
        )
    folders: dict[str, Path] = {}
    for raw in backups or ():
        path = Path(raw)
        if path.is_symlink() or not path.is_dir():
            raise Unreadable(f"--backups {raw} must be an existing directory, not a link")
        absolute = Path(os.path.abspath(path))
        folders[str(absolute)] = absolute
    return operator, [folders[key] for key in sorted(folders)]


# -- the store ------------------------------------------------------------------------------------

def recover(db: Path) -> None:
    """Roll back a hot journal left by an earlier stop. A read-only connection cannot, so open the store
    writable, take the write lock and let go."""
    try:
        con = sqlite3.connect(str(db), isolation_level=None, timeout=10.0)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute("ROLLBACK")
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        raise Unavailable("the store is busy; try again") from exc


def _read(db: Path):
    return store.connect_readonly(db)


def _committed(db: Path, change_id: str) -> bool:
    recover(db)
    con = _read(db)
    try:
        return con.execute(
            "SELECT 1 FROM write_log WHERE change_id = ? AND status = 'applied'", (change_id,)
        ).fetchone() is not None
    finally:
        con.close()


def _next_change_id(db: Path, source_id: str) -> str:
    stem = "brain-redact-" + hashlib.sha256(source_id.encode()).hexdigest()[:20]
    con = _read(db)
    try:
        count = con.execute("SELECT count(*) FROM write_log WHERE change_id GLOB ?", (stem + "-[0-9]*",)).fetchone()[0]
    finally:
        con.close()
    return f"{stem}-{count + 1}"


# -- the plan -------------------------------------------------------------------------------------

def plan(instance: Path, source_id: str, backups=()) -> dict:
    """What a redaction of source_id would rewrite, delete and keep. It first recovers the store (a read-only
    connection cannot roll a hot journal back), then reads."""
    instance = Path(instance)
    db = instance / "brain.sqlite3"
    if not db.is_file():
        raise Unavailable(f"no store at {db}")
    recover(db)
    con = _read(db)
    try:
        _check_version(con)
        if con.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
            raise NotAllowed(f"redact refused: no source {source_id}")
        survey = redact_ops.survey(con, source_id)
        sources = []
        for sid in survey["sources"]:
            row = con.execute(
                "SELECT id, external_ref, kind, source_date FROM sources WHERE id = ?", (sid,)
            ).fetchone()
            sources.append({"id": row[0], "external_ref": row[1], "kind": row[2], "source_date": row[3]})
        writers = {
            row[0].split(":", 1)[0]
            for row in con.execute(
                "SELECT captured_by FROM sources WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(survey["sources"]),)
            )
        }
        name = _instance_name(instance)
    finally:
        con.close()
    unchecked: list[str] = []
    exports = delete_exports(instance, survey["refs"], unchecked, delete=False)
    folders = {}
    for folder in (instance / "backups", *backups):
        folders[str(Path(folder).resolve() if Path(folder).exists() else Path(folder))] = len(
            _replaceable(Path(folder), name, folder == instance / "backups")
        )
    return {
        "source_id": source_id,
        "sources": sources,
        "rows": survey["rows"],
        "write_log_held": survey["write_log_held"],
        "threads_by_subject": survey["threads_by_subject"],
        "people": survey["people"],
        "systems": survey["systems"],
        "exports": exports,
        "exports_unchecked": unchecked,
        "folders": folders,
        "residual": survey["residual"],
        "collectors": _live_collectors(instance, writers),
    }


def _check_version(con) -> None:
    """An old or newer store is refused before the plan is printed and the person is asked to confirm."""
    from brain import migrate

    version, latest = con.execute("PRAGMA user_version").fetchone()[0], migrate.latest_version()
    if version > latest:
        raise Unavailable(f"store is at version {version}, newer than this engine ({latest})")
    if version != latest:
        raise Unavailable(f"store is at version {version}, engine expects {latest}; run brain migrate")


def _live_collectors(instance: Path, writers) -> list[str]:
    """The configured collectors, other than capture, that wrote a source of the chain. They read an external
    system again on every poll, so the original is the thing to change there."""
    from brain import config as config_mod

    try:
        cfg = config_mod.load_config(Path(instance) / "brain.toml")
    except config_mod.ConfigError:
        return []
    return sorted(name for name, item in cfg.collectors.items() if item.type != "capture" and item.agent in writers)


def _instance_name(instance: Path) -> str:
    from brain import config as config_mod

    try:
        return config_mod.load_config(instance / "brain.toml").name
    except config_mod.ConfigError as exc:
        raise Unavailable(f"brain.toml: {exc}") from exc


def render_plan(found: dict) -> str:
    """The text printed on stderr before the question."""
    sources = found["sources"]
    head = next((s for s in sources if s["id"] == found["source_id"]), sources[0])
    lines = [f"brain redact {head['id']} ({head['external_ref'] or 'no external ref'}, {head['kind']}, {head['source_date']})"]
    others = [s["id"] for s in sources if s["id"] != head["id"]]
    if others:
        noun = "version" if len(others) == 1 else "versions"
        lines.append(
            f"Also redacted: {len(others)} other {noun} of the same item, {', '.join(others)} "
            "(including the current one: that text cannot be captured again until the file changes)."
        )
    rows = ", ".join(
        f"{table} {found['rows'][table]}" + (f" ({found['write_log_held']} held)" if table == "write_log" and found["write_log_held"] else "")
        for table in _ROW_TABLES
        if found["rows"].get(table)
    )
    lines.append(f"It rewrites: {rows or 'no rows'}")
    folders = " and ".join(f"{path} ({count})" for path, count in found["folders"].items())
    export_count = len(found["exports"])
    lines.append(
        f"It deletes: {export_count} export file{'' if export_count == 1 else 's'} in fetch/inbox, and every older "
        f"bundle of this brain in {folders}"
    )
    if found["exports_unchecked"]:
        lines.append("It could not check: " + ", ".join(found["exports_unchecked"]) + " (they may hold the text).")
    if found["threads_by_subject"]:
        lines.append(
            "Threads taken in by their subject: "
            + ", ".join(f"{t['id']} ({t['channel']}, {t['account']})" for t in found["threads_by_subject"])
        )
    if found.get("residual"):
        lines.append("Still holds the text without quote marks after the scrub (listed, not removed): " + _residual_text(found["residual"]))
    if found.get("collectors"):
        lines.append(
            "Written by collector " + ", ".join(found["collectors"]) + ": it reads its source again on every poll and "
            "can store this text again unless the original is changed there. Subjects, titles and test names that "
            "were redacted stay redacted."
        )
    lines.append(
        "It keeps: numbers, dates and true/false values, locators and labels, the item's name, "
        f"people {found['people']} and systems {found['systems']} rows that cite it, and any note that copies "
        "the text without quote marks or restates it."
    )
    lines.append("Backups and copies outside those folders still hold the text: see docs/REDACTION.md.")
    lines.append("This cannot be undone. Type the source id to confirm:")
    return "\n".join(lines)


def _residual_text(residual: dict) -> str:
    parts = []
    for table in sorted(residual):
        ids = residual[table]
        shown = ", ".join(ids[: redact_ops.RESIDUAL_LIST_MAX])
        more = f" and {len(ids) - redact_ops.RESIDUAL_LIST_MAX} more" if len(ids) > redact_ops.RESIDUAL_LIST_MAX else ""
        parts.append(f"{table} {shown}{more}")
    return "; ".join(parts) + "."


def _confirm(found: dict, yes: bool, stdin) -> None:
    print(render_plan(found), file=sys.stderr, flush=True)
    if yes:
        return
    tty = getattr(stdin, "isatty", None)
    if tty is None or not tty():
        raise NotAllowed("redact refused: standard input is not a terminal; pass --yes to confirm")
    answer = stdin.readline().rstrip("\r\n")
    if answer != found["source_id"]:
        raise NotAllowed("redact refused: the source id typed does not match")


# -- the command ----------------------------------------------------------------------------------

def command(instance: Path, source_id: str, reason: str, *, yes: bool = False, operator=None, backups=(),
            stdin=None) -> dict:
    """Validate, plan, ask, then run."""
    operator, folders = check_arguments(source_id, reason, operator, backups)
    instance = Path(instance)
    stdin = sys.stdin if stdin is None else stdin
    found = plan(instance, source_id, folders)
    _confirm(found, yes, stdin)

    def again(new: dict) -> None:
        if yes:
            raise NotAllowed("redact refused: the chain changed after the plan; run it again")
        _confirm(new, yes, stdin)

    return run(instance, source_id, reason, operator, folders, confirmed=[s["id"] for s in found["sources"]],
               changed=again, plan_again=lambda: plan(instance, source_id, folders))


def _refuse_marker(instance: Path, source_id: str, folders: list[Path]) -> None:
    marker = instance / MARKER
    if not marker.is_file():
        return
    try:
        recorded = json.loads(marker.read_text(encoding="utf-8"))  # trusted: the REDACTING marker, written by brain redact
        other = recorded.get("source_id", "")
        used = recorded.get("backups", [])
    except (OSError, ValueError, AttributeError):
        recorded, other, used = {}, "", []
    if other and other != source_id:
        raise NotAllowed(f"redact refused: a redaction of {other} did not finish; run brain redact {other} first")
    if other == source_id and sorted(used if isinstance(used, list) else []) != [str(p) for p in folders]:
        shown = ", ".join(used) if used else "none"
        raise NotAllowed(f"redact refused: the unfinished redaction used --backups {shown}; pass the same folders")


def _write_marker(instance: Path, source_id: str, change_id: str, folders: list[Path]) -> None:
    body = store.canonical_json(
        {"source_id": source_id, "change_id": change_id, "started_at": store.utc_now(),
         "backups": [str(p) for p in folders]}
    )
    tmp = instance / f".{MARKER}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, instance / MARKER)


def _check_room(instance: Path, db: Path, folders: list[Path]) -> None:
    size = db.stat().st_size
    if size > MEMORY_CEILING:
        raise NotAllowed("redact refused: the database is over 2 GiB; VACUUM would need that much memory")
    device = instance.stat().st_dev
    shared = sum(1 for folder in folders if folder.stat().st_dev == device)
    need = 2 * size + size * (1 + shared) + HEADROOM
    if shutil.disk_usage(instance).free < need:
        raise NotAllowed(f"redact refused: not enough free disk space (need {math.ceil(need / (1 << 20))} MiB)")


def run(instance: Path, source_id: str, reason: str, operator: str, folders=(), *, confirmed=None,
        changed=None, plan_again=None) -> dict:
    from brain import config as config_mod

    instance = Path(instance)
    folders = [Path(p) for p in folders]
    db = instance / "brain.sqlite3"
    if not db.is_file():
        raise Unavailable(f"no store at {db}")
    try:
        config_mod.load_config(instance / "brain.toml")
    except config_mod.ConfigError as exc:
        raise Unavailable(f"brain.toml: {exc}") from exc
    if (instance / "PAUSED").exists():
        raise Unavailable("instance is paused: see PAUSED; investigate, then delete it")
    lock = _take_lock(instance)
    try:
        recover(db)
        con = _read(db)
        try:
            if con.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is None:
                raise NotAllowed(f"redact refused: no source {source_id}")
            chain = redact_ops.chain_ids(con, source_id)
        finally:
            con.close()
        if confirmed is not None and chain != sorted(confirmed):
            changed(plan_again())
        _refuse_marker(instance, source_id, folders)
        _check_room(instance, db, folders)
        _preflight(instance)
        change_id = _next_change_id(db, source_id)
        _write_marker(instance, source_id, change_id, folders)
        return _finish(instance, db, source_id, reason, operator, folders, change_id)
    finally:
        lock.close()


def _preflight(instance: Path) -> None:
    """Nothing is committed, and no marker is written, while `brain check` already reports a problem the
    redaction's own steps would only trip over later (a bundle that cannot be taken, for one). An unfinished
    redaction of this same source is the one thing a rerun is for."""
    own = ("a redaction did not finish", "redaction_gate is not empty", "redaction_packets is not empty")
    # the applier refuses the stale gate and packet list itself (exit 5), and a rerun is what finishes a redaction
    # and so is a leftover copy of a redacted source ("source <id> is redacted, but ... still holds its text")
    problems = [
        p for p in store.check_instance(instance)
        if not p.startswith(own) and re.fullmatch(r"source \S+ is redacted, but .* still holds its text", p) is None
    ]
    if problems:
        raise NotAllowed("redact refused: brain check reports: " + "; ".join(problems[:3]))


def _take_lock(instance: Path):
    try:
        handle = backup.open_lock(instance)
    except OSError as exc:
        raise Unavailable(f"cannot open {backup.LOCK_NAME}: {exc}") from exc
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            raise Unavailable("a backup or another redaction is running; try again when it finishes") from exc
        raise
    return handle


def _remove_marker(instance: Path) -> None:
    (instance / MARKER).unlink(missing_ok=True)


def _finish(instance, db, source_id, reason, operator, folders, change_id) -> dict:
    committed_line = f"redaction of {source_id} is committed; run brain redact {source_id} again"
    packet = {
        "packet_version": 1,
        "change_id": change_id,
        "agent": "brain",
        "intent": f"redact source {source_id}",
        "ops": [{"op": "redact_source", "source_id": source_id, "reason": reason, "operator": operator}],
    }
    applier = None
    try:
        applier = Applier(instance, allow_reserved_agent=True)
        outcome = applier.apply(store.canonical_json(packet).encode())
        info = applier.redaction
    except (NotAllowed, Unavailable, Unreadable):
        applier = _close(applier)
        if not _committed(db, change_id):
            _remove_marker(instance)
        raise
    except Exception as exc:
        applier = _close(applier)
        if _committed(db, change_id):
            _warn(f"{exc}")
            raise Unavailable(committed_line) from exc
        _remove_marker(instance)
        raise Unavailable(f"redaction of {source_id} failed before it committed: {exc}") from exc
    finally:
        _close(applier)
    status = outcome.receipt["status"]
    if status != "applied":
        _remove_marker(instance)
        raise NotAllowed("redact refused: " + _OP_PREFIX.sub("", outcome.receipt["error"]))
    if outcome.exit_code == 5:
        raise Unavailable(
            f"redaction of {source_id} is committed, but the store failed its check ({outcome.integrity}); see PAUSED"
        )
    try:
        return _after_commit(instance, db, source_id, change_id, folders, outcome, info)
    except Unavailable as exc:
        if f"run brain redact {source_id} again" in str(exc):
            raise
        # A refusal from the bundle step, for one, must say that the redaction committed
        _warn(f"{exc}")
        raise Unavailable(committed_line) from exc
    except Exception as exc:
        _warn(f"{exc}")
        raise Unavailable(committed_line) from exc


def _close(applier) -> None:
    if applier is not None:
        try:
            applier.close()
        except sqlite3.Error:
            pass
    return None


# -- after the commit -----------------------------------------------------------------------------

def _open_compact(db: Path):
    con = sqlite3.connect(str(db), isolation_level=None, timeout=10.0)
    con.execute("PRAGMA temp_store = MEMORY")
    return con


def _optimize(con) -> None:
    """Deleting and re-inserting an FTS5 row leaves its lower-cased tokens in the shadow tables until the
    segments are merged."""
    con.execute("INSERT INTO search_index(search_index) VALUES('optimize')")


def _vacuum(con, source_id: str) -> None:
    last = None
    for attempt in range(VACUUM_ATTEMPTS):
        try:
            con.execute("VACUUM")
            return
        except sqlite3.OperationalError as exc:
            last = exc
            if attempt + 1 < VACUUM_ATTEMPTS:
                _sleep(min(2**attempt, 30))
    raise Unavailable(
        f"redaction of {source_id} is committed, but VACUUM could not run ({last}); run brain redact {source_id} again"
    )


def _freelist(con) -> int:
    return con.execute("PRAGMA freelist_count").fetchone()[0]


def compact(db: Path, source_id: str) -> dict:
    before = db.stat().st_size
    con = _open_compact(db)
    try:
        _optimize(con)
        _vacuum(con, source_id)
        free = _freelist(con)
    finally:
        con.close()
    if free != 0:
        raise Unavailable(f"VACUUM left {free} free pages; run brain redact {source_id} again")
    return {"bytes_before": before, "bytes_after": db.stat().st_size, "freelist_count": free}


def _after_commit(instance, db, source_id, change_id, folders, outcome, info) -> dict:
    database = compact(db, source_id)
    unchecked: list[str] = []
    exports = delete_exports(instance, info["refs"], unchecked)
    for path in unchecked:
        _warn(f"{path} could not be checked and may hold the text")
    name = _instance_name(instance)
    out = instance / "backups"
    made = backup.backup(instance, out, "post-redaction", redacting=True)
    bundle = Path(made["bundle"])
    pruned: dict[str, list[str]] = {}
    unrecognised: dict[str, list[str]] = {}
    for folder in (out, *folders):
        if folder != out:
            _copy_bundle(bundle, folder)
        removed, left = replace_bundles(folder, name, bundle, own=folder == out)
        pruned[str(folder.resolve())] = removed
        if left:
            unrecognised[str(folder.resolve())] = left
            _warn(f"{folder.resolve()} holds {', '.join(left)}, which redaction did not remove; they may hold the text")
    residual = {table: ids[: redact_ops.RESIDUAL_LIST_MAX] for table, ids in info["residual"].items()}
    if info["residual"]:
        _warn("rows still hold the removed text without quote marks (not removed): " + _residual_text(info["residual"]))
    originals = find_originals(instance, info["refs"])
    for path in originals:
        _warn(f"the original {path} is still there; delete or move it, or brain-fetch run --rescan will copy it into the inbox again")
    _remove_marker(instance)
    rows = {table: 0 for table in _ROW_TABLES}
    for item in outcome.receipt["targets"]:
        rows[item["table"]] = rows.get(item["table"], 0) + 1
    return {
        "status": "redacted",
        "source_id": source_id,
        "sources": info["sources"],
        "change_id": change_id,
        "rows": rows,
        "held_rejected": info["held_rejected"],
        "signals_opened": info["signals_opened"],
        "database": database,
        "exports_deleted": exports,
        "exports_unchecked": unchecked,
        "bundle": str(bundle),
        "pruned": pruned,
        "threads_by_subject": info["threads_by_subject"],
        "unrecognised": unrecognised,
        "originals": originals,
        "residual": residual,
    }


# -- exports --------------------------------------------------------------------------------------

def delete_exports(instance: Path, refs, unchecked: list[str] | None = None, *, delete: bool = True) -> list[str]:
    """Delete the brain-fetch exports under fetch/inbox/<collector>/ whose items carry one of refs.
    Regular files only, never through a link, only files named as brain-fetch names them, up to the cap, each
    parsed. A file that could not be checked is left alone and appended to `unchecked` (relative paths). The
    deleted names are returned relative to the instance, sorted. It never raises on a hostile file."""
    removed: list[str] = []
    unchecked = [] if unchecked is None else unchecked
    wanted = {ref for ref in refs if ref}
    instance = Path(instance)
    fetch, inbox = instance / "fetch", instance / "fetch" / "inbox"
    if not wanted or fetch.is_symlink() or inbox.is_symlink() or not inbox.is_dir():
        return removed
    root = instance.resolve()
    try:
        if inbox.resolve().parent.parent != root:
            return removed
        with os.scandir(inbox) as top:
            entries = sorted(top, key=lambda e: e.name)
    except OSError:
        return removed

    def rel(path) -> str:
        return str(Path(path).relative_to(instance))

    for folder in entries:
        if not (_COLLECTOR_DIR.fullmatch(folder.name) and folder.is_dir(follow_symlinks=False)):
            unchecked.append(rel(folder.path))
            continue
        try:
            with os.scandir(folder.path) as files:
                members = sorted(files, key=lambda e: e.name)
        except OSError:
            unchecked.append(rel(folder.path))
            continue
        for entry in members:
            name = rel(entry.path)
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False) or not _EXPORT_NAME.fullmatch(entry.name):
                    unchecked.append(name)
                    continue
                if entry.stat(follow_symlinks=False).st_size > EXPORT_READ_MAX:
                    unchecked.append(name)
                    continue
                doc = ingress.load_file(entry.path, limit=EXPORT_READ_MAX, what=name)
            except (OSError, ingress.IngressError, MemoryError):
                unchecked.append(name)
                continue
            items = doc.get("items") if isinstance(doc, dict) else None
            if isinstance(items, list) and any(isinstance(i, dict) and i.get("ref") in wanted for i in items):
                if delete:
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        unchecked.append(name)
                        continue
                removed.append(name)
    return sorted(removed)


# -- bundles --------------------------------------------------------------------------------------

def _replaceable(folder: Path, name: str, own: bool) -> list[Path]:
    """The bundles of this brain that a redaction would delete from `folder`."""
    found = []
    for path in backup.list_bundles(folder, name):
        if not own:
            manifest = backup.read_manifest(path)
            if manifest is None or manifest.get("instance") != name:
                continue
        found.append(path)
    return found


def _copy_bundle(bundle: Path, folder: Path) -> None:
    target = folder / bundle.name
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise backup.Refused(f"redact refused: {target} already exists") from None
    with os.fdopen(fd, "wb") as out, open(bundle, "rb") as source:
        shutil.copyfileobj(source, out, 1024 * 1024)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(target, 0o600)


def replace_bundles(folder: Path, name: str, keep: Path, *, own: bool) -> tuple[list[str], list[str]]:
    """Delete every bundle of this brain in `folder` but `keep` (in an extra folder, only those whose manifest
    names this brain), and in the instance's own folder every stale `.backup-*` and `.tmp-*` entry. Returns the
    names deleted and the names left that are not the new bundle."""
    doomed = [path for path in _replaceable(folder, name, own) if path.name != keep.name]
    removed = backup.delete_bundles(doomed)
    if own:
        for path in sorted(folder.iterdir()):
            if path.name.startswith((".backup-", ".tmp-")):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    try:
                        path.unlink()
                    except OSError:
                        pass
    left = sorted(path.name for path in folder.iterdir() if path.name != keep.name)
    return sorted(removed), left


# -- originals ------------------------------------------------------------------------------------

def find_originals(instance: Path, refs) -> list[str]:
    """The files under a capture collector's live folder that the refs name and that still exist. The folder is
    read only for the collector whose origin the ref matches, and a `..` segment or a link that leads out of the
    folder is never followed."""
    from brain import config as config_mod

    try:
        cfg = config_mod.load_config(Path(instance) / "brain.toml")
    except config_mod.ConfigError:
        return []
    found = set()
    for collector in cfg.collectors.values():
        if collector.type != "capture":
            continue
        folder_text = collector.options.get("live_folder") or ""
        origin = collector.options.get("origin")
        if not folder_text or not origin:
            continue
        folder = Path(os.path.expanduser(folder_text))
        try:
            base = Path(os.path.realpath(folder))
        except OSError:
            continue
        for ref in refs:
            for kind in schemas_kinds(collector):
                prefix = f"{kind}:{origin}/"
                if not ref.startswith(prefix) or len(ref) == len(prefix):
                    continue
                parts = ref[len(prefix):].split("/")
                if any(part in ("", ".", "..") for part in parts):
                    continue
                target = base.joinpath(*parts)
                try:
                    real = Path(os.path.realpath(target))
                    inside = real == base or base in real.parents
                    if inside and not any(p.is_symlink() for p in _parents_within(target, base)) and real.is_file():
                        found.add(str(target))
                except OSError:
                    continue
    return sorted(found)


def schemas_kinds(collector) -> list[str]:
    kinds = collector.options.get("kinds") or ()
    return list(dict.fromkeys(kinds))


def _parents_within(target: Path, base: Path):
    """The path components below `base` down to and including `target`."""
    current = target
    while current != base and base in current.parents:
        yield current
        current = current.parent
