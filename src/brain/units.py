"""Units: parsing a claim's value_raw, rendering a stored value, and canonical values.

Standard library only. A key's unit is its canonical unit. Conversion is exact in Decimal, and
only the result becomes a float, so equal amounts always agree.
"""
from __future__ import annotations

import calendar
import decimal
import re
from datetime import date, timedelta
from decimal import Decimal

from brain.evidence_gate import match_key

_INT_MAX = 2**63 - 1

# ASCII digits only. \s stays Unicode-aware, so a no-break space still separates the unit.
_NUMBER = re.compile(
    r"^\s*(?P<pre>[^0-9\s+-]+)?\s*(?P<num>[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)"
    r"(?P<suf>[kKM](?![A-Za-z]))?\s*(?P<rest>.*?)\s*$"
)
_SUFFIX = {"k": Decimal(1000), "K": Decimal(1000), "M": Decimal(1_000_000)}


def _names(pairs: dict[tuple[str, ...], Decimal]) -> dict[str, Decimal]:
    table: dict[str, Decimal] = {}
    for names, factor in pairs.items():
        for name in names:
            table[name] = factor
    return table


BYTES = _names(
    {
        ("b", "byte", "bytes"): Decimal(1),
        ("kb", "kilobyte", "kilobytes"): Decimal(10) ** 3,
        ("mb", "megabyte", "megabytes"): Decimal(10) ** 6,
        ("gb", "gigabyte", "gigabytes"): Decimal(10) ** 9,
        ("tb", "terabyte", "terabytes"): Decimal(10) ** 12,
        ("kib", "kibibyte", "kibibytes"): Decimal(2) ** 10,
        ("mib", "mebibyte", "mebibytes"): Decimal(2) ** 20,
        ("gib", "gibibyte", "gibibytes"): Decimal(2) ** 30,
        ("tib", "tebibyte", "tebibytes"): Decimal(2) ** 40,
    }
)
TIME = _names(
    {
        ("ms", "millisecond", "milliseconds"): Decimal(1) / Decimal(1000),
        ("s", "sec", "second", "seconds"): Decimal(1),
        ("min", "minute", "minutes"): Decimal(60),
        ("h", "hr", "hour", "hours"): Decimal(3600),
        ("day", "days"): Decimal(86400),
    }
)
PERCENT = {"%": Decimal(1), "percent": Decimal(1)}
CURRENCIES = {
    "AUD": ("A$", "AU$", "$"),
    "USD": ("US$", "$"),
    "NZD": ("NZ$", "$"),
    "CAD": ("C$", "CA$", "$"),
    "EUR": ("€",),
    "GBP": ("£",),
    "JPY": ("¥",),
}
_FAMILIES = (("bytes", BYTES), ("time", TIME), ("percent", PERCENT))


class UnitError(ValueError):
    """A value_raw that doesn't parse. The message is the tail after `op <l> (<op>): `."""

    def __init__(self, tail: str) -> None:
        super().__init__(tail)
        self.tail = tail


def family(unit: str) -> str:
    """The family of a key's unit: bytes, time, percent, currency, count or label."""
    if unit == "":
        return "count"
    lowered = unit.lower()
    for name, table in _FAMILIES:
        if lowered in table:
            return name
    if unit.upper() in CURRENCIES:
        return "currency"
    return "label"


def _context() -> decimal.Context:
    return decimal.Context(prec=1000)


