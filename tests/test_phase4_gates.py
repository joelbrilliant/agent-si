from __future__ import annotations

import ast
import builtins
import contextlib
import hashlib
import importlib
import io
import json
import random
import select
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain import collect
from brain import store
from brain.applier import Unavailable
from brain.cli import main
from brain.collectors import REGISTRY
from brain.collectors.base import RunContext
from brain.collectors.github import TYPE as GITHUB
from brain.collectors.messages import GMAIL
from brain.config import load_config
from tests.collector_fixtures import (
    SAMPLES,
    collect as collect_doc,
    collect_file,
    collector_instance,
    github_doc,
    gmail_doc,
    slack_doc,
    vanta_doc,
    write_fixture,
)
from tests.conftest import file_sha, make_packet, ro_rows, source_op, upsert_op

_COLLECTOR_FOR_TYPE = {
    "files": "notes",
    "github": "repo",
    "vanta": "compliance",
    "calendar": "work-cal",
    "gmail": "gmail-work",
    "slack": "slack-team",
    "capture": "work-docs",
}
_SUBJECT_POOL = ('"', "'", "\u201c", "\u201d", "\\", "\t", "\u00e9", "\u2028", "\U0001f600")


def gmail_threads(n: int) -> dict:
    return gmail_doc(
        threads=[
            {
                "id": f"t{i:05d}",
                "subject": f"Thread {i}",
                "messages": [
                    {"at": "2026-10-02T09:00:00Z", "direction": "outbound"},
                    {"at": "2026-10-04T09:00:00Z", "direction": "inbound"},
                ],
            }
            for i in range(n)
        ]
    )


def _bump_time(mark: int) -> str:
    from datetime import datetime, timedelta, timezone

    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=mark)
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _bump_ci(instance: Path) -> None:
    rows = ro_rows(instance, "SELECT version FROM facts WHERE key = 'eng.repo.ci_state'")
    base = None if not rows else rows[0][0]
    mark = 0 if base is None else base
    src = f"src-bump-{mark}-note"
    packet = make_packet(
        "tester",
        # The value is quoted from the source.
        source_op(src, "Synthetic note used only to move a fact version. CI state is failure."),
        upsert_op(
            "upsert_fact",
            {
                "key": "eng.repo.ci_state",
                "value": "failure",
                "basis": "observed",
                "as_of": "2026-10-01",
                # A fresh time each bump: since 0.7.0 an identical repeat writes nothing.
                "last_verified_at": _bump_time(mark),
                "quote": "CI state is failure",
                "value_raw": "failure",
            },
            src,
            base=base,
        ),
    )
    code, payload, _err = _apply(instance, packet)
    assert code == 0, payload


def _apply(instance: Path, packet: dict) -> tuple[int, dict, str]:
    path = instance.parent / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["apply", "--instance", str(instance), str(path)])
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def _phase3_split(groups, build) -> list[list]:
    batches: list[list] = []
    current: list = []

    def too_big(trial: list) -> bool:
        ops = build(trial, len(batches) + 1)
        if len(ops) > collect.BATCH_OPS:
            return True
        return len(store.canonical_json(ops).encode()) > collect.BATCH_BYTES

    for group in groups:
        trial = [*current, group]
        if current and too_big(trial):
            batches.append(current)
            current = [group]
        else:
            current = trial
    batches.append(current)
    return batches


def _marked_subjects(n: int) -> dict:
    doc = gmail_threads(n)
    drawn = random.Random(4)
    for thread in doc["threads"]:
        thread["subject"] = "".join(drawn.choice(_SUBJECT_POOL) for _ in range(12))
    return doc


def _planned(directory: Path, type_name: str, doc: dict):
    name = _COLLECTOR_FOR_TYPE[type_name]
    instance = collector_instance(directory, name)
    collector = load_config(instance / "brain.toml").collectors[name]
    ctype = REGISTRY[collector.type]
    ctx = RunContext(name, dict(collector.options), "UTC", "0" * 24, "2026-10-08T00:00:00Z")
    raw = store.canonical_json(doc).encode()
    known = collect._snapshot(instance / "brain.sqlite3", ctype, doc, ctx)
    plan = ctype.plan(doc, known, ctx)
    return plan, ctx, hashlib.sha256(raw).hexdigest(), ctype


