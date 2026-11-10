"""Error wording that names the fix.

An agent that gets "/order is not allowed" sends the same call again. So every refusal an agent can read, from a
bad read argument (reads.py), a bad packet (validate.py) or a refused write (applier.py), is built here, and each
one says what to send instead: the argument to use, the allowed values, the limit or an example. This module
imports only schemas, so all three can use it. tests/test_error_fixes.py walks a table of bad inputs and fails,
naming the input, when a message loses its fix.

Each function returns the text after the path, such as "is above the maximum of 100".
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3

from brain import schemas

EXAMPLE_UTC = "2026-10-14T00:00:00Z"
EXAMPLE_DATE = "2026-10-14"
# The filter operators in the order a message lists them.
OPERATORS = ("eq", "in", "lt", "lte", "gt", "gte", "is_null", "contains", "under")
# Names agents carry over from other systems: the property to use instead, and how.
_ORDER = ("order_by", " (desc: true for descending)")
_FILTERS = ("filters", " (a map of field to value, or to an operator object)")
RENAMES = {
    "order": _ORDER,
    "sort": _ORDER,
    "sort_by": _ORDER,
    "orderby": _ORDER,
    "order_by_desc": _ORDER,
    "where": _FILTERS,
    "filter": _FILTERS,
    "query": _FILTERS,
    "conditions": _FILTERS,
    "q": ("query", ""),
    "text": ("query", ""),
    "keywords": ("query", ""),
    "max_results": ("limit", ""),
    "page_size": ("limit", ""),
    "count": ("limit", ""),
    "skip": ("offset", ""),
    "columns": ("fields", ""),
    "select": ("fields", ""),
}
_FIELD_PLUMBING = frozenset({"locator", "updated_by", "created_by", "captured_by"})


def listed(values) -> str:
    return ", ".join(str(value) for value in values)


def enum_error(values) -> str:
    return f"is not an allowed value; use one of: {listed(values)}"


def required_error(values=None) -> str:
    return "is required" if not values else f"is required; use one of: {listed(values)}"


def closest(name: str, allowed) -> str | None:
    """The allowed name an agent most likely meant: a known carry-over, a longer name that starts with it, or a
    near spelling."""
    allowed = list(allowed)
    renamed = RENAMES.get(name.lower())
    if renamed is not None and renamed[0] in allowed:
        return renamed[0]
    for candidate in allowed:
        if candidate.startswith(name + "_"):
            return candidate
    near = difflib.get_close_matches(name, allowed, n=1, cutoff=0.6)
    return near[0] if near else None


def unknown_property_error(name: str, allowed) -> str:
    allowed = list(allowed)
    guess = closest(name, allowed)
    use = ""
    if guess is not None:
        renamed = RENAMES.get(name.lower())
        use = f"; use {guess}" + (renamed[1] if renamed is not None and renamed[0] == guess else "")
    return f"is not allowed{use}. Allowed: {listed(allowed)}"


def unknown_field_message(name: str, allowed) -> str:
    """The message of an unknown_field error. The allowed names travel in its allowed list."""
    guess = closest(name, allowed)
    return f"unknown field {name}" + (f"; use {guess}" if guess is not None else "")


def utc_error() -> str:
    return f"must be a canonical UTC timestamp, like {EXAMPLE_UTC}"


def date_error() -> str:
    return f"must be a real date, like {EXAMPLE_DATE}"


_PREFIXED_ID = re.compile(r"^\^([a-z]{3})-")


def pattern_error(pattern: str) -> str:
    match = _PREFIXED_ID.match(pattern)
    if match is None:
        return "does not match the required pattern"
    return (
        "does not match the required pattern; it must start with "
        f"{match.group(1)}- and use lowercase letters, digits and hyphens"
    )


def one_line_error(found: str | None = None) -> str:
    """Text that must be one line holds a line break, or (found is the character) another character a line
    can't hold. Built here for text.single_line and for validate's single_line format."""
    if found is None:
        return "must be one line: replace each line break (a newline, U+2028, U+2029 or U+0085) with a space"
    return f"must be one line of printable text: replace U+{ord(found):04X} with a space"


def blank_line_error() -> str:
    return "must not be blank: write at least one visible character"


def bound_error(kind: str, limit) -> str:
    """kind is maximum or minimum."""
    return f"is {'above' if kind == 'maximum' else 'below'} the {kind} of {limit}"


def size_error(what: str, kind: str, limit) -> str:
    """what is characters, items or properties; kind is maximum or minimum."""
    words = {
        ("characters", "maximum"): "is too long",
        ("characters", "minimum"): "is too short",
        ("items", "maximum"): "has too many items",
        ("items", "minimum"): "has too few items",
        ("properties", "maximum"): "has too many properties",
    }[(what, kind)]
    unit = ""
    if what == "characters":
        unit = " character" if limit == 1 else " characters"
    return f"{words}; use {'at most' if kind == 'maximum' else 'at least'} {limit}{unit}"


def missing_fields_error(names, enums: dict | None = None, *, to_create: bool = True) -> str:
    """Every missing required field in one message, with the allowed values of the enum ones."""
    names = list(names)
    enums = enums or {}
    verb = "is" if len(names) == 1 else "are"
    head = names[0] if len(names) == 1 else f"{listed(names[:-1])} and {names[-1]}"
    text = f"{head} {verb} required" + (" to create" if to_create else "")
    for name in names:
        if enums.get(name):
            text += f"; {name} is one of {listed(enums[name])}"
    return text


def unknown_op_error(name: str, ops) -> str:
    return f"unknown op {name}; use one of: {listed(sorted(ops))}"


def unorderable_error(kind: str, examples) -> str:
    return f"{kind} fields cannot be ordered; order_by takes a plain field, such as {' or '.join(examples)}"


def not_found_error(entity: str, row_id: str) -> str:
    return f"no {entity} {row_id}; records_query on {entity} lists the ids, and search finds one by words"


def filter_object_error(ops) -> str:
    return (
        "a filter object must hold one operator, or one lower and one upper bound; "
        f"the operators here are {listed(ops)}"
    )


def operator_error(op: str, kind: str, ops, texts=()) -> str:
    text = f"{op} is not allowed for {kind}; it takes {listed(ops)}"
    if op == "contains" and texts:
        text += f". To match part of a name, use contains on {listed(texts)}, or call search"
    return text


def text_fields(fields) -> list[str]:
    """The text columns a contains filter can use, leaving out the bookkeeping ones."""
    return [field.name for field in fields if field.kind == "text" and field.name not in _FIELD_PLUMBING][:3]


def plain_text_hint(name: str, value: str) -> str:
    part = json.dumps({name: {"contains": value}}, ensure_ascii=False)
    return f"{name} matched {json.dumps(value, ensure_ascii=False)} exactly; to match part of it, use filters {part}"


def base_exists_error(table: str, key, version: int) -> str:
    return f'{table} {key} already exists at version {version}; to update it, send "base": {version}'


def base_missing_error(table: str, key, base: int) -> str:
    return f"{table} {key} doesn't exist (base was {base}); to create it, leave base out or send null"


_MIB = 1024 * 1024


def ingress_issue(kind: str, limit: int | None = None) -> str:
    """What was wrong with a document that came from outside, after its subject, such as "nests deeper than 64
    levels". The limit is in the text so the sender knows the size to stay under. brain.ingress raises these and
    each entry point shows them with its own subject, such as "packet" or "--args"."""
    if kind == "size":
        if limit % _MIB == 0:
            amount = f"{limit // _MIB} MiB"
        elif limit % 1024 == 0:
            amount = f"{limit // 1024} KiB"
        else:
            amount = f"{limit} bytes"
        return f"is larger than {amount}"
    if kind == "nesting":
        return f"nests deeper than {limit} levels"
    if kind == "number":
        return "holds a number outside the supported range"
    if kind == "surrogate":
        return "holds a lone surrogate escape"
    if kind == "encoding":
        return "is not valid UTF-8"
    return "is not valid JSON"


def future_error(field: str, now: str) -> str:
    return f"{field} is more than 300 seconds in the future; use the current UTC time, like {now}"


def source_missing_error(source_id: str) -> str:
    return f"cited source {source_id} does not exist; add it first with add_source, in this packet or an earlier one"


# set_source_author (1.8.0): each refusal names the fix.
def author_principal_error() -> str:
    return (
        "only an agent with author_principal = true in brain.toml may change who wrote a source; "
        "send the packet as that agent, or ask the brain's owner to"
    )


def author_target_error(source_id: str, why: str) -> str:
    return f"source {source_id} {why}, so it has no author to change; pick a live source of another kind"


def author_person_missing_error(person_id: str) -> str:
    return f"person {person_id} does not exist; add them with upsert_person first, or send author_person_id null"


def author_unchanged_error(source_id: str, author: str | None) -> str:
    return f"source {source_id} already has author_person_id {author or 'null'}; leave this op out of the packet"


def author_twice_error(source_id: str) -> str:
    return f"this packet changes source {source_id} twice; send one set_source_author op per source, with its final author"


def author_self_cite_error(source_id: str) -> str:
    return (
        f"the op cites {source_id}, the source it changes; cite a different source that says who wrote it, "
        "such as an observation you add first in the same packet"
    )


def author_principal_target_error(principal_id: str) -> str:
    return (
        f"set_source_author never makes {principal_id} a source's author; to record the principal's own words, "
        f"add a new source with author_person_id {principal_id} as an author_principal agent"
    )


def author_corrected_error(source_id: str, author: str | None) -> str:
    return (
        f"source {source_id} is already stored, and set_source_author changed its author_person_id to "
        f"{author or 'null'}; send author_person_id {author or 'null'}, or leave this op out"
    )


def speaker_lines_parent_error(parent_id: str) -> str:
    return (
        f"external_ref speaker-lines:{parent_id} names no stored source the lines could come from; add_source "
        f"{parent_id} first, name the thread or transcript itself (not another speaker-lines source), or use "
        "another external_ref"
    )


def speaker_lines_copy_error(snippet: str, parent_id: str) -> str:
    return (
        f'the line "{snippet}" is not one line of source {parent_id} copied exactly; copy each line character for '
        "character from it, in whole words, one line per paragraph with a blank line between lines, and leave out "
        "labels, times and quote marks"
    )


def author_stored_error(source_id: str, author: str | None) -> str:
    return (
        f"source {source_id} is already stored with author_person_id {author or 'null'}; send author_person_id "
        f"{author or 'null'}, leave author_person_id out, or leave this op out"
    )


def agent_person_missing_error(agent: str, person_id: str) -> str:
    return (
        f"agent {agent} writes observations as person {person_id}, which does not exist; add it first with "
        f'{{"op": "upsert_person", "row": {{"id": "{person_id}", "display_name": "..."}}}}, send '
        "author_person_id yourself, or remove person_id from the agent in brain.toml"
    )


# -- Database rules, in words -----------------------------------------------------------------------

_CROSS_COLUMN = (
    (
        re.compile(r"\(status IN \('done', 'dropped'\)\) = \(closed_at IS NOT NULL\)"),
        "set closed_at when status is done or dropped, and leave it null otherwise",
    ),
    (
        re.compile(r"\(due_date IS NULL\) = \(due_kind IS NULL\)"),
        "set due_date and due_kind together, or leave both null",
    ),
    (
        re.compile(r"\(status = 'open'\) = \(resolved_at IS NULL\)"),
        "set resolved_at when a signal is resolved or dismissed, and leave it null while it is open",
    ),
    (re.compile(r"ends_at >= starts_at"), "ends_at must not be before starts_at"),
    (re.compile(r"finished_at >= started_at"), "finished_at must not be before started_at"),
)
_QUOTED = re.compile(r"'([^']*)'")
_IN_LIST = re.compile(r"(\w+) IN \(((?:'[^']*'(?:, )?)+)\)")
_LENGTH_BETWEEN = re.compile(r"length\((?:trim\()?(\w+)\)?\) BETWEEN (\d+) AND (\d+)")
_LENGTH_AT_MOST = re.compile(r"length\((\w+)\) <= (\d+)")
_TIMESTAMP = re.compile(r"strftime\('%Y-%m-%dT%H:%M:%SZ', (\w+)\) IS")
_DAY = re.compile(r"\bdate\((\w+)\) IS")
_SELF = re.compile(r"(\w+) <> id")


def _check_words(expr: str, table: str | None, row: dict) -> str:
    expr = " ".join(expr.split())  # not one-line: an SQL expression matched against patterns, never shown
    for pattern, words in _CROSS_COLUMN:
        if pattern.search(expr):
            return words
    match = _IN_LIST.search(expr)
    if match and " NOT IN " not in expr and match.group(1) != "NOT":
        values = _QUOTED.findall(match.group(2))
        return f"{match.group(1)} {enum_error(values)}"
    match = _LENGTH_BETWEEN.search(expr)
    if match:
        low, high = int(match.group(2)), int(match.group(3))
        return f"{match.group(1)} must be {low} to {high} characters"
    match = _LENGTH_AT_MOST.search(expr)
    if match:
        limit = int(match.group(2))
        tail = "; keep a short version here and put the full text in the cited source's content" if limit >= 200 else ""
        return f"{match.group(1)} must be at most {limit} characters{tail}"
    match = _TIMESTAMP.search(expr)
    if match:
        return f"{match.group(1)} must be a UTC timestamp like {EXAMPLE_UTC}"
    match = _DAY.search(expr)
    if match:
        return f"{match.group(1)} must be a date like {EXAMPLE_DATE}"
    match = _SELF.search(expr)
    if match:
        return f"{match.group(1)} can't be the row's own id"
    named = [name for name in row if re.search(rf"\b{re.escape(str(name))}\b", expr)]
    where = f" on {table}" if table else ""
    check = f"; check {listed(named)}" if named else ""
    return f"breaks a rule{where}: {expr}{check}"


def _missing_references(con: sqlite3.Connection, table: str, row: dict) -> list[str]:
    entities = {name: entity for entity, name in schemas.ENTITY_TABLE.items()}
    found: list[str] = []

    def absent(ref_table: str, column: str, value) -> bool:
        return con.execute(f"SELECT 1 FROM {ref_table} WHERE {column} = ?", (value,)).fetchone() is None

    def problem(field: str, value, ref_table: str) -> str:
        entity = entities.get(ref_table)
        where = f"; records_query on {entity} lists the ids" if entity else ""
        return f"{field} {value} does not exist{where}"

    for key in con.execute(f"PRAGMA foreign_key_list({table})").fetchall():
        ref_table, column, target = key[2], key[3], key[4] or "id"
        value = row.get(column)
        if isinstance(value, str) and absent(ref_table, target, value):
            found.append(problem(column, value, ref_table))
    for field, values in row.items():
        if field.endswith("_person_ids") and isinstance(values, list):
            for value in values:
                if isinstance(value, str) and absent("people", "id", value):
                    found.append(problem(field, value, "people"))
    return found


def integrity_error(con: sqlite3.Connection | None, exc: Exception, table: str | None = None, row: dict | None = None) -> str:
    """A database refusal in words that name the fix. Anything it doesn't know passes through unchanged."""
    text = str(exc)
    row = row or {}
    if text.startswith("FOREIGN KEY constraint failed"):
        found: list[str] = []
        if con is not None and table:
            try:
                found = _missing_references(con, table, row)
            except sqlite3.Error:
                found = []
        return "; ".join(found) or (
            "a row it points to does not exist; records_query on the entity lists the ids"
        )
    if text.startswith("NOT NULL constraint failed: "):
        return f"{text.rsplit('.', 1)[-1]} can't be null; send a value"
    if text.startswith("UNIQUE constraint failed: "):
        return f"{text.rsplit('.', 1)[-1]} is already used on {table or 'another row'}; send a different one"
    if text.startswith("CHECK constraint failed: "):
        return _check_words(text[len("CHECK constraint failed: "):], table, row)
    return text


