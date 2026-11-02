"""The benchmark generator and Tier A.

Tier A is model-free. It builds the invented Example Co instance from the committed dataset in
bench/exampleco/, then checks the figures against bench/budgets.json. Every name, value and host here
is invented.
"""
from __future__ import annotations

import ast
import contextlib
import datetime
import hashlib
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from bench import generate, grading, harness
from brain import reads, store
from brain.cli import main as brain_main
from tests.conftest import ro_rows
from tests import test_phase7 as p7
from tests.test_strict import assert_worst_brief, worst_brief_instance
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

REPO = harness.ROOT
DATASET = harness.DATASET
TODAY = datetime.date(2026, 10, 12)
ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TOOL_ORDER = ["brief", "search", "get", "fact_get", "records_query", "signals"]
NOTE_LABEL = "Written by an agent, unsourced. Not a fact."
HOST = re.compile(r"\b[a-z0-9.-]+\.(?:com|org|net|io|ai|dev|co|au|uk)\b")
WEEKDAY = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)\b",
    re.I,
)


# -- fixtures and helpers ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    return harness.load_manifest()


@pytest.fixture(scope="module")
def questions() -> list[dict]:
    return json.loads((DATASET / "questions.json").read_text(encoding="utf-8"))["questions"]


@pytest.fixture(scope="module")
def budgets() -> dict:
    return json.loads(harness.BUDGETS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def bench(tmp_path_factory) -> Path:
    """The benchmark instance, built once per module. No test writes to it."""
    return harness.build_instance(DATASET, tmp_path_factory.mktemp("bench"))


def committed() -> dict[str, bytes]:
    return {
        path.relative_to(DATASET).as_posix(): path.read_bytes()
        for path in sorted(DATASET.rglob("*"))
        if path.is_file()
    }


def packets() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((DATASET / "packets").glob("*.json"))
    }


def all_ops() -> list[dict]:
    return [op for packet in packets().values() for op in packet["ops"]]


def met(item, text: str) -> bool:
    """The matching rule, as bench/grading.py states it."""
    return grading.met(item, text)


def alternatives(item) -> list[str]:
    return [item] if isinstance(item, str) else list(item)


def call(instance: Path, tool: str, now: str, **arguments) -> reads.ToolResult:
    return reads.run_tool(instance, tool, arguments, now=now)


def write_dataset(files: dict[str, bytes], target: Path) -> Path:
    for path, data in files.items():
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    return target