def _number(value_type: str, unit: str, raw: str) -> dict:
    match = _NUMBER.fullmatch(raw)
    if match is None:
        raise UnitError(f"value {raw} is not a number")
    pre = match.group("pre") or ""
    suf = match.group("suf") or ""
    rest = match.group("rest") or ""
    kind = family(unit)
    with decimal.localcontext(_context()):
        amount = Decimal(match.group("num").replace(",", ""))
        if kind == "count":
            if pre:
                raise UnitError(f"value {raw} uses {pre}, but this key takes a bare number")
            if rest:
                raise UnitError(f"value {raw} uses {rest}, but this key takes a bare number")
            if suf:
                amount *= _SUFFIX[suf]
        elif kind == "label":
            if pre:
                raise UnitError(f"value {raw} uses {pre}, which doesn't convert to {unit}")
            if rest and rest.lower() != unit.lower():
                raise UnitError(f"value {raw} uses {rest}, which doesn't convert to {unit}")
            if suf:
                amount *= _SUFFIX[suf]
        elif kind == "currency":
            code = unit.upper()
            allowed = {code.lower()}
            symbols = CURRENCIES[code]
            if pre and pre.lower() not in allowed and pre not in symbols:
                raise UnitError(f"value {raw} uses {pre}, which doesn't convert to {unit}")
            if suf:
                raise UnitError(f"value {raw} uses {suf}, which doesn't convert to {unit}")
            if rest and rest.lower() not in allowed and rest not in symbols:
                raise UnitError(f"value {raw} uses {rest}, which doesn't convert to {unit}")
        else:
            table = dict(_FAMILIES)[kind]
            if pre:
                raise UnitError(f"value {raw} uses {pre}, which doesn't convert to {unit}")
            if suf:
                raise UnitError(f"value {raw} uses {suf}, which doesn't convert to {unit}")
            if rest and rest.lower() not in table:
                raise UnitError(f"value {raw} uses {rest}, which doesn't convert to {unit}")
            factor = table[rest.lower()] if rest else table[unit.lower()]
            amount = amount * factor / table[unit.lower()]
        empty = {"value_text": None, "value_int": None, "value_real": None, "value_date": None}
        if value_type == "int":
            if amount != amount.to_integral_value():
                raise UnitError(f"value {raw} is not a whole number")
            whole = int(amount)
            if not -_INT_MAX <= whole <= _INT_MAX:
                raise UnitError(f"value {raw} is outside the supported range")
            return {**empty, "value_int": whole}
        try:
            real = float(amount)
        except OverflowError:
            raise UnitError(f"value {raw} is outside the supported range") from None
        if real in (float("inf"), float("-inf")) or real != real:
            raise UnitError(f"value {raw} is outside the supported range")
        if real == 0 and amount != 0:
            # Below the float range: it would agree with 0.
            raise UnitError(f"value {raw} is outside the supported range")
        return {**empty, "value_real": real}


def words_of(value_words) -> dict[str, tuple[str, ...]]:
    """The usable entries of a key's value_words: each value mapped to a list of strings. Anything
    else, such as a malformed stored object, is ignored, so it can never make a parse raise."""
    if not isinstance(value_words, dict):
        return {}
    found: dict[str, tuple[str, ...]] = {}
    for value, items in value_words.items():
        if isinstance(value, str) and isinstance(items, list) and all(isinstance(item, str) for item in items):
            found[value] = tuple(items)
    return found


def _by_word(raw: str, candidates, value_words) -> list[str]:
    """The values among candidates one of whose value words has raw's match_key."""
    key = match_key(raw)
    if not key:
        return []
    words = words_of(value_words)
    return [
        value for value in candidates if any(match_key(word) == key for word in words.get(value, ()))
    ]


def as_date(value) -> date | None:
    """A source date as a date: a date stays one, an ISO string is read, anything else is None."""
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.isoformat() == value else None
    return None


DAY_SHIFTS = {"today": 0, "yesterday": -1, "tomorrow": 1}


