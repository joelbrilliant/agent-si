from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
from pathlib import Path

import pytest

from brain import migrate, reads
from brain.cli import main
from brain.collectors.base import stable_id
from tests.conftest import tools_list_budgets
from tests.test_read_server import McpClient

REPO = Path(__file__).resolve().parents[1]
DEMO = REPO / "examples" / "demo"
NOW = "2026-10-09T00:00:00Z"
BASELINE_START_BYTES = 40_325
PEOPLE = {"per-alex-example", "per-robin-example", "per-casey-example"}
HOST = re.compile(r"\b[a-z0-9.-]+\.(?:com|org|net|io|ai|dev|co|au|uk)\b")
COPY_CONFIG = None
STEPS = (
    ("init", ["init", "demo", "--dir", "{parent}", "--principal", "Alex Example", "--timezone", "UTC"]),
    ("config", COPY_CONFIG),
    ("apply-config", ["apply-config", "--instance", "{demo}"]),
    ("notes", ["apply", "--instance", "{demo}", "{files}/packets/1-notes.json"]),
    ("mail", ["collect", "--instance", "{demo}", "--fixture", "{files}/fixtures/mail.json", "mail"]),
    ("work", ["apply", "--instance", "{demo}", "{files}/packets/2-work.json"]),
    ("check", ["check", "--instance", "{demo}"]),
)


def _run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    parent = tmp_path_factory.mktemp("proof")
    demo = parent / "demo"
    results = {}
    for name, argv in STEPS:
        if argv is COPY_CONFIG:
            shutil.copyfile(DEMO / "demo.toml", demo / "brain.toml")
            continue
        filled = [part.format(parent=parent, demo=demo, files=DEMO) for part in argv]
        results[name] = _run(filled)
    return demo, results


def _at(value, path: str):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def _size(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())


def test_demo_builds_from_the_example_files(built) -> None:
    demo, results = built
    for name, (code, out, err) in results.items():
        assert code == 0, (name, out, err)
    out = {name: json.loads(text) for name, (_code, text, _err) in results.items()}
    assert out["init"]["status"] == "created"
    assert out["init"]["principal"] == "per-alex-example"
    assert out["apply-config"]["status"] == "applied"
    assert out["apply-config"]["warnings"] == []
    assert out["notes"]["status"] == "applied"
    assert (out["mail"]["status"], out["mail"]["items"]) == ("ok", 3)
    assert out["work"]["status"] == "applied"
    assert out["check"] == {"ok": True, "problems": []}
    used = {DEMO / "demo.toml"}
    for _name, argv in STEPS:
        for part in argv or ():
            if "{files}" in part:
                used.add(Path(part.format(files=DEMO)))
    present = {path for path in DEMO.rglob("*") if path.is_file()}
    assert used == present - {DEMO / "questions.json"}


def test_demo_thread_ids_match_the_fixture() -> None:
    fixture = json.loads((DEMO / "fixtures" / "mail.json").read_text(encoding="utf-8"))
    packet = json.loads((DEMO / "packets" / "2-work.json").read_text(encoding="utf-8"))
    links = {
        op["row"]["id"]: op["row"]["thread_id"]
        for op in packet["ops"]
        if op["op"] == "upsert_thread_link"
    }
    assert links == {
        "lnk-demo-renewal": stable_id("thr-email-", fixture["account"], "demo-renewal-2027"),
        "lnk-demo-onboarding": stable_id("thr-email-", fixture["account"], "demo-onboarding"),
    }


def test_demo_answers_each_question_in_one_call(built) -> None:
    demo, _results = built
    questions = json.loads((DEMO / "questions.json").read_text(encoding="utf-8"))["questions"]
    assert {item["id"] for item in questions} == {"q1", "q2", "q3"}
    for item in questions:
        assert len(item["minimal_path"]) == 1, item["id"]
        if item["baseline_calls"]["min"] > 0:
            assert len(item["minimal_path"]) < item["baseline_calls"]["min"], item["id"]
        assert item["expect"] and item["answer_must"], item["id"]
        call = item["minimal_path"][0]
        result = reads.run_tool(demo, call["tool"], call["arguments"], now=NOW)
        assert not result.is_error, (item["id"], result.text)
        for path, value in item["expect"].items():
            assert _at(result.structured, path) == value, (item["id"], path)


