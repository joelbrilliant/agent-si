from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from brain.applier import Applier, Unavailable
from brain import store
from tests.conftest import CORE_TABLES, FTS_TABLES, file_sha, make_packet, ro_rows, seed, source_op


@pytest.mark.parametrize("table", CORE_TABLES)
def test_direct_insert_into_every_table_is_refused(instance: Path, apply, table: str) -> None:
    seed(apply)
    digest = file_sha(instance / "brain.sqlite3")
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such function: brain_applier_context"):
            con.execute(f"INSERT INTO {table} DEFAULT VALUES")
    finally:
        con.close()
    assert file_sha(instance / "brain.sqlite3") == digest


def test_direct_update_and_delete_are_refused(instance: Path, apply) -> None:
    seed(apply)
    digest = file_sha(instance / "brain.sqlite3")
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE people SET display_name = 'X'")
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM people")
    finally:
        con.close()
    assert ro_rows(instance, "SELECT display_name FROM people WHERE id = 'per-dana-okafor'") == [
        ("Dana Okafor",)
    ]
    assert file_sha(instance / "brain.sqlite3") == digest


def test_context_function_returning_zero_is_refused(instance: Path, apply) -> None:
    seed(apply)
    digest = file_sha(instance / "brain.sqlite3")
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.create_function("brain_applier_context", 0, lambda: 0)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="direct writes are refused"):
            con.execute("INSERT INTO people (id, display_name) VALUES ('per-x', 'X')")
        with pytest.raises(sqlite3.IntegrityError, match="direct writes are refused"):
            con.execute("UPDATE people SET display_name = 'X'")
        with pytest.raises(sqlite3.IntegrityError, match="direct writes are refused"):
            con.execute("DELETE FROM people")
    finally:
        con.close()
    assert file_sha(instance / "brain.sqlite3") == digest


def test_applier_connection_cannot_write_outside_a_decision(instance: Path, applier, apply) -> None:
    seed(apply)
    with pytest.raises(sqlite3.IntegrityError, match="direct writes are refused"):
        applier.con.execute("UPDATE people SET display_name = 'X'")


def test_readonly_connection_cannot_write(instance: Path) -> None:
    from brain.store import connect_readonly

    digest = file_sha(instance / "brain.sqlite3")
    con = connect_readonly(instance / "brain.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO sources DEFAULT VALUES")
    finally:
        con.close()
    assert file_sha(instance / "brain.sqlite3") == digest


def test_sqlite3_command_line_write_is_refused(instance: Path) -> None:
    if shutil.which("sqlite3") is None:
        pytest.skip("sqlite3 command not available")
    digest = file_sha(instance / "brain.sqlite3")
    result = subprocess.run(
        ["sqlite3", str(instance / "brain.sqlite3"), "INSERT INTO people (id) VALUES ('per-x')"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "brain_applier_context" in result.stderr
    assert file_sha(instance / "brain.sqlite3") == digest


def test_the_applier_writes(instance: Path, apply) -> None:
    seed(apply)
    rows = ro_rows(
        instance,
        "SELECT id, version, updated_by FROM people WHERE id IN ('per-dana-okafor', 'per-sam-ito')",
    )
    assert {row[0] for row in rows} == {"per-dana-okafor", "per-sam-ito"}
    assert all(row[1] == 1 and row[2].startswith("tester:") for row in rows)


def test_only_the_applier_registers_the_context_function() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "brain"
    hits = []
    for path in root.glob("*.py"):
        text = path.read_text()
        if "create_function(" in text:
            hits.append(path.name)
    assert hits == ["applier.py"]


def test_every_table_has_insert_update_delete_guards(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        tables = store.tables_to_guard(con)
        assert tables == sorted(CORE_TABLES)
        schema_tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert schema_tables - set(CORE_TABLES) == set(FTS_TABLES)
        triggers = list(con.execute("SELECT name, tbl_name FROM sqlite_schema WHERE type = 'trigger'"))
        names = {row[0] for row in triggers}
        assert not any(row[1] in FTS_TABLES for row in triggers)
        for table in CORE_TABLES:
            assert f"guard_{table}_insert" in names
            assert f"guard_{table}_update" in names
            assert f"guard_{table}_delete" in names
    finally:
        con.close()


def test_missing_guard_refuses_apply_and_writes_nothing(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("DROP TRIGGER guard_people_insert")
        con.commit()
    finally:
        con.close()
    digest = file_sha(instance / "brain.sqlite3")
    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    assert "guard_people_insert" in check.stdout
    writer = Applier(instance)
    try:
        with pytest.raises(Unavailable, match="guard_people_insert"):
            writer.apply(
                json.dumps(
                    make_packet(
                        "tester",
                        source_op("src-unguarded", "A packet that must not write after a dropped guard."),
                    )
                ).encode()
            )
    finally:
        writer.close()
    assert file_sha(instance / "brain.sqlite3") == digest
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count


def test_missing_guard_after_commit_pauses_the_store(instance: Path, apply, monkeypatch) -> None:
    original = Applier._finish

    def drop_then_finish(self, change_id: str):
        self.con.execute("DROP TRIGGER IF EXISTS guard_people_insert")
        return original(self, change_id)

    monkeypatch.setattr(Applier, "_finish", drop_then_finish)
    first = apply(
        make_packet(
            "tester",
            source_op("src-guard-pause", "A valid source used to trip a missing-guard pause."),
        )
    )
    assert first.receipt["status"] == "applied"
    assert first.exit_code == 5
    assert (instance / "PAUSED").is_file()
    paused = (instance / "PAUSED").read_text()
    assert first.receipt["change_id"] in paused
    assert "guard_people_insert" in paused
    monkeypatch.undo()
    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    with pytest.raises(Unavailable):
        apply(
            make_packet(
                "tester",
                source_op("src-after-guard-pause", "Must not apply while paused or unguarded."),
            )
        )
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    assert "paused" in check.stdout.lower() or "guard_people_insert" in check.stdout
    (instance / "PAUSED").unlink()
    with pytest.raises(Unavailable, match="guard_people_insert"):
        apply(
            make_packet(
                "tester",
                source_op("src-after-unpause", "Must not apply while a guard is still missing."),
            )
        )
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count