# -- Links with sources and the evidence gate (0008) ---------------------------------------------------
# The gate reads every source an op cites: its envelope `source` and each item of `sources`. With one source the
# words are the ones the gate always gave; with more, each refusal names every source it checked.


def _checked(ids) -> str:
    return listed(ids)


def link_source_missing_error(source_id: str) -> str:
    return (
        f"sources names {source_id}, which does not exist; add it first with add_source, in this packet or an "
        "earlier one, or leave it out of sources"
    )


def quote_not_in_sources_error(snippet: str, ids) -> str:
    ids = list(ids)
    if len(ids) == 1:
        return f'quote "{snippet}" is not in the cited source {ids[0]}. Quote the source exactly or remove the quote marks.'
    return (
        f'quote "{snippet}" is not in any cited source (checked {_checked(ids)}). Quote one of them exactly, cite '
        "the source that holds it in sources, or remove the quote marks."
    )


def attribution_error(snippet: str, display_name: str, person_id: str, ids=()) -> str:
    """A sentence that puts words or a decision in the principal's mouth. It names the two routes that work:
    quote an existing source the principal wrote (an agent can't create one in that name), or reword."""
    ids = list(ids)
    text = (
        f'"{snippet}" attributes words or a decision to {display_name}. '
        f"Either quote at least 5 consecutive words from an existing source authored by {person_id} "
        '(a collected email, Slack message or transcript), or reword neutrally ("after approval", '
        '"pending a decision", "the agreed wording") or name who said it.'
    )
    if len(ids) > 1:
        text += f" Checked {_checked(ids)}."
    return text


