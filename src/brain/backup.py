"""brain backup and brain restore.

A bundle is one gzip tar: a manifest, the router, the store, the config and the fetch cursors. Restore
treats the bundle as hostile: it extracts nothing, checks every member, refuses a changed schema and
never touches a live instance. Clocks are read through store.utc_now so a test patches one name.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tarfile
import zlib
from datetime import datetime, timezone
from pathlib import Path

from brain import __version__
from brain import config as config_mod
from brain import ingress, migrate, store
from brain.applier import NotAllowed, Unavailable, Unreadable

FORMAT = "agent-brain.backup.v1"
_LABEL = re.compile(r"[a-z][a-z0-9-]{0,29}")
_CURSOR = re.compile(r"[a-z][a-z0-9-]{1,39}\.state\.json")
_CURSOR_MEMBER = re.compile(r"fetch/[a-z][a-z0-9-]{1,39}\.state\.json")
_STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z")
_FIXED = ("ROUTER.md", "brain.sqlite3", "brain.toml")
_MAGIC = b"SQLite format 3\x00"
_CHUNK = 1024 * 1024
_MIB = 1024 * 1024
_LIMITS = {
    "manifest.json": _MIB,
    "brain.toml": _MIB,
    "ROUTER.md": 64 * 1024,
    "brain.sqlite3": 8 * 1024 * 1024 * 1024,
}
_CURSOR_LIMIT = 16 * _MIB
_MAX_MEMBERS = 100
_STALE_SECONDS = 24 * 3600
_KEEP_PRE_UPDATE = 2
_ROUTER_NOTE = "aliases differ from brain.toml; run brain apply-config"
LOCK_NAME = ".redact.lock"
MARKER_NAME = "REDACTING"


class Refused(NotAllowed):
    pass


def _limit_for(name: str) -> int:
    return _CURSOR_LIMIT if _CURSOR_MEMBER.fullmatch(name) else _LIMITS[name]


def _total_limit() -> int:
    return sum(_LIMITS.values()) + _MAX_MEMBERS * _CURSOR_LIMIT


# -- shared helpers -----------------------------------------------------------------------------


def _now_epoch() -> float:
    """The store clock as epoch seconds, so a frozen store.utc_now freezes the staging cleanup too."""
    stamp = datetime.strptime(store.utc_now(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return stamp.timestamp()


def log_digest(con: sqlite3.Connection) -> tuple[int, int, str, str]:
    """(greatest applied_seq or 0, row count, greatest decided_at or "", digest) of write_log."""
    seq = con.execute("SELECT coalesce(max(applied_seq), 0) FROM write_log").fetchone()[0]
    last = con.execute("SELECT coalesce(max(decided_at), '') FROM write_log").fetchone()[0]
    digest = hashlib.sha256()
    count = 0
    for row in con.execute(
        "SELECT change_id, agent, status, decided_at, applied_seq, reviewed_by, reviewed_at, payload_sha256 "
        "FROM write_log ORDER BY change_id"
    ):
        count += 1
        change_id, agent, status, decided_at, applied_seq, reviewed_by, reviewed_at, payload = row
        line = "|".join(
            (
                change_id,
                agent,
                status,
                decided_at,
                "" if applied_seq is None else str(applied_seq),
                reviewed_by,
                "" if reviewed_at is None else reviewed_at,
                payload,
            )
        )
        digest.update((line + "\n").encode())
    return int(seq), count, last, digest.hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _bundle_name(name: str, utc: str, seq: int, label: str) -> str:
    stamp = utc.replace("-", "").replace(":", "")
    suffix = f"-{label}" if label else ""
    return f"{name}-{stamp}-seq{seq}{suffix}.tar.gz"


def list_bundles(out: Path, name: str, label: str | None = None) -> list[Path]:
    """The instance's bundles in out, oldest first by (utc, seq) read from the file name. With a label,
    only that label's."""
    pattern = re.compile(
        re.escape(name) + r"-([0-9]{8}T[0-9]{6}Z)-seq([0-9]+)(?:-([a-z][a-z0-9-]{0,29}))?\.tar\.gz"
    )
    found = []
    out = Path(out)
    if not out.is_dir():
        return []
    for path in out.iterdir():
        match = pattern.fullmatch(path.name)
        if match is None or not path.is_file() or path.is_symlink():
            continue
        if label is not None and (match.group(3) or "") != label:
            continue
        found.append((match.group(1), int(match.group(2)), path))
    found.sort(key=lambda item: (item[0], item[1], item[2].name))
    return [item[2] for item in found]


