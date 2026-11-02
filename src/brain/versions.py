"""The PEP 440 subset agent-si releases use: X.Y.Z with an optional aN, bN or rcN.
Anything else is ignored, never guessed at."""
from __future__ import annotations

import re

_VERSION = re.compile(r"(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,4})\.(0|[1-9][0-9]{0,5})(?:(a|b|rc)(0|[1-9][0-9]{0,3}))?")
_PRE = {"a": 0, "b": 1, "rc": 2}


def parse(text) -> tuple | None:
    """(major, minor, patch, pre) with pre None for a final release, or (rank, number) for a pre-release."""
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    major, minor, patch, kind, number = match.groups()
    return int(major), int(minor), int(patch), (_PRE[kind], int(number)) if kind else None


def key(text: str) -> tuple:
    """A sort key: a final release sorts after its pre-releases."""
    major, minor, patch, pre = parse(text)
    return major, minor, patch, pre if pre is not None else (9, 0)


def newer(left: str, right: str) -> bool:
    return parse(left) is not None and parse(right) is not None and key(left) > key(right)


def is_pre(text: str) -> bool:
    parsed = parse(text)
    return parsed is not None and parsed[3] is not None


def from_wheel(name: str) -> str | None:
    """The version in a wheel's file name, agent_brain-0.8.1-py3-none-any.whl, or None."""
    parts = name.split("-")
    return parts[1] if len(parts) >= 5 and name.endswith(".whl") and parse(parts[1]) else None
