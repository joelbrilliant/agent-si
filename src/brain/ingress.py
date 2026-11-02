"""The one door for JSON and bytes that come from outside the engine.

A packet, a candidate, a fixture, a request line, a hub body, a manifest or an export is written by someone else, so
it can be deep, huge, endless or malformed. The same crash was fixed at three entry points one after another
(apply, then serve and read --args, then collect), and `brain extract` hung on /dev/zero. Every entry point now
calls `loads` or `read_bytes` here, and gets one answer: the parsed value, or an IngressError that says what was
wrong and the limit. tests/test_ingress_only.py fails on a `json.loads` anywhere else, unless the line says
`# trusted: <what wrote it>` because the engine wrote that JSON itself (a database column, its own state file).

`loads` refuses: more than `max_nesting` levels of brackets, text that isn't UTF-8 JSON, NaN and Infinity, an
integer outside 64 bits or a float too large to be finite (SQLite can't store them), and a lone surrogate (no UTF-8
encoder accepts one). `read_bytes` never reads past `limit` bytes, so /dev/zero or a pipe that never ends can't
hang or fill memory.
"""
from __future__ import annotations

import json
import math
import os
import re

from brain import advice

SIZE, NESTING, NUMBER, SURROGATE, ENCODING, SYNTAX = "size", "nesting", "number", "surrogate", "encoding", "syntax"

MAX_NESTING = 1000  # a fixture, a manifest, a request: well inside Python's own recursion limit
PACKET_NESTING = 64  # a packet or a candidate
MAX_PACKET_BYTES = 5 * 1024 * 1024
MAX_CANDIDATE_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 1024 * 1024  # one MCP request line, or the --args of a read
MAX_DOCUMENT_BYTES = 32 * 1024 * 1024  # a fixture, or another tool's config file

_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1
_INT_DIGITS = len(str(_INT_MIN))  # 20 characters, sign included

SURROGATE_ESCAPE = re.compile(r"\\u[dD][89a-fA-F][0-9a-fA-F]{2}")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")
_JSON_STRING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"?', re.DOTALL)
_NOT_BRACKET = re.compile(r"[^\[\]{}]+")


class IngressError(ValueError):
    """Outside input was refused. `kind` is size, nesting, number, surrogate, encoding or syntax; the message is
    the subject (`what`) and the issue, such as "packet nests deeper than 64 levels"."""

    def __init__(self, kind: str, what: str, limit: int | None = None):
        self.kind, self.what, self.limit = kind, what, limit
        self.issue = advice.ingress_issue(kind, limit)
        super().__init__(f"{what} {self.issue}")

    @property
    def malformed(self) -> bool:
        """Not JSON at all (bad bytes or bad syntax), as opposed to JSON that is too big, deep or odd."""
        return self.kind in (ENCODING, SYNTAX)


class _OutOfRange(ValueError):
    pass


def _ranged_int(value: str) -> int:
    if len(value) > _INT_DIGITS:  # also keeps int() clear of Python's digit limit
        raise _OutOfRange(value[:40])
    number = int(value)
    if not _INT_MIN <= number <= _INT_MAX:
        raise _OutOfRange(value)
    return number


def _finite_float(value: str) -> float:
    number = float(value)
    if math.isinf(number):
        raise _OutOfRange(value[:40])
    return number


def _no_constant(value: str):
    raise ValueError(value)


def nesting_depth(text: str, limit: int | None = None) -> int:
    """The deepest bracket nesting in JSON text, ignoring brackets inside strings.
    With a limit, the scan stops as soon as the depth passes it."""
    brackets = _NOT_BRACKET.sub("", _JSON_STRING.sub("", text))
    depth = deepest = 0
    for char in brackets:
        if char in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
                if limit is not None and deepest > limit:
                    return deepest
        elif depth:
            depth -= 1
    return deepest


def has_lone_surrogate(doc) -> bool:
    """Whether any string (key or value) in a parsed JSON document holds a lone surrogate.
    JSON can carry one as a \\uD800-style escape, and no UTF-8 encoder accepts it."""
    stack = [doc]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            if _LONE_SURROGATE.search(value):
                return True
        elif isinstance(value, dict):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return False


def check_size(raw, limit: int, what: str = "input") -> None:
    """Raise IngressError if `raw` is over `limit` bytes."""
    if len(raw) > limit:
        raise IngressError(SIZE, what, limit)


def loads(raw: bytes | str, *, what: str = "input", max_nesting: int = MAX_NESTING, max_bytes: int | None = None):
    """Parse JSON from outside, or raise IngressError. `what` names the input in the message."""
    if max_bytes is not None:
        check_size(raw, max_bytes, what)
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise IngressError(ENCODING, what) from None
    if nesting_depth(text, max_nesting) > max_nesting:
        raise IngressError(NESTING, what, max_nesting)
    try:
        doc = json.loads(text, parse_constant=_no_constant, parse_float=_finite_float, parse_int=_ranged_int)
    except _OutOfRange:
        raise IngressError(NUMBER, what) from None
    except RecursionError:
        raise IngressError(NESTING, what, max_nesting) from None
    except ValueError:
        raise IngressError(SYNTAX, what) from None
    if (SURROGATE_ESCAPE.search(text) or _LONE_SURROGATE.search(text)) and has_lone_surrogate(doc):
        raise IngressError(SURROGATE, what)
    return doc


def read_bytes(source, limit: int, what: str = "input") -> bytes:
    """Up to `limit` bytes from a path or a binary stream; more raises IngressError. It reads at most limit + 1
    bytes, so /dev/zero or a stream that never ends is refused rather than waited on. A path that can't be opened
    raises OSError, as open does."""
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as handle:
            data = handle.read(limit + 1)
    else:
        data = source.read(limit + 1)
    check_size(data, limit, what)
    return data


def load_file(path, *, limit: int, what: str = "input", max_nesting: int = MAX_NESTING):
    """read_bytes, then loads."""
    return loads(read_bytes(path, limit, what), what=what, max_nesting=max_nesting)