def delete_bundles(paths) -> list[str]:
    """Delete the named bundle files (2b's redaction deletes every bundle older than its own). Returns the
    names removed."""
    removed = []
    for path in paths:
        path = Path(path)
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path.name)
    return removed


# -- backup ---------------------------------------------------------------------------------------


def _clean_stale(out: Path) -> None:
    cutoff = _now_epoch() - _STALE_SECONDS
    for path in out.iterdir():
        if not (path.name.startswith(".backup-") or path.name.startswith(".tmp-")):
            continue
        try:
            info = path.lstat()
        except OSError:
            continue
        if info.st_mtime >= cutoff:
            continue
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                path.unlink()
            except OSError:
                pass


def _read_cursors(instance: Path) -> tuple[dict[str, bytes], list[str]]:
    """(cursor name to bytes, names left out because they are over 16 MiB)"""
    found: dict[str, bytes] = {}
    skipped: list[str] = []
    folder = instance / "fetch"
    if not folder.is_dir() or folder.is_symlink():
        return found, skipped
    for path in sorted(folder.iterdir()):
        if _CURSOR.fullmatch(path.name) is None:
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            if info.st_size > _CURSOR_LIMIT:
                skipped.append(path.name)
                continue
            found[f"fetch/{path.name}"] = path.read_bytes()
        except OSError:
            continue
    return found, skipped


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = 0
    info.mode = 0o600
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _write_bundle(handle, members: list[tuple[str, "bytes | Path", int]]) -> None:
    with gzip.GzipFile(filename="", fileobj=handle, mode="wb", compresslevel=6, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name, body, size in members:
                if isinstance(body, bytes):
                    tar.addfile(_tar_info(name, size), io.BytesIO(body))
                else:
                    with open(body, "rb") as source:
                        tar.addfile(_tar_info(name, size), source)


def backup(instance, out, label: str = "", *, redacting: bool = False) -> dict:
    """No bundle is written while a redaction runs or is unfinished, because free pages
    hold the removed text between its commit and its VACUUM. A backup holds `.redact.lock` shared, so two
    backups coexist and a redaction cannot start while one runs. `redacting` is passed only by brain redact,
    which already holds the lock exclusively, for its post-redaction bundle."""
    instance = Path(instance)
    if redacting:
        return _backup(instance, out, label)
    _refuse_if_unfinished(instance)
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        return _backup(instance, out, label)
    try:
        handle = open_lock(instance)
    except OSError:
        # No instance directory to lock (or one that cannot be written): _backup reports what is wrong.
        return _backup(instance, out, label)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("backup refused: a redaction is running; try again when it finishes") from None
        _refuse_if_unfinished(instance)
        return _backup(instance, out, label)
    finally:
        handle.close()


def open_lock(instance: Path):
    """The redaction lock file of an instance, created mode 0600 and opened for locking."""
    fd = os.open(Path(instance) / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
    return os.fdopen(fd, "a+b")


def _refuse_if_unfinished(instance: Path) -> None:
    if (instance / MARKER_NAME).is_file():
        found = store.redaction_marker_id(instance)
        raise Refused(f"backup refused: a redaction of {found} did not finish; run brain redact {found} again")


def read_manifest(path: Path) -> dict | None:
    """The manifest of a bundle, read without extracting anything else, or None if the file is not a bundle
    this engine can read. Used by brain redact to tell whose bundle sits in a folder it was given."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            member = tar.next()
            if member is None or member.name != "manifest.json" or not member.isfile() or member.size > _MIB:
                return None
            handle = tar.extractfile(member)
            found = ingress.loads(handle.read(), what="manifest") if handle is not None else None
    except (OSError, tarfile.TarError, ValueError, EOFError):  # IngressError is a ValueError
        return None
    return found if isinstance(found, dict) else None


def _backup(instance, out, label: str = "") -> dict:
    instance = Path(instance)
    out = Path(out)
    if label and _LABEL.fullmatch(label) is None:
        raise Unreadable(
            "--label must be 1 to 30 lower-case letters, digits and hyphens, starting with a letter"
        )
    try:
        cfg = config_mod.load_config(instance / "brain.toml")
    except config_mod.ConfigError as exc:
        raise Unavailable(f"brain.toml: {exc}") from exc
    db = instance / "brain.sqlite3"
    if not db.is_file():
        raise Unavailable(f"no store at {db}")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    _clean_stale(out)
    cursors, skipped = _read_cursors(instance)  # first, so a restored cursor is never ahead of the restored store
    paused = (instance / "PAUSED").exists()
    if paused:
        print("brain: instance is paused; backing up anyway", file=sys.stderr)
    for name in skipped:
        print(f"brain: cursor {name} is over 16 MiB and was left out of the bundle", file=sys.stderr)
    staging = out / f".backup-{secrets.token_hex(6)}"
    staging.mkdir(mode=0o700)
    tmp: Path | None = None
    try:
        copy = staging / "brain.sqlite3"
        try:
            source = store.connect_readonly(db)
            try:
                target = sqlite3.connect(str(copy))
                try:
                    source.backup(target)
                    # Planner statistics (ANALYZE) are not part of the engine's schema and restore refuses
                    # them in a bundle, so the copy leaves them out. The live store keeps its own.
                    for (stat,) in target.execute(
                        "SELECT name FROM sqlite_schema WHERE type = 'table' AND name IN "
                        "('sqlite_stat1', 'sqlite_stat2', 'sqlite_stat3', 'sqlite_stat4')"
                    ).fetchall():
                        target.execute(f"DROP TABLE {stat}")
                    target.commit()
                finally:
                    target.close()
            finally:
                source.close()
        except sqlite3.Error as exc:
            raise Unavailable(f"cannot read the store: {exc}") from exc
        shutil.copyfile(instance / "brain.toml", staging / "brain.toml")
        router = instance / "ROUTER.md"
        if router.is_file():
            shutil.copyfile(router, staging / "ROUTER.md")
        problems = store.check_instance(staging)
        if problems:
            raise Refused("backup refused: brain check found problems: " + "; ".join(problems[:3]))
        con = store.connect_readonly(copy)
        try:
            schema = con.execute("PRAGMA user_version").fetchone()[0]
            seq, rows, last, digest = log_digest(con)
        finally:
            con.close()
        listing: dict[str, dict] = {}
        bodies: dict[str, "bytes | Path"] = {}
        for name in _FIXED:
            sha, size = _sha256_file(staging / name)
            listing[name] = {"sha256": sha, "size": size}
            bodies[name] = staging / name
        for name, blob in cursors.items():
            listing[name] = {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)}
            bodies[name] = blob
        utc = store.utc_now()
        manifest = {
            "format": FORMAT,
            "engine": __version__,
            "schema": schema,
            "created_at": utc,
            "instance": cfg.name,
            "label": label,
            "applied_seq": seq,
            "write_log_rows": rows,
            "last_decided_at": last,
            "write_log_digest": digest,
            "paused": paused,
            "members": {name: listing[name] for name in sorted(listing)},
        }
        raw_manifest = store.canonical_json(manifest).encode()
        members = [("manifest.json", raw_manifest, len(raw_manifest))] + [
            (name, bodies[name], listing[name]["size"]) for name in sorted(listing)
        ]
        final = out / _bundle_name(cfg.name, utc, seq, label)
        if final.exists():
            raise Refused(f"backup refused: {final} already exists")
        tmp = out / f".tmp-{os.getpid()}-{final.name}"
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            _write_bundle(handle, members)
            handle.flush()
            os.fsync(handle.fileno())
        if not _verified(tmp, manifest):
            raise Refused("backup refused: the bundle failed its own verification")
        os.replace(tmp, final)
        tmp = None
        pruned = _prune(out, cfg.name, final) if label == "pre-update" else []
        return {
            "status": "backed_up",
            "bundle": str(final.resolve()),
            "bytes": final.stat().st_size,
            "label": label,
            "schema": schema,
            "applied_seq": seq,
            "write_log_rows": rows,
            "pruned": pruned,
            "skipped": skipped,
        }
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _verified(path: Path, manifest: dict) -> bool:
    """Stream the new bundle back through restore's member pass (which compares every size and hash with
    the manifest it read) and check that it is the manifest this backup meant to write."""
    try:
        _seen, stored = _scan(path, None, None)
    except (Refused, Unreadable):
        return False
    return stored == manifest


def _prune(out: Path, name: str, keep: Path) -> list[str]:
    """Keep the new pre-update bundle and the newest other one; delete the rest (pre-update only)."""
    others = [path for path in list_bundles(out, name, "pre-update") if path != keep]
    return delete_bundles(others[: max(len(others) - (_KEEP_PRE_UPDATE - 1), 0)])


# -- restore --------------------------------------------------------------------------------------


def _refuse(text: str) -> Refused:
    return Refused(f"restore refused: {text}")


def _scan(bundle: Path, stage: Path | None, target_parent: Path | None):
    """Scan a bundle, returning ({member: (sha256, size)}, manifest). With a
    stage directory the allowed members other than the manifest are copied into it by this function (never
    by tarfile). Raises Refused for a hostile bundle and Unreadable for a file that isn't a gzip tar."""
    seen: dict[str, tuple[str, int]] = {}
    manifest_bytes = b""
    total = 0
    try:
        tar = tarfile.open(str(bundle), mode="r|gz")
    except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
        raise Unreadable(f"bundle is unreadable: {exc}") from exc
    try:
        count = 0
        try:
            for member in tar:
                count += 1
                name = member.name
                allowed = name in _LIMITS or _CURSOR_MEMBER.fullmatch(name) is not None
                if not allowed:
                    raise _refuse(f"bundle member {name} is not allowed")
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise _refuse(f"bundle member {name} is not a regular file")
                if name in seen:
                    raise _refuse(f"bundle holds {name} twice")
                total += member.size
                if count > _MAX_MEMBERS or member.size > _limit_for(name) or total > _total_limit():
                    raise _refuse(f"bundle member {name} is larger than its limit")
                if name == "brain.sqlite3" and target_parent is not None:
                    need = 2 * member.size + 64 * _MIB
                    if shutil.disk_usage(target_parent).free < need:
                        raise _refuse(f"not enough free disk space (need {-(-need // _MIB)} MiB)")
                sha, size, body = _copy_member(tar, member, name, stage)
                seen[name] = (sha, size)
                if name == "manifest.json":
                    manifest_bytes = body
        except (tarfile.TarError, OSError, EOFError, zlib.error) as exc:
            raise Unreadable(f"bundle is unreadable: {exc}") from exc
    finally:
        tar.close()
    return seen, _check_manifest(seen, manifest_bytes)


def _copy_member(tar, member, name, stage: Path | None) -> tuple[str, int, bytes]:
    source = tar.extractfile(member)
    digest = hashlib.sha256()
    size = 0
    out = None
    chunks: list[bytes] = []
    try:
        if stage is not None and name != "manifest.json":
            path = stage / name
            if name.startswith("fetch/"):
                (stage / "fetch").mkdir(mode=0o700, exist_ok=True)
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
            out = os.fdopen(os.open(path, flags, 0o600), "wb")
        first = True
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            if first and name == "brain.sqlite3" and not chunk.startswith(_MAGIC):
                raise _refuse("bundle database is not a rollback-journal SQLite file")
            first = False
            digest.update(chunk)
            size += len(chunk)
            if name == "manifest.json":
                chunks.append(chunk)
            if out is not None:
                out.write(chunk)
        if name == "brain.sqlite3" and size == 0:
            raise _refuse("bundle database is not a rollback-journal SQLite file")
    finally:
        if out is not None:
            out.close()
    return digest.hexdigest(), size, b"".join(chunks)


_MANIFEST_FIELDS = {
    "format": str,
    "engine": str,
    "schema": int,
    "created_at": str,
    "instance": str,
    "label": str,
    "applied_seq": int,
    "write_log_rows": int,
    "last_decided_at": str,
    "write_log_digest": str,
    "paused": bool,
    "members": dict,
}


def _check_manifest(seen: dict, raw: bytes) -> dict:
    if "manifest.json" not in seen:
        raise _refuse("bundle has no manifest.json")
    invalid = _refuse("manifest.json is not a valid backup manifest")
    try:
        manifest = ingress.loads(raw, what="manifest.json")
    except ingress.IngressError:
        raise invalid from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise invalid
    for field, kind in _MANIFEST_FIELDS.items():
        value = manifest.get(field)
        if type(value) is not kind:
            raise invalid
    for name, entry in manifest["members"].items():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size"}
            or not isinstance(entry["sha256"], str)
            or type(entry["size"]) is not int
        ):
            raise invalid
    for name in _FIXED:
        if name not in seen:
            raise _refuse(f"bundle is missing {name}")
    for name in sorted(manifest["members"]):
        if name not in seen:
            raise _refuse(f"bundle is missing {name}")
    for name in sorted(seen):
        if name == "manifest.json":
            continue
        entry = manifest["members"].get(name)
        if entry is None or (entry["sha256"], entry["size"]) != seen[name]:
            raise _refuse(f"bundle member {name} doesn't match its manifest entry")
    return manifest


