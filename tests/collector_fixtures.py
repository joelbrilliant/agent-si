from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path

from brain import store
from brain.cli import main
from brain.init import init_instance

from tests.conftest import render_config

SHA64 = "ab" * 32
SHA40 = "cd" * 20
PRINCIPAL = {
    "person_id": "per-alex-example",
    "display_name": "Alex Example",
    "names": ["Alex"],
    "aliases": [],
}
_FILES_OPS = ["add_source", "record_collector_run"]
_GITHUB_OPS = ["add_source", "upsert_fact", "record_collector_run"]
_VANTA_OPS = ["add_source", "upsert_fact", "upsert_checklist_item", "record_collector_run"]
_CALENDAR_OPS = ["add_source", "upsert_thread", "upsert_meeting", "record_collector_run"]
_MESSAGE_OPS = ["add_source", "upsert_thread", "record_collector_run"]
_CAPTURE_OPS = ["add_source", "record_collector_run"]

COLLECTORS = {
    "notes": (
        {"type": "files", "agent": "notes", "root": "notes"},
        {"kind": "collector", "operations": _FILES_OPS},
    ),
    "repo": (
        {
            "type": "github",
            "agent": "repo",
            "repo": "example/engine",
            "fact_prefix": "eng.repo",
        },
        {
            "kind": "collector",
            "operations": _GITHUB_OPS,
            "scope": {"fact_prefixes": ["eng.repo"]},
        },
    ),
    "compliance": (
        {
            "type": "vanta",
            "agent": "compliance",
            "fact_prefix": "vendor.vanta",
            "project": "ops",
            "document_ok_statuses": ["OK"],
        },
        {
            "kind": "collector",
            "operations": _VANTA_OPS,
            "scope": {"fact_prefixes": ["vendor.vanta"], "projects": ["ops"]},
        },
    ),
    "work-cal": (
        {"type": "calendar", "agent": "workcal", "account": "work-cal"},
        {"kind": "collector", "operations": _CALENDAR_OPS},
    ),
    "gmail-work": (
        {"type": "gmail", "agent": "gmailwork", "account": "work"},
        {"kind": "collector", "operations": _MESSAGE_OPS},
    ),
    "slack-team": (
        {"type": "slack", "agent": "slackteam", "account": "team"},
        {"kind": "collector", "operations": _MESSAGE_OPS},
    ),
    "work-docs": (
        {"type": "capture", "agent": "workdocs", "origin": "work-docs"},
        {"kind": "collector", "operations": _CAPTURE_OPS, "author_principal": True},
    ),
}

_GITHUB_FACTS = [
    {
        "key": "eng.repo.ci_state",
        "value_type": "enum",
        "allowed_values": ["success", "failure", "pending", "none"],
    },
    {"key": "eng.repo.last_push_on", "value_type": "date"},
    {"key": "eng.repo.open_pull_requests", "value_type": "int"},
]
_VANTA_FACTS = [
    {"key": "vendor.vanta.controls_complete", "value_type": "int"},
    {"key": "vendor.vanta.controls_total", "value_type": "int"},
    {"key": "vendor.vanta.documents_ok", "value_type": "int"},
    {"key": "vendor.vanta.documents_total", "value_type": "int"},
    {"key": "vendor.vanta.failing_tests", "value_type": "int"},
]
_EXTRA_FACTS = {"repo": _GITHUB_FACTS, "compliance": _VANTA_FACTS}


def _default_agents() -> dict:
    return {
        "tester": {"operations": "*", "author_principal": True},
        "plain": {"operations": "*"},
        "scoped": {
            "operations": ["add_source", "upsert_action", "upsert_fact"],
            "scope": {"projects": ["general"], "fact_prefixes": ["demo"]},
        },
        "careful": {"operations": "*", "approval": "required", "approvers": ["tester"]},
        "collector": {
            "operations": [
                "add_source",
                "upsert_thread",
                "upsert_thread_link",
                "upsert_signal",
                "record_collector_run",
            ]
        },
    }


def _default_fact_keys() -> list[dict]:
    return [
        {"key": "demo.region", "value_type": "enum", "allowed_values": ["au", "us"]},
        {"key": "demo.seats", "value_type": "int"},
        {"key": "demo.price", "value_type": "number", "unit": "AUD"},
        {"key": "demo.launch", "value_type": "date"},
        {"key": "demo.enabled", "value_type": "bool"},
        {"key": "demo.site", "value_type": "url"},
        {"key": "demo.motto", "value_type": "text"},
        {"key": "ops.window", "value_type": "text"},
    ]


def collector_instance(
    tmp_path: Path,
    *names: str,
    agents: dict | None = None,
    fact_keys: list[dict] | None = None,
    timezone: str = "UTC",
) -> Path:
    parent = Path(tmp_path)
    parent.mkdir(parents=True, exist_ok=True)
    merged = _default_agents()
    tables = {}
    for name in names:
        table, block = COLLECTORS[name]
        tables[name] = table
        merged[table["agent"]] = block
    if agents:
        merged.update(agents)
    keys = list(_default_fact_keys() if fact_keys is None else fact_keys)
    seen = {item["key"] for item in keys}
    for name in names:
        for item in _EXTRA_FACTS.get(name, ()):
            if item["key"] not in seen:
                keys.append(item)
                seen.add(item["key"])
    text = render_config(
        timezone=timezone,
        principal=PRINCIPAL,
        fact_keys=keys,
        agents=merged,
        collectors=tables,
    )
    init_instance("demo", parent, config_text=text)
    return parent / "demo"


