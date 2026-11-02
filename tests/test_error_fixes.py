"""Errors that don't say what to do instead, so agents repeat the call.

A bad `records_query` or `get` call used to come back with texts such as "/order is not allowed", "/entity is required", "/limit is
above the maximum" and, on writes, a raw "CHECK constraint failed" or "FOREIGN KEY constraint failed".

These tables hold one bad input for each message an agent got. Every row names the fix its message must carry: the
argument to use, the allowed values, the limit or an example. A row fails, naming its input, when the text the agent
sees lacks that fix. Add a row when an agent meets a new unhelpful error. Reads go through the read tools and writes
through the applier's dry run, which is the verdict `brain apply` and `brain validate packet --instance` give.

This file imports only what existed before the fix, so the same table can be run against older code.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from brain import reads, schemas
from brain.applier import Applier
from brain.init import init_instance
from tests.conftest import TEST_CONFIG, call, make_packet, read_instance, seed, source_op, upsert_op

UTC_EXAMPLE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b")
DATE_EXAMPLE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _property_names(tool: str) -> list[str]:
    return list(reads._TOOLS[tool]["inputSchema"]["properties"])


def _enum_of(tool: str, name: str) -> list[str]:
    return list(reads._TOOLS[tool]["inputSchema"]["properties"][name]["enum"])


def _limit_of(tool: str, name: str, keyword: str) -> int:
    return reads._TOOLS[tool]["inputSchema"]["properties"][name][keyword]


def _field_names(entity: str) -> list[str]:
    return [field.name for field in reads._ENTITIES[entity]["fields"]]


def missing_fixes(text: str, needles: list) -> list:
    """The needles (a substring, or a compiled pattern for an example) that `text` lacks."""
    return [
        needle
        for needle in needles
        if (needle.search(text) is None if isinstance(needle, re.Pattern) else needle not in text)
    ]


def check_message(name: str, text: str, needles: list) -> None:
    lacking = missing_fixes(text, needles)
    assert not lacking, f"{name}: the message {text!r} does not name its fix; it lacks {lacking!r}"


def test_a_message_without_its_fix_fails_the_check_and_names_the_input() -> None:
    with pytest.raises(AssertionError) as caught:
        check_message("records_query with order", '{"error":{"message":"/order is not allowed"}}', ["order_by"])
    assert "records_query with order" in str(caught.value) and "order_by" in str(caught.value)
    check_message("a message that has it", "use order_by", ["order_by"])
    assert missing_fixes("2026-10-14", [UTC_EXAMPLE, DATE_EXAMPLE]) == [UTC_EXAMPLE]


# -- Reads -----------------------------------------------------------------------------------------------------

# (input name, tool, arguments, what the text must hold). A fix is a substring, or a pattern for an example.
READ_ROWS = [
    ("order instead of order_by", "records_query", {"entity": "record", "order": "title"}, ["order_by", "desc"]),
    ("sort instead of order_by", "records_query", {"entity": "record", "sort": "title"}, ["order_by", "desc"]),
    ("where instead of filters", "records_query", {"entity": "record", "where": {"title": "x"}}, ["filters"]),
    ("query instead of filters", "records_query", {"entity": "record", "query": "kettle"}, ["filters"]),
    (
        "an unknown argument lists the allowed ones",
        "get",
        {"entity": "record", "id": "rec-kettle", "page": 2},
        _property_names("get"),
    ),
    (
        "records_query without entity",
        "records_query",
        {},
        ["entity", *_enum_of("records_query", "entity")],
    ),
    ("get without entity", "get", {"id": "rec-kettle"}, ["entity", *_enum_of("get", "entity")]),
    (
        "an entity that doesn't exist",
        "records_query",
        {"entity": "records"},
        _enum_of("records_query", "entity"),
    ),
    (
        "limit above the maximum (records_query)",
        "records_query",
        {"entity": "record", "limit": 500},
        [re.compile(rf"\b{_limit_of('records_query', 'limit', 'maximum')}\b")],
    ),
    (
        "limit above the maximum (search)",
        "search",
        {"query": "kettle", "limit": 500},
        [re.compile(rf"\b{_limit_of('search', 'limit', 'maximum')}\b")],
    ),
    (
        "limit below the minimum",
        "records_query",
        {"entity": "record", "limit": 0},
        [re.compile(rf"\b{_limit_of('records_query', 'limit', 'minimum')}\b")],
    ),
    (
        "a date where a UTC timestamp is needed",
        "records_query",
        {"entity": "action", "filters": {"closed_at": {"gte": "14 Oct 2026"}}},
        [UTC_EXAMPLE],
    ),
    (
        "words where a date is needed",
        "records_query",
        {"entity": "action", "filters": {"due_date": "tomorrow"}},
        [DATE_EXAMPLE],
    ),
    (
        "an operator that doesn't exist",
        "records_query",
        {"entity": "record", "filters": {"title": {"like": "kettle"}}},
        sorted(reads._FIELD_OPS["text"]),
    ),
    (
        "an empty operator object",
        "records_query",
        {"entity": "record", "filters": {"title": {}}},
        sorted(reads._FIELD_OPS["text"]),
    ),
    (
        "contains on an id (14 October)",
        "records_query",
        {"entity": "record", "filters": {"id": {"contains": "x"}}},
        ["title", "search"],
    ),
    (
        "a value that isn't in the enum",
        "records_query",
        {"entity": "record", "filters": {"status": "gone"}},
        list(schemas.RECORD_STATUSES),
    ),
    (
        "a field that doesn't exist",
        "records_query",
        {"entity": "record", "filters": {"owner": "x"}},
        _field_names("record"),
    ),
    (
        "order_by a field that doesn't exist",
        "records_query",
        {"entity": "record", "order_by": "name"},
        _field_names("record"),
    ),
    (
        "order_by a list field",
        "records_query",
        {"entity": "action", "order_by": "owner_person_ids"},
        ["order_by", "title"],
    ),
    (
        "get of a row that isn't there",
        "get",
        {"entity": "record", "id": "rec-nope"},
        ["records_query", "search"],
    ),
    ("brief under an unknown folder", "brief", {"under": "nowhere"}, ["records_query on project"]),
    ("search under an unknown folder", "search", {"query": "kettle", "under": "nowhere"}, ["records_query on project"]),
    (
        "cites an unknown source",
        "records_query",
        {"entity": "record", "filters": {"cites": "src-nope"}},
        ["records_query on source"],
    ),
]


@pytest.fixture(scope="module")
def reading(tmp_path_factory) -> Path:
    return read_instance(tmp_path_factory.mktemp("reading"))


@pytest.mark.parametrize(("name", "tool", "arguments", "needles"), READ_ROWS, ids=[row[0] for row in READ_ROWS])
def test_a_bad_read_names_its_fix(reading, name, tool, arguments, needles) -> None:
    result = call(reading, tool, **arguments)
    assert result.is_error, f"{name}: {tool} {arguments} was accepted, so there is no message to check"
    check_message(f"{name} ({tool} {json.dumps(arguments)})", result.text, needles)


def test_a_bare_string_on_a_text_field_says_it_matched_exactly_and_how_to_match_part(reading) -> None:
    arguments = {"entity": "record", "filters": {"title": "kettle"}}
    result = call(reading, "records_query", **arguments)
    assert not result.is_error and json.loads(result.text)["total"] == 0
    check_message(
        f"a bare string filter (records_query {json.dumps(arguments)})",
        result.text,
        ["contains", "exactly", "title"],
    )
    # With a match there is nothing to explain, and an exact match on a different field isn't flagged.
    found = call(reading, "records_query", entity="record", filters={"title": "Descale the office kettle"})
    assert json.loads(found.text)["total"] == 1 and "hint" not in json.loads(found.text)


# -- Writes ----------------------------------------------------------------------------------------------------

NEW_ACTION = {
    "id": "act-fix-new",
    "title": "Chase the quote",
    "status": "open",
    "status_basis": "observed",
    "project_id": "general",
    "last_verified_at": "2026-10-01T00:00:00Z",
}


def _action(**changes) -> dict:
    return {**NEW_ACTION, **changes}


def _op(row: dict, *, base="absent", source: str = "src-note-anon", name: str = "upsert_action") -> dict:
    op = upsert_op(name, row, source)
    if base == "absent":
        del op["base"]
    else:
        op["base"] = base
    return op


# (input name, the op, what the refusal must hold). The refusal is the receipt's error text.
WRITE_ROWS = [
    (
        "an existing row without base",
        _op({"id": "act-fix-old", "title": "New title"}),
        ["version 1", '"base": 1'],
    ),
    (
        "a base for a row that doesn't exist",
        _op(_action(id="act-fix-ghost"), base=4),
        ["leave base out", "null"],
    ),
    (
        "required fields on create, all at once",
        _op({"id": "act-fix-bare", "title": "Bare"}),
        ["status", "status_basis", "project_id", "last_verified_at", *schemas.BASIS, *schemas.ACTION_STATUSES],
    ),
    (
        "an enum value that isn't allowed",
        _op(_action(status_basis="guess")),
        list(schemas.BASIS),
    ),
    ("a timestamp that isn't UTC", _op(_action(last_verified_at="14 Oct 2026")), [UTC_EXAMPLE]),
    ("a date that isn't a date", _op(_action(due_date="tomorrow", due_kind="deadline")), [DATE_EXAMPLE]),
    (
        "a property that doesn't exist",
        _op(_action(owner="per-dana-okafor")),
        ["owner_person_id", *schemas.OPS["upsert_action"]["row"]["properties"]],
    ),
    ("an op that doesn't exist", {"op": "upsert_actions", "row": {}}, sorted(schemas.OPS)),
    ("a title over the limit", _op(_action(title="x" * 200)), ["90"]),
    ("an id with no prefix", _op(_action(id="launch-review")), ["act-"]),
    (
        "done without closed_at (CHECK)",
        _op({"id": "act-fix-old", "status": "done"}, base=1),
        ["closed_at", "done", "dropped", "null"],
    ),
    (
        "a project that doesn't exist (FOREIGN KEY)",
        _op(_action(project_id="nope")),
        ["project_id", "nope", "does not exist", "records_query", "project"],
    ),
    (
        "a person that doesn't exist (FOREIGN KEY)",
        _op(_action(owner_person_id="per-nobody")),
        ["owner_person_id", "per-nobody", "records_query", "person"],
    ),
    (
        "a due date with no due kind (CHECK)",
        _op(_action(due_date="2026-10-01")),
        ["due_date", "due_kind", "together"],
    ),
    (
        "a verified time ahead of the clock",
        _op(_action(last_verified_at="2036-10-01T00:00:00Z")),
        ["current UTC time", UTC_EXAMPLE],
    ),
    (
        "a source that isn't in the store",
        _op(_action(), source="src-nowhere"),
        ["src-nowhere", "add_source"],
    ),
    (
        "a linked source that isn't in the store",
        {**_op(_action(id="act-fix-link")), "sources": [{"source_id": "src-nowhere", "locator": "p1"}]},
        ["src-nowhere", "add_source", "leave it out of sources"],
    ),
    (
        "original times from an agent that isn't a legacy import",
        _op(_action(id="act-fix-times", created_at="2025-01-01T00:00:00Z")),
        ["legacy_import", "leave it out"],
    ),
    (
        "speaker lines that aren't copied exactly from their source",
        {"op": "add_source", "row": {**source_op("src-lines-fix", "We will cut the access form next week.")["row"],
                                     "external_ref": "speaker-lines:src-note-anon"}},
        ["src-note-anon", "exactly", "character for character", "blank line"],
    ),
    (
        "speaker lines from a source that isn't in the store",
        {"op": "add_source", "row": {**source_op("src-lines-ghost", "We will cut the access form.")["row"],
                                     "external_ref": "speaker-lines:src-nowhere"}},
        ["src-nowhere", "add_source", "external_ref"],
    ),
]


@pytest.fixture(scope="module")
def writing(tmp_path_factory):
    instance = Path(init_instance("demo", tmp_path_factory.mktemp("writing"), config_text=TEST_CONFIG)["instance"])
    writer = Applier(instance)
    try:
        def send(packet: dict, *, dry_run: bool = False):
            return writer.apply(json.dumps(packet).encode(), dry_run=dry_run)

        assert seed(send).receipt["status"] == "applied"
        old = send(make_packet("tester", _op(_action(id="act-fix-old"), base=None)))
        assert old.receipt["status"] == "applied", old.receipt
        yield send
    finally:
        writer.close()


@pytest.mark.parametrize(("name", "op", "needles"), WRITE_ROWS, ids=[row[0] for row in WRITE_ROWS])
def test_a_bad_write_names_its_fix(writing, name, op, needles) -> None:
    outcome = writing(make_packet("tester", op), dry_run=True)
    receipt = outcome.receipt
    assert receipt["status"] in {"rejected", "conflict"}, f"{name}: the packet was {receipt['status']}, not refused"
    check_message(f"{name} ({json.dumps(op)})", receipt["error"], needles)


# -- base ------------------------------------------------------------------------------------------------------

def test_an_op_without_base_creates_a_row_that_isnt_there(writing) -> None:
    without = _op(_action(id="act-fix-created"))
    with_null = _op(_action(id="act-fix-created"), base=None)
    assert "base" not in without and with_null["base"] is None
    for op in (without, with_null):
        dry = writing(make_packet("tester", op), dry_run=True)
        assert dry.receipt["status"] == "applied" and dry.exit_code == 0, dry.receipt["error"]
        assert dry.receipt["targets"] == [{"table": "actions", "id": "act-fix-created", "action": "insert"}]


def test_a_stale_or_wrong_base_is_still_a_conflict(writing) -> None:
    stale = writing(make_packet("tester", _op({"id": "act-fix-old", "title": "Again"}, base=7)), dry_run=True)
    assert stale.receipt["status"] == "conflict" and "has moved on: base 7, now 1" in stale.receipt["error"]


def test_the_version_in_a_base_conflict_is_the_one_to_send(writing) -> None:
    refused = writing(make_packet("tester", _op({"id": "act-fix-old", "title": "Again"})), dry_run=True)
    assert refused.receipt["status"] == "conflict"
    match = re.search(r'send "base": (\d+)', refused.receipt["error"])
    assert match is not None, refused.receipt["error"]
    again = writing(make_packet("tester", _op({"id": "act-fix-old", "title": "Again"}, base=int(match.group(1)))), dry_run=True)
    assert again.receipt["status"] == "applied", again.receipt["error"]


def test_the_examples_in_messages_are_real_timestamps_and_dates(writing) -> None:
    refused = writing(make_packet("tester", _op(_action(last_verified_at="14 Oct 2026"))), dry_run=True)
    stamp = UTC_EXAMPLE.search(refused.receipt["error"])
    assert stamp is not None and datetime.strptime(stamp.group(0), "%Y-%m-%dT%H:%M:%SZ")
    # The example passes the check it comes with.
    sent = writing(make_packet("tester", _op(_action(id="act-fix-example", last_verified_at=stamp.group(0)))), dry_run=True)
    assert sent.receipt["status"] == "applied", sent.receipt["error"]
    dated = writing(make_packet("tester", _op(_action(due_date="tomorrow", due_kind="deadline"))), dry_run=True)
    day = DATE_EXAMPLE.search(dated.receipt["error"])
    assert day is not None and datetime.strptime(day.group(0), "%Y-%m-%d")


def test_a_source_without_a_name_in_it_is_a_source_op(writing) -> None:
    """The cited-source message names add_source, and add_source is an op in the same packet that works."""
    packet = make_packet(
        "tester",
        source_op("src-fix-fresh", "Chase the quote on Friday."),
        _op(_action(id="act-fix-fresh"), source="src-fix-fresh"),
    )
    assert writing(packet, dry_run=True).receipt["status"] == "applied"


def test_refusals_of_outside_input_name_the_limit() -> None:
    """Every entry point shows brain.ingress's message, which says what was wrong and the limit to stay
    under. (Imported here, so the tables above still run against code from before brain.ingress.)"""
    from brain import ingress

    def refusal(raw, **options) -> str:
        with pytest.raises(ingress.IngressError) as caught:
            ingress.loads(raw, what="packet", **options)
        return str(caught.value)

    check_message("too big", refusal(b" " * 11, max_bytes=10), ["packet", "larger than 10 bytes"])
    check_message("too big in MiB", str(ingress.IngressError(ingress.SIZE, "packet", 5 * 1024 * 1024)), ["larger than 5 MiB"])
    check_message("too deep", refusal(b"[" * 70 + b"]" * 70, max_nesting=64), ["nests deeper than 64 levels"])
    check_message("integer", refusal(b'{"n": 9223372036854775808}'), ["number outside the supported range"])
    check_message("float", refusal(b'{"n": 1e999}'), ["number outside the supported range"])
    check_message("surrogate", refusal(b'"\\ud800"'), ["lone surrogate escape"])
    check_message("encoding", refusal(b"\xff"), ["not valid UTF-8"])
    check_message("syntax", refusal(b"{nope"), ["not valid JSON"])


# -- Folder tokens --------------------------------------------------------------------------------------

FOLDER_READ_ROWS = [
    ("fact_get for a folder token", "fact_get", {"key": "demo.seats"}),
    ("get on a fact for a folder token", "get", {"entity": "fact", "id": "demo.seats"}),
    ("get on a system for a folder token", "get", {"entity": "system", "id": "sys-compliance"}),
    ("get on a claim for a folder token", "get", {"entity": "claim", "id": "clm-nope"}),
    ("records_query on facts for a folder token", "records_query", {"entity": "fact"}),
    ("records_query on systems for a folder token", "records_query", {"entity": "system"}),
]


@pytest.mark.parametrize(("name", "tool", "arguments"), FOLDER_READ_ROWS, ids=[row[0] for row in FOLDER_READ_ROWS])
def test_a_folder_token_refusal_names_its_fix(reading, name, tool, arguments) -> None:
    result = reads.run_tool(reading, tool, arguments, folders=("general",))
    assert result.is_error and result.code == "folder_token", f"{name}: {result.text}"
    check_message(name, result.text, ["only the folders general", "search", "records_query", "entity record"])


def test_the_refusals_of_a_folder_token_at_the_hub_name_their_fix(reading) -> None:
    from brain import advice
    from brain_hub import tokens

    check_message("--folder with write scope", advice.folder_write_error(), ["--scope read"])
    check_message("a folder that doesn't exist", advice.folder_missing_error("nowhere"), ["nowhere", "records_query on project"])
    check_message("receipts for a folder token", advice.folder_route_error("/v1/receipts"), ["whole-brain token", "--folder"])
    with pytest.raises(tokens.TokenError) as caught:
        tokens.issue(reading, "tester", "read write", "Writer", folders=["general"])
    check_message("tokens.issue with write scope", str(caught.value), ["--scope read"])
    with pytest.raises(tokens.TokenError) as caught:
        tokens.issue(reading, "tester", "read", "Reader", folders=["nowhere"])
    check_message("tokens.issue with a missing folder", str(caught.value), ["nowhere", "records_query on project"])
