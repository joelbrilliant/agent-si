from __future__ import annotations

import ast
import builtins
import json
import re
import socket
from pathlib import Path

import pytest

from brain import advice, store
from brain.cli import main
from brain.collectors import REGISTRY
from brain.collectors import github
from brain.collectors.files import SCHEMA as FILES_SCHEMA
from brain.collectors.base import (
    CollectorType,
    Group,
    LiveAdapter,
    LiveDisabled,
    Option,
    Plan,
    RunContext,
    hash_parts,
)
from brain.config import ConfigError, load_config
from tests.collector_fixtures import (
    COLLECTORS,
    PRINCIPAL,
    SHA40,
    SHA64,
    SAMPLES,
    _default_agents,
    _default_fact_keys,
    block_network,
    collect,
    collect_file,
    collector_instance,
    files_doc,
    github_doc,
    scenario_documents,
    vanta_doc,
    write_fixture,
)
from tests.conftest import NOW, file_sha, make_packet, render_config, ro_rows, source_op, upsert_op

NOTES_AGENT = {"kind": "collector", "operations": ["add_source", "record_collector_run"]}
NOTES_TABLE = {"type": "files", "agent": "notes", "root": "notes"}
REPO_AGENT = {
    "kind": "collector",
    "operations": ["add_source", "upsert_fact", "record_collector_run"],
    "scope": {"fact_prefixes": ["eng.repo"]},
}
REPO_TABLE = {"type": "github", "agent": "repo", "repo": "example/engine", "fact_prefix": "eng.repo"}
VANTA_AGENT = {
    "kind": "collector",
    "operations": ["add_source", "upsert_fact", "upsert_checklist_item", "record_collector_run"],
    "scope": {"fact_prefixes": ["vendor.vanta"], "projects": ["ops"]},
}
VANTA_TABLE = {
    "type": "vanta",
    "agent": "compliance",
    "fact_prefix": "vendor.vanta",
    "project": "ops",
    "document_ok_statuses": ["OK"],
}
GITHUB_FACT_KEYS = [
    {
        "key": "eng.repo.ci_state",
        "value_type": "enum",
        "allowed_values": ["success", "failure", "pending", "none"],
    },
    {"key": "eng.repo.last_push_on", "value_type": "date"},
    {"key": "eng.repo.open_pull_requests", "value_type": "int"},
]
_CI = [
    (None, None, "none"),
    ("queued", None, "pending"),
    ("in_progress", None, "pending"),
    ("completed", "success", "success"),
    ("completed", "neutral", "success"),
    ("completed", "skipped", "success"),
    ("completed", "failure", "failure"),
    ("completed", "cancelled", "failure"),
    ("completed", "timed_out", "failure"),
    ("completed", "action_required", "failure"),
    ("completed", "startup_failure", "failure"),
]


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch) -> None:
    monkeypatch.setattr(store, "utc_now", lambda: NOW)


def _config(*, agents=None, collectors=None, fact_keys=None) -> str:
    merged = _default_agents()
    if agents:
        merged.update(agents)
    return render_config(
        principal=PRINCIPAL,
        agents=merged,
        collectors=collectors,
        fact_keys=_default_fact_keys() if fact_keys is None else fact_keys,
    )


def _load(tmp_path: Path, text: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "brain.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def _apply(instance: Path, packet: dict) -> tuple[int, dict, str]:
    import contextlib
    import io

    path = instance.parent / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["apply", "--instance", str(instance), str(path)])
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def _file(path: str, sha: str, size: int = 4, mtime: str = "2026-10-07T00:00:00Z") -> dict:
    return {"path": path, "size": size, "mtime": mtime, "sha256": sha}


def _file_id(path: str, sha: str) -> str:
    return f"src-file-{hash_parts('notes', path)[:16]}-{sha[:16]}"


def _runs(instance: Path) -> list[tuple]:
    return ro_rows(instance, "SELECT status, items, error FROM collector_runs")


def _run_error(instance: Path) -> str:
    return ro_rows(instance, "SELECT error FROM collector_runs ORDER BY rowid DESC LIMIT 1")[0][0]


def _fact(instance: Path, key: str):
    rows = ro_rows(
        instance,
        "SELECT value_text, value_int, value_date, last_verified_at, version FROM facts WHERE key = ?",
        (key,),
    )
    return rows[0] if rows else None


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


def _checklist(instance: Path, item_id: str, ref: str, state: str, project: str = "ops") -> None:
    src = f"src-{item_id}"
    packet = make_packet(
        "tester",
        source_op(src, "Synthetic source for a checklist row."),
        upsert_op(
            "upsert_checklist_item",
            {
                "id": item_id,
                "project_id": project,
                "title": "Linked control",
                "state": state,
                "state_basis": "reported",
                "external_ref": ref,
                "last_verified_at": "2026-10-01T00:00:00Z",
            },
            src,
        ),
    )
    code, payload, _err = _apply(instance, packet)
    assert code == 0, payload


def _item(instance: Path, item_id: str) -> tuple:
    return ro_rows(
        instance,
        "SELECT state, state_basis, version FROM checklist_items WHERE id = ?",
        (item_id,),
    )[0]


def _register_probe(monkeypatch) -> None:
    monkeypatch.setitem(
        REGISTRY,
        "probe",
        CollectorType(
            name="probe",
            format="agent-brain.probe.v1",
            ops=("add_source", "record_collector_run"),
            options={"flag": Option("bool", required=True)},
            document_schema={"type": "object"},
            live=LiveAdapter("probe", (), (), "No live access"),
            known=lambda *_args: {},
            plan=lambda *_args: Plan((), 0),
        ),
    )


