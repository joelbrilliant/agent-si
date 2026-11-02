"""The conversation privacy scrub.

Email addresses become [email] and phone numbers [phone], by fixed patterns, before a capture is hashed. The
patterns are a backstop, not a guarantee: their miss rate on unusual formats is measured, not assumed. The scrub is
idempotent, so a file brain-fetch already scrubbed hashes the same when the collector scrubs it again.
"""
from __future__ import annotations

import re

EMAIL_TOKEN = "[email]"
PHONE_TOKEN = "[phone]"

# A match may only start where a local part starts, so each run of word characters is tried once, not once per
# character: without it a long unbroken word costs quadratic time.
_START = r"(?<![\w.%+'\-\"])"
_LOCAL = r"[\w.%+'\-]+"
_LABEL = r"[\w\-]+"
_TLD = r"[^\W\d_]{2,24}"
_AT = r"(?:\s*(?:\(at\)|\[at\]|\{at\}|<at>)\s*)"
_DOT = r"(?:\s*(?:\(dot\)|\[dot\]|\{dot\}|<dot>|\[\.\]|\(\.\))\s*)"
_WORD_AT = r"(?:\s+at\s+)"
_WORD_DOT = r"(?:\s+dot\s+)"

_EMAILS = (
    # alex@example.com, alex.o'neil+tag@mail.example.co.uk, and the same with spaces around @ and the dots.
    re.compile(rf"{_START}(?:\"[^\"\n@]{{1,64}}\"|{_LOCAL})\s*@\s*{_LABEL}(?:\s*\.\s*{_LABEL})*\s*\.\s*{_TLD}\b"),
    # alex(at)example(dot)com, alex [at] example.com, alex@example[.]com
    re.compile(rf"{_START}{_LOCAL}(?:{_AT}|\s*@\s*){_LABEL}(?:(?:{_DOT}|\.){_LABEL})*(?:{_DOT}|\.){_TLD}\b", re.IGNORECASE),
    # alex at example dot com: the plain words need a spelled-out dot before the last part.
    re.compile(rf"{_START}{_LOCAL}{_WORD_AT}{_LABEL}(?:(?:{_WORD_DOT}|\.){_LABEL})*{_WORD_DOT}{_TLD}\b", re.IGNORECASE),
)

# A candidate is a run of digits joined by spaces, tabs, brackets, dots or dashes, optionally after +. It can't
# start inside a word, a number, a price or a sign, and can't end in front of a letter, a percent or more decimals.
_PHONE_RUN = re.compile(r"(?<![\w$€£¥.+\-/:])\+?\(?\d(?:[ \t().\-]{0,3}\d)+(?![\w%/:]|\.\d)")
_DATES = (
    re.compile(r"\d{4}[ .\-]\d{1,2}[ .\-]\d{1,2}"),
    re.compile(r"\d{1,2}[ .\-]\d{1,2}[ .\-]\d{4}"),
    re.compile(r"(?:19|20)\d\d\s*-\s*(?:19|20)\d\d"),
)


def _is_phone(run: str) -> bool:
    digits = sum(char.isdigit() for char in run)
    if not 8 <= digits <= 15:
        return False
    plus = run.startswith("+")
    separators = {char for char in run if not char.isdigit() and char != "+"}
    if not separators:
        # One unbroken run is an id or an amount unless it is international or starts with a trunk 0.
        return plus or (run.startswith("0") and digits <= 12)
    if not plus and any(pattern.fullmatch(run) for pattern in _DATES):
        return False
    if separators == {"."} and not plus:
        groups = run.split(".")
        if len(groups) == 2:
            return False  # a decimal
        if len(groups) == 4 and all(len(group) <= 3 for group in groups):
            return False  # an IPv4 address
    return True


def _phone(match: re.Match) -> str:
    run = match.group(0)
    return PHONE_TOKEN if _is_phone(run) else run


def scrub(text: str) -> str:
    """Addresses first, so a number inside an address goes with it; then phone numbers."""
    for pattern in _EMAILS:
        text = pattern.sub(EMAIL_TOKEN, text)
    return _PHONE_RUN.sub(_phone, text)
