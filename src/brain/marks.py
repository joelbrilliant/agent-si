"""The export mark: an HMAC that lets brain-fetch, and only brain-fetch, claim
that an item is the principal's own. The key is fetch/mark.key under the instance. A hand-written
export has no valid mark. The honest limit is in the README: an agent that can read the key can sign."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
from pathlib import Path

from brain.store import canonical_json

_HEX64 = re.compile(r"[0-9a-f]{64}")


def key_path(instance) -> Path:
    return Path(instance) / "fetch" / "mark.key"


def ensure_key(instance) -> str:
    """The key as 64 hex digits, created (O_EXCL, mode 0600, directory 0700) when it is missing."""
    path = key_path(instance)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return path.read_text(encoding="ascii").strip()
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(key + "\n")
    return key


def message(doc: dict) -> str:
    principal = sorted(
        (item["ref"], hashlib.sha256(canonical_json(item).encode()).hexdigest())
        for item in doc.get("items", ())
        if item.get("from_principal") is True
    )
    return canonical_json(
        {
            "origin": doc.get("origin"),
            "fetched_at": doc.get("fetched_at"),
            "principal": [list(pair) for pair in principal],
        }
    )


def sign(key_hex: str, doc: dict) -> str:
    return hmac.new(bytes.fromhex(key_hex), message(doc).encode(), hashlib.sha256).hexdigest()


def _key_ok(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) & 0o177 == 0
    )


def verify(instance, doc: dict) -> bool:
    mark = doc.get("mark")
    if not isinstance(mark, str) or _HEX64.fullmatch(mark) is None:
        return False
    path = key_path(instance)
    if not _key_ok(path):
        return False
    try:
        key = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return False
    if _HEX64.fullmatch(key) is None:
        return False
    return hmac.compare_digest(sign(key, doc), mark)
