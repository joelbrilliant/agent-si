"""The Intake line of the brief and reads.intake_status. Every name and time here is invented."""
from __future__ import annotations

import contextlib
import io
import json
from datetime import datetime, timedelta
from pathlib import Path

from brain import reads, store
from brain.config import load_config
from tests.conftest import context_connection
from tests.phase2a import custom_config, custom_instance
from tests.test_strict import assert_worst_brief, worst_brief_instance
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

NOW = "2026-10-08T00:00:00Z"
LINE = "Intake: work-docs failing since 2026-10-07 (folder). Ask the person to run brain-fetch check."
FOLDER = "/Users/example/Documents/Example Co"
ERROR = "folder: folder is missing or unreadable"


def at(minutes_before: float, base: str = NOW) -> str:
    stamp = datetime.strptime(base, "%Y-%m-%dT%H:%M:%SZ") - timedelta(minutes=minutes_before)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def add_runs(instance: Path, name: str, rows: list[tuple[str, str, str]]) -> None:
    """collector_runs rows (started_at, status, error) written straight into the store, as brain-fetch's runs would."""
    con = context_connection(instance)
    try:
        for index, (started, status, error) in enumerate(rows):
            con.execute(
                "INSERT INTO collector_runs (id, collector, started_at, finished_at, status, items, input_sha256, error, "
                "recorded_at, recorded_by) VALUES (?, ?, ?, ?, ?, 0, '', ?, ?, 'workdocs:test')",
                (f"run-{name}-{started.lower().replace(':', '')}-{index}", name, started, started, status, error, started),
            )
        con.commit()
    finally:
        con.close()


def brief(instance: Path, now: str = NOW) -> list[str]:
    result = reads.run_tool(instance, "brief", {}, now=now)
    assert result.is_error is False, result.text
    return result.text.splitlines()


def intake_lines(instance: Path, now: str = NOW) -> list[str]:
    return [line for line in brief(instance, now) if line.startswith("Intake:")]


def folder_instance(tmp_path: Path, **kwargs) -> Path:
    return custom_instance(tmp_path, table={"live_folder": FOLDER}, **kwargs)


def day(hour: int, minute: int = 0, date: str = "2026-10-07") -> str:
    return f"{date}T{hour:02d}:{minute:02d}:00Z"


def test_three_failed_runs_raise_the_intake_line(tmp_path: Path, clock: Clock) -> None:
    instance = folder_instance(tmp_path)
    assert intake_lines(instance) == []  # no runs: neither failing nor silent
    add_runs(instance, "work-docs", [(day(23, 30, "2026-10-06"), "ok", ""), (day(0), "error", ERROR), (day(0, 30), "error", ERROR)])
    assert intake_lines(instance, day(0, 40)) == []  # two errors do not
    add_runs(instance, "work-docs", [(day(1), "error", ERROR)])
    assert intake_lines(instance, day(1, 10)) == [LINE]
    # since is the first error after the last non-error run.
    entry = reads.intake_status(_con(instance), day(1, 10), load_config(instance / "brain.toml"))[0]
    assert (entry["since"], entry["cause"], entry["runs"]) == ("2026-10-07", "folder", 3)
    # An ok run takes it away; a new streak of three brings it back, counted from the ok run.
    add_runs(instance, "work-docs", [(day(1, 30), "ok", "")])
    assert intake_lines(instance, day(1, 40)) == []
    add_runs(instance, "work-docs", [(day(2), "error", ERROR), (day(2, 30), "error", ERROR), (day(3), "error", ERROR)])
    assert intake_lines(instance, day(3, 10)) == [LINE]
    # A partial run takes it away too.
    add_runs(instance, "work-docs", [(day(3, 30), "partial", "the document is incomplete")])
    assert intake_lines(instance, day(3, 40)) == []