def _register_calendar(monkeypatch) -> None:
    monkeypatch.setitem(
        REGISTRY,
        "calendar",
        CollectorType(
            name="calendar",
            format="agent-brain.calendar.v1",
            ops=("add_source", "record_collector_run"),
            options={"account": Option("label", required=True)},
            document_schema={"type": "object"},
            live=LiveAdapter("calendar", (), (), "No live access"),
            known=lambda *_args: {},
            plan=lambda *_args: Plan((), 0),
        ),
    )


def _collector_for(type_name: str) -> str:
    for name, (table, _block) in COLLECTORS.items():
        if table["type"] == type_name:
            return name
    raise AssertionError(type_name)


def _cases() -> list[dict]:
    def notes(**table):
        body = dict(NOTES_TABLE)
        body.update(table)
        return _config(agents={"notes": NOTES_AGENT}, collectors={"notes": body})

    github_keys = list(GITHUB_FACT_KEYS)

    def repo_config(agent=None, table=None, fact_keys=None):
        return _config(
            agents={"repo": dict(REPO_AGENT if agent is None else agent)},
            collectors={"repo": dict(REPO_TABLE if table is None else table)},
            fact_keys=github_keys if fact_keys is None else fact_keys,
        )

    return [
        {
            "id": "not-a-table",
            "build": lambda: notes().replace("[collectors]\n", "[collectors]\nfoo = 1\n"),
            "error": "collectors.foo must be a table (collectors are [collectors.<name>] tables)",
        },
        {
            "id": "invalid-name",
            "build": lambda: _config(
                agents={"notes": NOTES_AGENT},
                collectors={"x": NOTES_TABLE},
            ),
            "error": "collector name x is invalid",
        },
        {
            "id": "unknown-type",
            "build": lambda: notes(type="postal"),
            "error": "collectors.notes.type is unknown: postal",
        },
        {
            "id": "live-path",
            "build": lambda: notes(live_path="refused"),
            "error": (
                "collectors.notes."
                + "live_path is for the live adapter, which is disabled until approved"
            ),
        },
        {
            "id": "capture",
            "build": lambda: notes(capture=True),
            "error": "collectors.notes.capture is not available in this version: files collects metadata only",
        },
        {
            "id": "authored-by-principal",
            "build": lambda: notes(authored_by_principal=True),
            "error": (
                "collectors.notes.authored_by_principal is not available in this version: "
                "files collects metadata only"
            ),
        },
        {
            "id": "unknown-key",
            "build": lambda: notes(colour="blue"),
            "error": "unknown key collectors.notes.colour",
        },
        {
            "id": "missing-root",
            "build": lambda: _config(
                agents={"notes": NOTES_AGENT},
                collectors={"notes": {"type": "files", "agent": "notes"}},
            ),
            "error": "collectors.notes.root is required",
        },
        {
            "id": "bad-label",
            "build": lambda: notes(root="Notes"),
            "error": "collectors.notes.root must be a label",
        },
        {
            "id": "bad-enum",
            "build": lambda: notes(source_kind="api"),
            "error": "collectors.notes.source_kind must be one of document, meeting_transcript, user_note",
        },
        {
            "id": "bad-fact-prefix",
            "build": lambda: repo_config(table={**REPO_TABLE, "fact_prefix": "Eng"}),
            "error": "collectors.repo.fact_prefix must be a fact prefix",
        },
        {
            "id": "bad-repo",
            "build": lambda: repo_config(table={**REPO_TABLE, "repo": "example"}),
            "error": "collectors.repo.repo must be a repo",
        },
        {
            "id": "bad-workstream",
            "build": lambda: _config(
                agents={"compliance": VANTA_AGENT},
                collectors={"compliance": {**VANTA_TABLE, "project": "missing"}},
            ),
            "error": "collectors.compliance.project must be a configured workstream",
        },
        {
            "id": "bad-strings",
            "build": lambda: _config(
                agents={"compliance": VANTA_AGENT},
                collectors={"compliance": {**VANTA_TABLE, "document_ok_statuses": []}},
            ),
            "error": (
                "collectors.compliance.document_ok_statuses must be 1 to 10 "
                "one-line strings of 1 to 40 characters"
            ),
        },
        {
            "id": "bad-bool",
            "register": _register_probe,
            "build": lambda: _config(
                collectors={"probe": {"type": "probe", "agent": "notes", "flag": 1}},
            ),
            "error": "collectors.probe.flag must be a boolean",
        },
        {
            "id": "missing-agent",
            "build": lambda: notes(agent="ghost"),
            "error": "collectors.notes.agent ghost is not a configured agent",
        },
        {
            "id": "agent-kind",
            "build": lambda: _config(
                agents={"assistant": {"kind": "agent", "operations": "*"}, "notes": NOTES_AGENT},
                collectors={"notes": {**NOTES_TABLE, "agent": "assistant"}},
            ),
            "error": 'collectors.notes.agent assistant must have kind = "collector"',
        },
        {
            "id": "manual-sources",
            "build": lambda: _config(
                agents={"notes": {**NOTES_AGENT, "manual_sources": True}},
                collectors={"notes": NOTES_TABLE},
            ),
            "error": "agents.notes: manual_sources = true is not allowed on collector notes's agent",
        },
        {
            "id": "two-collectors",
            "build": lambda: _config(
                agents={"notes": NOTES_AGENT},
                collectors={
                    "notes": NOTES_TABLE,
                    "extra": {"type": "files", "agent": "notes", "root": "notes"},
                },
            ),
            "error": "agent notes is used by collectors notes and extra",
        },
        {
            "id": "operations",
            "build": lambda: _config(
                agents={"notes": {"kind": "collector", "operations": ["add_source"]}},
                collectors={"notes": NOTES_TABLE},
            ),
            "error": (
                'collectors.notes: agent notes must have operations exactly '
                '["add_source", "record_collector_run"]'
            ),
        },
        {
            "id": "approval",
            "build": lambda: _config(
                agents={
                    "notes": {
                        **NOTES_AGENT,
                        "approval": "required",
                        "approvers": ["tester"],
                    }
                },
                collectors={"notes": NOTES_TABLE},
            ),
            "error": "collectors.notes: agent notes must not need approval",
        },
        {
            "id": "scope-none",
            "build": lambda: _config(
                agents={"notes": {**NOTES_AGENT, "scope": {"projects": ["general"]}}},
                collectors={"notes": NOTES_TABLE},
            ),
            "error": "collectors.notes: agent notes must have scope none",
        },
        {
            "id": "scope-github",
            "build": lambda: repo_config(agent={**REPO_AGENT, "scope": {}}),
            "error": 'collectors.repo: agent repo must have scope {"fact_prefixes":["eng.repo"]}',
        },
        {
            "id": "scope-vanta",
            "build": lambda: _config(
                agents={"compliance": {**VANTA_AGENT, "scope": {"fact_prefixes": ["vendor.vanta"]}}},
                collectors={"compliance": VANTA_TABLE},
                fact_keys=[
                    {"key": "vendor.vanta.controls_complete", "value_type": "int"},
                    {"key": "vendor.vanta.controls_total", "value_type": "int"},
                    {"key": "vendor.vanta.documents_ok", "value_type": "int"},
                    {"key": "vendor.vanta.documents_total", "value_type": "int"},
                    {"key": "vendor.vanta.failing_tests", "value_type": "int"},
                ],
            ),
            "error": (
                "collectors.compliance: agent compliance must have scope "
                '{"fact_prefixes":["vendor.vanta"],"projects":["ops"]}'
            ),
        },
        {
            "id": "author-principal",
            "build": lambda: _config(
                agents={"notes": {**NOTES_AGENT, "author_principal": True}},
                collectors={"notes": NOTES_TABLE},
            ),
            "error": "collectors.notes: agent notes must not have author_principal",
        },
        {
            "id": "fact-key",
            "build": lambda: repo_config(fact_keys=GITHUB_FACT_KEYS[1:]),
            "error": "collectors.repo needs fact key eng.repo.ci_state (enum)",
        },
        {
            "id": "duplicate-account",
            "register": _register_calendar,
            "build": lambda: _config(
                agents={
                    "cala": {"kind": "collector", "operations": ["add_source", "record_collector_run"]},
                    "calb": {"kind": "collector", "operations": ["add_source", "record_collector_run"]},
                },
                collectors={
                    "cal-a": {"type": "calendar", "agent": "cala", "account": "work"},
                    "cal-b": {"type": "calendar", "agent": "calb", "account": "work"},
                },
            ),
            "error": "collectors cal-a and cal-b both collect calendar account work",
        },
        {
            "id": "named-agent-needs-no-opt-in",
            "build": lambda: notes(),
            "error": None,
        },
    ]


