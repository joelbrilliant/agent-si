"""Strict writes.

The strict rules as pure functions. They read through their arguments and never write, so the
applier and tools/rollout_check.py refuse exactly the same packets. This module imports only
schemas, units, truth, evidence_gate and advice; candidates imports it, never the reverse.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime

from brain import advice, evidence_gate, schemas, truth, units

NOTE_LABEL = "Written by an agent, unsourced. Not a fact."
# The dates a direct op's source must state. True marks a forward field.
STATE_DATES = {
    "upsert_action": (("due_date", True), ("asked_on", False)),
    "upsert_decision": (("decided_on", False),),
}
_DAY_INDEX = {name: index for index, name in enumerate(schemas.DAY_WORDS)}
# The day words a field may read: a forward field takes tomorrow, a backward one yesterday.
_FORWARD_WORDS = ("today", "tomorrow", *schemas.DAY_WORDS)
_BACKWARD_WORDS = ("today", "yesterday", *schemas.DAY_WORDS)
_BOOL_NAMES = {"true": ("true", "yes"), "false": ("false", "no")}
# The words that negate the value word, name or value right after them.
NEGATORS = frozenset({
    "not", "no", "never", "without", "non", "isn't", "isnt", "aren't", "arent",
    "doesn't", "doesnt", "don't", "dont", "cannot", "can't", "cant",
})
# C4 by type: a separator between digits, and what may follow a URL.
_DIGIT_SEPARATOR = re.compile(r"(?<=[0-9])[.,](?=[0-9])")
_URL_END = r"(?![A-Za-z0-9/_~%#?&=+-]|\.[A-Za-z0-9])"


def sets(row: dict, current: dict | None, field: str) -> bool:
    """An op sets a field when its row carries the field as a string that differs from the stored
    value. On a new row, an empty string doesn't set it."""
    if field not in row or not isinstance(row[field], str):
        return False
    if current is None:
        return row[field] != ""
    return row[field] != current.get(field)


def map_fact_value(registry, value) -> tuple[dict | None, str]:
    """A fact row's value as value columns, type-checked against its key (moved from the applier)."""
    value_type = registry["value_type"]
    empty = {"value_text": None, "value_int": None, "value_real": None, "value_date": None}
    if value_type == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            return None, "value must be a non-blank string"
        return {**empty, "value_text": value}, ""
    if value_type == "url":
        if not isinstance(value, str) or len(value) > 2000 or not (
            value.startswith("http://") or value.startswith("https://")
        ):
            return None, "value must be an http(s) URL"
        return {**empty, "value_text": value}, ""
    if value_type == "enum":
        allowed = json.loads(registry["allowed_values"])  # trusted: fact_keys.allowed_values, written by the applier
        if not isinstance(value, str) or value not in allowed:
            return None, "value is not in allowed_values"
        return {**empty, "value_text": value}, ""
    if value_type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            return None, "value must be an integer"
        return {**empty, "value_int": value}, ""
    if value_type == "bool":
        if not isinstance(value, bool):
            return None, "value must be a boolean"
        return {**empty, "value_int": 1 if value else 0}, ""
    if value_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            return None, "value must be a finite number"
        return {**empty, "value_real": float(value)}, ""
    if value_type == "date":
        if not isinstance(value, str):
            return None, "value must be a date"
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            if parsed.date().isoformat() != value:
                raise ValueError
        except ValueError:
            return None, "value must be a real date"
        return {**empty, "value_date": value}, ""
    return None, "unsupported value_type"


def _field(registry, name: str):
    """A registry column, or None when the row (schema 4) doesn't have it."""
    try:
        return registry[name]
    except (KeyError, IndexError):
        return None


def allowed_values(registry):
    raw = _field(registry, "allowed_values")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)  # trusted: a fact_keys column, written by the applier
    except ValueError:
        return None


def value_words(registry):
    """A key's stored value_words as parsed JSON, or None (absent, null or unreadable)."""
    raw = _field(registry, "value_words")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)  # trusted: a fact_keys column, written by the applier
    except ValueError:
        return None


