from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from brain import migrate, schemas, store
from tests.conftest import CORE_TABLES, seed


RECORD_TABLES = [
    "people",
    "systems",
    "projects",
    "fact_keys",
    "facts",
    "records",
    "decisions",
    "actions",
    "checklist_items",
    "threads",
    "thread_links",
    "meetings",
    "signals",
]


def _open_context(instance: Path) -> sqlite3.Connection:
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA trusted_schema = ON")
    con.create_function("brain_applier_context", 0, lambda: 1)
    return con


def test_fresh_store_is_at_the_latest_version_and_passes_checks(instance: Path) -> None:
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrate.latest_version()
        assert store.integrity_problems(con) == []
        assert store.missing_guards(con) == []
    finally:
        con.close()


def test_every_table_is_strict(instance: Path) -> None:
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        rows = con.execute(
            "SELECT name, strict FROM pragma_table_list "
            "WHERE schema = 'main' AND type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        assert {name for name, _ in rows} == set(CORE_TABLES)
        assert all(strict == 1 for _, strict in rows)
    finally:
        con.close()


def test_record_tables_require_a_source(instance: Path) -> None:
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        for table in RECORD_TABLES:
            info = {row[1]: row for row in con.execute(f"PRAGMA table_info({table})")}
            assert info["source_id"][3] == 1
            assert info["locator"][3] == 1
            fks = con.execute(f"PRAGMA foreign_key_list({table})").fetchall()
            assert any(row[2] == "sources" and row[3] == "source_id" for row in fks)
    finally:
        con.close()


def test_history_tables_are_append_only(instance: Path) -> None:
    con = _open_context(instance)
    try:
        src = con.execute("SELECT id FROM sources LIMIT 1").fetchone()[0]
        now = "2026-10-01T00:00:00Z"
        con.execute(
            "INSERT INTO records "
            "(id, kind, title, body, source_id, locator, version, created_at, updated_at, updated_by) "
            "VALUES ('rec-hist-ao', 'note', 'Title', 'Body text', ?, 'x', 1, ?, ?, 't:x')",
            (src, now, now),
        )
        con.execute(
            "INSERT INTO facts "
            "(key, value_type, value_text, status, basis, as_of, last_verified_at, "
            "source_id, locator, version, created_at, updated_at, updated_by) "
            "VALUES ('demo.motto', 'text', 'hi', 'current', 'observed', '2026-10-01', ?, ?, 'x', 1, ?, ?, 't:x')",
            (now, src, now, now),
        )
        for table in ("record_history", "fact_history"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                con.execute(f"UPDATE {table} SET locator = locator")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                con.execute(f"DELETE FROM {table}")
    finally:
        con.close()


def test_history_rows_are_written_for_every_version(instance: Path, apply) -> None:
    from tests.conftest import make_packet, ro_rows, upsert_op

    seed(apply)
    apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {"id": "rec-hist", "kind": "note", "title": "One", "body": "Body one"},
                "src-note-anon",
            ),
        )
    )
    apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {"id": "rec-hist", "title": "Two"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    versions = [
        row[0]
        for row in ro_rows(instance, "SELECT version FROM record_history WHERE record_id = 'rec-hist'")
    ]
    assert versions == [1, 2]


def test_fact_value_checks_reject_the_wrong_column_for_every_type(instance: Path, apply) -> None:
    seed(apply)
    con = _open_context(instance)
    src = "src-note-anon"
    try:
        cases = [
            ("demo.motto", {"value_int": 1}),
            ("demo.seats", {"value_text": "x"}),
            ("demo.price", {"value_text": "x"}),
            ("demo.launch", {"value_text": "x"}),
            ("demo.enabled", {"value_text": "x"}),
            ("demo.site", {"value_int": 1}),
            ("demo.region", {"value_int": 1}),
        ]
        for key, wrong in cases:
            cols = {
                "key": key,
                "value_type": {
                    "demo.motto": "text",
                    "demo.seats": "int",
                    "demo.price": "number",
                    "demo.launch": "date",
                    "demo.enabled": "bool",
                    "demo.site": "url",
                    "demo.region": "enum",
                }[key],
                "value_text": None,
                "value_int": None,
                "value_real": None,
                "value_date": None,
                "status": "current",
                "basis": "observed",
                "as_of": "2026-10-01",
                "last_verified_at": "2026-10-01T00:00:00Z",
                "source_id": src,
                "locator": "x",
                "version": 1,
                "created_at": "2026-10-01T00:00:00Z",
                "updated_at": "2026-10-01T00:00:00Z",
                "updated_by": "tester:x",
            }
            cols.update(wrong)
            names = list(cols)
            with pytest.raises(sqlite3.IntegrityError):
                con.execute(
                    f"INSERT INTO facts ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                    [cols[name] for name in names],
                )
    finally:
        con.close()


def test_people_reject_email_addresses_at_the_database(instance: Path) -> None:
    con = _open_context(instance)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO people (id, display_name, source_id, locator, version, created_at, updated_at, updated_by) "
                "VALUES ('per-mail', 'Ada@x', 'src-missing', 'x', 1, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 't:x')"
            )
    finally:
        con.close()


def test_sources_never_change(instance: Path, apply) -> None:
    seed(apply)
    con = _open_context(instance)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="sources never change"):
            con.execute("UPDATE sources SET title = 'X' WHERE id = 'src-note-anon'")
    finally:
        con.close()


def test_write_log_decided_rows_are_final_and_never_deleted(instance: Path) -> None:
    con = _open_context(instance)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="decided change is final"):
            con.execute("UPDATE write_log SET error = 'x' WHERE status = 'applied'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            con.execute("DELETE FROM write_log")
    finally:
        con.close()


def test_dates_and_timestamps_are_checked(instance: Path) -> None:
    con = _open_context(instance)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO sources (id, kind, title, source_date, content, content_sha256, captured_at, captured_by) "
                "VALUES ('src-bad-date', 'user_note', 'T', '2026-02-30', 'c', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "'2026-10-01T00:00:00Z', 't:x')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO sources (id, kind, title, source_date, content, content_sha256, captured_at, captured_by) "
                "VALUES ('src-bad-ts', 'user_note', 'T', '2026-10-01', 'c', "
                "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', "
                "'2026-10-08 05:37:00', 't:x')"
            )
    finally:
        con.close()


def test_schema_enums_match_sql_checks(instance: Path) -> None:
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        sql = {
            row[0]: row[1]
            for row in con.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'table'")
        }
    finally:
        con.close()
    pattern = re.compile(
        r"(?:(\w+)\s+IS NULL OR\s+)?(\w+)\s+IN\s+\(([^)]+)\)",
        re.I,
    )
    for spec in schemas.OPS.values():
        table = spec["table"]
        for field, field_schema in spec["row"]["properties"].items():
            if "enum" not in field_schema:
                continue
            if field == "value":
                continue
            expected = set(field_schema["enum"])
            matches = [
                match
                for match in pattern.finditer(sql[table] or "")
                if match.group(2) == field or match.group(1) == field
            ]
            assert matches, f"no CHECK IN for {table}.{field}"
            values = {item.strip().strip("'") for item in matches[0].group(3).split(",")}
            assert values == expected, f"{table}.{field}: {values} != {expected}"
