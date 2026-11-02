"""Folders (0007): a project sits inside a workstream or another project,
like a folder inside a folder. The applier refuses a missing parent, a loop, a self-parent and a workstream with a
parent. records_query takes "under" for a folder and every folder inside it, and get on a project shows where it
sits, the folders inside it and the open work in its whole branch."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from brain import reads
from brain.applier import FOLDER_DEPTH_MAX
from brain.migrate import latest_version, migrate
from tests.conftest import make_packet, source_op, upsert_op


def _project(pid: str, parent: str | None = None, base=None) -> dict:
    row = {"id": pid, "kind": "project", "title": pid.replace("-", " ").title()}
    if parent is not None:
        row["parent_id"] = parent
    return upsert_op("upsert_project", row, "src-folders", base=base)


def _move(pid: str, parent: str | None, base: int) -> dict:
    return upsert_op("upsert_project", {"id": pid, "parent_id": parent}, "src-folders", base=base)


@pytest.fixture
def tree(instance, apply) -> Path:
    assert apply(make_packet("tester", source_op("src-folders", "Folder tests."))).receipt["status"] == "applied"
    for pid, parent in (("garden", None), ("beds", "garden"), ("greenhouse", "beds"),
                        ("orchard", None)):
        outcome = apply(make_packet("tester", _project(pid, parent)))
        assert outcome.receipt["status"] == "applied", outcome.receipt
    action = {
        "id": "act-greenhouse-test",
        "title": "Test the greenhouse",
        "status": "open",
        "status_basis": "observed",
        "project_id": "greenhouse",
        "last_verified_at": "2026-10-01T00:00:00Z",
    }
    assert apply(make_packet("tester", upsert_op("upsert_action", action, "src-folders"))).receipt["status"] == "applied"
    return instance


def _error(outcome) -> str:
    assert outcome.receipt["status"] == "rejected", outcome.receipt
    return outcome.receipt["error"]


def test_a_folder_sits_inside_another(tree) -> None:
    con = sqlite3.connect(tree / "brain.sqlite3")
    try:
        assert con.execute("SELECT parent_id FROM projects WHERE id = 'greenhouse'").fetchone() == ("beds",)
        assert con.execute("SELECT parent_id FROM projects WHERE id = 'garden'").fetchone() == (None,)
    finally:
        con.close()


def test_a_loop_is_refused(tree, apply) -> None:
    assert "would make a loop" in _error(apply(make_packet("tester", _move("garden", "greenhouse", base=1))))


def test_a_folder_cant_be_its_own_parent(tree, apply) -> None:
    assert "its own parent" in _error(apply(make_packet("tester", _move("greenhouse", "greenhouse", base=1))))


def test_a_missing_parent_is_refused(tree, apply) -> None:
    assert "parent nowhere is not a project" in _error(apply(make_packet("tester", _project("orphan", "nowhere"))))


def test_a_parent_can_be_created_in_the_same_packet(tree, apply) -> None:
    outcome = apply(make_packet("tester", _project("rows", "orchard"), _project("pears", "rows")))
    assert outcome.receipt["status"] == "applied", outcome.receipt


def test_a_folder_moves_and_can_go_back_to_the_top(tree, apply) -> None:
    assert apply(make_packet("tester", _move("greenhouse", "orchard", base=1))).receipt["status"] == "applied"
    folder = json.loads(reads.run_tool(tree, "get", {"entity": "project", "id": "greenhouse"}).text)["related"]["folder"]
    assert folder["path"] == ["orchard"]
    assert apply(make_packet("tester", _move("greenhouse", None, base=2))).receipt["status"] == "applied"
    folder = json.loads(reads.run_tool(tree, "get", {"entity": "project", "id": "greenhouse"}).text)["related"]["folder"]
    assert folder["path"] == []


def test_nesting_stops_at_the_depth_limit(tree, apply) -> None:
    parent = "greenhouse"  # already three deep
    created = []
    for depth in range(FOLDER_DEPTH_MAX):
        pid = f"deep-{depth}"
        outcome = apply(make_packet("tester", _project(pid, parent)))
        if outcome.receipt["status"] != "applied":
            assert f"at most {FOLDER_DEPTH_MAX} deep" in outcome.receipt["error"]
            break
        created.append(pid)
        parent = pid
    else:
        pytest.fail("the depth limit never applied")
    assert created


def test_under_reads_a_folder_and_everything_inside_it(tree) -> None:
    for top, expected in (("garden", ["act-greenhouse-test"]), ("greenhouse", ["act-greenhouse-test"]), ("orchard", [])):
        result = reads.run_tool(tree, "records_query", {"entity": "action", "filters": {"project_id": {"under": top}}})
        assert not result.is_error, result.text
        assert [row["id"] for row in json.loads(result.text)["rows"]] == expected
    result = reads.run_tool(tree, "records_query", {"entity": "project", "filters": {"id": {"under": "garden"}}})
    assert sorted(row["id"] for row in json.loads(result.text)["rows"]) == ["beds", "garden", "greenhouse"]


def test_under_a_misspelt_folder_is_an_error_not_an_empty_answer(tree) -> None:
    result = reads.run_tool(tree, "records_query", {"entity": "action", "filters": {"project_id": {"under": "agnet-system"}}})
    assert result.is_error
    assert "no project agnet-system" in result.text


def test_under_only_works_on_project_ids(tree) -> None:
    result = reads.run_tool(tree, "records_query", {"entity": "action", "filters": {"title": {"under": "greenhouse"}}})
    assert result.is_error
    assert "under takes a project id" in result.text


def test_get_shows_the_path_the_folders_inside_and_the_branch(tree) -> None:
    folder = json.loads(reads.run_tool(tree, "get", {"entity": "project", "id": "garden"}).text)["related"]["folder"]
    assert folder["path"] == []
    assert [child["id"] for child in folder["children"]] == ["beds"]
    assert folder["branch"] == {"folders": 2, "open_actions": 1, "records": 0}
    folder = json.loads(reads.run_tool(tree, "get", {"entity": "project", "id": "greenhouse"}).text)["related"]["folder"]
    assert folder["path"] == ["garden", "beds"]
    assert folder["children"] == []


def test_the_migration_adds_the_column_and_keeps_every_project(tmp_path: Path) -> None:
    db = tmp_path / "brain.sqlite3"
    migrate(db, create=True, upto=6)  # the schema before 0007_folders added parent_id
    con = sqlite3.connect(db)
    try:
        assert "parent_id" not in [row[1] for row in con.execute("PRAGMA table_info(projects)")]
    finally:
        con.close()
    assert migrate(db) == (6, latest_version())
    con = sqlite3.connect(db)
    try:
        assert "parent_id" in [row[1] for row in con.execute("PRAGMA table_info(projects)")]
    finally:
        con.close()
