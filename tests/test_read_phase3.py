from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from pathlib import Path

from brain import reads, server, validate
from brain.applier import Applier
from brain.init import init_instance
from tests.conftest import (
    NOW,
    TEST_CONFIG,
    TOOLS_LIST_CEILING,
    call,
    make_packet,
    read_instance,
    render_config,
    source_op,
    tools_list_budgets,
    upsert_op,
)

_CLOSED = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


def test_reads_and_validate_share_one_pointer_helper() -> None:
    assert not hasattr(reads, "_pointer")
    assert not hasattr(reads, "_type_name")
    assert reads.json_pointer is validate.json_pointer
    value = {"a/b~c": 1}
    read_paths = [item["path"] for item in reads.check_schema(_CLOSED, value)]
    validate_paths = [item["path"] for item in validate.check(value, _CLOSED)]
    assert "/a~1b~0c" in read_paths
    assert "/a~1b~0c" in validate_paths


def _tool_message(text: str) -> str:
    return json.loads(text)["error"]["message"]


def test_missing_store_message_holds_no_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "brain.toml").write_text(TEST_CONFIG, encoding="utf-8")
    absent = reads.run_tool(missing, "brief", {}, now=NOW)
    assert absent.is_error is True
    assert absent.code == "unavailable"
    message = _tool_message(absent.text)
    assert message == "no store at brain.sqlite3"
    assert str(missing) not in message

    stdin = io.BytesIO(
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "brief", "arguments": {}},
                }
            )
            + "\n"
        ).encode()
    )
    stdout = io.BytesIO()
    code = server.serve(missing, stdin, stdout, now=NOW)
    assert code == 0
    response = json.loads(stdout.getvalue().decode())
    assert "error" not in response
    body = response["result"]
    assert body["isError"] is True
    served = body["content"][0]["text"]
    assert _tool_message(served) == "no store at brain.sqlite3"
    assert str(missing) not in served


def _signals_line(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("Signals:"))