def files_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.files.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "root": "notes",
        "files": [
            {
                "path": "2026/board-prep.md",
                "size": 66,
                "mtime": "2026-10-07T22:00:00Z",
                "sha256": SHA64,
            }
        ],
    }
    doc.update(overrides)
    return doc


def github_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.github.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "repo": "example/engine",
        "default_branch": "main",
        "pull_requests": [
            {"number": 12, "state": "open", "draft": False, "updated_at": "2026-10-07T01:00:00Z"}
        ],
        "runs": [
            {
                "id": 9001,
                "branch": "main",
                "head_sha": SHA40,
                "status": "completed",
                "conclusion": "success",
                "updated_at": "2026-10-07T02:00:00Z",
            }
        ],
        "pushes": [
            {"branch": "main", "head_sha": SHA40, "at": "2026-10-07T00:30:00Z"}
        ],
    }
    doc.update(overrides)
    return doc


def vanta_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.vanta.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "controls": [{"id": "ctl-0001", "external_id": "AC-1", "complete": True}],
        "documents": [{"id": "doc-0001", "status": "OK"}],
        "failing_tests": [
            {
                "id": "tst-0001",
                "name": "MFA on for every account",
                "status": "NEEDS_ATTENTION",
                "due": "2026-10-20",
                "link": "https://app.vanta.example/tests/tst-0001",
                "controls": ["AC-1"],
            }
        ],
    }
    doc.update(overrides)
    return doc


def calendar_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.calendar.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "account": "work-cal",
        "outbound_visible": True,
        "events": [
            {
                "id": "evt-board-prep",
                "title": "Board prep",
                "start": "2026-10-09T01:00:00Z",
                "end": "2026-10-09T02:00:00Z",
                "all_day": False,
                "status": "confirmed",
                "attendee_count": 4,
                "updated": "2026-10-07T03:00:00Z",
                "updated_by_self": False,
            }
        ],
    }
    doc.update(overrides)
    return doc


def gmail_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.gmail.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "account": "work",
        "outbound_visible": True,
        "threads": [
            {
                "id": "thread-review",
                "subject": "Launch review",
                "messages": [
                    {"at": "2026-10-02T09:00:00Z", "direction": "outbound"},
                    {"at": "2026-10-04T09:00:00Z", "direction": "inbound"},
                ],
            }
        ],
    }
    doc.update(overrides)
    return doc


def slack_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.slack.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "account": "team",
        "outbound_visible": True,
        "threads": [
            {
                "id": "C0ABC1234:1791400000.000100",
                "messages": [
                    {"at": "2026-10-02T09:00:00Z", "direction": "outbound"},
                    {"at": "2026-10-04T09:00:00Z", "direction": "inbound"},
                ],
            }
        ],
    }
    doc.update(overrides)
    return doc


def capture_doc(**overrides) -> dict:
    doc = {
        "format": "agent-brain.capture.v1",
        "fetched_at": "2026-10-08T00:00:00Z",
        "complete": True,
        "origin": "work-docs",
        "items": [
            {
                "ref": "document:work-docs/specs/pricing.md",
                "kind": "document",
                "title": "specs/pricing.md",
                "date": "2026-10-07",
                "text": "# Pricing\n\nThe Pro plan costs 49 AUD a seat.",
            }
        ],
    }
    doc.update(overrides)
    return doc


SAMPLES = {
    "files": files_doc,
    "github": github_doc,
    "vanta": vanta_doc,
    "calendar": calendar_doc,
    "gmail": gmail_doc,
    "slack": slack_doc,
    "capture": capture_doc,
}


def scenario_documents() -> list[dict]:
    return [
        files_doc(),
        files_doc(complete=False),
        github_doc(),
        github_doc(complete=False),
        vanta_doc(),
        vanta_doc(complete=False),
    ]


def write_fixture(directory: Path, doc: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw = store.canonical_json(doc).encode()
    path = directory / f"{hashlib.sha256(raw).hexdigest()[:16]}.json"
    path.write_bytes(raw)
    return path


def _run_collect(instance: Path, name: str, fixture: Path | None) -> tuple[int, dict | None, str]:
    argv = ["collect", "--instance", str(instance), name]
    if fixture is not None:
        argv[1:1] = ["--fixture", str(fixture)]
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    text = stdout.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, stderr.getvalue()


def collect(instance: Path, name: str, doc: dict) -> tuple[int, dict | None, str]:
    path = write_fixture(Path(instance).parent / "fixtures", doc)
    return _run_collect(instance, name, path)


def collect_file(instance: Path, name: str, fixture: Path) -> tuple[int, dict | None, str]:
    return _run_collect(instance, name, fixture)


@contextlib.contextmanager
def block_network():
    from unittest.mock import patch

    def boom(*_args, **_kwargs):
        raise AssertionError("network used")

    with (
        patch("socket.socket.connect", boom),
        patch("socket.create_connection", boom),
        patch("socket.getaddrinfo", boom),
    ):
        yield