def decision_quote_missing_error(ids) -> str:
    ids = list(ids)
    if len(ids) == 1:
        return f"decision quote is not in the cited source {ids[0]}"
    return (
        f"decision quote is not in any cited source (checked {_checked(ids)}); quote one of them exactly, or cite "
        "the source that holds it in source or sources"
    )


def decision_author_error(display_name: str, person_id: str, authors) -> str:
    """authors: (source id, author person id or None) for each cited source that holds the quote."""
    authors = list(authors)
    if len(authors) == 1:
        sid, author = authors[0]
        where = f"{sid} is authored by {author or 'nobody'}"
    else:
        where = "checked " + _checked(f"{sid} (authored by {author or 'nobody'})" for sid, author in authors)
    return (
        f"a decision by {display_name} needs its quote from a source authored by {person_id}; {where}. "
        "Cite an existing source they wrote, such as a collected email, Slack message or transcript, "
        "or record who actually decided."
    )


def date_not_in_sources_error(field: str, value: str, ids) -> str:
    return (
        f"{field} {value} is neither the date of nor in any cited source (checked {_checked(ids)}); use a date one "
        "of them states, or cite the source that states it in sources"
    )


def original_times_agent_error(field: str, agent: str) -> str:
    return (
        f"{field} is only for a legacy_import agent's insert, and {agent} is not one; leave it out and the "
        "applier stamps the apply time"
    )