def parse_raw(registry, raw: str, source_date) -> dict:
    """C3: units.parse with the key's value words and the source's date. Raises units.UnitError."""
    return units.parse(
        registry["value_type"],
        registry["unit"],
        allowed_values(registry),
        raw,
        value_words=value_words(registry),
        source_date=source_date,
    )


def fact_error(label: str, registry, row: dict, mapped: dict | None, source) -> str:
    """The strict checks of an upsert_fact row, or '' when it passes."""
    prefix = f"op {label} (upsert_fact): "
    has_quote, has_raw = "quote" in row, "value_raw" in row
    if row.get("status") == "retired":
        if has_quote or has_raw:
            return prefix + "a retired fact takes no quote or value_raw"
        return ""
    if "value" not in row:
        if has_quote or has_raw:
            return prefix + "quote and value_raw go with a value"
        return ""
    if not (has_quote and has_raw):
        return prefix + "a value needs quote and value_raw"
    raw, quote = row["value_raw"], row["quote"]
    value_type = registry["value_type"]
    source_date = source["source_date"]
    try:
        parsed = parse_raw(registry, raw, source_date)
    except units.UnitError as exc:
        return prefix + exc.tail
    key = row["key"]
    if mapped is None or truth.canonical(key, {"value_type": value_type, **parsed}) != truth.canonical(
        key, {"value_type": value_type, **mapped}
    ):
        shown = "" if mapped is None else units.render(value_type, units.typed(value_type, mapped))
        return prefix + f"value_raw {raw} is not the value {shown}"
    if not evidence_gate.holds(source, evidence_gate.match_key(quote)):
        return prefix + f"quote is not in source {source['id']}"
    if not value_in_quote(registry, raw, parsed, quote, source_date):
        return prefix + f"value {raw} is not in the quote"
    return ""


def _value_terms(registry) -> tuple[dict[str, list[tuple[str, str]]], bool]:
    """Each value of a bool or enum key with its own names and its configured words."""
    value_type = registry["value_type"]
    words = units.words_of(value_words(registry))
    terms: dict[str, list[tuple[str, str]]] = {}
    if value_type == "bool":
        for value, names in _BOOL_NAMES.items():
            terms[value] = [(name, "name") for name in names]
    else:
        for value in allowed_values(registry) or []:
            if isinstance(value, str):
                terms.setdefault(value, []).append((value, "name"))
    for value, items in words.items():
        if value in terms:
            terms[value].extend((item, "word") for item in items)
    return terms, value_type == "bool"


def _negated(tokens: list[str], start: int, run: list[str]) -> bool:
    """Whether the run at tokens[start:] comes right after a negator, unless the run itself starts with
    one (a configured negative form, or a text value such as "No pain no gain")."""
    return start > 0 and tokens[start - 1] in NEGATORS and run[0] not in NEGATORS


def _said(quote_key: str, raw_key: str) -> bool:
    """contains, one occurrence at a time: some occurrence of raw_key's word run in quote_key isn't right
    after a negator, so "not 64K tokens" says nothing about 64K."""
    tokens, run = quote_key.split(), raw_key.split()
    size = len(run)
    return bool(run) and any(
        tokens[start:start + size] == run and not _negated(tokens, start, run)
        for start in range(len(tokens) - size + 1)
    )


def _after_negator(text: str, start: int) -> bool:
    """Whether the match_key word right before text[start:] is a negator."""
    before = evidence_gate.match_key(text[:start]).split()
    return bool(before) and before[-1] in NEGATORS