def _header_problem(path: Path) -> str | None:
    with open(path, "rb") as handle:
        head = handle.read(100)
    if len(head) < 100 or not head.startswith(_MAGIC) or head[18] != 1 or head[19] != 1:
        return "bundle database is not a rollback-journal SQLite file"
    if int.from_bytes(head[56:60], "big") != 1:
        return "bundle database is not UTF-8"
    return None


def _open_immutable(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True, isolation_level=None)
    con.execute("PRAGMA trusted_schema = OFF")
    return con


def _inspect_database(path: Path, manifest: dict) -> int:
    """Check the staged database file. Returns its schema version."""
    problem = _header_problem(path)
    if problem:
        raise _refuse(problem)
    latest = migrate.latest_version()
    try:
        con = _open_immutable(path)
        try:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version > latest:
                raise _refuse(f"bundle is at schema {version}, newer than this engine ({latest})")
            if version < 1:
                raise _refuse(f"bundle is at schema {version}, not a version this engine reads")
            differences = migrate.schema_differences(con, version)
            if differences:
                raise _refuse(f"bundle schema differs from the engine's: {differences[0]}")
            rows = [tuple(row) for row in con.execute("PRAGMA integrity_check")]
            if rows != [("ok",)]:
                raise _refuse("bundle database failed its integrity check")
            seq, count, _last, digest = log_digest(con)
        finally:
            con.close()
    except sqlite3.Error:
        raise _refuse("bundle database failed its integrity check") from None
    if (seq, count, digest) != (
        manifest["applied_seq"],
        manifest["write_log_rows"],
        manifest["write_log_digest"],
    ):
        raise _refuse("bundle database doesn't match its manifest sequence, count and digest")
    return version


