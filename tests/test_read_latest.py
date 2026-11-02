"""Finding the newest note. get on a project lists the records in it that changed last, a query that names a
project starts with that project and its newest record, and every search hit shows its date, so an agent asking
where a project stands reads the latest state note and not the oldest notes that share a title."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from brain import reads
from brain.applier import Applier
from brain.init import init_instance
from tests.conftest import make_packet, render_config, source_op, upsert_op

NOW = "2026-10-26T06:00:00Z"


def _record(rid: str, project: str, title: str, **extra) -> dict:
    row = {"id": rid, "kind": "note", "status": "active", "project_id": project, "title": title,
           "body": f"{title}."}
    row.update(extra)
    return upsert_op("upsert_record", row, "src-latest")


@pytest.fixture
def latest(tmp_path: Path) -> Path:
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(timezone="Australia/Brisbane"))["instance"])
    writer = Applier(instance)
    try:
        packets = [
            make_packet("tester", source_op("src-latest", "Where the pond build stands.")),
            make_packet("tester", upsert_op("upsert_project", {"id": "pond-build", "kind": "project",
                                                               "title": "Wildlife pond build"}, "src-latest"),
                        upsert_op("upsert_project", {"id": "shed", "kind": "project", "title": "Shed"}, "src-latest")),
            make_packet("tester", source_op("src-pond-1", "progress note: waiting on the liner delivery.",
                                            title="progress note on pond-build", date="2026-10-13"),
                        source_op("src-pond-2", "progress note: the liner is down and the first plants are in.",
                                  title="progress note on pond-build", date="2026-10-25")),
            make_packet("tester", _record("rec-pond-old", "pond-build", "Liner quote"),
                        _record("rec-pond-state", "pond-build", "Where pond-build stands"),
                        _record("rec-pond-plan", "pond-build", "Pond plan"),
                        _record("rec-pond-gone", "pond-build", "Pond notes replaced"),
                        _record("rec-shed", "shed", "Shed note")),
        ]
        for packet in packets:
            outcome = writer.apply(json.dumps(packet).encode())
            assert outcome.receipt["status"] == "applied", outcome.receipt
    finally:
        writer.close()
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.create_function("brain_applier_context", 0, lambda: 1)
    try:
        # When each record last changed, set by hand so the order doesn't depend on the clock.
        for rid, stamp, status in (("rec-pond-old", "2026-10-13T01:00:00Z", "active"),
                                   ("rec-pond-plan", "2026-10-14T06:00:00Z", "active"),
                                   ("rec-pond-state", "2026-10-25T15:30:00Z", "active"),
                                   ("rec-pond-gone", "2026-10-26T01:00:00Z", "superseded")):
            con.execute("UPDATE records SET updated_at = ?, status = ?, version = version + 1 WHERE id = ?",
                        (stamp, status, rid))
        con.commit()
    finally:
        con.close()
    return instance


def _read(instance: Path, tool: str, **arguments) -> dict:
    result = reads.run_tool(instance, tool, arguments, now=NOW)
    assert not result.is_error, result.text
    return json.loads(result.text)


def test_get_on_a_project_lists_its_newest_records_first(latest) -> None:
    recent = _read(latest, "get", entity="project", id="pond-build")["related"]["recent_records"]
    assert [item["id"] for item in recent["items"]] == ["rec-pond-state", "rec-pond-plan", "rec-pond-old"]
    assert recent["items"][0] == {"id": "rec-pond-state", "kind": "note", "title": "Where pond-build stands",
                                  "status": "active", "updated_at": "2026-10-25T15:30:00Z"}
    # The superseded record changed last, but it isn't where the project stands.
    assert recent["total"] == 3


@pytest.mark.parametrize("query", ["pond-build", "Pond build", "Wildlife pond build"])
def test_a_query_naming_a_project_starts_with_it_and_its_newest_record(latest, query) -> None:
    hits = _read(latest, "search", query=query, limit=5)["hits"]
    assert [(hit["entity"], hit["id"]) for hit in hits[:2]] == [("project", "pond-build"),
                                                                ("record", "rec-pond-state")]
    assert all(hit["pinned"] is True and hit["score"] is None for hit in hits[:2])
    assert not any(hit.get("pinned") for hit in hits[2:])
    # A pinned row isn't listed a second time further down.
    assert len({(hit["entity"], hit["id"]) for hit in hits}) == len(hits)


def test_a_query_that_only_mentions_a_project_pins_nothing(latest) -> None:
    hits = _read(latest, "search", query="pond-build progress", limit=5)["hits"]
    assert hits and not any(hit.get("pinned") for hit in hits)


def test_pins_follow_the_page_and_the_filters(latest) -> None:
    assert not any(hit.get("pinned") for hit in _read(latest, "search", query="pond-build", offset=1)["hits"])
    sources = _read(latest, "search", query="pond-build", entities=["source"])["hits"]
    assert sources and {hit["entity"] for hit in sources} == {"source"}
    records = _read(latest, "search", query="pond-build", entities=["record"])["hits"]
    assert records[0]["id"] == "rec-pond-state" and records[0]["pinned"] is True
    assert not any(hit.get("pinned") for hit in _read(latest, "search", query="pond-build", under="shed")["hits"])


def test_every_hit_shows_its_date(latest) -> None:
    hits = {hit["id"]: hit for hit in _read(latest, "search", query="pond-build progress", limit=25)["hits"]}
    assert hits["src-pond-1"]["date"] == "2026-10-13" and hits["src-pond-2"]["date"] == "2026-10-25"
    pinned = {hit["id"]: hit for hit in _read(latest, "search", query="pond-build")["hits"]}
    # 15.30 UTC on 25 October is 1.30am on 26 October in Brisbane, the instance's time zone.
    assert pinned["rec-pond-state"]["date"] == "2026-10-26"
