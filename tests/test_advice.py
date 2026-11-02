"""The database rules in words (brain.advice.integrity_error), checked against what SQLite really raises.

A raw "CHECK constraint failed: (status IN ('done', 'dropped')) = (closed_at IS NOT NULL)" does not tell an agent
what to change. The packet schema catches most bad values first, so these rules show up only for a value the schema
accepts, such as a done action with no closed_at, or for a store whose own schema carries more rules than this one.
"""
from __future__ import annotations

import sqlite3

import pytest

from brain import advice

SCRATCH = """
CREATE TABLE things (
  id TEXT PRIMARY KEY,
  status TEXT CHECK (status IN ('open', 'done', 'dropped')),
  title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 90),
  summary TEXT CHECK (length(summary) <= 1200),
  at TEXT CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', at) IS at),
  day TEXT CHECK (date(day) IS day),
  closed_at TEXT,
  parent_id TEXT REFERENCES things(id),
  weird INTEGER CHECK (weird % 7 <> 3),
  CHECK ((status IN ('done', 'dropped')) = (closed_at IS NOT NULL))
) STRICT;
"""
GOOD = {"id": "t-1", "status": "open", "title": "A", "summary": "s", "at": "2026-10-14T00:00:00Z", "day": "2026-10-14"}


def _refusal(row: dict) -> str:
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCRATCH)
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    try:
        with pytest.raises(sqlite3.IntegrityError) as caught:
            con.execute(f"INSERT INTO things ({columns}) VALUES ({marks})", list(row.values()))
        return advice.integrity_error(con, caught.value, "things", row)
    finally:
        con.close()


RULES = [
    ("a done row with no closed_at", {**GOOD, "status": "done"}, ["closed_at", "done", "dropped", "null"]),
    ("a closed_at on an open row", {**GOOD, "closed_at": "2026-10-14T00:00:00Z"}, ["closed_at", "null"]),
    ("a title that is empty", {**GOOD, "title": " "}, ["title", "1 to 90 characters"]),
    ("a summary over its limit", {**GOOD, "summary": "x" * 1201}, ["summary", "1200", "full text", "source"]),
    ("a timestamp that isn't UTC", {**GOOD, "at": "14 Oct"}, ["at", "UTC timestamp", "2026-10-14T00:00:00Z"]),
    ("a day that isn't a date", {**GOOD, "day": "14 Oct"}, ["day", "date", "2026-10-14"]),
    ("a missing title", {k: v for k, v in GOOD.items() if k != "title"}, ["title", "can't be null"]),
    ("a parent that isn't there", {**GOOD, "parent_id": "t-none"}, ["parent_id", "t-none", "does not exist"]),
    ("a rule it has no words for", {**GOOD, "weird": 3}, ["weird", "breaks a rule on things"]),
]


@pytest.mark.parametrize(("name", "row", "needles"), RULES, ids=[rule[0] for rule in RULES])
def test_a_database_rule_is_put_in_words(name, row, needles) -> None:
    text = _refusal(row)
    lacking = [needle for needle in needles if needle not in text]
    assert not lacking, f"{name}: {text!r} lacks {lacking!r}"
    assert "constraint failed" not in text, f"{name}: the raw SQLite text reached the agent: {text!r}"


def test_a_duplicate_key_says_to_send_another() -> None:
    con = sqlite3.connect(":memory:")
    con.executescript(SCRATCH)
    con.execute("INSERT INTO things (id, status, title) VALUES ('t-1', 'open', 'A')")
    with pytest.raises(sqlite3.IntegrityError) as caught:
        con.execute("INSERT INTO things (id, status, title) VALUES ('t-1', 'open', 'B')")
    text = advice.integrity_error(con, caught.value, "things", {"id": "t-1"})
    assert "id" in text and "already used" in text and "different" in text


def test_a_refusal_it_doesnt_know_passes_through() -> None:
    assert advice.integrity_error(None, sqlite3.IntegrityError("something else"), "things") == "something else"


def test_unknown_names_get_the_closest_allowed_one() -> None:
    allowed = ["entity", "filters", "order_by", "desc", "limit", "offset", "owner_person_id", "title"]
    assert advice.closest("order", allowed) == "order_by"
    assert advice.closest("where", allowed) == "filters"
    assert advice.closest("owner", allowed) == "owner_person_id"
    assert advice.closest("titel", allowed) == "title"
    assert advice.closest("zzzz", allowed) is None
    assert advice.closest("query", ["entity", "limit"]) is None