def original_times_update_error(field: str) -> str:
    return f"{field} is set only when a row is created; leave it out of an update to an existing row"


def original_times_order_error(created_at: str, updated_at: str) -> str:
    return (
        f"updated_at {updated_at} is before created_at {created_at}; send an updated_at on or after created_at, "
        "or leave it out to use created_at"
    )


def original_times_future_error(field: str, value: str, now: str) -> str:
    return f"{field} {value} is after the apply time {now}; send the original time, which is no later than {now}"


# -- Folder tokens (1.12.0) -------------------------------------------------------------------------------------


def folder_token_error(folders, what: str, tool: str) -> str:
    """A read a folder token can't answer: facts, systems and claims aren't filed in folders."""
    return (
        f"this token reads only the folders {', '.join(folders)}; {what} aren't filed in folders, so {tool} can't answer "
        "for it. search and records_query (entity record) find what's filed there"
    )


def folder_write_error() -> str:
    return "folder tokens read only in v1; use --scope read"


def folder_missing_error(folder: str) -> str:
    return f"no folder {folder} in this brain; records_query on project lists the ids, and brain hub token list shows each token's folders"


def folder_route_error(route: str) -> str:
    return (
        f"{route} shows brain-wide write records, so a folder token can't read it; "
        "ask for a whole-brain token (issue one without --folder)"
    )


