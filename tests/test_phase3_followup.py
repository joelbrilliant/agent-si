from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest

from brain import store
from brain.cli import main
from brain.collectors import REGISTRY
from brain.collectors.base import stable_id
from brain.config import ConfigError, load_config
from tests.collector_fixtures import (
    COLLECTORS,
    PRINCIPAL,
    _EXTRA_FACTS,
    _default_agents,
    _default_fact_keys,
    collect,
    collector_instance,
    gmail_doc,
)
from tests.conftest import NOW, make_packet, render_config, ro_rows, source_op, upsert_op

_ACCOUNT = "work"
_BATCH_THREAD = "thread-b"


def _apply(instance: Path, packet: dict) -> dict:
    path = instance.parent / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["apply", "--instance", str(instance), str(path)])
    assert code == 0, (stdout.getvalue(), stderr.getvalue())
    return json.loads(stdout.getvalue())


def _thread(external_id: str, subject: str) -> dict:
    return {
        "id": external_id,
        "subject": subject,
        "messages": [{"at": "2026-10-04T09:00:00Z", "direction": "inbound"}],
    }


def _document() -> dict:
    return gmail_doc(
        threads=[
            _thread("thread-a", "Alpha"),
            _thread(_BATCH_THREAD, "Beta"),
            _thread("thread-c", "Gamma"),
        ]
    )


def _seed_batch_two(instance: Path) -> None:
    _apply(
        instance,
        make_packet(
            "tester",
            source_op("src-seed-thread-b", "Synthetic note used only to give batch 2 a base."),
            upsert_op(
                "upsert_thread",
                {
                    "id": stable_id("thr-email-", _ACCOUNT, _BATCH_THREAD),
                    "channel": "email",
                    "account": _ACCOUNT,
                    "external_id": _BATCH_THREAD,
                    "subject": "Earlier subject",
                    "outbound_seen": False,
                },
                "src-seed-thread-b",
            ),
        ),
    )


def _bump_thread(instance: Path, external_id: str) -> None:
    thread_id = stable_id("thr-email-", _ACCOUNT, external_id)
    rows = ro_rows(instance, "SELECT version FROM threads WHERE id = ?", (thread_id,))
    if not rows:
        return
    base = rows[0][0]
    source_id = f"src-bump-{external_id}-{base}"
    _apply(
        instance,
        make_packet(
            "tester",
            source_op(source_id, "Synthetic note used only to move a thread version."),
            upsert_op(
                "upsert_thread",
                {"id": thread_id, "subject": f"Moved {base}"},
                source_id,
                base=base,
            ),
        ),
    )


def _known_wrapper(instance: Path, original, *, once: bool, keep_first_batch: bool = False):
    seen = {"n": 0}

    def known(con, doc, ctx):
        snapshot = original(con, doc, ctx)
        seen["n"] += 1
        if once and seen["n"] != 1:
            return snapshot
        if con.in_transaction:
            con.execute("COMMIT")
        _bump_thread(instance, _BATCH_THREAD)
        # After batch 1 has landed, move it too. A later plan then emits the same
        # batch-1 evidence, so attempt 1's source id is reused with the same content.
        if keep_first_batch:
            _bump_thread(instance, "thread-a")
        return snapshot

    return known


def _threads(instance: Path) -> dict[str, str]:
    return dict(ro_rows(instance, "SELECT external_id, subject FROM threads"))


def _collector_log(instance: Path) -> list[tuple]:
    return ro_rows(
        instance,
        "SELECT status, error, packet_json FROM write_log WHERE agent = 'gmailwork' ORDER BY rowid",
    )


def _input_sha256(doc: dict) -> str:
    return hashlib.sha256(store.canonical_json(doc).encode()).hexdigest()