@pytest.mark.parametrize("case", _cases(), ids=lambda case: case["id"])
def test_collector_config_is_validated(tmp_path: Path, case: dict, monkeypatch) -> None:
    register = case.get("register")
    if register is not None:
        register(monkeypatch)
    if case["error"] is None:
        cfg = _load(tmp_path, case["build"]())
        assert cfg.agents["notes"].manual_sources is False
        assert cfg.collectors["notes"].options["source_kind"] == "document"
    else:
        with pytest.raises(ConfigError, match=f"^{re.escape(case['error'])}$"):
            _load(tmp_path, case["build"]())


def test_collect_needs_a_fixture(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "notes")
    before = file_sha(instance / "brain.sqlite3")
    import contextlib
    import io

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["collect", "--instance", str(instance), "notes"])
    assert code == 2
    assert stdout.getvalue() == ""
    assert "brain collect needs --input FILE; the engine reads exported files only" in stderr.getvalue()
    assert file_sha(instance / "brain.sqlite3") == before
    assert _runs(instance) == []


def test_unknown_collector_is_a_usage_error(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "notes")
    path = write_fixture(tmp_path, files_doc())
    code, payload, err = collect_file(instance, "nope", path)
    assert code == 2
    assert payload is None
    assert "no collector nope" in err
    assert _runs(instance) == []


@pytest.mark.parametrize(
    "kind",
    ["missing", "huge", "binary", "text", "nan", "nested"],
)
def test_unreadable_fixture_writes_nothing(tmp_path: Path, kind: str) -> None:
    instance = collector_instance(tmp_path, "notes")
    before = file_sha(instance / "brain.sqlite3")
    path = tmp_path / "fixture.json"
    if kind == "missing":
        path = tmp_path / "missing.json"
    elif kind == "huge":
        descriptor = path.open("wb")
        try:
            descriptor.truncate(32 * 1024 * 1024 + 1)
        finally:
            descriptor.close()
    elif kind == "binary":
        path.write_bytes(b"\xff")
    elif kind == "text":
        path.write_text("{", encoding="utf-8")
    elif kind == "nan":
        path.write_text('{"value": NaN}', encoding="utf-8")
    else:
        path.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    code, payload, err = collect_file(instance, "notes", path)
    assert code == 2
    assert payload is None
    assert err.startswith("brain: fixture ")
    assert file_sha(instance / "brain.sqlite3") == before
    assert _runs(instance) == []


@pytest.mark.parametrize("kind", ["format", "field", "duplicate"])
def test_invalid_document_records_an_error_run(tmp_path: Path, kind: str) -> None:
    instance = collector_instance(tmp_path, "notes")
    sources = ro_rows(instance, "SELECT count(*) FROM sources")[0][0]
    if kind == "format":
        doc = files_doc(format="nope")
        needle = "document is invalid: /format must be 'agent-brain.files.v1'"
    elif kind == "field":
        doc = files_doc(extra=True)
        needle = "document is invalid: /extra " + advice.unknown_property_error("extra", FILES_SCHEMA["properties"])
    else:
        item = _file("2026/board-prep.md", SHA64)
        doc = files_doc(files=[item, dict(item)])
        needle = "duplicate file id 2026/board-prep.md"
    code, payload, _err = collect(instance, "notes", doc)
    assert code == 1
    assert payload["status"] == "error"
    assert payload["items"] == 0
    assert _runs(instance) == [("error", 0, needle)]
    assert ro_rows(instance, "SELECT count(*) FROM sources")[0][0] == sources


