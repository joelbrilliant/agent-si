from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from brain import migrate, store
from brain.applier import Applier, Unavailable
from brain.config import ConfigError, load_config
from brain.store import check_instance
from tests.conftest import (
    context_connection,
    file_sha,
    make_packet,
    ro_rows,
    seed,
    source_op,
    upsert_op,
    v1_instance,
)


def test_guard_helpers_are_fts5_safe(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("CREATE VIRTUAL TABLE records_fts USING fts5(title)")
        store.install_guards(con)
        assert store.missing_guards(con) == []
    finally:
        con.close()
    # The extra table and its shadow tables are schema differences, and the only problems.
    problems = check_instance(instance)
    assert problems and [item for item in problems if not item.startswith("schema: ")] == []


def test_guards_cover_base_tables_and_skip_fts_tables(instance: Path) -> None:
    from tests.conftest import CORE_TABLES, FTS_TABLES

    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        assert store.tables_to_guard(con) == sorted(CORE_TABLES)
        kinds = {
            row[0]: row[1]
            for row in con.execute("SELECT name, type FROM pragma_table_list WHERE schema = 'main'")
        }
        assert kinds["search_index"] == "virtual"
        for name in FTS_TABLES:
            if name == "search_index":
                continue
            assert kinds[name] == "shadow"
        triggers = con.execute("SELECT tbl_name FROM sqlite_schema WHERE type = 'trigger'").fetchall()
        assert not any(row[0] in FTS_TABLES for row in triggers)
    finally:
        con.close()


def test_fresh_store_has_every_search_object(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        assert store.missing_search_objects(con) == []
        assert len(store.required_search_objects()) == 31
        assert len(store.required_search_objects(5)) == 36
        names = con.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND name LIKE 'search_%'"
        ).fetchall()
        assert len(names) == 32
        assert store.search_drift(con) == []
        docs, keys, indexed = store.search_counts(con)
        assert docs == keys == indexed
    finally:
        con.close()


def _doc(instance: Path, entity: str, ref_id: str) -> list[tuple]:
    return ro_rows(
        instance,
        "SELECT entity, ref_id, status, title, body FROM search_docs "
        "WHERE entity = ? AND ref_id = ?",
        (entity, ref_id),
    )


def _indexed(instance: Path, entity: str, ref_id: str) -> list[tuple]:
    return ro_rows(
        instance,
        "SELECT s.entity, s.ref_id, s.status, s.title, s.body "
        "FROM search_keys AS k JOIN search_index AS s ON s.rowid = k.doc "
        "WHERE k.entity = ? AND k.ref_id = ?",
        (entity, ref_id),
    )


def _matches(instance: Path, word: str, entity: str, ref_id: str) -> bool:
    rows = ro_rows(
        instance,
        "SELECT ref_id FROM search_index WHERE search_index MATCH ? AND entity = ? AND ref_id = ?",
        (f'"{word}"', entity, ref_id),
    )
    return rows == [(ref_id,)]


def _drift(instance: Path) -> list[str]:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        return store.search_drift(con)
    finally:
        con.close()


def _snap(instance: Path) -> tuple[list[tuple], list[tuple]]:
    return (
        ro_rows(instance, "SELECT doc, entity, ref_id FROM search_keys ORDER BY doc"),
        ro_rows(instance, "SELECT rowid, * FROM search_index ORDER BY rowid"),
    )


@pytest.mark.parametrize(
    "entity",
    [
        "record",
        "decision",
        "action",
        "checklist_item",
        "project",
        "person",
        "system",
        "fact",
        "thread",
        "source",
    ],
)
def test_index_follows_inserts_and_updates(instance: Path, apply, entity: str) -> None:
    seed(apply)
    word = f"word{entity.replace('_', '')}"
    updated = f"upd{entity.replace('_', '')}"
    ref_id, update = _insert_entity(instance, apply, entity, word)
    assert _doc(instance, entity, ref_id) == _indexed(instance, entity, ref_id)
    assert _doc(instance, entity, ref_id)
    assert _matches(instance, word, entity, ref_id)
    if entity == "source":
        return
    outcome = update(updated)
    assert outcome.receipt["status"] == "applied"
    assert outcome.exit_code == 0
    assert _doc(instance, entity, ref_id) == _indexed(instance, entity, ref_id)
    assert _matches(instance, updated, entity, ref_id)
    assert not _matches(instance, word, entity, ref_id)
    assert _drift(instance) == []


def _insert_entity(instance: Path, apply, entity: str, word: str):
    source = "src-note-anon"
    if entity == "record":
        ref_id = "rec-wordrecord"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_record",
                    {"id": ref_id, "kind": "note", "title": f"Note {word}", "body": "Body text."},
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_record",
                        {"id": ref_id, "title": f"Note {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "decision":
        ref_id = "dec-worddecision"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_decision",
                    {
                        "id": ref_id,
                        "title": f"Decision {word}",
                        "quote": "We will cut the access form from the onboarding pack this week.",
                        "decided_on": "2026-10-01",
                        "decided_by_person_id": "per-dana-okafor",
                        "status": "in_effect",
                        "project_id": "general",
                    },
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_decision",
                        {"id": ref_id, "title": f"Decision {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "action":
        ref_id = "act-wordaction"
        row = {
            "id": ref_id,
            "title": f"Chase {word}",
            "status": "open",
            "status_basis": "observed",
            "project_id": "general",
            "last_verified_at": "2026-10-01T00:00:00Z",
        }
        outcome = apply(make_packet("tester", upsert_op("upsert_action", row, source)))

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_action",
                        {"id": ref_id, "title": f"Chase {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "checklist_item":
        ref_id = "chk-wordcheck"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_checklist_item",
                    {
                        "id": ref_id,
                        "project_id": "general",
                        "title": f"Step {word}",
                        "state": "not_started",
                        "state_basis": "observed",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_checklist_item",
                        {"id": ref_id, "title": f"Step {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "project":
        ref_id = "proj-word"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_project",
                    {"id": ref_id, "kind": "project", "title": f"Project {word}"},
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_project",
                        {"id": ref_id, "title": f"Project {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "person":
        ref_id = "per-wordperson"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_person",
                    {"id": ref_id, "display_name": word},
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_person",
                        {"id": ref_id, "display_name": new_word},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "system":
        ref_id = "sys-wordsystem"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_system",
                    {"id": ref_id, "name": f"System {word}", "kind": "saas"},
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_system",
                        {"id": ref_id, "name": f"System {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    elif entity == "fact":
        ref_id = "demo.registry"
        writer = Applier(instance, allow_reserved_agent=True)
        try:
            outcome = writer.apply(
                json.dumps(
                    make_packet(
                        "brain",
                        upsert_op(
                            "upsert_fact_key",
                            {
                                "key": ref_id,
                                "value_type": "text",
                                "description": f"Described as {word}",
                            },
                            source,
                        ),
                    )
                ).encode()
            )
        finally:
            writer.close()

        def update(new_word: str):
            brain = Applier(instance, allow_reserved_agent=True)
            try:
                return brain.apply(
                    json.dumps(
                        make_packet(
                            "brain",
                            upsert_op(
                                "upsert_fact_key",
                                {"key": ref_id, "description": f"Described as {new_word}"},
                                source,
                                base=1,
                            ),
                        )
                    ).encode()
                )
            finally:
                brain.close()

    elif entity == "thread":
        ref_id = "thr-wordthread"
        outcome = apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_thread",
                    {
                        "id": ref_id,
                        "channel": "email",
                        "account": "work",
                        "external_id": "ext-wordthread",
                        "subject": f"Subject {word}",
                    },
                    source,
                ),
            )
        )

        def update(new_word: str):
            return apply(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_thread",
                        {"id": ref_id, "subject": f"Subject {new_word}"},
                        source,
                        base=1,
                    ),
                )
            )

    else:
        ref_id = "src-wordsource"
        outcome = apply(
            make_packet(
                "tester",
                source_op("src-wordsource", "Source body text.", title=f"Title {word}"),
            )
        )

        def update(new_word: str):  # pragma: no cover
            raise AssertionError(new_word)

    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert outcome.exit_code == 0
    return ref_id, update


@pytest.mark.parametrize(
    "table,entity,ref_id",
    [
        ("records", "record", "rec-del"),
        ("decisions", "decision", "dec-del"),
        ("actions", "action", "act-del"),
        ("checklist_items", "checklist_item", "chk-del"),
        ("projects", "project", "proj-del"),
        ("people", "person", "per-delete"),
        ("systems", "system", "sys-delete"),
        ("fact_keys", "fact", "demo.del"),
        ("threads", "thread", "thr-del"),
    ],
)
def test_delete_trigger_removes_the_index_row(
    instance: Path, apply, table: str, entity: str, ref_id: str
) -> None:
    seed(apply)
    _insert_for_delete(instance, apply, table, ref_id)
    assert _indexed(instance, entity, ref_id)
    key = "key" if table == "fact_keys" else "id"
    con = context_connection(instance, foreign_keys=False)
    try:
        con.execute(f"DELETE FROM {table} WHERE {key} = ?", (ref_id,))
        con.commit()
    finally:
        con.close()
    assert ro_rows(
        instance,
        "SELECT 1 FROM search_keys WHERE entity = ? AND ref_id = ?",
        (entity, ref_id),
    ) == []
    assert ro_rows(
        instance,
        "SELECT 1 FROM search_index WHERE entity = ? AND ref_id = ?",
        (entity, ref_id),
    ) == []


def _insert_for_delete(instance: Path, apply, table: str, ref_id: str) -> None:
    source = "src-note-anon"
    if table == "records":
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_record",
                    {"id": ref_id, "kind": "note", "title": "Delete me", "body": "Body text."},
                    source,
                ),
            )
        )
    elif table == "decisions":
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_decision",
                    {
                        "id": ref_id,
                        "title": "Delete me",
                        "quote": "We will cut the access form from the onboarding pack this week.",
                        "decided_on": "2026-10-01",
                        "decided_by_person_id": "per-dana-okafor",
                        "status": "in_effect",
                        "project_id": "general",
                    },
                    source,
                ),
            )
        )
    elif table == "actions":
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_action",
                    {
                        "id": ref_id,
                        "title": "Delete me",
                        "status": "open",
                        "status_basis": "observed",
                        "project_id": "general",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    source,
                ),
            )
        )
    elif table == "checklist_items":
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_checklist_item",
                    {
                        "id": ref_id,
                        "project_id": "general",
                        "title": "Delete me",
                        "state": "not_started",
                        "state_basis": "observed",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    source,
                ),
            )
        )
    elif table == "projects":
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_project",
                    {"id": ref_id, "kind": "project", "title": "Delete me"},
                    source,
                ),
            )
        )
    elif table == "people":
        apply(
            make_packet(
                "tester",
                upsert_op("upsert_person", {"id": ref_id, "display_name": "Delete Me"}, source),
            )
        )
    elif table == "systems":
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_system",
                    {"id": ref_id, "name": "Delete me", "kind": "internal"},
                    source,
                ),
            )
        )
    elif table == "fact_keys":
        writer = Applier(instance, allow_reserved_agent=True)
        try:
            outcome = writer.apply(
                json.dumps(
                    make_packet(
                        "brain",
                        upsert_op(
                            "upsert_fact_key",
                            {"key": ref_id, "value_type": "text", "description": "delete me"},
                            source,
                        ),
                    )
                ).encode()
            )
        finally:
            writer.close()
        assert outcome.receipt["status"] == "applied", outcome.receipt
    else:
        apply(
            make_packet(
                "tester",
                upsert_op(
                    "upsert_thread",
                    {
                        "id": ref_id,
                        "channel": "email",
                        "account": "work",
                        "external_id": "ext-delete",
                        "subject": "Delete me",
                    },
                    source,
                ),
            )
        )


