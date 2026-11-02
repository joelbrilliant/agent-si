"""Fast path: settings parsed once per file content, the search objects checked once per store
version, and one full integrity check per batch of bulk writes. The evidence gate and the per-commit check for
single writes are unchanged."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from brain import config as config_mod
from brain import reads, store
from brain.applier import Applier
from tests.conftest import make_packet, source_op


def test_config_is_parsed_once_per_content_and_an_edit_is_read_fresh(instance: Path) -> None:
    path = instance / "brain.toml"
    first = config_mod.load_config(path)
    assert config_mod.load_config(path) is first
    text = path.read_text()
    path.write_text(text.replace('timezone = "UTC"', 'timezone = "Australia/Brisbane"', 1)
                    if 'timezone = "UTC"' in text else text + "\n# edited\n")
    edited = config_mod.load_config(path)
    assert edited is not first
    path.write_text(text)
    assert config_mod.load_config(path) == first


def test_a_dropped_search_object_is_still_found_after_a_good_search(instance: Path) -> None:
    ok = reads.run_tool(instance, "search", {"query": "anything"})
    assert not ok.is_error
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        kind, name = next(item for item in store.required_search_objects(version) if item[0] == "trigger")
        con.execute(f'DROP TRIGGER "{name}"')
    finally:
        con.close()
    failed = reads.run_tool(instance, "search", {"query": "anything"})
    assert failed.is_error
    assert name in failed.text


def _bulk(writer: Applier, count: int) -> list:
    return [
        writer.apply(json.dumps(make_packet("tester", source_op(f"src-bulk-{i}", f"Bulk source {i}."))).encode())
        for i in range(count)
    ]


def test_a_batch_runs_one_full_check_when_it_ends(instance: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    original = store.integrity_problems

    def spy(con):
        result = original(con)
        calls.append(result)
        return result

    monkeypatch.setattr(store, "integrity_problems", spy)
    writer = Applier(instance, batch=True)
    try:
        outcomes = _bulk(writer, 5)
        assert [outcome.receipt["status"] for outcome in outcomes] == ["applied"] * 5
        assert {outcome.check for outcome in outcomes} == {"targeted"}
        assert calls == []
        assert writer.end_batch() == []
        assert calls == [[]]
    finally:
        writer.close()
    assert len(calls) == 1  # nothing was written after end_batch, so close adds no second check


def test_close_runs_the_batch_check_and_a_problem_pauses(instance: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "integrity_problems", lambda con: ["synthetic problem"])
    writer = Applier(instance, batch=True)
    try:
        _bulk(writer, 2)
        assert not (instance / "PAUSED").exists()
    finally:
        writer.close()
    paused = json.loads((instance / "PAUSED").read_text())
    assert paused["batch"] is True and paused["problems"] == ["synthetic problem"]


def test_a_single_writer_still_checks_every_commit(instance: Path, monkeypatch) -> None:
    calls: list[int] = []
    original = store.integrity_problems
    monkeypatch.setattr(store, "integrity_problems", lambda con: calls.append(1) or original(con))
    writer = Applier(instance)
    try:
        outcomes = _bulk(writer, 3)
    finally:
        writer.close()
    assert {outcome.check for outcome in outcomes} == {"full"}
    assert len(calls) == 3
