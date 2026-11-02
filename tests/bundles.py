"""Helpers for the backup and restore tests: read a bundle apart and build hostile ones. Invented data only."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

from brain import store
from brain.cli import main


def cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def backup_cli(instance: Path, out: Path, *extra: str) -> tuple[int, dict | None, str]:
    code, stdout, stderr = cli("backup", "--instance", str(instance), "--out", str(out), *extra)
    return code, (json.loads(stdout) if stdout.strip() else None), stderr


def restore_cli(bundle: Path, target: Path) -> tuple[int, dict | None, str]:
    code, stdout, stderr = cli("restore", str(bundle), "--dir", str(target))
    return code, (json.loads(stdout) if stdout.strip() else None), stderr


def read_bundle(path: Path) -> tuple[dict, dict[str, bytes]]:
    """(manifest, members other than the manifest)."""
    members: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            members[member.name] = tar.extractfile(member).read()
    manifest = json.loads(members.pop("manifest.json"))
    return manifest, members


def manifest_for(base: dict, members: dict[str, bytes]) -> dict:
    manifest = dict(base)
    manifest["members"] = {
        name: {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)} for name, blob in sorted(members.items())
    }
    return manifest


def write_bundle(path: Path, entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> Path:
    path = Path(path)
    with open(path, "wb") as handle:
        with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.USTAR_FORMAT) as tar:
                for entry, blob in entries:
                    tar.addfile(entry, None if blob is None else io.BytesIO(blob))
    return path


def info(name: str, size: int = 0, kind: bytes = tarfile.REGTYPE, linkname: str = "") -> tarfile.TarInfo:
    entry = tarfile.TarInfo(name)
    entry.size = size
    entry.type = kind
    entry.mode = 0o600
    entry.linkname = linkname
    return entry


def rebuilt(path: Path, source: Path, *, members=None, manifest=None, drop=(), keep_manifest: bool = False) -> Path:
    """A new bundle from source's parts. members maps name to new bytes (None drops it); the manifest is
    recomputed to match unless keep_manifest, and manifest maps keys to override."""
    base, parts = read_bundle(source)
    for name, blob in (members or {}).items():
        if blob is None:
            parts.pop(name, None)
        else:
            parts[name] = blob
    for name in drop:
        parts.pop(name, None)
    new = base if keep_manifest else manifest_for(base, parts)
    new = {**new, **(manifest or {})}
    raw = store.canonical_json(new).encode()
    entries = [(info("manifest.json", len(raw)), raw)] + [(info(name, len(blob)), blob) for name, blob in sorted(parts.items())]
    return write_bundle(path, entries)


def digest_of(rows) -> str:
    """The write_log digest, worked out from rows (change_id, agent, status, decided_at,
    applied_seq, reviewed_by, reviewed_at, payload_sha256)."""
    text = ""
    for row in sorted(rows):
        change_id, agent, status, decided_at, seq, by, at, payload = row
        text += "|".join([change_id, agent, status, decided_at, "" if seq is None else str(seq), by, "" if at is None else at, payload]) + "\n"
    return hashlib.sha256(text.encode()).hexdigest()