def test_config_sources_are_not_indexed(instance: Path) -> None:
    rows = ro_rows(instance, "SELECT id FROM sources WHERE kind = 'config'")
    assert rows
    for (source_id,) in rows:
        assert ro_rows(
            instance,
            "SELECT 1 FROM search_keys WHERE entity = 'source' AND ref_id = ?",
            (source_id,),
        ) == []
    assert ro_rows(
        instance,
        "SELECT ref_id FROM search_index WHERE search_index MATCH '\"workstreams\"' AND entity = 'source'",
    ) == []


def test_rejected_and_conflict_packets_leave_the_index_unchanged(instance: Path, apply) -> None:
    seed(apply)
    before = _snap(instance)
    rejected = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {
                    "id": "rec-rejected",
                    "kind": "note",
                    "title": "Rejected",
                    "body": 'Dana wrote "the launch review is cancelled for good".',
                },
                "src-note-anon",
            ),
        )
    )
    conflict = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {"id": "rec-missing", "kind": "note", "title": "Missing", "body": "Body text."},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert rejected.receipt["status"] == "rejected"
    assert conflict.receipt["status"] == "conflict"
    assert _snap(instance) == before
    assert _drift(instance) == []


def test_plain_connection_cannot_rebuild_or_wipe_the_index(instance: Path) -> None:
    digest = file_sha(instance / "brain.sqlite3")
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        before = store.search_counts(con)
        with pytest.raises(RuntimeError, match="transaction"):
            store.rebuild_search(con)
        con.execute("BEGIN")
        with pytest.raises(sqlite3.OperationalError, match="brain_applier_context"):
            store.rebuild_search(con)
        con.execute("ROLLBACK")
        assert store.search_counts(con) == before
    finally:
        con.close()
    assert file_sha(instance / "brain.sqlite3") == digest