def _apply(instance: Path, *ops) -> None:
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(make_packet("tester", *ops)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt


def _crowded_brief_instance(root: Path) -> Path:
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
    instance = Path(
        init_instance(
            "demo",
            root,
            config_text=render_config(workstreams=workstreams, fact_keys=fact_keys),
        )["instance"]
    )
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
    ops.extend(
        [
            upsert_op(
                "upsert_thread",
                {
                    "id": "thr-quiet",
                    "channel": "email",
                    "account": "work",
                    "external_id": "thread-quiet",
                    "subject": "Quiet thread",
                    "last_inbound_at": "2026-10-01T00:00:00Z",
                    "outbound_seen": False,
                },
                "src-bulk",
            ),
            upsert_op(
                "upsert_action",
                {
                    "id": "act-quiet",
                    "title": "Reply later",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-bulk",
            ),
            upsert_op(
                "upsert_thread_link",
                {
                    "id": "lnk-quiet",
                    "action_id": "act-quiet",
                    "thread_id": "thr-quiet",
                    "role": "owes_reply",
                    "since": "2026-10-01T00:00:00Z",
                },
                "src-bulk",
            ),
        ]
    )
    _apply(instance, *ops)
    (instance / "PAUSED").write_text("hold\n", encoding="utf-8")
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        rowid = con.execute("SELECT rowid FROM search_index LIMIT 1").fetchone()[0]
        con.execute("DELETE FROM search_index WHERE rowid = ?", (rowid,))
        con.commit()
    finally:
        con.close()
    return instance


def test_brief_counts_unknown_outbound_within_budget(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    seeded = call(instance, "brief")
    assert seeded.is_error is False, seeded.text
    assert _signals_line(seeded.text) == (
        "Signals: 1 reply, 0 new message, 1 owes reply, 1 stored, 1 unknown."
    )

    _apply(
        instance,
        upsert_op(
            "upsert_thread",
            {"id": "thr-hidden", "outbound_seen": True},
            "src-read-notes",
            base=1,
        ),
    )
    visible = call(instance, "brief")
    assert _signals_line(visible.text) == (
        "Signals: 1 reply, 0 new message, 2 owes reply, 1 stored."
    )

    root = tmp_path / "crowded"
    root.mkdir()
    crowded = _crowded_brief_instance(root)
    result = call(crowded, "brief")
    assert result.is_error is False, result.text
    lines = result.text.splitlines()
    assert len(lines) == 13
    assert all(len(line.encode()) <= 200 for line in lines)
    assert len(result.text.encode()) <= 4096
    assert lines[8].endswith(", 1 unknown.")


_PHASE2_TOOLS_SHA = "1ef6ff3081fb40a9ab882660465655e1d02128250612cc98ae315d2c1f38ea0e"
# Later releases rewrote these two descriptions. Putting them back recovers the earlier list exactly.
_PHASE2_DESCRIPTIONS = {
    "brief": (
        "Start here, once per session. A short summary of this brain covering workstreams, "
        "open work, stale facts, signals and anything blocking writes."
    ),
    "search": (
        "Full-text search over records, decisions, actions, checklist items, projects, people, "
        "systems, fact keys, threads and sources. Returns ids and snippets. Use get for the full "
        "row. Closed items are left out unless include_closed is true."
    ),
}
# Links trimmed get's description and reworded one records_query sentence to pay for under and cites.
_PRE_LINKS_GET = (
    "One row by entity and id, with its source and related rows. Long text (a record body or source content) "
    "comes in pages, so pass field and offset for the next page."
)
_LINKS_QUERY_NOW = " No SQL; an unknown field's error lists the allowed ones. cites takes a source id, as primary or link."
_LINKS_QUERY_BEFORE = " No SQL is accepted, and an unknown field is an error that lists the allowed ones."
_MEETING_FIELDS = [
    "id",
    "thread_id",
    "project_id",
    "transcript_source_id",
    "title",
    "starts_at",
    "ends_at",
    "all_day",
    "status",
    "attendee_count",
    "source_id",
    "locator",
    "version",
    "created_at",
    "updated_at",
    "updated_by",
]


def _calendar_thread(thread_id: str, external_id: str, subject: str) -> dict:
    return upsert_op(
        "upsert_thread",
        {
            "id": thread_id,
            "channel": "calendar",
            "account": "work-cal",
            "external_id": external_id,
            "subject": subject,
            "last_inbound_at": "2026-10-01T09:00:00Z",
            "outbound_seen": True,
        },
        "src-read-notes",
    )


def _meeting(meeting_id: str, thread_id: str, **row) -> dict:
    body = {
        "id": meeting_id,
        "thread_id": thread_id,
        "title": row.pop("title"),
        "starts_at": row.pop("starts_at"),
        "ends_at": row.pop("ends_at"),
        "status": row.pop("status"),
    }
    body.update(row)
    return upsert_op("upsert_meeting", body, "src-read-notes")


def test_records_query_and_get_cover_meetings(tmp_path: Path) -> None:
    instance = read_instance(tmp_path)
    _apply(
        instance,
        _calendar_thread("thr-cal-zeta", "event-zeta", "Later session"),
        _calendar_thread("thr-cal-alpha", "event-alpha", "Planning session"),
        _calendar_thread("thr-cal-beta", "event-beta", "Tentative session"),
        _calendar_thread("thr-cal-gamma", "event-gamma", "Far session"),
        _calendar_thread("thr-cal-delta", "event-delta", "All day session"),
        _calendar_thread("thr-cal-epsilon", "event-epsilon", "Other project"),
        _calendar_thread("thr-cal-blank", "event-blank", "Unassigned session"),
        _meeting(
            "mtg-zeta",
            "thr-cal-zeta",
            title="Later session",
            starts_at="2026-10-08T12:00:00Z",
            ends_at="2026-10-08T13:00:00Z",
            status="confirmed",
            all_day=False,
            project_id="general",
        ),
        _meeting(
            "mtg-alpha",
            "thr-cal-alpha",
            title="Planning session",
            starts_at="2026-10-08T09:00:00Z",
            ends_at="2026-10-08T10:00:00Z",
            status="confirmed",
            all_day=False,
            project_id="general",
            transcript_source_id="src-read-notes",
            attendee_count=2,
        ),
        _meeting(
            "mtg-beta",
            "thr-cal-beta",
            title="Tentative session",
            starts_at="2026-10-08T15:00:00Z",
            ends_at="2026-10-08T16:00:00Z",
            status="tentative",
            all_day=False,
            project_id="general",
        ),
        _meeting(
            "mtg-gamma",
            "thr-cal-gamma",
            title="Far session",
            starts_at="2026-10-20T09:00:00Z",
            ends_at="2026-10-20T10:00:00Z",
            status="confirmed",
            all_day=False,
            project_id="general",
        ),
        _meeting(
            "mtg-delta",
            "thr-cal-delta",
            title="All day session",
            starts_at="2026-10-08T00:00:00Z",
            ends_at="2026-10-09T00:00:00Z",
            status="confirmed",
            all_day=True,
            project_id="general",
        ),
        _meeting(
            "mtg-epsilon",
            "thr-cal-epsilon",
            title="Other project",
            starts_at="2026-10-08T11:00:00Z",
            ends_at="2026-10-08T11:30:00Z",
            status="confirmed",
            all_day=False,
            project_id="ops",
        ),
        _meeting(
            "mtg-blank",
            "thr-cal-blank",
            title="Unassigned session",
            starts_at="2026-10-08T08:00:00Z",
            ends_at="2026-10-08T08:30:00Z",
            status="confirmed",
            all_day=False,
        ),
    )
    queried = call(
        instance,
        "records_query",
        entity="meeting",
        filters={
            "status": "confirmed",
            "starts_at": {"gte": "2026-10-08T00:00:00Z", "lt": "2026-10-10T00:00:00Z"},
            "all_day": False,
            "project_id": "general",
        },
    )
    assert queried.is_error is False, queried.text
    assert queried.structured["fields"] == [
        "id",
        "thread_id",
        "title",
        "starts_at",
        "ends_at",
        "status",
        "project_id",
        "version",
    ]
    assert [row["id"] for row in queried.structured["rows"]] == ["mtg-alpha", "mtg-zeta"]
    flagged = call(
        instance,
        "records_query",
        entity="meeting",
        fields=["id", "all_day"],
        filters={"all_day": True},
    )
    assert flagged.structured["rows"] == [{"id": "mtg-delta", "all_day": True}]
    unknown = call(instance, "records_query", entity="meeting", filters={"nope": "x"})
    assert unknown.code == "unknown_field"
    payload = json.loads(unknown.text)["error"]
    assert payload["path"] == "/filters/nope"
    assert payload["allowed"] == _MEETING_FIELDS

    got = call(instance, "get", entity="meeting", id="mtg-alpha")
    assert got.is_error is False, got.text
    assert got.structured["row"]["all_day"] is False
    assert got.structured["related"] == {
        "thread": {
            "id": "thr-cal-alpha",
            "channel": "calendar",
            "account": "work-cal",
            "subject": "Planning session",
            "last_inbound_at": "2026-10-01T09:00:00Z",
            "last_outbound_at": None,
            "outbound_seen": True,
        },
        "project": {"id": "general", "kind": "workstream", "title": "General", "status": "active"},
        "transcript": {
            "id": "src-read-notes",
            "kind": "user_note",
            "title": "Read fixture notes",
            "source_date": "2026-10-01",
        },
    }
    blank = call(instance, "get", entity="meeting", id="mtg-blank")
    assert blank.structured["row"]["all_day"] is False
    assert blank.structured["related"]["project"] is None
    assert blank.structured["related"]["transcript"] is None
    assert blank.structured["related"]["thread"]["id"] == "thr-cal-blank"
    allday = call(instance, "get", entity="meeting", id="mtg-delta")
    assert allday.structured["row"]["all_day"] is True
    thread = call(instance, "get", entity="thread", id="thr-review")
    assert set(thread.structured["related"]) == {"thread_links"}


# fact_get as engine 0.6.1 defined it, before later releases extended it.
_FACT_GET_061 = json.loads(
    r'''{"name":"fact_get","title":"Get a fact","description":"A typed fact value with its source, as_of date and staleness. Pass an exact key, or a prefix ending in .* for up to 25 facts. An unknown key is an error.","inputSchema":{"type":"object","properties":{"key":{"type":"string","minLength":3,"maxLength":122,"pattern":"^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*\\.([a-z][a-z0-9_]*|\\*)$"},"include_retired":{"type":"boolean","default":false}},"additionalProperties":false,"required":["key"]},"outputSchema":{"type":"object","properties":{"query":{"type":"string"},"facts":{"type":"array","items":{"type":"object","properties":{"key":{"type":"string"},"value":{"type":["string","integer","number","boolean"]},"value_type":{"enum":["text","int","number","date","bool","url","enum"]},"unit":{"type":"string"},"status":{"enum":["current","disputed","retired"]},"basis":{"enum":["observed","reported","inferred"]},"as_of":{"type":"string","format":"date"},"last_verified_at":{"type":"string","format":"date-time"},"age_days":{"type":"integer"},"stale_after_days":{"type":["integer","null"]},"stale":{"type":"boolean"},"version":{"type":"integer"},"source":{"type":"object","properties":{"id":{"type":"string"},"kind":{"type":"string"},"title":{"type":"string"},"uri":{"type":"string"},"source_date":{"type":"string"},"locator":{"type":"string"}},"additionalProperties":false,"required":["id","kind","title","uri","source_date","locator"]}},"additionalProperties":false,"required":["key","value","value_type","unit","status","basis","as_of","last_verified_at","age_days","stale_after_days","stale","version","source"]}},"total":{"type":"integer"},"truncated":{"type":"boolean"},"unset":{"type":"array","items":{"type":"string"}}},"additionalProperties":false,"required":["query","facts","total","truncated","unset"]},"annotations":{"readOnlyHint":true,"openWorldHint":false}}'''
)


def test_tools_list_grows_only_by_the_meeting_entity() -> None:
    compact = json.dumps({"tools": reads.TOOLS}, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(compact) <= TOOLS_LIST_CEILING
    assert len(compact) == tools_list_budgets()["tools_list"]
    listed = json.loads(compact)
    # One store: put back search's query rule, drop since and until, and take session out of search and get. The
    # two descriptions go back below, with the earlier ones.
    search_tool = next(tool for tool in listed["tools"] if tool["name"] == "search")
    props = search_tool["inputSchema"]["properties"]
    assert props["query"] == {"type": "string", "maxLength": 200}
    props["query"] = {"type": "string", "minLength": 1, "maxLength": 200}
    for name in ("since", "until"):
        assert props.pop(name) == {"type": "string"}
    props["entities"]["items"]["enum"].remove("session")
    assert props["entities"]["maxItems"] == 11
    props["entities"]["maxItems"] = 10
    assert "required" not in search_tool["inputSchema"]
    search_tool["inputSchema"]["required"] = ["query"]
    next(tool for tool in listed["tools"] if tool["name"] == "get")["inputSchema"]["properties"]["entity"]["enum"].remove("session")
    # Put back the earlier records_query description, from before the open sentence.
    query = next(tool for tool in listed["tools"] if tool["name"] == "records_query")
    # Links: put back get's and records_query's descriptions from before links, and drop under from brief
    # and search. Their descriptions go back below, with the earlier ones.
    get = next(tool for tool in listed["tools"] if tool["name"] == "get")
    get["description"] = _PRE_LINKS_GET
    for name in ("brief", "search"):
        tool = next(tool for tool in listed["tools"] if tool["name"] == name)
        assert tool["inputSchema"]["properties"].pop("under") == {"type": "string", "maxLength": 40}
    assert _LINKS_QUERY_NOW in query["description"]
    query["description"] = query["description"].replace(_LINKS_QUERY_NOW, _LINKS_QUERY_BEFORE)
    # Folders: put back the records_query description from before the under operator.
    assert ", or under (a project and every project in it)." in query["description"]
    query["description"] = query["description"].replace(", or under (a project and every project in it).", ".")
    assert query["description"].endswith(" For actions, open: true matches every open status.")
    query["description"] = query["description"].removesuffix(" For actions, open: true matches every open status.")
    # Put back the 0.6.1 fact_get and take claim out of get's entity enum.
    listed["tools"] = [_FACT_GET_061 if tool["name"] == "fact_get" else tool for tool in listed["tools"]]
    get = next(tool for tool in listed["tools"] if tool["name"] == "get")
    get["inputSchema"]["properties"]["entity"]["enum"].remove("claim")
    removed = 0
    for tool in listed["tools"]:
        if tool["name"] in _PHASE2_DESCRIPTIONS:
            tool["description"] = _PHASE2_DESCRIPTIONS[tool["name"]]
        entity = tool["inputSchema"]["properties"].get("entity")
        if not isinstance(entity, dict) or "meeting" not in entity.get("enum", []):
            continue
        assert entity["enum"][entity["enum"].index("thread_link") + 1] == "meeting"
        entity["enum"].remove("meeting")
        removed += 1
    assert removed == 2
    stripped = json.dumps(listed, ensure_ascii=False, separators=(",", ":")).encode()
    assert hashlib.sha256(stripped).hexdigest() == _PHASE2_TOOLS_SHA
    search = next(tool for tool in reads.TOOLS if tool["name"] == "search")
    assert "meeting" not in search["inputSchema"]["properties"]["entities"]["items"]["enum"]