def _occurrences(registry, parsed: dict, quote_key: str):
    """The first steps of the rule for a bool or enum key: the parsed value, whether the key is
    bool, the occurrences left after the nesting and negator drops, and the negated ones."""
    terms, is_bool = _value_terms(registry)
    if is_bool:
        wanted = "true" if parsed.get("value_int") else "false"
    else:
        wanted = parsed.get("value_text")
    tokens = quote_key.split()
    found: list[tuple[int, int, str, str, str]] = []  # start, end, value, term, kind
    for value, items in terms.items():
        for term, kind in items:
            run = evidence_gate.match_key(term).split()
            if not run:
                continue
            size = len(run)
            for start in range(len(tokens) - size + 1):
                if tokens[start:start + size] == run:
                    found.append((start, start + size, value, " ".join(run), kind))
    kept = [
        item
        for item in found
        if not any(
            other[2] != item[2]
            and other[1] - other[0] > item[1] - item[0]
            and other[0] <= item[0]
            and item[1] <= other[1]
            for other in found
        )
    ]
    # A negator right before an occurrence drops it, unless its own run starts with one (a
    # configured negative form such as "not included"). It counts for no value.
    unnegated, negated = [], []
    for item in kept:
        if _negated(tokens, item[0], item[3].split()):
            negated.append(item)
            continue
        unnegated.append(item)
    return wanted, is_bool, unnegated, negated


def _exclusive(registry, parsed: dict, quote_key: str) -> bool:
    """C4 for a bool or enum key: the exclusive-occurrence rule."""
    wanted, is_bool, unnegated, negated = _occurrences(registry, parsed, quote_key)
    # A negated name or word of the parsed value refuses it ("It is not true", D9).
    refused = any(item[2] == wanted for item in negated)
    spans: dict[tuple[int, int], list[tuple[str, str, str]]] = {}
    for start, end, value, term, kind in unnegated:
        spans.setdefault((start, end), []).append((value, term, kind))
    proved = False
    for entries in spans.values():
        values = {value for value, _term, _kind in entries}
        if wanted in values:
            # A run the wanted value shares counts for it and against none.
            proved = True
            continue
        for _value, term, kind in entries:
            if kind == "word" or (is_bool and term in ("true", "false")):
                refused = True
    return proved and not refused


def value_named(registry, parsed: dict, quote_key: str) -> bool:
    """resolve_fact's step 9 for a bool or enum key: an occurrence of the kept value's name or word
    survives the nesting and negator drops. Other values' occurrences and the kept value's negated ones
    refuse nothing, because a person's resolution names the side it rejects. A bare yes or no names
    nothing here: only configured words, true and false, and enum names do."""
    wanted, is_bool, unnegated, _negated_items = _occurrences(registry, parsed, quote_key)
    return any(
        item[2] == wanted and not (is_bool and item[4] == "name" and item[3] in ("yes", "no"))
        for item in unnegated
    )


def _joined(text: str) -> str:
    """match_key with a separator between two digits kept, so 1,280 and 80.5 stay one word
. MATCH_WORD keeps '_'."""
    return evidence_gate.match_key(_DIGIT_SEPARATOR.sub("_", text))


def value_in_quote(registry, raw: str, parsed: dict, quote: str, source_date) -> bool:
    """C4: the quote holds the value, read by the key's type."""
    quote_key = evidence_gate.match_key(quote)
    value_type = registry["value_type"]
    if value_type in ("bool", "enum"):
        return _exclusive(registry, parsed, quote_key)
    # Every type reads only occurrences that aren't right after a negator.
    if value_type in ("int", "number"):
        return _said(_joined(quote), _joined(raw))
    if value_type == "url":
        pattern = r"(?<![A-Za-z0-9])" + re.escape(raw) + _URL_END
        return any(not _after_negator(quote, match.start()) for match in re.finditer(pattern, quote, re.I))
    if value_type != "date":
        return _said(quote_key, evidence_gate.match_key(raw))
    # A date: an ISO raw by its words; any other raw only by a day word or a stated date, which
    # carry the year, so "14 Sep" can't be proved by "14 Sep 2027".
    if units.as_date(raw) is not None and _said(quote_key, evidence_gate.match_key(raw)):
        return True
    target = units.as_date(parsed.get("value_date"))
    if target is None:
        return False
    for word, shift in units.DAY_SHIFTS.items():
        if _said(quote_key, word) and units.shifted(source_date, shift) == target:
            return True
    return any(
        stated == target and not _after_negator(quote, start)
        for start, stated in units.stated_spans(quote, source_date, forward=True)
    )


