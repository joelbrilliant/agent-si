from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from brain import advice, cli, reads, store
from brain.applier import Applier
from brain.init import init_instance
from brain.migrate import latest_version, migrate
from brain.schemas import ACTION_STATUSES
from tests.conftest import (
    NOW,
    TEST_CONFIG,
    call,
    file_sha,
    make_packet,
    read_instance,
    render_config,
    source_op,
    upsert_op,
    v1_instance,
)

_HIT_KEYS = ("entity", "id", "title", "status", "date", "snippet", "score")
# A hit found only through note text says so, right after score.
_NOTE_HIT_KEYS = (*_HIT_KEYS, "match")
IMPORTER = """
[agents.importer]
kind = "collector"
manual_sources = true
legacy_import = true
operations = "*"
"""


def _add_importer(instance: Path) -> None:
    """A legacy agent writes the long notes, whose insert is exempt from
    the note cap, so the reads these tests check are unchanged."""
    with (instance / "brain.toml").open("a", encoding="utf-8") as handle:
        handle.write(IMPORTER)


def _schema_keywords(node) -> set[str]:
    found: set[str] = set()

    def walk(schema) -> None:
        if not isinstance(schema, dict):
            return
        found.update(schema)
        for key, value in schema.items():
            if key == "properties" and isinstance(value, dict):
                for child in value.values():
                    walk(child)
            elif key == "items":
                walk(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        walk(item)

    walk(node)
    return found


def test_tool_schemas_use_only_supported_keywords() -> None:
    found: set[str] = set()
    for tool in reads.TOOLS:
        found.update(_schema_keywords(tool["inputSchema"]))
        if "outputSchema" in tool:
            found.update(_schema_keywords(tool["outputSchema"]))
    assert found <= set(reads.SCHEMA_KEYWORDS)


@pytest.mark.parametrize(
    ("tool", "arguments", "path"),
    [
        ("brief", {"extra": True}, "/extra"),
        ("search", {"query": "kettle", "extra": True}, "/extra"),
        ("search", {"query": 1}, "/query"),
        ("search", {"query": "kettle", "limit": 0}, "/limit"),
        ("search", {"query": "kettle", "limit": 99}, "/limit"),
        ("get", {"entity": "action", "id": "act-review", "extra": True}, "/extra"),
        ("get", {"entity": "action", "id": 1}, "/id"),
        ("get", {"entity": "action", "id": "act-review", "offset": -1}, "/offset"),
        ("get", {"entity": "action", "id": "act-review", "offset": 2_000_001}, "/offset"),
        ("fact_get", {"key": "demo.seats", "extra": True}, "/extra"),
        ("fact_get", {"key": 12}, "/key"),
        ("fact_get", {"key": "no"}, "/key"),
        ("records_query", {"entity": "action", "extra": True}, "/extra"),
        ("records_query", {"entity": 1}, "/entity"),
        ("records_query", {"entity": "action", "limit": 0}, "/limit"),
        ("records_query", {"entity": "action", "limit": 101}, "/limit"),
        ("records_query", {"entity": "action", "offset": 10_001}, "/offset"),
        ("signals", {"extra": True}, "/extra"),
        ("signals", {"limit": "many"}, "/limit"),
        ("signals", {"limit": 0}, "/limit"),
        ("signals", {"limit": 101}, "/limit"),
        ("signals", {"project_id": "x"}, "/project_id"),
    ],
)
def test_every_tool_validates_its_arguments(tool: str, arguments, path: str) -> None:
    result = reads.run_tool(Path("/no/such/instance"), tool, arguments, now=NOW)
    assert result.is_error is True
    assert result.code == "invalid_arguments"
    assert result.structured is None
    payload = json.loads(result.text)
    assert payload["error"]["path"] == path
    assert payload["error"]["code"] == "invalid_arguments"


def test_reads_refuse_an_old_or_missing_store(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "brain.toml").write_text(TEST_CONFIG, encoding="utf-8")
    absent = reads.run_tool(missing, "brief", {}, now=NOW)
    assert absent.is_error is True
    assert absent.code == "unavailable"
    assert absent.structured is None
    assert absent.text.startswith('{"error":')
    message = json.loads(absent.text)["error"]["message"]
    assert message == "no store at brain.sqlite3"
    assert str(missing) not in message

    old_root = tmp_path / "old"
    old_root.mkdir()
    old = v1_instance(old_root)
    version = reads.run_tool(old, "brief", {}, now=NOW)
    assert version.code == "unavailable"
    message = json.loads(version.text)["error"]["message"]
    assert message == f"store is at version 1, engine expects {latest_version()}; run brain migrate"

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "brain.toml").mkdir()
    (broken / "brain.sqlite3").write_bytes(b"")
    unreadable = reads.run_tool(broken, "brief", {}, now=NOW)
    assert unreadable.code == "unavailable"
    reason = json.loads(unreadable.text)["error"]["message"]
    assert reason.startswith("brain.toml: ")


_FACT_OUTPUT = next(tool["outputSchema"] for tool in reads.TOOLS if tool["name"] == "fact_get")

_FACT_EXPECTATIONS = {
    "demo.seats": {
        "value": 12,
        "py": int,
        "value_type": "int",
        "unit": "",
        "status": "current",
        "basis": "observed",
        "as_of": "2026-09-01",
        "last_verified_at": "2026-09-01T00:00:00Z",
        "age_days": 37,
        "stale_after_days": 30,
        "stale": True,
    },
    "demo.price": {
        "value": 49.5,
        "py": float,
        "value_type": "number",
        "unit": "AUD",
        "status": "current",
        "basis": "reported",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": 90,
        "stale": False,
    },
    "demo.region": {
        "value": "au",
        "py": str,
        "value_type": "enum",
        "unit": "",
        "status": "current",
        "basis": "observed",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": None,
        "stale": False,
    },
    "demo.launch": {
        "value": "2026-11-02",
        "py": str,
        "value_type": "date",
        "unit": "",
        "status": "current",
        "basis": "reported",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": None,
        "stale": False,
    },
    "demo.enabled": {
        "value": True,
        "py": bool,
        "value_type": "bool",
        "unit": "",
        "status": "current",
        "basis": "observed",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": None,
        "stale": False,
    },
    "demo.site": {
        "value": "https://example.com",
        "py": str,
        "value_type": "url",
        "unit": "",
        "status": "current",
        "basis": "observed",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": None,
        "stale": False,
    },
    "demo.motto": {
        "value": "Small steps",
        "py": str,
        "value_type": "text",
        "unit": "",
        "status": "current",
        "basis": "inferred",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": None,
        "stale": False,
    },
    "vendor.compliance.instance": {
        "value": "au",
        "py": str,
        "value_type": "enum",
        "unit": "",
        "status": "current",
        "basis": "observed",
        "as_of": "2026-10-01",
        "last_verified_at": "2026-10-01T00:00:00Z",
        "age_days": 7,
        "stale_after_days": 180,
        "stale": False,
    },
}


