"""Watched facts: fixed rules that say a new source mentions a fact that matters (1.17.0).

A fact is only as current as its last claim, and nothing moved a claim when new information arrived as a source.
So the owner lists the few facts other people rely on in watch.toml, beside brain.toml: one table per fact key,
whose words are a list of word groups. A source mentions the fact when one paragraph of its title or text holds a
phrase from every group.

Nothing is stored. The applier tests each new source as it is added and names the fact in the receipt. The reads
work out, every time, which sources that arrived since the fact was last written or verified mention it, and show
them with the fact. A newer claim or a re-verification clears them, because they are then no longer newer. Rules
are words written once by whoever lists the fact, matched by code: no model reads anything here.

The list has its own file for two reasons. An engine older than this one refuses a brain.toml key it doesn't
know, so a rule there would stop every long-running reader that still holds the old code, and would have to come
out before a rollback. And a dozen watched facts read better on one page than scattered through hundreds of
[[fact_keys]] entries. A watch.toml that is missing means no fact is watched. One that is wrong never stops a read
or a write: the entries that can be used apply, and brain check and the brief name the rest.

Measured on a real brain on 12 November 2026 before this was built: ranking every key against a source's text by
word overlap put the right key in the top ten for 193 of 610 past cases, so the engine does not guess which facts a
source touches. A hand-written rule for one fact flagged 15 of 1,504 later sources, the two that carried the newer
answer among them.

This module imports evidence_gate only, so the applier, the reads and the command line can all use it.
"""
from __future__ import annotations

import re
import sqlite3
import tomllib
import unicodedata
from pathlib import Path

from brain import evidence_gate

FILE = "watch.toml"
MAX_BYTES = 256 * 1024
MAX_GROUPS = 4
MAX_PHRASES = 24
MAX_PHRASE_CHARS = 60
SHOWN = 5  # source ids listed with a fact, newest first
NEXT = (
    "Read these sources. If one changes the value, send add_claim on this key citing it. If the value stands, "
    "send upsert_fact with this key and last_verified_at."
)
# Words as the search index splits them (reads._WORD and reads._fold), so a rule's phrases mean the same in the
# index's first pass and in the paragraph test.
_WORD = re.compile(r"[^\W_]+")

Rule = tuple[tuple[str, ...], ...]


def fold(text) -> str:
    """Lowercase, NFD, and no combining marks."""
    return "".join(ch for ch in unicodedata.normalize("NFD", str(text or "").lower()) if not unicodedata.combining(ch))


def phrase(text) -> str:
    """A phrase's words, single-spaced: 'Help-Desk' and 'help desk' are one phrase."""
    return " ".join(_WORD.findall(fold(text)))


def parse(value) -> Rule:
    """A fact's words from watch.toml as a rule, or ValueError naming the fix. Groups are joined by and, the
    phrases inside a group by or."""
    example = 'such as [["help desk", "support line"], ["hours", "open"]]'
    if not isinstance(value, list) or not value:
        raise ValueError(f"must be a list of 1 to {MAX_GROUPS} word groups, {example}")
    if len(value) > MAX_GROUPS:
        raise ValueError(f"holds {len(value)} groups; at most {MAX_GROUPS}")
    groups = []
    for g, group in enumerate(value):
        if not isinstance(group, list) or not group:
            raise ValueError(f"[{g}] must be a list of 1 to {MAX_PHRASES} phrases, {example}")
        if len(group) > MAX_PHRASES:
            raise ValueError(f"[{g}] holds {len(group)} phrases; at most {MAX_PHRASES}")
        phrases = []
        for p, item in enumerate(group):
            if not isinstance(item, str) or len(item) > MAX_PHRASE_CHARS:
                raise ValueError(f"[{g}][{p}] must be text of at most {MAX_PHRASE_CHARS} characters")
            words = phrase(item)
            if not words:
                raise ValueError(f"[{g}][{p}] holds no word")
            if words not in phrases:
                phrases.append(words)
        groups.append(tuple(phrases))
    return tuple(groups)


def _key(text) -> str:
    return " " + phrase(text) + " "


def _holds(rule: Rule, key: str) -> bool:
    return all(any(f" {item} " in key for item in group) for group in rule)


def mentions(rule: Rule, *texts) -> bool:
    """Whether one paragraph of the texts holds a phrase from every group. A paragraph is a line, with
    hard-wrapped lines joined: a chat message, a list item, an email paragraph."""
    if not rule or not _holds(rule, _key("\n".join(str(text or "") for text in texts))):
        return False  # the cheap test: every group is somewhere in the text
    for text in texts:
        for line in evidence_gate.join_wrapped(text):
            if line.strip() and _holds(rule, _key(line)):
                return True
    return False