def test_conflict_on_a_later_batch_retries_with_fresh_source_ids(tmp_path: Path, monkeypatch) -> None:
    import brain.collect as collect_mod

    monkeypatch.setattr(store, "utc_now", lambda: NOW)
    retry = collector_instance(tmp_path / "retry", "gmail-work")
    blocked = collector_instance(tmp_path / "blocked", "gmail-work")
    doc = _document()
    _seed_batch_two(retry)
    _seed_batch_two(blocked)

    gmail = REGISTRY["gmail"]
    original = gmail.known
    # One source plus one thread op is 2, so each thread is its own batch.
    monkeypatch.setattr(collect_mod, "BATCH_OPS", 2)
    monkeypatch.setitem(
        REGISTRY,
        "gmail",
        replace(gmail, known=_known_wrapper(retry, original, once=True)),
    )

    code, payload, err = collect(retry, "gmail-work", doc)
    log = _collector_log(retry)
    packets = [(status, json.loads(raw)) for status, _error, raw in log]
    assert [packet["intent"] for _status, packet in packets[:2]] == [
        "collect gmail-work: batch 1 of 3, 3 items",
        "collect gmail-work: batch 2 of 3, 3 items",
    ], (payload, err, log)
    assert any(status == "conflict" for status, _packet in packets), (payload, err, log)
    assert code == 0 and payload["status"] == "ok", (payload, err, log)
    assert _threads(retry) == {thread["id"]: thread["subject"] for thread in doc["threads"]}
    runs = ro_rows(retry, "SELECT status, items FROM collector_runs")
    assert runs == [("ok", len(doc["threads"]))]

    run_id = ro_rows(retry, "SELECT id FROM collector_runs")[0][0]
    run_key = run_id.removeprefix("run-gmail-work-")
    first_id = f"src-gmail-work-{run_key}-1"
    sources = ro_rows(
        retry,
        "SELECT id, content FROM sources WHERE id LIKE ? ORDER BY id",
        (f"src-gmail-work-{run_key}-%",),
    )
    by_id = {source_id: json.loads(content) for source_id, content in sources}
    assert first_id in by_id
    assert by_id[first_id] == {
        "collector": "gmail-work",
        "format": "agent-brain.gmail.v1",
        "fetched_at": doc["fetched_at"],
        "complete": True,
        "input_sha256": _input_sha256(doc),
        "items": [
            {
                "thread": "thread-a",
                "subject": "Alpha",
                "inbound": "2026-10-04T09:00:00Z",
                "outbound": None,
                "outbound_visible": True,
            }
        ],
    }
    retry_ids = [source_id for source_id in by_id if source_id != first_id]
    assert retry_ids
    assert all(
        re.fullmatch(rf"src-gmail-work-{run_key}-a2-[0-9]+", source_id) for source_id in retry_ids
    )
    assert all(status != "rejected" for status, _packet in packets)

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        check = main(["check", "--instance", str(retry)])
    assert check == 0, stderr.getvalue()
    assert json.loads(stdout.getvalue())["ok"] is True

    logged = ro_rows(retry, "SELECT count(*) FROM write_log")[0][0]
    again_code, again, again_err = collect(retry, "gmail-work", doc)
    assert again_code == 0, (again, again_err)
    assert again["duplicate"] is True
    assert again["status"] == "ok"
    assert again["packets"] == 0
    assert ro_rows(retry, "SELECT count(*) FROM write_log")[0][0] == logged

    monkeypatch.setitem(
        REGISTRY,
        "gmail",
        replace(gmail, known=_known_wrapper(blocked, original, once=False, keep_first_batch=True)),
    )
    blocked_code, blocked_payload, blocked_err = collect(blocked, "gmail-work", doc)
    blocked_log = _collector_log(blocked)
    blocked_packets = [(status, json.loads(raw)) for status, _error, raw in blocked_log]
    first_conflict = next(packet for status, packet in blocked_packets if status == "conflict")
    assert first_conflict["intent"] == "collect gmail-work: batch 2 of 3, 3 items", (
        blocked_payload,
        blocked_err,
        blocked_log,
    )
    assert blocked_code == 3, (blocked_payload, blocked_err, blocked_log)
    assert ro_rows(blocked, "SELECT count(*) FROM collector_runs")[0][0] == 0

    monkeypatch.setitem(REGISTRY, "gmail", gmail)
    clean_code, clean, clean_err = collect(blocked, "gmail-work", doc)
    assert clean_code == 0 and clean["status"] == "ok", (clean, clean_err)
    assert _threads(blocked) == {thread["id"]: thread["subject"] for thread in doc["threads"]}


