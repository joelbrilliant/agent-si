from __future__ import annotations

import copy
import json
import random
import re
import subprocess
from pathlib import Path

import pytest

from brain import advice, ingress, schemas, validate
from brain.applier import Applier, NotAllowed, Outcome, Unavailable, Unreadable
from brain.init import init_instance
from tests.conftest import make_packet, render_config, ro_rows, source_op

AGENTS = {
    "tester": {"operations": "*", "author_principal": True},
    "alpha": {"operations": "*"},
    "beta": {"operations": "*"},
}

NESTING = "packet nests deeper than 64 levels"
AGENT = "agent is missing or invalid"
PREFIX = "change_id must start with the agent name and a hyphen"
INTENT = "intent is missing or longer than 500 characters"


@pytest.fixture
def inst(tmp_path: Path) -> Path:
    result = init_instance("demo", tmp_path, config_text=render_config(agents=AGENTS))
    return Path(result["instance"])


@pytest.fixture
def writer(inst: Path):
    applier = Applier(inst)
    try:
        yield applier
    finally:
        applier.close()


def _log(inst: Path) -> list[tuple]:
    return ro_rows(inst, "SELECT change_id, status FROM write_log ORDER BY rowid")


def _good(agent="alpha", **changes) -> dict:
    named = agent if isinstance(agent, str) and re.fullmatch(schemas.AGENT, agent) else "alpha"
    packet = make_packet(named, source_op(f"src-{named}-p061", "Patch envelope source text."))
    packet["agent"] = agent
    packet.update(changes)
    return packet


def _without(packet: dict, key: str) -> dict:
    packet = dict(packet)
    packet.pop(key)
    return packet


def _nested(depth: int) -> str:
    """A packet whose bracket depth is exactly `depth` (the object itself counts as 1)."""
    packet = _good()
    inner = "[" * (depth - 1) + "]" * (depth - 1)
    body = json.dumps(packet)
    return body[:-1] + ', "deep": ' + inner + "}"


def _bad_envelopes() -> list[tuple[str, bytes, str]]:
    long_intent = _good(intent="x" * 501)
    return [
        ("D1 no intent", json.dumps(_without(_good(), "intent")).encode(), INTENT),
        ("D1 intent not a string", json.dumps(_good(intent=7)).encode(), INTENT),
        ("D2 long intent", json.dumps(long_intent).encode(), INTENT),
        ("D3 no agent", json.dumps(_without(_good(), "agent")).encode(), AGENT),
        ("D3 integer agent", json.dumps(_good(agent=5)).encode(), AGENT),
        ("D3 bad agent name", json.dumps(_good(agent="Not-A-Name")).encode(), AGENT),
        ("D4 deep nesting", _nested(65).encode(), NESTING),
        ("D5 prefix mismatch", json.dumps(_good(change_id="beta-x1")).encode(), PREFIX),
    ]


# -- 1. intent --------------------------------------------------------------------------------


def test_missing_or_bad_intent_is_unreadable(inst: Path, writer: Applier) -> None:
    before = _log(inst)
    for packet in (_without(_good(), "intent"), _good(intent="y" * 501), _good(intent=None)):
        with pytest.raises(Unreadable) as exc:
            writer.apply(json.dumps(packet).encode())
        assert str(exc.value) == INTENT
    assert _log(inst) == before
    # The boundary: 500 characters and an empty intent are both readable.
    for intent in ("z" * 500, ""):
        outcome = writer.apply(json.dumps(_good(intent=intent, change_id=f"alpha-i{len(intent)}")).encode())
        assert isinstance(outcome, Outcome)
        assert outcome.receipt["status"] in {"applied", "rejected"}


# -- 2. agent ---------------------------------------------------------------------------------


def test_missing_or_bad_agent_is_unreadable(inst: Path, writer: Applier) -> None:
    before = _log(inst)
    for packet in (
        _without(_good(), "agent"),
        _good(agent=12),
        _good(agent="Not-A-Name"),
        _good(agent="alpha\n"),
        _good(agent=["alpha"]),
    ):
        with pytest.raises(Unreadable) as exc:
            writer.apply(json.dumps(packet).encode())
        assert str(exc.value) == AGENT
    assert _log(inst) == before