def parse(
    value_type: str, unit: str, allowed_values, raw: str, *, value_words=None, source_date=None
) -> dict:
    """The value columns for value_raw, or UnitError with the refusal's tail.

    value_words (a key's stored value words) let a bool or enum raw be one of its words, and
    source_date lets a date raw be a day word or one date written in words."""
    empty = {"value_text": None, "value_int": None, "value_real": None, "value_date": None}
    if value_type in ("int", "number"):
        return _number(value_type, unit or "", raw)
    if value_type == "text":
        if not raw.strip():
            raise UnitError("value is blank")
        return {**empty, "value_text": raw}
    if value_type == "enum":
        allowed = list(allowed_values or [])
        if raw in allowed:
            return {**empty, "value_text": raw}
        key = match_key(raw)
        found = [item for item in allowed if match_key(item) == key]
        if len(found) == 1:
            return {**empty, "value_text": found[0]}
        worded = _by_word(raw, allowed, value_words)
        if len(worded) == 1:
            return {**empty, "value_text": worded[0]}
        raise UnitError(f"value {raw} is not one of the allowed values")
    if value_type == "bool":
        key = match_key(raw)
        if key in ("true", "yes"):
            return {**empty, "value_int": 1}
        if key in ("false", "no"):
            return {**empty, "value_int": 0}
        worded = _by_word(raw, ("true", "false"), value_words)
        if len(worded) == 1:
            return {**empty, "value_int": 1 if worded[0] == "true" else 0}
        raise UnitError(f"value {raw} is not true or false")
    if value_type == "date":
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None and parsed.isoformat() == raw:
            return {**empty, "value_date": raw}
        anchor = as_date(source_date)
        if anchor is not None:
            shift = DAY_SHIFTS.get(match_key(raw))
            moved = None if shift is None else shifted(anchor, shift)
            if moved is not None:
                return {**empty, "value_date": moved.isoformat()}
            written = date_in_words(raw, anchor)
            if written is not None:
                return {**empty, "value_date": written.isoformat()}
        raise UnitError(f"value {raw} is not an ISO date")
    if value_type == "url":
        ok = False
        for scheme in ("http://", "https://"):
            if raw.startswith(scheme) and len(raw) > len(scheme):
                ok = True
        if not ok or any(ch.isspace() for ch in raw):
            raise UnitError(f"value {raw} is not an http(s) URL")
        return {**empty, "value_text": raw}
    raise UnitError(f"value {raw} has an unsupported value_type")


def canonical_number(value: float) -> str:
    if value == 0:
        return "0"
    return format(Decimal(repr(float(value))).normalize(), "f")


def render(value_type: str, value) -> str:
    """value_raw for an upsert_fact claim, and the text a brief line shows."""
    if value_type == "int":
        return str(int(value))
    if value_type == "number":
        return canonical_number(value)
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


def typed(value_type: str, row) -> str | int | float | bool:
    """The typed value of a row's value columns."""
    if value_type == "int":
        return int(row["value_int"])
    if value_type == "bool":
        return bool(row["value_int"])
    if value_type == "number":
        return float(row["value_real"])
    if value_type == "date":
        return row["value_date"]
    return row["value_text"]


def canonical(value_type: str, row) -> str:
    """The canonical value of a row's value columns. Computed on read, never stored."""
    if value_type == "text":
        return match_key(row["value_text"])
    if value_type in ("enum", "url"):
        return row["value_text"]
    if value_type == "date":
        return row["value_date"]
    if value_type == "bool":
        return "true" if row["value_int"] else "false"
    if value_type == "int":
        return str(int(row["value_int"]))
    if value_type == "number":
        return canonical_number(row["value_real"])
    return row["value_text"]


# -- Dates written in words ------------------------------------------------

_WEEKDAYS = (
    ("monday", 0), ("mon", 0),
    ("tuesday", 1), ("tues", 1), ("tue", 1),
    ("wednesday", 2), ("wed", 2),
    ("thursday", 3), ("thurs", 3), ("thur", 3), ("thu", 3),
    ("friday", 4), ("fri", 4),
    ("saturday", 5), ("sat", 5),
    ("sunday", 6), ("sun", 6),
)
_MONTHS = (
    ("january", 1), ("jan", 1), ("february", 2), ("feb", 2), ("march", 3), ("mar", 3),
    ("april", 4), ("apr", 4), ("may", 5), ("june", 6), ("jun", 6), ("july", 7), ("jul", 7),
    ("august", 8), ("aug", 8), ("september", 9), ("sept", 9), ("sep", 9), ("october", 10), ("oct", 10),
    ("november", 11), ("nov", 11), ("december", 12), ("dec", 12),
)
_WEEKDAY_OF = dict(_WEEKDAYS)
_MONTH_OF = dict(_MONTHS)
# Longer alternatives come first in each list, so sept is tried before sep and thurs before thu.
_WEEKDAY_RE = "|".join(name for name, _ in _WEEKDAYS)
_MONTH_RE = "|".join(name for name, _ in _MONTHS)
_DAY_RE = r"[0-9]{1,2}(?:st|nd|rd|th)?"
# The text is matched with only ASCII letters lowered (_ASCII_LOWER), so a non-ASCII letter never
# reads as part of a month or a weekday, and positions stay the same. \s stays Unicode-aware.
_STATED = re.compile(
    r"(?<![a-z0-9])(?:"
    rf"(?:(?P<wd>{_WEEKDAY_RE})\.?,?\s+)?"
    rf"(?:(?P<d1>{_DAY_RE})(?:\s+of)?\s+(?P<m1>{_MONTH_RE})\.?(?:,?\s+(?P<y1>[0-9]{{4}}))?"
    rf"|(?P<m2>{_MONTH_RE})\.?\s+(?P<d2>{_DAY_RE})(?:,?\s+(?P<y2>[0-9]{{4}}))?)"
    r"|(?P<nd>[0-9]{1,2})/(?P<nm>[0-9]{1,2})/(?P<ny>[0-9]{4})"
    r")(?![a-z0-9])"
)
_ASCII_LOWER = str.maketrans({chr(code): chr(code + 32) for code in range(ord("A"), ord("Z") + 1)})