def _assert_same_batches(groups, ctx, doc, input_sha256, ctype, attempt) -> None:
    new = collect._split(groups, ctx, doc, input_sha256, ctype, attempt)
    old = _phase3_split(
        groups,
        lambda trial, index: collect._materialise(
            trial, index, ctx, doc, input_sha256, ctype, attempt
        ),
    )
    assert [[id(group) for group in batch] for batch in new] == [
        [id(group) for group in batch] for batch in old
    ]
    for index, (left, right) in enumerate(zip(new, old, strict=True), start=1):
        assert collect._materialise(
            left, index, ctx, doc, input_sha256, ctype, attempt
        ) == collect._materialise(right, index, ctx, doc, input_sha256, ctype, attempt)


def test_f2_split_matches_the_phase3_split(tmp_path: Path, monkeypatch) -> None:
    covered: set[str] = set()
    for type_name, builder in SAMPLES.items():
        covered.add(type_name)
        doc = builder()
        for attempt in (1, 2, 12):
            for op_cap in (1, 2, 3, 5, 13):
                for byte_cap in (600, 2_000, 10_000, 250_000):
                    monkeypatch.setattr(collect, "BATCH_OPS", op_cap)
                    monkeypatch.setattr(collect, "BATCH_BYTES", byte_cap)
                    plan, ctx, digest, ctype = _planned(
                        tmp_path / f"{type_name}-{attempt}-{op_cap}-{byte_cap}",
                        type_name,
                        doc,
                    )
                    _assert_same_batches(plan.groups, ctx, doc, digest, ctype, attempt)
    assert covered == set(REGISTRY)
    doc = _marked_subjects(1200)
    for attempt in (1, 2):
        for op_cap in (7, 400):
            for byte_cap in (2_000, 50_000, 4 * 1024 * 1024):
                monkeypatch.setattr(collect, "BATCH_OPS", op_cap)
                monkeypatch.setattr(collect, "BATCH_BYTES", byte_cap)
                plan, ctx, digest, ctype = _planned(
                    tmp_path / f"gmail-{attempt}-{op_cap}-{byte_cap}",
                    "gmail",
                    doc,
                )
                _assert_same_batches(plan.groups, ctx, doc, digest, ctype, attempt)


def test_f2_split_work_is_linear(tmp_path: Path, monkeypatch) -> None:
    real_dumps = json.dumps

    def counting(*args, **kwargs):
        text = real_dumps(*args, **kwargs)
        counting.calls += 1
        counting.chars += len(text)
        return text

    for n in (500, 1_000, 2_000):
        doc = gmail_threads(n)
        plan, ctx, digest, ctype = _planned(tmp_path / f"linear-{n}", "gmail", doc)
        base = 0
        evidence_groups = 0
        ops = 0
        for group in plan.groups:
            ops += len(group.ops)
            for op in group.ops:
                base += len(store.canonical_json(op))
            if group.evidence is not None:
                evidence_groups += 1
                base += len(store.canonical_json(group.evidence))
        counting.calls = 0
        counting.chars = 0
        monkeypatch.setattr(json, "dumps", counting)
        try:
            batches = collect._split(plan.groups, ctx, doc, digest, ctype, 1)
        finally:
            monkeypatch.setattr(json, "dumps", real_dumps)
        assert counting.chars <= 3 * base + 4096 * len(batches)
        assert counting.calls >= ops + evidence_groups


def test_f2_5000_thread_split_is_fast(tmp_path: Path) -> None:
    doc = gmail_threads(5000)
    plan, ctx, digest, ctype = _planned(tmp_path, "gmail", doc)
    started = time.perf_counter()
    batches = collect._split(plan.groups, ctx, doc, digest, ctype, 1)
    elapsed = time.perf_counter() - started
    print(f"collect._split 5000 threads: {elapsed:.4f}s")
    assert len(batches) == 13
    assert elapsed < 5.0


