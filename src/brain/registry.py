"""The machine registry and machine state.

$XDG_CONFIG_HOME/agent-si/instances.toml lists every instance on this machine, so brain update migrates them
all. $XDG_STATE_HOME/agent-si/ holds update.lock, update.json and update.log. New machine paths use the public
name: no earlier release used another name for them.
"""
from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

from brain import ingress, store

PUBLIC_NAME = "agent-si"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / PUBLIC_NAME


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / PUBLIC_NAME


def registry_path() -> Path:
    return config_dir() / "instances.toml"


def instances() -> list[Path]:
    """Every registered instance directory, in file order, without duplicates. A missing or unreadable file
    is an empty registry; brain update says so rather than guessing."""
    try:
        data = tomllib.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    found: list[Path] = []
    for entry in data.get("instances") or []:
        path = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(path, str) and path.startswith("/") and Path(path) not in found:
            found.append(Path(path))
    return found


def add(instance) -> bool:
    """Register an instance by absolute path. False when it was already there."""
    path = Path(instance).resolve()
    known = instances()
    if path in known:
        return False
    known.append(path)
    target = registry_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "# Every agent-si instance on this machine; brain update migrates each one.\n" + "".join(
        f'\n[[instances]]\npath = {json.dumps(str(item))}\n' for item in known)
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(body, encoding="utf-8")
    os.replace(temp, target)
    return True


def read_state() -> dict:
    """update.json, or {} when it's missing or unreadable."""
    try:
        data = ingress.load_file(state_dir() / "update.json", limit=ingress.MAX_REQUEST_BYTES, what="update.json")
    except (OSError, ingress.IngressError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(changes: dict) -> dict:
    data = {**read_state(), **changes}
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / f".update.json.{os.getpid()}.tmp"
    temp.write_text(store.canonical_json(data) + "\n", encoding="utf-8")
    os.replace(temp, folder / "update.json")
    return data


def log(line: str) -> None:
    folder = state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "update.log", "a", encoding="utf-8") as handle:
        handle.write(f"{store.utc_now()} {line}\n")
