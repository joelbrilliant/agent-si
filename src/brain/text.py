"""One line of text: what "one line" means, decided in one place (class 8).

Free text reached one-line outputs (the brief, a refusal, a config line) with a different cleaning at each
place: `org` refused only \\n and \\r though str.splitlines() also splits on U+2028, U+2029, U+0085, \\v, \\f and
\\x1c to \\x1e; a blank value got through; a cut split a combining sequence or left "....". Every one-line field
now goes through `single_line`, and tests/test_one_line.py fails on hand-rolled cleaning anywhere else in the
engine. Where text is only matched or normalised, not shown on one line, end the line with
`# not one-line: <reason>`.

Two modes:

- Clean (the default) returns a line. Every line break, every whitespace character and every non-printable
  character becomes a space, runs of spaces collapse and the ends are stripped. The one exception is a joiner
  (a zero width joiner, a non-joiner or an emoji tag character), which stays so that an emoji family or a flag is
  not torn apart.
- Refuse (`refuse=True`) raises NotOneLine for any of those characters instead of replacing it, so config and
  options are not silently rewritten. Plain space runs and ends are still tidied.

`max_chars` and `max_bytes` (UTF-8) are different budgets and both include the `ellipsis`. A cut happens at a
grapheme boundary: it never splits a combining sequence, a joiner sequence, a Hangul syllable or a flag pair, and
never leaves a full stop before an ellipsis that starts with one. `required=True` refuses a result that is blank.
"""
from __future__ import annotations

import unicodedata

from brain import advice

ELLIPSIS = "..."


class NotOneLine(ValueError):
    """Text that can't be one line, or is blank where a value is required. str(exc) names the fix."""


# Characters that stay in a line although they are not printable: they join the characters around them.
_TAGS = frozenset(chr(code) for code in range(0xE0020, 0xE0080))  # emoji tag characters
_JOINERS = frozenset({chr(0x200C), chr(0x200D)}) | _TAGS  # a non-joiner, a joiner
# Characters that belong to the grapheme before them, so a cut never lands in front of them.
_EXTENDERS = frozenset(
    {
        chr(0x200D),  # zero width joiner
        chr(0xFE0E),  # variation selectors 15 and 16
        chr(0xFE0F),
        *(chr(code) for code in range(0x1F3FB, 0x1F400)),  # skin-tone modifiers
        *_TAGS,
        *(chr(code) for code in range(0x1160, 0x1200)),  # conjoining Hangul vowels and finals
        *(chr(code) for code in range(0xD7B0, 0xD800)),
    }
)
_REGIONAL = "".join(chr(code) for code in range(0x1F1E6, 0x1F200))


def has_line_break(value: str) -> bool:
    """True when value holds any character str.splitlines() splits on (\\n, \\r, \\v, \\f, \\x1c to \\x1e, U+0085,
    U+2028, U+2029). Python's own answer, so no list here can fall behind it."""
    return len(("x" + value + "x").splitlines()) != 1


def _shown(ch: str) -> bool:
    """A character that may stay in a line: printable (an ASCII space is) or a joiner."""
    return ch.isprintable() or ch in _JOINERS


def single_line(
    value,
    *,
    max_chars: int | None = None,
    max_bytes: int | None = None,
    ellipsis: str = "",
    refuse: bool = False,
    required: bool = False,
) -> str:
    """value as one line. See the module text for the two modes and the two budgets."""
    text = "" if value is None else str(value)
    if refuse and (max_chars is not None or max_bytes is not None):
        raise TypeError("refuse=True checks a value as it is; leave out max_chars and max_bytes")
    if refuse:
        bad = next((ch for ch in text if not _shown(ch)), None)
        if bad is not None:
            raise NotOneLine(advice.one_line_error(bad))
    if not text.isprintable():
        text = "".join(ch if _shown(ch) else " " for ch in text)
    text = " ".join(text.split())
    if required and not text:
        raise NotOneLine(advice.blank_line_error())
    if max_chars is None and max_bytes is None:
        return text
    if (max_chars is None or len(text) <= max_chars) and (max_bytes is None or len(text.encode()) <= max_bytes):
        return text
    room_chars = None if max_chars is None else max(0, max_chars - len(ellipsis))
    room_bytes = None if max_bytes is None else max(0, max_bytes - len(ellipsis.encode()))
    kept = used = 0
    for ch in text:
        size = len(ch.encode())
        if (room_chars is not None and kept >= room_chars) or (room_bytes is not None and used + size > room_bytes):
            break
        used += size
        kept += 1
    head = text[: _grapheme_cut(text, kept)]
    if ellipsis.startswith("."):
        head = head.rstrip(".")
    return head + ellipsis


def _HANGUL_LEADING(ch: str) -> bool:
    """A conjoining Hangul leading consonant, which binds to a following one (L L V)."""
    return chr(0x1100) <= ch <= chr(0x115F) or chr(0xA960) <= ch <= chr(0xA97F)


def _extends(ch: str) -> bool:
    """True when ch belongs to the grapheme of the character before it."""
    return unicodedata.category(ch) in {"Mn", "Mc", "Me"} or ch in _EXTENDERS


def _grapheme_cut(text: str, k: int) -> int:
    """The cut point at or before k that splits no grapheme. k is below len(text)."""
    while True:
        start = k
        # A joiner or a virama before the cut binds the next character to it, as in a ZWJ emoji or
        # an Indic conjunct, so the cut moves back past both.
        while k > 0 and (
            _extends(text[k])
            or text[k - 1] == chr(0x200D)
            or (unicodedata.combining(text[k - 1]) == 9 and unicodedata.category(text[k]).startswith("L"))
            or (_HANGUL_LEADING(text[k - 1]) and _HANGUL_LEADING(text[k]))
        ):
            k -= 1
        # An odd run of regional indicators before the cut means it split a flag pair.
        if (k - len(text[:k].rstrip(_REGIONAL))) % 2 == 1:
            k -= 1
        if k == start:
            return k