def test_rebuild_search_is_reserved_for_the_brain_agent(instance: Path, apply, tmp_path: Path) -> None:
    seed(apply)
    before = _snap(instance)
    outcome = apply(make_packet("tester", {"op": "rebuild_search"}))
    assert outcome.receipt["status"] == "rejected"
    assert "not allowed for agent tester" in outcome.receipt["error"]
    assert _snap(instance) == before
    path = tmp_path / "granted.toml"
    from tests.conftest import config_with

    path.write_text(
        config_with(agents={"tester": {"operations": ["rebuild_search"]}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="rebuild_search"):
        load_config(path)
    assert load_config(instance / "brain.toml").agents["tester"].allows("rebuild_search") is False


def test_rebuild_search_must_be_the_only_op_and_takes_no_row(instance: Path, apply) -> None:
    from brain.validate import validate_packet

    seed(apply)
    before = _snap(instance)
    mixed = {
        "packet_version": 1,
        "change_id": "tester-rebuild-mix",
        "agent": "tester",
        "intent": "mix a rebuild with a source",
        "ops": [
            {"op": "rebuild_search"},
            source_op("src-mixed", "A source that must not land with a rebuild."),
        ],
    }
    rowed = {
        "packet_version": 1,
        "change_id": "tester-rebuild-row",
        "agent": "tester",
        "intent": "rebuild with a row",
        "ops": [{"op": "rebuild_search", "row": {}}],
    }
    mixed_errors = validate_packet(mixed)
    row_errors = validate_packet(rowed)
    assert any(
        item["path"] == "/ops/0/op" and "must be the only op" in item["message"]
        for item in mixed_errors
    )
    assert any(
        item["path"] == "/ops/0/row" and "is not allowed" in item["message"] for item in row_errors
    )
    first = apply(mixed)
    second = apply(rowed)
    assert first.receipt["status"] == "rejected"
    assert second.receipt["status"] == "rejected"
    assert _snap(instance) == before


def test_brain_search_rebuild_repairs_drift_and_is_logged(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        rowid = con.execute("SELECT rowid FROM search_index LIMIT 1").fetchone()[0]
        con.execute("DELETE FROM search_index WHERE rowid = ?", (rowid,))
        con.commit()
    finally:
        con.close()
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    assert "search drift" in check.stdout
    result = subprocess.run(
        ["brain", "search-rebuild", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "applied"
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        docs, keys, indexed = store.search_counts(con)
    finally:
        con.close()
    assert payload["indexed"] == docs == keys == indexed
    row = ro_rows(
        instance,
        "SELECT agent, intent, targets_json FROM write_log ORDER BY applied_seq DESC LIMIT 1",
    )[0]
    assert row[0] == "brain"
    assert row[1] == "rebuild the search index (manual)"
    assert json.loads(row[2]) == [{"table": "search_index", "id": "*", "action": "rebuild"}]
    assert _drift(instance) == []
    again = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert again.returncode == 0


def test_post_commit_check_is_targeted_and_rebuild_is_full(instance: Path, apply, monkeypatch) -> None:
    seed(apply)
    drift_calls: list[int] = []
    target_calls: list[list[dict]] = []
    real_drift = store.search_drift
    real_targets = store.search_targets_drift

    def spy_drift(con):
        drift_calls.append(1)
        return real_drift(con)

    def spy_targets(con, targets):
        target_calls.append(list(targets))
        return real_targets(con, targets)

    monkeypatch.setattr(store, "search_drift", spy_drift)
    monkeypatch.setattr(store, "search_targets_drift", spy_targets)
    outcome = apply(make_packet("tester", source_op("src-spy", "Spy source text.")))
    assert outcome.exit_code == 0
    assert drift_calls == []
    assert target_calls == [[{"table": "sources", "id": "src-spy", "action": "insert"}]]
    drift_calls.clear()
    target_calls.clear()
    from brain.init import search_rebuild

    rebuilt = search_rebuild(instance)
    assert rebuilt.exit_code == 0
    assert drift_calls == [1]
    assert target_calls == []


def test_missing_search_trigger_refuses_apply(instance: Path, apply) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("DROP TRIGGER search_records_au")
        con.commit()
    finally:
        con.close()
    digest = file_sha(instance / "brain.sqlite3")
    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    with pytest.raises(Unavailable, match="search_records_au"):
        apply(make_packet("tester", source_op("src-missing-trigger", "Must not write.")))
    assert file_sha(instance / "brain.sqlite3") == digest
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    assert "search_records_au" in check.stdout


def test_missing_search_object_after_commit_pauses_the_store(instance: Path, apply, monkeypatch) -> None:
    original = Applier._finish

    def drop_then_finish(self, change_id: str):
        self.con.execute("DROP TRIGGER IF EXISTS search_records_au")
        return original(self, change_id)

    monkeypatch.setattr(Applier, "_finish", drop_then_finish)
    first = apply(make_packet("tester", source_op("src-search-pause", "A source that trips the pause.")))
    assert first.receipt["status"] == "applied"
    assert first.exit_code == 5
    assert "search_records_au" in (instance / "PAUSED").read_text()
    monkeypatch.undo()
    with pytest.raises(Unavailable):
        apply(make_packet("tester", source_op("src-after-search-pause", "Must not apply.")))


def test_tampered_sync_trigger_pauses_on_the_next_write(instance: Path, apply) -> None:
    seed(apply)
    created = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {"id": "rec-tamper", "kind": "note", "title": "Before", "body": "Body text."},
                "src-note-anon",
            ),
        )
    )
    assert created.exit_code == 0
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.executescript(
            """
            DROP TRIGGER search_records_au;
            CREATE TRIGGER search_records_au AFTER UPDATE ON records
            BEGIN
              SELECT 0 WHERE 0;
            END;
            """
        )
    finally:
        con.close()
    updated = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {"id": "rec-tamper", "title": "After the tamper"},
                "src-note-anon",
                base=1,
            ),
        )
    )
    assert updated.receipt["status"] == "applied"
    assert updated.exit_code == 5
    assert "search index out of sync for record rec-tamper" in (instance / "PAUSED").read_text()