# -- brain setup (1.13.0) ---------------------------------------------------------------------------------------


def setup_name_error(value: str) -> str:
    return (
        f"--name {value!r} isn't a brain name; use lowercase letters, digits and dashes, starting with a letter, "
        "2 to 40 characters, for example work"
    )


def setup_principal_error(value: str) -> str:
    return (
        f"--principal {value!r} can't make a person id; use your full name written with letters, such as "
        '"Alex Example", or --principal "" for no name'
    )


def setup_timezone_error(zone: str) -> str:
    return (
        f"this machine has no time zone data for {zone}; pass --timezone with a name from the IANA list "
        "(Australia/Sydney, America/New_York, UTC), and if UTC fails too install your system's tzdata package "
        "(apt install tzdata) and run again"
    )


def setup_folder_error(folder: str) -> str:
    return f"--dir {folder} isn't a folder brain setup can use; pass --dir with a folder you can write to, such as ~/Brains"


# -- updates come from a release page (1.14.0) -------------------------------------------------------------------

RELEASES_URL = "https://github.com/joelbrilliant/agent-si/releases"


def updates_elsewhere_error(what: str = "brain update") -> str:
    return (
        f"{what} doesn't look for releases or fetch anything by name: agent-si and agent-memory aren't published "
        f"on a package index. Download install-agent-si.sh or the agent-si wheel from {RELEASES_URL}, then run "
        "the installer again, or run brain update --from-wheel PATH-TO-THE-WHEEL"
    )