def test_collect_is_idempotent_by_input(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "notes")
    code, first, _err = collect(instance, "notes", files_doc())
    assert code == 0 and first["duplicate"] is False
    runs = ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0]
    logged = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    code, second, _err = collect(instance, "notes", files_doc())
    assert code == 0
    assert second["duplicate"] is True
    assert second["packets"] == 0
    assert second["receipt"] == first["receipt"]
    path = write_fixture(tmp_path, files_doc())
    other = tmp_path / "again.json"
    other.write_bytes(path.read_bytes())
    code, third, _err = collect_file(instance, "notes", other)
    assert code == 0 and third["duplicate"] is True
    assert third["receipt"] == first["receipt"]
    assert ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0] == runs
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == logged


def test_change_ids_are_content_addressed_and_retry_after_conflict(tmp_path: Path, monkeypatch) -> None:
    instance = collector_instance(tmp_path / "one", "repo")
    original = github.known
    seen = {"n": 0}

    def once(con, doc, ctx):
        snapshot = original(con, doc, ctx)
        seen["n"] += 1
        if seen["n"] == 1:
            if con.in_transaction:
                con.execute("COMMIT")
            _bump_ci(instance)
        return snapshot

    monkeypatch.setattr(github, "known", once)
    code, payload, err = collect(instance, "repo", github_doc())
    assert code == 0, (payload, err)
    rows = ro_rows(
        instance,
        "SELECT change_id, status FROM write_log WHERE agent = 'repo' ORDER BY rowid",
    )
    assert any(status == "conflict" for _change, status in rows)
    assert rows[-1][1] == "applied"
    assert len({change for change, _status in rows}) > 1
    assert ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0] == 1

    other = collector_instance(tmp_path / "two", "repo")

    def always(con, doc, ctx):
        snapshot = original(con, doc, ctx)
        if con.in_transaction:
            con.execute("COMMIT")
        _bump_ci(other)
        return snapshot

    monkeypatch.setattr(github, "known", always)
    code, payload, err = collect(other, "repo", github_doc())
    assert code == 3, (payload, err)
    assert payload["status"] is None
    assert ro_rows(other, "SELECT count(*) FROM collector_runs")[0][0] == 0

    monkeypatch.setattr(github, "known", original)
    doc = github_doc()
    left = collector_instance(tmp_path / "left", "repo")
    right = collector_instance(tmp_path / "right", "repo")
    assert collect(left, "repo", doc)[0] == 0
    assert collect(right, "repo", doc)[0] == 0
    left_ids = [
        row[0]
        for row in ro_rows(left, "SELECT change_id FROM write_log WHERE agent = 'repo' ORDER BY change_id")
    ]
    right_ids = [
        row[0]
        for row in ro_rows(right, "SELECT change_id FROM write_log WHERE agent = 'repo' ORDER BY change_id")
    ]
    assert left_ids == right_ids and left_ids


def test_large_runs_split_into_batches_with_the_run_last(tmp_path: Path, monkeypatch) -> None:
    import brain.collect as collect_mod

    instance = collector_instance(tmp_path, "notes", "compliance")
    monkeypatch.setattr(collect_mod, "BATCH_OPS", 5)
    files = [
        _file(f"n/{index}.md", f"{index:02x}" * 32, size=index) for index in range(12)
    ]
    code, payload, err = collect(instance, "notes", files_doc(files=files))
    assert code == 0, (payload, err)
    packets = [
        json.loads(row[0])
        for row in ro_rows(
            instance,
            "SELECT packet_json FROM write_log WHERE agent = 'notes' ORDER BY applied_seq",
        )
    ]
    assert [len(packet["ops"]) for packet in packets] == [5, 5, 3]
    assert [any(op["op"] == "record_collector_run" for op in packet["ops"]) for packet in packets] == [
        False,
        False,
        True,
    ]
    assert [packet["intent"] for packet in packets] == [
        "collect notes: batch 1 of 3, 12 items",
        "collect notes: batch 2 of 3, 12 items",
        "collect notes: batch 3 of 3, 12 items",
    ]
    monkeypatch.setattr(collect_mod, "BATCH_OPS", 4)
    code, payload, err = collect(instance, "compliance", vanta_doc())
    assert code == 0, (payload, err)
    packets = [
        json.loads(row[0])
        for row in ro_rows(
            instance,
            "SELECT packet_json FROM write_log WHERE agent = 'compliance' ORDER BY applied_seq",
        )
    ]
    assert len(packets) == 2
    for index, packet in enumerate(packets, start=1):
        assert packet["intent"] == f"collect compliance: batch {index} of 2, 3 items"
        source = packet["ops"][0]
        assert source["op"] == "add_source"
        assert re.fullmatch(r"src-compliance-[0-9a-f]{24}-" + str(index), source["row"]["id"])
        body = json.loads(source["row"]["content"])
        evidence_ops = [
            op for op in packet["ops"] if op["op"] not in {"add_source", "record_collector_run"}
        ]
        assert [item["fact"] for item in body["items"]] == [
            op["source"]["locator"].removeprefix("fact ") for op in evidence_ops
        ]


