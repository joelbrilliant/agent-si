from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from brain.applier import Applier
from brain.collectors.base import stable_id
from brain.init import init_instance
from tests.collector_fixtures import (
    COLLECTORS,
    PRINCIPAL,
    _EXTRA_FACTS,
    _default_agents,
    _default_fact_keys,
)
from tests.conftest import make_packet, render_config, source_op, upsert_op

SAM = "per-sam-example"
ALEX_NOTE = (
    "Notes from Alex, Tuesday 6 October 2026, written today. We will drop the nightly backups "
    "from the old server on Friday. Widget pricing starts at 40 dollars a seat for the first year."
)
TEAM_NOTE = (
    "Team call, 6 October 2026. Sam agreed to send the migration plan by 2026-10-09 and asked "
    "for a reply tomorrow. The security review moves to Thursday."
)
SCAN = {
    "kind": "extractor",
    "operations": ["apply_candidate", "upsert_decision", "upsert_action", "upsert_thread_link", "add_claim"],
    "approval": "required",
    "approvers": ["tester"],
}
# The claim candidate's key : its implicit names are in the quote.
WIDGET_PRICING = {"key": "widget.pricing", "value_type": "int", "unit": "dollars"}

_THREADS = (
    ("thread-plan", "Migration plan", "last_outbound_at", "2026-10-06T01:00:00Z"),
    ("thread-inbound", "Vendor question", "last_inbound_at", "2026-10-06T02:00:00Z"),
    ("thread-empty", "Placeholder", None, None),
)


def thread_id(external_id: str) -> str:
    return stable_id("thr-email-", "work", external_id)


def extract_instance(tmp_path, *names, agents=None) -> Path:
    parent = Path(tmp_path)
    parent.mkdir(parents=True, exist_ok=True)
    merged = _default_agents()
    tables = {}
    for name in names:
        table, block = COLLECTORS[name]
        tables[name] = table
        merged[table["agent"]] = block
    merged["scan"] = dict(SCAN)
    if agents:
        merged.update(agents)
    keys = [*_default_fact_keys(), WIDGET_PRICING]
    seen = {item["key"] for item in keys}
    for name in names:
        for item in _EXTRA_FACTS.get(name, ()):
            if item["key"] not in seen:
                keys.append(item)
                seen.add(item["key"])
    text = render_config(
        timezone="UTC",
        principal=PRINCIPAL,
        fact_keys=keys,
        agents=merged,
        collectors=tables,
    )
    init_instance("demo", parent, config_text=text)
    instance = parent / "demo"
    ops = [
        source_op("src-seed", "Seed rows for the extraction tests.", date="2026-10-06"),
        upsert_op(
            "upsert_person",
            {"id": SAM, "display_name": "Sam Example"},
            "src-seed",
            locator="seed",
        ),
        source_op(
            "src-note-alex",
            ALEX_NOTE,
            author="per-alex-example",
            title="Notes from Alex",
            date="2026-10-06",
        ),
        source_op(
            "src-note-team",
            TEAM_NOTE,
            title="Team call",
            date="2026-10-06",
        ),
    ]
    for external_id, subject, time_key, time_value in _THREADS:
        row = {
            "id": thread_id(external_id),
            "channel": "email",
            "account": "work",
            "external_id": external_id,
            "subject": subject,
            "outbound_seen": True,
        }
        if time_key is not None:
            row[time_key] = time_value
        ops.append(upsert_op("upsert_thread", row, "src-seed", locator="seed"))
    packet = make_packet("tester", *ops, change_id="tester-extract-seed")
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(packet).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", outcome.receipt
    return instance


def candidate(kind: str) -> dict:
    return deepcopy(CANDIDATE_SAMPLES[kind])


CANDIDATE_SAMPLES = {
    "decision_candidate": {
        "candidate_version": 1,
        "kind": "decision_candidate",
        "source_id": "src-note-alex",
        "locator": "para 1",
        "quote": "We will drop the nightly backups from the old server on Friday",
        "fields": {
            "title": "Drop the nightly backups from the old server",
            "decided_on": "2026-10-06",
            "decided_by": "per-alex-example",
            "status": "in_effect",
            "workstream": "ops",
        },
    },
    "action_candidate": {
        "candidate_version": 1,
        "kind": "action_candidate",
        "source_id": "src-note-team",
        "locator": "para 1",
        "quote": "Sam agreed to send the migration plan by 2026-10-09",
        "fields": {
            "title": "Send the migration plan",
            "status": "waiting",
            "status_basis": "reported",
            "workstream": "ops",
            "waiting_on": SAM,
            "due_date": "2026-10-09",
            "due_kind": "deadline",
            "asked_on": "2026-10-06",
            "thread": {
                "channel": "email",
                "account": "work",
                "external_id": "thread-plan",
                "role": "awaiting_reply",
            },
        },
    },
    "claim_candidate": {
        "candidate_version": 2,
        "kind": "claim_candidate",
        "source_id": "src-note-alex",
        "locator": "para 2",
        "quote": "Widget pricing starts at 40 dollars a seat",
        "fields": {"key": "widget pricing", "value_raw": "40 dollars"},
    },
}


def _run_brain(argv: list[str]) -> tuple[int, dict | None, str]:
    import contextlib
    import io
    import json

    from brain.cli import main

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    text = stdout.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, stderr.getvalue()


def extract(instance, doc, agent="scan"):
    import hashlib
    from pathlib import Path

    from brain import store

    raw = store.canonical_json(doc).encode() if isinstance(doc, dict) else bytes(doc)
    directory = Path(instance).parent / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(raw).hexdigest()[:16]}.json"
    path.write_bytes(raw)
    return _run_brain(["extract", "--instance", str(instance), "--as", agent, str(path)])


def held(instance, *argv):
    return _run_brain(["held", "--instance", str(instance), *argv])


def review(instance, verb, change_id, agent="tester", note="not now"):
    argv = [verb, "--instance", str(instance), change_id, "--as", agent]
    if verb == "refuse":
        argv.extend(["--note", note])
    return _run_brain(argv)