def test_long_lived_applier_passes_integrity_after_an_outside_fts_write(instance: Path, applier, apply) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("INSERT INTO search_index(search_index) VALUES ('optimize')")
        con.commit()
    finally:
        con.close()
    assert store.integrity_problems(applier.con) == []
    outcome = apply(make_packet("tester", source_op("src-after-optimize", "After optimize.")))
    assert outcome.integrity == "ok"
    assert outcome.exit_code == 0
    assert not (instance / "PAUSED").exists()


def test_migration_adds_the_index_to_an_existing_store(tmp_path: Path) -> None:
    instance = v1_instance(tmp_path)
    result = subprocess.run(
        ["brain", "migrate", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["from"] == 1
    assert payload["to"] == migrate.latest_version()
    assert payload["search_rebuild"]["status"] == "applied"
    assert ro_rows(instance, "SELECT agent, intent FROM write_log") == [
        ("brain", f"rebuild the search index (migrate 1 to {migrate.latest_version()})")
    ]
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        assert store.search_counts(con) == (5, 5, 5)
        assert store.search_drift(con) == []
        assert store.missing_guards(con) == []
    finally:
        con.close()
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stdout


def test_update_before_rebuild_stays_searchable(tmp_path: Path) -> None:
    from brain import migrate

    instance = v1_instance(tmp_path)
    old, new = migrate.migrate(instance / "brain.sqlite3")
    assert (old, new) == (1, migrate.latest_version())
    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet(
                    "tester",
                    upsert_op(
                        "upsert_record",
                        {"id": "rec-old-a", "title": "Old kettle descaling note"},
                        "src-old-notes",
                        base=1,
                    ),
                )
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied"
    assert outcome.exit_code == 0
    assert ro_rows(
        instance,
        "SELECT ref_id FROM search_index WHERE search_index MATCH '\"descaling\"'",
    ) == [("rec-old-a",)]
    assert ro_rows(
        instance,
        "SELECT 1 FROM search_keys WHERE entity = 'record' AND ref_id = 'rec-old-b'",
    ) == []
    assert any("not indexed" in item and "record:rec-old-b" in item for item in _drift(instance))
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    from brain.init import search_rebuild

    rebuilt = search_rebuild(instance)
    assert rebuilt.exit_code == 0
    assert _drift(instance) == []


def test_migrate_reports_a_failed_rebuild(tmp_path: Path, monkeypatch, capsys) -> None:
    from brain import cli

    instance = v1_instance(tmp_path)

    def boom(*_args, **_kwargs):
        raise Unavailable("rebuild failed")

    monkeypatch.setattr(cli, "search_rebuild", boom)
    code = cli.main(["migrate", "--instance", str(instance)])
    captured = capsys.readouterr()
    assert code == 5
    assert json.loads(captured.out) == {"from": 1, "to": migrate.latest_version(), "search_rebuild": None}
    assert "rebuild failed" in captured.err
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrate.latest_version()
    finally:
        con.close()


def test_cli_refuses_without_fts5(instance: Path, monkeypatch, capsys) -> None:
    from brain import cli

    assert store.fts5_missing_message() is None
    monkeypatch.setattr(store, "fts5_missing_message", lambda: "no fts5 here")
    assert cli.main(["--version"]) == 5
    first = capsys.readouterr()
    assert "no fts5 here" in first.err
    assert cli.main(["check", "--instance", str(instance)]) == 5
    second = capsys.readouterr()
    assert "no fts5 here" in second.err


def test_wal_mode_is_refused(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        mode = con.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    finally:
        con.close()
    assert mode == "wal"
    writer = Applier(instance)
    try:
        with pytest.raises(Unavailable, match="journal mode is wal"):
            writer.apply(
                json.dumps(
                    make_packet("tester", source_op("src-wal", "Must not write in wal mode."))
                ).encode()
            )
    finally:
        writer.close()
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 1
    assert "journal mode is wal" in check.stdout
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        con.execute("PRAGMA journal_mode = DELETE")
    finally:
        con.close()
    writer = Applier(instance)
    try:
        outcome = writer.apply(
            json.dumps(
                make_packet("tester", source_op("src-wal-ok", "Writes again in delete mode."))
            ).encode()
        )
    finally:
        writer.close()
    assert outcome.exit_code == 0
    names = {path.name for path in instance.iterdir()}
    assert not any(name.endswith("-wal") or name.endswith("-shm") for name in names)