def test_rejected_batch_records_an_error_run(tmp_path: Path, monkeypatch) -> None:
    import brain.collect as collect_mod

    instance = collector_instance(tmp_path, "repo")
    original = github.plan

    def wrapped(doc, known, ctx):
        plan = original(doc, known, ctx)
        bad = Group(
            (
                {
                    "op": "upsert_fact",
                    "base": None,
                    "source": {"locator": "fact motto"},
                    "row": {
                        "key": "demo.motto",
                        "value": "steady",
                        "basis": "observed",
                        "as_of": "2026-10-08",
                        "last_verified_at": doc["fetched_at"],
                    },
                },
            ),
            {"fact": "motto", "value": "steady"},
        )
        return Plan(plan.groups + (bad,), plan.items, plan.note)

    monkeypatch.setattr(github, "plan", wrapped)
    monkeypatch.setattr(collect_mod, "BATCH_OPS", 2)
    code, payload, err = collect(instance, "repo", github_doc())
    assert code == 1, (payload, err)
    message = _run_error(instance)
    assert re.fullmatch(
        r"packet repo-[0-9a-f]{24} was rejected: op 1 \(upsert_fact\): fact key demo\.motto "
        r"is outside agent repo's fact prefixes \[eng\.repo\]",
        message,
    )
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id LIKE 'src-repo-%'")[0][0] >= 1
    assert _fact(instance, "eng.repo.ci_state") is not None


def test_l1_collector_agents_submit_only_through_brain_collect(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "notes")
    packet = make_packet(
        "notes",
        source_op("src-hand-note", "A note submitted through brain apply."),
    )
    code, payload, _err = _apply(instance, packet)
    assert code == 1
    assert payload["error"] == (
        "agent notes belongs to collector notes and submits only through brain collect"
    )
    code, collected, err = collect(instance, "notes", files_doc())
    assert code == 0, (collected, err)
    assert collected["status"] == "ok"


def test_l1_assistant_quotes_a_collected_source(tmp_path: Path) -> None:
    instance = collector_instance(
        tmp_path,
        "compliance",
        agents={"assistant": {"kind": "agent", "operations": "*"}},
    )
    code, payload, err = collect(instance, "compliance", vanta_doc())
    assert code == 0, (payload, err)
    source_id = ro_rows(
        instance, "SELECT id FROM sources WHERE id LIKE 'src-compliance-%'"
    )[0][0]
    content = ro_rows(instance, "SELECT content FROM sources WHERE id = ?", (source_id,))[0][0]
    assert "MFA on for every account" in content
    person = make_packet(
        "tester",
        source_op("src-sam-note", "Synthetic source for a person row."),
        upsert_op(
            "upsert_person",
            {"id": "per-sam-example", "display_name": "Sam Example"},
            "src-sam-note",
        ),
    )
    assert _apply(instance, person)[0] == 0
    good = make_packet(
        "assistant",
        upsert_op(
            "upsert_decision",
            {
                "id": "dec-mfa",
                "title": "Review the failing test",
                "quote": "MFA on for every account",
                "decided_on": "2026-10-08",
                "decided_by_person_id": "per-sam-example",
                "status": "in_effect",
                "project_id": "ops",
            },
            source_id,
            locator="fact failing_tests",
        ),
    )
    code, payload, _err = _apply(instance, good)
    assert code == 0, payload
    bad = make_packet(
        "assistant",
        upsert_op(
            "upsert_decision",
            {
                "id": "dec-other",
                "title": "Review the failing test",
                "quote": "purple widgets are never collected",
                "decided_on": "2026-10-08",
                "decided_by_person_id": "per-sam-example",
                "status": "proposed",
                "project_id": "ops",
            },
            source_id,
            locator="fact failing_tests",
        ),
    )
    code, payload, _err = _apply(instance, bad)
    assert code == 1
    assert "decision quote is not in the cited source" in payload["error"]
    refused = make_packet(
        "assistant",
        source_op("src-assistant-note", "Assistant tries to add its own source."),
    )
    code, payload, _err = _apply(instance, refused)
    assert code == 1
    assert payload["error"] == "op 0 (add_source): not allowed for agent assistant"


def test_live_adapters_are_disabled_and_unwired(tmp_path: Path) -> None:
    order = ("files", "calendar", "github", "vanta", "gmail", "slack", "capture")
    present = tuple(name for name in order if name in REGISTRY)
    assert tuple(REGISTRY) == present
    expected = {
        "files": (("live_path",), ()),
        # Brain-fetch feeds calendar directly, so its two fetch keys are options now.
        "calendar": ((), ("BRAIN_<NAME>_TOKEN_FILE",)),
        "github": (("live_api_url",), ("BRAIN_<NAME>_TOKEN",)),
        "vanta": (
            ("live_api_url",),
            ("BRAIN_<NAME>_CLIENT_ID", "BRAIN_<NAME>_CLIENT_SECRET"),
        ),
        "gmail": (
            ("live_query", "live_max_threads"),
            ("BRAIN_<NAME>_TOKEN_FILE",),
        ),
        "slack": (
            ("live_channels", "live_max_threads"),
            ("BRAIN_<NAME>_TOKEN",),
        ),
        "capture": ((), ()),
    }
    for type_name, ctype in REGISTRY.items():
        adapter = getattr(ctype, "live")
        with pytest.raises(LiveDisabled):
            getattr(adapter, "fet" + "ch")()
        assert tuple(adapter.config_keys) == expected[type_name][0]
        assert tuple(adapter.env_vars) == expected[type_name][1]
        collector = _collector_for(type_name)
        for key in adapter.config_keys:
            table = dict(COLLECTORS[collector][0])
            table[key] = "refused"
            text = _config(collectors={collector: table})
            with pytest.raises(
                ConfigError,
                match="is for the live adapter, which is disabled until approved",
            ):
                _load(tmp_path / type_name / key, text)
    needles = ("fet" + "ch" + "(", "." + "live")
    root = Path(__file__).resolve().parents[1]
    for base in (root / "src", root / "tests"):
        for path in base.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel.startswith("src/brain/collectors/"):
                continue
            text = path.read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in text, rel


