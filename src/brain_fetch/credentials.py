"""Where a collector's credential lives: /run/secrets/<collector>, else the macOS
keychain. The value is never printed, logged or put in an exception message."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

SECRETS_DIR = Path("/run/secrets")
SECURITY = "/usr/bin/security"
_MAX_BYTES = 4096


class SecretFileError(Exception):
    pass


def keychain_service(instance_name: str, collector: str) -> str:
    return f"agent-si/{instance_name}/{collector}"


def store_command(instance_name: str, collector: str) -> str:
    """The command that stores a secret; the value is left to the prompt, so it never reaches ps or history."""
    return f"security add-generic-password -U -s {keychain_service(instance_name, collector)} -a {collector} -w"


def _from_file(path: Path, collector: str) -> str | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()):
        return None
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SecretFileError(f"secret file {collector} must not be writable by group or other")
    if info.st_size > _MAX_BYTES:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.strip().splitlines()
    return lines[0].strip() if lines and lines[0].strip() else None


def _from_keychain(instance_name: str, collector: str, security: str) -> str | None:
    try:
        done = subprocess.run(
            [security, "find-generic-password", "-s", keychain_service(instance_name, collector),
             "-a", collector, "-w"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
            check=False,
            env={"PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    return lines[0] if lines and lines[0] else None


def read_secret(instance_name: str, collector: str, *, secrets_dir=None, security=None) -> str | None:
    """The credential, or None. The defaults are SECRETS_DIR and SECURITY, looked up when called so a test
    replaces them in one place."""
    found = _from_file(Path(secrets_dir or SECRETS_DIR) / collector, collector)
    if found is not None:
        return found
    if sys.platform == "darwin":
        return _from_keychain(instance_name, collector, security or SECURITY)
    return None


def where_it_looked(instance_name: str, collector: str, *, secrets_dir=None) -> str:
    places = [str(Path(secrets_dir or SECRETS_DIR) / collector)]
    if sys.platform == "darwin":
        places.append(f"keychain {keychain_service(instance_name, collector)}")
    return ", ".join(places)