def _highest_stored_attempt(instance: Path) -> tuple[str, int]:
    ids = [
        source_id
        for (source_id,) in ro_rows(instance, "SELECT id FROM sources")
        if source_id.startswith("src-gmail-work-")
    ]
    assert len(ids) == 1, ids
    assert re.fullmatch(r"src-gmail-work-[0-9a-f]{24}-1", ids[0]), ids
    prefix = ids[0].removesuffix("1")
    used = 0
    for source_id in ids:
        match = re.fullmatch(r"(?:a([0-9]+)-)?[0-9]+", source_id[len(prefix):])
        assert match, source_id
        used = max(used, int(match.group(1) or 1))
    return prefix, used


def test_a_later_call_never_reuses_an_earlier_calls_source_ids(tmp_path: Path, monkeypatch) -> None:
    import brain.collect as collect_mod
    from brain.applier import Applier

    monkeypatch.setattr(store, "utc_now", lambda: NOW)
    doc = _document()
    expected = {thread["id"]: thread["subject"] for thread in doc["threads"]}
    gmail = REGISTRY["gmail"]
    original_known = gmail.known
    original_apply = Applier.apply
    instances = {
        path: collector_instance(tmp_path / path, "gmail-work")
        for path in ("exhausted", "interrupted")
    }
    for instance in instances.values():
        _seed_batch_two(instance)
    monkeypatch.setattr(collect_mod, "BATCH_OPS", 2)

    def recover(instance: Path) -> None:
        monkeypatch.setitem(REGISTRY, "gmail", gmail)
        monkeypatch.setattr(Applier, "apply", original_apply)
        prefix, used = _highest_stored_attempt(instance)
        before = ro_rows(instance, "SELECT coalesce(max(rowid), 0) FROM write_log")[0][0]
        code, payload, err = collect(instance, "gmail-work", doc)
        log = _collector_log(instance)
        assert code == 0 and payload["status"] == "ok", (payload, err, log)
        assert _threads(instance) == expected
        assert ro_rows(instance, "SELECT status, items FROM collector_runs") == [
            ("ok", len(doc["threads"]))
        ]
        assert all(status != "rejected" for status, _error, _raw in log)
        new_rows = ro_rows(
            instance,
            "SELECT status, packet_json FROM write_log WHERE rowid > ? ORDER BY rowid",
            (before,),
        )
        assert new_rows and all(status == "applied" for status, _raw in new_rows)
        fresh = [
            op["row"]["id"]
            for _status, raw in new_rows
            for op in json.loads(raw)["ops"]
            if op["op"] == "add_source"
        ]
        assert fresh
        assert all(
            re.fullmatch(rf"{re.escape(prefix)}a{used + 1}-[0-9]+", source_id) for source_id in fresh
        ), fresh
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            check = main(["check", "--instance", str(instance)])
        assert check == 0, stderr.getvalue()
        assert json.loads(stdout.getvalue())["ok"] is True
        logged = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
        again_code, again, again_err = collect(instance, "gmail-work", doc)
        assert again_code == 0, (again, again_err)
        assert again["duplicate"] is True
        assert again["status"] == "ok"
        assert again["packets"] == 0
        assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == logged

    for path, instance in instances.items():
        monkeypatch.setitem(REGISTRY, "gmail", gmail)
        monkeypatch.setattr(Applier, "apply", original_apply)
        if path == "exhausted":
            monkeypatch.setitem(
                REGISTRY,
                "gmail",
                replace(gmail, known=_known_wrapper(instance, original_known, once=False)),
            )
            code, payload, err = collect(instance, "gmail-work", doc)
            log = _collector_log(instance)
            assert code == 3, (payload, err, log)
            assert ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0] == 0
        else:
            seen = {"n": 0}

            def apply(self, raw, seen=seen):
                seen["n"] += 1
                if seen["n"] == 2:
                    raise KeyboardInterrupt
                return original_apply(self, raw)

            monkeypatch.setattr(Applier, "apply", apply)
            with pytest.raises(KeyboardInterrupt):
                collect(instance, "gmail-work", doc)
            assert seen["n"] == 2
            assert ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0] == 0
        recover(instance)