def rollback_wheel_error(name: str, current: str) -> str:
    return (
        f"brain update can't keep a wheel of the {name} {current} you are running, so it couldn't roll back if the "
        f"update failed. Download the {current} wheel from {RELEASES_URL} and pass it with --rollback-from PATH, "
        "or run the installer file again instead"
    )


def rollback_version_error(path: str, found: str | None, current: str) -> str:
    return (
        f"--rollback-from {path} is {'the wheel for ' + found if found else 'not a wheel brain update can read'}, "
        f"but the version you are running is {current}; pass the {current} wheel from {RELEASES_URL}"
    )


# The session record (brain.sessions): each refusal names the fix.
def session_ref_error(ref: str) -> str:
    return (
        f"{ref!r} is not a session id; a session id is the tool's name, a colon and the tool's own id, such as "
        "claude:0f3a..., as search and brain sessions status show it (tools: claude, codex, grok, hermes)"
    )


def session_missing_error(ref: str) -> str:
    return (
        f"the session record has no session {ref}; search lists sessions with their ids, and brain sessions sync "
        "adds what the tools have written since the last sync"
    )


def session_message_missing_error(session: str, n: int, held: int) -> str:
    return (
        f"session {session} has {held} messages and no message {n}; open it with {session}#1 to {session}#{held}, "
        f"or with {session} alone for its start"
    )


def sessions_off_error() -> str:
    return (
        "session recording is off for this brain; to turn it on, set record = true under [sessions] in brain.toml "
        "(or remove the line), then run brain sessions sync"
    )


def sessions_local_error() -> str:
    return (
        "past sessions are read on the machine that holds them: run brain serve or brain read there. A hub "
        "connection and a folder token don't see the session record"
    )


def search_needs_words_error() -> str:
    return (
        "search needs a query with at least one word, or since or until (a day written like 2026-11-09) to list "
        "that period's sessions"
    )


def session_range_error(first: int, last: int, most: int, *, held: int | None = None, too_long: bool = False) -> str:
    if held is not None:
        return f"the session has {held} messages, so --from {first} --to {last} asks past its end; use 1 to {held}"
    if too_long:
        return f"messages {first} to {last} are longer than a source holds (2,000,000 characters); take fewer messages"
    return (
        f"--from {first} --to {last} is not a range an excerpt takes: from must not be after to, and an excerpt "
        f"holds at most {most} messages; split a longer stretch into two excerpts"
    )


def session_excerpt_hint(session: str, first: int, last: int) -> str:
    return f"brain sessions excerpt {session} --from {first} --to {last} --instance <this directory> prints the op to send"


def session_excerpt_ref_error(ref: str) -> str:
    return (
        f"external_ref {ref!r} starts with session: but is not a session excerpt; an excerpt's external_ref is "
        "session:<session id>#<first>-<last>, such as session:claude:0f3a...#12-14, and brain sessions excerpt "
        "<session id> --from 12 --to 14 prints the whole op"
    )


def session_excerpt_shape_error(kind: str, hint: str) -> str:
    return (
        f"a session excerpt is a source of kind {kind} with no author_person_id and no supersedes_id: the record "
        f"holds what was said, not who typed it. {hint}"
    )


def session_excerpt_content_error(session: str, first: int, last: int, hint: str) -> str:
    return (
        f"this content is not messages {first} to {last} of {session} as the session record holds them, so it is "
        f"refused; an excerpt is copied, never edited or summarised. {hint}"
    )

