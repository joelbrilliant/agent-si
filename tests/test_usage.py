"""The content-free read log and brain usage report."""
from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from brain import server, store, usage
from tests.phase2a import custom_instance
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

LINE_KEYS = {"minute", "tool", "client", "token", "bytes", "rows", "outcome", "ms", "session", "first"}


def _serve(inst: Path, calls: list[tuple[str, dict]], client: str = "claude-code") -> list[dict]:
    lines = [{"jsonrpc": "2.0", "id": 0, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": client, "version": "2.1"}}}]
    lines += [{"jsonrpc": "2.0", "id": n + 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
              for n, (name, args) in enumerate(calls)]
    out = io.BytesIO()
    server.serve(inst, io.BytesIO("".join(json.dumps(line) + "\n" for line in lines).encode()), out)
    return [json.loads(line) for line in out.getvalue().decode().splitlines()]


def _logged(inst: Path) -> list[dict]:
    return [json.loads(line) for path in sorted((inst / "usage").glob("*.jsonl")) for line in path.read_text().splitlines()]


def test_serve_logs_one_content_free_line_per_read(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst")
    answers = _serve(inst, [("brief", {}), ("search", {"query": "SecretPhrase Nonword"}), ("fact_get", {"key": "demo.unknown"}),
                            ("records_query", {"entity": "project"}), ("fact_get", {"key": "demo.seats"}),
                            ("search", {"query": "SecretPhrase Example"})])
    assert len(answers) == 7
    # The last search finds nothing with every word, so its any-word hits are logged as a hit.
    assert json.loads(answers[6]["result"]["content"][0]["text"])["relaxed"] is True
    logged = _logged(inst)
    assert [(item["tool"], item["outcome"]) for item in logged] == [
        ("brief", "hit"), ("search", "empty"), ("fact_get", "unknown_key"), ("records_query", "hit"), ("fact_get", "empty"),
        ("search", "hit")]
    # Only a search line says whether any word counted (relaxed), so the log can tell a strict hit from a loose one.
    assert all(set(item) == (LINE_KEYS | {"relaxed"} if item["tool"] == "search" else LINE_KEYS) for item in logged)
    assert [item["relaxed"] for item in logged if item["tool"] == "search"] == [False, True]
    assert [item["first"] for item in logged] == [True, False, False, False, False, False]
    assert {item["client"] for item in logged} == {"claude-code"} and len({item["session"] for item in logged}) == 1
    assert logged[0]["minute"] == "2026-10-09T00:00Z" and all(item["token"] == "" for item in logged)
    text = "".join(path.read_text() for path in (inst / "usage").glob("*.jsonl"))
    assert "SecretPhrase" not in text and "demo.unknown" not in text and "demo.seats" not in text and str(inst) not in text
    assert all(len(line.encode()) < 512 for line in text.splitlines(keepends=True))
    assert logged[3]["rows"] >= 1 and logged[0]["bytes"] == len(answers[1]["result"]["content"][0]["text"].encode())


def test_record_false_switches_it_off(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst", extra_toml="[usage]\nrecord = false\n")
    _serve(inst, [("brief", {})])
    assert not (inst / "usage").exists()


def test_a_hostile_client_name_is_reduced_to_an_identifier(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst")
    _serve(inst, [("brief", {})], client='evil"\n{"x":1}/../' + "A" * 500)
    item = _logged(inst)[0]
    import re

    assert re.fullmatch(r"[A-Za-z0-9 ._-]{1,40}", item["client"]) and item["client"].startswith("evilx1..AAA")


def test_many_writers_append_whole_lines(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst")

    def write_many():
        for _ in range(200):
            usage.record(inst, tool="search", client="c", token="", result_bytes=10, rows=1, result="hit", ms=0.5,
                         session="abcd1234", first=False)

    threads = [threading.Thread(target=write_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(_logged(inst)) == 1600


def test_logging_costs_under_a_millisecond(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst")
    started = time.perf_counter()
    for _ in range(500):
        usage.record(inst, tool="search", client="claude-code", token="", result_bytes=1234, rows=3, result="hit", ms=1.2,
                     session="abcd1234", first=False)
    per_call_ms = (time.perf_counter() - started) * 1000 / 500
    assert per_call_ms < 1.0, per_call_ms  # Measured: about 0.02 ms a line on a laptop


def test_prune_keeps_thirteen_months(tmp_path) -> None:
    folder = tmp_path / "usage"
    folder.mkdir()
    for name in ("2025-08.jsonl", "2025-09.jsonl", "2025-10.jsonl", "2026-10.jsonl", "notes.txt"):
        (folder / name).write_text("")
    assert usage.prune(tmp_path, "2026-10-12T00:00:00Z") == 2
    assert sorted(path.name for path in folder.iterdir()) == ["2025-10.jsonl", "2026-10.jsonl", "notes.txt"]


REPORT_KEYS = {"period", "from", "to", "calls", "hit_rate", "hit_rate_by_tool", "misses", "disputed_reads", "brief_first_rate",
               "sessions_by_client", "reads_by_tool", "bytes", "writes", "tokens", "relaxed_searches"}


def _strings(value, path=""):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield path + "/key", key
            yield from _strings(item, f"{path}/{key}")
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, path + "[]")


def test_the_report_holds_counts_and_enums_only(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst")
    _serve(inst, [("brief", {}), ("search", {"query": "SecretPhrase"}), ("get", {"entity": "project", "id": "general"})])
    _serve(inst, [("search", {"query": "pilot"})], client="codex")
    from brain.cli import main

    out = io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out):
        assert main(["usage", "report", "--period", "week", "--instance", str(inst)]) == 0
    paths = json.loads(out.getvalue())
    data = json.loads(Path(paths["json"]).read_text())
    assert set(data) == REPORT_KEYS
    assert data["calls"] == 4 and data["brief_first_rate"] == 0.5 and data["sessions_by_client"] == {"claude-code": 1, "codex": 1}
    assert set(data["misses"]) == {"empty", "unknown_key", "ambiguous_key", "error"} and data["misses"]["empty"] == 2
    assert set(data["reads_by_tool"]) == {"brief", "search", "get"}
    assert set(data["reads_by_tool"]["search"]) == {"calls", "median_bytes", "p95_bytes", "errors", "median_ms"}
    assert set(data["writes"]) == {"applied", "held", "rejected", "held_now", "oldest_held_hours", "approved", "refused"}
    assert data["relaxed_searches"] == {"relaxed": 0, "of": 2}
    allowed_words = {"week", "unknown", "brief", "search", "get", "claude-code", "codex"} | REPORT_KEYS | {
        "calls", "median_bytes", "p95_bytes", "errors", "median_ms", "empty", "unknown_key", "ambiguous_key", "error",
        "applied", "held", "rejected", "held_now", "oldest_held_hours", "approved", "refused", "relaxed", "of"}
    for where, text in _strings(data):
        assert text in allowed_words or (where in ("/from", "/to") and text.startswith("2026-")), (where, text)
    markdown = Path(paths["markdown"]).read_text()
    assert markdown.startswith("# Brain usage, week to 2026-10-09") and "SecretPhrase" not in markdown
    assert "- Searches where any word counted: 0 of 2" in markdown


@pytest.mark.parametrize("table, message", [("record = 1", "usage.record must be true or false"), ("send = true", "unknown key usage.send")])
def test_usage_config(tmp_path, table, message) -> None:
    from brain.config import ConfigError
    from tests.phase2a import custom_config, load_text

    with pytest.raises(ConfigError, match=message):
        load_text(tmp_path, custom_config(extra_toml=f"[usage]\n{table}\n"))


def test_model_tokens_come_from_agent_memory_when_it_is_installed(tmp_path, monkeypatch, clock: Clock) -> None:
    import sqlite3

    from brain import usage

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("AGENT_SESSION_DB", raising=False)
    inst = custom_instance(tmp_path / "inst")
    assert usage.report(inst, "week")["tokens"] == "unknown"  # agent memory isn't installed
    db = tmp_path / "data" / "agent-memory" / "usage.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE session_usage (harness TEXT, session TEXT, started_at TEXT, ended_at TEXT, model TEXT, "
                "input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER, tool_calls INTEGER, "
                "brain_tool_calls INTEGER, project TEXT)")
    rows = [("claude", "a", "2026-10-08T01:00:00Z", "2026-10-08T01:10:00Z", "m", 100, 9000, 400, 80, 2, 1, None),
            ("claude", "b", "2026-10-08T02:00:00Z", "2026-10-08T02:10:00Z", "m", 50, 1000, 0, 20, 1, 0, None),
            ("hermes", "c", "2026-10-08T03:00:00Z", None, "m", 500, None, None, 60, None, None, None),
            ("claude", "old", "2026-09-01T01:00:00Z", "2026-09-01T01:10:00Z", "m", 7, 7, 7, 7, 0, 0, None)]
    con.executemany("INSERT INTO session_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    before = db.read_bytes()
    tokens = usage.report(inst, "week")["tokens"]
    assert tokens == {"claude": {"sessions": 2, "input": 150, "cache_read": 10000, "cache_write": 400, "output": 100},
                      "hermes": {"sessions": 1, "input": 500, "cache_read": "unknown", "cache_write": "unknown", "output": 60}}
    assert db.read_bytes() == before  # read-only
    text = usage.markdown(usage.report(inst, "week"))
    assert "- Model tokens: claude 2 sessions, input 150, cache read 10000, cache write 400, output 100; hermes 1 sessions" in text
    # AGENT_SESSION_DB moves it, as agent memory's own setting does.
    monkeypatch.setenv("AGENT_SESSION_DB", str(tmp_path / "elsewhere" / "sessions.db"))
    assert usage.report(inst, "week")["tokens"] == "unknown"