def match_expr(rule: Rule) -> str:
    """The FTS5 match for the first pass: a document holding a phrase from every group somewhere."""
    return " AND ".join("(" + " OR ".join(f'"{item}"' for item in group) + ")" for group in rule)


def _tables(data: dict, prefix: str = "") -> list[tuple[str, dict]]:
    """Each table that holds settings, by its dotted name: ["svc.hosting"] and [svc.hosting] are the same fact."""
    found = []
    for name, value in data.items():
        if not isinstance(value, dict):
            continue
        path = f"{prefix}.{name}" if prefix else str(name)
        if any(not isinstance(item, dict) for item in value.values()) or not value:
            found.append((path, {key: item for key, item in value.items() if not isinstance(item, dict)}))
        found.extend(_tables({key: item for key, item in value.items() if isinstance(item, dict)}, path))
    return found


def load(instance_dir, fact_keys) -> tuple[dict[str, Rule], list[str]]:
    """The watch list: each watched key's rule, and what is wrong with the file, in words that name the fix.
    fact_keys are the keys brain.toml registers. No file is no rules and no problem."""
    path = Path(instance_dir) / FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {}, []
    except OSError as exc:
        return {}, [f"{FILE} can't be read ({type(exc).__name__}), so no fact is watched"]
    if len(raw) > MAX_BYTES:
        return {}, [f"{FILE} is over {MAX_BYTES // 1024} KB, so no fact is watched"]
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"{FILE} is not valid TOML ({exc}), so no fact is watched"]
    known = set(fact_keys)
    rules: dict[str, Rule] = {}
    problems = [f"{FILE}: {name} sits outside a table; write [\"<fact key>\"] then words = [[...]]"
                for name, value in data.items() if not isinstance(value, dict)]
    for key, table in _tables(data):
        if key not in known:
            problems.append(f"{FILE}: {key} is not a fact key in brain.toml, so it is not watched")
            continue
        extra = sorted(set(table) - {"words"})
        if extra:
            problems.append(f"{FILE}: {key}.{extra[0]} is not a setting; a watched fact takes words")
            continue
        try:
            rules[key] = parse(table.get("words"))
        except ValueError as exc:
            problems.append(f"{FILE}: {key}.words {exc}")
    return rules, problems


def checked(con: sqlite3.Connection, key: str) -> tuple[str, str]:
    """When a fact was last written or verified (UTC timestamp), and the date its newest live claim speaks for.
    Both are '' for a fact with no claim yet."""
    row = con.execute("SELECT last_verified_at FROM facts WHERE key = ?", (key,)).fetchone()
    verified = (row[0] if row else "") or ""
    claim = con.execute("SELECT max(updated_at) FROM claims WHERE key = ?", (key,)).fetchone()
    written = (claim[0] if claim else "") or ""
    live = con.execute("SELECT max(as_of) FROM claims WHERE key = ? AND status = 'live'", (key,)).fetchone()
    return max(verified, written), (live[0] if live else "") or ""


def newer(con: sqlite3.Connection, key: str, rule: Rule) -> list[tuple[str, str]]:
    """The sources that mention a watched fact and that nobody has checked it against: captured after the fact was
    last written or verified, dated on or after the day its value speaks for, and cited by no claim on the key.
    (id, source_date) pairs, newest capture first. The search index finds the candidates and the paragraph test
    decides, so a source is read only when every group is somewhere in it."""
    since, as_of = checked(con, key)
    try:
        rows = con.execute(
            "SELECT s.id, s.source_date, s.title, s.content FROM search_index AS i "
            "JOIN sources AS s ON s.id = i.ref_id "
            "WHERE search_index MATCH ? AND i.entity = 'source' AND s.captured_at > ? AND s.source_date >= ? "
            "AND s.id NOT IN (SELECT source_id FROM claims WHERE key = ?) "
            "ORDER BY s.captured_at DESC, s.id",
            (match_expr(rule), since, as_of, key),
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # no search index to ask: the fact reads as it did before
    return [(row[0], row[1]) for row in rows if mentions(rule, row[2], row[3])]


def summary(found: list[tuple[str, str]]) -> dict:
    """What a fact's readers see: how many newer sources mention it, and when there are some, the newest few and
    what to send next."""
    out: dict = {"newer_sources": len(found)}
    if found:
        out["latest"] = max(day for _id, day in found)
        out["sources"] = [source_id for source_id, _day in found[:SHOWN]]
        out["next"] = NEXT
    return out