def test_collector_modules_import_no_io_or_network() -> None:
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "hashlib",
        "json",
        "re",
        "sqlite3",
        "typing",
        "unicodedata",
        "zoneinfo",
        "brain.schemas",
        "brain.store",
        "brain.validate",
        "brain.collectors",
    }
    forbidden = {"open", "os", "environ", "getenv", "socket", "subprocess", "urllib", "http", "ssl"}
    root = Path(__file__).resolve().parents[1] / "src" / "brain" / "collectors"
    modules = list(root.glob("*.py"))
    assert modules

    def allowed_module(name: str) -> bool:
        return name in allowed or name.startswith("brain.collectors.")

    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert allowed_module(alias.name), path.name
                    assert alias.asname not in forbidden
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                assert allowed_module(node.module or ""), path.name
                for alias in node.names:
                    assert alias.name not in forbidden
                    assert alias.asname not in forbidden
            elif isinstance(node, ast.Name):
                assert node.id not in forbidden, path.name
            elif isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, path.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                assert node.name not in forbidden
            elif isinstance(node, ast.arg):
                assert node.arg not in forbidden


def test_brain_collect_runs_with_network_blocked(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, *COLLECTORS)
    with block_network():
        for type_name, builder in SAMPLES.items():
            name = _collector_for(type_name)
            code, payload, err = collect(instance, name, builder())
            assert code == 0, (name, payload, err)
            assert payload["status"] == "ok"


def test_collector_fixtures_hold_no_addresses_or_real_hosts() -> None:
    docs = [builder() for builder in SAMPLES.values()]
    docs.extend(scenario_documents())
    for doc in docs:
        text = store.canonical_json(doc)
        assert "@" not in text
        for host in re.findall(r"https?://([^/\s\"]+)", text):
            host = host.split(":")[0]
            assert host in {"example.com", "example.org"} or host.endswith(".example")


def test_plan_is_pure_and_deterministic(tmp_path: Path, monkeypatch) -> None:
    instance = collector_instance(tmp_path, *COLLECTORS)
    cfg = load_config(instance / "brain.toml")
    con = store.connect_reader(instance / "brain.sqlite3")
    prepared = []
    try:
        for type_name, builder in SAMPLES.items():
            name = _collector_for(type_name)
            ctype = REGISTRY[type_name]
            doc = builder()
            ctx = RunContext(name, cfg.collectors[name].options, cfg.timezone, "a" * 24, NOW)
            prepared.append((ctype, doc, ctype.known(con, doc, ctx), ctx))
    finally:
        con.close()

    planned = []
    for ctype, doc, known, ctx in prepared:
        first = ctype.plan(doc, known, ctx)
        assert ctype.plan(doc, known, ctx) == first
        planned.append((ctype, doc, known, ctx, first))

    def boom(*_args, **_kwargs):
        raise AssertionError("plan used a side effect")

    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(store, "utc_now", boom)
    for ctype, doc, known, ctx, first in planned:
        assert ctype.plan(doc, known, ctx) == first


def test_files_adds_one_source_per_file_version(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "notes")
    first_sha = "11" * 32
    second_sha = "22" * 32
    files = [_file("a.md", first_sha), _file("b.md", second_sha)]
    code, payload, err = collect(instance, "notes", files_doc(files=files))
    assert code == 0, (payload, err)
    rows = ro_rows(
        instance,
        "SELECT id, uri, external_ref FROM sources WHERE id LIKE 'src-file-%' ORDER BY external_ref",
    )
    assert rows == [
        (_file_id("a.md", first_sha), "", "file:notes/a.md"),
        (_file_id("b.md", second_sha), "", "file:notes/b.md"),
    ]
    changed = "33" * 32
    code, _payload, err = collect(
        instance,
        "notes",
        files_doc(files=[_file("a.md", changed), _file("b.md", second_sha)]),
    )
    assert code == 0, err
    ids = {
        row[0]
        for row in ro_rows(instance, "SELECT id FROM sources WHERE id LIKE 'src-file-%'")
    }
    assert _file_id("a.md", first_sha) in ids
    assert _file_id("a.md", changed) in ids
    assert len(ids) == 3
    code, _payload, err = collect(
        instance,
        "notes",
        files_doc(
            files=[
                _file("a.md", changed, mtime="2026-10-07T02:00:00Z"),
                _file("b.md", second_sha),
            ],
        ),
    )
    assert code == 0, err
    assert (
        ro_rows(instance, "SELECT count(*) FROM sources WHERE id LIKE 'src-file-%'")[0][0] == 3
    )


def test_files_are_metadata_only(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "notes")
    item = _file("2026/board-prep.md", SHA64, size=66, mtime="2026-10-07T22:00:00Z")
    code, payload, err = collect(instance, "notes", files_doc(files=[item]))
    assert code == 0, (payload, err)
    content, author = ro_rows(
        instance,
        "SELECT content, author_person_id FROM sources WHERE id = ?",
        (_file_id(item["path"], SHA64),),
    )[0]
    assert author is None
    assert content == (
        "file: notes/2026/board-prep.md\n"
        f"sha256: {SHA64}\n"
        "size: 66 bytes\n"
        "modified: 2026-10-07T22:00:00Z"
    )
    doc = files_doc()
    doc["files"][0]["text"] = "file body"
    code, payload, _err = collect(instance, "notes", doc)
    assert code == 1
    assert "is not allowed" in _run_error(instance)
    with pytest.raises(ConfigError, match="files collects metadata only"):
        _load(tmp_path, _config(agents={"notes": NOTES_AGENT}, collectors={"notes": {**NOTES_TABLE, "capture": True}}))
    with pytest.raises(ConfigError, match="files collects metadata only"):
        _load(
            tmp_path / "other",
            _config(
                agents={"notes": NOTES_AGENT},
                collectors={"notes": {**NOTES_TABLE, "authored_by_principal": True}},
            ),
        )