# -- 3. nesting -------------------------------------------------------------------------------


def test_deep_nesting_is_unreadable(inst: Path, writer: Applier) -> None:
    before = _log(inst)
    with pytest.raises(Unreadable) as exc:
        writer.apply(_nested(65).encode())
    assert str(exc.value) == NESTING
    # A million levels, past every parser limit on every supported Python.
    with pytest.raises(Unreadable) as exc:
        writer.apply(_nested(1_000_000).encode())
    assert str(exc.value) == NESTING
    with pytest.raises(Unreadable) as exc:
        writer.apply(("[" * 1_000_000).encode())
    assert str(exc.value) == NESTING
    assert _log(inst) == before
    # 64 levels parses and gets its normal verdict: the extra field is not allowed.
    outcome = writer.apply(_nested(64).encode())
    assert outcome.receipt["status"] == "rejected"
    assert "/deep is not allowed" in outcome.receipt["error"]
    # Brackets inside strings, escaped quotes included, don't count.
    packet = _good(intent='\\"' + "[" * 200 + '\\" {{{')
    packet["change_id"] = "alpha-strings"
    packet["ops"][0]["row"]["title"] = '"' + "{[" * 40 + '\\'
    outcome = writer.apply(json.dumps(packet).encode())
    assert outcome.receipt["status"] == "applied", outcome.receipt["error"]


def test_nesting_scanner_handles_escapes() -> None:
    assert ingress.nesting_depth('"\\\\" [[') == 2
    assert ingress.nesting_depth('"\\"[[[" [') == 1
    assert ingress.nesting_depth('{"a": "]]]]", "b": [[]]}') == 3
    assert ingress.nesting_depth("") == 0


# -- 4. change_id prefix ----------------------------------------------------------------------


def test_change_id_prefix_mismatch_is_unreadable_and_not_squatted(inst: Path, writer: Applier) -> None:
    before = _log(inst)
    with pytest.raises(Unreadable) as exc:
        writer.apply(json.dumps(_good("alpha", change_id="beta-x1")).encode())
    assert str(exc.value) == PREFIX
    assert _log(inst) == before
    outcome = writer.apply(json.dumps(_good("beta", change_id="beta-x1")).encode())
    assert outcome.duplicate is False
    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert outcome.receipt["agent"] == "beta"


# -- 5. brain validate ------------------------------------------------------------------------


