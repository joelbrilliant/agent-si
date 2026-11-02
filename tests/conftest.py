from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from brain import __version__, migrate
from brain import reads
from brain.applier import Applier
from brain.init import init_instance, render_router
from brain.store import connect_readonly, sha256_text

CORE_TABLES = [
    "sources",
    "people",
    "systems",
    "projects",
    "fact_keys",
    "facts",
    "fact_history",
    "records",
    "record_history",
    "decisions",
    "actions",
    "checklist_items",
    "threads",
    "thread_links",
    "meetings",
    "action_people",
    "checklist_item_people",
    "unchecked_text",
    "signals",
    "collector_runs",
    "write_log",
    "source_author_changes",
    "search_keys",
    "claims",
    "aliases",
    "note_meta",
    "redaction_gate",
    "redaction_packets",
    "links",
]

FTS_TABLES = [
    "search_index",
    "search_index_config",
    "search_index_content",
    "search_index_data",
    "search_index_docsize",
    "search_index_idx",
]


def context_connection(instance: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    con = sqlite3.connect(instance / "brain.sqlite3")
    con.execute("PRAGMA foreign_keys = ON" if foreign_keys else "PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA trusted_schema = ON")
    con.create_function("brain_applier_context", 0, lambda: 1)
    return con


def v1_instance(tmp_path: Path) -> Path:
    instance = tmp_path / "demo"
    instance.mkdir()
    (instance / "brain.toml").write_text(TEST_CONFIG, encoding="utf-8")
    (instance / "ROUTER.md").write_text(render_router("demo"), encoding="utf-8")
    migrations = tmp_path / "schema-v1"
    migrations.mkdir()
    core = Path(__file__).resolve().parents[1] / "src" / "brain" / "migrations" / "0001_core.sql"
    (migrations / "0001_core.sql").write_text(core.read_text(encoding="utf-8"), encoding="utf-8")
    migrate.migrate(instance / "brain.sqlite3", create=True, migrations_dir=migrations)
    now = "2026-10-01T00:00:00Z"
    content = "Old kettle notes. Descale monthly. Water filter swapped in spring."
    digest = sha256_text(content)
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO sources (id, kind, title, source_date, content, content_sha256, "
            "captured_at, captured_by) VALUES (?, 'user_note', 'Old notes', '2026-10-01', "
            "?, ?, ?, 'tester:v1')",
            ("src-old-notes", content, digest, now),
        )
        con.execute(
            "INSERT INTO projects (id, kind, title, description, source_id, locator, version, "
            "created_at, updated_at, updated_by) VALUES ('general', 'workstream', 'General', "
            "'Default workstream.', 'src-old-notes', 'synthetic', 1, ?, ?, 'tester:v1')",
            (now, now),
        )
        con.execute(
            "INSERT INTO records (id, kind, title, body, source_id, locator, version, "
            "created_at, updated_at, updated_by) VALUES "
            "('rec-old-a', 'note', 'Old kettle note', 'Descale the kettle monthly.', "
            "'src-old-notes', 'synthetic', 1, ?, ?, 'tester:v1')",
            (now, now),
        )
        con.execute(
            "INSERT INTO records (id, kind, title, body, source_id, locator, version, "
            "created_at, updated_at, updated_by) VALUES "
            "('rec-old-b', 'note', 'Water filter', 'Swap the water filter in spring.', "
            "'src-old-notes', 'synthetic', 1, ?, ?, 'tester:v1')",
            (now, now),
        )
        con.execute(
            "INSERT INTO actions (id, title, status, status_basis, project_id, last_verified_at, "
            "source_id, locator, version, created_at, updated_at, updated_by) VALUES "
            "('act-old', 'Order filters', 'open', 'observed', 'general', ?, "
            "'src-old-notes', 'synthetic', 1, ?, ?, 'tester:v1')",
            (now, now, now),
        )
        con.execute("COMMIT")
    finally:
        con.close()
    return instance


def _toml_str(value: str) -> str:
    return json.dumps(value)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return json.dumps(value)
    return json.dumps(value)


def render_config(
    *,
    name: str = "demo",
    timezone: str = "UTC",
    principal: dict | None | bool = None,
    workstreams: list[dict] | None = None,
    fact_keys: list[dict] | None = None,
    agents: dict | None = None,
    collectors: dict[str, dict] | None = None,
) -> str:
    if principal is None:
        principal = {
            "person_id": "per-jade",
            "display_name": "Jade",
            "names": ["Jade"],
            "aliases": [],
        }
    if workstreams is None:
        workstreams = [
            {"id": "general", "title": "General", "description": "Default workstream.", "sort_order": 100},
            {"id": "ops", "title": "Ops", "description": "Operations.", "sort_order": 200},
        ]
    if fact_keys is None:
        fact_keys = [
            {"key": "demo.region", "value_type": "enum", "allowed_values": ["au", "us"]},
            {"key": "demo.seats", "value_type": "int"},
            {"key": "demo.price", "value_type": "number", "unit": "AUD"},
            {"key": "demo.launch", "value_type": "date"},
            {"key": "demo.enabled", "value_type": "bool"},
            {"key": "demo.site", "value_type": "url"},
            {"key": "demo.motto", "value_type": "text"},
            {"key": "ops.window", "value_type": "text"},
        ]
    if agents is None:
        agents = {
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
    lines = [
        "[instance]",
        f"name = {_toml_str(name)}",
        f"timezone = {_toml_str(timezone)}",
        "",
    ]
    if principal:
        lines += [
            "[principal]",
            f"person_id = {_toml_str(principal['person_id'])}",
            f"display_name = {_toml_str(principal['display_name'])}",
            f"names = {json.dumps(list(principal.get('names', [])))}",
            f"aliases = {json.dumps(list(principal.get('aliases', [])))}",
            "",
        ]
    for item in workstreams:
        lines += [
            "[[workstreams]]",
            f"id = {_toml_str(item['id'])}",
            f"title = {_toml_str(item['title'])}",
            f"description = {_toml_str(item.get('description', ''))}",
            f"sort_order = {item.get('sort_order', 100)}",
            "",
        ]
    for item in fact_keys:
        lines += [
            "[[fact_keys]]",
            f"key = {_toml_str(item['key'])}",
            f"value_type = {_toml_str(item['value_type'])}",
            f"unit = {_toml_str(item.get('unit', ''))}",
            f"description = {_toml_str(item.get('description', ''))}",
        ]
        if "allowed_values" in item:
            lines.append(f"allowed_values = {json.dumps(item['allowed_values'])}")
        if item.get("stale_after_days") is not None:
            lines.append(f"stale_after_days = {item['stale_after_days']}")
        lines.append("")
    for agent_name, block in agents.items():
        lines.append(f"[agents.{agent_name}]")
        lines.append(f"kind = {_toml_str(block.get('kind', 'collector'))}")
        if "kind" not in block:
            lines.append("manual_sources = true")
        elif "manual_sources" in block:
            lines.append(f"manual_sources = {_toml_value(block['manual_sources'])}")
        if "legacy_import" in block:
            lines.append(f"legacy_import = {_toml_value(block['legacy_import'])}")
        if "person_id" in block:
            lines.append(f"person_id = {_toml_value(block['person_id'])}")
        ops = block.get("operations", "*")
        if ops == "*":
            lines.append('operations = "*"')
        else:
            lines.append(f"operations = {json.dumps(list(ops))}")
        lines.append(f"approval = {_toml_str(block.get('approval', 'none'))}")
        lines.append(f"approvers = {json.dumps(list(block.get('approvers', [])))}")
        lines.append(f"author_principal = {'true' if block.get('author_principal') else 'false'}")
        scope = block.get("scope") or {}
        if scope:
            lines.append(f"[agents.{agent_name}.scope]")
            if "projects" in scope:
                lines.append(f"projects = {json.dumps(list(scope['projects']))}")
            if "fact_prefixes" in scope:
                lines.append(f"fact_prefixes = {json.dumps(list(scope['fact_prefixes']))}")
        lines.append("")
    lines += ["[collectors]", ""]
    for collector_name, table in (collectors or {}).items():
        lines.append(f"[collectors.{collector_name}]")
        for key, value in table.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines)


TEST_CONFIG = render_config()

NOW = "2026-10-08T00:00:00Z"

READ_FACT_KEYS = [
    {"key": "demo.region", "value_type": "enum", "allowed_values": ["au", "us"]},
    {"key": "demo.seats", "value_type": "int", "stale_after_days": 30},
    {"key": "demo.price", "value_type": "number", "unit": "AUD", "stale_after_days": 90},
    {"key": "demo.launch", "value_type": "date"},
    {"key": "demo.enabled", "value_type": "bool"},
    {"key": "demo.site", "value_type": "url"},
    {"key": "demo.motto", "value_type": "text"},
    {"key": "ops.window", "value_type": "text"},
    {
        "key": "vendor.compliance.instance",
        "value_type": "enum",
        "allowed_values": ["au", "eu", "us"],
        "stale_after_days": 180,
        "description": "Which compliance-platform instance the company uses",
    },
]

READ_CONFIG = render_config(fact_keys=READ_FACT_KEYS)


@pytest.fixture(scope="session", autouse=True)
def _ensure_brain_on_path() -> None:
    # The venv's own bin as well as the interpreter it links to, so a venv Python run directly finds its brain.
    extras = [Path.home() / ".local" / "bin", Path(sys.executable).resolve().parent, Path(sys.executable).parent]
    parts = os.environ.get("PATH", "").split(os.pathsep)
    for extra in extras:
        text = str(extra)
        if extra.is_dir() and text not in parts:
            parts.insert(0, text)
    os.environ["PATH"] = os.pathsep.join(parts)


@pytest.fixture(scope="session", autouse=True)
def _machine_folders(tmp_path_factory) -> None:
    """The registry and update state live under XDG folders. Every test, and every process it
    starts, gets its own, so no test reads or writes this machine's ~/.config/agent-si or ~/.local/state."""
    root = tmp_path_factory.mktemp("machine")
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    os.environ["XDG_STATE_HOME"] = str(root / "state")
    # No test finds this machine's uv tools: a real brain, or another tool, installed there.
    (root / "uv-bin").mkdir()
    os.environ["UV_TOOL_BIN_DIR"] = str(root / "uv-bin")
    # Nor this machine's agent memory, whose token counts brain usage report reads when it's installed.
    os.environ["XDG_DATA_HOME"] = str(root / "data")
    os.environ.pop("AGENT_SESSION_DB", None)


def config_with(**changes) -> str:
    return render_config(**changes)


def attribution_refusal(sentence: str, display_name: str = "Alex Example", person_id: str = "per-alex-example") -> str:
    """The evidence gate's refusal text for `sentence`, so tests about plumbing do not each copy its wording.
    tests/test_attribution_corpus.py pins the wording itself."""
    from brain.evidence_gate import Principal, attribution_refusal as refusal

    return refusal(sentence, Principal(person_id, display_name, (display_name.split()[0],)))


def database_refusal(rule: str, table: str, row: dict | None = None) -> str:
    """The words a refused write gives for a database rule such as "UNIQUE constraint failed: claims.id", built
    by the engine's own helper so a test never copies them. The store isn't consulted, so a FOREIGN KEY rule
    gets its general form."""
    import sqlite3

    from brain import advice

    return advice.integrity_error(None, sqlite3.IntegrityError(rule), table, row)


def make_packet(agent: str, *ops, change_id: str | None = None, intent: str = "test") -> dict:
    return {
        "packet_version": 1,
        "change_id": change_id or f"{agent}-{uuid4().hex[:12]}",
        "agent": agent,
        "intent": intent,
        "ops": list(ops),
    }


def source_op(
    id: str,
    content: str,
    *,
    kind: str = "user_note",
    author=None,
    title: str = "Synthetic source",
    date: str = "2026-10-01",
) -> dict:
    row = {
        "id": id,
        "kind": kind,
        "title": title,
        "source_date": date,
        "author_person_id": author,
        "content": content,
    }
    return {"op": "add_source", "row": row}


def upsert_op(op: str, row: dict, source_id: str, *, locator: str = "synthetic", base=None) -> dict:
    return {
        "op": op,
        "row": row,
        "source": {"source_id": source_id, "locator": locator},
        "base": base,
    }


TOOLS_LIST_CEILING = 6_144  # the 6 KiB cap on the whole tools/list reply; a limit, not a measurement


def tools_list_budgets() -> dict:
    """bench/budgets.json's a3 section: the one place the tools/list byte counts are pinned.

    The numbers move only with a changelog entry and a new fingerprint there (test_bench_gate), so growth stays a
    deliberate edit to that file and not a number copied into each test."""
    path = Path(__file__).resolve().parents[1] / "bench" / "budgets.json"
    return json.loads(path.read_text(encoding="utf-8"))["a3"]


def router_budgets() -> dict:
    """bench/budgets.json's a2 section, the one place ROUTER.md's byte counts are pinned (alongside router_max)."""
    path = Path(__file__).resolve().parents[1] / "bench" / "budgets.json"
    return json.loads(path.read_text(encoding="utf-8"))["a2"]


def router_bytes(name: str, notes: bool = True) -> int:
    """ROUTER.md's size for an instance name: the fixed text plus the name, less the notes line with notes off."""
    a2 = router_budgets()
    return a2["router_base_bytes"] + len(name.encode("utf-8")) - (0 if notes else a2["router_notes_off_saving"])


def version_banner() -> str:
    """What `brain --version` prints, read from the package so a release bump needs no test edits."""
    return f"agent-si {__version__} (schema {migrate.latest_version()})"


def ro_rows(instance: Path, sql: str, params=()) -> list[tuple]:
    con = connect_readonly(instance / "brain.sqlite3")
    try:
        return list(con.execute(sql, params))
    finally:
        con.close()


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def seed(apply):
    packet = make_packet(
        "tester",
        source_op(
            "src-note-anon",
            "We will cut the access form from the onboarding pack this week.",
        ),
        upsert_op(
            "upsert_person",
            {"id": "per-dana-okafor", "display_name": "Dana Okafor"},
            "src-note-anon",
            locator="seed",
        ),
        upsert_op(
            "upsert_person",
            {"id": "per-sam-ito", "display_name": "Sam Ito"},
            "src-note-anon",
            locator="seed",
        ),
        source_op(
            "src-note-jade",
            "We will cut the access form from the onboarding pack this week. Dana can own the new checklist.",
            author="per-jade",
        ),
        source_op(
            "src-email-dana",
            "Can we move the launch review to Thursday afternoon? Friday clashes with the board pack.",
            kind="email",
            author="per-dana-okafor",
        ),
    )
    return apply(packet)


def read_seed(apply):
    source_id = "src-read-notes"
    verified = "2026-10-01T00:00:00Z"
    notes = (
        "Descale the office kettle with citric acid every month. Café rules apply. "
        "The board pack goes out on 20 October. The compliance platform runs on the AU instance. "
        # The strict facts' quotes and act-review's due date come from the source.
        "Follow up by 2026-10-06. We have 12 seats. Each seat costs 49.5 AUD. The demo region is AU. "
        "Launch day is 2026-11-02. Demo enabled is true. The site https://example.com is live. "
        "Our motto is Small steps. Later we had 13 seats. Then we had 14 seats."
    )
    ops = [
        source_op(source_id, notes, title="Read fixture notes", date="2026-10-01"),
        upsert_op(
            "upsert_record",
            {
                "id": "rec-kettle",
                "kind": "process",
                "title": "Descale the office kettle",
                "body": "Descale the office kettle with citric acid every month. Café rules apply.",
            },
            source_id,
        ),
        upsert_op(
            "upsert_record",
            {
                "id": "rec-kettle-old",
                "kind": "process",
                "title": "Old kettle routine",
                "body": "Descale the kettle with vinegar.",
                "status": "superseded",
            },
            source_id,
        ),
        upsert_op(
            "upsert_action",
            {
                "id": "act-review",
                "title": "Launch review follow-up",
                "status": "waiting",
                "status_basis": "observed",
                "project_id": "general",
                "due_date": "2026-10-06",
                "due_kind": "deadline",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_action",
            {
                "id": "act-old-review",
                "title": "Old launch review chase",
                "status": "done",
                "status_basis": "observed",
                "project_id": "general",
                "closed_at": "2026-10-02T00:00:00Z",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_action",
            {
                "id": "act-board",
                "title": "Send the board pack",
                "status": "open",
                "status_basis": "reported",
                "project_id": "ops",
                "due_date": "2026-10-20",
                "due_kind": "deadline",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_checklist_item",
            {
                "id": "chk-access",
                "project_id": "ops",
                "title": "Remove the access form",
                "state": "waiting",
                "state_basis": "reported",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_checklist_item",
            {
                "id": "chk-badges",
                "project_id": "ops",
                "title": "Order new badges",
                "state": "done",
                "state_basis": "observed",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_checklist_item",
            {
                "id": "chk-na",
                "project_id": "ops",
                "title": "Print welcome cards",
                "state": "not_applicable",
                "state_basis": "observed",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread",
            {
                "id": "thr-review",
                "channel": "email",
                "account": "work",
                "external_id": "thread-review",
                "subject": "Launch review",
                "last_inbound_at": "2026-10-04T09:00:00Z",
                "last_outbound_at": "2026-10-02T09:00:00Z",
                "outbound_seen": True,
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread",
            {
                "id": "thr-other",
                "channel": "email",
                "account": "work",
                "external_id": "thread-other",
                "subject": "Launch review question",
                "last_inbound_at": "2026-10-05T09:00:00Z",
                "outbound_seen": False,
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread",
            {
                "id": "thr-board",
                "channel": "email",
                "account": "work",
                "external_id": "thread-board",
                "subject": "Board pack",
                "last_inbound_at": "2026-10-03T09:00:00Z",
                "last_outbound_at": "2026-10-01T09:00:00Z",
                "outbound_seen": True,
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread",
            {
                "id": "thr-hidden",
                "channel": "email",
                "account": "work",
                "external_id": "thread-hidden",
                "subject": "Access form",
                "last_inbound_at": "2026-09-20T09:00:00Z",
                "outbound_seen": False,
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread_link",
            {
                "id": "lnk-review",
                "action_id": "act-review",
                "thread_id": "thr-review",
                "role": "awaiting_reply",
                "since": "2026-10-02T09:00:00Z",
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread_link",
            {
                "id": "lnk-board",
                "action_id": "act-board",
                "thread_id": "thr-board",
                "role": "owes_reply",
                "since": "2026-10-02T09:00:00Z",
            },
            source_id,
        ),
        upsert_op(
            "upsert_thread_link",
            {
                "id": "lnk-hidden",
                "checklist_item_id": "chk-access",
                "thread_id": "thr-hidden",
                "role": "owes_reply",
                "since": "2026-09-25T09:00:00Z",
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.seats",
                "value": 12,
                "quote": "We have 12 seats",
                "value_raw": "12",
                "basis": "observed",
                "as_of": "2026-09-01",
                "last_verified_at": "2026-09-01T00:00:00Z",
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.price",
                "value": 49.5,
                "quote": "Each seat costs 49.5 AUD",
                "value_raw": "49.5",
                "basis": "reported",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.region",
                "value": "au",
                "quote": "The demo region is AU",
                "value_raw": "au",
                "basis": "observed",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.launch",
                "value": "2026-11-02",
                "quote": "Launch day is 2026-11-02",
                "value_raw": "2026-11-02",
                "basis": "reported",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.enabled",
                "value": True,
                "quote": "Demo enabled is true",
                "value_raw": "true",
                "basis": "observed",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.site",
                "value": "https://example.com",
                "quote": "The site https://example.com is live",
                "value_raw": "https://example.com",
                "basis": "observed",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "demo.motto",
                "value": "Small steps",
                "quote": "Our motto is Small steps",
                "value_raw": "Small steps",
                "basis": "inferred",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_fact",
            {
                "key": "vendor.compliance.instance",
                "value": "au",
                "quote": "The compliance platform runs on the AU instance",
                "value_raw": "au",
                "basis": "observed",
                "as_of": "2026-10-01",
                "last_verified_at": verified,
            },
            source_id,
        ),
        upsert_op(
            "upsert_decision",
            {
                "id": "dec-access-form",
                "title": "Cut the access form",
                "quote": "We will cut the access form from the onboarding pack this week.",
                "decided_on": "2026-10-01",
                "decided_by_person_id": "per-jade",
                "status": "in_effect",
                "project_id": "general",
            },
            "src-note-jade",
        ),
        upsert_op(
            "upsert_system",
            {
                "id": "sys-compliance",
                "name": "Compliance platform",
                "kind": "saas",
                "vendor": "Example Compliance Co",
            },
            source_id,
        ),
        upsert_op(
            "upsert_signal",
            {
                "id": "sig-review-reply",
                "kind": "reply",
                "subject_table": "actions",
                "subject_id": "act-review",
                "thread_id": "thr-review",
                "basis": "observed",
            },
            source_id,
        ),
        upsert_op(
            "upsert_signal",
            {
                "id": "sig-board-note",
                "kind": "other",
                "subject_table": "actions",
                "subject_id": "act-board",
                "detail": "The board pack needs a second reader.",
                "basis": "inferred",
            },
            source_id,
        ),
    ]
    return apply(make_packet("tester", *ops, intent="read fixture"))


def read_instance(tmp_path: Path) -> Path:
    result = init_instance("demo", tmp_path, config_text=READ_CONFIG)
    instance = Path(result["instance"])
    writer = Applier(instance)
    try:
        def apply(packet):
            return writer.apply(json.dumps(packet).encode())

        seeded = seed(apply)
        if seeded.receipt["status"] != "applied":
            raise AssertionError(seeded.receipt)
        extra = read_seed(apply)
        if extra.receipt["status"] != "applied":
            raise AssertionError(extra.receipt)
    finally:
        writer.close()
    return instance


def call(instance: Path, tool: str, **arguments):
    return reads.run_tool(instance, tool, arguments, now=NOW)


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    result = init_instance("demo", tmp_path, config_text=TEST_CONFIG)
    return Path(result["instance"])


@pytest.fixture
def applier(instance: Path):
    writer = Applier(instance)
    try:
        yield writer
    finally:
        writer.close()


@pytest.fixture
def apply(applier: Applier):
    def _apply(packet: dict):
        return applier.apply(json.dumps(packet).encode())

    return _apply