@pytest.mark.parametrize("path", ["/abs", "a/../b", "./a", "a//b", "a\\b", "a\u0001b"])
def test_files_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    instance = collector_instance(tmp_path, "notes")
    code, payload, _err = collect(instance, "notes", files_doc(files=[_file(path, SHA64)]))
    assert code == 1
    assert payload["status"] == "error"
    assert _run_error(instance) == f"unsafe path {path}"


def test_incomplete_document_writes_no_aggregate_facts(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "repo", "compliance")
    _checklist(instance, "chk-ac-1", "vanta:AC-1", "in_progress")
    code, payload, err = collect(instance, "repo", github_doc(fetched_at="2026-10-07T12:00:00Z"))
    assert code == 0, (payload, err)
    before = {
        key: _fact(instance, key)[4]
        for key in (
            "eng.repo.ci_state",
            "eng.repo.open_pull_requests",
            "eng.repo.last_push_on",
        )
    }
    code, payload, err = collect(
        instance, "repo", github_doc(complete=False)
    )
    assert code == 0, (payload, err)
    assert payload["status"] == "partial"
    assert "the document is incomplete" in _run_error(instance)
    packets = [
        json.loads(row[0])
        for row in ro_rows(
            instance,
            "SELECT packet_json FROM write_log WHERE agent = 'repo' ORDER BY rowid DESC LIMIT 1",
        )
    ]
    assert all(op["op"] != "upsert_fact" for op in packets[0]["ops"])
    for key, version in before.items():
        assert _fact(instance, key)[4] == version
    for key in (
        "vendor.vanta.controls_total",
        "vendor.vanta.controls_complete",
        "vendor.vanta.documents_total",
        "vendor.vanta.documents_ok",
        "vendor.vanta.failing_tests",
    ):
        packet = make_packet(
            "tester",
            source_op(f"src-seed-{key.replace('.', '-')}", "Synthetic fact seed. The count is 9."),
            upsert_op(
                "upsert_fact",
                {
                    "key": key,
                    "value": 9,
                    "basis": "observed",
                    "as_of": "2026-10-01",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                    "quote": "The count is 9",
                    "value_raw": "9",
                },
                f"src-seed-{key.replace('.', '-')}",
            ),
        )
        assert _apply(instance, packet)[0] == 0
    seeded = {key: _fact(instance, key)[4] for key in (
        "vendor.vanta.controls_total",
        "vendor.vanta.controls_complete",
        "vendor.vanta.documents_total",
        "vendor.vanta.documents_ok",
        "vendor.vanta.failing_tests",
    )}
    code, payload, err = collect(instance, "compliance", vanta_doc(complete=False))
    assert code == 0, (payload, err)
    assert payload["status"] == "partial"
    assert _item(instance, "chk-ac-1")[0] == "done"
    for key, version in seeded.items():
        assert _fact(instance, key)[4] == version
    vanta_packets = [
        json.loads(row[0])
        for row in ro_rows(
            instance, "SELECT packet_json FROM write_log WHERE agent = 'compliance'"
        )
    ]
    assert all(
        op["op"] != "upsert_fact" for packet in vanta_packets for op in packet["ops"]
    )


@pytest.mark.parametrize("status,conclusion,expected", _CI)
def test_github_writes_ci_pr_and_push_facts(tmp_path, status, conclusion, expected) -> None:
    instance = collector_instance(tmp_path, "repo", timezone="Australia/Sydney")
    runs = []
    if status is not None:
        runs = [
            {
                "id": 1,
                "branch": "main",
                "head_sha": SHA40,
                "status": "completed",
                "conclusion": "failure" if expected != "failure" else "success",
                "updated_at": "2026-10-07T01:00:00Z",
            },
            {
                "id": 2,
                "branch": "dev",
                "head_sha": SHA40,
                "status": "completed",
                "conclusion": "failure",
                "updated_at": "2026-10-07T04:00:00Z",
            },
            {
                "id": 3,
                "branch": "main",
                "head_sha": SHA40,
                "status": status,
                "conclusion": conclusion,
                "updated_at": "2026-10-07T02:00:00Z",
            },
        ]
    else:
        runs = [
            {
                "id": 2,
                "branch": "dev",
                "head_sha": SHA40,
                "status": "completed",
                "conclusion": "success",
                "updated_at": "2026-10-07T02:00:00Z",
            }
        ]
    doc = github_doc(
        pull_requests=[
            {"number": 12, "state": "open", "draft": False, "updated_at": "2026-10-07T01:00:00Z"},
            {"number": 13, "state": "open", "draft": True, "updated_at": "2026-10-07T01:00:00Z"},
            {"number": 14, "state": "closed", "draft": False, "updated_at": "2026-10-07T01:00:00Z"},
        ],
        runs=runs,
        pushes=[
            {"branch": "main", "head_sha": SHA40, "at": "2026-10-07T01:00:00Z"},
            {"branch": "main", "head_sha": SHA40, "at": "2026-10-07T20:00:00Z"},
        ],
    )
    code, payload, err = collect(instance, "repo", doc)
    assert code == 0, (payload, err)
    assert _fact(instance, "eng.repo.ci_state")[0] == expected
    assert _fact(instance, "eng.repo.open_pull_requests")[1] == 2
    assert _fact(instance, "eng.repo.last_push_on")[2] == "2026-10-08"


def test_github_requires_registered_fact_keys(tmp_path: Path) -> None:
    missing = _config(
        agents={"repo": REPO_AGENT},
        collectors={"repo": REPO_TABLE},
        fact_keys=GITHUB_FACT_KEYS[1:],
    )
    with pytest.raises(ConfigError, match="^collectors.repo needs fact key eng.repo.ci_state \\(enum\\)$"):
        _load(tmp_path, missing)
    wrong = dict(GITHUB_FACT_KEYS[0])
    wrong["allowed_values"] = ["success", "failure"]
    text = _config(
        agents={"repo": REPO_AGENT},
        collectors={"repo": REPO_TABLE},
        fact_keys=[wrong, *GITHUB_FACT_KEYS[1:]],
    )
    with pytest.raises(ConfigError, match="^collectors.repo needs fact key eng.repo.ci_state \\(enum\\)$"):
        _load(tmp_path / "wrong", text)