def _write_config(directory: Path, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "brain.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _collector_config(name: str, approvers: list[str]) -> str:
    table, block = COLLECTORS[name]
    agents = _default_agents()
    agents[table["agent"]] = dict(block)
    agents["careful"] = {
        "operations": "*",
        "approval": "required",
        "approvers": list(approvers),
    }
    keys = list(_default_fact_keys())
    seen = {item["key"] for item in keys}
    for item in _EXTRA_FACTS.get(name, ()):
        if item["key"] not in seen:
            keys.append(dict(item))
            seen.add(item["key"])
    return render_config(
        principal=PRINCIPAL,
        agents=agents,
        collectors={name: dict(table)},
        fact_keys=keys,
    )


def test_a_collectors_agent_cannot_be_an_approver(tmp_path: Path) -> None:
    covered = {table["type"] for table, _block in COLLECTORS.values()}
    assert covered == set(REGISTRY)

    default = load_config(_write_config(tmp_path / "default", render_config()))
    assert default.agents["careful"].approvers == ("tester",)
    assert default.collectors == {}

    for name, (table, _block) in COLLECTORS.items():
        agent = table["agent"]
        message = (
            f"agents.careful: approver {agent} is collector {name}'s agent, "
            "and a collector's agent can't approve"
        )
        with pytest.raises(ConfigError, match=f"^{re.escape(message)}$"):
            load_config(_write_config(tmp_path / f"bad-{name}", _collector_config(name, [agent])))
        loaded = load_config(
            _write_config(tmp_path / f"good-{name}", _collector_config(name, ["tester"]))
        )
        assert loaded.collectors[name].agent == agent
        assert loaded.agents["careful"].approvers == ("tester",)


def test_collector_annotations_use_defined_names() -> None:
    import ast
    import builtins
    import sys

    covered: set[str] = set()
    modules = {}
    for ctype in REGISTRY.values():
        covered.add(ctype.name)
        module = sys.modules[ctype.plan.__module__]
        modules.setdefault(module.__name__, module)
    assert covered == set(REGISTRY)

    builtin_names = set(dir(builtins))
    missing: list[str] = []
    for module in modules.values():
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        defined = set(vars(module))

        def annotation_names(node: ast.AST) -> list[tuple[str, int]]:
            found = []
            for child in ast.walk(node):
                if isinstance(child, ast.Name):
                    found.append((child.id, child.lineno))
            return found

        names: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
                    names.extend(annotation_names(node.returns))
                annotations = [
                    arg.annotation
                    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
                ]
                if node.args.vararg is not None:
                    annotations.append(node.args.vararg.annotation)
                if node.args.kwarg is not None:
                    annotations.append(node.args.kwarg.annotation)
                for annotation in annotations:
                    if annotation is not None:
                        names.extend(annotation_names(annotation))
            elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
                names.extend(annotation_names(node.annotation))
        for name, line in names:
            if name not in defined and name not in builtin_names:
                missing.append(f"{module.__name__}:{line} {name}")
    assert missing == []