def _run_main(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _hold_lock(instance: Path, name: str):
    with collect.collector_lock(instance, name):
        return


_BUSY_STDOUT = '{"collector":"gmail-work","status":"busy"}\n'
_BUSY_STDERR = "brain: collector gmail-work is already running; try again when it finishes\n"


def test_o1_second_collect_for_a_collector_exits_5(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    fixture = write_fixture(instance.parent / "fixtures", gmail_doc())
    before = file_sha(instance / "brain.sqlite3")
    with collect.collector_lock(instance, "gmail-work"):
        code, stdout, stderr = _run_main(
            ["collect", "--fixture", str(fixture), "--instance", str(instance), "gmail-work"]
        )
        assert code == 5
        assert stdout == _BUSY_STDOUT
        assert stderr == _BUSY_STDERR
        assert file_sha(instance / "brain.sqlite3") == before
    code, payload, err = collect_file(instance, "gmail-work", fixture)
    assert code == 0, (payload, err)


def test_o1_concurrent_collect_cannot_burn_an_input(tmp_path: Path, monkeypatch) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    fixture = write_fixture(instance.parent / "fixtures", gmail_doc())
    original = GMAIL.known
    seen = {"calls": 0, "raised": None, "before": None, "after": None}

    def wrapper(con, doc, ctx):
        if seen["calls"] == 0:
            seen["calls"] += 1
            if con.in_transaction:
                con.execute("COMMIT")
            seen["before"] = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
            try:
                collect.run(instance, "gmail-work", fixture)
            except Unavailable as exc:
                seen["raised"] = exc
            seen["after"] = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
        return original(con, doc, ctx)

    monkeypatch.setitem(REGISTRY, "gmail", replace(GMAIL, known=wrapper))
    code, payload, err = collect_file(instance, "gmail-work", fixture)
    assert isinstance(seen["raised"], Unavailable)
    assert str(seen["raised"]) == (
        "collector gmail-work is already running; try again when it finishes"
    )
    assert seen["before"] == seen["after"]
    assert code == 0, (payload, err)
    assert ro_rows(instance, "SELECT status FROM collector_runs") == [("ok",)]
    assert ro_rows(
        instance,
        "SELECT count(*) FROM write_log WHERE agent = 'gmailwork' AND status = 'rejected'",
    ) == [(0,)]
    code, again, err = collect_file(instance, "gmail-work", fixture)
    assert code == 0, (again, err)
    assert again["duplicate"] is True


def test_o1_lock_is_released_on_every_exit(tmp_path: Path, monkeypatch) -> None:
    ok = collector_instance(tmp_path / "ok", "gmail-work")
    refused = collector_instance(tmp_path / "refused", "gmail-work")
    conflict = collector_instance(tmp_path / "conflict", "repo")
    paused = collector_instance(tmp_path / "paused", "gmail-work")
    interrupted = collector_instance(tmp_path / "interrupt", "gmail-work")
    gmail_known = GMAIL.known
    github_known = GITHUB.known

    code, payload, err = collect_doc(ok, "gmail-work", gmail_doc())
    assert code == 0, (payload, err)
    _hold_lock(ok, "gmail-work")

    code, payload, err = collect_doc(refused, "gmail-work", gmail_doc(account="other"))
    assert code == 1, (payload, err)
    _hold_lock(refused, "gmail-work")

    def conflicting(con, doc, ctx):
        snapshot = github_known(con, doc, ctx)
        if con.in_transaction:
            con.execute("COMMIT")
        _bump_ci(conflict)
        return snapshot

    monkeypatch.setitem(REGISTRY, "github", replace(GITHUB, known=conflicting))
    code, payload, err = collect_doc(conflict, "repo", github_doc())
    assert code == 3, (payload, err)
    _hold_lock(conflict, "repo")

    def pausing(con, doc, ctx):
        (paused / "PAUSED").write_text("paused\n", encoding="utf-8")
        return gmail_known(con, doc, ctx)

    monkeypatch.setitem(REGISTRY, "gmail", replace(GMAIL, known=pausing))
    code, payload, err = collect_doc(paused, "gmail-work", gmail_doc())
    assert code == 5, (payload, err)
    _hold_lock(paused, "gmail-work")

    def interrupting(con, doc, ctx):
        raise KeyboardInterrupt

    monkeypatch.setitem(REGISTRY, "gmail", replace(GMAIL, known=interrupting))
    fixture = write_fixture(interrupted.parent / "fixtures", gmail_doc())
    with pytest.raises(KeyboardInterrupt):
        collect.run(interrupted, "gmail-work", fixture)
    _hold_lock(interrupted, "gmail-work")


def test_o1_lock_holds_across_processes(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    fixture = write_fixture(instance.parent / "fixtures", gmail_doc())
    lock_path = instance / ".collect-gmail-work.lock"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, sys\n"
            "handle = open(sys.argv[1], 'a+b')\n"
            "fcntl.flock(handle.fileno(), fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "sys.stdin.read()\n",
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        ready, _, _ = select.select([child.stdout], [], [], 30)
        assert ready, "child did not take the lock within 30s"
        assert child.stdout.readline() == "locked\n"
        code, stdout, stderr = _run_main(
            ["collect", "--fixture", str(fixture), "--instance", str(instance), "gmail-work"]
        )
        assert code == 5
        assert stdout == _BUSY_STDOUT
        assert stderr == _BUSY_STDERR
    finally:
        if child.stdin is not None:
            child.stdin.close()
        assert child.wait(timeout=30) == 0
    code, payload, err = collect_file(instance, "gmail-work", fixture)
    assert code == 0, (payload, err)


def test_o1_other_collectors_and_writers_do_not_wait(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work", "slack-team")
    with collect.collector_lock(instance, "gmail-work"):
        code, payload, err = collect_doc(instance, "slack-team", slack_doc())
        assert code == 0, (payload, err)
        code, payload, err = _apply(
            instance,
            make_packet(
                "tester",
                source_op("src-while-locked", "A note applied while another collector is locked."),
                change_id="tester-while-locked",
            ),
        )
        assert code == 0, (payload, err)
        assert payload["status"] == "applied"
        code, _stdout, stderr = _run_main(["read", "brief", "--instance", str(instance)])
        assert code == 0, stderr


def test_o1_lock_file_is_ignored_and_harmless(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    assert ".collect-*.lock" in (root / ".gitignore").read_text().splitlines()
    instance = collector_instance(tmp_path, "gmail-work")
    code, payload, err = collect_doc(instance, "gmail-work", gmail_doc())
    assert code == 0, (payload, err)
    lock = instance / ".collect-gmail-work.lock"
    assert lock.is_file()
    assert lock.read_bytes() == b""
    assert store.check_instance(instance) == []
    code, _stdout, stderr = _run_main(["check", "--instance", str(instance)])
    assert code == 0, stderr
    fixture = write_fixture(tmp_path / "nope-fixture", gmail_doc())
    code, _stdout, stderr = _run_main(
        ["collect", "--fixture", str(fixture), "--instance", str(instance), "nope"]
    )
    assert code == 2
    assert stderr == "brain: no collector nope\n"
    assert not (instance / ".collect-nope.lock").exists()


def test_o3_attempt_lookup_searches_the_index(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "gmail-work")
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        rows = con.execute(
            "EXPLAIN QUERY PLAN " + collect._ATTEMPT_SQL, ("src-x-", "src-x-")
        ).fetchall()
    finally:
        con.close()
    details = [row[3] for row in rows]
    assert any("SEARCH sources USING COVERING INDEX" in detail for detail in details)
    assert not any("SCAN" in detail for detail in details)
    mem = sqlite3.connect(":memory:")
    mem.execute("CREATE TABLE sources (id TEXT PRIMARY KEY) STRICT")
    for source_id in (
        "src-c-j-a7-1",
        "src-c-k-1",
        "src-c-k-a2-1",
        "src-c-k-a3-2",
        "src-c-k-zz",
        "src-c-kx-a9-1",
        "src-c-l-a8-1",
    ):
        mem.execute("INSERT INTO sources (id) VALUES (?)", (source_id,))
    assert collect._used_attempts(SimpleNamespace(con=mem), "src-c-k-") == 3


def _facts_reference(con, keys, agent) -> dict:
    marks = ",".join("?" for _ in keys)
    # Known_facts also says whether the collector's own live claim is checked.
    rows = con.execute(
        "SELECT key, value_type, value_text, value_int, value_real, value_date, "
        "as_of, last_verified_at, version, status, "
        "EXISTS (SELECT 1 FROM claims AS c WHERE c.key = facts.key AND c.proposed_by = ? "
        "AND c.status = 'live' AND c.checked = 1) FROM facts WHERE key IN (" + marks + ")",
        [agent, *keys],
    )
    facts = {}
    for row in rows:
        facts[row[0]] = {
            "value_type": row[1],
            "value_text": row[2],
            "value_int": row[3],
            "value_real": row[4],
            "value_date": row[5],
            "as_of": row[6],
            "last_verified_at": row[7],
            "version": row[8],
            "status": row[9],
            "own_checked": bool(row[10]),
        }
    return facts


def test_s2_collectors_share_one_facts_query(tmp_path: Path) -> None:
    import brain.collectors.files as files
    import brain.collectors.github as github
    import brain.collectors.vanta as vanta
    from brain.collectors.base import known_facts

    root = Path(__file__).resolve().parents[1] / "src" / "brain" / "collectors"
    holding = [
        path.name
        for path in sorted(root.glob("*.py"))
        if "FROM facts" in path.read_text(encoding="utf-8")
    ]
    assert holding == ["base.py"]
    instance = collector_instance(tmp_path, "repo", "compliance")
    code, payload, err = collect_doc(instance, "repo", github_doc())
    assert code == 0, (payload, err)
    code, payload, err = collect_doc(instance, "compliance", vanta_doc())
    assert code == 0, (payload, err)
    con = store.connect_reader(instance / "brain.sqlite3")
    try:
        for module, prefix, agent in ((github, "eng.repo", "repo"), (vanta, "vendor.vanta", "compliance")):
            names = [name for name, _kind, _allowed in module.FACTS]
            keys = [f"{prefix}.{name}" for name in names]
            reference = _facts_reference(con, keys, agent)
            assert known_facts(con, prefix, names, agent) == reference
            assert all(item["own_checked"] for item in reference.values())
    finally:
        con.close()
    for fn in (
        files._known,
        files._plan,
        github._known,
        github._plan,
        vanta._known,
        vanta._plan,
    ):
        assert callable(fn)


def _brain_modules():
    root = Path(__file__).resolve().parents[1] / "src" / "brain"
    return sorted(root.rglob("*.py"))


def test_s3_runner_uses_only_public_applier_names() -> None:
    from brain.applier import Applier

    root = Path(__file__).resolve().parents[1]
    hits = [
        str(path.relative_to(root))
        for path in _brain_modules()
        if "_preconditions" in path.read_text(encoding="utf-8")
    ]
    assert hits == []
    assert callable(Applier.preconditions)
    tree = ast.parse(Path(collect.__file__).read_text(encoding="utf-8"))
    nested = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)) and child is not node:
                nested.append((node.lineno, child.lineno))
    assert nested == []


def _bind_imports(tree: ast.AST, ns: dict) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    ns[alias.asname] = importlib.import_module(alias.name)
                else:
                    ns[alias.name.split(".")[0]] = importlib.import_module(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            package = importlib.import_module(node.module)
            for alias in node.names:
                if alias.name == "*":
                    continue
                try:
                    value = getattr(package, alias.name)
                except AttributeError:
                    value = importlib.import_module(f"{node.module}.{alias.name}")
                ns[alias.asname or alias.name] = value


def _lookup(node: ast.AST, ns: dict):
    if isinstance(node, ast.Name):
        if node.id in ns:
            return ns[node.id]
        return getattr(builtins, node.id)
    if isinstance(node, ast.Attribute):
        return getattr(_lookup(node.value, ns), node.attr)
    raise LookupError(ast.dump(node))


def _except_classes(node: ast.AST | None, ns: dict) -> list[type]:
    if node is None:
        return [BaseException]
    if isinstance(node, ast.Tuple):
        found: list[type] = []
        for item in node.elts:
            found.extend(_except_classes(item, ns))
        return found
    value = _lookup(node, ns)
    if not isinstance(value, type):
        raise LookupError(ast.dump(node))
    return [value]


def test_s3_no_except_handler_is_unreachable() -> None:
    unreachable = []
    unresolved = []
    src = Path(__file__).resolve().parents[1] / "src"
    for path in _brain_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not any(isinstance(node, ast.Try) for node in ast.walk(tree)):
            continue
        module_name = ".".join(path.relative_to(src).with_suffix("").parts)
        module = importlib.import_module(module_name)
        ns = dict(module.__dict__)
        _bind_imports(tree, ns)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            seen: list[type] = []
            for handler in node.handlers:
                try:
                    classes = _except_classes(handler.type, ns)
                except (AttributeError, LookupError) as exc:
                    unresolved.append(f"{module_name}:{handler.lineno} {exc}")
                    continue
                for cls in classes:
                    for earlier in seen:
                        if issubclass(cls, earlier):
                            unreachable.append(
                                f"{module_name}:{handler.lineno} {cls.__name__} after {earlier.__name__}"
                            )
                seen.extend(classes)
    assert unresolved == []
    assert unreachable == []
