from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from brain import advice
from brain import candidates
from brain import schemas
from brain import store
from brain.applier import Applier
from brain.collectors.base import stable_id
from brain.config import load_config
from brain.evidence_gate import match_key
from brain.validate import report, validate_candidate
from tests.conftest import file_sha, make_packet, upsert_op
from tests.extract_fixtures import CANDIDATE_SAMPLES, candidate, extract_instance


def _brain_validate(tmp_path: Path, doc: dict) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "candidate.json"
    path.write_text(store.canonical_json(doc), encoding="utf-8")
    return subprocess.run(
        ["brain", "validate", "candidate", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_kind_fields_are_closed_and_required() -> None:
    covered = []
    for kind in CANDIDATE_SAMPLES:
        covered.append(kind)
        extra = candidate(kind)
        extra["fields"]["extra"] = "nope"
        properties = schemas.CANDIDATE_KINDS[kind]["properties"]
        assert validate_candidate(extra) == [
            {"path": "/fields/extra", "message": advice.unknown_property_error("extra", properties)}
        ]
        for name in schemas.CANDIDATE_KINDS[kind]["required"]:
            doc = candidate(kind)
            del doc["fields"][name]
            assert validate_candidate(doc) == [
                {"path": f"/fields/{name}", "message": advice.required_error(properties[name].get("enum"))}
            ]
    assert covered == list(schemas.CANDIDATE_KINDS)


def _set_field(name: str, value):
    def change(doc: dict) -> None:
        doc["fields"][name] = value

    return change


def _drop_field(name: str):
    def change(doc: dict) -> None:
        del doc["fields"][name]

    return change


def _set_thread_role(value: str):
    def change(doc: dict) -> None:
        doc["fields"]["thread"]["role"] = value

    return change


def _set_root(name: str, value):
    def change(doc: dict) -> None:
        doc[name] = value

    return change


def _rule(kind: str, *names: str) -> dict:
    """The schema node of a candidate field, so a test builds the expected message from the same limits."""
    node = schemas.CANDIDATE_KINDS[f"{kind}_candidate"]
    for name in names:
        node = node["properties"][name]
    return node


_STRUCTURAL = [
    ("decision", [_set_field("decided_on", "2026-02-30")], "/fields/decided_on", advice.date_error()),
    (
        "decision",
        [_set_field("decided_on", "tomorrow")],
        "/fields/decided_on",
        "does not match the required pattern",
    ),
    ("decision", [_set_field("status", "final")], "/fields/status", advice.enum_error(_rule("decision", "status")["enum"])),
    (
        "decision",
        [_set_field("title", "x" * 91)],
        "/fields/title",
        advice.size_error("characters", "maximum", _rule("decision", "title")["max_length"]),
    ),
    ("decision", [_set_field("title", "Two\nlines")], "/fields/title", advice.one_line_error()),
    ("decision", [_set_field("title", "Two\u2028lines")], "/fields/title", advice.one_line_error()),
    (
        "decision",
        [_set_field("workstream", "Ops")],
        "/fields/workstream",
        "does not match the required pattern",
    ),
    (
        "action",
        [_set_field("status", "done"), _set_field("status", "dropped")],
        "/fields/status",
        advice.enum_error(_rule("action", "status")["enum"]),
    ),
    (
        "action",
        [_set_field("status_basis", "observed")],
        "/fields/status_basis",
        advice.enum_error(_rule("action", "status_basis")["enum"]),
    ),
    (
        "action",
        [_set_field("due_date", "yesterday")],
        "/fields/due_date",
        "does not match the required pattern",
    ),
    (
        "action",
        [_drop_field("due_kind"), _set_field("due_date", None)],
        "/fields/due_kind",
        "must be set exactly when due_date is",
    ),
    ("action", [_set_field("asked_on", "2026-13-01")], "/fields/asked_on", advice.date_error()),
    (
        "action",
        [_set_thread_role("cc")],
        "/fields/thread/role",
        advice.enum_error(_rule("action", "thread", "role")["enum"]),
    ),
    # Claim candidates are version 2.
    (
        "claim",
        [_set_field("key", "ab")],
        "/fields/key",
        advice.size_error("characters", "minimum", _rule("claim", "key")["min_length"]),
    ),
    (
        "claim",
        [_set_field("scope", "now!")],
        "/fields/scope",
        "does not match the required pattern",
    ),
    (
        "claim",
        [_set_field("value_raw", "")],
        "/fields/value_raw",
        advice.size_error("characters", "minimum", _rule("claim", "value_raw")["min_length"]),
    ),
    ("claim", [_set_field("supersedes", ["clm-a", "clm-a"])], "/fields/supersedes", "has duplicate items"),
    ("any", [_set_root("quote", "four words only here")], "/quote", "needs at least 5 words"),
    # Each kind has its own version.
    ("decision", [_set_root("candidate_version", 2)], "/candidate_version", "must be 1"),
    ("action", [_set_root("candidate_version", 2)], "/candidate_version", "must be 1"),
    ("claim", [_set_root("candidate_version", 1)], "/candidate_version", "must be 2"),
    (
        "any",
        [_set_root("kind", "meeting_candidate")],
        "/kind",
        "unknown candidate kind meeting_candidate",
    ),
]


@pytest.mark.parametrize(("sample", "changes", "path", "message"), _STRUCTURAL)
def test_structural_checks_hold_bad_candidates(tmp_path: Path, sample, changes, path, message) -> None:
    kinds = list(schemas.CANDIDATE_KINDS) if sample == "any" else [f"{sample}_candidate"]
    if sample == "any":
        assert kinds == list(CANDIDATE_SAMPLES)
    covered = []
    for kind in kinds:
        covered.append(kind)
        for change in changes:
            doc = candidate(kind)
            change(doc)
            errors = validate_candidate(doc)
            assert errors == [{"path": path, "message": message}]
            result = _brain_validate(tmp_path, doc)
            assert result.returncode == 1, result.stderr
            assert json.loads(result.stdout) == report("candidate", errors)
    if sample == "any":
        assert covered == list(schemas.CANDIDATE_KINDS)


DECIDED_AT = "2026-10-08T00:00:00Z"
DECISION_ID = "dec-63dfe78e9fcafa59f83ad34d"
ACTION_ID = "act-1d808128fe7566fec33b3dad"
LINK_ID = "lnk-4fb1d41307273e5aec24840d"
PLAN_THREAD = "thr-email-8e3d3b6316b2c9cbd01eb768"
INBOUND_THREAD = "thr-email-8eae062559450ce15bd6122e"
EMPTY_THREAD = "thr-email-fbef70cdf3f435e48acf4f4a"
GENERAL_DECISION = "dec-general-seed"


def _plan(instance: Path, doc: dict, decided_at: str = DECIDED_AT):
    con = store.connect_reader(instance / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        return candidates.plan(con, load_config(instance / "brain.toml"), doc, decided_at)
    finally:
        con.close()


def _apply(instance: Path, *ops, change_id: str) -> None:
    packet = make_packet("tester", *ops, change_id=change_id)
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(packet).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt


def _seed_decision(instance: Path, *, row_id: str, title: str, project_id: str, change_id: str) -> None:
    _apply(
        instance,
        upsert_op(
            "upsert_decision",
            {
                "id": row_id,
                "title": title,
                "detail": "",
                "quote": "We will drop the nightly backups from the old server on Friday",
                "decided_on": "2026-10-06",
                "decided_by_person_id": "per-alex-example",
                "status": "in_effect",
                "project_id": project_id,
                "supersedes_id": None,
            },
            "src-note-alex",
            locator="para 1",
        ),
        change_id=change_id,
    )


def _seed_action(instance: Path) -> None:
    _apply(
        instance,
        upsert_op(
            "upsert_action",
            {
                "id": ACTION_ID,
                "title": "Send the migration plan",
                "detail": "",
                "next_step": "",
                "status": "waiting",
                "status_basis": "reported",
                "project_id": "ops",
                "owner_person_id": None,
                "waiting_on_person_id": "per-sam-example",
                "due_date": "2026-10-09",
                "due_kind": "deadline",
                "asked_on": "2026-10-06",
                "last_verified_at": DECIDED_AT,
            },
            "src-note-team",
            locator="para 1",
        ),
        change_id="tester-seed-action",
    )


def _missing_source(doc: dict) -> None:
    doc["source_id"] = "src-missing"


def _bad_quote(doc: dict) -> None:
    # A claim plan reports every check, so the quote that isn't in the note still
    # names widget pricing and holds 40 dollars, and only C1 fails for every kind.
    doc["quote"] = "Widget pricing starts at 40 dollars for everyone"


def _supersedes_missing(doc: dict) -> None:
    doc["fields"]["supersedes"] = "dec-missing"


def _supersedes_general(doc: dict) -> None:
    doc["fields"]["supersedes"] = GENERAL_DECISION


def _thread(external_id: str | None = None, role: str | None = None):
    def change(doc: dict) -> None:
        if external_id is not None:
            doc["fields"]["thread"]["external_id"] = external_id
        if role is not None:
            doc["fields"]["thread"]["role"] = role

    return change


def _kinds(sample: str) -> list[str]:
    if sample == "every":
        return list(schemas.CANDIDATE_KINDS)
    return [f"{sample}_candidate"]


_INSTANCE = [
    ("every", [_missing_source], "/source_id", "source src-missing doesn't exist", None),
    ("every", [_bad_quote], "/quote", None, None),
    ("decision", [_set_field("decided_by", "per-nobody")], "/fields/decided_by", "person per-nobody doesn't exist", None),
    ("decision", [_set_field("workstream", "sales")], "/fields/workstream", "sales is not a workstream of this instance", None),
    ("decision", [_supersedes_missing], "/fields/supersedes", "decision dec-missing doesn't exist", None),
    (
        "decision",
        [_supersedes_general],
        "/fields/supersedes",
        f"decision {GENERAL_DECISION} is in workstream general, not ops",
        "general",
    ),
    (
        "decision",
        [lambda doc: None],
        "/fields/title",
        f"decision {DECISION_ID} already exists for this source and title",
        "decision",
    ),
    (
        "action",
        [_set_field("lead", "per-nobody"), _set_field("waiting_on", "per-nobody")],
        None,
        "person per-nobody doesn't exist",
        None,
    ),
    (
        "action",
        [lambda doc: None],
        "/fields/title",
        f"action {ACTION_ID} already exists for this source and title",
        "action",
    ),
    (
        "action",
        [_thread("thread-missing")],
        "/fields/thread",
        "thread email work thread-missing isn't in the store",
        None,
    ),
    (
        "action",
        [_thread("thread-inbound")],
        "/fields/thread",
        f"thread {INBOUND_THREAD} has no outbound message for an awaiting_reply link",
        None,
    ),
    (
        "action",
        [_thread(role="owes_reply")],
        "/fields/thread",
        f"thread {PLAN_THREAD} has no inbound message for an owes_reply link",
        None,
    ),
    (
        "action",
        [_thread("thread-empty", "event")],
        "/fields/thread",
        f"thread {EMPTY_THREAD} has no message for an event link",
        None,
    ),
    # C2 and C4.
    ("claim", [_set_field("key", "gadget pricing")], "/fields/key", "unknown fact key gadget pricing", None),
    ("claim", [_set_field("value_raw", "45 dollars")], "/fields/value_raw", "value 45 dollars is not in the quote", None),
]


def test_candidate_kinds_registry_is_exact() -> None:
    assert list(schemas.CANDIDATE_KINDS) == [
        "decision_candidate",
        "action_candidate",
        "claim_candidate",
    ]
    assert list(candidates.PLANNERS) == list(schemas.CANDIDATE_KINDS)
    assert list(CANDIDATE_SAMPLES) == list(schemas.CANDIDATE_KINDS)


def test_every_sample_is_valid(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    covered = []
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        doc = candidate(kind)
        assert validate_candidate(doc) == []
        result = _brain_validate(tmp_path, doc)
        assert result.returncode == 0, result.stderr
        assert _plan(instance, doc).errors == ()
    assert covered == list(schemas.CANDIDATE_KINDS)


@pytest.mark.parametrize(("sample", "changes", "path", "message", "seed"), _INSTANCE)
def test_instance_checks_report_every_failure(tmp_path, sample, changes, path, message, seed) -> None:
    instance = extract_instance(tmp_path)
    if seed == "general":
        _seed_decision(
            instance,
            row_id=GENERAL_DECISION,
            title="Keep the weekly export",
            project_id="general",
            change_id="tester-seed-general",
        )
    elif seed == "decision":
        _seed_decision(
            instance,
            row_id=DECISION_ID,
            title="Drop the nightly backups from the old server",
            project_id="ops",
            change_id="tester-seed-decision",
        )
    elif seed == "action":
        _seed_action(instance)
    kinds = _kinds(sample)
    covered = []
    lead_paths = ("/fields/lead", "/fields/waiting_on")
    for kind in kinds:
        covered.append(kind)
        for index, change in enumerate(changes):
            doc = candidate(kind)
            change(doc)
            expected_path = lead_paths[index] if path is None else path
            expected = message
            if message is None:
                expected = f"quote is not in source {doc['source_id']}"
            plan = _plan(instance, doc)
            assert list(plan.errors) == [{"path": expected_path, "message": expected}]
            assert plan.ops == ()
    if sample == "every":
        assert covered == list(schemas.CANDIDATE_KINDS)
    if seed == "decision":
        return
    combined = candidate("decision_candidate")
    combined["fields"]["decided_on"] = "monday"
    combined["fields"]["decided_by"] = "per-nobody"
    combined["fields"]["workstream"] = "sales"
    plan = _plan(instance, combined)
    assert list(plan.errors) == [
        {"path": "/fields/decided_on", "message": "decided_on monday isn't in the source"},
        {"path": "/fields/decided_by", "message": "person per-nobody doesn't exist"},
        {"path": "/fields/workstream", "message": "sales is not a workstream of this instance"},
    ]
    assert plan.ops == ()
    for kind in schemas.CANDIDATE_KINDS:
        missing = candidate(kind)
        missing["source_id"] = "src-missing"
        missing_plan = _plan(instance, missing)
        assert list(missing_plan.errors) == [
            {"path": "/source_id", "message": "source src-missing doesn't exist"}
        ]


_DATES = [
    ("decision", "decided_on", "friday", "2026-10-02"),
    ("decision", "decided_on", "tuesday", "2026-10-06"),
    ("decision", "decided_on", "today", "2026-10-06"),
    ("decision", "decided_on", "2026-10-06", "2026-10-06"),
    ("decision", "decided_on", "monday", "decided_on monday isn't in the source"),
    ("decision", "decided_on", "yesterday", "decided_on yesterday isn't in the source"),
    ("decision", "decided_on", "2026-10-01", "decided_on 2026-10-01 is neither the source date nor in the source"),
    ("action", "due_date", "thursday", "2026-10-08"),
    ("action", "due_date", "tomorrow", "2026-10-07"),
    ("action", "due_date", "2026-10-09", "2026-10-09"),
    ("action", "due_date", "2026-10-20", "due_date 2026-10-20 is neither the source date nor in the source"),
    ("action", "asked_on", "thursday", "2026-10-01"),
    ("action", "asked_on", "today", "asked_on today isn't in the source"),
    ("action", "asked_on", "2026-10-09", "asked_on 2026-10-09 is after the source date 2026-10-06"),
]


@pytest.mark.parametrize(("sample", "field", "value", "expected"), _DATES)
def test_dates_resolve_from_the_source_date(tmp_path, sample, field, value, expected) -> None:
    instance = extract_instance(tmp_path)
    doc = candidate(f"{sample}_candidate")
    doc["fields"][field] = value
    plan = _plan(instance, doc)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", expected):
        assert plan.errors == ()
        assert plan.ops[0]["row"][field] == expected
    else:
        assert plan.ops == ()
        assert list(plan.errors) == [{"path": f"/fields/{field}", "message": expected}]


def test_plans_are_exact(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    decision = _plan(instance, candidate("decision_candidate"))
    action = _plan(instance, candidate("action_candidate"))
    assert decision.appliable is True
    assert action.appliable is True
    assert decision.ops == (
        {
            "op": "upsert_decision",
            "base": None,
            "source": {"source_id": "src-note-alex", "locator": "para 1"},
            "row": {
                "id": DECISION_ID,
                "title": "Drop the nightly backups from the old server",
                "detail": "",
                "quote": "We will drop the nightly backups from the old server on Friday",
                "decided_on": "2026-10-06",
                "decided_by_person_id": "per-alex-example",
                "status": "in_effect",
                "project_id": "ops",
                "supersedes_id": None,
            },
        },
    )
    assert action.ops == (
        {
            "op": "upsert_action",
            "base": None,
            "source": {"source_id": "src-note-team", "locator": "para 1"},
            "row": {
                "id": ACTION_ID,
                "title": "Send the migration plan",
                "detail": "",
                "next_step": "",
                "status": "waiting",
                "status_basis": "reported",
                "project_id": "ops",
                "owner_person_id": None,
                "waiting_on_person_id": "per-sam-example",
                "due_date": "2026-10-09",
                "due_kind": "deadline",
                "asked_on": "2026-10-06",
                "last_verified_at": DECIDED_AT,
            },
        },
        {
            "op": "upsert_thread_link",
            "base": None,
            "source": {"source_id": "src-note-team", "locator": "para 1"},
            "row": {
                "id": LINK_ID,
                "action_id": ACTION_ID,
                "thread_id": PLAN_THREAD,
                "role": "awaiting_reply",
                "since": "2026-10-06T01:00:00Z",
            },
        },
    )


def _reorder(value):
    if isinstance(value, dict):
        keys = list(value)
        rotated = keys[1:] + keys[:1] if len(keys) > 1 else keys
        return {key: _reorder(value[key]) for key in rotated}
    if isinstance(value, list):
        return [_reorder(item) for item in value]
    return value


def test_ids_are_content_addressed(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    decision = candidate("decision_candidate")
    action = candidate("action_candidate")
    claim = candidate("claim_candidate")
    assert _plan(instance, decision).ops[0]["row"]["id"] == DECISION_ID
    planned = _plan(instance, action)
    assert planned.ops[0]["row"]["id"] == ACTION_ID
    assert planned.ops[1]["row"]["id"] == LINK_ID
    assert candidates.change_id("scan", decision) == "scan-78a911977dbb2cfc8d5fede3"
    assert candidates.change_id("scan", action) == "scan-66053619efc673f6b99bdda7"
    assert candidates.change_id("scan", claim) == "scan-70550cc5e18843ce8649ce97"
    renamed = candidate("action_candidate")
    renamed["fields"]["title"] = "Send the  migration PLAN."
    assert _plan(instance, renamed).ops[0]["row"]["id"] == ACTION_ID
    moved = candidate("action_candidate")
    moved["source_id"] = "src-note-alex"
    moved["quote"] = "We will drop the nightly backups from the old server on Friday"
    moved["fields"]["title"] = "Send the  migration PLAN."
    moved["fields"]["due_date"] = "2026-10-06"
    moved["fields"]["asked_on"] = "2026-10-06"
    moved_id = _plan(instance, moved).ops[0]["row"]["id"]
    assert moved_id != ACTION_ID
    assert moved_id == stable_id("act-", "src-note-alex", match_key("Send the  migration PLAN."))
    reordered = _reorder(decision)
    assert candidates.change_id("scan", reordered) == candidates.change_id("scan", decision)
    assert candidates.change_id("scan2", decision) != candidates.change_id("scan", decision)
    for agent, doc in (("scan", decision), ("scan", action), ("scan", claim), ("scan2", decision)):
        assert re.fullmatch(schemas.CHANGE_ID, candidates.change_id(agent, doc))
    assert _plan(instance, decision) == _plan(instance, decision)


def test_claim_candidates_plan_one_add_claim(tmp_path: Path) -> None:
    # A version 2 claim candidate plans one add_claim with an engine id.
    instance = extract_instance(tmp_path)
    claim = _plan(instance, candidate("claim_candidate"))
    assert claim.appliable is True
    assert claim.errors == ()
    assert claim.ops == (
        {
            "op": "add_claim",
            "row": {
                # The extractor comes first, '' when planned without one.
                "id": stable_id(
                    "clm-", "", "src-note-alex", "para 2", "widget.pricing", "", "40 dollars",
                    "Widget pricing starts at 40 dollars a seat",
                ),
                "key": "widget.pricing",
                "value_raw": "40 dollars",
                "basis": "reported",
                "quote": "Widget pricing starts at 40 dollars a seat",
            },
            "source": {"source_id": "src-note-alex", "locator": "para 2"},
        },
    )
    gadget = candidate("claim_candidate")
    gadget["fields"]["key"] = "gadget pricing"
    refused = _plan(instance, gadget)
    assert refused.appliable is True
    assert refused.ops == ()
    assert list(refused.errors) == [{"path": "/fields/key", "message": "unknown fact key gadget pricing"}]
    both = candidate("claim_candidate")
    both["fields"]["value_raw"] = "40 GB"
    both["quote"] = "We will drop the nightly backups from the old server on Friday"
    refused = _plan(instance, both)
    assert list(refused.errors) == [
        {"path": "/fields/value_raw", "message": "value 40 GB uses GB, which doesn't convert to dollars"},
        {"path": "/quote", "message": "the quote doesn't name pricing or one of its aliases"},
        {"path": "/quote", "message": "neither the quote nor the source title names widget or one of its aliases"},
    ]


def _row_paths_holding(value, needle: str, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_row_paths_holding(item, needle, f"{path}/{key}"))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_row_paths_holding(item, needle, f"{path}/{index}"))
    elif isinstance(value, str) and needle in value:
        found.append(path)
    return found


def test_planning_is_pure_and_read_only(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    db = instance / "brain.sqlite3"
    before = file_sha(db)
    config = load_config(instance / "brain.toml")
    covered = []
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        statements: list[str] = []
        con = store.connect_reader(db)
        con.row_factory = sqlite3.Row
        con.set_trace_callback(statements.append)
        try:
            planned = candidates.plan(con, config, candidate(kind), DECIDED_AT)
        finally:
            con.close()
        assert statements
        assert all(statement.startswith("SELECT") for statement in statements)
        for op in planned.ops:
            assert _row_paths_holding(op["row"], DECIDED_AT) == (
                ["/last_verified_at"] if "last_verified_at" in op["row"] else []
            )
    assert covered == list(schemas.CANDIDATE_KINDS)
    assert file_sha(db) == before

    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "hashlib",
        "re",
        "sqlite3",
        "typing",
        "brain.collectors.base",
        "brain.config",
        "brain.evidence_gate",
        "brain.schemas",
        "brain.store",
        # The planners share strict's rules, truth's resolution and
        # units' parse, all pure modules.
        "brain.strict",
        "brain.truth",
        "brain.units",
    }
    forbidden = {
        "open",
        "os",
        "environ",
        "getenv",
        "socket",
        "subprocess",
        "urllib",
        "http",
        "ssl",
        "time",
        "now",
        "today",
        "utc_now",
        "local_today",
    }
    path = Path(__file__).resolve().parents[1] / "src" / "brain" / "candidates.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed
                assert alias.asname not in forbidden
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert (node.module or "") in allowed
            for alias in node.names:
                assert alias.name not in forbidden
                assert alias.asname not in forbidden
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert node.name not in forbidden
        elif isinstance(node, ast.arg):
            assert node.arg not in forbidden