def run_generate(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = generate.main(argv)
    return code, out.getvalue(), err.getvalue()


def shift_dates(text: str, days: int) -> str:
    return ISO.sub(lambda m: (datetime.date.fromisoformat(m.group(0)) + datetime.timedelta(days=days)).isoformat(), text)


def number(raw: str) -> float:
    short = re.fullmatch(r"([0-9]+)K", raw)
    return int(short.group(1)) * 1000 if short else float(raw.split(" ")[0])


def claim_rows() -> list[dict]:
    return [op["row"] for op in all_ops() if op["op"] == "add_claim"]


# -- 6 to 14: the generator and the dataset -------------------------------------------------------------


def test_generator_is_deterministic_and_matches_the_committed_dataset(manifest) -> None:
    first = generate.generate(1, 1, TODAY)
    assert first == generate.generate(1, 1, TODAY)
    paths = list(first)
    assert paths[-1] == "MANIFEST.json" and paths[:-1] == sorted(paths[:-1])
    files = committed()
    assert sorted(first) == sorted(files)
    assert first == files
    assert set(manifest["files"]) == set(files) - {"MANIFEST.json"}
    for path, digest in manifest["files"].items():
        assert hashlib.sha256(files[path]).hexdigest() == digest, path
    assert (manifest["seed"], manifest["scale"], manifest["today"]) == (1, 1, "2026-10-12")
    assert (manifest["now"], manifest["applied_at"]) == ("2026-10-12T09:00:00Z", "2026-10-09T08:00:00Z")


def test_generator_counts_at_scale_1(bench, manifest) -> None:
    def one(sql: str, params=()):
        return ro_rows(bench, sql, params)[0][0]

    assert one("SELECT count(*) FROM people") == 40
    assert one("SELECT count(*) FROM projects") == 6
    assert ro_rows(bench, "SELECT kind, count(*) FROM projects GROUP BY kind ORDER BY kind") == [
        ("project", 3), ("workstream", 3)]
    assert ro_rows(bench, "SELECT kind, count(*) FROM sources GROUP BY kind ORDER BY kind") == [
        ("config", 1), ("document", 40), ("email", 100), ("meeting_transcript", 45), ("slack", 90), ("user_note", 25)]
    assert one("SELECT count(*) FROM sources") == 301
    assert one("SELECT count(*) FROM checklist_items") == 150
    assert ro_rows(bench, "SELECT status, count(*) FROM actions GROUP BY status ORDER BY status") == [
        ("blocked", 4), ("done", 10), ("open", 24), ("proposed", 4), ("sent", 4), ("waiting", 14)]
    assert one("SELECT count(*) FROM actions") == 60
    assert ro_rows(bench, "SELECT status, count(*) FROM facts GROUP BY status ORDER BY status") == [
        ("current", 70), ("disputed", 10)]
    assert one("SELECT count(*) FROM fact_keys") == 82
    assert ro_rows(bench, "SELECT status, count(*) FROM claims GROUP BY status ORDER BY status") == [
        ("live", 96), ("superseded", 5)]
    assert one("SELECT count(*) FROM decisions") == 6
    assert one("SELECT count(*) FROM records") == 10
    assert one("SELECT count(*) FROM note_meta WHERE written_by = 'loader'") == 20
    assert one("SELECT count(*) FROM note_meta WHERE written_by = 'brain'") == 3
    assert one("SELECT count(*) FROM signals WHERE kind = 'conflict' AND status = 'open'") == 10
    assert store.check_instance(bench) == []
    assert sum(1 for op in all_ops() if op["op"] == "add_source") == 300
    assert [op["op"] for op in packets()["0006-work.json"]["ops"]].count("upsert_action") == 60


def test_generator_dates_shift_with_today(manifest) -> None:
    first = generate.generate(1, 1, TODAY)
    later_day = TODAY + datetime.timedelta(days=22)
    later = generate.generate(1, 1, later_day)
    assert sorted(first) == sorted(later)
    for path in first:
        if path == "MANIFEST.json":
            continue
        assert shift_dates(first[path].decode("ascii"), 22) == later[path].decode("ascii"), path
    moved = json.loads(later["MANIFEST.json"])
    assert (moved["today"], moved["now"], moved["applied_at"]) == (
        "2026-11-03", "2026-11-03T09:00:00Z", "2026-10-31T08:00:00Z")
    for path, data in first.items():
        assert WEEKDAY.search(data.decode("ascii")) is None, path


def test_generator_other_seeds_build_and_answer(tmp_path) -> None:
    for seed in (2, 3):
        dataset = write_dataset(generate.generate(seed, 1, datetime.date(2027, 2, 3)), tmp_path / f"seed{seed}")
        parent = tmp_path / f"build{seed}"
        parent.mkdir()
        instance = harness.build_instance(dataset, parent)
        now = harness.load_manifest(dataset)["now"]
        items = json.loads((dataset / "questions.json").read_text(encoding="utf-8"))["questions"]
        failed = {
            item["id"]: harness.check_expect(harness.run_path(instance, item, now), item["expect"]) for item in items
        }
        assert {k: v for k, v in failed.items() if v} == {}, seed
        assert len(items) == 24


def test_generator_command_line(tmp_path, capsys) -> None:
    date_error = "bench: --today must be a date written YYYY-MM-DD\n"
    empty = tmp_path / "empty"
    for argv in ([], ["--today", "2026-13-01"], ["--today", "12 October"], ["--scale", "4"]):
        code, out, err = run_generate([*argv, "--out", str(empty)])
        assert (code, out, err) == (2, "", date_error), argv
    # Scale 4 builds (the history about 4 times larger; tools/scale_check.py measures A8 on it).
    code, out, err = run_generate(["--today", "2026-10-12", "--scale", "4", "--out", str(empty)])
    assert code == 0 and out.startswith("bench: wrote 2") and err == ""
    assert json.loads((empty / "MANIFEST.json").read_text())["scale"] == 4
    assert "0003-mail-and-chat-2.json" in json.loads((empty / "MANIFEST.json").read_text())["packets"]
    full = tmp_path / "full"
    full.mkdir()
    (full / "x").write_text("x", encoding="utf-8")
    code, out, err = run_generate(["--today", "2026-10-12", "--out", str(full)])
    assert (code, out, err) == (2, "", f"bench: {full} is not empty\n")
    code, out, err = run_generate(["--today", "2026-10-12", "--out", str(full / "x")])
    assert (code, out, err) == (2, "", f"bench: {full / 'x'} is not empty\n")
    # an empty directory, and a path that does not exist yet, both work
    for name, make in (("made", True), ("fresh", False)):
        target = tmp_path / name
        if make:
            target.mkdir()
        code, out, err = run_generate(["--today", "2026-10-12", "--out", str(target)])
        files = {p.relative_to(target).as_posix(): p.read_bytes() for p in sorted(target.rglob("*")) if p.is_file()}
        assert (code, err) == (0, "")
        assert out == f"bench: wrote 24 files, {sum(len(d) for d in files.values())} bytes, to {target}\n"
        assert files == committed()
    # the command line, as a subprocess
    sub = tmp_path / "sub"
    done = subprocess.run(
        [sys.executable, str(REPO / "bench" / "generate.py"), "--today", "2026-10-12", "--out", str(sub)],
        capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert {p.relative_to(sub).as_posix(): p.read_bytes() for p in sub.rglob("*") if p.is_file()} == committed()
    # a date at the limit of the calendar is a bench message, not a traceback
    for edge in ("0001-01-01", "9999-12-31"):
        code, out, err = run_generate(["--today", edge, "--out", str(tmp_path / "edge")])
        assert (code, out, err) == (2, "", "bench: --today is too close to the limit of the calendar\n"), edge
    assert not (tmp_path / "edge").exists()
    # --seed takes only digits
    code, _out, _err = run_generate(["--today", "2026-10-12", "--seed", "7", "--out", str(tmp_path / "seven")])
    assert code == 0
    for bad in ("-1", "+1", "1_0", "x"):
        code, out, err = run_generate(["--today", "2026-10-12", "--seed", bad, "--out", str(tmp_path / "bad")])
        assert (code, out) == (2, "") and err, bad
        assert not (tmp_path / "bad").exists()
    # standard library only, no entropy and no clock
    source = (REPO / "bench" / "generate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names)
    assert not imported & {"brain", "random", "secrets", "uuid", "time"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"urandom", "now", "today", "utcnow"}, node.lineno
    assert not any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "hash"
        for node in ast.walk(tree)
    )


def test_bench_files_hold_only_placeholders() -> None:
    ids = {f"per-{first.lower()}-example" for first in generate.FIRST_NAMES}
    assert len(ids) == 40
    for path, data in committed().items():
        text = data.decode("ascii")
        assert "@" not in text, path
        assert "/Users/" not in text, path
        for host in HOST.findall(text.lower()):
            assert host.endswith(("example.com", "example.org")), (path, host)
        assert set(re.findall(r"per-[a-z0-9-]+", text)) <= ids, path
    names = [op["row"]["display_name"] for op in packets()["0001-people.json"]["ops"] if op["op"] == "upsert_person"]
    assert len(names) == 39 and all(name.endswith(" Example") for name in names)
    assert 'display_name = "Alex Example"' in (DATASET / "exampleco.toml").read_text(encoding="utf-8")
    for path in sorted((REPO / "bench").glob("*.py")):
        assert "/Users/" not in path.read_text(encoding="utf-8"), path.name


def test_questions_are_well_formed(questions) -> None:
    assert [q["id"] for q in questions] == [f"q{n:02d}" for n in range(1, 25)]
    groups = ["lookups", "waiting-on", "due-dates", "product-truth", "conflicts", "attribution"]
    assert [q["group"] for q in questions] == [g for g in groups for _ in range(4)]
    fields = ["id", "group", "shape", "kind", "question", "minimal_path", "expect", "answer_must", "answer_must_not"]
    kinds = {q["id"]: q["kind"] for q in questions if q["kind"] != "answer"}
    assert kinds == {"q03": "abstain", "q15": "abstain", "q04": "note_only"}
    key = re.compile(r"^[0-9]+:(\$error|[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*)$")
    for q in questions:
        assert list(q) == fields, q["id"]
        assert q["question"] and q["shape"]
        assert q["minimal_path"] and all(set(c) == {"tool", "arguments"} for c in q["minimal_path"])
        for c in q["minimal_path"]:
            assert c["tool"] in reads._TOOLS, q["id"]
            assert reads.check_schema(reads._TOOLS[c["tool"]]["inputSchema"], c["arguments"]) == [], q["id"]
        assert q["expect"]
        for expect_key in q["expect"]:
            assert key.fullmatch(expect_key), (q["id"], expect_key)
            assert int(expect_key.split(":")[0]) < len(q["minimal_path"]), (q["id"], expect_key)
        assert q["answer_must"], q["id"]
        for item in q["answer_must"]:
            assert (isinstance(item, str) and item) or (
                isinstance(item, list) and item and all(isinstance(a, str) and a for a in item)), q["id"]
        assert isinstance(q["answer_must_not"], list) and all(isinstance(s, str) and s for s in q["answer_must_not"])
    by_id = {q["id"]: q for q in questions}
    assert by_id["q05"]["expect"]["0:rows.0.id"] != by_id["q09"]["expect"]["0:rows.0.id"]
    assert by_id["q02"]["expect"]["0:rows.0.id"] != by_id["q12"]["expect"]["0:rows.0.id"]


def test_questions_are_answerable_from_both_arms(bench, manifest, questions) -> None:
    markdown = {
        path: data.decode("ascii") for path, data in committed().items() if path.startswith("markdown/")
    }
    everything = "\n".join(markdown.values())
    assert 100_000 <= sum(len(text.encode()) for text in markdown.values()) <= 130_000
    for q in questions:
        results = harness.run_path(bench, q, manifest["now"])
        seen = "\n".join(r.text for r in results)
        for item in q["answer_must"]:
            if q["kind"] in ("answer", "note_only"):
                assert met(item, seen), (q["id"], item, "not in the path's results")
            digits_only = all(grading.is_numeric(alt) for alt in alternatives(item))
            if q["kind"] == "answer" and not digits_only:
                assert met(item, everything), (q["id"], item, "not in the markdown")
        for wrong in q["answer_must_not"]:
            assert not met(wrong, seen), (q["id"], wrong, "in the brain's results")
        # The first wrong string is the value as the sources write it, so the markdown holds it. The rest are
        # other spellings of it (256,000, 49.50), or the other titles of a set question, which the markdown holds.
        if q["answer_must_not"]:
            assert met(q["answer_must_not"][0], everything), (q["id"], "the trap is not in the markdown")
        if q["id"] in ("q06", "q07"):
            assert all(met(wrong, everything) for wrong in q["answer_must_not"]), q["id"]
        # no string of one item matches inside another item or inside a wrong string
        items = [alternatives(item) for item in q["answer_must"]]
        for i, alts in enumerate(items):
            for a in alts:
                for j, others in enumerate(items):
                    assert i == j or not any(met(a, b) for b in others), (q["id"], a)
                assert not any(met(a, wrong) for wrong in q["answer_must_not"]), (q["id"], a)
        for wrong in q["answer_must_not"]:
            assert not any(met(wrong, b) for alts in items for b in alts), (q["id"], wrong)
    # the notes and traps are in the markdown, unlabelled
    notes = markdown["markdown/notes.md"]
    rows = [op["row"] for op in packets()["0006-work.json"]["ops"]]
    bodies = [row["body"] for row in rows if row.get("kind") == "note"]
    assert len(bodies) == 10 and all(body in notes for body in bodies)
    # The project files hold the checklist and the project actions; a workstream action sits in actions.md.
    projects = "\n".join(
        text for path, text in markdown.items()
        if path.startswith("markdown/projects/") or path == "markdown/actions.md")
    next_steps = [row["next_step"] for row in rows if "next_step" in row]
    details = [row["detail"] for row in rows if "detail" in row]
    assert len(next_steps) == 5 and len(details) == 5
    assert all(text in projects for text in next_steps + details)
    assert all("unsourced" not in text.lower() for text in markdown.values())
    for path, data in committed().items():
        if path != "questions.json":
            assert not re.search(r"revenue|turnover|headcount", data.decode("ascii"), re.I), path


def test_budgets_file_is_complete_and_fingerprinted(budgets, questions) -> None:
    assert list(budgets) == ["format", "a1", "a2", "a3", "a5", "a7", "changelog"]
    assert budgets["format"] == 1
    assert set(budgets["a1"]) == {"exampleco", "demo"}
    for entry in budgets["a1"].values():
        assert set(entry) == {"budget", "ceiling"} and all(type(v) is int for v in entry.values())
        floor = math.ceil(entry["budget"] / 1000) * 1000
        if floor * 100 < entry["budget"] * 103:
            floor += 1000
        assert entry["ceiling"] == floor
        assert entry["ceiling"] * 100 >= entry["budget"] * 103
    assert set(budgets["a2"]) == {"router_max", "router_base_bytes", "router_notes_off_saving", "router_template_bytes",
                                  "brief_max", "brief_lines_max", "disputed_key_max", "prefix_item_bytes",
                                  "prefix_page_bytes"}
    assert "prefix_25_target" not in budgets["a2"]
    assert (budgets["a2"]["router_max"], budgets["a2"]["brief_max"], budgets["a2"]["brief_lines_max"],
            budgets["a2"]["disputed_key_max"]) == (2048, 4096, 40, 2048)
    assert all(type(v) is int and v > 0 for v in budgets["a2"].values())
    assert set(budgets["a3"]) == {"tools_list", "tools"} and list(budgets["a3"]["tools"]) == TOOL_ORDER
    assert all(type(v) is int for v in budgets["a3"]["tools"].values())
    assert list(budgets["a5"]) == [q["id"] for q in questions]
    for entry in budgets["a5"].values():
        assert set(entry) == {"calls", "bytes"} and all(type(v) is int and v > 0 for v in entry.values())
    assert budgets["a7"] == {"repeats": 20, "p95_ms": 50}
    log = budgets["changelog"]
    assert log and all(set(entry) == {"date", "phase", "note", "fingerprint"} for entry in log)
    assert [entry["date"] for entry in log] == sorted(entry["date"] for entry in log)
    assert len({entry["fingerprint"] for entry in log}) == len(log)
    assert log[-1]["fingerprint"] == harness.fingerprint(budgets)
    assert all(re.fullmatch(r"[0-9a-f]{64}", entry["fingerprint"]) for entry in log)


# -- 15 to 21: Tier A ------------------------------------------------------------------------------------


def test_a1_startup_bytes(bench, manifest, budgets) -> None:
    start = harness.startup(bench, manifest["now"])
    s = start["S"]
    assert s == start["router"] + start["initialize"] + start["tools_list"] + start["brief"]
    print(
        f"startup bytes: router {start['router']}, initialize {start['initialize']}, tools/list {start['tools_list']}, "
        f"brief {start['brief']}, total {s} (about {s // 4} tokens by bytes/4)"
    )
    entry = budgets["a1"]["exampleco"]
    assert s <= entry["ceiling"], f"startup {s} is above the ceiling {entry['ceiling']}"
    assert s * 10 <= entry["budget"] * 11, f"startup {s} is more than 10% over the budget {entry['budget']}"


def test_a2_router_and_brief_sizes(bench, manifest, budgets, tmp_path, clock) -> None:
    limits = budgets["a2"]
    start = harness.startup(bench, manifest["now"])
    assert start["router"] <= limits["router_max"]
    assert start["brief_text_bytes"] <= limits["brief_max"]
    assert start["brief_lines"] <= limits["brief_lines_max"]
    shown = harness.figures(bench, [], manifest["now"], timing=False)["a2"]
    assert shown["disputed_key_max"] <= limits["disputed_key_max"]
    for name in ("prefix_item_bytes", "prefix_page_bytes"):
        assert shown[name] * 10 <= limits[name] * 11, f"{name} {shown[name]} is more than 10% over {limits[name]}"
    # the worst-case brief: 1b's fixture with every added line at its cap
    worst = worst_brief_instance(tmp_path, clock)
    lines = assert_worst_brief(worst)
    text = p7._brief(worst)
    size = len(text.encode())
    print(f"worst-case brief: {len(lines)} lines, {size} bytes")
    assert len(lines) <= limits["brief_lines_max"] and size <= limits["brief_max"]


def test_a3_tool_schema_bytes(bench, manifest, budgets) -> None:
    start = harness.startup(bench, manifest["now"])
    pins = budgets["a3"]
    assert [tool["name"] for tool in reads.TOOLS] == TOOL_ORDER
    assert start["tools_list"] == pins["tools_list"]
    assert start["tools"] == pins["tools"]
    fact_get = next(tool for tool in reads.TOOLS if tool["name"] == "fact_get")
    claims = fact_get["outputSchema"]["properties"]["facts"]["items"]["properties"]["claims"]
    assert claims == {"type": "object"}


def test_a4_exact_answers(bench, manifest, questions) -> None:
    failed = {}
    for item in questions:
        problems = harness.check_expect(harness.run_path(bench, item, manifest["now"]), item["expect"])
        if problems:
            failed[item["id"]] = problems
    assert not failed, f"{24 - len(failed)} of 24 meet expect; failed: {failed}"


def test_a5_calls_and_bytes(bench, manifest, questions, budgets) -> None:
    for item in questions:
        results = harness.run_path(bench, item, manifest["now"])
        calls, size = len(results), sum(harness.result_bytes(r) for r in results)
        pin = budgets["a5"][item["id"]]
        assert calls <= pin["calls"], (item["id"], calls, pin)
        assert size * 10 <= pin["bytes"] * 11, (item["id"], size, pin)
        assert calls <= 3, item["id"]


def snapshot(instance: Path, questions: list[dict], now: str) -> dict:
    out = {}
    for item in questions:
        out[item["id"]] = [
            (r.text, r.is_error, r.code, harness.result_bytes(r)) for r in harness.run_path(instance, item, now)
        ]
    brief = reads.run_tool(instance, "brief", {}, now=now)
    out["brief"] = [(brief.text, brief.is_error, brief.code, harness.result_bytes(brief))]
    out["startup"] = harness.startup(instance, now)
    return out


def test_a6_repeatability(bench, manifest, questions, tmp_path) -> None:
    now = manifest["now"]
    first, second = snapshot(bench, questions, now), snapshot(bench, questions, now)
    saved = store.utc_now
    store.utc_now = lambda: "2026-10-13T00:00:00Z"
    try:
        third = snapshot(bench, questions, now)
        other = harness.build_instance(DATASET, tmp_path)
        fourth = snapshot(other, questions, now)
    finally:
        store.utc_now = saved
    assert first == second == third == fourth
    assert store.check_instance(bench) == [] and store.check_instance(other) == []
    for results in first.values():
        if isinstance(results, list):
            assert not any("brain-config-" in row[0] for row in results)
    # no set order or hash seed reaches the figures
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    prints = []
    for hash_seed in ("1", "2"):
        done = subprocess.run(
            [sys.executable, "-m", "bench.harness", "measure", "--no-timing"], cwd=REPO,
            env={**env, "PYTHONHASHSEED": hash_seed}, capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stderr
        prints.append(done.stdout)
    assert prints[0] == prints[1]


def test_a7_read_speed(bench, manifest, questions, budgets) -> None:
    limit = budgets["a7"]["p95_ms"]
    paths = [("brief", {"minimal_path": [{"tool": "brief", "arguments": {}}]})] + [(q["id"], q) for q in questions]
    slowest = ("", 0.0)
    for name, item in paths:
        value = harness.p95_ms(bench, item, manifest["now"], repeats=budgets["a7"]["repeats"])
        if value > slowest[1]:
            slowest = (name, value)
        assert value <= limit, f"{name} p95 {value:.1f} ms is over {limit} ms"
    print(f"largest p95: {slowest[0]} {slowest[1]:.1f} ms (limit {limit} ms)")


# -- 22 to 27: A9, the product's promises about disputed facts and notes ---------------------------------------


def disputed_keys() -> list[str]:
    return [f"{entity}.{attr}" for entity, attr in generate.DISPUTED]


def test_a9_disputed_facts_show_both_values_and_the_brief_names_them(bench, manifest) -> None:
    now = manifest["now"]
    raws: dict[str, list[str]] = {}
    for row in claim_rows():
        raws.setdefault(row["key"], []).append(row["value_raw"])
    keys = disputed_keys()
    assert len(keys) == 10
    for key in keys:
        shown = call(bench, "fact_get", now, key=key)
        assert not shown.is_error, shown.text
        fact = shown.structured["facts"][0]
        assert fact["status"] == "disputed" and "value" not in fact, key
        values = fact["claims"]["values"]
        assert len(values) == 2, key
        assert sorted(v["raw"] for v in values) == sorted(raws[key]), key
        assert len({v["source"]["id"] for v in values}) == 2 and all(v["quote"] for v in values), key
    text = call(bench, "brief", now).text
    named = re.findall(r"^Disputed (\S+) \(now\):", text, re.M)
    assert len(named) == 3
    assert "7 more disputed. records_query on signal with kind conflict lists them." in text.splitlines()
    signals = call(bench, "records_query", now, entity="signal", filters={"kind": "conflict", "status": "open"},
                   fields=["subject_id"], order_by="subject_id", limit=25)
    subjects = [row["subject_id"] for row in signals.structured["rows"]]
    assert sorted(subjects) == sorted(keys)
    assert set(named) <= set(subjects)


def test_a9_no_read_surface_returns_a_bare_value_for_a_disputed_key(bench, manifest) -> None:
    now = manifest["now"]
    for key in disputed_keys():
        entity = key.rpartition(".")[0]
        assert "value" not in call(bench, "fact_get", now, key=key).structured["facts"][0], key
        prefix = call(bench, "fact_get", now, key=f"{entity}.*").structured["facts"]
        assert "value" not in next(item for item in prefix if item["key"] == key), key
        assert call(bench, "get", now, entity="fact", id=key).structured["row"]["value"] == "disputed", key
        rows = call(bench, "records_query", now, entity="fact", filters={"key": key}).structured["rows"]
        assert [row["value"] for row in rows] == ["disputed"], key
        found = call(bench, "search", now, query=key.replace(".", " "), entities=["fact"]).structured["hits"]
        hit = next(item for item in found if item["id"] == key)
        assert hit["snippet"] == "disputed" and hit["disputed"] is True, key
        for extra in ({"claims": "all"}, {"scope": "now"}):
            assert "value" not in call(bench, "fact_get", now, key=key, **extra).structured["facts"][0], (key, extra)
        history = call(bench, "get", now, entity="fact", id=key, include_history=True).structured
        assert history["row"]["value"] == "disputed"
        assert history["history"]["items"][0]["value"] == "disputed", key


def test_a9_a_held_claim_that_would_conflict_names_its_key(bench, manifest, tmp_path, clock) -> None:
    copy = tmp_path / "exampleco"
    shutil.copytree(bench, copy)
    clock.at("2026-10-11T09:00:00Z")
    from brain.applier import Applier
    from tests.conftest import make_packet

    source = {"op": "add_source", "row": {
        "id": "src-slack-a9", "kind": "slack", "title": "Slack thread about prices", "source_date": "2026-10-11",
        "author_person_id": "per-kai-example",
        "content": "Kai Example: The Example Vault price is 999 AUD a seat for teams."}}
    writer = Applier(copy)
    try:
        applied = writer.apply(json.dumps(make_packet("loader", source)).encode())
    finally:
        writer.close()
    assert applied.receipt["status"] == "applied", applied.receipt
    candidate = {"candidate_version": 2, "kind": "claim_candidate", "source_id": "src-slack-a9", "locator": "para 1",
                 "quote": "The Example Vault price is 999 AUD a seat",
                 "fields": {"key": "Example Vault price", "value_raw": "999 AUD"}}
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = brain_main(["extract", "--instance", str(copy), "--as", "scan", str(path)])
    assert code == 4, (out.getvalue(), err.getvalue())
    assert err.getvalue() == "brain: op 0.0 (add_claim): would open a conflict on exvault.price (now)\n"
    lines = call(copy, "brief", manifest["now"]).text.splitlines()
    assert ("Held: 1 waiting for a person (1 would open a conflict: exvault.price). Oldest 1 day. "
            "Ask the person to review: brain held.") in lines


def test_a9_a_document_update_is_not_a_dispute(bench, manifest) -> None:
    now = manifest["now"]
    seconds = {row["key"]: row["value_raw"] for op in packets()["0008-updated-claims.json"]["ops"] for row in [op["row"]]}
    assert len(seconds) == 5
    open_keys = {
        row[0] for row in ro_rows(bench, "SELECT subject_id FROM signals WHERE kind = 'conflict' AND status = 'open'")
    }
    for key, second in seconds.items():
        fact = call(bench, "fact_get", now, key=key).structured["facts"][0]
        assert fact["status"] == "current", key
        assert fact["value"] == number(second), key
        rows = ro_rows(bench, "SELECT id, status, superseded_by FROM claims WHERE key = ? ORDER BY id", (key,))
        assert len(rows) == 2, key
        (first_id, first_status, first_by), (second_id, second_status, second_by) = rows
        assert (first_status, first_by) == ("superseded", second_id), key
        assert (second_status, second_by) == ("live", None), key
        assert key not in open_keys
    brief = call(bench, "brief", now).text
    assert ("Changed in the last 7 days: 5 facts (exbasic.retention_days, exbasic.seats, exscale.max_context, +2 more)."
            in brief.splitlines())


def test_a9_a_note_never_answers_a_fact_question(bench, manifest) -> None:
    now = manifest["now"]
    builder = generate.Builder(1, TODAY)
    builder.build_people()
    builder.build_facts()
    claims = {row["key"]: row["value_raw"] for row in claim_rows()}
    records = {op["row"]["id"]: op["row"] for op in packets()["0006-work.json"]["ops"] if op["op"] == "upsert_record"}
    assert len(generate.TRAP_KEYS) == 9
    for index, key in enumerate(generate.TRAP_KEYS, start=1):
        body = records[f"rec-note-{index:02d}"]["body"]
        wrong = builder.trap_raw[key]
        assert wrong in body and wrong != claims[key], key
        shown = call(bench, "fact_get", now, key=key)
        fact = shown.structured["facts"][0]
        sourced = claims[key]
        if key == "exscale.sso":
            assert (sourced, wrong, fact["value"]) == ("included", "not included", True)
        elif key.endswith(".region"):
            assert fact["value"] == sourced.lower()
        else:
            assert fact["value"] == number(sourced), key
        assert not met(wrong, shown.text), (key, wrong)
    missing = call(bench, "fact_get", now, key="EU launch codename")
    assert missing.is_error and missing.code == "unknown_key"
    assert call(bench, "search", now, query="codename", entities=["fact"]).structured["hits"] == []
    got = call(bench, "get", now, entity="record", id="rec-note-10").structured
    assert got["notes"]["label"] == NOTE_LABEL
    assert got["notes"]["fields"]["body"]["by"] == "loader"


def test_a9_an_unknown_key_says_not_found(bench, manifest) -> None:
    now = manifest["now"]
    for key in ("exbasic.headcount", "Example Co annual revenue"):
        shown = call(bench, "fact_get", now, key=key)
        error = json.loads(shown.text)["error"]
        assert shown.is_error and shown.code == "unknown_key", key
        assert error["code"] == "unknown_key" and error["message"].startswith("unknown fact key "), key
    unset = call(bench, "fact_get", now, key="exscale.pentest_date")
    assert unset.is_error and unset.code == "no_value"
    assert json.loads(unset.text)["error"]["message"] == "fact key exscale.pentest_date is registered but has no value"


# -- 28: A10, CI -------------------------------------------------------------------------------------------


def test_a10_ci_runs_the_whole_suite_on_both_pythons() -> None:
    workflow = (REPO / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    lines = [line.strip() for line in workflow.splitlines()]
    assert 'python-version: ["3.11", "3.14"]' in lines
    assert "- run: python -m pytest -q" in lines
    for line in lines:
        assert "--deselect" not in line and "--ignore" not in line, line
        if "pytest" in line:
            tail = line.split("pytest", 1)[1]
            assert re.search(r"\s-[km](\s|$)", tail) is None, line
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'testpaths = ["tests"]' in pyproject
    assert (REPO / "tests" / "test_bench_gate.py").is_file()


# -- the amended grading rules and question definitions --


def correct_answer(q: dict) -> str:
    return ". ".join(alternatives(item)[0] for item in q["answer_must"]) + "."


def test_grading_matches_the_generators_normal_form() -> None:
    for text in ("49.5 AUD", "49.50 AUD", "32,000 tokens", "32,000,000", "Version 1.5", "a.b, c", "2026-10-12", "$5m",
                 "exbasic.price", "support_hours", "x\u00a0y", "Example Basic's SSO", "Example Basic\u2019s SSO",
                 "it's the 5's and the kids' toys"):
        assert grading.norm(text) == generate.norm(text), text
    assert grading.norm("32,000") == grading.norm("32000") == " 32000 "
    assert grading.norm("49.5") != grading.norm("49 5")
    assert grading.norm("Example Basic's SSO") == grading.norm("Example Basic SSO")


def test_grading_numbers_are_whole_tokens() -> None:
    assert grading.mentions("29", "It costs 29 AUD.") and grading.mentions("49.5 AUD", "It costs $49.5 AUD.")
    for number, text in (("29", "costs 29.99 AUD"), ("5", "includes 5.5 seats"), ("32000", "32,000,000 tokens"),
                         ("49.5 AUD", "costs 49.50 AUD"), ("5", "version 1.5"), ("50 seats", "250 seats")):
        assert not grading.mentions(number, text), (number, text)
    assert grading.mentions("32,000", "a 32000 token window") and grading.mentions("128000", "128,000 tokens")


def test_grading_bare_numbers_ignore_dates_and_ids() -> None:
    for number, text in (("10", "Today is 2026-10-12."), ("14", "The nearest is on 2026-10-14."),
                         ("14", "Action act-14 and chk-014 are due."), ("5", "see src-note-05 at 2026-09-05T08:00:00Z"),
                         ("9", "rec-note-9")):
        assert not grading.mentions(number, text), (number, text)
    assert grading.mentions("14", "There are 14 of them, due by 2026-10-19.")
    assert not grading.is_numeric("2026-10-14") and grading.is_numeric("14") and grading.is_numeric("49.5")


def test_grading_accepts_a_correct_answer_for_every_question(questions) -> None:
    for q in questions:
        graded = grading.grade(q, correct_answer(q))
        assert graded["strict"] and graded["lenient"] and not graded["flagged_trap"], (q["id"], graded)


def test_grading_rejects_answers_that_only_look_right(questions) -> None:
    by = {q["id"]: q for q in questions}

    def bare(qid: str, index: int = 0) -> str:
        return next(a for a in alternatives(by[qid]["answer_must"][index]) if grading.is_numeric(a))

    n13 = bare("q13")
    weak = {
        # A date, a version and an id are not a count; q19 needs the ten facts, not one
        "q19": ["I could not run the query. Today is 2026-10-12. Example Basic may be one of them.", "10 facts."],
        "q10": [f"Nothing is due soon; the nearest is on 2026-10-{bare('q10').zfill(2)}.", f"Action act-{bare('q10')} is due."],
        "q11": [f"Overdue since 2026-10-{bare('q11').zfill(2)}."],
        "q13": [f"Example Basic includes 77 seats. Version 1.{n13} is out.", f"Example Basic includes {n13}.5 seats."],
        # A different decimal or thousands tail is a different number
        "q01": [f"Example Pro costs {bare('q01')}.99 AUD a seat."],
        "q14": [f"The window was {bare('q14', 0)}0000 tokens.", "The window was 32,000,000 tokens."],
        "q20": [f"Each node has {bare('q20', 0)}.5 GB or {bare('q20', 1)}.2 GB."],
        # A confabulated value isn't an abstention
        "q03": ["Revenue? That does not hold in my head, but it is $5m."],
        "q15": ["The test is not recorded, but I think it is 2026-11-02."],
        # The codename with no word that it is only a note
        "q04": ["The codename is Project Heron."],
        # Naming every action isn't an answer
        "q06": [correct_answer(by["q06"]) + " Also " + ", ".join(by["q06"]["answer_must_not"]) + "."],
        "q07": [correct_answer(by["q07"]) + " Also " + ", ".join(by["q07"]["answer_must_not"]) + "."],
    }
    for qid, answers in weak.items():
        for answer in answers:
            graded = grading.grade(by[qid], answer)
            assert not graded["strict"] and not graded["lenient"], (qid, answer, graded)
    # q19 holds the ten keys and the count as separate items
    assert len(by["q19"]["answer_must"]) == 11 and by["q19"]["answer_must"][0] == "10"
    assert len(by["q06"]["answer_must_not"]) + len(by["q06"]["answer_must"]) == 22
    assert len(by["q07"]["answer_must_not"]) + len(by["q07"]["answer_must"]) == 25
    # and the honest wordings pass
    assert grading.grade(by["q03"], "The brain says the key is unknown, so I can't say.")["strict"]
    assert grading.grade(by["q15"], "I couldn't find a value for that.")["strict"]
    assert grading.grade(by["q04"], "Project Heron, per the scratch note.")["strict"]
    assert grading.grade(by["q19"], correct_answer(by["q19"]))["strict"]


def test_grading_lenient_score_forgives_a_stale_mention(questions) -> None:
    by = {q["id"]: q for q in questions}
    q01, q13, q22 = by["q01"], by["q13"], by["q22"]
    wrong = q01["answer_must_not"][0]
    careful = f"{correct_answer(q01)} A stale reminder note says {wrong}, which is wrong."
    graded = grading.grade(q01, careful)
    assert (graded["strict"], graded["lenient"], graded["flagged_trap"]) == (False, True, True)
    asserted = f"{correct_answer(q01)} It also costs {wrong} a seat."
    graded = grading.grade(q01, asserted)
    assert (graded["strict"], graded["lenient"], graded["flagged_trap"]) == (False, False, True)
    first = q13["answer_must_not"][0]
    graded = grading.grade(q13, f"{correct_answer(q13)} (The first edition said {first}.)")
    assert (graded["strict"], graded["lenient"]) == (False, True)
    graded = grading.grade(q22, f"{correct_answer(q22)} Another note wrongly says {q22['answer_must_not'][0]}.")
    assert (graded["strict"], graded["lenient"]) == (False, True)
    # a spelling of the trap that differs only by a trailing zero is still the trap
    spelled = q01["answer_must_not"][0]
    spelled = re.sub(r"(\.[0-9])", r"\g<1>0", spelled) if "." in spelled else f"{spelled.split(' ')[0]}.50 AUD"
    assert any(grading.mentions(w, f"It costs {spelled} a seat.") for w in q01["answer_must_not"])


def test_grading_abstention_rules(questions) -> None:
    by = {q["id"]: q for q in questions}
    for qid in ("q03", "q15"):
        q = by[qid]
        for phrase in ("does not hold", "unknown", "not found", "couldn't find", "can't find", "not available",
                       "no data", "don't have"):
            assert grading.grade(q, f"The brain says: {phrase}.")["strict"], (qid, phrase)
        assert grading.grade(q, "It is $5m.")["invented"] == ["5"]
        assert not grading.grade(q, "That is unknown, though 2026-10-12 might be it.")["strict"]
        assert not grading.grade(q, "It is something.")["strict"]


def test_question_texts_state_their_definitions(manifest, questions) -> None:
    by = {q["id"]: q for q in questions}
    today = datetime.date.fromisoformat(manifest["today"])
    day = lambda k: (today + datetime.timedelta(days=k)).isoformat()  # noqa: E731
    bracket = "(every status except done and proposed)"
    assert by["q10"]["question"] == f"How many open actions {bracket} are due from {day(0)} to {day(7)} inclusive?"
    assert by["q11"]["question"] == f"How many open actions {bracket} have a due date before {day(0)}?"
    assert bracket in by["q06"]["question"] and "status waiting" in by["q08"]["question"]
    # what the plain words say, recomputed from the packets, is what `expect` says
    actions = [op["row"] for op in all_ops() if op["op"] == "upsert_action"]
    open_ = [a for a in actions if a["status"] not in ("done", "proposed")]
    dated = [a for a in open_ if "due_date" in a]
    assert by["q10"]["expect"]["0:total"] == len([a for a in dated if day(0) <= a["due_date"] <= day(7)])
    assert by["q11"]["expect"]["0:total"] == len([a for a in dated if a["due_date"] < day(0)])
    waiting = sorted((a["asked_on"], a["id"]) for a in actions if a["status"] == "waiting")
    assert by["q08"]["expect"]["0:rows.0.id"] == waiting[0][1]
    person = by["q06"]["minimal_path"][0]["arguments"]["filters"]["waiting_on_person_id"]
    assert by["q06"]["expect"]["0:total"] == len([a for a in open_ if a.get("waiting_on_person_id") == person])


def test_dataset_wording_has_no_plural_slips() -> None:
    for path, data in committed().items():
        assert not re.search(r"\b1 (hours|days|seats)\b", data.decode("ascii")), path


def test_grading_lenient_score_is_an_upper_bound(questions) -> None:
    """RR1: "not", "was" and "were" are not staleness markers, and a sentence that negates the right answer
    asserts the trap. The three counterexamples fail both scores; the careful answers keep their lenient pass."""
    q01 = next(q for q in questions if q["id"] == "q01")
    right, trap = "29 AUD", "49.5 AUD"
    assert right in alternatives(q01["answer_must"][0]) and trap in q01["answer_must_not"]
    for wrong in (f"Example Pro costs {trap} a seat, not {right}.",
                  f"Example Pro costs {right}, but it was really {trap} a seat.",
                  f"{trap} is the right price, {right} is wrong"):
        graded = grading.grade(q01, wrong)
        assert not graded["strict"] and not graded["lenient"] and graded["flagged_trap"], wrong
    for careful in (f"Example Pro costs {right} a seat. A stale reminder note says {trap}, which is wrong.",
                    f"Example Pro costs {right} a seat. The earlier note, superseded now, said {trap}."):
        graded = grading.grade(q01, careful)
        assert not graded["strict"] and graded["lenient"] and graded["flagged_trap"], careful
    assert grading.asserted(trap, f"It costs {trap} a seat, not {right}.", q01["answer_must"])
    assert not grading.asserted(trap, f"A stale note says {trap}.", q01["answer_must"])
    for word in ("not", "was", "were", "isn't"):
        assert grading._STALE.search(f"it {word} here") is None, word


def test_grading_q19_accepts_natural_prose(questions) -> None:
    """RR2: a possessive is dropped and "of" forms are alternatives, so a right answer in prose passes."""
    q19 = next(q for q in questions if q["id"] == "q19")
    aliases = dict(generate.ATTRIBUTE_ALIASES)
    parts = []
    for index, (entity, attr) in enumerate(generate.DISPUTED):
        product = dict(generate.PRODUCTS)[entity]
        if index % 3 == 0:
            parts.append(f"the {attr.replace('_', ' ')} of {product}")
        elif index % 3 == 1:
            parts.append(f"{product}'s {aliases[attr][0]}")
        else:
            parts.append(f"the {aliases[attr][-1]} of {product}")
    prose = "There are 10 disputed facts: " + ", ".join(parts) + "."
    assert len(parts) == 10 and "'s " in prose and " of " in prose
    graded = grading.grade(q19, prose)
    assert graded["strict"] and graded["lenient"], graded
    # the reviewer's own phrasings
    for phrase in ("the price of Example Basic", "Example Basic's SSO", "the region of Example Cloud",
                   "Example Edge's context window"):
        assert any(grading.mentions(alt, phrase) for item in q19["answer_must"][1:] for alt in alternatives(item)), phrase
    assert grading.mentions("Example Edge context window", "Example Edge's context window")
    assert grading.mentions("Example Basic sso", "Example Basic's SSO")
    # one fact dropped, or only a count and a date, still fail
    assert not grading.grade(q19, prose.replace(parts[0] + ", ", ""))["strict"]
    assert not grading.grade(q19, "There are 10 disputed facts as of 2026-10-12.")["strict"]


def test_grading_q04_needs_the_codename_not_just_a_note_word(questions) -> None:
    """RR3: a non-answer that holds a note phrase and a stray "Heron" fails."""
    q04 = next(q for q in questions if q["id"] == "q04")
    for wrong in ("The codename is unsourced gossip; I don't know it. Heron is a bird.",
                  "It is only a note and Heron is a bird.", "The codename is Project Heron."):
        graded = grading.grade(q04, wrong)
        assert not graded["strict"] and not graded["lenient"], wrong
    for right in ("Project Heron, per the scratch note.", "Only an unverified note says Project Heron."):
        assert grading.grade(q04, right)["strict"], right


def test_grading_q13_forgives_the_old_value_as_history(questions) -> None:
    """RR4: "up from 250" mentions the first edition's value as history, not as the answer."""
    q13 = next(q for q in questions if q["id"] == "q13")
    assert "250" in q13["answer_must_not"] and "5 seats" in alternatives(q13["answer_must"][0])
    for right in ("Example Basic now includes 5 seats, up from 250.", "It has 5 seats now, raised from 250 seats.",
                  "5 seats; it was changed from 250 seats in the second edition."):
        graded = grading.grade(q13, right)
        assert not graded["strict"] and graded["lenient"] and graded["flagged_trap"], right
    assert not grading.grade(q13, "Example Basic includes 250 seats.")["lenient"]
    assert not grading.grade(q13, "Example Basic includes 5 seats and 250 seats from now on.")["lenient"]


def test_a8_brain_reads_stay_flat_from_scale_1_to_scale_4() -> None:
    """Tier A, A8: the history grows about 4 times; startup bytes and every shortest-path answer may
    grow at most 25%. Measured: no growth at all, while the markdown grew 4.08 times."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    done = subprocess.run([sys.executable, str(root / "tools" / "scale_check.py"), "--no-timing"], capture_output=True,
                          text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    report = json.loads(done.stdout)
    assert report["a8_pass"] and report["growth"]["S"] <= 0.25 and report["growth"]["worst_answer"] <= 0.25
    assert 3.5 <= report["growth"]["markdown"] <= 4.5