def test_validate_matches_parse(inst: Path, writer: Applier) -> None:
    for label, raw, message in _bad_envelopes():
        with pytest.raises(Unreadable) as exc:
            writer.apply(raw)
        assert str(exc.value) == message, label
        code, payload = validate.validate_command("packet", raw)
        assert code == 1, label
        assert payload["status"] == "held", label
        assert payload["errors"][0]["message"] == message, label
    # Through the CLI as well, for the deepest case.
    result = subprocess.run(
        ["brain", "validate", "packet", "-"],
        input=_nested(1_000_000),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["errors"][0]["message"] == NESTING
    # A good envelope is still valid.
    code, payload = validate.validate_command("packet", json.dumps(_good()).encode())
    assert (code, payload["status"]) == (0, "valid"), payload


# -- 6. never raises after _parse -------------------------------------------------------------

_ODD = [
    "",
    " ",
    "\u0000",
    "​",
    "﻿",
    "é",
    "\U0001f600" * 3,
    "‮" + "alpha",
    "ａｌｐｈａ",
    "x" * 100_000,
]


def _values(rng: random.Random) -> list:
    return [
        None,
        True,
        0,
        -1,
        1,
        2**63,
        10**300,
        1.5,
        1e308,
        float("inf"),
        -float("inf"),
        [],
        {},
        [None],
        {"op": None},
        [{"op": []}],
        [{"op": {}}],
        [{"op": ["add_source"], "row": {}}],
        [{"op": "add_source"}],
        [{"op": "add_source", "row": None}],
        [{"op": "add_source", "row": {"id": rng.choice(_ODD)}}],
        [1, "two", [3]],
        *_ODD,
    ]


def _envelope_pass(writer: Applier) -> int:
    rng = random.Random(61)
    fields = ("packet_version", "agent", "intent", "ops")
    parsed = 0
    for i in range(600):
        packet = _good()
        packet["change_id"] = f"alpha-fz{i}"
        for name in rng.sample(fields, rng.randint(1, len(fields))):
            if rng.random() < 0.15:
                packet.pop(name, None)
            else:
                packet[name] = rng.choice(_values(rng))
        if rng.random() < 0.3 and isinstance(packet.get("agent"), str):
            packet["change_id"] = f"{packet['agent']}-fz{i}" if packet["agent"] else packet["change_id"]
        raw = json.dumps(packet, ensure_ascii=rng.random() < 0.5).encode("utf-8", "surrogatepass")
        try:
            outcome = writer.apply(raw)
        except Unreadable:
            continue
        parsed += 1
        assert isinstance(outcome, Outcome), packet
        assert outcome.exit_code in {0, 1, 3, 4}, (packet, outcome)
    return parsed


# Valid ops of every kind, each with the agent and applier mode that may send it, for an
# extract_instance store seeded by _seed_ops below.
_V = "2026-10-06T00:00:00Z"
_SRC = {"source_id": "src-seed", "locator": "seed"}


def _up(op: str, row: dict, base=None) -> dict:
    return {"op": op, "row": row, "source": dict(_SRC), "base": base}


_RESOLVE_FZ = {"op": "resolve_fact", "row": {"key": "demo.motto", "scope": "", "keep": ["clm-fz"], "reject": [], "quote": "the motto is Seed rows for the extraction tests"}, "source": {"source_id": "src-fz-person", "locator": "fuzz"}}


def _base_ops() -> list[tuple[str, str, dict]]:
    from tests.extract_fixtures import candidate, thread_id

    return [
        ("tester", "plain", source_op("src-seed", "Seed rows for the extraction tests.", date="2026-10-06")),
        ("tester", "plain", source_op("src-fz-new", "A fresh fuzz source with some words. We have 5 seats.")),
        # Claims, resolution, a source version and aliases.
        ("tester", "plain", {"op": "add_claim", "row": {"id": "clm-fz", "key": "demo.motto", "value_raw": "Seed rows", "basis": "reported", "quote": "Seed rows for the extraction tests."}, "source": {"source_id": "src-seed", "locator": "fuzz"}}),
        ("tester", "plain", source_op("src-fz-person", "Sam says the motto is Seed rows for the extraction tests.", author="per-sam-example")),
        ("tester", "plain", _RESOLVE_FZ),
        ("asker", "plain", _RESOLVE_FZ),
        ("tester", "plain", {"op": "add_source", "row": {"id": "src-fz-v2", "kind": "user_note", "title": "Synthetic source", "source_date": "2026-10-01", "author_person_id": None, "content": "A fresh fuzz source with some words, second version.", "supersedes_id": "src-fz-new"}}),
        ("brain", "reserved", {"op": "set_aliases", "row": {"aliases": [{"kind": "attribute", "alias": "motto words", "target": "motto"}]}, "source": {"source_id": "src-seed", "locator": "fuzz"}}),
        ("tester", "plain", _up("upsert_person", {"id": "per-fz-new", "display_name": "Fuzz Person", "aliases": ["Fuzzy"]})),
        ("tester", "plain", _up("upsert_system", {"id": "sys-fz", "name": "Fuzz system", "kind": "saas", "vendor": "Example"})),
        ("tester", "plain", _up("upsert_project", {"id": "fzproj", "kind": "project", "title": "Seed rows", "status": "active", "sort_order": 5})),
        ("brain", "reserved", _up("upsert_fact_key", {"key": "demo.fuzz", "value_type": "int", "stale_after_days": 30})),
        # The strict fact quotes its source, and the fuzz mutates quote and value_raw.
        ("tester", "plain", {"op": "upsert_fact", "row": {"key": "demo.seats", "value": 5, "status": "current", "basis": "observed", "as_of": "2026-10-06", "last_verified_at": _V, "quote": "We have 5 seats", "value_raw": "5"}, "source": {"source_id": "src-fz-new", "locator": "fuzz"}, "base": None}),
        # A legacy import (unchecked, with a superseded_by link) and its verify_claim.
        ("tester", "plain", source_op("src-fz-legacy", "Legacy import notes. The launch moved to 2026-11-02 now. It was 2026-11-01 before.")),
        ("importer", "plain", {"op": "add_claim", "row": {"id": "clm-fz-legacy", "key": "demo.launch", "value_raw": "2026-11-02", "basis": "reported", "status": "live"}, "source": {"source_id": "src-fz-legacy", "locator": "fuzz"}}),
        ("importer", "plain", {"op": "add_claim", "row": {"id": "clm-fz-legacy-old", "key": "demo.launch", "value_raw": "2026-11-01", "basis": "reported", "status": "superseded", "superseded_by": "clm-fz-legacy"}, "source": {"source_id": "src-fz-legacy", "locator": "fuzz 2"}}),
        ("tester", "plain", {"op": "verify_claim", "row": {"claim_id": "clm-fz-legacy", "quote": "The launch moved to 2026-11-02 now"}, "source": {"source_id": "src-fz-legacy", "locator": "fuzz"}}),
        ("tester", "plain", _up("upsert_record", {"id": "rec-fz", "kind": "note", "title": "Seed rows", "body": "Seed rows for the extraction tests.", "project_id": "general"})),
        ("tester", "plain", _up("upsert_decision", {"id": "dec-fz", "title": "Seed rows", "quote": "Seed rows for the extraction tests.", "decided_on": "2026-10-06", "decided_by_person_id": "per-sam-example", "status": "in_effect", "project_id": "general"})),
        ("tester", "plain", _up("upsert_action", {"id": "act-fz", "title": "Seed rows", "status": "open", "status_basis": "observed", "project_id": "general", "owner_person_ids": ["per-sam-example"], "due_date": "2026-10-06", "due_kind": "deadline", "last_verified_at": _V}, base=1)),
        ("tester", "plain", _up("upsert_checklist_item", {"id": "chk-fz", "project_id": "general", "title": "Seed rows", "state": "not_started", "state_basis": "observed", "sort_order": 3, "last_verified_at": _V}, base=1)),
        ("tester", "plain", _up("upsert_thread", {"id": thread_id("thread-fz"), "channel": "email", "account": "work", "external_id": "thread-fz", "subject": "Fuzz", "outbound_seen": False})),
        ("tester", "plain", _up("upsert_thread_link", {"id": "lnk-fz", "action_id": "act-fz", "thread_id": thread_id("thread-plan"), "role": "awaiting_reply", "since": _V})),
        ("tester", "plain", _up("upsert_signal", {"id": "sig-fz", "kind": "other", "subject_table": "actions", "subject_id": "act-fz", "basis": "observed", "detail": ""})),
        ("tester", "plain", _up("upsert_meeting", {"id": "mtg-fz", "thread_id": "thr-fz-cal", "title": "Fuzz meeting", "starts_at": _V, "ends_at": "2026-10-06T01:00:00Z", "status": "confirmed", "attendee_count": 3})),
        ("tester", "plain", {"op": "record_collector_run", "row": {"id": "run-fz", "collector": "mail", "started_at": _V, "finished_at": _V, "status": "ok", "items": 3, "input_sha256": "", "error": ""}}),
        ("brain", "reserved", {"op": "rebuild_search"}),
        ("scan", "extract", {"op": "apply_candidate", "candidate": candidate("action_candidate")}),
        ("scan", "extract", {"op": "apply_candidate", "candidate": candidate("decision_candidate")}),
    ]


_SEED_OPS = [
    _up("upsert_thread", {"id": "thr-fz-cal", "channel": "calendar", "account": "work", "external_id": "cal-fz", "subject": "Fuzz meeting"}),
    _up("upsert_action", {"id": "act-fz", "title": "Seed rows", "status": "open", "status_basis": "observed", "project_id": "general", "last_verified_at": _V}),
    _up("upsert_checklist_item", {"id": "chk-fz", "project_id": "general", "title": "Seed rows", "state": "not_started", "state_basis": "observed", "last_verified_at": _V}),
]

_OP_VALUES = [
    None,
    True,
    False,
    0,
    -1,
    1,
    7,
    2**63 - 1,
    -(2**63),
    2**63,
    -(2**63) - 1,
    10**30,
    -(10**30),
    1.5,
    -0.0,
    1e308,
    [],
    [1],
    ["a", "b"],
    [None],
    [[]],
    {},
    {"a": 1},
    {"source_id": "src-seed"},
    "add_source",
    "upsert_action",
    "src-seed",
    "per-sam-example",
    "general",
    "2026-10-06",
    "2026-02-30",
    "2026-10-06T00:00:00Z",
    "9999-12-31T23:59:59Z",
    "x" * 5000,
    "word " * 600,
    "line\nbreak",
    *_ODD[:-1],
]


_EDGE_INTS = [2**63, -(2**63) - 1, 2**63 - 1, -(2**63), 10**30, 2**64]


def _paths(value, prefix=()) -> list[tuple]:
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            found.append(prefix + (key,))
            found.extend(_paths(child, prefix + (key,)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.append(prefix + (index,))
            found.extend(_paths(child, prefix + (index,)))
    return found


def _mutate(op: dict, rng: random.Random) -> dict:
    op = json.loads(json.dumps(op))
    for _ in range(rng.randint(1, 3)):
        paths = _paths(op)
        roll = rng.random()
        name = op.get("op")
        spec = schemas.OPS.get(name) if isinstance(name, str) else None
        if roll < 0.15 and spec and isinstance(op.get("row"), dict):
            # Push an integer field (or a fact's value, or base) to the edge of 64 bits and past it.
            props = spec["row"]["properties"]
            numeric = [field for field, schema in props.items() if "integer" in str(schema.get("type"))]
            target = rng.choice([*numeric, "base"])
            holder = op if target == "base" and "base" in op else op["row"]
            holder[target] = rng.choice(_EDGE_INTS)
            continue
        if roll < 0.25:
            # Add a field the schema knows about but the op doesn't carry.
            row = op.get("row")
            if spec and isinstance(row, dict):
                row[rng.choice(sorted(spec["row"]["properties"]))] = copy.deepcopy(rng.choice(_OP_VALUES))
            continue
        if not paths:
            break
        path = rng.choice(paths)
        parent = op
        for part in path[:-1]:
            parent = parent[part]
        if roll < 0.4:
            parent.pop(path[-1])
        else:
            parent[path[-1]] = copy.deepcopy(rng.choice(_OP_VALUES))
    return op


def _rebased(inst: Path, op: dict) -> dict:
    """The op with base set to the stored row's version, as an agent that re-read would send,
    so a mutated upsert reaches the write rather than stopping at a version conflict."""
    spec = schemas.OPS.get(op["op"])
    if spec is None or spec["kind"] != "upsert":
        return op
    rows = ro_rows(
        inst, f"SELECT version FROM {spec['table']} WHERE {spec['key']} = ?", (op["row"][spec["key"]],)
    )
    return {**op, "base": rows[0][0] if rows else None}


def _ops_pass(tmp_path: Path, cases: int) -> tuple[int, int]:
    from tests.extract_fixtures import SCAN, extract_instance

    inst = extract_instance(
        tmp_path / "ops",
        agents={
            "asker": {"kind": "agent", "operations": "*", "approvers": ["tester"]},
            # A legacy agent, so the fuzz reaches the legacy claim fields.
            "importer": {"kind": "collector", "manual_sources": True, "legacy_import": True, "operations": "*"},
        },
    )
    writers = {
        "plain": Applier(inst),
        "reserved": Applier(inst, allow_reserved_agent=True),
        "extract": Applier(inst, extract=True),
    }
    try:
        seeded = writers["plain"].apply(json.dumps(make_packet("tester", *_SEED_OPS)).encode())
        assert seeded.receipt["status"] == "applied", seeded.receipt
        bases = _base_ops()
        statuses = []
        for agent, mode, op in bases:  # every base op is readable and decided
            outcome = writers[mode].apply(json.dumps(make_packet(agent, op)).encode())
            if outcome.receipt["status"] == "held":
                outcome = writers["plain"].approve(outcome.receipt["change_id"], "tester")
            assert outcome.exit_code in {0, 1, 3, 4}, (op, outcome.receipt)
            statuses.append((op.get("op"), outcome.receipt["status"], outcome.receipt["error"]))
        assert all(status == "applied" for _op, status, _error in statuses), statuses
        rng = random.Random(611)
        parsed = 0
        failures: list[str] = []
        approved: list[str] = []
        fact_op = next(op for _agent, _mode, op in bases if op.get("op") == "upsert_fact")
        bare = json.loads(json.dumps(fact_op))
        for name in ("value", "last_verified_at"):
            bare["row"].pop(name)
        forced = [("tester", "plain", bare)]  # Both value and last_verified_at popped
        for i in range(cases + len(forced)):
            agent, mode, op = forced[i] if i < len(forced) else bases[(i - len(forced)) % len(bases)]
            ops = [_mutate(_rebased(inst, op), rng) if i >= len(forced) else _rebased(inst, op)]
            if rng.random() < 0.2:
                ops.append(_mutate(_rebased(inst, rng.choice([b for b in bases if b[1] == mode])[2]), rng))
            packet = make_packet(agent, *ops, change_id=f"{agent}-ops{i}")
            raw = json.dumps(packet, ensure_ascii=rng.random() < 0.5).encode()
            try:
                outcome = writers[mode].apply(raw)
            except Unreadable:
                continue
            except Exception as exc:  # the property: nothing else may escape
                failures.append(f"{type(exc).__name__}: {exc} <- {json.dumps(ops)[:300]}")
                continue
            parsed += 1
            assert isinstance(outcome, Outcome)
            if outcome.receipt["status"] == "held":
                # An extractor's packet is held; approving it runs the ops.
                try:
                    outcome = writers["plain"].approve(packet["change_id"], "tester")
                except NotAllowed:
                    continue
                except Exception as exc:
                    failures.append(f"approve {type(exc).__name__}: {exc} <- {json.dumps(ops)[:300]}")
                    continue
                approved.append(outcome.receipt["status"])
            if outcome.exit_code not in {0, 1, 3, 4}:
                failures.append(f"exit {outcome.exit_code}: {outcome.integrity} <- {json.dumps(ops)[:300]}")
        assert not failures, f"{len(failures)} of {cases} raised; first: " + " | ".join(failures[:5])
        assert approved, "no held packet was approved"
        return cases, parsed
    finally:
        for writer in writers.values():
            writer.close()


def test_apply_never_raises_after_parse(inst: Path, writer: Applier, tmp_path: Path) -> None:
    assert _envelope_pass(writer) > 50
    cases, parsed = _ops_pass(tmp_path, 3_300)
    assert cases >= 3_000
    assert parsed > 2_000, parsed


# -- 8. amendments: op type, existing sources, number range, validate dedupe -----------------

RANGE = "packet holds a number outside the supported range"
RANGE_VALIDATE = "input holds a number outside the supported range"


def test_non_string_op_is_rejected_not_raised(inst: Path, writer: Applier) -> None:
    for i, value in enumerate((["x"], {"a": 1}, 5, None, True)):
        message = "op is required" if value is None else "op must be a string"
        packet = make_packet("alpha", {"op": value, "row": {}}, change_id=f"alpha-nonstr{i}")
        outcome = writer.apply(json.dumps(packet).encode())
        assert outcome.exit_code == 1, outcome.receipt
        assert outcome.receipt["status"] == "rejected"
        assert f"/ops/0/op {message}" in outcome.receipt["error"], outcome.receipt["error"]
        assert ("alpha-nonstr%d" % i, "rejected") in _log(inst)
        code, payload = validate.validate_command("packet", json.dumps(packet).encode())
        assert code == 1 and payload["status"] == "held"
        assert {"path": "/ops/0/op", "message": message} in payload["errors"]
    missing = make_packet("alpha", {"row": {}}, change_id="alpha-noop")
    outcome = writer.apply(json.dumps(missing).encode())
    assert "/ops/0/op op is required" in outcome.receipt["error"]


def test_missing_op_says_required(tmp_path) -> None:
    from brain import validate
    packet = {"packet_version": 1, "change_id": "tester-op-missing", "agent": "tester", "intent": "t",
              "ops": [{"row": {}}, {"op": None, "row": {}}, {"op": 5, "row": {}}]}
    errors = validate.validate_packet(packet)
    by_path = {e["path"]: e["message"] for e in errors if e["path"].endswith("/op")}
    assert by_path["/ops/0/op"] == "op is required"
    assert by_path["/ops/1/op"] == "op is required"
    assert by_path["/ops/2/op"] == "op must be a string"


def test_add_source_on_existing_id_needs_every_field(inst: Path, writer: Applier) -> None:
    full = source_op("src-exists", "An existing source for the patch.")
    assert writer.apply(json.dumps(make_packet("alpha", full)).encode()).receipt["status"] == "applied"
    for field in ("kind", "title", "source_date", "content"):
        op = json.loads(json.dumps(full))
        op["row"].pop(field)
        outcome = writer.apply(json.dumps(make_packet("alpha", op)).encode())
        assert outcome.exit_code == 1, outcome.receipt
        enums = {field: schemas.OPS["add_source"]["row"]["properties"][field].get("enum")}
        expected = advice.missing_fields_error([field], {k: v for k, v in enums.items() if v}, to_create=False)
        assert outcome.receipt["error"] == f"op 0 (add_source): {expected}"
    # The identical row is still accepted as a no-op, and a changed one still refused.
    assert writer.apply(json.dumps(make_packet("alpha", full)).encode()).receipt["status"] == "applied"
    changed = json.loads(json.dumps(full))
    changed["row"]["content"] = "Different content."
    assert "sources never change" in writer.apply(json.dumps(make_packet("alpha", changed)).encode()).receipt["error"]


def _with_number(text: str, change_id: str) -> bytes:
    packet = _good(change_id=change_id)
    packet["ops"].append(
        {"op": "upsert_project", "base": None, "source": {"source_id": "src-alpha-p061", "locator": "x"},
         "row": {"id": f"p{change_id[-6:]}", "kind": "project", "title": "Patch envelope source", "sort_order": "@N@"}}
    )
    return json.dumps(packet).replace('"@N@"', text).encode()


def test_out_of_range_numbers_are_unreadable(inst: Path, writer: Applier) -> None:
    before = _log(inst)
    for i, text in enumerate(
        ("9223372036854775808", "-9223372036854775809", "1" + "0" * 5000, "1e400", "-1E400")
    ):
        raw = _with_number(text, f"alpha-big{i:03d}")
        with pytest.raises(Unreadable) as exc:
            writer.apply(raw)
        assert str(exc.value) == RANGE, text
        code, payload = validate.validate_command("packet", raw)
        assert code == 1 and payload["errors"] == [{"path": "", "message": RANGE_VALIDATE}], text
    assert _log(inst) == before
    # The ends of the 64-bit range still apply.
    for i, text in enumerate(("9223372036854775807", "-9223372036854775808")):
        outcome = writer.apply(_with_number(text, f"alpha-edge{i:02d}"))
        assert outcome.receipt["status"] == "applied", outcome.receipt
    # And the CLI exits 2 with the message, not a traceback.
    result = subprocess.run(
        ["brain", "apply", "-", "--instance", str(inst)],
        input=_with_number("9223372036854775808", "alpha-bigcli"),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stderr.decode().strip() == f"brain: {RANGE}"


def test_validate_lists_each_problem_once() -> None:
    for packet, path, message in (
        (_good(change_id="beta-x1"), "/change_id", PREFIX),
        (_good(intent="y" * 501), "/intent", INTENT),
        (_without(_good(), "intent"), "/intent", INTENT),
        (_good(agent="Not-A-Name"), "/agent", AGENT),
    ):
        _code, payload = validate.validate_command("packet", json.dumps(packet).encode())
        at_path = [item for item in payload["errors"] if item["path"] == path]
        assert at_path == [{"path": path, "message": message}], payload["errors"]
        assert payload["errors"][0] == {"path": path, "message": message}


# -- 7. CLI exit codes ------------------------------------------------------------------------


def test_cli_exit_codes(inst: Path) -> None:
    before = _log(inst)
    for label, raw, message in _bad_envelopes() + [
        ("D4 a million levels", _nested(1_000_000).encode(), NESTING),
    ]:
        result = subprocess.run(
            ["brain", "apply", "-", "--instance", str(inst)],
            input=raw,
            capture_output=True,
            timeout=60,
        )
        stderr = result.stderr.decode()
        assert result.returncode == 2, (label, result.returncode, stderr)
        assert stderr.strip() == f"brain: {message}", label
        assert "Traceback" not in stderr, label
    assert _log(inst) == before