def test_github_facts_never_regress_to_an_older_snapshot(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "repo")
    assert collect(instance, "repo", github_doc(fetched_at="2026-10-07T03:00:00Z"))[0] == 0
    version = _fact(instance, "eng.repo.ci_state")[4]
    verified = _fact(instance, "eng.repo.ci_state")[3]
    assert collect(instance, "repo", github_doc(fetched_at="2026-10-07T02:30:00Z"))[0] == 0
    assert _fact(instance, "eng.repo.ci_state")[4] == version
    assert _fact(instance, "eng.repo.ci_state")[3] == verified
    assert collect(instance, "repo", github_doc(fetched_at="2026-10-08T00:00:00Z"))[0] == 0
    row = _fact(instance, "eng.repo.ci_state")
    assert row[0] == "success"
    assert row[3] == "2026-10-08T00:00:00Z"
    assert row[4] == version + 1


def test_vanta_writes_compliance_facts(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "compliance")
    doc = vanta_doc(
        controls=[
            {"id": "ctl-0001", "external_id": "AC-1", "complete": True},
            {"id": "ctl-0002", "external_id": "AC-2", "complete": False},
        ],
        documents=[
            {"id": "doc-0001", "status": "OK"},
            {"id": "doc-0002", "status": "NO"},
        ],
        failing_tests=[
            vanta_doc()["failing_tests"][0],
            {
                "id": "tst-0002",
                "name": "Backups run every night",
                "status": "NEEDS_ATTENTION",
                "due": None,
                "link": "",
                "controls": [],
            },
        ],
    )
    code, payload, err = collect(instance, "compliance", doc)
    assert code == 0, (payload, err)
    assert _fact(instance, "vendor.vanta.controls_total")[1] == 2
    assert _fact(instance, "vendor.vanta.controls_complete")[1] == 1
    assert _fact(instance, "vendor.vanta.documents_total")[1] == 2
    assert _fact(instance, "vendor.vanta.documents_ok")[1] == 1
    assert _fact(instance, "vendor.vanta.failing_tests")[1] == 2


def test_vanta_marks_linked_checklist_items_done_and_reopens_regressions(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "compliance")
    _checklist(instance, "chk-ac-1", "vanta:AC-1", "in_progress")
    assert collect(instance, "compliance", vanta_doc())[0] == 0
    assert _item(instance, "chk-ac-1") == ("done", "observed", 2)
    regressed = vanta_doc(
        fetched_at="2026-10-08T00:05:00Z",
        controls=[{"id": "ctl-0001", "external_id": "AC-1", "complete": False}],
    )
    assert collect(instance, "compliance", regressed)[0] == 0
    assert _item(instance, "chk-ac-1") == ("in_progress", "observed", 3)


def test_vanta_never_creates_or_touches_unlinked_items(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "compliance")
    _checklist(instance, "chk-other-project", "vanta:AC-1", "in_progress", project="general")
    _checklist(instance, "chk-plain", "local:AC-1", "in_progress")
    _checklist(instance, "chk-na", "vanta:AC-1", "not_applicable")
    before = ro_rows(instance, "SELECT count(*) FROM checklist_items")[0][0]
    versions = {
        item_id: _item(instance, item_id)[2]
        for item_id in ("chk-other-project", "chk-plain", "chk-na")
    }
    assert collect(instance, "compliance", vanta_doc())[0] == 0
    assert ro_rows(instance, "SELECT count(*) FROM checklist_items")[0][0] == before
    for item_id, version in versions.items():
        assert _item(instance, item_id)[2] == version


def test_vanta_documents_drop_owner_fields(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path, "compliance")
    control = vanta_doc()
    control["controls"][0]["owner"] = "Alex Example"
    code, payload, _err = collect(instance, "compliance", control)
    assert code == 1
    assert payload["status"] == "error"
    assert "is not allowed" in _run_error(instance)
    failing = vanta_doc(fetched_at="2026-10-08T01:00:00Z")
    failing["failing_tests"][0]["owner"] = "Alex Example"
    code, payload, _err = collect(instance, "compliance", failing)
    assert code == 1
    assert "is not allowed" in _run_error(instance)


def test_collector_fact_entries_quote_cleanly(tmp_path: Path) -> None:
    """Test 47: a fact's quote is its evidence entry
    {"fact", "value"} as a contiguous run of the batch source, so the entry holds fact, value and only
    keys that sort after value, and no collector type declares a number or text fact."""
    import importlib

    declared = 0
    for name, ctype in REGISTRY.items():
        module = importlib.import_module(ctype.plan.__module__)
        for suffix, value_type, _allowed in getattr(module, "FACTS", ()):
            declared += 1
            assert value_type not in ("number", "text"), (name, suffix, value_type)
    assert declared == 8  # GitHub's three and Vanta's five
    instance = collector_instance(tmp_path, "repo", "compliance")
    for collector, doc in (("repo", github_doc()), ("compliance", vanta_doc())):
        code, payload, err = collect(instance, collector, doc)
        assert code == 0, (payload, err)
    rows = ro_rows(instance, "SELECT content FROM sources WHERE kind = 'api' ORDER BY id")
    entries = [item for (content,) in rows for item in json.loads(content)["items"] if "fact" in item]
    assert {entry["fact"] for entry in entries} == {
        "ci_state", "last_push_on", "open_pull_requests",
        "controls_complete", "controls_total", "documents_ok", "documents_total", "failing_tests",
    }
    for entry in entries:
        keys = list(entry)
        assert keys[:2] == ["fact", "value"], entry
        assert all(key > "value" for key in keys[2:]), entry
    assert any("value_names" in entry for entry in entries)