def test_runs_not_rows_count(tmp_path: Path, clock: Clock) -> None:
    instance = folder_instance(tmp_path)
    # Three runs of three exports each, seconds apart; the last export of every run errors.
    rows = []
    for number in range(3):
        start = datetime.strptime(day(number // 2, 30 * number % 60), "%Y-%m-%dT%H:%M:%SZ")
        stamps = [(start + timedelta(seconds=gap)).strftime("%Y-%m-%dT%H:%M:%SZ") for gap in (0, 4, 9)]
        rows += [(stamps[0], "partial", "the document is incomplete"), (stamps[1], "partial", "the document is incomplete"),
                 (stamps[2], "error", ERROR)]
    add_runs(instance, "work-docs", rows)
    assert intake_lines(instance, day(1, 10)) == [LINE]
    # The same rows with a partial last export do not raise it.
    other = folder_instance(tmp_path / "other")
    add_runs(other, "work-docs", [(stamp, "partial", "the document is incomplete") for stamp, _s, _e in rows])
    assert intake_lines(other, day(1, 10)) == []
    # Rows 30 minutes apart are separate runs: three of them raise the line, two do not.
    apart = folder_instance(tmp_path / "apart")
    add_runs(apart, "work-docs", [(day(0), "error", ERROR), (day(0, 30), "error", ERROR)])
    assert intake_lines(apart, day(0, 40)) == []
    add_runs(apart, "work-docs", [(day(1), "error", ERROR)])
    assert intake_lines(apart, day(1, 10)) == [LINE]


def test_silent_collector_line(tmp_path: Path, clock: Clock) -> None:
    instance = folder_instance(tmp_path)
    add_runs(instance, "work-docs", [(at(119), "ok", "")])
    assert intake_lines(instance) == []  # 119 minutes: no (3 times 30 is 90, but the floor is 120)
    old = folder_instance(tmp_path / "old")
    add_runs(old, "work-docs", [(at(121), "ok", "")])
    assert intake_lines(old) == ["Intake: work-docs silent since 2026-10-07. Ask the person to run brain-fetch check."]
    # A partial run is a run.
    part = folder_instance(tmp_path / "part")
    add_runs(part, "work-docs", [(at(200), "partial", "the document is incomplete")])
    assert len(intake_lines(part)) == 1
    # No live_folder: nothing to be silent about.
    none = custom_instance(tmp_path / "none")
    add_runs(none, "work-docs", [(at(5000), "ok", "")])
    assert intake_lines(none) == []
    # Larger [fetch] values widen the window past 120 minutes (5 runs of 60 minutes is 300).
    wide = folder_instance(tmp_path / "wide", extra_toml="[fetch]\nstale_after_runs = 5\nevery_minutes = 60\n")
    add_runs(wide, "work-docs", [(at(299), "ok", "")])
    assert intake_lines(wide) == []
    wider = folder_instance(tmp_path / "wider", extra_toml="[fetch]\nstale_after_runs = 5\nevery_minutes = 60\n")
    add_runs(wider, "work-docs", [(at(301), "ok", "")])
    assert len(intake_lines(wider)) == 1
    # A failing collector is reported as failing, not silent.
    both = folder_instance(tmp_path / "both")
    add_runs(both, "work-docs", [(at(24 * 60 - 30 * n), "error", ERROR) for n in range(3)])
    assert intake_lines(both) == [LINE]


def _many(tmp_path: Path, names: list[str], **kwargs) -> Path:
    agents = {name.replace("-", "")[:40]: {"kind": "collector", "operations": ["add_source", "record_collector_run"]} for name in names}
    tables = {
        name: {"type": "capture", "agent": name.replace("-", "")[:40], "origin": name, "live_folder": FOLDER}
        for name in names
    }
    text = custom_config(agents=agents, extra_collectors=tables, **kwargs)
    from brain.init import init_instance

    tmp_path.mkdir(parents=True, exist_ok=True)
    return Path(init_instance("demo", tmp_path, config_text=text)["instance"])


def test_intake_cause_and_clipping(tmp_path: Path, clock: Clock) -> None:
    causes = {"auth": "auth", "network": "network", "folder": "folder", "limit": "limit", "config": "config", "other": "other"}
    for number, (prefix, shown) in enumerate(causes.items()):
        instance = folder_instance(tmp_path / f"c{number}")
        add_runs(instance, "work-docs", [(at(24 * 60 - 30 * n), "error", f"{prefix}: something went wrong") for n in range(3)])
        assert intake_lines(instance) == [f"Intake: work-docs failing since 2026-10-07 ({shown}). Ask the person to run brain-fetch check."]
    for number, text in enumerate(("mass change: 25 documents would change", "no colon at all", "weird: thing")):
        instance = folder_instance(tmp_path / f"n{number}")
        add_runs(instance, "work-docs", [(at(24 * 60 - 30 * n), "error", text) for n in range(3)])
        assert intake_lines(instance) == ["Intake: work-docs failing since 2026-10-07. Ask the person to run brain-fetch check."]
    # The cause comes from the newest error run.
    mixed = folder_instance(tmp_path / "mixed")
    add_runs(mixed, "work-docs", [(at(24 * 60), "error", "network: down"), (at(24 * 60 - 30), "error", "network: down"),
                                 (at(24 * 60 - 60), "error", "auth: refused")])
    assert intake_lines(mixed) == ["Intake: work-docs failing since 2026-10-07 (auth). Ask the person to run brain-fetch check."]
    names = ["a" + "b" * 39, "c" + "d" * 39, "e" + "f" * 39, "g" + "h" * 39]
    instance = _many(tmp_path / "many", names)
    for name in names:
        add_runs(instance, name, [(at(24 * 60 - 30 * n), "error", ERROR) for n in range(3)])
    lines = intake_lines(instance)
    assert len(lines) == 1
    line = lines[0]
    assert len(line.encode()) <= 200 and line.endswith(", +1 more. Ask the person to run brain-fetch check.")
    assert line.count("failing since 2026-10-07 (folder)") == 3
    assert "a" + "b" * 39 not in line and "abbbbbb" in line  # the names were cut with _cut_bytes


def test_brief_budget_with_the_intake_line(tmp_path: Path, clock: Clock) -> None:
    names = ["a" + "b" * 39, "c" + "d" * 39, "e" + "f" * 39, "g" + "h" * 39]
    agents = {name.replace("-", "")[:40]: {"kind": "collector", "operations": ["add_source", "record_collector_run"]} for name in names}
    tables = {name: {"type": "capture", "agent": name, "origin": name, "live_folder": FOLDER} for name in names}
    instance = worst_brief_instance(tmp_path, clock, extra_agents=agents, collectors=tables)
    plain = assert_worst_brief(instance)
    plain_bytes = len("\n".join(plain).encode()) + 1
    assert not any(line.startswith("Intake:") for line in plain)
    for name in names:
        add_runs(instance, name, [(at(24 * 60 - 30 * n), "error", ERROR) for n in range(3)])
    from tests import test_phase7 as p7

    text = p7._brief(instance)
    lines = text.splitlines()
    assert len(lines) <= 40 and len(text.encode()) <= 4096
    assert all(len(line.encode()) <= 200 for line in lines)
    assert sum(1 for line in lines if line.startswith("Intake:")) == 1
    print(f"worst-case brief: {len(plain)} lines and {plain_bytes} bytes; with the Intake line: {len(lines)} lines and {len(text.encode())} bytes")
    [intake] = [line for line in lines if line.startswith("Intake:")]
    assert intake.endswith(", +1 more. Ask the person to run brain-fetch check.")  # cut by name, not clipped at the end
    print(f"Intake line: {len(intake.encode())} bytes: {intake}")


def _check_json(instance: Path) -> dict:
    from brain_fetch.cli import main

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        main(["check", "--instance", str(instance)])
    return json.loads(out.getvalue())


def _con(instance: Path):
    return store.connect_readonly(instance / "brain.sqlite3")


def test_check_and_brief_share_one_status(tmp_path: Path, clock: Clock) -> None:
    clock.at(NOW)
    agents = {"teamdocs": {"kind": "collector", "operations": ["add_source", "record_collector_run"]}}
    tables = {"team-docs": {"type": "capture", "agent": "teamdocs", "origin": "team-docs", "live_folder": FOLDER}}
    instance = folder_instance(tmp_path, agents=agents, extra_collectors=tables)
    add_runs(instance, "work-docs", [(at(24 * 60 - 30 * n), "error", "network: down") for n in range(3)])
    add_runs(instance, "team-docs", [(at(300), "ok", "")])
    config = load_config(instance / "brain.toml")
    entries = reads.intake_status(_con(instance), NOW, config)
    assert [(e["collector"], e["state"], e["since"], e["cause"]) for e in entries] == [
        ("team-docs", "silent", "2026-10-07", ""), ("work-docs", "failing", "2026-10-07", "network")]
    payload = _check_json(instance)
    shown = {item["collector"]: item["intake"] for item in payload["collectors"]}
    assert shown == {entry["collector"]: entry for entry in entries}
    assert payload["ok"] is False
    expected = "Intake: team-docs silent since 2026-10-07, work-docs failing since 2026-10-07 (network). Ask the person to run brain-fetch check."
    assert intake_lines(instance) == [expected]
