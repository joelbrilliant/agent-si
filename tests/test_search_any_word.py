"""Ranking by any word. A record that holds most of the query's words ranks above long notes that happen to hold
every word, and a search no result matches in full says so. Every query ranks by any word, relaxed means no result has
every word, and a hit is marked as a note match only when a word it matched is in its note alone."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import reads
from brain.applier import Applier
from brain.init import init_instance
from tests.conftest import make_packet, render_config, source_op, upsert_op

NOW = "2026-10-29T06:00:00Z"
FILLER = " ".join(["The day's notes covered the store and the jobs."] * 4)


def _record(rid: str, title: str, body: str) -> dict:
    row = {"id": rid, "kind": "note", "status": "active", "project_id": "fetch-jobs", "title": title, "body": body}
    return upsert_op("upsert_record", row, "src-ops")


@pytest.fixture
def ops(tmp_path: Path) -> Path:
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(timezone="Australia/Brisbane"))["instance"])
    writer = Applier(instance)
    try:
        packets = [
            make_packet("tester", source_op("src-ops", "How the weekly fetch and the handovers run.")),
            make_packet("tester", upsert_op("upsert_project", {"id": "fetch-jobs", "kind": "project", "title": "Fetch jobs"},
                                            "src-ops")),
            make_packet(
                "tester",
                _record("rec-yard-round", "Yard round: Pip fetches Compost, Mulch and Straw weekly",
                        "Pip fetches what's new every week on Saturdays."),
                _record("rec-handover", "Saturday handover",
                        f"{FILLER} Pip's queue was quiet. The cart needed a pull. Compost showed two empty "
                        f"bins. Mulch and Straw had nothing new. {FILLER}"),
                _record("rec-pip-straw", "Pip and Compost", "Straw was delivered on Monday."),
                _record("rec-pip-only", "Pip's role", "He fetches and never writes."),
            ),
        ]
        for packet in packets:
            outcome = writer.apply(json.dumps(packet).encode())
            assert outcome.receipt["status"] == "applied", json.dumps(outcome.receipt)
    finally:
        writer.close()
    return instance


def _search(instance: Path, **arguments) -> dict:
    result = reads.run_tool(instance, "search", arguments, now=NOW)
    assert not result.is_error, result.text
    return json.loads(result.text)


def _ids(result: dict) -> list[str]:
    return [hit["id"] for hit in result["hits"]]


def test_a_record_missing_one_word_still_ranks_first(ops) -> None:
    result = _search(ops, query="Pip pull Compost Mulch Straw", entities=["record"])
    # Before ranking by any word, the handover was the only hit, because it alone held "pull".
    assert _ids(result)[0] == "rec-yard-round"
    assert "rec-handover" in _ids(result)
    assert result["match"] == '"Pip" OR "pull" OR "Compost" OR "Mulch" OR "Straw"'


def test_relaxed_means_no_result_has_every_word(ops) -> None:
    # The handover holds every word, so the search isn't relaxed though its first hit lacks "pull".
    strict = _search(ops, query="Pip pull Compost Mulch Straw")
    assert "relaxed" not in strict and strict["warnings"] == []
    loose = _search(ops, query="Pip quokka")
    assert loose["relaxed"] is True
    assert loose["warnings"] == ["No result had every word; these match some of them."]
    # Every page says the same, as the flag is about the whole match, not the page.
    second = _search(ops, query="Pip quokka", limit=1, offset=1)
    assert second["relaxed"] is True and len(second["hits"]) == 1
    nothing = _search(ops, query="quokka wombat")
    assert nothing["hits"] == [] and "relaxed" not in nothing


def test_a_hit_is_a_note_match_only_when_a_word_it_matched_is_in_its_note_alone(ops) -> None:
    hits = {hit["id"]: hit for hit in _search(ops, query="Pip Straw", entities=["record"])["hits"]}
    # "Straw" is only in this record's note, so its match leans on unchecked text.
    assert hits["rec-pip-straw"]["match"] == "note"
    # The title holds both words.
    assert "match" not in hits["rec-yard-round"]
    # This record holds only "Pip", in its title. Missing "Straw" doesn't make it a note match.
    assert "match" not in hits["rec-pip-only"]
    assert hits["rec-handover"]["match"] == "note"