def test_fact_get_returns_a_typed_value_with_source_as_of_and_staleness(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    for key, expected in _FACT_EXPECTATIONS.items():
        result = call(instance, "fact_get", key=key)
        assert result.is_error is False, result.text
        assert result.code is None
        body = result.structured
        assert json.loads(result.text) == body
        assert reads.check_schema(_FACT_OUTPUT, body) == []
        assert body["query"] == key
        assert body["total"] == 1
        assert body["truncated"] is False
        assert body["unset"] == []
        fact = body["facts"][0]
        assert type(fact["value"]) is expected["py"]
        assert fact["value"] == expected["value"]
        for field in (
            "value_type",
            "unit",
            "status",
            "basis",
            "as_of",
            "last_verified_at",
            "age_days",
            "stale_after_days",
            "stale",
        ):
            assert fact[field] == expected[field], field
        assert fact["source"] == {
            "id": "src-read-notes",
            "kind": "user_note",
            "title": "Read fixture notes",
            "uri": "",
            "source_date": "2026-10-01",
            "locator": "synthetic",
        }


def test_fact_get_unknown_key_is_a_tool_error(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "fact_get", key="demo.missing")
    assert result.is_error is True
    assert result.code == "unknown_key"
    assert result.structured is None
    payload = json.loads(result.text)
    assert payload["error"]["code"] == "unknown_key"
    assert "demo.missing" in payload["error"]["message"]


def test_fact_get_registered_key_without_value_is_no_value(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "fact_get", key="ops.window")
    assert result.is_error is True
    assert result.code == "no_value"
    assert result.structured is None
    assert "ops.window" in json.loads(result.text)["error"]["message"]


def test_fact_get_prefix_returns_sorted_facts_and_unset_keys(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "fact_get", key="demo.*")
    retired = call(instance, "fact_get", key="demo.*", include_retired=True)
    assert result.is_error is False
    assert retired.structured == result.structured
    keys = [item["key"] for item in result.structured["facts"]]
    assert keys == [
        "demo.enabled",
        "demo.launch",
        "demo.motto",
        "demo.price",
        "demo.region",
        "demo.seats",
        "demo.site",
    ]
    assert result.structured["total"] == 7
    assert result.structured["truncated"] is False
    assert result.structured["unset"] == []
    assert reads.check_schema(_FACT_OUTPUT, result.structured) == []

    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_fact",
                        {
                            "key": "demo.motto",
                            "value": "Small steps",
                            "status": "retired",
                            "basis": "inferred",
                            "as_of": "2026-10-01",
                            "last_verified_at": "2026-10-01T00:00:00Z",
                        },
                        "src-read-notes",
                        base=1,
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied"
    hidden = call(instance, "fact_get", key="demo.*")
    shown = call(instance, "fact_get", key="demo.*", include_retired=True)
    assert "demo.motto" not in [item["key"] for item in hidden.structured["facts"]]
    assert hidden.structured["total"] == 6
    assert [item["key"] for item in shown.structured["facts"]] == keys
    assert shown.structured["facts"][2]["status"] == "retired"
    missing = call(instance, "fact_get", key="nope.*")
    assert missing.code == "unknown_key"
    assert "nope." in json.loads(missing.text)["error"]["message"]


def test_fact_get_shows_the_facts_open_signals(tmp_path: Path) -> None:
    """An agent asked about a fact reads fact_get and nothing else (a question about one fact's setting), so an open signal on the fact arrives with it under claims.signals, and a resolved one doesn't.
    A dispute's own conflict signal stays out: tests/test_truth.py pins the disputed answer key for key."""
    instance = read_instance(tmp_path)
    before = call(instance, "fact_get", key="demo.*").structured
    note = "Two sources describe the price differently. The store's note is record rec-price-note."
    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op("upsert_signal", {"id": "sig-price-note", "kind": "other", "subject_table": "facts",
                                                "subject_id": "demo.price", "detail": note, "basis": "observed"},
                              "src-read-notes"),
                    upsert_op("upsert_signal", {"id": "sig-price-old", "kind": "other", "subject_table": "facts",
                                                "subject_id": "demo.price", "detail": "An older note.",
                                                "status": "resolved", "resolved_at": "2026-10-01T00:00:00Z",
                                                "basis": "observed"},
                              "src-read-notes"),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    expected = {"items": [{"id": "sig-price-note", "kind": "other", "thread_id": None, "detail": note}], "total": 1}
    one = call(instance, "fact_get", key="demo.price").structured
    assert one["facts"][0]["claims"]["signals"] == expected
    assert reads.check_schema(_FACT_OUTPUT, one) == []
    after = call(instance, "fact_get", key="demo.*").structured
    by_key = {item["key"]: item for item in after["facts"]}
    assert by_key["demo.price"]["claims"]["signals"] == expected
    assert [item for item in after["facts"] if item["key"] != "demo.price"] == [
        item for item in before["facts"] if item["key"] != "demo.price"]  # no signal, no field: sizes unchanged
    assert reads.check_schema(_FACT_OUTPUT, after) == []


@pytest.mark.parametrize("key", ["Demo.Seats", "demo.", "*", "demo.*.x"])
def test_fact_get_rejects_malformed_keys(tmp_path: Path, key: str) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "fact_get", key=key)
    assert result.code == "invalid_arguments"
    assert json.loads(result.text)["error"]["path"] == "/key"


def _ids(result: reads.ToolResult) -> list[str]:
    assert result.is_error is False, result.text
    return [row["id"] for row in result.structured["rows"]]