def test_demo_checklist_rows_name_who_they_wait_on(built) -> None:
    demo, _results = built
    result = reads.run_tool(
        demo,
        "records_query",
        {"entity": "checklist_item", "filters": {"title": {"contains": "access review"}}},
        now=NOW,
    )
    assert not result.is_error, result.text
    assert "waiting_on_person_id" in result.structured["fields"]
    assert result.structured["rows"][0]["waiting_on_person_id"] == "per-casey-example"


def test_demo_fact_rows_carry_the_key_description(built) -> None:
    demo, _results = built
    result = reads.run_tool(demo, "records_query", {"entity": "fact"}, now=NOW)
    assert not result.is_error, result.text
    assert "description" in result.structured["fields"]
    assert result.structured["rows"] == [
        {
            "key": "vendor.compliance.instance",
            "description": "Regional instance of the compliance platform this company uses.",
            "value": "eu",
            "value_type": "enum",
            "unit": "",
            "status": "current",
            "as_of": "2026-09-30",
            "stale": False,
            "version": 1,
        }
    ]
    picked = reads.run_tool(
        demo, "records_query", {"entity": "fact", "filters": {"description": {"contains": "regional"}}}, now=NOW
    )
    assert [row["key"] for row in picked.structured["rows"]] == ["vendor.compliance.instance"]


def test_demo_startup_payload_is_under_the_baseline(built) -> None:
    demo, _results = built
    router = (demo / "ROUTER.md").read_bytes()
    questions = json.loads((DEMO / "questions.json").read_text(encoding="utf-8"))["questions"]
    with McpClient(demo) as client:
        opened = client.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "proof", "version": "0"},
            },
            id=1,
        )
        listed = client.request("tools/list", id=2)
        brief = client.request("tools/call", {"name": "brief", "arguments": {}}, id=3)
        calls = []
        for n, item in enumerate(questions, start=4):
            call = item["minimal_path"][0]
            reply = client.request(
                "tools/call", {"name": call["tool"], "arguments": call["arguments"]}, id=n
            )
            assert "isError" not in reply["result"], item["id"]
            calls.append(f"{item['id']} {call['tool']} {_size(reply['result'])}")
    r, i = len(router), _size(opened["result"])
    t, b = _size(listed["result"]), _size(brief["result"])
    s = r + i + t + b
    print(
        f"startup bytes: router {r}, initialize {i}, tools/list {t}, brief {b}, "
        f"total {s} (about {s // 4} tokens by bytes/4); "
        f"without tool schemas {s - t} (about {(s - t) // 4} tokens)"
    )
    print("minimal call result bytes: " + ", ".join(calls))
    assert t == tools_list_budgets()["tools_list"]
    assert r <= 2_048
    assert len(brief["result"]["content"][0]["text"].encode()) <= 4_096
    assert s < BASELINE_START_BYTES
    # A1 for the demo: under the ceiling, and within 10% of the budget.
    budget = json.loads((REPO / "bench" / "budgets.json").read_text(encoding="utf-8"))["a1"]["demo"]
    assert s <= budget["ceiling"], f"demo startup {s} is above the ceiling {budget['ceiling']}"
    assert s * 10 <= budget["budget"] * 11, f"demo startup {s} is more than 10% over the budget {budget['budget']}"
    startup = router.decode("utf-8") + json.dumps([opened, listed, brief], ensure_ascii=False)
    for text in ("vendor.compliance.instance", "EU regional instance", "eu.compliance.example.com"):
        assert text not in startup


def test_demo_files_hold_only_placeholders() -> None:
    files = sorted(path for path in DEMO.rglob("*") if path.is_file())
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "@" not in text, path.name
        for host in HOST.findall(text.lower()):
            assert host.endswith(("example.com", "example.org")), (path.name, host)
        assert set(re.findall(r"per-[a-z0-9-]+", text)) <= PEOPLE, path.name


def test_demo_brief_names_the_organisation(built) -> None:
    demo, _results = built
    result = reads.run_tool(demo, "brief", {}, now=NOW)
    assert not result.is_error, result.text
    assert result.text.splitlines()[0] == f"demo brain for Example Co, schema {migrate.latest_version()}, today 2026-10-09 (UTC)."