def restore(bundle, target) -> dict:
    bundle = Path(bundle)
    target = Path(target)
    if target.is_symlink():
        raise _refuse(f"{target} is a link; brain restore never writes through one")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise _refuse(f"{target} is not empty; brain restore never overwrites")
    if not bundle.is_file():
        raise Unreadable("bundle is unreadable: missing")
    target = target.absolute()
    parent = target.parent
    if not parent.is_dir():
        raise Unreadable(f"{parent} is not an existing directory")
    stage = parent / f".{target.name}.restore-{secrets.token_hex(6)}"
    stage.mkdir(mode=0o700)
    done = False
    try:
        _seen, manifest = _scan(bundle, stage, parent)
        version = _inspect_database(stage / "brain.sqlite3", manifest)
        latest = migrate.latest_version()
        migrated_from = None
        if version < latest:
            migrate.migrate(stage / "brain.sqlite3")
            migrated_from = version
            if version < store.SEARCH_SINCE:
                from brain.init import search_rebuild

                search_rebuild(stage, f"restore from schema {version}")
        try:
            cfg = config_mod.load_config(stage / "brain.toml")
        except config_mod.ConfigError as exc:
            raise _refuse(f"brain.toml: {exc}") from exc
        from brain.init import render_router

        wanted = render_router(cfg.name, cfg.writes.notes).encode("utf-8")
        router = stage / "ROUTER.md"
        refreshed = router.read_bytes() != wanted
        if refreshed:
            temp = stage / ".ROUTER.md.tmp"
            temp.write_bytes(wanted)
            os.replace(temp, router)
        problems = store.check_instance(stage)
        follow_up: list[str] = []
        if migrated_from is not None and _ROUTER_NOTE in problems:
            problems.remove(_ROUTER_NOTE)
            follow_up.append("brain apply-config")
        if problems:
            raise _refuse("brain check found problems: " + "; ".join(problems[:3]))
        con = store.connect_readonly(stage / "brain.sqlite3")
        try:
            seq, _count, last, _digest = log_digest(con)
        finally:
            con.close()
        cursors = sorted(path.name for path in (stage / "fetch").glob("*.state.json")) if (stage / "fetch").is_dir() else []
        try:
            if target.exists():
                os.rmdir(target)
            os.replace(stage, target)
        except OSError as exc:
            raise Unavailable(f"cannot place the restored directory: {exc}") from exc
        done = True
        return {
            "status": "restored",
            "dir": str(target),
            "schema": migrate.latest_version(),
            "applied_seq": seq,
            "last_decided_at": last,
            "migrated_from": migrated_from,
            "follow_up": follow_up,
            "router_refreshed": refreshed,
            "fetch_cursors": len(cursors),
        }
    finally:
        if not done:
            shutil.rmtree(stage, ignore_errors=True)