def test_records_query_filters_with_typed_operators(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    assert _ids(call(instance, "records_query", entity="action", filters={"status": "waiting"})) == [
        "act-review"
    ]
    assert _ids(
        call(
            instance,
            "records_query",
            entity="action",
            filters={"status": {"in": ["open", "waiting"]}},
        )
    ) == ["act-board", "act-review"]
    assert _ids(
        call(instance, "records_query", entity="action", filters={"title": {"contains": "Board"}})
    ) == ["act-board"]
    assert _ids(call(instance, "records_query", entity="action", filters={"owner_person_id": None})) == [
        "act-board",
        "act-old-review",
        "act-review",
    ]
    assert _ids(
        call(
            instance,
            "records_query",
            entity="action",
            filters={"due_date": {"gte": "2026-10-06", "lt": "2026-10-20"}},
        )
    ) == ["act-review"]
    assert _ids(call(instance, "records_query", entity="thread_link", filters={"new_message": True})) == [
        "lnk-board",
        "lnk-review",
    ]


def test_records_query_rejects_unknown_fields(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    allowed = [
        "id",
        "title",
        "detail",
        "next_step",
        "status",
        "status_basis",
        "project_id",
        "owner_person_id",
        "waiting_on_person_id",
        "owner_person_ids",
        "waiting_on_person_ids",
        "due_date",
        "due_kind",
        "asked_on",
        "blocked_by",
        "record_id",
        "last_verified_at",
        "closed_at",
        "close_reason",
        "unchecked",
        "open",
        "cites",
        "source_id",
        "locator",
        "version",
        "created_at",
        "updated_at",
        "updated_by",
    ]
    cases = (
        ({"filters": {"nope": "x"}}, "/filters/nope"),
        ({"fields": ["id", "nope"]}, "/fields/1"),
        ({"order_by": "nope"}, "/order_by"),
    )
    for arguments, path in cases:
        result = call(instance, "records_query", entity="action", **arguments)
        assert result.code == "unknown_field"
        payload = json.loads(result.text)["error"]
        assert payload["path"] == path
        assert payload["allowed"] == allowed


def test_records_query_accepts_no_raw_sql(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    before = file_sha(instance / "brain.sqlite3")
    sql = call(instance, "records_query", entity="action", sql="SELECT * FROM actions")
    assert sql.code == "invalid_arguments"
    injected = call(
        instance,
        "records_query",
        entity="action",
        filters={"id; DROP TABLE facts": "x"},
    )
    assert injected.code == "unknown_field"
    literal = call(instance, "records_query", entity="action", filters={"title": "'x' OR 1=1"})
    assert literal.is_error is False
    assert literal.structured["rows"] == []
    assert file_sha(instance / "brain.sqlite3") == before


def test_records_query_validates_values_by_type(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    cases = (
        ("action", {"status": "nope"}, "/filters/status", list(ACTION_STATUSES)),
        ("checklist_item", {"sort_order": True}, "/filters/sort_order", None),
        ("action", {"due_date": "2026-02-30"}, "/filters/due_date", None),
        ("action", {"status": ["open"]}, "/filters/status", None),
        ("action", {"status": {"eq": "open", "in": ["open"]}}, "/filters/status", None),
        ("action", {"status": {"in": ["open"] * 51}}, "/filters/status", None),
        ("action", {"title": {"contains": "a" * 201}}, "/filters/title", None),
    )
    for entity, filters, path, allowed in cases:
        result = call(instance, "records_query", entity=entity, filters=filters)
        assert result.code == "invalid_arguments", filters
        payload = json.loads(result.text)["error"]
        assert payload["path"] == path
        if allowed is not None:
            assert payload["allowed"] == allowed


def test_records_query_projects_json_and_fact_values_but_never_filters_them(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_person",
                        {
                            "id": "per-dana-okafor",
                            "display_name": "Dana Okafor",
                            "aliases": ["Dana"],
                        },
                        "src-note-anon",
                        base=1,
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied"
    people = call(
        instance,
        "records_query",
        entity="person",
        filters={"id": "per-dana-okafor"},
        fields=["id", "aliases"],
    )
    assert people.is_error is False
    assert people.structured["rows"] == [{"id": "per-dana-okafor", "aliases": ["Dana"]}]
    facts = call(
        instance,
        "records_query",
        entity="fact",
        filters={"key": {"in": ["demo.enabled", "demo.seats"]}},
        fields=["key", "value", "value_type"],
    )
    assert facts.is_error is False
    assert facts.structured["rows"] == [
        {"key": "demo.enabled", "value": True, "value_type": "bool"},
        {"key": "demo.seats", "value": 12, "value_type": "int"},
    ]
    filtered = call(instance, "records_query", entity="person", filters={"aliases": "Dana"})
    ordered = call(instance, "records_query", entity="fact", order_by="value")
    assert filtered.code == "invalid_arguments"
    assert json.loads(filtered.text)["error"]["path"] == "/filters/aliases"
    assert ordered.code == "invalid_arguments"
    assert json.loads(ordered.text)["error"]["path"] == "/order_by"


def test_records_query_pages_and_formats(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    full = call(instance, "records_query", entity="action", limit=10)
    assert full.structured["total"] == 3
    assert full.structured["next_offset"] is None
    pages = []
    offset = 0
    while True:
        page = call(instance, "records_query", entity="action", limit=1, offset=offset)
        assert page.structured["total"] == 3
        pages.extend(row["id"] for row in page.structured["rows"])
        if page.structured["next_offset"] is None:
            break
        assert page.structured["next_offset"] == offset + len(page.structured["rows"])
        offset = page.structured["next_offset"]
    assert pages == [row["id"] for row in full.structured["rows"]]
    listed = call(
        instance,
        "records_query",
        entity="action",
        fields=["id", "status"],
        format="rows",
        limit=10,
    )
    assert listed.structured["fields"] == ["id", "status"]
    assert listed.structured["rows"] == [
        [row["id"], row["status"]] for row in full.structured["rows"]
    ]
    ordered = call(
        instance,
        "records_query",
        entity="action",
        order_by="project_id",
        desc=True,
        fields=["id", "project_id"],
        limit=10,
    )
    assert [row["id"] for row in ordered.structured["rows"]] == [
        "act-board",
        "act-old-review",
        "act-review",
    ]


def _hit_ids(result: reads.ToolResult) -> list[str]:
    assert result.is_error is False, result.text
    assert set(result.structured) == {
        "query",
        "match",
        "hits",
        "next_offset",
        "truncated",
        "unverified",
        "warnings",
    }
    for hit in result.structured["hits"]:
        assert tuple(hit) == (_NOTE_HIT_KEYS if "match" in hit else _HIT_KEYS)
        assert hit.get("match", "note") == "note"
        assert isinstance(hit["score"], (int, float)) and not isinstance(hit["score"], bool)
        assert len(hit["snippet"]) <= 300
    return [hit["id"] for hit in result.structured["hits"]]


def test_search_finds_open_rows_and_hides_closed_ones(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    kettle = call(instance, "search", query="descale kettle")
    ids = _hit_ids(kettle)
    assert ids[0] == "rec-kettle"
    assert "rec-kettle-old" not in ids
    first = kettle.structured["hits"][0]
    assert first["entity"] == "record"
    assert first["title"] == "Descale the office kettle"
    assert first["status"] == "active"
    assert first["snippet"] == "[Descale] the office [kettle]"
    assert kettle.structured["match"] == '"descale" OR "kettle"'
    assert kettle.structured["query"] == "descale kettle"
    assert kettle.structured["unverified"] == 0
    assert kettle.structured["warnings"] == []
    review = call(instance, "search", query="launch review")
    found = set(_hit_ids(review))
    assert {"act-review", "thr-review"} <= found
    assert "act-old-review" not in found
    closed_kettle = set(_hit_ids(call(instance, "search", query="descale kettle", include_closed=True)))
    closed_review = set(_hit_ids(call(instance, "search", query="launch review", include_closed=True)))
    assert "rec-kettle-old" in closed_kettle
    assert "act-old-review" in closed_review


_RELAXED_WARNING = "No result had every word; these match some of them."


def test_search_with_every_word_matching_is_not_relaxed(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "search", query="descale kettle")
    assert list(result.structured) == [
        "query", "match", "hits", "next_offset", "truncated", "unverified", "warnings",
    ]
    assert result.structured["match"] == '"descale" OR "kettle"'
    assert result.structured["warnings"] == []
    assert _hit_ids(result)[0] == "rec-kettle"
    # Running out of pages is not a miss: an offset past the all-words hits stays empty and unlabelled.
    past = call(instance, "search", query="descale kettle", offset=50)
    assert past.structured["hits"] == []
    assert "relaxed" not in past.structured
    assert past.structured["match"] == '"descale" OR "kettle"'


def test_search_falls_back_to_any_word_and_says_so(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "search", query="descale quokka")
    assert result.is_error is False, result.text
    assert list(result.structured) == [
        "query", "match", "relaxed", "hits", "next_offset", "truncated", "unverified", "warnings",
    ]
    assert result.structured["relaxed"] is True
    assert result.structured["match"] == '"descale" OR "quokka"'
    assert result.structured["warnings"] == [_RELAXED_WARNING]
    assert [hit["id"] for hit in result.structured["hits"]][0] == "rec-kettle"
    assert result.structured["hits"][0]["snippet"] == "[Descale] the office kettle"


def test_search_matching_nothing_stays_empty_without_a_relaxed_flag(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "search", query="quokka wombat")
    assert result.is_error is False, result.text
    assert result.structured["hits"] == []
    assert "relaxed" not in result.structured
    assert result.structured["match"] == '"quokka" OR "wombat"'
    assert result.structured["warnings"] == []


def test_search_fallback_keeps_limit_offset_entities_and_closed_filter(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    query = "review board quokka"
    full = call(instance, "search", query=query, limit=25)
    assert full.structured["relaxed"] is True
    ids = [hit["id"] for hit in full.structured["hits"]]
    assert len(ids) > 2
    first = call(instance, "search", query=query, limit=1)
    assert first.structured["relaxed"] is True and first.structured["next_offset"] == 1
    assert [hit["id"] for hit in first.structured["hits"]] == ids[:1]
    second = call(instance, "search", query=query, limit=1, offset=1)
    assert second.structured["relaxed"] is True and second.structured["warnings"] == [_RELAXED_WARNING]
    assert [hit["id"] for hit in second.structured["hits"]] == ids[1:2]
    actions = call(instance, "search", query=query, entities=["action"], limit=25)
    assert actions.structured["relaxed"] is True
    assert {hit["entity"] for hit in actions.structured["hits"]} == {"action"}
    assert [hit["id"] for hit in actions.structured["hits"]] == [i for i in ids if i.startswith("act-")]
    kettle = call(instance, "search", query="descale quokka", limit=25)
    assert "rec-kettle-old" not in [hit["id"] for hit in kettle.structured["hits"]]
    closed = call(instance, "search", query="descale quokka", include_closed=True, limit=25)
    assert "rec-kettle-old" in [hit["id"] for hit in closed.structured["hits"]]
    assert closed.structured["relaxed"] is True


def test_search_folds_diacritics_and_prefixes(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    assert "rec-kettle" in _hit_ids(call(instance, "search", query="cafe"))
    prefixed = call(instance, "search", query="desc*")
    assert "rec-kettle" in _hit_ids(prefixed)
    assert prefixed.structured["match"] == '"desc"*'
    plain = call(instance, "search", query="d")
    starred = call(instance, "search", query="d*")
    assert starred.structured["match"] == plain.structured["match"] == '"d"'
    assert _hit_ids(starred) == _hit_ids(plain)


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("title:kettle", '"title kettle"'),
        ("kettle OR board", '"kettle" AND "OR" AND "board"'),
        ("NEAR(a b)", '"NEAR a" AND "b"'),
        ('"unbalanced', '"unbalanced"'),
        ("(kettle", '"kettle"'),
        ("kettle*", '"kettle"*'),
        ("k*", '"k"'),
        (
            "one two three four five six seven eight nine ten eleven twelve thirteen",
            '"one" AND "two" AND "three" AND "four" AND "five" AND "six" AND "seven" '
            'AND "eight" AND "nine" AND "ten" AND "eleven" AND "twelve"',
        ),
    ),
)
def test_search_query_builder_neutralises_fts_syntax(tmp_path: Path, raw: str, expected: str) -> None:
    assert reads.fts_query(raw) == expected
    result = call(read_instance(tmp_path), "search", query=raw)
    assert result.is_error is False, result.text


def test_search_rejects_a_query_with_no_words(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    for query in ("***", '"" ()'):
        result = call(instance, "search", query=query)
        assert result.code == "bad_query"
        assert json.loads(result.text)["error"]["message"] == advice.search_needs_words_error()


def test_search_pages_and_filters_by_entity(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    first = call(instance, "search", query="the", limit=1)
    assert first.structured["next_offset"] == 1
    assert len(first.structured["hits"]) == 1
    second = call(instance, "search", query="the", limit=1, offset=1)
    assert _hit_ids(second) != _hit_ids(first)
    actions = call(instance, "search", query="the", entities=["action"], limit=25)
    hits = actions.structured["hits"]
    assert hits
    assert {hit["entity"] for hit in hits} == {"action"}


def test_search_ignores_forged_and_stale_index_rows(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute(
            "INSERT INTO search_index (entity, ref_id, status, title, body) VALUES (?, ?, ?, ?, ?)",
            ("record", "rec-forged", "active", "zzforged note", "zzforged"),
        )
        con.execute(
            "UPDATE search_index SET title = ? WHERE entity = 'record' AND ref_id = 'rec-kettle'",
            ("zzforged title",),
        )
        con.commit()
    finally:
        con.close()
    result = call(instance, "search", query="zzforged")
    assert _hit_ids(result) == []
    assert result.structured["unverified"] == 2
    assert result.structured["warnings"] == [
        "2 hits failed verification. Run brain check, then brain search-rebuild."
    ]


def test_get_returns_the_row_its_source_and_related_rows(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "get", entity="action", id="act-review")
    assert result.is_error is False, result.text
    body = result.structured
    assert list(body) == ["entity", "id", "row", "source", "related", "truncated"]
    assert body["entity"] == "action" and body["id"] == "act-review"
    assert body["row"]["title"] == "Launch review follow-up"
    assert body["row"]["status"] == "waiting"
    assert body["row"]["project_id"] == "general"
    assert body["source"] == {
        "id": "src-read-notes",
        "kind": "user_note",
        "title": "Read fixture notes",
        "uri": "",
        "source_date": "2026-10-01",
        "author_person_id": None,
        "locator": "synthetic",
    }
    assert body["related"]["thread_links"]["items"] == [
        {
            "id": "lnk-review",
            "thread_id": "thr-review",
            "role": "awaiting_reply",
            "status": "active",
            "reply_state": "replied",
            "new_message": True,
            "thread_subject": "Launch review",
        }
    ]
    assert body["related"]["signals"]["items"] == [
        {
            "id": "sig-review-reply",
            "kind": "reply",
            "thread_id": "thr-review",
            "detail": "",
        }
    ]
    decision = call(instance, "get", entity="decision", id="dec-access-form")
    assert decision.is_error is False, decision.text
    assert decision.structured["related"]["decided_by"] == {
        "id": "per-jade",
        "display_name": "Jade",
    }


def test_get_pages_long_text(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    _add_importer(instance)
    body = "a" * 20_000
    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet(
                    "importer",
                    upsert_op(
                        "upsert_record",
                        {
                            "id": "rec-long",
                            "kind": "note",
                            "title": "Long kettle note",
                            "body": body,
                        },
                        "src-read-notes",
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    pages = []
    offset = 0
    while True:
        page = call(instance, "get", entity="record", id="rec-long", field="body", offset=offset)
        assert page.is_error is False, page.text
        assert list(page.structured) == [
            "entity",
            "id",
            "field",
            "text",
            "offset",
            "returned",
            "total",
            "next_offset",
            "notes_label",
        ]
        assert page.structured["notes_label"] == "Written by an agent, unsourced. Not a fact."
        assert page.structured["total"] == 20000
        assert page.structured["offset"] == offset
        assert page.structured["returned"] == len(page.structured["text"])
        assert page.structured["returned"] <= 8000
        pages.append(page.structured["text"])
        if page.structured["next_offset"] is None:
            break
        assert page.structured["next_offset"] == offset + page.structured["returned"]
        offset = page.structured["next_offset"]
    assert "".join(pages) == body
    assert [len(part) for part in pages] == [8000, 8000, 4000]
    bare = call(instance, "get", entity="record", id="rec-long", offset=1)
    assert bare.code == "invalid_arguments"
    assert json.loads(bare.text)["error"]["path"] == "/offset"
    past = call(instance, "get", entity="record", id="rec-long", field="body", offset=20001)
    assert past.code == "invalid_arguments"
    assert json.loads(past.text)["error"]["path"] == "/offset"
    wrong = call(instance, "get", entity="record", id="rec-long", field="content")
    assert wrong.code == "invalid_arguments"
    assert json.loads(wrong.text)["error"]["path"] == "/field"


def test_get_history_for_records_and_facts_only(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    writer = Applier(instance)
    try:
        first = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_record",
                        {"id": "rec-kettle", "title": "Descale the office kettle today"},
                        "src-read-notes",
                        base=1,
                    ),
                    upsert_op(
                        "upsert_fact",
                        {"key": "demo.seats", "value": 13, "basis": "observed", "as_of": "2026-10-01",
                         "quote": "Later we had 13 seats", "value_raw": "13"},
                        "src-read-notes",
                        base=1,
                    ),
                )
            ).encode()
        )
        second = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_record",
                        {"id": "rec-kettle", "title": "Descale the office kettle weekly"},
                        "src-read-notes",
                        base=2,
                    ),
                    upsert_op(
                        "upsert_fact",
                        {"key": "demo.seats", "value": 14, "basis": "observed", "as_of": "2026-10-01",
                         "quote": "Then we had 14 seats", "value_raw": "14"},
                        "src-read-notes",
                        base=2,
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert first.receipt["status"] == "applied", first.receipt
    assert second.receipt["status"] == "applied", second.receipt
    record = call(instance, "get", entity="record", id="rec-kettle", include_history=True)
    assert record.is_error is False, record.text
    history = record.structured["history"]
    assert history["total"] == 3
    assert [item["version"] for item in history["items"]] == [3, 2, 1]
    assert "body" not in history["items"][0]
    assert history["items"][0]["change"] == "updated"
    fact = call(instance, "get", entity="fact", id="demo.seats", include_history=True)
    assert fact.is_error is False, fact.text
    fact_history = fact.structured["history"]
    assert [item["version"] for item in fact_history["items"]] == [3, 2, 1]
    assert fact_history["items"][0]["value"] == 14
    assert fact.structured["row"]["value"] == 14
    refused = call(instance, "get", entity="action", id="act-review", include_history=True)
    assert refused.code == "invalid_arguments"
    assert json.loads(refused.text)["error"]["path"] == "/include_history"


def test_get_unknown_row_is_not_found(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "get", entity="action", id="act-missing")
    assert result.code == "not_found"
    assert json.loads(result.text)["error"]["message"] == advice.not_found_error("action", "act-missing")
    missing_fact = call(instance, "get", entity="fact", id="nope.missing")
    assert missing_fact.code == "not_found"
    assert json.loads(missing_fact.text)["error"]["message"] == advice.not_found_error("fact", "nope.missing")
    empty = call(instance, "get", entity="fact", id="ops.window")
    assert empty.is_error is False, empty.text
    assert empty.structured["row"] is None


def test_get_result_stays_under_64_kib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = read_instance(tmp_path)
    _add_importer(instance)
    char = "\U0002000b"
    body = char * 20_000
    title = char * 90
    ops = [
        upsert_op(
            "upsert_record",
            {"id": "rec-bulk", "kind": "note", "title": "Bulk kettle note", "body": body},
            "src-read-notes",
        )
    ]
    for index in range(20):
        ops.append(
            upsert_op(
                "upsert_action",
                {
                    "id": f"act-bulk{index:02d}",
                    "title": title,
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "record_id": "rec-bulk",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-read-notes",
            )
        )
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(make_packet("importer", *ops)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    full = call(instance, "get", entity="record", id="rec-bulk")
    assert full.is_error is False, full.text
    assert len(full.text.encode()) <= 65_536
    assert full.structured["truncated"] is False
    # A record's body page is in notes.
    assert len(full.structured["notes"]["fields"]["body"]["text"]) == 8000
    assert len(full.structured["related"]["actions"]["items"]) == 20
    monkeypatch.setattr(reads, "GET_BUDGET", 20_000)
    shrunk = call(instance, "get", entity="record", id="rec-bulk")
    assert shrunk.is_error is False, shrunk.text
    assert len(shrunk.text.encode()) <= 20_000
    assert shrunk.structured["truncated"] is True
    assert len(shrunk.structured["related"]["actions"]["items"]) == 5
    assert shrunk.structured["related"]["actions"]["total"] == 20
    assert len(shrunk.structured["notes"]["fields"]["body"]["text"]) == 2000
    assert shrunk.structured["pages"]["body"]["returned"] == 2000


def test_signals_derive_reply_and_owes_reply_from_linked_threads(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    result = call(instance, "signals")
    assert result.is_error is False, result.text
    body = result.structured
    assert list(body["counts"]) == ["reply", "new_message", "owes_reply", "stored"]
    assert body["counts"] == {"reply": 1, "new_message": 0, "owes_reply": 1, "stored": 1}
    assert body["unknown_outbound"] == 1
    assert body["total"] == 3
    assert body["truncated"] is False
    reply, owes, stored = body["signals"]
    assert reply["kind"] == "reply" and reply["derived"] is True
    assert reply["subject"] == {"entity": "action", "id": "act-review", "title": "Launch review follow-up"}
    assert reply["thread_id"] == "thr-review"
    assert reply["link_id"] == "lnk-review"
    assert reply["stored_signal_id"] == "sig-review-reply"
    assert owes["kind"] == "owes_reply" and owes["derived"] is True
    assert owes["subject"]["id"] == "act-board"
    assert owes["link_id"] == "lnk-board"
    assert owes["stored_signal_id"] is None
    assert stored["derived"] is False
    assert stored["signal_id"] == "sig-board-note"
    assert stored["kind"] == "other"
    assert stored["subject"]["id"] == "act-board"
    assert all(item.get("link_id") != "lnk-hidden" for item in body["signals"])


def test_signals_come_from_linked_threads_only(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    before = call(instance, "signals")
    assert before.is_error is False, before.text
    assert "thr-other" not in before.text
    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_person",
                        {"id": "per-later", "display_name": "Later Person"},
                        "src-note-anon",
                    ),
                    upsert_op(
                        "upsert_thread",
                        {
                            "id": "thr-later",
                            "channel": "email",
                            "account": "work",
                            "external_id": "thread-later",
                            "subject": "A later unlinked note",
                            "last_inbound_at": "2026-10-07T09:00:00Z",
                            "outbound_seen": False,
                        },
                        "src-read-notes",
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    after = call(instance, "signals")
    assert after.structured == before.structured
    assert "thr-later" not in after.text


def test_signals_sql_never_mentions_people(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    seen: list[str] = []

    def opener(db):
        con = store.connect_reader(db)
        con.set_trace_callback(seen.append)
        return con

    result = reads.run_tool(instance, "signals", {}, now=NOW, opener=opener)
    assert result.is_error is False, result.text
    for sql in (reads.SIGNALS_SQL, reads.STORED_SIGNALS_SQL):
        lowered = sql.lower()
        assert "person" not in lowered
        assert "people" not in lowered
    allowed = {"BEGIN", "COMMIT", "PRAGMA user_version", reads.SIGNALS_SQL, reads.STORED_SIGNALS_SQL}
    assert [sql.strip() for sql in seen] == [
        "BEGIN",
        "PRAGMA user_version",
        reads.SIGNALS_SQL,
        reads.STORED_SIGNALS_SQL,
        "COMMIT",
    ]
    assert set(sql.strip() for sql in seen) <= allowed


def test_signals_filter_by_kind_project_and_subject(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    owes = call(instance, "signals", kinds=["owes_reply"])
    assert owes.is_error is False, owes.text
    assert [item["subject"]["id"] for item in owes.structured["signals"]] == ["act-board"]
    assert owes.structured["counts"] == {"reply": 0, "new_message": 0, "owes_reply": 1, "stored": 0}
    ops = call(instance, "signals", project_id="ops")
    assert [(item["kind"], item["subject"]["id"]) for item in ops.structured["signals"]] == [
        ("owes_reply", "act-board"),
        ("other", "act-board"),
    ]
    assert ops.structured["counts"] == {"reply": 0, "new_message": 0, "owes_reply": 1, "stored": 1}
    review = call(instance, "signals", subject={"entity": "action", "id": "act-review"})
    assert len(review.structured["signals"]) == 1
    assert review.structured["signals"][0]["stored_signal_id"] == "sig-review-reply"
    assert review.structured["counts"] == {"reply": 1, "new_message": 0, "owes_reply": 0, "stored": 0}


def test_signals_new_message_on_event_links(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    writer = Applier(instance)
    try:
        created = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_thread",
                        {
                            "id": "thr-event",
                            "channel": "email",
                            "account": "work",
                            "external_id": "thread-event",
                            "subject": "Board event",
                            "last_inbound_at": "2026-10-01T00:00:00Z",
                            "outbound_seen": False,
                        },
                        "src-read-notes",
                    ),
                    upsert_op(
                        "upsert_thread_link",
                        {
                            "id": "lnk-event",
                            "action_id": "act-board",
                            "thread_id": "thr-event",
                            "role": "event",
                            "since": "2026-10-02T00:00:00Z",
                        },
                        "src-read-notes",
                    ),
                )
            ).encode()
        )
        assert created.receipt["status"] == "applied", created.receipt
        quiet = call(instance, "signals")
        assert all(item.get("link_id") != "lnk-event" for item in quiet.structured["signals"])
        updated = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_thread",
                        {"id": "thr-event", "last_inbound_at": "2026-10-07T00:00:00Z"},
                        "src-read-notes",
                        base=1,
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert updated.receipt["status"] == "applied", updated.receipt
    loud = call(instance, "signals")
    assert loud.is_error is False, loud.text
    matches = [item for item in loud.structured["signals"] if item.get("link_id") == "lnk-event"]
    assert len(matches) == 1
    assert matches[0]["kind"] == "new_message"
    assert matches[0]["derived"] is True
    assert matches[0]["subject"]["id"] == "act-board"


_BRIEF_PREFIX = [
    f"demo brain, schema {latest_version()}, today 2026-10-08 (UTC).",
    "Workstreams: general, ops. Projects: 0 active.",
    "Actions: 2 open, 1 waiting, 0 blocked. Overdue: 0 yours, 1 waiting on others.",
    "Checklist: 2 items, 1 done, 1 waiting, 0 blocked.",
    "Facts: 8 current, 0 disputed.",
    "1 fact stale: demo.seats (37d).",
    "Signals: 1 reply, 0 new message, 1 owes reply, 1 stored, 1 unknown.",
]
_BRIEF_SECTIONS = [
    "Due by 2026-10-15: 1 open (1 overdue).",
    "- act-review 2026-10-06 overdue: Launch review follow-up [general]",
    "Waiting: 1 action.",
    "- act-review since ?: Launch review follow-up; waiting on no one named",
    "Active projects: none.",
]


def test_brief_matches_the_seeded_store(tmp_path: Path) -> None:
    result = call(read_instance(tmp_path), "brief")
    assert result.is_error is False, result.text
    assert result.structured is None
    assert result.text.endswith("\n")
    lines = result.text.splitlines()
    assert lines[:-1] == _BRIEF_PREFIX + _BRIEF_SECTIONS
    assert re.fullmatch(r"Last change: \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ by tester\.", lines[-1])


def test_brief_fits_4_kb_and_32_lines(tmp_path: Path) -> None:
    workstreams = [
        {"id": "general", "title": "General", "description": "", "sort_order": 1},
        *[
            {"id": f"w{index:02d}", "title": "W", "description": "", "sort_order": index}
            for index in range(2, 13)
        ],
    ]
    fact_keys = [
        {"key": f"bulk.f{index}", "value_type": "text", "stale_after_days": 1}
        for index in range(9)
    ]
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(workstreams=workstreams, fact_keys=fact_keys))["instance"])
    # The strict facts quote their source.
    sentences = " ".join(f"The fact is value {index}." for index in range(9))
    ops = [source_op("src-bulk", f"Bulk notes for the stale facts. {sentences}")]
    for index in range(9):
        ops.append(
            upsert_op(
                "upsert_fact",
                {
                    "key": f"bulk.f{index}",
                    "value": f"value {index}",
                    "basis": "observed",
                    "as_of": "2026-01-01",
                    "last_verified_at": f"2026-01-{index + 1:02d}T00:00:00Z",
                    "quote": f"The fact is value {index}",
                    "value_raw": f"value {index}",
                },
                "src-bulk",
            )
        )
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(make_packet("tester", *ops)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    result = call(instance, "brief")
    assert result.is_error is False, result.text
    lines = result.text.splitlines()
    assert len(lines) <= 32
    assert all(len(line.encode()) <= 200 for line in lines)
    assert len(result.text.encode()) <= 4096
    stale = next(line for line in lines if line.endswith(" more.") or "stale" in line and "fact" in line)
    assert stale.endswith(", +4 more.")
    workstream = next(line for line in lines if line.startswith("Workstreams:"))
    assert ", +4 more" in workstream


def test_brief_reports_pause_and_keeps_reading(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    (instance / "PAUSED").write_text("hold\n", encoding="utf-8")
    result = call(instance, "brief")
    assert result.is_error is False, result.text
    assert result.text.splitlines()[1] == (
        "PAUSED. Writes are stopped until a person investigates and deletes PAUSED."
    )
    answered = (
        call(instance, "search", query="kettle"),
        call(instance, "get", entity="action", id="act-review"),
        call(instance, "fact_get", key="demo.seats"),
        call(instance, "records_query", entity="action"),
        call(instance, "signals"),
    )
    assert all(item.is_error is False for item in answered)


def test_brief_reports_an_unrebuilt_index(tmp_path: Path) -> None:
    instance = v1_instance(tmp_path)
    migrate(instance / "brain.sqlite3")
    unrebuilt = call(instance, "brief")
    assert unrebuilt.is_error is False, unrebuilt.text
    assert "Search index has 0 of 5 rows indexed. Run brain search-rebuild." in unrebuilt.text.splitlines()
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("DROP TRIGGER search_records_au")
        con.commit()
    finally:
        con.close()
    damaged = call(instance, "brief")
    assert damaged.is_error is False, damaged.text
    assert "Search index is damaged. Run brain check." in damaged.text.splitlines()
    search = call(instance, "search", query="kettle")
    assert search.code == "unavailable"


def _budget_instance(tmp_path: Path) -> Path:
    char = "\U0002000b"
    fact_keys = [{"key": f"bulk.f{index:02d}", "value_type": "text"} for index in range(30)]
    instance = Path(
        init_instance("demo", tmp_path, config_text=render_config(fact_keys=fact_keys))["instance"]
    )
    _add_importer(instance)
    # A strict text fact's value sits inside its quote of at most 500 characters,
    # so the facts are 480 characters; the long notes are written by the legacy importer (item 4).
    value = char * 480
    ops = [source_op("src-bulk", f"Bulk notes. {value} said here.")]
    for index in range(30):
        ops.append(
            upsert_op(
                "upsert_record",
                {
                    "id": f"rec-b{index:02d}",
                    "kind": "note",
                    "title": "bulkword " + char * 191,
                    "body": char * 400,
                },
                "src-bulk",
            )
        )
        ops.append(
            upsert_op(
                "upsert_action",
                {
                    "id": f"act-b{index:02d}",
                    "title": f"Bulk {index}",
                    "detail": char * 2000,
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-bulk",
            )
        )
        ops.append(
            upsert_op(
                "upsert_fact",
                {
                    "key": f"bulk.f{index:02d}",
                    "value": value,
                    "basis": "observed",
                    "as_of": "2026-10-01",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                    "quote": f"{value} said here",
                    "value_raw": value,
                },
                "src-bulk",
            )
        )
        ops.append(
            upsert_op(
                "upsert_signal",
                {
                    "id": f"sig-b{index:02d}",
                    "kind": "other",
                    "subject_table": "actions",
                    "subject_id": "act-b00",
                    "detail": char * 500,
                    "basis": "inferred",
                },
                "src-bulk",
            )
        )
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(make_packet("importer", *ops)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    return instance


def _budget_call(instance: Path, tool: str) -> reads.ToolResult:
    if tool == "search":
        return call(instance, "search", query="bulkword", limit=25)
    if tool == "fact_get":
        return call(instance, "fact_get", key="bulk.*")
    if tool == "records_query":
        return call(instance, "records_query", entity="action", fields=["id", "detail"], limit=25)
    return call(instance, "signals", limit=100)


@pytest.mark.parametrize("tool", ("search", "fact_get", "records_query", "signals"))
def test_list_results_stop_at_the_list_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    instance = _budget_instance(tmp_path)
    full = _budget_call(instance, tool)
    assert full.is_error is False, full.text
    assert len(full.text.encode()) <= 60_000
    monkeypatch.setattr(reads, "LIST_BUDGET", 20_000)
    shrunk = _budget_call(instance, tool)
    assert shrunk.is_error is False, shrunk.text
    assert len(shrunk.text.encode()) <= 20_000
    assert shrunk.structured["truncated"] is True
    if tool in {"search", "records_query"}:
        rows = shrunk.structured["hits" if tool == "search" else "rows"]
        assert shrunk.structured["next_offset"] == len(rows)


def test_standard_questions_take_at_most_two_calls_after_brief(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    assert call(instance, "brief").is_error is False
    seats = call(instance, "fact_get", key="demo.seats")
    assert seats.structured["facts"][0]["value"] == 12
    compliance = call(instance, "fact_get", key="vendor.compliance.instance")
    assert compliance.structured["facts"][0]["value"] == "au"
    waiting = call(instance, "signals")
    assert any(item["kind"] == "owes_reply" for item in waiting.structured["signals"])
    ops = call(instance, "records_query", entity="action", filters={"project_id": "ops"})
    assert "act-board" in [row["id"] for row in ops.structured["rows"]]
    found = call(instance, "search", query="descale kettle")
    assert found.structured["hits"][0]["id"] == "rec-kettle"
    kettle = call(instance, "get", entity="record", id=found.structured["hits"][0]["id"])
    assert "citric acid" in kettle.structured["notes"]["fields"]["body"]["text"]
    decided = call(instance, "search", query="access form")
    assert any(hit["id"] == "dec-access-form" for hit in decided.structured["hits"])
    decision = call(instance, "get", entity="decision", id="dec-access-form")
    assert decision.structured["row"]["title"] == "Cut the access form"


_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_PRAGMA,
}


def test_every_tool_only_reads(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    db = instance / "brain.sqlite3"
    before = file_sha(db)
    listing = sorted(path.name for path in instance.iterdir())
    observed: set[int] = set()
    pragma_values: list = []

    def opener(path):
        con = store.connect_reader(path)

        def authorizer(action, arg1, arg2, db_name, source):
            observed.add(action)
            if action == sqlite3.SQLITE_PRAGMA:
                pragma_values.append(arg2)
            return sqlite3.SQLITE_OK

        con.set_authorizer(authorizer)
        return con

    calls = (
        ("brief", {}),
        ("search", {"query": "kettle"}),
        ("get", {"entity": "action", "id": "act-review"}),
        ("fact_get", {"key": "demo.seats"}),
        ("records_query", {"entity": "action"}),
        ("signals", {}),
    )
    for name, arguments in calls:
        result = reads.run_tool(instance, name, arguments, now=NOW, opener=opener)
        assert result.is_error is False, result.text
    assert observed <= _READ_ACTIONS
    assert pragma_values
    assert all(value is None for value in pragma_values)
    assert file_sha(db) == before
    assert sorted(path.name for path in instance.iterdir()) == listing


def test_brain_read_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    instance = read_instance(tmp_path)
    assert cli.main(["read", "brief", "--instance", str(instance)]) == 0
    assert capsys.readouterr().out.startswith("demo brain")
    assert cli.main(["read", "fact_get", "--instance", str(instance), "--args", '{"key": "nope.missing"}']) == 1
    payload = capsys.readouterr().out
    assert "unknown_key" in payload
    assert cli.main(["read", "brief", "--instance", str(instance), "--args", "[1]"]) == 2
    assert cli.main(["read", "no-such-tool", "--instance", str(instance)]) == 2
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "brain.toml").write_text((instance / "brain.toml").read_text(), encoding="utf-8")
    assert cli.main(["read", "brief", "--instance", str(bare)]) == 5


def test_brain_read_rejects_deeply_nested_args(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = read_instance(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("[" * 200_000))
    code = cli.main(["read", "brief", "--instance", str(instance), "--args", "-"])
    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "brain: --args nests deeper than" in captured.err  # the ingress names the limit (class 6)
    assert "Traceback" not in captured.err


def test_reader_connection_refuses_writes(instance: Path) -> None:
    db = instance / "brain.sqlite3"
    before = file_sha(db)
    listing = sorted(path.name for path in instance.iterdir())
    con = store.connect_reader(db)
    try:
        for sql in (
            "INSERT INTO sources (id) VALUES ('src-forged')",
            "DELETE FROM search_index",
            "INSERT INTO search_index(search_index) VALUES('optimize')",
            "CREATE TEMP TABLE forged(x)",
            f"PRAGMA user_version = {latest_version()}",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                con.execute(sql)
    finally:
        con.close()
    assert file_sha(db) == before
    assert sorted(path.name for path in instance.iterdir()) == listing