def _capped(year: int, month: int, day: int) -> date | None:
    """The date with its day capped at the month's last day, or None outside the calendar."""
    if not 1 <= year <= 9999 or not 1 <= month <= 12:
        return None
    return date(year, month, min(max(day, 1), calendar.monthrange(year, month)[1]))


def _six_months_back(s: date) -> tuple[int, int, int]:
    """s moved back six calendar months as (year, month, day), uncapped, so a coming
    29 February compares by its own day."""
    month = s.month - 6
    year = s.year
    if month < 1:
        month += 12
        year -= 1
    return year, month, s.day


def _stated(match: re.Match, s: date, forward: bool) -> date | None:
    """The date one match states, or None (an impossible day, a wrong weekday, month over 12)."""
    if match.group("nd") is not None:
        day, month, year = int(match.group("nd")), int(match.group("nm")), int(match.group("ny"))
        weekday = None
    else:
        day_text = match.group("d1") or match.group("d2")
        day = int(re.match(r"[0-9]+", day_text).group(0))
        month = _MONTH_OF[match.group("m1") or match.group("m2")]
        year_text = match.group("y1") or match.group("y2")
        year = None if year_text is None else int(year_text)
        weekday = None if match.group("wd") is None else _WEEKDAY_OF[match.group("wd")]
    if not 1 <= month <= 12:
        return None
    if year is None:
        year = s.year
        if forward:
            # In tuple order, with no capping on either side.
            if (year, month, day) < _six_months_back(s):
                year += 1
        else:
            capped = _capped(year, month, day)
            if capped is None:
                return None
            if capped > s:
                year -= 1
    try:
        found = date(year, month, day)
    except ValueError:
        return None
    if weekday is not None and found.weekday() != weekday:
        return None
    return found


def stated_dates(text, source_date, *, forward: bool) -> frozenset[date]:
    """Every date the text states in words or as day/month/year figures. Pure, and it
    never raises: anything it can't read states nothing."""
    return frozenset(stated for _start, stated in stated_spans(text, source_date, forward=forward))


def stated_spans(text, source_date, *, forward: bool) -> list[tuple[int, date]]:
    """Each date the text states, with the index in text where its match starts. The
    lowering keeps every index, so the start is the original text's. Pure, and it never raises."""
    s = as_date(source_date)
    if not isinstance(text, str) or s is None:
        return []
    found: list[tuple[int, date]] = []
    for match in _STATED.finditer(text.translate(_ASCII_LOWER)):
        stated = _stated(match, s, forward)
        if stated is not None:
            found.append((match.start(), stated))
    return found


def date_in_words(raw, source_date) -> date | None:
    """The date a raw value states when, trimmed, it is exactly one date written in words, read
    forward, else None."""
    s = as_date(source_date)
    if not isinstance(raw, str) or s is None:
        return None
    match = _STATED.fullmatch(raw.strip().translate(_ASCII_LOWER))
    return None if match is None else _stated(match, s, True)


def shifted(source_date, days: int) -> date | None:
    """A source date moved by whole days, or None when it leaves the calendar."""
    s = as_date(source_date)
    if s is None:
        return None
    try:
        return s + timedelta(days=days)
    except OverflowError:
        return None