def day_date(word: str, s, *, forward: bool) -> date | None:
    """One day word (today, yesterday, tomorrow or a day name) from the source date: a day name is the earliest such day on or after the source date going
    forward, and the latest on or before it going back. None when it leaves the calendar."""
    anchor = units.as_date(s)
    if anchor is None:
        return None
    if word in units.DAY_SHIFTS:
        return units.shifted(anchor, units.DAY_SHIFTS[word])
    shift = _DAY_INDEX[word] - anchor.weekday()
    return units.shifted(anchor, shift % 7 if forward else -((-shift) % 7))


def day_dates(content, s, *, forward: bool) -> set[date]:
    """The dates the day words in a source resolve to."""
    key = evidence_gate.match_key(content)
    found: set[date] = set()
    for word in _FORWARD_WORDS if forward else _BACKWARD_WORDS:
        if evidence_gate.contains(key, word):
            resolved = day_date(word, s, forward=forward)
            if resolved is not None:
                found.add(resolved)
    return found


def date_rule(field: str, value: str, source, *, forward: bool) -> str:
    """The rule for one ISO date against its source: '' when it passes, else the tail
    after `op <l> (<name>): ` (or after the planner's path)."""
    source_date = source["source_date"]
    content = source["content"]
    if not forward and value > source_date:
        return f"{field} {value} is after the source date {source_date}"
    if value == source_date or value in content:
        return ""
    stated = units.as_date(value)
    if stated is not None and (
        stated in day_dates(content, source_date, forward=forward)
        or stated in units.stated_dates(content, source_date, forward=forward)
    ):
        return ""
    return f"{field} {value} is neither the source date nor in the source"


def date_error(label: str, name: str, row: dict, current: dict | None, source) -> str:
    """Each state date a direct op sets must come from a cited source. `source` is one source row or
    the list of every source the op cites (0008): a date passes when any of them allows it."""
    sources = [source] if isinstance(source, dict) or not isinstance(source, (list, tuple)) else list(source)
    for field, forward in STATE_DATES.get(name, ()):
        if not sets(row, current, field):
            continue
        tails = [date_rule(field, row[field], item, forward=forward) for item in sources]
        if all(tails):
            if len(sources) == 1:
                return f"op {label} ({name}): {tails[0]}"
            return f"op {label} ({name}): " + advice.date_not_in_sources_error(
                field, row[field], [item["id"] for item in sources]
            )
    return ""


def note_error(
    label: str, name: str, row: dict, current: dict | None, writes, *, cap_exempt: bool, switch_exempt: bool
) -> str:
    """The switch, then the cap, on the note fields an op sets."""
    fields = [field for field in schemas.NOTE_FIELDS.get(name, ()) if sets(row, current, field)]
    if not switch_exempt and not writes.notes:
        for field in fields:
            if row[field] != "":
                return f"op {label} ({name}): notes are off in this instance: {field}"
    if not cap_exempt:
        for field in fields:
            if len(row[field]) > writes.note_max_chars:
                return f"op {label} ({name}): {field} is a note and is at most {writes.note_max_chars} characters"
    return ""


def candidate_note_errors(fields: dict, names: tuple[str, ...], writes) -> list[dict]:
    """A candidate's note fields, checked as the derived op will be (an insert)."""
    errors: list[dict] = []
    for name in names:
        text = fields.get(name)
        if not isinstance(text, str) or text == "":
            continue
        if not writes.notes:
            errors.append({"path": f"/fields/{name}", "message": f"notes are off in this instance: {name}"})
        elif len(text) > writes.note_max_chars:
            errors.append(
                {"path": f"/fields/{name}", "message": f"{name} is a note and is at most {writes.note_max_chars} characters"}
            )
    return errors

