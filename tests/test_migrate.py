from __future__ import annotations

import re
from pathlib import Path

import pytest

from brain import migrate, store
from brain.migrate import MigrationError

_TXN_OR_PRAGMA = re.compile(
    r"\b(BEGIN\s+(IMMEDIATE|DEFERRED|EXCLUSIVE|TRANSACTION)|COMMIT|ROLLBACK|PRAGMA)\b"
)


def test_migrate_creates_a_store_at_the_latest_version(tmp_path: Path) -> None:
    db = tmp_path / "brain.sqlite3"
    old, new = migrate.migrate(db, create=True)
    assert old == 0
    assert new == migrate.latest_version()
    con = store.connect_readonly(db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == new
        assert store.missing_guards(con) == []
    finally:
        con.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "brain.sqlite3"
    migrate.migrate(db, create=True)
    old, new = migrate.migrate(db)
    assert old == new == migrate.latest_version()


def test_migrate_refuses_a_newer_store(tmp_path: Path) -> None:
    db = tmp_path / "brain.sqlite3"
    migrate.migrate(db, create=True)
    import sqlite3

    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA user_version = 99")
    finally:
        con.close()
    with pytest.raises(MigrationError, match="newer than this engine"):
        migrate.migrate(db)


def test_failed_migration_rolls_back_completely(tmp_path: Path) -> None:
    migrations = tmp_path / "migs"
    migrations.mkdir()
    core = Path(__file__).resolve().parents[1] / "src/brain/migrations/0001_core.sql"
    (migrations / "0001_core.sql").write_text(core.read_text())
    (migrations / "0002_broken.sql").write_text("CREATE TABLE nope ();\nTHIS IS NOT SQL;\n")
    db = tmp_path / "brain.sqlite3"
    with pytest.raises(MigrationError):
        migrate.migrate(db, create=True, migrations_dir=migrations)
    import sqlite3

    con = sqlite3.connect(db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 1
        names = [row[0] for row in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")]
        assert "nope" not in names
    finally:
        con.close()


def test_tables_added_by_later_migrations_get_guards(tmp_path: Path) -> None:
    migrations = tmp_path / "migs"
    migrations.mkdir()
    core = Path(__file__).resolve().parents[1] / "src/brain/migrations/0001_core.sql"
    (migrations / "0001_core.sql").write_text(core.read_text())
    (migrations / "0002_extra.sql").write_text(
        "CREATE TABLE extra_items (id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '') STRICT;\n"
    )
    db = tmp_path / "brain.sqlite3"
    migrate.migrate(db, create=True, migrations_dir=migrations)
    con = store.connect_readonly(db)
    try:
        missing = store.missing_guards(con)
        assert not any(name.startswith("guard_extra_items_") for name in missing)
        triggers = {
            row[0]
            for row in con.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger'")
        }
        assert {
            "guard_extra_items_insert",
            "guard_extra_items_update",
            "guard_extra_items_delete",
        } <= triggers
    finally:
        con.close()


def test_packaged_search_check_ignores_the_migration_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = migrate._package_migrations()

    def renamed_without_objects() -> list[tuple[int, str, str]]:
        found = []
        for number, slug, sql in packaged:
            if slug == "search":
                found.append((number, "search_v2", "SELECT 1;\n"))
            else:
                found.append((number, slug, sql))
        return found

    def renamed_intact() -> list[tuple[int, str, str]]:
        return [
            (number, "search_v2" if slug == "search" else slug, sql)
            for number, slug, sql in packaged
        ]

    monkeypatch.setattr(migrate, "_package_migrations", renamed_without_objects)
    with pytest.raises(MigrationError, match="missing search objects"):
        migrate.migrate(tmp_path / "broken.sqlite3", create=True)

    monkeypatch.setattr(migrate, "_package_migrations", renamed_intact)
    db = tmp_path / "intact.sqlite3"
    old, new = migrate.migrate(db, create=True)
    assert (old, new) == (0, migrate.latest_version())
    con = store.connect_readonly(db)
    try:
        assert store.missing_search_objects(con) == []
    finally:
        con.close()


def test_migration_files_are_contiguous_and_hold_no_transaction_or_pragma_statements() -> None:
    items = migrate.available_migrations()
    numbers = [item[0] for item in items]
    assert numbers == list(range(1, len(numbers) + 1))
    for number, slug, sql in items:
        statements = "\n".join(
            line for line in sql.splitlines() if line.strip() and not line.lstrip().startswith("--")
        ).upper()
        assert _TXN_OR_PRAGMA.search(statements) is None
        assert number >= 1
        assert slug


# -- migration 0005 ----------------------------------------------------------------------------

V4_NOW = "2026-10-01T00:00:00Z"
V4_KEYS = (
    ("demo.seats", "int"),
    ("demo.retired", "text"),
    ("demo.motto", "text"),
    ("demo.price", "number"),
    ("demo.third", "number"),
    ("excloud.current_version", "text"),
    ("excloud.max_context", "int"),
)
# key, value column, value, status, updated_by
V4_FACTS = (
    ("demo.seats", "value_int", 12, "current", "bot:bot-seed-1"),
    ("demo.retired", "value_text", "Old motto", "retired", "bot:bot-seed-1"),
    ("demo.motto", "value_text", "Small steps", "disputed", "tester:tester-seed-2"),
    ("demo.price", "value_real", 80.0, "current", "importer"),
    ("demo.third", "value_real", 1 / 3, "current", "bot:bot-seed-1"),
    ("excloud.current_version", "value_text", "1.3", "disputed", "notes:notes-seed-3"),
)
CLAIM_CANDIDATE = {
    "candidate_version": 1,
    "kind": "claim_candidate",
    "source_id": "src-old",
    "locator": "para 2",
    "quote": "The motto is small steps for the whole team",
    "fields": {"product": "widget", "area": "pricing", "topic": "motto", "value": "small steps", "status": "reported"},
}


def v4_store(tmp_path: Path, config_text: str, facts=V4_FACTS) -> Path:
    """A schema-4 store built from 0001 to 0004, as tests/test_phase6.py::_v3_instance builds schema 3."""
    import json as _json
    import shutil

    from brain.init import render_router
    from tests.conftest import context_connection

    instance = tmp_path / "demo"
    instance.mkdir(parents=True)
    (instance / "brain.toml").write_text(config_text, encoding="utf-8")
    (instance / "ROUTER.md").write_text(render_router("demo"), encoding="utf-8")
    migrations = tmp_path / "schema-v4"
    migrations.mkdir()
    source = Path(__file__).resolve().parents[1] / "src" / "brain" / "migrations"
    for name in ("0001_core.sql", "0002_search.sql", "0003_meetings.sql", "0004_people_and_unchecked.sql"):
        shutil.copy(source / name, migrations / name)
    migrate.migrate(instance / "brain.sqlite3", create=True, migrations_dir=migrations)
    content = "Old notes. The motto is small steps for the whole team. The current version is 1.3."
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO sources (id, kind, title, source_date, content, content_sha256, captured_at, captured_by) "
            "VALUES ('src-old', 'user_note', 'Old notes', '2026-10-01', ?, ?, ?, 'tester:v4')",
            (content, store.sha256_text(content), V4_NOW),
        )
        for key, value_type in V4_KEYS:
            con.execute(
                "INSERT INTO fact_keys (key, value_type, unit, description, source_id, locator, version, created_at, "
                "updated_at, updated_by) VALUES (?, ?, '', 'An old key.', 'src-old', 'synthetic', 1, ?, ?, 'tester:v4')",
                (key, value_type, V4_NOW, V4_NOW),
            )
        for key, column, value, status, updated_by in facts:
            value_type = dict(V4_KEYS)[key]
            con.execute(
                f"INSERT INTO facts (key, value_type, {column}, status, basis, as_of, last_verified_at, source_id, "
                "locator, version, created_at, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, 'reported', '2026-10-01', ?, 'src-old', 'synthetic', 1, ?, ?, ?)",
                (key, value_type, value, status, V4_NOW, V4_NOW, V4_NOW, updated_by),
            )
        for change_id, agent, packet in (
            ("careful-held-1", "careful", {"ops": [{"op": "add_source", "row": {"id": "src-held"}}]}),
            ("scan-held-2", "scan", {"ops": [{"op": "apply_candidate", "candidate": CLAIM_CANDIDATE}]}),
        ):
            packet = {"packet_version": 1, "change_id": change_id, "agent": agent, "intent": "held", **packet}
            text = _json.dumps(packet)
            con.execute(
                "INSERT INTO write_log (change_id, agent, intent, status, decided_at, payload_sha256, packet_json) "
                "VALUES (?, ?, 'held', 'held', ?, ?, ?)",
                (change_id, agent, V4_NOW, store.sha256_text(text), text),
            )
        con.execute("COMMIT")
    finally:
        con.close()
    return instance


def _v4_config() -> str:
    from tests.test_truth import KEYS, truth_config

    keys = [*KEYS, *[{"key": key, "value_type": value_type} for key, value_type in V4_KEYS if key.startswith("demo.")]]
    return truth_config(keys=keys)


def _rows(instance: Path, sql: str) -> list[tuple]:
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        return [tuple(row) for row in con.execute(sql)]
    finally:
        con.close()


_FACT_COLUMNS = (
    "key, value_type, value_text, value_int, value_real, value_date, status, basis, as_of, last_verified_at, "
    "source_id, locator, version, created_at, updated_at, updated_by"
)


def test_0005_keeps_fact_heads(tmp_path: Path) -> None:
    instance = v4_store(tmp_path, _v4_config())
    assert _rows(instance, "PRAGMA user_version") == [(4,)]
    before = _rows(instance, f"SELECT {_FACT_COLUMNS} FROM facts ORDER BY key")
    rowids = dict(_rows(instance, "SELECT key, rowid FROM facts"))
    assert migrate.migrate(instance / "brain.sqlite3") == (4, migrate.latest_version())
    assert _rows(instance, f"SELECT {_FACT_COLUMNS} FROM facts ORDER BY key") == before
    assert _rows(instance, "SELECT DISTINCT quote FROM facts") == [(None,)]
    claims = {
        row[1]: row
        for row in _rows(
            instance,
            "SELECT id, key, scope, value_raw, status, proposed_by, checked, quote, supersedes, superseded_by "
            "FROM claims ORDER BY id",
        )
    }
    assert set(claims) == {key for key, *_rest in V4_FACTS}
    expected = {
        "demo.seats": ("12", "live", "bot"),
        "demo.retired": ("Old motto", "superseded", "bot"),
        "demo.motto": ("Small steps", "live", "tester"),
        "demo.price": ("80", "live", "importer"),
        "demo.third": ("0.333333333333333", "live", "bot"),
        "excloud.current_version": ("1.3", "live", "notes"),
    }
    for key, (raw, status, proposed_by) in expected.items():
        claim_id, _key, scope, value_raw, claim_status, writer, checked, quote, supersedes, superseded_by = claims[key]
        assert claim_id == f"clm-m5-{rowids[key]}"
        assert (scope, value_raw, claim_status, writer, checked, quote, supersedes, superseded_by) == (
            "", raw, status, proposed_by, 0, "", "[]", None
        ), key
    values = "value_type, value_text, value_int, value_real, value_date, basis, as_of, source_id, locator"
    assert _rows(instance, f"SELECT key, {values} FROM claims ORDER BY key") == _rows(
        instance, f"SELECT key, {values} FROM facts ORDER BY key"
    )
    signals = _rows(instance, "SELECT id, kind, subject_id, status, detail FROM signals ORDER BY id")
    assert signals == [
        (f"sig-conflict-m5-{rowids['demo.motto']}", "conflict", "demo.motto", "open", "marked disputed before 0.7.0"),
        (f"sig-conflict-m5-{rowids['excloud.current_version']}", "conflict", "excloud.current_version", "open",
         "marked disputed before 0.7.0"),
    ]
    assert _rows(instance, "SELECT status FROM facts WHERE key = 'demo.motto'") == [("disputed",)]
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        assert store.truth_problems(con) == []
        assert store.integrity_problems(con) == []
        assert store.missing_guards(con) == []
    finally:
        con.close()
    assert _rows(instance, "SELECT change_id, status, error, hold_reason FROM write_log ORDER BY change_id") == [
        ("careful-held-1", "held", "", ""),
        ("scan-held-2", "rejected", "candidate format changed in 0.7.0", ""),
    ]
    assert _rows(instance, "SELECT name FROM sqlite_schema WHERE name = 'guard_write_log_update'") == [
        ("guard_write_log_update",)
    ]


def test_0005_search_bodies_unchanged(tmp_path: Path) -> None:
    instance = v4_store(tmp_path, _v4_config())
    index = "SELECT rowid, entity, ref_id, status, title, body FROM search_index ORDER BY rowid"
    keys = "SELECT doc, entity, ref_id FROM search_keys ORDER BY doc"
    before = (_rows(instance, index), _rows(instance, keys))
    assert len(before[0]) == len(V4_KEYS) + 1
    migrate.migrate(instance / "brain.sqlite3")
    assert (_rows(instance, index), _rows(instance, keys)) == before
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        assert store.search_drift(con) == []
        assert store.missing_search_objects(con) == []
    finally:
        con.close()


def test_0005_legacy_dispute_settles_with_resolve_fact(tmp_path: Path) -> None:
    import json as _json

    from brain.applier import Applier
    from tests.conftest import make_packet, upsert_op

    instance = v4_store(tmp_path, _v4_config())
    migrate.migrate(instance / "brain.sqlite3")
    motto = _rows(instance, "SELECT rowid FROM facts WHERE key = 'demo.motto'")[0][0]
    writer = Applier(instance)
    try:
        def apply(*ops):
            return writer.apply(_json.dumps(make_packet("alex", *ops)).encode())

        people = apply(
            {"op": "add_source", "row": {"id": "src-people", "kind": "user_note", "title": "People",
                                         "source_date": "2026-10-02", "author_person_id": None,
                                         "content": "Sam Example keeps the motto."}},
            upsert_op("upsert_person", {"id": "per-sam-example", "display_name": "Sam Example"}, "src-people"),
            {"op": "add_source", "row": {"id": "src-sam", "kind": "user_note", "title": "Sam's note",
                                         "source_date": "2026-10-02", "author_person_id": "per-sam-example",
                                         "content": "Sam: the motto really is Small steps, as before."}},
        )
        assert people.receipt["status"] == "applied", people.receipt
        settled = apply({
            "op": "resolve_fact",
            "row": {"key": "demo.motto", "scope": "", "keep": [f"clm-m5-{motto}"], "reject": [],
                    "quote": "the motto really is Small steps"},
            "source": {"source_id": "src-sam", "locator": "line 1"},
        })
    finally:
        writer.close()
    assert settled.receipt["status"] == "applied", settled.receipt
    assert _rows(instance, "SELECT status, value_text, version FROM facts WHERE key = 'demo.motto'") == [
        ("current", "Small steps", 2)
    ]
    assert _rows(instance, f"SELECT status FROM signals WHERE id = 'sig-conflict-m5-{motto}'") == [("resolved",)]
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        assert store.truth_problems(con) == []
    finally:
        con.close()
