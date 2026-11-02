"""Links with sources, the reads.

get lists the sources a row cites and the rows a source is cited by, records_query returns a record's summary and
takes a cites filter, search finds a record by its summary, and brief and search take under, a folder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import advice, reads, schemas
from brain.applier import Applier
from brain.init import init_instance
from tests.conftest import NOW, make_packet, render_config, source_op, upsert_op

AGENTS = {"tester": {"operations": "*", "author_principal": True}}
SOURCES = [f"src-{index:02d}" for index in range(22)]


def _apply(instance: Path, *ops) -> None:
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(make_packet("tester", *ops)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt["error"]


def _call(instance: Path, tool: str, **arguments) -> reads.ToolResult:
    return reads.run_tool(instance, tool, arguments, now=NOW)


def _ok(instance: Path, tool: str, **arguments) -> dict:
    result = _call(instance, tool, **arguments)
    assert result.is_error is False, result.text
    return result.structured


def _error(instance: Path, tool: str, **arguments) -> dict:
    result = _call(instance, tool, **arguments)
    assert result.is_error is True, result.text
    return json.loads(result.text)["error"]


def _linked(op: dict, *items: dict) -> dict:
    return {**op, "sources": list(items)}


def _project(pid: str, parent: str | None = None) -> dict:
    row = {"id": pid, "kind": "project", "title": pid.replace("-", " ").title(), "status": "active"}
    if parent is not None:
        row["parent_id"] = parent
    return upsert_op("upsert_project", row, "src-00")


def _action(aid: str, project: str, **fields) -> dict:
    row = {"id": aid, "title": f"Action {aid}", "status": "open", "status_basis": "reported", "project_id": project,
           "last_verified_at": "2026-10-05T00:00:00Z", **fields}
    return upsert_op("upsert_action", row, "src-00")


ROWS = {
    "record": ("upsert_record", {"id": "rec-kettle", "kind": "note", "title": "Kettle care", "body": "Descale it.",
                                 "summary": "Monthly descaling with citric acid.", "project_id": "general"}),
    "action": ("upsert_action", {"id": "act-filters", "title": "Order filters", "status": "open",
                                 "status_basis": "reported", "project_id": "general",
                                 "last_verified_at": "2026-10-05T00:00:00Z"}),
    "checklist_item": ("upsert_checklist_item", {"id": "chk-filters", "project_id": "general", "title": "Filters",
                                                 "state": "not_started", "state_basis": "reported",
                                                 "last_verified_at": "2026-10-05T00:00:00Z"}),
    "decision": ("upsert_decision", {"id": "dec-descale", "title": "Descale monthly",
                                     "quote": "Descale the kettle every month", "decided_on": "2026-10-01",
                                     "decided_by_person_id": "per-kim", "status": "in_effect",
                                     "project_id": "general"}),
}


@pytest.fixture
def inst(tmp_path: Path) -> Path:
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(agents=AGENTS))["instance"])
    _apply(instance, *[source_op(source_id, f"Descale the kettle every month. Note {source_id}.",
                                 title=f"Note {source_id}", date="2026-10-01") for source_id in SOURCES])
    _apply(instance, upsert_op("upsert_person", {"id": "per-kim", "display_name": "Kim"}, "src-00"))
    for name, row in ROWS.values():
        _apply(instance, _linked(upsert_op(name, row, "src-00", locator="para 1"),
                                 {"source_id": "src-01", "locator": "line 2"},
                                 {"source_id": "src-02", "locator": "line 3", "label": "status"}))
    return instance


# -- get: a row's sources and a source's cited_by -------------------------------------------------------------------


@pytest.mark.parametrize("entity", list(ROWS))
def test_get_lists_the_primary_source_first_then_each_link(inst: Path, entity: str) -> None:
    got = _ok(inst, "get", entity=entity, id=ROWS[entity][1]["id"])
    sources = got["related"]["sources"]
    assert sources["total"] == 3
    assert sources["items"][0] == {"label": "primary", "id": "src-00", "locator": "para 1"}
    assert got["source"]["id"] == "src-00"  # the primary in full, as before
    assert sources["items"][1:] == [
        {"label": "cites", "id": "src-01", "locator": "line 2", "title": "Note src-01", "kind": "user_note",
         "source_date": "2026-10-01", "author_person_id": None},
        {"label": "status", "id": "src-02", "locator": "line 3", "title": "Note src-02", "kind": "user_note",
         "source_date": "2026-10-01", "author_person_id": None},
    ]


def test_a_row_without_links_lists_only_its_primary(inst: Path) -> None:
    _apply(inst, upsert_op("upsert_record", {"id": "rec-plain", "kind": "note", "title": "Plain", "body": "x"},
                           "src-03", locator="p9"))
    got = _ok(inst, "get", entity="record", id="rec-plain")
    assert got["related"]["sources"] == {"items": [{"label": "primary", "id": "src-03", "locator": "p9"}], "total": 1}


def test_get_shows_at_most_20_sources_with_the_total(inst: Path) -> None:
    many = [{"source_id": source_id, "locator": "p1"} for source_id in SOURCES[1:21]]
    _apply(inst, _linked(upsert_op("upsert_record", {"id": "rec-many", "kind": "note", "title": "Many",
                                                     "body": "x"}, "src-00"), *many))
    sources = _ok(inst, "get", entity="record", id="rec-many")["related"]["sources"]
    assert sources["total"] == 21 and len(sources["items"]) == 20
    assert sources["items"][0]["label"] == "primary"


def test_get_on_a_source_lists_the_rows_citing_it_either_way(inst: Path) -> None:
    cited = _ok(inst, "get", entity="source", id="src-01")["related"]["cited_by"]
    assert cited == {
        "items": [
            {"entity": "action", "id": "act-filters", "title": "Order filters", "label": "cites"},
            {"entity": "checklist_item", "id": "chk-filters", "title": "Filters", "label": "cites"},
            {"entity": "decision", "id": "dec-descale", "title": "Descale monthly", "label": "cites"},
            {"entity": "record", "id": "rec-kettle", "title": "Kettle care", "label": "cites"},
        ],
        "total": 4,
    }
    primary = _ok(inst, "get", entity="source", id="src-00")["related"]["cited_by"]
    assert {(item["entity"], item["label"]) for item in primary["items"]} >= {
        ("record", "primary"), ("action", "primary"), ("checklist_item", "primary"), ("decision", "primary")}
    assert all(item["label"] == "primary" for item in primary["items"])
    assert _ok(inst, "get", entity="source", id="src-21")["related"]["cited_by"] == {"items": [], "total": 0}


def test_cited_by_shows_at_most_20_with_the_total(inst: Path) -> None:
    _apply(inst, *[_linked(_action(f"act-{index:02d}", "general"), {"source_id": "src-21", "locator": "p1"})
                   for index in range(23)])
    cited = _ok(inst, "get", entity="source", id="src-21")["related"]["cited_by"]
    assert cited["total"] == 23 and len(cited["items"]) == 20


# -- summary ------------------------------------------------------------------------------------------------------


def test_get_and_records_query_return_a_records_summary(inst: Path) -> None:
    got = _ok(inst, "get", entity="record", id="rec-kettle")
    assert got["row"]["summary"] == "Monthly descaling with citric acid."
    queried = _ok(inst, "records_query", entity="record", filters={"id": "rec-kettle"})
    assert "summary" in queried["fields"]
    assert queried["rows"][0]["summary"] == "Monthly descaling with citric acid."
    found = _ok(inst, "records_query", entity="record", filters={"summary": {"contains": "citric"}})
    assert [row["id"] for row in found["rows"]] == ["rec-kettle"]


def test_search_finds_a_record_by_its_summary_as_gated_text(inst: Path) -> None:
    hits = _ok(inst, "search", query="citric", entities=["record"])["hits"]
    assert [hit["id"] for hit in hits] == ["rec-kettle"]
    assert "match" not in hits[0]  # the summary is gated text, not a note
    _apply(inst, upsert_op("upsert_record", {"id": "rec-kettle", "summary": "Vinegar works too."}, "src-00", base=1))
    assert _ok(inst, "search", query="citric", entities=["record"])["hits"] == []
    assert [hit["id"] for hit in _ok(inst, "search", query="vinegar", entities=["record"])["hits"]] == ["rec-kettle"]


def test_a_record_without_a_summary_keeps_its_search_document(inst: Path) -> None:
    _apply(inst, upsert_op("upsert_record", {"id": "rec-plain", "kind": "note", "title": "Plain", "body": "Body."},
                           "src-03"))
    writer = Applier(inst)
    try:
        doc = writer.con.execute("SELECT body FROM search_docs WHERE entity = 'record' AND ref_id = 'rec-plain'")
        assert doc.fetchone()[0] == "Body."
    finally:
        writer.close()


# -- records_query cites ------------------------------------------------------------------------------------------


def test_every_linked_entity_takes_a_cites_filter() -> None:
    linked = {entity for entity, table in schemas.ENTITY_TABLE.items() if table in schemas.LINK_TABLES}
    with_cites = {entity for entity, spec in reads._ENTITIES.items()
                  if any(field.name == "cites" for field in spec["fields"])}
    assert with_cites == linked, "a link subject without a cites field in reads._ENTITIES (src/brain/reads.py)"


@pytest.mark.parametrize("entity", list(ROWS))
def test_cites_matches_a_primary_or_a_linked_source(inst: Path, entity: str) -> None:
    row_id = ROWS[entity][1]["id"]
    for source_id in ("src-00", "src-01", "src-02"):
        found = _ok(inst, "records_query", entity=entity, filters={"cites": source_id})
        assert [row["id"] for row in found["rows"]] == [row_id], source_id
    assert _ok(inst, "records_query", entity=entity, filters={"cites": "src-03"})["rows"] == []
    either = _ok(inst, "records_query", entity=entity, filters={"cites": {"in": ["src-03", "src-02"]}})
    assert [row["id"] for row in either["rows"]] == [row_id]
    shown = _ok(inst, "records_query", entity=entity, filters={"id": row_id}, fields=["id", "cites"])
    assert shown["rows"] == [{"id": row_id, "cites": ["src-00", "src-01", "src-02"]}]


def test_cites_refuses_an_unknown_source_and_a_wrong_operator(inst: Path) -> None:
    error = _error(inst, "records_query", entity="record", filters={"cites": "src-none"})
    assert error["code"] == "not_found" and "records_query on source" in error["message"]
    assert error["path"] == "/filters/cites"
    error = _error(inst, "records_query", entity="record", filters={"cites": {"contains": "src"}})
    assert error["code"] == "invalid_arguments" and "eq" in error["message"] and "in" in error["message"]
    error = _error(inst, "records_query", entity="record", order_by="cites")
    assert error["code"] == "invalid_arguments" and error["path"] == "/order_by"


# -- brief and search under a folder ------------------------------------------------------------------------------


@pytest.fixture
def folders(inst: Path) -> Path:
    _apply(inst, _project("home"), _project("kitchen", "home"), _project("garden"))
    _apply(inst, _action("act-kitchen", "kitchen", due_date="2026-10-01", due_kind="deadline"), _action("act-home", "home"),
           _action("act-garden", "garden", due_date="2026-10-01", due_kind="deadline"),
           upsert_op("upsert_record", {"id": "rec-shed", "kind": "note", "title": "Shed kettle", "body": "kettle",
                                       "project_id": "garden"}, "src-00"),
           upsert_op("upsert_record", {"id": "rec-sink", "kind": "note", "title": "Sink kettle", "body": "kettle",
                                       "project_id": "kitchen"}, "src-00"))
    return inst


def _brief_lines(instance: Path, **arguments) -> list[str]:
    result = _call(instance, "brief", **arguments)
    assert result.is_error is False, result.text
    return result.text.splitlines()


def test_brief_under_counts_and_lists_only_that_folder(folders: Path) -> None:
    whole = _brief_lines(folders)
    lines = _brief_lines(folders, under="home")
    assert lines[0] == whole[0]
    assert lines[1] == "Folder home: Home, with 1 folder inside. Counts and lists cover only it; facts are brain-wide."
    assert "Workstreams: none. Projects: 2 active." in lines
    assert any(line.startswith("Actions: 2 open,") for line in lines)
    due = [line for line in lines if line.startswith("- act-")]
    assert due == ["- act-kitchen 2026-10-01 overdue: Action act-kitchen [kitchen]"]
    projects = [line for line in lines if line.startswith(("- home:", "- kitchen:", "- garden:"))]
    assert [line.split(":")[0] for line in projects] == ["- home", "- kitchen"]
    assert any(line.startswith("- garden:") for line in whole)
    general = _brief_lines(folders, under="general")
    # general is a workstream holding only act-filters; the folder actions are elsewhere.
    assert "Workstreams: general. Projects: 0 active." in general
    assert any(line.startswith("Actions: 1 open,") for line in general)


def test_brief_and_search_refuse_an_unknown_folder_naming_the_fix(folders: Path) -> None:
    for tool, arguments in (("brief", {}), ("search", {"query": "kettle"})):
        error = _error(folders, tool, under="nowhere", **arguments)
        assert error["code"] == "not_found" and error["path"] == "/under"
        assert error["message"] == advice.not_found_error("project", "nowhere")
        assert "records_query on project" in error["message"]


def test_search_under_returns_only_hits_inside_the_folder(folders: Path) -> None:
    every = {hit["id"] for hit in _ok(folders, "search", query="kettle", limit=25)["hits"]}
    assert {"rec-shed", "rec-sink", "rec-kettle"} <= every
    inside = [hit["id"] for hit in _ok(folders, "search", query="kettle", under="home", limit=25)["hits"]]
    assert inside == ["rec-sink"]
    homes = {hit["id"] for hit in _ok(folders, "search", query="action", under="home", limit=25)["hits"]}
    assert homes == {"act-home", "act-kitchen"}
    projects = {hit["id"] for hit in _ok(folders, "search", query="kitchen", under="home",
                                         entities=["project"])["hits"]}
    assert projects == {"kitchen"}
    # A row with no folder (a source, a person) is never inside one.
    assert _ok(folders, "search", query="descale", under="home", entities=["source"])["hits"] == []
