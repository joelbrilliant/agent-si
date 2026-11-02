from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from importlib.resources import files
from pathlib import Path

import pytest

from brain import schemas
from brain.applier import Applier
from brain.config import Agent, ConfigError, load_config
from brain.init import _render_template, init_instance
from tests.conftest import make_packet, render_config
from tests.extract_fixtures import SCAN

_TEMPLATE_TAIL = """\
# Agents that may submit change packets. kind = "agent" (the default) cites sources but
# can't add them. Only collectors (kind = "collector") add sources, so every quote is
# checked against text a collector captured.
[agents.assistant]
kind = "agent"
operations = "*"               # "*" or a list of operation names
approval = "none"              # "none" or "required"
approvers = []                 # agents that may approve this agent's held packets
author_principal = false       # kind = "collector" only: may add sources authored by the principal

# Each collector writes as its own agent, with exactly its type's operations.
# [agents.notes]
# kind = "collector"
# operations = ["add_source", "record_collector_run"]

# Collectors read fixture documents only: brain collect --instance DIR --fixture FILE NAME
[collectors]
# [collectors.notes]
# type = "files"
# agent = "notes"
# root = "notes"
"""

_MANUAL_LINE = "brain: agent {name} adds sources by hand (manual_sources = true)"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "brain.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_l1_assistant_cannot_add_a_source(tmp_path: Path) -> None:
    result = subprocess.run(
        ["brain", "init", "demo"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    demo = tmp_path / "demo"
    before = _source_ids(demo)
    packets = {
        "add_source": {
            "op": "add_source",
            "row": {
                "id": "src-assistant-try",
                "kind": "user_note",
                "title": "Note",
                "source_date": "2026-10-08",
                "content": "Assistant tried to add a source.",
            },
        },
        "record_collector_run": {
            "op": "record_collector_run",
            "row": {
                "id": "run-assistant-try",
                "collector": "notes",
                "started_at": "2026-10-08T00:00:00Z",
                "finished_at": "2026-10-08T00:00:01Z",
                "status": "ok",
            },
        },
    }
    writer = Applier(demo)
    try:
        for op_name, op in packets.items():
            outcome = writer.apply(
                json.dumps(
                    make_packet("assistant", op, change_id=f"assistant-{op_name.replace('_', '-')[:20]}")
                ).encode()
            )
            assert outcome.receipt["status"] == "rejected"
            assert outcome.receipt["error"] == f"op 0 ({op_name}): not allowed for agent assistant"
    finally:
        writer.close()
    assert _source_ids(demo) == before


def test_l1_only_collectors_may_be_granted_collector_ops(tmp_path: Path) -> None:
    denied = render_config(agents={"notes": {"kind": "agent", "operations": ["add_source"]}})
    with pytest.raises(
        ConfigError,
        match=re.escape(
            'agents.notes: add_source is only for collectors (kind = "collector")'
        ),
    ):
        load_config(_write(tmp_path, denied))
    allowed = render_config(
        agents={
            "notes": {
                "kind": "collector",
                "manual_sources": True,
                "operations": ["add_source"],
            }
        }
    )
    loaded = load_config(_write(tmp_path, allowed))
    assert loaded.agents["notes"].kind == "collector"
    assert loaded.agents["notes"].allows("add_source") is True


def test_l1_star_grants_collector_ops_only_to_collectors() -> None:
    agent = Agent("assistant", "*", kind="agent")
    collector = Agent("notes", "*", kind="collector")
    for op_name in ("add_source", "record_collector_run"):
        assert agent.allows(op_name) is False
        assert collector.allows(op_name) is True
    assert agent.allows("upsert_fact_key") is False
    assert collector.allows("upsert_fact_key") is False


def test_l1_author_principal_needs_a_collector(tmp_path: Path) -> None:
    text = render_config(
        agents={"notes": {"kind": "agent", "author_principal": True, "operations": "*"}}
    )
    with pytest.raises(
        ConfigError,
        match=re.escape('agents.notes.author_principal needs kind = "collector"'),
    ):
        load_config(_write(tmp_path, text))


def test_l1_agent_kind_must_be_agent_or_collector(tmp_path: Path) -> None:
    invalid = render_config(agents={"notes": {"kind": "bot", "operations": "*"}})
    with pytest.raises(ConfigError, match=re.escape("agents.notes.kind is invalid")):
        load_config(_write(tmp_path, invalid))
    omitted = render_config(agents={"notes": {"kind": "agent", "operations": "*"}})
    omitted = omitted.replace('kind = "agent"\n', "", 1)
    loaded = load_config(_write(tmp_path, omitted))
    assert loaded.agents["notes"].kind == "agent"


def test_template_assistant_is_an_agent(tmp_path: Path) -> None:
    raw = files("brain").joinpath("templates/brain.toml.template").read_text(encoding="utf-8")
    marker = "# Agents that may submit change packets."
    assert "manual_sources" not in raw
    assert raw[raw.index(marker) :] == _TEMPLATE_TAIL
    for principal in (None, "Alex Example"):
        rendered = _render_template("demo", "UTC", principal)
        assert "manual_sources" not in rendered
        assert rendered[rendered.index(marker) :] == _TEMPLATE_TAIL
        path = tmp_path / f"{'with' if principal else 'without'}.toml"
        path.write_text(rendered, encoding="utf-8")
        loaded = load_config(path)
        assert loaded.agents["assistant"].kind == "agent"


def test_l1_unbound_collector_agent_needs_manual_sources(tmp_path: Path, capsys) -> None:
    unbound = render_config(
        agents={"notes": {"kind": "collector", "operations": ["add_source"]}}
    )
    with pytest.raises(
        ConfigError,
        match=re.escape(
            'agents.notes: kind = "collector" needs a [collectors] entry or manual_sources = true'
        ),
    ):
        load_config(_write(tmp_path, unbound))
    opted = render_config(
        agents={
            "notes": {
                "kind": "collector",
                "manual_sources": True,
                "operations": ["add_source"],
            }
        }
    )
    loaded = load_config(_write(tmp_path, opted))
    assert loaded.agents["notes"].manual_sources is True

    agent_manual = render_config(
        agents={"notes": {"kind": "agent", "manual_sources": True, "operations": "*"}}
    )
    with pytest.raises(
        ConfigError,
        match=re.escape('agents.notes.manual_sources needs kind = "collector"'),
    ):
        load_config(_write(tmp_path, agent_manual))

    not_bool = render_config(
        agents={"notes": {"kind": "agent", "manual_sources": "yes", "operations": "*"}}
    )
    with pytest.raises(
        ConfigError,
        match=re.escape("agents.notes.manual_sources must be a boolean"),
    ):
        load_config(_write(tmp_path, not_bool))

    from brain import cli

    hand = tmp_path / "hand"
    hand.mkdir()
    created = init_instance("demo", hand, config_text=render_config())
    code = cli.main(["check", "--instance", created["instance"]])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["ok"] is True
    names = ["careful", "collector", "plain", "scoped", "tester"]
    assert captured.err.splitlines() == [_MANUAL_LINE.format(name=name) for name in names]

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    init = subprocess.run(
        ["brain", "init", "demo"],
        cwd=fresh,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert init.returncode == 0, init.stderr
    check = subprocess.run(
        ["brain", "check", "--instance", str(fresh / "demo")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stdout
    assert "manual_sources" not in check.stderr
    assert "adds sources by hand" not in check.stderr


def _source_ids(instance: Path) -> list[str]:
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        return [row[0] for row in con.execute("SELECT id FROM sources ORDER BY id")]
    finally:
        con.close()


def _tester() -> dict:
    return {"tester": {"operations": "*"}}


@pytest.mark.parametrize(
    ("agents", "message"),
    [
        (
            {"notes": {"kind": "agent", "operations": ["apply_candidate"]}},
            'agents.notes: apply_candidate is only for extractors (kind = "extractor")',
        ),
        (
            {
                **_tester(),
                "scan": {
                    "kind": "extractor",
                    "operations": "*",
                    "approval": "required",
                    "approvers": ["tester"],
                },
            },
            'agents.scan: kind = "extractor" needs an operations list',
        ),
        (
            {
                **_tester(),
                "scan": {
                    "kind": "extractor",
                    "operations": ["upsert_decision"],
                    "approval": "required",
                    "approvers": ["tester"],
                },
            },
            "agents.scan: an extractor's operations must include apply_candidate",
        ),
        (
            {
                **_tester(),
                "scan": {
                    "kind": "extractor",
                    "operations": ["apply_candidate", "upsert_fact"],
                    "approval": "required",
                    "approvers": ["tester"],
                },
            },
            "agents.scan: upsert_fact is not allowed for an extractor",
        ),
        (
            {
                "scan": {
                    "kind": "extractor",
                    "operations": [
                        "apply_candidate",
                        "upsert_decision",
                        "upsert_action",
                        "upsert_thread_link",
                    ],
                    "approval": "none",
                }
            },
            'agents.scan: an extractor must have approval = "verified" or "required"',
        ),
        (
            {
                **_tester(),
                "scan": dict(SCAN),
                "careful": {
                    "operations": "*",
                    "approval": "required",
                    "approvers": ["scan"],
                },
            },
            "agents.careful: approver scan is an extractor, and an extractor can't approve",
        ),
    ],
)
def test_extractor_config_is_validated(tmp_path: Path, agents: dict, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(_write(tmp_path, render_config(agents=agents)))
    loaded = load_config(
        _write(tmp_path, render_config(agents={**_tester(), "scan": dict(SCAN)}))
    )
    assert loaded.agents["scan"].kind == "extractor"


def test_extractor_ops_are_only_for_extractors() -> None:
    scan = Agent("scan", ("apply_candidate", "upsert_decision"), kind="extractor")
    assert scan.allows("apply_candidate") is True
    assert scan.allows("upsert_decision") is True
    assert scan.allows("add_source") is False
    assistant = Agent("assistant", "*", kind="agent")
    notes = Agent("notes", "*", kind="collector")
    covered = set()
    for name in schemas.EXTRACTOR_OPS:
        covered.add(name)
        assert assistant.allows(name) is False
        assert notes.allows(name) is False
    assert covered == set(schemas.EXTRACTOR_OPS)


def test_extraction_config_is_refused(tmp_path: Path) -> None:
    # [extraction] is replaced by [truth], so every form of the block is refused
    # with one message, where 0.7.0a1 accepted or checked it.
    bare = load_config(_write(tmp_path, render_config()))
    assert not hasattr(bare, "extraction")
    message = re.escape(
        "extraction is replaced by [truth] in 0.7.0: remove [extraction], then register fact keys under "
        "[[fact_keys]] and their aliases under [truth]"
    )
    blocks = [
        '\n[extraction]\nproducts = ["widget"]\nclaim_areas = ["pricing", "security"]\n',
        "\n[extraction]\nmodels = []\n",
        *(
            f"\n[extraction]\nproducts = {value}\nclaim_areas = [\"pricing\"]\n"
            for value in (
                '["widget", "widget"]',
                json.dumps([f"s{i:02d}" for i in range(51)]),
                '["Widget"]',
                '"widget"',
            )
        ),
    ]
    for block in blocks:
        with pytest.raises(ConfigError, match=message):
            load_config(_write(tmp_path, render_config() + block))
