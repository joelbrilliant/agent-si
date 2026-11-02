from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from brain.init import render_router
from brain.migrate import latest_version, migrate
from tests.conftest import NOW, TEST_CONFIG, make_packet, ro_rows, source_op, upsert_op


def test_migration_0003_adds_meetings_with_guards(instance: Path) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == latest_version()
        strict = con.execute(
            "SELECT strict FROM pragma_table_list WHERE schema = 'main' AND name = 'meetings'"
        ).fetchone()
        assert strict == (1,)
        triggers = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'trigger' AND tbl_name = 'meetings'"
            )
        }
        assert triggers == {
            "guard_meetings_insert",
            "guard_meetings_update",
            "guard_meetings_delete",
        }
        indexes = {row[1] for row in con.execute("PRAGMA index_list(meetings)")}
        assert {"meetings_by_start", "meetings_by_project"} <= indexes
    finally:
        con.close()
    plain = sqlite3.connect(instance / "brain.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError, match="brain_applier_context"):
            plain.execute("INSERT INTO meetings DEFAULT VALUES")
    finally:
        plain.close()


def test_upsert_meeting_creates_and_updates_a_meeting(instance: Path, apply) -> None:
    created = apply(make_packet("tester", *_calendar("thr-planning", "evt-planning"), _meeting()))
    assert created.receipt["status"] == "applied", created.receipt
    assert any(item["table"] == "meetings" for item in created.receipt["targets"])
    updated = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_meeting",
                {"id": "mtg-planning", "status": "tentative"},
                "src-meeting",
                base=1,
            ),
        )
    )
    assert updated.receipt["status"] == "applied", updated.receipt
    assert any(item["table"] == "meetings" for item in updated.receipt["targets"])
    assert ro_rows(
        instance, "SELECT version, status FROM meetings WHERE id = 'mtg-planning'"
    ) == [(2, "tentative")]


def test_meeting_needs_a_calendar_thread(instance: Path, apply) -> None:
    prepared = apply(
        make_packet(
            "tester",
            source_op("src-meeting", "Email thread notes for a meeting that is not calendar."),
            upsert_op(
                "upsert_thread",
                {
                    "id": "thr-mail",
                    "channel": "email",
                    "account": "work",
                    "external_id": "msg-mail",
                },
                "src-meeting",
            ),
        )
    )
    assert prepared.receipt["status"] == "applied", prepared.receipt
    refused = apply(make_packet("tester", _meeting(thread_id="thr-mail", meeting_id="mtg-mail")))
    assert refused.receipt["status"] == "rejected"
    assert (
        refused.receipt["error"]
        == "op 0 (upsert_meeting): thread thr-mail is not a calendar thread"
    )


def test_meeting_times_may_be_in_the_future(instance: Path, apply) -> None:
    start = datetime.strptime(NOW, "%Y-%m-%dT%H:%M:%SZ") + timedelta(days=30)
    end = start + timedelta(hours=1)
    outcome = apply(
        make_packet(
            "tester",
            *_calendar("thr-future", "evt-future"),
            _meeting(
                thread_id="thr-future",
                meeting_id="mtg-future",
                starts_at=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ends_at=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
    )
    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert ro_rows(instance, "SELECT id FROM meetings WHERE id = 'mtg-future'") == [("mtg-future",)]


def test_meeting_ends_after_it_starts(apply) -> None:
    outcome = apply(
        make_packet(
            "tester",
            *_calendar("thr-order", "evt-order"),
            _meeting(
                thread_id="thr-order",
                meeting_id="mtg-order",
                starts_at="2026-10-08T02:00:00Z",
                ends_at="2026-10-08T01:00:00Z",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"].startswith("op 2 (upsert_meeting):")


def test_migrate_adds_meetings_to_a_schema_2_store(tmp_path: Path) -> None:
    migrations = tmp_path / "schema-v2"
    migrations.mkdir()
    packaged = Path(__file__).resolve().parents[1] / "src" / "brain" / "migrations"
    for name in ("0001_core.sql", "0002_search.sql"):
        (migrations / name).write_text((packaged / name).read_text(encoding="utf-8"), encoding="utf-8")
    instance = tmp_path / "demo"
    instance.mkdir()
    (instance / "brain.toml").write_text(TEST_CONFIG, encoding="utf-8")
    (instance / "ROUTER.md").write_text(render_router("demo"), encoding="utf-8")
    migrate(instance / "brain.sqlite3", create=True, migrations_dir=migrations)
    before = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    result = subprocess.run(
        ["brain", "migrate", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"from": 2, "to": latest_version(), "search_rebuild": None}
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == before
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stdout


def _calendar(thread_id: str, external_id: str) -> tuple[dict, dict]:
    return (
        source_op("src-meeting", "Calendar notes for the planning meeting."),
        upsert_op(
            "upsert_thread",
            {
                "id": thread_id,
                "channel": "calendar",
                "account": "work-cal",
                "external_id": external_id,
            },
            "src-meeting",
        ),
    )


def _meeting(
    *,
    thread_id: str = "thr-planning",
    meeting_id: str = "mtg-planning",
    starts_at: str = "2026-10-08T01:00:00Z",
    ends_at: str = "2026-10-08T02:00:00Z",
) -> dict:
    return upsert_op(
        "upsert_meeting",
        {
            "id": meeting_id,
            "thread_id": thread_id,
            "title": "Planning",
            "starts_at": starts_at,
            "ends_at": ends_at,
            "status": "confirmed",
        },
        "src-meeting",
    )
