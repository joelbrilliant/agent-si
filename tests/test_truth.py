"""Claims, conflicts and reads.

Every name, value and host here is invented. Tests go through Applier, reads.run_tool, the CLI,
config.load_config, migrate.migrate and store.check_instance.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from brain import advice, migrate, reads, schemas, store, truth, units
from brain.applier import Applier
from brain.cli import main
from brain.collectors import REGISTRY
from brain.config import ConfigError, load_config
from brain.init import init_instance, render_router
from tests.conftest import context_connection, database_refusal, make_packet, render_config, ro_rows, router_bytes, upsert_op, version_banner

NOW = "2026-10-09T00:00:00Z"
READ_NOW = "2026-10-09T12:00:00Z"
PRINCIPAL = {
    "person_id": "per-alex-example",
    "display_name": "Alex Example",
    "names": ["Alex"],
    "aliases": [],
}
AGENTS = {
    "alex": {"operations": "*", "author_principal": True},
    "notes": {"operations": "*", "approvers": ["alex"]},
    "bot": {"kind": "agent", "operations": "*", "approvers": ["alex"]},
    "solo": {"kind": "agent", "operations": "*"},
}
KEYS = [
    {"key": "excloud.max_context", "value_type": "int", "unit": "tokens"},
    {"key": "excloud.gpu_memory", "value_type": "int", "unit": "GB"},
    {"key": "excloud.current_version", "value_type": "text"},
    {"key": "exbasic.price", "value_type": "number", "unit": "AUD"},
    {"key": "vendor.compliance.instance", "value_type": "enum", "allowed_values": ["au", "eu", "us"]},
    {"key": "vendor.compliance.current_version", "value_type": "text"},
    {"key": "exunits.bytes", "value_type": "int", "unit": "B"},
    {"key": "exunits.disk", "value_type": "number", "unit": "TB"},
    {"key": "exunits.hours", "value_type": "number", "unit": "h"},
    {"key": "exunits.share", "value_type": "number", "unit": "%"},
    {"key": "exunits.seats", "value_type": "int", "unit": "seats"},
    {"key": "exunits.count", "value_type": "int"},
    {"key": "exunits.site", "value_type": "url"},
    {"key": "exunits.region", "value_type": "enum", "allowed_values": ["EU", "eu."]},
    {"key": "exunits.motto", "value_type": "text"},
    {"key": "exunits.enabled", "value_type": "bool"},
    {"key": "exunits.launch", "value_type": "date"},
    {"key": "exunits.stale", "value_type": "int", "stale_after_days": 30},
]
TRUTH_BLOCK = """
[truth.entities]
excloud = ["Example Cloud", "EC"]
"vendor.compliance" = ["compliance platform"]

[truth.attributes]
gpu_memory = ["VRAM", "GPU memory", "GPU RAM"]
max_context = ["context window", "context length", "max context"]
"""


def truth_config(*, keys=None, agents=None, truth_block: str = TRUTH_BLOCK) -> str:
    text = render_config(principal=PRINCIPAL, agents=agents or AGENTS, fact_keys=keys or KEYS)
    return text + truth_block


class Clock:
    """Fixes store.utc_now, which every decision reads, so stamps and orders are exact."""

    def __init__(self, monkeypatch) -> None:
        self.now = NOW
        monkeypatch.setattr(store, "utc_now", lambda: self.now)

    def at(self, stamp: str) -> None:
        self.now = stamp


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)


def _inst(tmp_path: Path, config: str | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Path(init_instance("demo", tmp_path, config_text=config or truth_config())["instance"])


def _apply(inst: Path, agent: str, *ops, reserved: bool = False):
    writer = Applier(inst, allow_reserved_agent=reserved)
    try:
        return writer.apply(json.dumps(make_packet(agent, *ops)).encode())
    finally:
        writer.close()


def _ok(outcome):
    assert outcome.receipt["status"] == "applied", (outcome.receipt["status"], outcome.receipt["error"])
    return outcome


def _err(outcome, status: str = "rejected") -> str:
    assert outcome.receipt["status"] == status, outcome.receipt
    return outcome.receipt["error"]


def _src(sid: str, content: str, *, kind: str = "document", date: str = "2026-09-01", author=None,
         uri: str = "", supersedes=None, external_ref: str = "") -> dict:
    row = {"id": sid, "kind": kind, "title": "Synthetic source", "source_date": date,
           "author_person_id": author, "content": content}
    if uri:
        row["uri"] = uri
    if external_ref:
        row["external_ref"] = external_ref
    if supersedes is not None:
        row["supersedes_id"] = supersedes
    return {"op": "add_source", "row": row}


def _clm(cid: str, key: str, raw: str, quote: str, source: str, *, scope=None, basis: str = "reported",
         planned=None, supersedes=None, as_of=None, locator: str = "para 1") -> dict:
    row = {"id": cid, "key": key, "value_raw": raw, "basis": basis, "quote": quote}
    if scope is not None:
        row["scope"] = scope
    if planned is not None:
        row["planned"] = planned
    if supersedes is not None:
        row["supersedes"] = list(supersedes)
    if as_of is not None:
        row["as_of"] = as_of
    return {"op": "add_claim", "row": row, "source": {"source_id": source, "locator": locator}}


def _resolve(key: str, keep, reject, quote: str, source: str, *, scope: str = "", locator: str = "message 1") -> dict:
    return {
        "op": "resolve_fact",
        "row": {"key": key, "scope": scope, "keep": list(keep), "reject": list(reject), "quote": quote},
        "source": {"source_id": source, "locator": locator},
    }


def _base(inst: Path, key: str):
    rows = ro_rows(inst, "SELECT version FROM facts WHERE key = ?", (key,))
    return rows[0][0] if rows else None


def _put(inst: Path, key: str, value, source: str, *, status=None, basis: str = "observed",
         as_of: str = "2026-10-01", verified: str | None = "2026-10-01T00:00:00Z", locator: str = "synthetic",
         base="read", quote: str | None = None, raw: str | None = None) -> dict:
    row: dict = {"key": key}
    if value is not None:
        row["value"] = value
    # A strict upsert_fact quotes its cited source and gives its raw value.
    if quote is not None:
        row["quote"] = quote
    if raw is not None:
        row["value_raw"] = raw
    if status is not None:
        row["status"] = status
    if basis is not None:
        row["basis"] = basis
    if as_of is not None:
        row["as_of"] = as_of
    if verified is not None:
        row["last_verified_at"] = verified
    return upsert_op("upsert_fact", row, source, locator=locator, base=_base(inst, key) if base == "read" else base)


def _fact(inst: Path, key: str) -> dict | None:
    con = store.connect_readonly(inst / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM facts WHERE key = ?", (key,)).fetchone()
        return None if row is None else dict(row)
    finally:
        con.close()


def _claims(inst: Path, key: str) -> dict[str, dict]:
    con = store.connect_readonly(inst / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        return {row["id"]: dict(row) for row in con.execute("SELECT * FROM claims WHERE key = ? ORDER BY id", (key,))}
    finally:
        con.close()


def _conflicts(inst: Path, key: str | None = None, status: str = "open") -> list[tuple]:
    sql = "SELECT id, subject_id, detail, status FROM signals WHERE kind = 'conflict'"
    params: list = []
    if status is not None:
        sql += " AND status = ?"
        params.append(status)
    if key is not None:
        sql += " AND subject_id = ?"
        params.append(key)
    return ro_rows(inst, sql + " ORDER BY id", tuple(params))


def _sig(key: str, label: str, n: int = 1) -> str:
    digest = hashlib.sha256((key + "\n" + label).encode()).hexdigest()[:16]
    return f"sig-conflict-{digest}-{n}"


def _read(inst: Path, tool: str, now: str = READ_NOW, **arguments):
    return reads.run_tool(inst, tool, arguments, now=now)


def _fact_get(inst: Path, key: str, **arguments) -> dict:
    result = _read(inst, "fact_get", key=key, **arguments)
    assert result.is_error is False, result.text
    return result.structured


def _fact_error(inst: Path, key: str, **arguments) -> tuple[str, str]:
    result = _read(inst, "fact_get", key=key, **arguments)
    assert result.is_error is True, result.text
    payload = json.loads(result.text)["error"]
    return payload["code"], payload["message"]


def _brief(inst: Path, now: str = READ_NOW) -> list[str]:
    result = _read(inst, "brief", now=now)
    assert result.is_error is False, result.text
    return result.text.splitlines()


def _disputes(inst: Path, now: str = READ_NOW) -> list[str]:
    return [line for line in _brief(inst, now) if line.startswith("Disputed ")]


def _cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(part) for part in argv])
    return code, out.getvalue(), err.getvalue()


def _packet_file(inst: Path, agent: str, *ops) -> tuple[Path, str]:
    packet = make_packet(agent, *ops)
    path = inst.parent / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    return path, packet["change_id"]


def _people(inst: Path, *people) -> None:
    ops = [_src("src-people", "People in the claim tests.", kind="user_note")]
    for person_id, name in people:
        ops.append(upsert_op("upsert_person", {"id": person_id, "display_name": name}, "src-people"))
    _ok(_apply(inst, "alex", *ops))


# The state of the worked example: current version 1.3, clm-a2 (an older document) agrees with
# clm-a, clm-b is scoped to the current version and disagrees, and clm-c is a checked document
# claim scoped to 1.2.
CTX = "excloud.max_context"


def _example_state(inst: Path) -> None:
    _ok(_apply(inst, "alex",
               _src("src-docs-ec-limits", "Limits page. Context window: 128K tokens. Nodes ship with VRAM.",
                    date="2026-09-01"),
               _src("src-docs-ec-old", "Old limits page. Context window: 128K tokens.", date="2026-08-01"),
               _src("src-slack-ec-0920", "Thread: max context on 1.3 is 131,072 now.", kind="slack",
                    date="2026-09-20"),
               _src("src-docs-ec-v12", "Release 1.2 notes: max context is 64K tokens.", date="2026-07-01"),
               _src("src-docs-ec-version", "The current version is 1.3 for every region.", date="2026-09-25")))
    _ok(_apply(inst, "notes", _clm("clm-a2", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-old")))
    _ok(_apply(inst, "notes", _clm("clm-a", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-limits")))
    _ok(_apply(inst, "alex", _clm("clm-version-13", "excloud.current_version", "1.3",
                                  "The current version is 1.3 for every region", "src-docs-ec-version")))
    _ok(_apply(inst, "notes", _clm("clm-c", CTX, "64K", "max context is 64K tokens", "src-docs-ec-v12", scope="1.2")))
    _ok(_apply(inst, "bot", _clm("clm-b", CTX, "131,072", "max context on 1.3 is 131,072", "src-slack-ec-0920",
                                 scope="1.3")))


EXAMPLE = {
    "query": "Example Cloud context window",
    "resolved": "excloud.max_context",
    "facts": [
        {
            "key": "excloud.max_context",
            "value_type": "int",
            "unit": "tokens",
            "status": "disputed",
            "last_verified_at": "2026-10-09T00:00:00Z",
            "age_days": 0,
            "stale_after_days": None,
            "stale": False,
            "version": 3,
            "claims": {
                "scope": "now",
                "confidence": "disputed",
                "current_version": "1.3",
                "values": [
                    {"id": "clm-a", "value": 128000, "raw": "128K", "basis": "reported", "checked": True,
                     "agree": 1, "suggested": True,
                     "source": {"id": "src-docs-ec-limits", "kind": "document", "date": "2026-09-01"},
                     "quote": "Context window: 128K tokens"},
                    {"id": "clm-b", "value": 131072, "raw": "131,072", "scope": "1.3", "basis": "reported",
                     "checked": True, "agree": 0,
                     "source": {"id": "src-slack-ec-0920", "kind": "slack", "date": "2026-09-20"},
                     "quote": "max context on 1.3 is 131,072"},
                ],
                "other_scopes": {"1.2": 64000},
                "other_scopes_sources": {"1.2": "src-docs-ec-v12"},
            },
        }
    ],
    "total": 1,
    "truncated": False,
    "unset": [],
}


# -- aliases and keys --------------------------------------------------------------------------


def test_alias_words_resolve_to_one_key(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-vram", "Each node has 80GB of GPU memory."),
               _clm("clm-vram", "excloud.gpu_memory", "80GB", "Each node has 80GB of GPU memory", "src-vram")))
    for text in ("Example Cloud VRAM", "EC GPU memory", "EC GPU RAM", "excloud vram", "excloud.vram",
                 "excloud.gpu_memory"):
        result = _fact_get(inst, text)
        assert result["query"] == text
        assert [item["key"] for item in result["facts"]] == ["excloud.gpu_memory"], text
        assert result["facts"][0]["value"] == 80
        if text == "excloud.gpu_memory":
            assert "resolved" not in result
        else:
            assert result["resolved"] == "excloud.gpu_memory"
            assert list(result)[:2] == ["query", "resolved"]


def _config_error(tmp_path: Path, text: str) -> str:
    path = tmp_path / "brain.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    return str(exc.value)


def test_alias_clash_is_a_config_error(tmp_path: Path) -> None:
    entities = truth_config(truth_block='\n[truth.entities]\nexcloud = ["EC"]\nexbasic = ["ec"]\n')
    assert _config_error(tmp_path, entities) == 'truth.entities: "EC" is an alias of both exbasic and excloud'
    attributes = truth_config(
        truth_block='\n[truth.attributes]\ngpu_memory = ["memory"]\nmax_context = ["Memory"]\n'
    )
    assert _config_error(tmp_path, attributes) == (
        'truth.attributes: "memory" is an alias of both gpu_memory and max_context'
    )
    implicit = truth_config(truth_block='\n[truth.attributes]\ngpu_memory = ["max context"]\n')
    assert _config_error(tmp_path, implicit) == (
        'truth.attributes: "max context" is an alias of both gpu_memory and max_context'
    )
    inst = _inst(tmp_path / "inst")
    (inst / "brain.toml").write_text(entities, encoding="utf-8")
    assert 'brain.toml: truth.entities: "EC" is an alias of both exbasic and excloud' in store.check_instance(inst)
    code, out, _stderr = _cli("check", "--instance", inst)
    assert code == 1
    assert 'brain.toml: truth.entities: "EC" is an alias of both exbasic and excloud' in json.loads(out)["problems"]


def test_check_compares_aliases_with_the_store_keys(tmp_path: Path) -> None:
    """A key dropped from brain.toml stays in the store, so brain check runs the clash rule on it."""
    keys = [*KEYS, {"key": "exold.speed", "value_type": "int"}]
    inst = _inst(tmp_path, truth_config(keys=keys))
    clashing = truth_config(truth_block=TRUTH_BLOCK.replace('"GPU RAM"]', '"GPU RAM", "speed"]'))
    (inst / "brain.toml").write_text(clashing, encoding="utf-8")
    load_config(inst / "brain.toml")  # it loads: brain.toml no longer names exold.speed
    problems = store.check_instance(inst)
    assert 'brain.toml: truth.attributes: "speed" is an alias of both gpu_memory and speed' in problems


def test_check_names_an_unreadable_store(tmp_path: Path) -> None:
    """Brain check says when it can't read the fact keys."""
    inst = _inst(tmp_path)
    (inst / "brain.sqlite3").write_bytes(b"not a database " * 100)
    _code, _out, stderr = _cli("check", "--instance", inst)
    assert any(line.startswith("brain: could not read fact keys: ") for line in stderr.splitlines()), stderr


def test_unknown_key_gives_up_to_three_hints(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    assert _fact_error(inst, "exbasic.cost") == (
        "unknown_key",
        'unknown fact key exbasic.cost. Closest keys: exbasic.price. Use search with entities ["fact"], '
        "or fact_get with a prefix such as exbasic.*",
    )
    assert _fact_error(inst, "zzz.qqq") == (
        "unknown_key",
        'unknown fact key zzz.qqq. Use search with entities ["fact"], or fact_get with a prefix such as zzz.*',
    )
    code, message = _fact_error(inst, "Example Cloud bandwidth")
    assert code == "unknown_key"
    assert message == (
        "unknown fact key Example Cloud bandwidth. Closest keys: excloud.current_version, excloud.gpu_memory, "
        'excloud.max_context. Use search with entities ["fact"].'
    )
    assert _fact_error(inst, "nothing like this") == (
        "unknown_key",
        'unknown fact key nothing like this. Use search with entities ["fact"].',
    )


def test_ambiguous_key_lists_both(tmp_path: Path) -> None:
    keys = [*KEYS, {"key": "ex_a.price", "value_type": "int"}, {"key": "ex.a.price", "value_type": "int"}]
    inst = _inst(tmp_path, truth_config(keys=keys))
    result = _read(inst, "fact_get", key="ex a price")
    assert result.code == "ambiguous_key"
    error = json.loads(result.text)["error"]
    assert error["message"] == (
        "ex a price matches more than one fact key: ex.a.price, ex_a.price. Pass one of them as key."
    )
    assert error["allowed"] == ["ex.a.price", "ex_a.price"]


def _receipt_ops(inst: Path, change_id: str) -> list[dict]:
    rows = ro_rows(inst, "SELECT packet_json FROM write_log WHERE change_id = ?", (change_id,))
    return json.loads(rows[0][0])["ops"]


def test_apply_config_loads_aliases_and_check_compares(tmp_path: Path, clock: Clock) -> None:
    plain = render_config(principal=PRINCIPAL, agents=AGENTS, fact_keys=KEYS)
    inst = _inst(tmp_path, plain)
    assert ro_rows(inst, "SELECT count(*) FROM aliases") == [(0,)]
    (inst / "brain.toml").write_text(plain + TRUTH_BLOCK, encoding="utf-8")
    assert "aliases differ from brain.toml; run brain apply-config" in store.check_instance(inst)
    code, out, err = _cli("apply-config", "--instance", inst)
    assert code == 0, (out, err)
    receipt = json.loads(out)["receipt"]
    ops = _receipt_ops(inst, receipt["change_id"])
    assert [op["op"] for op in ops] == ["add_source", "set_aliases"]
    expected = [
        ("attribute", "context length"),
        ("attribute", "context window"),
        ("attribute", "gpu memory"),
        ("attribute", "gpu ram"),
        ("attribute", "max context"),
        ("attribute", "vram"),
        ("entity", "compliance platform"),
        ("entity", "ec"),
        ("entity", "example cloud"),
    ]
    alias_targets = [item for item in receipt["targets"] if item["table"] == "aliases"]
    assert alias_targets == [{"table": "aliases", "id": f"{k}:{a}", "action": "insert"} for k, a in expected]
    assert store.check_instance(inst) == []
    code, out, _stderr = _cli("apply-config", "--instance", inst)
    assert (code, json.loads(out)["status"]) == (0, "nothing to apply")
    fewer = plain + TRUTH_BLOCK.replace('"GPU RAM"', "").replace(", ]", "]")
    (inst / "brain.toml").write_text(fewer, encoding="utf-8")
    code, out, _stderr = _cli("apply-config", "--instance", inst)
    assert code == 0
    targets = json.loads(out)["receipt"]["targets"]
    assert [item for item in targets if item["table"] == "aliases"] == [
        {"table": "aliases", "id": "attribute:gpu ram", "action": "delete"}
    ]
    (inst / "brain.toml").write_text(fewer.replace('"EC"]', '"EC", "Example Cloud Platform"]'), encoding="utf-8")
    assert store.check_instance(inst) == ["aliases differ from brain.toml; run brain apply-config"]
    code, out, _stderr = _cli("check", "--instance", inst)
    assert code == 1
    assert json.loads(out)["problems"] == ["aliases differ from brain.toml; run brain apply-config"]


def test_entity_is_everything_before_the_last_dot(tmp_path: Path, clock: Clock) -> None:
    keys = [*KEYS, {"key": "vendor.current_version", "value_type": "text"}]
    inst = _inst(tmp_path, truth_config(keys=keys))
    _ok(_apply(inst, "alex",
               _src("src-cp", "The compliance platform is on version 2.0. The vendor ships version 3.0. "
                              "On 2.0 the instance is eu. On 3.0 the instance is us."),
               _clm("clm-cp-version", "vendor.compliance.current_version", "2.0",
                    "compliance platform is on version 2.0", "src-cp"),
               _clm("clm-vendor-version", "vendor.current_version", "3.0", "The vendor ships version 3.0", "src-cp"),
               _clm("clm-cp-eu", "vendor.compliance.instance", "eu", "On 2.0 the instance is eu", "src-cp",
                    scope="2.0"),
               _clm("clm-cp-us", "vendor.compliance.instance", "us", "On 3.0 the instance is us", "src-cp",
                    scope="3.0")))
    assert truth.key_parts("vendor.compliance.current_version") == ("vendor.compliance", "current_version")
    result = _fact_get(inst, "compliance platform instance")
    assert result["resolved"] == "vendor.compliance.instance"
    item = result["facts"][0]
    assert (item["value"], item["status"]) == ("eu", "current")
    assert item["claims"]["current_version"] == "2.0"
    assert item["claims"]["other_scopes"] == {"3.0": "us"}
    assert _fact(inst, "vendor.current_version")["value_text"] == "3.0"


# -- units ---------------------------------------------------------------------------------------


def _unit_claims(inst: Path, cases: list[tuple[str, str]]) -> dict[str, dict]:
    ops = []
    quotes = []
    for index, (key, raw) in enumerate(cases):
        quote = f"case {index} reads {raw} here"
        quotes.append(quote)
        ops.append(_clm(f"clm-u{index}", key, raw, quote, "src-units", locator=f"case {index}"))
    _ok(_apply(inst, "alex", _src("src-units", ". ".join(quotes) + "."), *ops))
    found = {}
    for index, (key, _raw) in enumerate(cases):
        found[f"clm-u{index}"] = _claims(inst, key)[f"clm-u{index}"]
    return found


def test_units_normalise_within_family(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    cases = [
        ("excloud.gpu_memory", "80GB"),
        ("excloud.gpu_memory", "80 GB"),
        ("excloud.gpu_memory", "80 gigabytes"),
        ("exunits.count", "128K"),
        ("excloud.max_context", "128K"),
        ("exunits.bytes", "1 GiB"),
        ("exunits.share", "45%"),
        ("exunits.hours", "90 min"),
        ("exbasic.price", "A$49.50"),
        ("exbasic.price", "$49.50 AUD"),
        ("exbasic.price", "AUD 49.50"),
    ]
    found = _unit_claims(inst, cases)
    stored = [
        found[f"clm-u{i}"]["value_int"] if found[f"clm-u{i}"]["value_type"] == "int" else found[f"clm-u{i}"]["value_real"]
        for i in range(len(cases))
    ]
    assert stored == [80, 80, 80, 128000, 128000, 1073741824, 45.0, 1.5, 49.5, 49.5, 49.5]
    assert [found[f"clm-u{i}"]["value_raw"] for i in range(len(cases))] == [raw for _key, raw in cases]
    assert _fact(inst, "excloud.gpu_memory")["status"] == "current"
    assert _fact(inst, "exbasic.price")["value_real"] == 49.5
    assert _conflicts(inst) == []


def test_decimal_comparison_has_no_float_drift(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    cents = list(range(1, 1000))  # every two-decimal TB value from 0.01 to 9.99
    for key in ("exunits.bytes", "exunits.disk"):
        previous: list[str] = []
        for start in range(0, len(cents), 200):
            chunk = cents[start:start + 200]
            quotes, ops = [], []
            for cent in chunk:
                tb = f"{cent // 100}.{cent % 100:02d} TB"
                gb = f"{cent * 10} GB"
                pair = [f"clm-{key[8:]}-tb{cent}", f"clm-{key[8:]}-gb{cent}"]
                quotes += [f"size {cent} is {tb} here", f"size {cent} is {gb} here"]
                source = f"src-{key[8:]}-{start}"
                ops.append(_clm(pair[0], key, tb, quotes[-2], source, locator=f"tb {cent}",
                                supersedes=previous or None))
                ops.append(_clm(pair[1], key, gb, quotes[-1], source, locator=f"gb {cent}"))
                previous = pair
            _ok(_apply(inst, "alex", _src(f"src-{key[8:]}-{start}", ". ".join(quotes) + "."), *ops))
        claims = _claims(inst, key)
        column = "value_int" if key == "exunits.bytes" else "value_real"
        for cent in cents:
            tb, gb = claims[f"clm-{key[8:]}-tb{cent}"], claims[f"clm-{key[8:]}-gb{cent}"]
            assert tb[column] == gb[column], (key, cent, tb[column], gb[column])
        # Support every time, never a dispute: no conflict signal was ever opened and every
        # headline the derivation wrote was current.
        assert _conflicts(inst, key, status=None) == []
        assert ro_rows(inst, "SELECT DISTINCT status FROM fact_history WHERE key = ?", (key,)) == [("current",)]
        last = _fact_get(inst, key, claims="all")["facts"][0]["claims"]["values"]
        assert [entry["agree"] for entry in last] == [1, 1]
    bytes_claims, disk_claims = _claims(inst, "exunits.bytes"), _claims(inst, "exunits.disk")
    assert bytes_claims["clm-bytes-tb27"]["value_int"] == bytes_claims["clm-bytes-gb27"]["value_int"] == 270_000_000_000
    assert disk_claims["clm-disk-tb27"]["value_real"] == disk_claims["clm-disk-gb27"]["value_real"] == 0.27


def test_wrong_family_and_currency_are_refused(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-refuse", "Values that should not parse.")))
    cases = [
        ("excloud.max_context", "80 GB", "value 80 GB uses GB, which doesn't convert to tokens"),
        ("excloud.gpu_memory", "5 s", "value 5 s uses s, which doesn't convert to GB"),
        ("exbasic.price", "US$5", "value US$5 uses US$, which doesn't convert to AUD"),
        ("exbasic.price", "€5", "value €5 uses €, which doesn't convert to AUD"),
        ("exunits.count", "1.5", "value 1.5 is not a whole number"),
        ("excloud.gpu_memory", "128K GB", "value 128K GB uses K, which doesn't convert to GB"),
        ("exunits.count", "fast", "value fast is not a number"),
        ("exunits.site", "https://", "value https:// is not an http(s) URL"),
        ("exunits.region", "eu", "value eu is not one of the allowed values"),
        # ASCII digits only.
        ("excloud.gpu_memory", "８０GB", "value ８０GB is not a number"),
        ("exunits.count", "1٢", "value 1٢ uses ٢, but this key takes a bare number"),
        # A value below the float range would agree with 0.
        ("exunits.disk", TINY, f"value {TINY} is outside the supported range"),
    ]
    for key, raw, tail in cases:
        outcome = _apply(inst, "alex", _clm("clm-bad", key, raw, f"the value is {raw} ok", "src-refuse"))
        assert outcome.exit_code == 1
        assert _err(outcome) == f"op 0 (add_claim): {tail}", (key, raw)


TINY = "0." + "0" * 400 + "1"


def test_units_outside_the_table_are_labels(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    found = _unit_claims(inst, [("exunits.seats", "12"), ("exunits.seats", "12 seats")])
    assert [found["clm-u0"]["value_int"], found["clm-u1"]["value_int"]] == [12, 12]
    _ok(_apply(inst, "alex", _src("src-seats-k", "We sold 1.2K seats in total."),
               _clm("clm-seats-k", "exunits.seats", "1.2K seats", "We sold 1.2K seats in total", "src-seats-k",
                    supersedes=["clm-u0", "clm-u1"])))
    assert _claims(inst, "exunits.seats")["clm-seats-k"]["value_int"] == 1200
    outcome = _apply(inst, "alex", _clm("clm-tokens", "exunits.seats", "12 tokens", "We sold 12 tokens", "src-seats-k"))
    assert _err(outcome) == "op 0 (add_claim): value 12 tokens uses tokens, which doesn't convert to seats"


# -- claims, support, scope and versions ---------------------------------------------------------

LIMITS = "Limits page. Context window: 128K tokens."
SLACK = "Thread: max context on 1.3 is 131,072 now."


def _two_sides(inst: Path) -> None:
    """A dispute on excloud.max_context: a document says 128K, a Slack thread says 131,072."""
    _ok(_apply(inst, "alex", _src("src-docs-ec-limits", LIMITS, date="2026-09-01"),
               _src("src-slack-ec-0920", SLACK, kind="slack", date="2026-09-20")))
    _ok(_apply(inst, "notes", _clm("clm-a", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-limits")))
    _ok(_apply(inst, "bot", _clm("clm-b", CTX, "131,072", "max context on 1.3 is 131,072", "src-slack-ec-0920")))


def test_same_value_from_two_sources_is_support(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-s1", LIMITS),
               _src("src-s2", "Slack: the context window is 128K tokens.", kind="slack")))
    _ok(_apply(inst, "notes", _clm("clm-s1", CTX, "128K", "Context window: 128K tokens", "src-s1")))
    _ok(_apply(inst, "bot", _clm("clm-s2", CTX, "128K", "the context window is 128K tokens", "src-s2")))
    fact = _fact(inst, CTX)
    assert (fact["status"], fact["value_int"], fact["source_id"]) == ("current", 128000, "src-s1")
    values = _fact_get(inst, CTX, claims="all")["facts"][0]["claims"]["values"]
    assert [(entry["id"], entry["agree"]) for entry in values] == [("clm-s1", 1), ("clm-s2", 1)]
    assert all("suggested" not in entry for entry in values)
    assert _conflicts(inst, status=None) == []
    assert _disputes(inst) == []


def test_different_scopes_never_conflict(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-rel", "Release 1.2: max context is 64K tokens. Release 1.3: max context is 128K tokens.")))
    _ok(_apply(inst, "notes", _clm("clm-r12", CTX, "64K", "Release 1.2: max context is 64K tokens", "src-rel", scope="1.2"),
               _clm("clm-r13", CTX, "128K", "Release 1.3: max context is 128K tokens", "src-rel", scope="1.3",
                    locator="para 2")))
    assert _conflicts(inst, status=None) == []
    assert _fact(inst, CTX) is None
    assert _fact_error(inst, CTX) == (
        "no_value",
        "fact key excloud.max_context has no value now. Current version unknown. Values by version: "
        "1.2: 64000 tokens, 1.3: 128000 tokens. Pass scope to read one.",
    )
    assert _fact_error(inst, CTX, scope="1.4") == ("no_value", "fact key excloud.max_context has no value for scope 1.4")
    # At most 8 versions, then +<n> more; a prefix read with a scope counts only that scope.
    _ok(_apply(inst, "alex", _src("src-vs", " ".join(f"On v{i} it is {i + 1}K tokens." for i in range(8)))))
    _ok(_apply(inst, "notes", *[
        _clm(f"clm-vs{i}", CTX, f"{i + 1}K", f"On v{i} it is {i + 1}K tokens", "src-vs", scope=f"v{i}",
             locator=f"line {i}")
        for i in range(8)
    ]))
    assert _fact_error(inst, CTX) == (
        "no_value",
        "fact key excloud.max_context has no value now. Current version unknown. Values by version: "
        "1.2: 64000 tokens, 1.3: 128000 tokens, v0: 1000 tokens, v1: 2000 tokens, v2: 3000 tokens, "
        "v3: 4000 tokens, v4: 5000 tokens, v5: 6000 tokens, +2 more. Pass scope to read one.",
    )
    prefix = _fact_get(inst, "excloud.*", scope="v3")
    assert ([item["key"] for item in prefix["facts"]], prefix["total"], prefix["unset"]) == (
        [CTX], 1, ["excloud.current_version", "excloud.gpu_memory"]
    )
    assert prefix["facts"][0]["value"] == 4000 and prefix["facts"][0]["claims"]["scope"] == "v3"


def _history(inst: Path, key: str) -> list[tuple]:
    return ro_rows(
        inst,
        "SELECT version, change, status, value_text, value_int FROM fact_history WHERE key = ? ORDER BY version",
        (key,),
    )


def test_current_version_moves_the_headline(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex",
               _src("src-rel", "Release 1.2: max context is 64K tokens. Release 1.3: max context is 128K tokens."),
               _src("src-ver", "The current version is 1.3 for every region.", date="2026-09-25"),
               _src("src-ver-back", "We rolled back: the current version is 1.2 again.", date="2026-10-01")))
    _ok(_apply(inst, "notes", _clm("clm-r12", CTX, "64K", "Release 1.2: max context is 64K tokens", "src-rel", scope="1.2"),
               _clm("clm-r13", CTX, "128K", "Release 1.3: max context is 128K tokens", "src-rel", scope="1.3",
                    locator="para 2")))
    clock.at("2026-10-09T01:00:00Z")
    moved = _ok(_apply(inst, "alex", _clm("clm-v13", "excloud.current_version", "1.3",
                                          "The current version is 1.3 for every region", "src-ver")))
    assert {"table": "facts", "id": CTX, "action": "insert"} in moved.receipt["targets"]
    assert _history(inst, CTX) == [(1, "created", "current", None, 128000)]
    clock.at("2026-10-09T02:00:00Z")
    back = _ok(_apply(inst, "alex", _put(inst, "excloud.current_version", "1.2", "src-ver-back", basis="reported",
                                      quote="the current version is 1.2 again", raw="1.2")))
    assert {"table": "facts", "id": CTX, "action": "update"} in back.receipt["targets"]
    assert _history(inst, CTX)[-1] == (2, "updated", "current", None, 64000)
    assert _claims(inst, "excloud.current_version")["clm-v13"]["status"] == "superseded"
    assert "Changed in the last 7 days: 2 facts (excloud.current_version, excloud.max_context)." in _brief(inst)
    # A version label compares by its exact text: 1.3 and 1-3 are two values.
    _ok(_apply(inst, "alex", _src("src-cp-a", "The platform runs version 1.3 today."),
               _src("src-cp-b", "The platform runs version 1-3 today.")))
    _ok(_apply(inst, "alex", _clm("clm-cp-a", "vendor.compliance.current_version", "1.3",
                                  "The platform runs version 1.3 today", "src-cp-a")))
    _ok(_apply(inst, "notes", _clm("clm-cp-b", "vendor.compliance.current_version", "1-3",
                                   "The platform runs version 1-3 today", "src-cp-b")))
    assert _fact(inst, "vendor.compliance.current_version")["status"] == "disputed"
    assert len(_conflicts(inst, "vendor.compliance.current_version")) == 1


def test_scoped_version_claim_is_refused(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-ver", "The current version is 1.3 for every region. The beta is 1.3 beta.")))
    for key in ("excloud.current_version", "vendor.compliance.current_version"):
        outcome = _apply(inst, "alex", _clm("clm-bad", key, "1.3", "The current version is 1.3", "src-ver", scope="1.2"))
        assert _err(outcome) == f"op 0 (add_claim): {key} is a current_version key and must be unscoped"
    for scope in ("now", "NOW", "Now"):
        outcome = _apply(inst, "alex", _clm("clm-bad", CTX, "128K", "context is 128K tokens", "src-ver", scope=scope))
        assert _err(outcome) == "op 0 (add_claim): scope now is reserved"
    # A version value must be a scope label, or no scoped claim could ever match it.
    for raw in ("1.3 ", "1.3 beta", "now", "NOW", "-1.3", "v" * 41):
        outcome = _apply(inst, "alex", _clm("clm-bad", "excloud.current_version", raw, "The current version is 1.3",
                                            "src-ver"))
        assert _err(outcome) == "op 0 (add_claim): a current_version value must be a scope label", raw
    put = _apply(inst, "alex", _put(inst, "excloud.current_version", "1.3 beta", "src-ver", basis="reported",
                                    quote="The beta is 1.3 beta", raw="1.3 beta"))
    assert _err(put) == "op 0 (upsert_fact): a current_version value must be a scope label"
    _ok(_apply(inst, "alex", _clm("clm-good", "excloud.current_version", "1.3", "The current version is 1.3", "src-ver")))


def test_disputed_version_leaves_only_unscoped_in_force(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-v13", "The current version is 1.3 for every region."),
               _src("src-v14", "The current version is 1.4 for every region.", kind="slack"),
               _src("src-ctx", "Context window: 100K tokens. On 1.3 the context window is 128K tokens.")))
    _ok(_apply(inst, "alex", _clm("clm-v13", "excloud.current_version", "1.3", "The current version is 1.3", "src-v13")))
    _ok(_apply(inst, "notes", _clm("clm-plain", CTX, "100K", "Context window: 100K tokens", "src-ctx"),
               _clm("clm-on13", CTX, "128K", "On 1.3 the context window is 128K tokens", "src-ctx", scope="1.3",
                    locator="para 2")))
    assert _fact(inst, CTX)["status"] == "disputed"
    _ok(_apply(inst, "notes", _clm("clm-v14", "excloud.current_version", "1.4", "The current version is 1.4", "src-v14")))
    assert _fact(inst, "excloud.current_version")["status"] == "disputed"
    item = _fact_get(inst, CTX)["facts"][0]
    assert (item["value"], item["status"]) == (100000, "current")
    assert item["claims"]["current_version"] is None
    assert item["claims"]["other_scopes"] == {"1.3": 128000}
    assert _conflicts(inst, CTX) == []
    assert any(line.startswith("Disputed excloud.current_version (now): ") for line in _brief(inst))


def test_planned_never_conflicts(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex",
               _src("src-v1", "Version one: context window is 100K tokens.", date="2026-07-01"),
               _src("src-v2", "Version two: context window is 128K tokens.", date="2026-08-01", supersedes="src-v1"),
               _src("src-v3", "Version three: context window is 131,072 tokens.", date="2026-09-01", supersedes="src-v2"),
               _src("src-plan", "Roadmap: context window will be 256K tokens.", date="2026-09-05"),
               _src("src-v12", "Release 1.2: context window is 64K tokens.", date="2026-06-01")))
    clock.at("2026-10-09T01:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-v2", CTX, "128K", "context window is 128K tokens", "src-v2")))
    first = "2026-10-09T01:00:00Z"
    assert _fact(inst, CTX)["last_verified_at"] == first
    clock.at("2026-10-09T02:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-plan", CTX, "256K", "context window will be 256K tokens", "src-plan", planned=True)))
    clock.at("2026-10-09T03:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-s12", CTX, "64K", "context window is 64K tokens", "src-v12", scope="1.2")))
    clock.at("2026-10-09T04:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-v1", CTX, "100K", "context window is 100K tokens", "src-v1")))
    fact = _fact(inst, CTX)
    assert (fact["status"], fact["value_int"], fact["last_verified_at"]) == ("current", 128000, first)
    assert _claims(inst, CTX)["clm-v1"]["superseded_by"] == "clm-v2"
    item = _fact_get(inst, CTX)["facts"][0]
    assert item["claims"]["planned"] == [
        {"id": "clm-plan", "value": 256000, "raw": "256K",
         "source": {"id": "src-plan", "kind": "document", "date": "2026-09-05"},
         "quote": "context window will be 256K tokens"}
    ]
    clock.at("2026-10-09T05:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-v3", CTX, "131,072", "context window is 131,072 tokens", "src-v3")))
    fact = _fact(inst, CTX)
    assert (fact["value_int"], fact["last_verified_at"]) == (131072, "2026-10-09T05:00:00Z")
    assert _claims(inst, CTX)["clm-v2"]["superseded_by"] == "clm-v3"
    assert _conflicts(inst, status=None) == []


# -- update or conflict --------------------------------------------------------------------------


def _claim_states(inst: Path, key: str) -> dict[str, tuple]:
    return {cid: (claim["status"], claim["superseded_by"]) for cid, claim in _claims(inst, key).items()}


def test_rule_1_update_both_orders(tmp_path: Path, clock: Clock) -> None:
    ends = []
    for order in ("ab", "ba"):
        inst = _inst(tmp_path / order)
        _ok(_apply(inst, "alex", _src("src-ver-a", "Version A: context window is 64K tokens.", date="2026-08-01"),
                   _src("src-ver-b", "Version B: context window is 128K tokens.", date="2026-09-01",
                        supersedes="src-ver-a")))
        claims = {
            "a": ("notes", _clm("clm-ver-a", CTX, "64K", "context window is 64K tokens", "src-ver-a")),
            "b": ("bot", _clm("clm-ver-b", CTX, "128K", "context window is 128K tokens", "src-ver-b")),
        }
        for index, name in enumerate(order):
            clock.at(f"2026-10-09T0{index + 1}:00:00Z")
            agent, op = claims[name]
            _ok(_apply(inst, agent, op))
        fact = _fact(inst, CTX)
        ends.append((_claim_states(inst, CTX), fact["status"], fact["value_int"], _conflicts(inst, status=None)))
        changed = [line for line in _brief(inst) if line.startswith("Changed in the last 7 days")]
        assert changed == (["Changed in the last 7 days: 1 fact (excloud.max_context)."] if order == "ab" else [])
    assert ends[0] == ends[1] == (
        {"clm-ver-a": ("superseded", "clm-ver-b"), "clm-ver-b": ("live", None)}, "current", 128000, []
    )


def test_rule_1b_older_version_late_both_orders(tmp_path: Path, clock: Clock) -> None:
    ends = []
    for order in ("ca", "ac"):
        inst = _inst(tmp_path / order)
        _ok(_apply(inst, "alex",
                   _src("src-ver-a", "Version A: context window is 64K tokens.", date="2026-07-01"),
                   _src("src-ver-b", "Version B: context window is 100K tokens.", date="2026-08-01", supersedes="src-ver-a"),
                   _src("src-ver-c", "Version C: context window is 128K tokens.", date="2026-09-01", supersedes="src-ver-b")))
        claims = {
            "a": ("bot", _clm("clm-ver-a", CTX, "64K", "context window is 64K tokens", "src-ver-a")),
            "c": ("notes", _clm("clm-ver-c", CTX, "128K", "context window is 128K tokens", "src-ver-c")),
        }
        for index, name in enumerate(order):
            clock.at(f"2026-10-09T0{index + 1}:00:00Z")
            agent, op = claims[name]
            outcome = _ok(_apply(inst, agent, op))
            if order == "ca" and name == "a":
                assert not any(item["table"] == "signals" for item in outcome.receipt["targets"])
        fact = _fact(inst, CTX)
        ends.append((_claim_states(inst, CTX), fact["status"], fact["value_int"], _conflicts(inst, status=None)))
    assert ends[0] == ends[1] == (
        {"clm-ver-a": ("superseded", "clm-ver-c"), "clm-ver-c": ("live", None)}, "current", 128000, []
    )
    # A revert (C goes back to A's value) ends the same in every arrival order, because rules 1
    # and 1b apply whatever the values.
    import itertools

    values = {"a": "64K", "b": "128K", "c": "64K"}
    revert = set()
    for order in itertools.permutations("abc"):
        inst = _inst(tmp_path / ("revert-" + "".join(order)))
        _ok(_apply(inst, "alex",
                   _src("src-r-a", "Limits A. Context window: 64K tokens.", date="2026-07-01"),
                   _src("src-r-b", "Limits B. Context window: 128K tokens.", date="2026-08-01", supersedes="src-r-a"),
                   _src("src-r-c", "Limits C. Context window: 64K tokens.", date="2026-09-01", supersedes="src-r-b")))
        for name in order:
            _ok(_apply(inst, "notes", _clm(f"clm-r-{name}", CTX, values[name], f"Context window: {values[name]} tokens",
                                           f"src-r-{name}")))
        statuses = tuple(sorted((cid, claim["status"]) for cid, claim in _claims(inst, CTX).items()))
        fact = _fact(inst, CTX)
        revert.add((statuses, fact["status"], fact["value_int"], tuple(_conflicts(inst))))
    assert revert == {(
        (("clm-r-a", "superseded"), ("clm-r-b", "superseded"), ("clm-r-c", "live")), "current", 64000, ()
    )}


def test_version_move_compares_joining_claims(tmp_path: Path, clock: Clock) -> None:
    """Claims that join the now bucket when the version moves are compared under rules 1 and 1b."""
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex",
               _src("src-a", "Limits A: context window is 64K tokens."),
               _src("src-b", "Limits B: on 1.4 the context window is 256K tokens.", supersedes="src-a"),
               _src("src-v", "The current version is 1.3 for every region. The current version is 1.4 for every region.")))
    _ok(_apply(inst, "alex", _clm("clm-v13", "excloud.current_version", "1.3",
                                  "The current version is 1.3 for every region", "src-v")))
    _ok(_apply(inst, "notes", _clm("clm-a", CTX, "64K", "context window is 64K tokens", "src-a")))
    _ok(_apply(inst, "notes", _clm("clm-b", CTX, "256K", "on 1.4 the context window is 256K tokens", "src-b", scope="1.4")))
    assert (_fact(inst, CTX)["status"], _fact(inst, CTX)["value_int"]) == ("current", 64000)
    moved = _ok(_apply(inst, "alex", _clm("clm-v14", "excloud.current_version", "1.4",
                                          "The current version is 1.4 for every region", "src-v",
                                          supersedes=["clm-v13"], locator="para 2")))
    assert {"table": "claims", "id": "clm-a", "action": "update"} in moved.receipt["targets"]
    fact = _fact(inst, CTX)
    assert (fact["status"], fact["value_int"]) == ("current", 256000)
    assert _claim_states(inst, CTX) == {"clm-a": ("superseded", "clm-b"), "clm-b": ("live", None)}
    assert _conflicts(inst, status=None) == []
    assert _disputes(inst) == []


def _own_chain(tmp_path: Path, clock: Clock, order, claims: dict) -> tuple:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-own", "Q says 100K tokens. N says 110K tokens. P says 120K tokens. O says 90K tokens.")))
    for index, name in enumerate(order):
        clock.at(f"2026-10-09T0{index + 1}:00:00Z")
        _ok(_apply(inst, "notes", claims[name]))
    fact = _fact(inst, CTX)
    return _claim_states(inst, CTX), fact["status"], fact["value_int"], _conflicts(inst)


def test_rule_2_own_supersede_both_orders(tmp_path: Path, clock: Clock) -> None:
    import itertools

    pair = {
        "o": _clm("clm-o", CTX, "90K", "O says 90K tokens", "src-own"),
        "n": _clm("clm-n", CTX, "110K", "N says 110K tokens", "src-own", supersedes=["clm-o"], locator="para 2"),
    }
    ends = [_own_chain(tmp_path / f"pair-{order}", clock, order, pair) for order in ("on", "no")]
    assert ends[0] == ends[1] == ({"clm-n": ("live", None), "clm-o": ("superseded", "clm-n")}, "current", 110000, [])
    chain = {
        "q": _clm("clm-q", CTX, "100K", "Q says 100K tokens", "src-own"),
        "n": _clm("clm-n", CTX, "110K", "N says 110K tokens", "src-own", supersedes=["clm-q"], locator="para 2"),
        "p": _clm("clm-p", CTX, "120K", "P says 120K tokens", "src-own", supersedes=["clm-n"], locator="para 3"),
    }
    ends = [
        _own_chain(tmp_path / f"chain-{''.join(order)}", clock, order, chain)
        for order in itertools.permutations("qnp")
    ]
    assert len(ends) == 6
    expected = (
        {"clm-n": ("superseded", "clm-p"), "clm-p": ("live", None), "clm-q": ("superseded", "clm-n")},
        "current",
        120000,
        [],
    )
    assert all(end == expected for end in ends), ends


def test_rule_3_other_writer_supersede_is_held(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-own", "O says 90K tokens. N says 110K tokens."),
               _src("src-gpu", "O says 80 GB of VRAM. N says 96 GB of VRAM.")))
    _ok(_apply(inst, "notes", _clm("clm-o", CTX, "90K", "O says 90K tokens", "src-own"),
               _clm("clm-o2", "excloud.gpu_memory", "80 GB", "O says 80 GB of VRAM", "src-gpu")))
    reason = "op 0 (add_claim): superseding clm-o by notes needs a person's approval"
    path, change_id = _packet_file(
        inst, "bot", _clm("clm-n", CTX, "110K", "N says 110K tokens", "src-own", supersedes=["clm-o"], locator="para 2")
    )
    code, out, err = _cli("apply", path, "--instance", inst)
    assert code == 4, (out, err)
    assert err.strip() == f"brain: {reason}"
    assert json.loads(out)["status"] == "held" and json.loads(out)["error"] == ""
    assert "clm-n" not in _claims(inst, CTX)
    code, out, err = _cli("apply", path, "--instance", inst)  # A resubmit still names the reason
    assert (code, err.strip()) == (4, f"brain: {reason}")
    code, out, _stderr = _cli("held", "--instance", inst)
    assert code == 0
    held = json.loads(out)["held"]
    assert [(item["change_id"], item["reason"]) for item in held] == [(change_id, reason)]
    code, out, err = _cli("approve", change_id, "--as", "alex", "--instance", inst)
    assert code == 0, (out, err)
    assert _claim_states(inst, CTX) == {"clm-n": ("live", None), "clm-o": ("superseded", "clm-n")}
    assert ro_rows(inst, "SELECT status, hold_reason, reviewed_by FROM write_log WHERE change_id = ?", (change_id,)) == [
        ("applied", reason, "alex")
    ]
    solo = _apply(inst, "solo", _clm("clm-n2", "excloud.gpu_memory", "96 GB", "N says 96 GB of VRAM", "src-gpu",
                                     supersedes=["clm-o2"], locator="para 2"))
    assert solo.exit_code == 1
    assert _err(solo) == (
        "op 0 (add_claim): superseding clm-o2 by notes needs a person's approval, and agent solo has no approvers"
    )
    # SR12: every op runs first, so a later rejection decides the packet, not the earlier hold.
    mixed = _apply(inst, "bot",
                   _clm("clm-n3", "excloud.gpu_memory", "96 GB", "N says 96 GB of VRAM", "src-gpu",
                        supersedes=["clm-o2"], locator="para 3"),
                   _clm("clm-n4", "excloud.unknown", "96 GB", "N says 96 GB of VRAM", "src-gpu"))
    assert mixed.exit_code == 1
    assert _err(mixed) == "op 1 (add_claim): unknown fact key excloud.unknown"


def test_supersedes_id_from_another_agent_is_held(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    v1 = "Version 1 of the limits doc: context window is 64K tokens."
    v2 = "Version 2 of the limits doc: context window is 128K tokens."
    _ok(_apply(inst, "alex", _src("src-doc-v1", v1, date="2026-08-01")))
    held = _apply(inst, "notes", _src("src-doc-v2", v2, date="2026-09-01", supersedes="src-doc-v1"))
    assert held.exit_code == 4
    assert held.warning == "op 0 (add_source): supersedes_id needs a person's approval"
    assert ro_rows(inst, "SELECT count(*) FROM sources WHERE id = 'src-doc-v2'") == [(0,)]
    _ok(_apply(inst, "alex", _src("src-doc-v2", v2, date="2026-09-01", supersedes="src-doc-v1")))
    assert ro_rows(inst, "SELECT supersedes_id FROM sources WHERE id = 'src-doc-v2'") == [("src-doc-v1",)]
    # The same text added by notes without the link: its claim conflicts instead of updating.
    _ok(_apply(inst, "notes", _src("src-doc-v2b", v2, date="2026-09-01")))
    _ok(_apply(inst, "notes", _clm("clm-v1", CTX, "64K", "context window is 64K tokens", "src-doc-v1")))
    _ok(_apply(inst, "bot", _clm("clm-v2b", CTX, "128K", "context window is 128K tokens", "src-doc-v2b")))
    assert _fact(inst, CTX)["status"] == "disputed"
    assert _claim_states(inst, CTX) == {"clm-v1": ("live", None), "clm-v2b": ("live", None)}
    assert len(_conflicts(inst, CTX)) == 1
    missing = _apply(inst, "alex", _src("src-doc-v3", "Version 3.", supersedes="src-nope"))
    assert _err(missing) == "op 0 (add_source): supersedes_id src-nope does not exist"
    twice = _apply(inst, "alex", _src("src-doc-v3", "Version 3.", supersedes="src-doc-v1"))
    assert _err(twice) == "op 0 (add_source): source src-doc-v1 already has a successor"
    assert {name for name, ctype in REGISTRY.items() if ctype.may_supersede} == {"capture"}


def test_upsert_fact_is_shorthand(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-bot", "Bot observations. Context 100 tokens. Context 200 tokens. "
                                             "Context 300 tokens.", kind="observation"),
               _src("src-notes", "Notes observations. Context 200 tokens.", kind="user_note")))
    _ok(_apply(inst, "bot", _put(inst, CTX, 100, "src-bot", quote="Context 100 tokens", raw="100")))
    _ok(_apply(inst, "bot", _put(inst, CTX, 200, "src-bot", quote="Context 200 tokens", raw="200")))
    claims = _claims(inst, CTX)
    assert len(claims) == 2
    first, second = sorted(claims.values(), key=lambda claim: claim["created_at"] + claim["id"])
    states = {claim["value_int"]: (claim["status"], claim["superseded_by"]) for claim in claims.values()}
    assert states[100][0] == "superseded" and states[200] == ("live", None)
    assert states[100][1] == next(cid for cid, claim in claims.items() if claim["value_int"] == 200)
    assert (_fact(inst, CTX)["value_int"], _fact(inst, CTX)["status"]) == (200, "current")
    again = _ok(_apply(inst, "bot", _put(inst, CTX, 200, "src-bot", verified="2026-10-05T00:00:00Z",
                                         quote="Context 200 tokens", raw="200")))
    assert len(_claims(inst, CTX)) == 2
    assert not any(item["table"] == "claims" for item in again.receipt["targets"])
    assert _fact(inst, CTX)["last_verified_at"] == "2026-10-05T00:00:00Z"
    refused = _apply(inst, "bot", _put(inst, CTX, 200, "src-bot", status="disputed", quote="Context 200 tokens",
                                       raw="200"))
    assert _err(refused) == "op 0 (upsert_fact): status is worked out from claims; a packet can't set disputed"
    _ok(_apply(inst, "notes", _put(inst, CTX, 200, "src-notes", quote="Context 200 tokens", raw="200")))
    _ok(_apply(inst, "bot", _put(inst, CTX, None, "src-bot", status="retired")))
    live = {claim["proposed_by"] for claim in _claims(inst, CTX).values() if claim["status"] == "live"}
    assert live == {"notes"}
    assert _fact(inst, CTX)["status"] == "current"
    _ok(_apply(inst, "solo", _put(inst, CTX, 300, "src-bot", quote="Context 300 tokens", raw="300")))
    assert _fact(inst, CTX)["status"] == "disputed"
    assert len(_conflicts(inst, CTX)) == 1


OBSERVED = "Observed 131072 tokens"


def _settled_against_bot(tmp_path: Path, clock: Clock) -> tuple[Path, str]:
    """bot observes 131,072, notes cites 128K, and a person keeps 128K and rejects bot's claim."""
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex",
               *[_src(f"src-run{n}", f"Run {n}. Observed 131072 tokens. Observed 140000 tokens.", kind="api")
                 for n in range(1, 5)],
               _src("src-doc", "Limits page: context window is 128K tokens."),
               _src("src-doc2", "Limits page v2: context window is 100K tokens."),
               _src("src-alex", "Alex: the limits page is right, the context window is 128K tokens.", kind="user_note",
                    author="per-alex-example")))
    _ok(_apply(inst, "bot", _put(inst, CTX, 131072, "src-run1", quote=OBSERVED, raw="131072")))
    _ok(_apply(inst, "notes", _clm("clm-doc", CTX, "128K", "context window is 128K tokens", "src-doc")))
    assert _fact(inst, CTX)["status"] == "disputed"
    observed = next(cid for cid, claim in _claims(inst, CTX).items() if claim["proposed_by"] == "bot")
    _ok(_apply(inst, "alex", _resolve(CTX, ["clm-doc"], [observed],
                                      "the limits page is right, the context window is 128K tokens", "src-alex")))
    assert _fact(inst, CTX)["status"] == "current"
    return inst, observed


def test_rejected_value_from_its_writer_stays_rejected(tmp_path: Path, clock: Clock) -> None:
    """While a person's settlement stands, a writer's re-assertion of the
    value the person rejected records nothing and changes nothing, and says so. Once the settlement no
    longer stands, the same value is recorded and judged as usual. The settled value comes from the
    resolving packet, never from timestamps, and a retired fact keeps the settlement."""
    inst, observed = _settled_against_bot(tmp_path / "one", clock)
    before, count = _fact(inst, CTX), len(_claims(inst, CTX))
    clock.at("2026-10-20T00:00:00Z")
    again = _ok(_apply(inst, "bot", _put(inst, CTX, 131072, "src-run2", verified="2026-10-20T00:00:00Z", quote=OBSERVED,
                                         raw="131072")))
    assert again.exit_code == 0 and again.receipt["targets"] == []
    assert again.warning == f"op 0 (upsert_fact): value 131072 was rejected by a person ({observed}), so nothing was recorded"
    assert _fact(inst, CTX) == before  # not even last_verified_at moves
    assert len(_claims(inst, CTX)) == count and _conflicts(inst) == []
    # The writer of the kept value moves on (rule 2): the settlement no longer stands, so bot's
    # 131,072 is a real disagreement with 100K and disputes.
    _ok(_apply(inst, "notes", _clm("clm-doc2", CTX, "100K", "context window is 100K tokens", "src-doc2",
                                   supersedes=["clm-doc"])))
    assert (_fact(inst, CTX)["status"], _fact(inst, CTX)["value_int"]) == ("current", 100000)
    clock.at("2026-10-21T00:00:00Z")
    disagree = _ok(_apply(inst, "bot", _put(inst, CTX, 131072, "src-run3", verified="2026-10-21T00:00:00Z", quote=OBSERVED,
                                            raw="131072")))
    assert disagree.warning == ""
    assert len(_claims(inst, CTX)) == count + 2
    assert _fact(inst, CTX)["status"] == "disputed"
    assert len(_conflicts(inst, CTX)) == 1
    # A different value from the same writer is recorded and judged as usual.
    other, _observed = _settled_against_bot(tmp_path / "two", clock)
    count = len(_claims(other, CTX))
    _ok(_apply(other, "bot", _put(other, CTX, 140000, "src-run4", quote="Observed 140000 tokens", raw="140000")))
    assert len(_claims(other, CTX)) == count + 1
    assert _fact(other, CTX)["status"] == "disputed"
    assert len(_conflicts(other, CTX)) == 1
    # The kept side moves in the same second as the resolution. A same-second change must not
    # hide the disagreement, so bot's 131,072 the next day is recorded and disputes.
    same, _observed = _settled_against_bot(tmp_path / "three", clock)
    _ok(_apply(same, "notes", _clm("clm-doc2", CTX, "100K", "context window is 100K tokens", "src-doc2",
                                   supersedes=["clm-doc"])))
    assert (_fact(same, CTX)["status"], _fact(same, CTX)["value_int"]) == ("current", 100000)
    count = len(_claims(same, CTX))
    clock.at("2026-10-10T00:00:00Z")
    moved = _ok(_apply(same, "bot", _put(same, CTX, 131072, "src-run2", verified="2026-10-10T00:00:00Z", quote=OBSERVED,
                                         raw="131072")))
    assert moved.warning == "" and len(_claims(same, CTX)) == count + 1
    assert _fact(same, CTX)["status"] == "disputed"
    assert len(_conflicts(same, CTX)) == 1
    # The kept side retires, so no live claim stands. bot's rejected value must not become the
    # undisputed headline: it is suppressed and the fact stays retired.
    clock.at(NOW)
    retired, observed = _settled_against_bot(tmp_path / "four", clock)
    clock.at("2026-10-09T01:00:00Z")
    _ok(_apply(retired, "notes", _put(retired, CTX, None, "src-doc", status="retired")))
    before, count = _fact(retired, CTX), len(_claims(retired, CTX))
    assert before["status"] == "retired"
    clock.at("2026-10-09T02:00:00Z")
    again = _ok(_apply(retired, "bot", _put(retired, CTX, 131072, "src-run2", verified="2026-10-09T02:00:00Z", quote=OBSERVED,
                                            raw="131072")))
    assert again.receipt["targets"] == []
    assert again.warning == f"op 0 (upsert_fact): value 131072 was rejected by a person ({observed}), so nothing was recorded"
    assert _fact(retired, CTX) == before and len(_claims(retired, CTX)) == count
    assert _conflicts(retired) == []
    # Once the fact is current with another value, the settlement no longer stands and bot disputes.
    clock.at("2026-10-09T03:00:00Z")
    _ok(_apply(retired, "notes", _clm("clm-doc2", CTX, "100K", "context window is 100K tokens", "src-doc2")))
    assert (_fact(retired, CTX)["status"], _fact(retired, CTX)["value_int"]) == ("current", 100000)
    clock.at("2026-10-09T04:00:00Z")
    _ok(_apply(retired, "bot", _put(retired, CTX, 131072, "src-run3", verified="2026-10-09T04:00:00Z", quote=OBSERVED,
                                    raw="131072")))
    assert len(_claims(retired, CTX)) == count + 2
    assert _fact(retired, CTX)["status"] == "disputed"
    assert len(_conflicts(retired, CTX)) == 1


BATCH_KEYS = [f"exbatch{n:03}.max_context" for n in range(300)]
SETTLED = "the limits page is right, the context window is 128K tokens"


def _batch_against_one_resolution(tmp_path: Path, clock: Clock) -> Path:
    """300 keys where one large resolution kept notes' 128K and rejected bot's 131,072; on every
    tenth key notes has since moved to 100K, so that settlement no longer stands."""
    inst = _inst(tmp_path, truth_config(keys=KEYS + [
        {"key": key, "value_type": "int", "unit": "tokens"} for key in BATCH_KEYS]))
    _ok(_apply(inst, "alex", _src("src-run1", f"Run 1. {OBSERVED}.", kind="api"),
               _src("src-run2", f"Run 2. {OBSERVED}.", kind="api"),
               _src("src-doc", "Limits page: context window is 128K tokens."),
               _src("src-doc2", "Limits page v2: context window is 100K tokens.")))
    _ok(_apply(inst, "bot", *[_put(inst, key, 131072, "src-run1", quote=OBSERVED, raw="131072") for key in BATCH_KEYS]))
    _ok(_apply(inst, "notes", *[_clm(f"clm-b{n:03}", key, "128K", "context window is 128K tokens", "src-doc")
                                for n, key in enumerate(BATCH_KEYS)]))
    observed = dict(tuple(row) for row in ro_rows(inst, "SELECT key, id FROM claims WHERE proposed_by = 'bot'"))
    padding = "Synthetic padding from a long review note. " * 46_000
    notes = [_src(f"src-alex{n}", f"Alex, note {n}: {SETTLED}. {padding}"[:2_000_000], kind="user_note",
                  author="per-alex-example") for n in (1, 2)]
    _ok(_apply(inst, "alex", *notes, *[_resolve(key, [f"clm-b{n:03}"], [observed[key]], SETTLED, "src-alex1")
                                       for n, key in enumerate(BATCH_KEYS)]))
    clock.at("2026-10-09T01:00:00Z")
    _ok(_apply(inst, "notes", *[_clm(f"clm-c{n:03}", key, "100K", "context window is 100K tokens", "src-doc2",
                                     supersedes=[f"clm-b{n:03}"]) for n, key in enumerate(BATCH_KEYS) if n % 10 == 0]))
    return inst


def test_reassertions_against_one_resolution_read_its_packet_once(tmp_path: Path, clock: Clock,
                                                                  monkeypatch) -> None:
    """Fix round 4: a batch of 300 re-assertions against one large resolution parses the resolving
    packet once per apply call, finishes within 2 s, and gives exactly the uncached result."""
    inst = _batch_against_one_resolution(tmp_path / "cached", clock)
    other = tmp_path / "uncached"
    shutil.copytree(inst, other)
    clock.at("2026-10-09T02:00:00Z")
    packet = json.dumps(make_packet("bot", *[_put(inst, key, 131072, "src-run2", verified="2026-10-09T02:00:00Z",
                                                  quote=OBSERVED, raw="131072")
                                             for key in BATCH_KEYS])).encode()
    reads_of_packets: list[str] = []
    writer = Applier(inst)
    writer.con.set_trace_callback(
        lambda sql: reads_of_packets.append(sql) if "SELECT packet_json FROM write_log" in sql else None)
    try:
        started = time.perf_counter()
        cached = writer.apply(packet)
        took = time.perf_counter() - started
        assert writer._packets is None  # dropped at the end of the call, so it can't go stale
    finally:
        writer.close()
    _ok(cached)
    assert took < 2.0, took
    assert len(reads_of_packets) == 1, len(reads_of_packets)
    notes = cached.warning.split("; ")
    assert len(notes) == 270 and all(note.endswith("so nothing was recorded") for note in notes)
    moved = [key for n, key in enumerate(BATCH_KEYS) if n % 10 == 0]
    assert [key for key in BATCH_KEYS if _fact(inst, key)["status"] == "disputed"] == moved
    # The same packet through the uncached path, on an identical copy of the store.
    packet_ops = Applier._packet_ops

    def uncached(self, change_id):
        self._packets = None
        return packet_ops(self, change_id)

    monkeypatch.setattr(Applier, "_packet_ops", uncached)
    reads_of_packets.clear()
    writer = Applier(other)
    writer.con.set_trace_callback(
        lambda sql: reads_of_packets.append(sql) if "SELECT packet_json FROM write_log" in sql else None)
    try:
        plain = writer.apply(packet)
    finally:
        writer.close()
    assert len(reads_of_packets) == 300
    assert (cached.exit_code, cached.receipt, cached.warning) == (plain.exit_code, plain.receipt, plain.warning)

    def dump(instance: Path) -> dict:
        return {table: [tuple(row) for row in ro_rows(instance, f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("facts", "fact_history", "claims", "signals")}

    assert dump(inst) == dump(other)


def test_collector_reverify_updates_last_verified_at_only(tmp_path: Path, clock: Clock) -> None:
    from tests.collector_fixtures import collect, collector_instance, github_doc

    inst = collector_instance(tmp_path / "gh", "repo")
    code, payload, err = collect(inst, "repo", github_doc())
    assert code == 0, (payload, err)
    keys = ("eng.repo.ci_state", "eng.repo.last_push_on", "eng.repo.open_pull_requests")
    before = {key: _fact(inst, key) for key in keys}
    assert all(fact["version"] == 1 for fact in before.values())
    count = ro_rows(inst, "SELECT count(*) FROM claims")[0][0]
    assert count == 3
    code, payload, err = collect(inst, "repo", github_doc(fetched_at="2026-10-08T06:00:00Z"))
    assert code == 0, (payload, err)
    assert ro_rows(inst, "SELECT count(*) FROM claims")[0][0] == count
    for key in keys:
        after = _fact(inst, key)
        assert after["source_id"] == before[key]["source_id"] == next(
            claim["source_id"] for claim in _claims(inst, key).values()
        )
        assert (after["version"], after["last_verified_at"]) == (2, "2026-10-08T06:00:00Z")
    # Outranked: an observed claim from a document outranks the collector's api batch source.
    _ok(_apply(inst, "tester",
               _src("src-ci-export", "CI dashboard export: the build state is failure.", date="2026-10-08"),
               _clm("clm-ci-export", "eng.repo.ci_state", "failure", "the build state is failure", "src-ci-export",
                    basis="observed")))
    assert (_fact(inst, "eng.repo.ci_state")["status"], _fact(inst, "eng.repo.ci_state")["value_text"]) == (
        "disputed", "failure"
    )
    # The outranking claim refreshed last_verified_at to the decision time, so the next
    # observation comes after it.
    failing = github_doc(fetched_at="2026-10-09T00:04:00Z")
    failing["runs"][0]["conclusion"] = "failure"
    code, payload, err = collect(inst, "repo", failing)
    assert code == 0, (payload, err)
    fact = _fact(inst, "eng.repo.ci_state")
    assert (fact["status"], fact["value_text"]) == ("current", "failure")
    live = [claim for claim in _claims(inst, "eng.repo.ci_state").values() if claim["status"] == "live"]
    assert sorted(claim["proposed_by"] for claim in live) == ["repo", "tester"]
    assert _conflicts(inst, "eng.repo.ci_state") == []


# -- conflicts and resolution --------------------------------------------------------------------


def test_conflict_sets_disputed_opens_one_signal_and_a_brief_line(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    assert _fact(inst, CTX)["status"] == "disputed"
    sig = _sig(CTX, "now")
    assert _conflicts(inst) == [(sig, CTX, "values disagree (now)", "open")]
    assert ro_rows(inst, "SELECT kind, subject_table, basis, version, source_id, thread_id FROM signals WHERE id = ?",
                   (sig,)) == [("conflict", "facts", "observed", 1, "src-slack-ec-0920", None)]
    _ok(_apply(inst, "alex", _src("src-call", "On the call: the context window is 120K tokens.", kind="meeting_transcript")))
    _ok(_apply(inst, "solo", _clm("clm-c3", CTX, "120K", "the context window is 120K tokens", "src-call")))
    assert [row[0] for row in _conflicts(inst)] == [sig]
    assert _disputes(inst) == [
        "Disputed excloud.max_context (now): 128000 tokens, document 2026-09-01 | 131072 tokens, slack 2026-09-20 "
        "| +1 more. Ask a person."
    ]
    # Two claims scoped to the current version open one signal, labelled now.
    _ok(_apply(inst, "alex", _src("src-ver", "The current version is 1.3 for every region."),
               _src("src-gpu-doc", "On 1.3 each node has 80 GB of VRAM."),
               _src("src-gpu-slack", "On 1.3 each node has 96 GB of VRAM, says the thread.", kind="slack")))
    _ok(_apply(inst, "alex", _clm("clm-v13", "excloud.current_version", "1.3", "The current version is 1.3", "src-ver")))
    _ok(_apply(inst, "notes", _clm("clm-g80", "excloud.gpu_memory", "80 GB", "each node has 80 GB of VRAM", "src-gpu-doc",
                                   scope="1.3")))
    _ok(_apply(inst, "bot", _clm("clm-g96", "excloud.gpu_memory", "96 GB", "each node has 96 GB of VRAM", "src-gpu-slack",
                                 scope="1.3")))
    gpu = _conflicts(inst, "excloud.gpu_memory")
    assert gpu == [(_sig("excloud.gpu_memory", "now"), "excloud.gpu_memory", "values disagree (now)", "open")]
    assert [line for line in _disputes(inst) if "gpu_memory" in line] == [
        "Disputed excloud.gpu_memory (now): 80 GB, document 2026-09-01 | 96 GB, slack 2026-09-01. Ask a person."
    ]


def test_packets_cannot_create_or_change_conflict_signals(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    sig = _sig(CTX, "now")
    refusal = "op 0 (upsert_signal): conflict signals are opened and closed only by the applier"
    base = {"subject_table": "facts", "subject_id": CTX, "basis": "observed"}
    attempts = [
        upsert_op("upsert_signal", {"id": "sig-mine", "kind": "conflict", **base}, "src-docs-ec-limits"),
        upsert_op("upsert_signal", {"id": sig, "status": "dismissed", "resolved_at": NOW}, "src-docs-ec-limits", base=1),
        upsert_op("upsert_signal", {"id": sig, "detail": "nothing to see"}, "src-docs-ec-limits", base=1),
    ]
    for op in attempts:
        assert _err(_apply(inst, "bot", op)) == refusal
    reserved = _apply(inst, "bot", upsert_op(
        "upsert_signal", {"id": "sig-conflict-0123456789abcdef-1", "kind": "other", **base}, "src-docs-ec-limits"
    ))
    assert _err(reserved) == "op 0 (upsert_signal): signal ids starting sig-conflict- are reserved for the applier"
    assert ro_rows(inst, "SELECT status, version FROM signals WHERE id = ?", (sig,)) == [("open", 1)]


SOURCE_RANK = (
    '\n[truth.source_rank]\nkinds = ["slack", "document", "api", "observation", "config", "web", "email", '
    '"user_note", "meeting_transcript", "calendar"]\n'
)


def test_ranking_suggests_but_never_resolves(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    sig = _sig(CTX, "now")

    def suggested() -> str:
        values = _fact_get(inst, CTX)["facts"][0]["claims"]["values"]
        return next(entry["id"] for entry in values if entry.get("suggested"))

    assert suggested() == "clm-a"
    assert _disputes(inst)[0].startswith("Disputed excloud.max_context (now): 128000 tokens, document")
    before = _fact(inst, CTX)
    (inst / "brain.toml").write_text(truth_config(truth_block=TRUTH_BLOCK + SOURCE_RANK), encoding="utf-8")
    assert suggested() == "clm-b"
    assert _disputes(inst)[0].startswith("Disputed excloud.max_context (now): 131072 tokens, slack")
    assert _fact(inst, CTX) == before  # a rank change alone writes nothing
    clock.at("2026-10-09T01:00:00Z")
    _ok(_apply(inst, "bot", _put(inst, CTX, None, "src-docs-ec-limits", basis=None, as_of=None,
                                 verified="2026-10-09T01:00:00Z")))
    after = _fact(inst, CTX)
    assert (after["source_id"], after["value_int"], after["status"]) == ("src-slack-ec-0920", 131072, "disputed")
    assert after["version"] == before["version"] + 1
    assert _history(inst, CTX)[-1] == (after["version"], "updated", "disputed", None, 131072)
    assert [row[0] for row in _conflicts(inst, status=None)] == [sig]
    assert {claim["status"] for claim in _claims(inst, CTX).values()} == {"live"}
    prefixes = '\nuri_prefixes = ["https://docs.example.com/"]\n'
    (inst / "brain.toml").write_text(truth_config(truth_block=TRUTH_BLOCK + SOURCE_RANK + prefixes), encoding="utf-8")
    _ok(_apply(inst, "alex", _src("src-cal", "Calendar note: context window is 120K tokens.", kind="calendar",
                                  uri="https://docs.example.com/limits")))
    _ok(_apply(inst, "solo", _clm("clm-cal", CTX, "120K", "context window is 120K tokens", "src-cal")))
    assert suggested() == "clm-cal"
    assert [row[0] for row in _conflicts(inst, status=None)] == [sig]


def test_resolve_fact_refusals(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _people(inst, ("per-sam-example", "Sam Example"))
    _two_sides(inst)
    _ok(_apply(inst, "alex",
               _src("src-sam", "Sam says the context window is 128K tokens for everyone.", kind="user_note",
                    author="per-sam-example"),
               _src("src-anon", "The context window is 128K tokens, says the doc.", kind="user_note"),
               _src("src-alex", "Alex: it is 128K tokens.", kind="user_note", author="per-alex-example"),
               _src("src-gpu", "Each node has 80GB of GPU memory.")))
    _ok(_apply(inst, "notes", *[
        _clm(f"clm-g{index:02d}", "excloud.gpu_memory", "80GB", "Each node has 80GB of GPU memory", "src-gpu",
             locator=f"line {index}")
        for index in range(41)
    ]))
    _ok(_apply(inst, "notes",
               _clm("clm-plan", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-limits", planned=True,
                    locator="para 7"),
               _clm("clm-s13", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-limits", scope="1.3",
                    locator="para 8")))
    quote = "the context window is 128K tokens"
    cases = [
        (_resolve("excloud.nothing", ["clm-a"], ["clm-b"], quote, "src-sam"), "unknown fact key excloud.nothing"),
        # A re-read can't fix these, so they exit 1 rather than 3.
        (_resolve(CTX, ["clm-a", "clm-plan"], ["clm-b"], quote, "src-sam"), "claim clm-plan is planned"),
        (_resolve(CTX, ["clm-a", "clm-s13"], ["clm-b"], quote, "src-sam"), "claim clm-s13 is in 1.3, not now"),
        (_resolve(CTX, ["clm-a"], ["clm-b"], quote, "src-missing"), advice.source_missing_error("src-missing")),
        (_resolve(CTX, ["clm-a"], ["clm-a", "clm-b"], quote, "src-sam"), "claim clm-a is in both keep and reject"),
        (_resolve(CTX, ["clm-a", "clm-zz"], ["clm-b"], quote, "src-sam"), "claim clm-zz does not exist"),
        (_resolve(CTX, ["clm-a", "clm-g00"], ["clm-b"], quote, "src-sam"),
         "claim clm-g00 is not a claim on excloud.max_context"),
        (_resolve("excloud.gpu_memory", ["clm-g00"], [], "Each node has 80GB of GPU memory", "src-gpu"),
         "now has 41 live claims, more than one resolve_fact can name (40)"),
        (_resolve(CTX, ["clm-a", "clm-b"], [], quote, "src-sam"), "kept claims hold different values"),
        (_resolve(CTX, ["clm-a"], ["clm-b"], "words that are nowhere in it", "src-sam"), "quote is not in source src-sam"),
        (_resolve(CTX, ["clm-a"], ["clm-b"], "Sam says the context window is", "src-sam"),
         "the quote holds no kept claim's value"),
        (_resolve(CTX, ["clm-a"], ["clm-b"], quote, "src-anon"), "source src-anon has no named person as author"),
        (_resolve(CTX, ["clm-a"], ["clm-b"], "it is 128K tokens", "src-alex"),
         "a quote from the principal needs at least 5 words"),
    ]
    for op, message in cases:
        outcome = _apply(inst, "alex", op)
        assert outcome.exit_code == 1, message
        assert _err(outcome) == f"op 0 (resolve_fact): {message}"
    assert _fact(inst, CTX)["status"] == "disputed"
    # On a bool or enum key, the quote must name the kept value, not negated. A resolution
    # names the side it rejects, so the other value's words don't refuse it.
    sso = ('\n[[fact_keys]]\nkey = "exbasic.sso"\nvalue_type = "bool"\n'
           'value_words = {true = ["included", "enabled"], false = ["not offered", "not included", "not enabled"]}\n')
    inst = _inst(tmp_path / "sso", truth_config() + sso)
    _ok(_apply(inst, "alex",
               _src("src-sso", "Plan notes. SSO is included on Pro. A note says SSO is not included on Pro."),
               _src("src-alex-sso", "Alex: SSO is included on Pro. The note that says it is not included is out of "
                                    "date. SSO is not enabled on Basic. SSO is never included on Basic. There is no doubt that SSO is "
                                    "included on Pro. Yes, the note is right.", kind="user_note",
                    author="per-alex-example")))
    _ok(_apply(inst, "bot", _clm("clm-in", "exbasic.sso", "included", "SSO is included on Pro", "src-sso")))
    _ok(_apply(inst, "notes", _clm("clm-out", "exbasic.sso", "not included", "A note says SSO is not included on Pro",
                                   "src-sso")))
    assert _fact(inst, "exbasic.sso")["status"] == "disputed"
    for keep, reject, quote in (("clm-in", "clm-out", "SSO is not enabled on Basic"),
                                ("clm-in", "clm-out", "SSO is never included on Basic"),
                                # A bare yes or no names no value in a resolution.
                                ("clm-out", "clm-in", "There is no doubt that SSO is included on Pro"),
                                ("clm-in", "clm-out", "Yes, the note is right")):
        refused = _resolve("exbasic.sso", [keep], [reject], quote, "src-alex-sso")
        assert _err(_apply(inst, "alex", refused)) == "op 0 (resolve_fact): the quote holds no kept claim's value", quote
    honest = _resolve("exbasic.sso", ["clm-in"], ["clm-out"],
                      "SSO is included on Pro. The note that says it is not included is out of date", "src-alex-sso")
    _ok(_apply(inst, "alex", honest))
    fact = _fact(inst, "exbasic.sso")
    assert (fact["status"], fact["value_int"]) == ("current", 1)


def _settle_packet() -> dict:
    return _resolve(CTX, ["clm-a", "clm-a2"], ["clm-b"], "the docs are right, the context window is 128K tokens",
                    "src-note-alex-1003")


def _alex_note(inst: Path) -> None:
    _ok(_apply(inst, "alex", _src("src-note-alex-1003",
                                  "Message 1: the docs are right, the context window is 128K tokens.",
                                  kind="user_note", date="2026-10-03", author="per-alex-example")))


def test_resolve_fact_settles_and_closes_the_signal(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _example_state(inst)
    _alex_note(inst)
    sig = _sig(CTX, "now")
    assert _conflicts(inst) == [(sig, CTX, "values disagree (now)", "open")]
    assert any(line.startswith("Disputed excloud.max_context (now): ") for line in _disputes(inst))
    clock.at("2026-10-09T01:00:00Z")
    _ok(_apply(inst, "alex", _settle_packet()))
    fact = _fact(inst, CTX)
    assert (fact["status"], fact["value_int"], fact["source_id"]) == ("current", 128000, "src-docs-ec-limits")
    rejected = _claims(inst, CTX)["clm-b"]
    assert (rejected["status"], rejected["resolution_source_id"], rejected["resolution_locator"]) == (
        "rejected", "src-note-alex-1003", "message 1"
    )
    assert ro_rows(inst, "SELECT status, resolved_at, version FROM signals WHERE id = ?", (sig,)) == [
        ("resolved", "2026-10-09T01:00:00Z", 2)
    ]
    assert _disputes(inst) == []


def test_resolve_fact_race_exits_3(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    _alex_note(inst)
    written = _resolve(CTX, ["clm-a"], ["clm-b"], "the docs are right, the context window is 128K tokens",
                       "src-note-alex-1003")
    _ok(_apply(inst, "alex", _src("src-docs-ec-old", "Old limits page. Context window: 128K tokens.", date="2026-08-01")))
    _ok(_apply(inst, "notes", _clm("clm-a2", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-old")))
    raced = _apply(inst, "alex", written)
    assert raced.exit_code == 3
    assert _err(raced, "conflict") == (
        "op 0 (resolve_fact): claim clm-a2 is live in now but in neither keep nor reject; "
        "re-read with claims all and resubmit"
    )
    auto = _fact_get(inst, CTX)["facts"][0]["claims"]["values"]
    assert [(entry["id"], entry["agree"]) for entry in auto] == [("clm-a", 1), ("clm-b", 0)]
    every = _fact_get(inst, CTX, claims="all")["facts"][0]["claims"]["values"]
    assert [entry["id"] for entry in every] == ["clm-a", "clm-a2", "clm-b"]
    # A named claim superseded meanwhile by its writer.
    _ok(_apply(inst, "alex", _src("src-slack-ec-0921", "Thread: correction, max context is 132,000.", kind="slack",
                                  date="2026-09-21")))
    _ok(_apply(inst, "bot", _clm("clm-b2", CTX, "132,000", "correction, max context is 132,000", "src-slack-ec-0921",
                                 supersedes=["clm-b"])))
    again = _apply(inst, "alex", _settle_packet())
    assert again.exit_code == 3
    assert _err(again, "conflict") == (
        "op 0 (resolve_fact): claim clm-b is no longer live in now; re-read with claims all and resubmit"
    )
    every = _fact_get(inst, CTX, claims="all")["facts"][0]["claims"]["values"]
    keep = [entry["id"] for entry in every if entry["value"] == 128000]
    reject = [entry["id"] for entry in every if entry["value"] != 128000]
    assert (keep, reject) == (["clm-a", "clm-a2"], ["clm-b2"])
    _ok(_apply(inst, "alex", _resolve(CTX, keep, reject, "the docs are right, the context window is 128K tokens",
                                      "src-note-alex-1003")))
    assert _fact(inst, CTX)["status"] == "current"
    assert _conflicts(inst) == []


def test_resolve_fact_from_another_agent_is_held(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _people(inst, ("per-sam-example", "Sam Example"))
    _two_sides(inst)
    _ok(_apply(inst, "alex", _src("src-sam", "Sam says the context window is 128K tokens for everyone.",
                                  kind="user_note", author="per-sam-example")))
    op = _resolve(CTX, ["clm-a"], ["clm-b"], "the context window is 128K tokens", "src-sam")
    held = _apply(inst, "bot", op)
    assert held.exit_code == 4
    assert held.warning == "op 0 (resolve_fact): needs a person's approval"
    assert _fact(inst, CTX)["status"] == "disputed"
    writer = Applier(inst)
    try:
        approved = writer.approve(held.receipt["change_id"], "alex")
    finally:
        writer.close()
    assert approved.exit_code == 0, approved.receipt
    assert _fact(inst, CTX)["status"] == "current"
    assert _claims(inst, CTX)["clm-b"]["status"] == "rejected"
    _ok(_apply(inst, "alex", _src("src-slack-later", "Thread: no, it is 131,072.", kind="slack")))
    _ok(_apply(inst, "bot", _clm("clm-b3", CTX, "131,072", "no, it is 131,072", "src-slack-later")))
    solo = _apply(inst, "solo", _resolve(CTX, ["clm-a"], ["clm-b3"], "the context window is 128K tokens", "src-sam"))
    assert solo.exit_code == 1
    assert _err(solo) == "op 0 (resolve_fact): needs a person's approval, and agent solo has no approvers"


def test_claim_matching_a_rejected_claim_is_refused(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _example_state(inst)
    _alex_note(inst)
    _ok(_apply(inst, "alex", _settle_packet()))
    again = _apply(inst, "bot", _clm("clm-b9", CTX, "131,072", "max context on 1.3 is 131,072", "src-slack-ec-0920",
                                     scope="1.3"))
    assert _err(again) == (
        "op 0 (add_claim): a claim with this key, scope, source, locator and value was rejected (clm-b)"
    )
    same = _ok(_apply(inst, "bot", _clm("clm-b", CTX, "131,072", "max context on 1.3 is 131,072", "src-slack-ec-0920",
                                        scope="1.3")))
    assert same.receipt["targets"] == []
    assert _claims(inst, CTX)["clm-b"]["status"] == "rejected"
    _ok(_apply(inst, "alex", _src("src-gpu", "Each node has 80GB of GPU memory. Later: each node has 96GB of GPU memory.")))
    first = _clm("clm-z1", "excloud.gpu_memory", "80GB", "Each node has 80GB of GPU memory", "src-gpu")
    _ok(_apply(inst, "notes", first))
    _ok(_apply(inst, "notes", _clm("clm-z2", "excloud.gpu_memory", "96GB", "each node has 96GB of GPU memory", "src-gpu",
                                   supersedes=["clm-z1"], locator="para 2")))
    resent = _ok(_apply(inst, "notes", first))
    assert resent.receipt["targets"] == []
    assert _claims(inst, "excloud.gpu_memory")["clm-z1"]["status"] == "superseded"


def test_claim_content_is_immutable(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-imm", "Each node has 80GB of GPU memory."),
               _clm("clm-imm", "excloud.gpu_memory", "80GB", "Each node has 80GB of GPU memory", "src-imm")))
    con = context_connection(inst)
    try:
        for sql in (
            "UPDATE claims SET quote = 'each node has memory' WHERE id = 'clm-imm'",
            "UPDATE claims SET value_raw = '81GB' WHERE id = 'clm-imm'",
            "UPDATE claims SET key = 'excloud.max_context' WHERE id = 'clm-imm'",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="claims never change: add a new claim"):
                con.execute(sql)
        with pytest.raises(sqlite3.DatabaseError, match="claims are never deleted"):
            con.execute("DELETE FROM claims WHERE id = 'clm-imm'")
        con.execute("UPDATE claims SET status = 'superseded' WHERE id = 'clm-imm'")
        assert con.execute("SELECT status FROM claims WHERE id = 'clm-imm'").fetchone() == ("superseded",)
        con.rollback()
    finally:
        con.close()


# -- reads ---------------------------------------------------------------------------------------


def test_fact_get_disputed_shape_has_no_value(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _example_state(inst)
    result = _read(inst, "fact_get", now=NOW, key="Example Cloud context window")
    assert result.is_error is False, result.text
    assert result.structured == EXAMPLE
    assert json.dumps(result.structured) == json.dumps(EXAMPLE)  # key for key, in order
    item = result.structured["facts"][0]
    assert not {"value", "basis", "as_of", "source"} & set(item)
    assert reads.check_schema(next(tool for tool in reads.TOOLS if tool["name"] == "fact_get")["outputSchema"],
                              result.structured) == []


def test_fact_get_claims_auto_and_all(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-docs-ec-limits", LIMITS),
               _src("src-docs-ec-old", "Old limits page. Context window: 128K tokens.", date="2026-08-01"),
               _src("src-slack-ec-0920", SLACK, kind="slack", date="2026-09-20"),
               _src("src-obs", "Observed seat count. We have 12 seats.", kind="observation")))
    _ok(_apply(inst, "notes", _clm("clm-a", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-limits")))
    checked = _fact_get(inst, CTX)["facts"][0]["claims"]
    assert checked == {"scope": "now", "confidence": "high", "current_version": None}
    every = _fact_get(inst, CTX, claims="all")["facts"][0]["claims"]
    assert [entry["id"] for entry in every["values"]] == ["clm-a"]
    _ok(_apply(inst, "bot", _put(inst, "exunits.seats", 12, "src-obs", quote="We have 12 seats", raw="12")))
    # A strict upsert_fact claim is checked, so auto lists no values and all shows it checked.
    strict_claims = _fact_get(inst, "exunits.seats")["facts"][0]["claims"]
    assert strict_claims["confidence"] == "high" and "values" not in strict_claims
    every_seat = _fact_get(inst, "exunits.seats", claims="all")["facts"][0]["claims"]
    assert [(entry["raw"], entry["checked"]) for entry in every_seat["values"]] == [("12", True)]
    _ok(_apply(inst, "notes", _clm("clm-a2", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-old")))
    _ok(_apply(inst, "bot", _clm("clm-b", CTX, "131,072", "max context on 1.3 is 131,072", "src-slack-ec-0920")))
    entries = _fact_get(inst, CTX, claims="all")["facts"][0]["claims"]["values"]
    assert [(entry["id"], entry["agree"], entry.get("suggested")) for entry in entries] == [
        ("clm-a", 1, True), ("clm-a2", 1, None), ("clm-b", 0, None)
    ]
    auto = _fact_get(inst, CTX)["facts"][0]["claims"]["values"]
    assert [(entry["id"], entry["agree"]) for entry in auto] == [("clm-a", 1), ("clm-b", 0)]
    # Values_more past 8 values under auto and past 40 claims under all.
    many = _inst(tmp_path / "many")
    raws = [f"{index}K" for index in range(1, 45)]
    _ok(_apply(many, "alex", _src("src-many", " ".join(f"Context window: {raw} tokens." for raw in raws))))
    _ok(_apply(many, "notes", *[
        _clm(f"clm-v{index:02d}", CTX, raw, f"Context window: {raw} tokens", "src-many", locator=f"line {index}")
        for index, raw in enumerate(raws)
    ]))
    auto = _fact_get(many, CTX)["facts"][0]["claims"]
    assert (len(auto["values"]), auto["values_more"]) == (8, 36)
    assert list(auto)[list(auto).index("values") + 1] == "values_more"
    every = _fact_get(many, CTX, claims="all")["facts"][0]["claims"]
    assert (len(every["values"]), every["values_more"]) == (40, 4)


def test_fact_get_scope_other_scopes_and_planned(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _example_state(inst)
    scoped = _fact_get(inst, CTX, scope="1.2")
    assert scoped["facts"] == [
        {
            "key": CTX,
            "value": 64000,
            "value_type": "int",
            "unit": "tokens",
            "status": "current",
            "basis": "reported",
            "as_of": "2026-07-01",
            "source": {"id": "src-docs-ec-v12", "kind": "document", "title": "Synthetic source", "uri": "",
                       "source_date": "2026-07-01", "locator": "para 1"},
            "claims": {"scope": "1.2", "confidence": "high", "current_version": "1.3"},
        }
    ]
    assert scoped["total"] == 1 and "resolved" not in scoped
    assert _fact_get(inst, CTX, scope="1.3") == _fact_get(inst, CTX) == _fact_get(inst, CTX, scope="now")
    _ok(_apply(inst, "alex", _src("src-r11", "Release 1.1: max context is 32K tokens. Also 48K tokens.")))
    _ok(_apply(inst, "notes", _clm("clm-r11a", CTX, "32K", "max context is 32K tokens", "src-r11", scope="1.1")))
    _ok(_apply(inst, "bot", _clm("clm-r11b", CTX, "48K", "Also 48K tokens", "src-r11", scope="1.1")))
    _ok(_apply(inst, "notes", _clm("clm-r12p", CTX, "64K", "max context is 64K tokens", "src-docs-ec-v12",
                                   scope="1.2", planned=True, locator="para 9")))
    now_claims = _fact_get(inst, CTX)["facts"][0]["claims"]
    assert now_claims["other_scopes"] == {"1.1": "disputed", "1.2": 64000}
    # Each agreed older value carries its source, so it can be cited without a second call; a disputed one doesn't.
    assert now_claims["other_scopes_sources"] == {"1.2": "src-docs-ec-v12"}
    assert "planned" not in now_claims
    in_12 = _fact_get(inst, CTX, scope="1.2")["facts"][0]["claims"]
    assert in_12["planned"] == [
        {"id": "clm-r12p", "value": 64000, "raw": "64K", "scope": "1.2",
         "source": {"id": "src-docs-ec-v12", "kind": "document", "date": "2026-07-01"},
         "quote": "max context is 64K tokens"}
    ]
    assert in_12["other_scopes"] == {"1.1": "disputed"}
    assert "other_scopes_sources" not in in_12  # only a disputed scope is left
    in_11 = _fact_get(inst, CTX, scope="1.1")["facts"][0]
    assert (in_11["status"], in_11["claims"]["confidence"]) == ("disputed", "disputed")
    assert not {"value", "basis", "as_of", "source"} & set(in_11)
    assert (_sig(CTX, "1.1"), CTX, "values disagree (1.1)", "open") in _conflicts(inst, CTX)
    # A planned claim scoped to the current version is listed with now.
    _ok(_apply(inst, "alex", _src("src-plan13", "On 1.3 the context window will be 256K tokens.")))
    _ok(_apply(inst, "notes", _clm("clm-pl13", CTX, "256K", "the context window will be 256K tokens", "src-plan13",
                                   scope="1.3", planned=True)))
    for arguments in ({}, {"scope": "1.3"}, {"scope": "now"}):
        listed = _fact_get(inst, CTX, **arguments)["facts"][0]["claims"]["planned"]
        assert [entry["id"] for entry in listed] == ["clm-pl13"], arguments
    # Other_scopes and planned stop at 8.
    _ok(_apply(inst, "alex", _src("src-many", " ".join(f"On v{i} it is {i + 1}K tokens." for i in range(10)))))
    _ok(_apply(inst, "notes", *[
        _clm(f"clm-many{i}", CTX, f"{i + 1}K", f"On v{i} it is {i + 1}K tokens", "src-many", scope=f"v{i}",
             locator=f"line {i}")
        for i in range(10)
    ], *[
        _clm(f"clm-plan{i}", CTX, f"{i + 1}K", f"On v{i} it is {i + 1}K tokens", "src-many", planned=True,
             locator=f"plan {i}")
        for i in range(10)
    ]))
    many = _fact_get(inst, CTX)["facts"][0]["claims"]
    assert list(many["other_scopes"]) == ["1.1", "1.2", "v0", "v1", "v2", "v3", "v4", "v5"]
    assert len(many["planned"]) == 8


def test_planned_only_reads_show_the_plan(tmp_path: Path, clock: Clock) -> None:
    """A key or scope with only planned claims names them in its no_value message."""
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-p", "Next quarter nodes ship with 96GB of GPU memory.")))
    _ok(_apply(inst, "notes", _clm("clm-pl", "excloud.gpu_memory", "96GB", "nodes ship with 96GB of GPU memory", "src-p",
                                   planned=True)))
    assert _fact_error(inst, "excloud.gpu_memory") == (
        "no_value", "fact key excloud.gpu_memory is registered but has no value. Planned: now: 96 GB."
    )
    _ok(_apply(inst, "notes", _clm("clm-pl2", "excloud.gpu_memory", "96GB", "nodes ship with 96GB of GPU memory", "src-p",
                                   planned=True, scope="2.0", locator="para 2")))
    assert _fact_error(inst, "excloud.gpu_memory") == (
        "no_value", "fact key excloud.gpu_memory is registered but has no value. Planned: 2.0: 96 GB, now: 96 GB."
    )
    assert _fact_error(inst, "excloud.gpu_memory", scope="2.0") == (
        "no_value", "fact key excloud.gpu_memory has no value for scope 2.0. Planned: 2.0: 96 GB."
    )
    _ok(_apply(inst, "notes", _clm("clm-s3", "excloud.gpu_memory", "96GB", "nodes ship with 96GB of GPU memory", "src-p",
                                   scope="3.0", locator="para 3")))
    assert _fact_error(inst, "excloud.gpu_memory") == (
        "no_value",
        "fact key excloud.gpu_memory has no value now. Current version unknown. Values by version: 3.0: 96 GB. "
        "Pass scope to read one. Planned: 2.0: 96 GB, now: 96 GB.",
    )


def test_confidence_levels(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex",
               _src("src-note", "Note: we counted 12 desks.", kind="user_note"),
               _src("src-doc", "Doc: 40 seats are licensed."),
               _src("src-chat", "Chat: about 4096 bytes each. Maybe 2 hours.", kind="slack"),
               _src("src-obs", "Observed share. The share is 45 percent.", kind="observation"),
               _src("src-old", "Old doc: 7 of them.", date="2026-01-01")))
    _ok(_apply(inst, "notes",
               _clm("clm-obs", "exunits.count", "12", "we counted 12 desks", "src-note", basis="observed"),
               _clm("clm-top", "exunits.seats", "40", "40 seats are licensed", "src-doc"),
               _clm("clm-med", "exunits.bytes", "4096", "about 4096 bytes each", "src-chat"),
               _clm("clm-inf", "exunits.hours", "2", "Maybe 2 hours", "src-chat", basis="inferred", locator="para 2")))
    _ok(_apply(inst, "bot", _put(inst, "exunits.share", 45, "src-obs", quote="The share is 45 percent", raw="45")))
    clock.at("2026-08-01T00:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-stale", "exunits.stale", "7", "7 of them", "src-old")))
    clock.at(NOW)
    _two_sides(inst)
    levels = {
        key: _fact_get(inst, key)["facts"][0]["claims"]["confidence"]
        for key in ("exunits.count", "exunits.seats", "exunits.bytes", "exunits.hours", "exunits.share",
                    "exunits.stale", CTX)
    }
    assert levels == {
        "exunits.count": "high",
        "exunits.seats": "high",
        "exunits.bytes": "medium",
        "exunits.hours": "low",
        "exunits.share": "high",  # An observed upsert_fact claim is checked now
        "exunits.stale": "low",
        CTX: "disputed",
    }
    assert _fact_get(inst, "exunits.stale")["facts"][0]["stale"] is True


def test_source_current_false(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-v1", "Version 1: 12 desks."), _src("src-other", "Other: 40 seats.")))
    _ok(_apply(inst, "notes", _clm("clm-v1", "exunits.count", "12", "Version 1: 12 desks", "src-v1"),
               _clm("clm-o", "exunits.seats", "40", "Other: 40 seats", "src-other")))
    assert "source_current" not in _fact_get(inst, "exunits.count")["facts"][0]["claims"]
    _ok(_apply(inst, "alex", _src("src-v2", "Version 2: 12 desks still.", supersedes="src-v1")))
    assert _fact_get(inst, "exunits.count")["facts"][0]["claims"]["source_current"] is False
    assert "source_current" not in _fact_get(inst, "exunits.seats")["facts"][0]["claims"]


def test_disputed_on_every_read_surface(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    _ok(_apply(inst, "alex", _src("src-on", "The feature flag is true in the doc."),
               _src("src-off", "The thread says the flag is false.", kind="slack")))
    _ok(_apply(inst, "notes", _clm("clm-on", "exunits.enabled", "true", "The feature flag is true", "src-on")))
    _ok(_apply(inst, "bot", _clm("clm-off", "exunits.enabled", "false", "the flag is false", "src-off")))
    got = _read(inst, "get", entity="fact", id=CTX, include_history=True)
    assert got.is_error is False, got.text
    assert got.structured["row"]["value"] == "disputed"
    history = got.structured["history"]["items"]
    assert [(item["status"], item["value"]) for item in history] == [("disputed", "disputed"), ("current", 128000)]
    rows = _read(inst, "records_query", entity="fact", fields=["key", "value", "value_type", "status"]).structured["rows"]
    by_key = {row["key"]: row for row in rows}
    assert by_key[CTX]["value"] == "disputed" and by_key[CTX]["value_type"] == "int"
    assert by_key["exunits.enabled"]["value"] == "disputed" and by_key["exunits.enabled"]["value_type"] == "bool"
    hidden = _read(inst, "records_query", entity="fact", fields=["key", "value"]).structured["rows"]
    assert {row["key"]: row["value"] for row in hidden} == {CTX: "disputed", "exunits.enabled": "disputed"}
    for query in ("context window", "131", "128K", "flag"):
        hits = _read(inst, "search", query=query, entities=["fact"]).structured["hits"]
        for hit in hits:
            if hit["id"] in (CTX, "exunits.enabled"):
                assert hit["disputed"] is True and hit["snippet"] == "disputed", hit
                assert list(hit)[-1] == "disputed"
    hits = _read(inst, "search", query="context window", entities=["fact"]).structured["hits"]
    assert [hit["id"] for hit in hits] == [CTX]
    # fact_get lists each side inside claims, never a bare top-level value.
    item = _fact_get(inst, CTX)["facts"][0]
    assert "value" not in item and [entry["value"] for entry in item["claims"]["values"]] == [128000, 131072]
    for value in ("128000", "131072", "true", "false"):
        assert value not in json.dumps(rows)
    # A dispute that ends by its bucket emptying leaves no bare value either.
    for path in ("version-move", "retire"):
        _assert_no_hidden_value(_retired_after_dispute(tmp_path / path, path), with_history=path == "version-move")


def _retired_after_dispute(tmp_path: Path, path: str) -> Path:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex",
               _src("src-v", "The current version is 1.3 for every region. Later the current version is 1.4."),
               _src("src-x", "On 1.3 the context window is 128K tokens."),
               _src("src-y", "On 1.3 the context window is 131,072 tokens.", kind="slack")))
    if path == "version-move":
        # The scoped claims come first, so the fact is first written disputed.
        _ok(_apply(inst, "notes", _clm("clm-x", CTX, "128K", "the context window is 128K tokens", "src-x", scope="1.3")))
        _ok(_apply(inst, "bot", _clm("clm-y", CTX, "131,072", "the context window is 131,072 tokens", "src-y",
                                     scope="1.3")))
        _ok(_apply(inst, "alex", _clm("clm-v13", "excloud.current_version", "1.3", "The current version is 1.3",
                                      "src-v")))
        assert (_fact(inst, CTX)["status"], _fact(inst, CTX)["value_int"]) == ("disputed", 128000)
        _ok(_apply(inst, "alex", _clm("clm-v14", "excloud.current_version", "1.4", "the current version is 1.4",
                                      "src-v", supersedes=["clm-v13"], locator="para 2")))
    else:
        # One writer: 131,072 first (so the first headline isn't 128000), then 128K, then retire.
        _ok(_apply(inst, "notes", _clm("clm-y", CTX, "131,072", "the context window is 131,072 tokens", "src-y")))
        _ok(_apply(inst, "notes", _clm("clm-x", CTX, "128K", "the context window is 128K tokens", "src-x")))
        assert (_fact(inst, CTX)["status"], _fact(inst, CTX)["value_int"]) == ("disputed", 128000)
        _ok(_apply(inst, "notes", _put(inst, CTX, None, "src-x", status="retired")))
    fact = _fact(inst, CTX)
    assert (fact["status"], fact["value_int"]) == ("retired", 128000)  # stored, never shown
    return inst


SIDES = ("128000", "128K", "131072", "131,072")


def _assert_no_hidden_value(inst: Path, *, with_history: bool) -> None:
    """Neither side of the dispute shows as a value on any surface. In the retire path the
    first history row was current at 131,072 before the dispute began, so history is checked there
    only for the rows after it."""
    surfaces = {
        "fact_get": _read(inst, "fact_get", key=CTX).text,
        "fact_get prefix": _read(inst, "fact_get", key="excloud.*", include_retired=True).text,
        "get": _read(inst, "get", entity="fact", id=CTX, include_history=with_history).text,
        "records_query": _read(inst, "records_query", entity="fact", fields=["key", "value", "status"]).text,
    }
    for query in ("context window", "128K", "131"):
        # The result echoes the query, so only its hits are checked.
        hits = _read(inst, "search", query=query, entities=["fact"]).structured["hits"]
        surfaces[f"search {query}"] = json.dumps(hits, ensure_ascii=False)
    for name, text in surfaces.items():
        for side in SIDES:
            assert side not in text, (name, side, text[:400])
    for query in ("context window", "128K", "131"):
        hits = [hit for hit in _read(inst, "search", query=query, entities=["fact"]).structured["hits"] if hit["id"] == CTX]
        assert hits and all(hit["disputed"] is True and hit["snippet"] == "disputed" for hit in hits), (query, hits)
    # The brief names a side only inside a dispute line, which lists both sides.
    assert all(line.startswith("Disputed ") for line in _brief(inst) if any(side in line for side in SIDES))
    item = _fact_get(inst, CTX)["facts"][0]
    assert item["status"] == "retired" and not {"value", "basis", "as_of", "source"} & set(item)
    got = _read(inst, "get", entity="fact", id=CTX, include_history=True).structured
    assert got["row"]["value"] == "disputed"
    assert all(entry["value"] == "disputed" for entry in got["history"]["items"] if entry["status"] != "current")
    rows = _read(inst, "records_query", entity="fact", fields=["key", "value"]).structured["rows"]
    assert {row["key"]: row["value"] for row in rows}[CTX] == "disputed"


def test_get_claim(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _example_state(inst)
    got = _read(inst, "get", entity="claim", id="clm-b")
    assert got.is_error is False, got.text
    body = got.structured
    assert list(body) == ["entity", "id", "row", "source", "related", "truncated"]
    stamp = body["row"]["created_by"]
    assert stamp.startswith("bot:bot-")
    assert body["row"] == {
        "id": "clm-b", "key": CTX, "scope": "1.3", "value": 131072, "value_type": "int", "value_raw": "131,072",
        "status": "live", "basis": "reported", "as_of": "2026-09-20", "quote": "max context on 1.3 is 131,072",
        "checked": True, "supersedes": [], "proposed_by": "bot", "superseded_by": None,
        "resolution_source_id": None, "resolution_locator": None, "created_at": NOW, "created_by": stamp,
        "updated_at": NOW, "updated_by": stamp,
    }
    assert body["source"] == {"id": "src-slack-ec-0920", "kind": "slack", "title": "Synthetic source", "uri": "",
                              "source_date": "2026-09-20", "author_person_id": None, "locator": "para 1"}
    assert body["related"] == {"fact": {"key": CTX, "status": "disputed"}, "resolution_source": None}
    assert body["truncated"] is False
    _alex_note(inst)
    _ok(_apply(inst, "alex", _settle_packet()))
    related = _read(inst, "get", entity="claim", id="clm-b").structured["related"]
    assert related["resolution_source"]["id"] == "src-note-alex-1003"
    assert related["resolution_source"]["locator"] == "message 1"
    missing = _read(inst, "get", entity="claim", id="clm-missing")
    assert (missing.code, json.loads(missing.text)["error"]["message"]) == ("not_found", "no claim clm-missing")
    history = _read(inst, "get", entity="claim", id="clm-b", include_history=True)
    assert json.loads(history.text)["error"]["message"] == "include_history is only for records and facts"


def test_search_finds_aliases_and_quotes(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    for query in ("vram", "GPU RAM"):
        hits = _read(inst, "search", query=query, entities=["fact"]).structured["hits"]
        assert [hit["id"] for hit in hits] == ["excloud.gpu_memory"], query
    _ok(_apply(inst, "alex", _src("src-node", "Spec sheet: each node has 80GB of GPU memory."),
               _clm("clm-node", "excloud.gpu_memory", "80GB",
                    "each node has 80GB of GPU memory", "src-node")))
    hits = _read(inst, "search", query="node has", entities=["fact"]).structured["hits"]
    assert [hit["id"] for hit in hits] == ["excloud.gpu_memory"]
    assert store.check_instance(inst) == []
    (inst / "brain.toml").write_text(truth_config(truth_block=TRUTH_BLOCK.replace('"EC"]', '"EC", "ExCloud"]')),
                                     encoding="utf-8")
    code, out, stderr = _cli("apply-config", "--instance", inst)
    assert code == 0, (out, stderr)
    assert store.check_instance(inst) == []
    found = _read(inst, "search", query="ExCloud", entities=["fact"]).structured["hits"]
    assert sorted(hit["id"] for hit in found) == ["excloud.current_version", "excloud.gpu_memory", "excloud.max_context"]
    _two_sides(inst)
    _alex_note(inst)
    _ok(_apply(inst, "alex", _resolve(CTX, ["clm-a"], ["clm-b"], "the docs are right, the context window is 128K tokens",
                                      "src-note-alex-1003")))
    assert store.check_instance(inst) == []
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        assert store.search_drift(con) == []
    finally:
        con.close()


def _budget_state(inst: Path, key: str, ids: list[str], sources: list[str], quotes: list[str], raws: list[str],
                  scope_claim: dict | None = None) -> None:
    _ok(_apply(inst, "alex", *[
        _src(sid, quote, kind=kind, date="2026-09-01")
        for sid, quote, kind in zip(sources, quotes, ["document", "slack", "meeting_transcript", "web", "email",
                                                       "user_note", "calendar", "api"])
    ]))
    for index, (cid, sid, quote, raw) in enumerate(zip(ids, sources, quotes, raws)):
        agent = ("notes", "bot", "solo", "alex")[index % 4]
        _ok(_apply(inst, agent, _clm(cid, key, raw, quote, sid)))
    if scope_claim is not None:
        _ok(_apply(inst, "alex", _src(scope_claim["source"]["source_id"], scope_claim["row"]["quote"])))
        _ok(_apply(inst, "notes", scope_claim))


def _quote(raw: str, size: int, fill: str = "x") -> str:
    head = f"the limit is {raw} "
    return head + (fill * (size - len(head)))


def _size(result: dict) -> int:
    return len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())


def test_fact_get_budget_one_disputed_key(tmp_path: Path, clock: Clock) -> None:
    raws = ["128K", "131,072", "96K", "64,000", "100K", "120,000", "150K", "160,000"]
    ids = [f"clm-{index}" + "b" * (40 - 4 - len(str(index))) for index in range(8)]
    sources = [f"src-{index}" + "s" * (40 - 4 - len(str(index))) for index in range(8)]
    assert {len(item) for item in ids + sources} == {40}
    quotes = [_quote(raw, 200) for raw in raws]
    scope = _clm("clm-scope-12", CTX, "64K", _quote("64K", 200), "src-scope-12", scope="1.2")
    sizes = {}
    for count in (3, 8):
        inst = _inst(tmp_path / f"v{count}")
        _budget_state(inst, CTX, ids[:count], sources[:count], quotes[:count], raws[:count], scope_claim=scope)
        result = _fact_get(inst, CTX)
        assert len(result["facts"][0]["claims"]["values"]) == count
        assert result["facts"][0]["claims"]["other_scopes"] == {"1.2": 64000}
        sizes[f"{count} values"] = _size(result)
    assert sizes["3 values"] <= 2048
    # 120-character keys and ids, with 200-character quotes of 3-byte characters.
    long_key = "exlong." + "k" * 113
    keys = [*KEYS, {"key": long_key, "value_type": "int", "unit": "tokens"}]
    inst = _inst(tmp_path / "long", truth_config(keys=keys))
    long_ids = [f"clm-{index}" + "c" * (120 - 4 - len(str(index))) for index in range(3)]
    long_sources = [f"src-{index}" + "t" * (120 - 4 - len(str(index))) for index in range(3)]
    wide = ["中" * 96 + f" {raw} " + "文" * (200 - 98 - len(raw)) for raw in raws[:3]]
    assert {len(item) for item in wide} == {200}
    _budget_state(inst, long_key, long_ids, long_sources, wide, raws[:3])
    sizes["120-character keys and ids, 3-byte quotes"] = _size(_fact_get(inst, long_key))
    # 500-character raws on a text key.
    inst = _inst(tmp_path / "raw")
    texts = [(f"value {index} " + "word " * 120)[:500] for index in range(3)]
    _budget_state(inst, "exunits.motto", ids[:3], sources[:3], texts, texts)
    sizes["500-character raws"] = _size(_fact_get(inst, "exunits.motto"))
    # A 25-key prefix, each key disputed with three values.
    bulk = [{"key": f"exbulk.k{index:02d}", "value_type": "int", "unit": "tokens"} for index in range(25)]
    inst = _inst(tmp_path / "prefix", truth_config(keys=[*KEYS, *bulk]))
    _ok(_apply(inst, "alex", *[_src(sid, quote, kind=kind) for sid, quote, kind in
                               zip(sources[:3], quotes[:3], ["document", "slack", "meeting_transcript"])]))
    for index, item in enumerate(bulk):
        for position in range(3):
            agent = ("notes", "bot", "solo")[position]
            _ok(_apply(inst, agent, _clm(f"clm-k{index:02d}-{position}", item["key"], raws[position],
                                         quotes[position], sources[position])))
    sizes["25-key prefix"] = _size(_fact_get(inst, "exbulk.*"))
    print("fact_get bytes: " + ", ".join(f"{name} {size}" for name, size in sizes.items()))


# -- brief, checks and version -------------------------------------------------------------------

KEY100 = "exlong." + "a" * 93
KEY60 = "exlong.ascii_" + "x" * 47
KEY60W = "exlong.wide_" + "w" * 48
KEY120 = "exlong.label_" + "y" * 107
LABEL40 = "release-2026.10.01-candidate-build-00001"
LONG_KEYS = [
    *KEYS,
    {"key": KEY100, "value_type": "int", "unit": "GB"},
    {"key": KEY60, "value_type": "text"},
    {"key": KEY60W, "value_type": "text"},
    {"key": KEY120, "value_type": "text"},
]


def _split_line(line: str) -> list[str]:
    body = line.split("): ", 1)[1]
    return body.split(" | ")


def test_brief_dispute_lines_order_and_cut(tmp_path: Path, clock: Clock) -> None:
    from brain import migrate
    from tests.test_migrate import _v4_config, v4_store

    assert (len(KEY100), len(KEY60), len(KEY60W), len(KEY120), len(LABEL40)) == (100, 60, 60, 120, 40)
    # Legacy: a fact marked disputed before 0.7.0 shows its one value.
    legacy = v4_store(tmp_path / "legacy", _v4_config())
    migrate.migrate(legacy / "brain.sqlite3")
    assert "Disputed demo.motto (now): Small steps, user_note 2026-10-01. Ask a person." in _disputes(legacy)
    inst = _inst(tmp_path / "main", truth_config(keys=LONG_KEYS))
    _ok(_apply(inst, "alex",
               _src("src-gpu-doc", "Each node has 80 GB of VRAM."),
               _src("src-gpu-slack", "No, each node has 96 GB of VRAM.", kind="slack"),
               _src("src-gpu-call", "On the call: each node has 72 GB of VRAM.", kind="meeting_transcript"),
               _src("src-n-doc", "Price is A$49.50. Count is 12. Seats are 12 seats."),
               _src("src-n-slack", "Price is A$55. Count is 13. Seats are 14 seats.", kind="slack")))
    clock.at("2026-10-09T01:00:00Z")
    _two_sides(inst)
    clock.at("2026-10-09T02:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-g80", "excloud.gpu_memory", "80 GB", "each node has 80 GB of VRAM", "src-gpu-doc")))
    _ok(_apply(inst, "bot", _clm("clm-g96", "excloud.gpu_memory", "96 GB", "each node has 96 GB of VRAM", "src-gpu-slack")))
    _ok(_apply(inst, "solo", _clm("clm-g72", "excloud.gpu_memory", "72 GB", "each node has 72 GB of VRAM", "src-gpu-call")))
    assert _disputes(inst) == [
        "Disputed excloud.gpu_memory (now): 80 GB, document 2026-09-01 | 96 GB, slack 2026-09-01 | +1 more. "
        "Ask a person.",
        "Disputed excloud.max_context (now): 128000 tokens, document 2026-09-01 | 131072 tokens, slack 2026-09-20. "
        "Ask a person.",
    ]
    # Two disputes opened in one packet share created_at, so subject_id breaks the tie.
    clock.at("2026-10-09T03:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-p1", "exbasic.price", "A$49.50", "Price is A$49.50", "src-n-doc"),
               _clm("clm-c1", "exunits.count", "12", "Count is 12", "src-n-doc", locator="para 2")))
    _ok(_apply(inst, "bot", _clm("clm-c2", "exunits.count", "13", "Count is 13", "src-n-slack", locator="para 2"),
               _clm("clm-p2", "exbasic.price", "A$55", "Price is A$55", "src-n-slack")))
    clock.at("2026-10-09T04:00:00Z")
    _ok(_apply(inst, "notes", _clm("clm-s1", "exunits.seats", "12 seats", "Seats are 12 seats", "src-n-doc",
                                   locator="para 3")))
    _ok(_apply(inst, "bot", _clm("clm-s2", "exunits.seats", "14 seats", "Seats are 14 seats", "src-n-slack",
                                 locator="para 3")))
    lines = _brief(inst)
    facts = lines.index(next(line for line in lines if line.startswith("Facts: ")))
    assert lines[facts:facts + 5] == [
        "Facts: 0 current, 5 disputed.",
        "Disputed exunits.seats (now): 12 seats, document 2026-09-01 | 14 seats, slack 2026-09-01. Ask a person.",
        "Disputed exbasic.price (now): 49.5 AUD, document 2026-09-01 | 55 AUD, slack 2026-09-01. Ask a person.",
        "Disputed exunits.count (now): 12, document 2026-09-01 | 13, slack 2026-09-01. Ask a person.",
        "2 more disputed. records_query on signal with kind conflict lists them.",
    ]
    # The byte cut keeps the unit: 19-digit values on a 100-character GB key.
    long_inst = _inst(tmp_path / "long", truth_config(keys=LONG_KEYS))
    _ok(_apply(long_inst, "alex", _src("src-big", "Big one 1111111111111111111. Big two 2222222222222222222.")))
    _ok(_apply(long_inst, "notes", _clm("clm-u1", KEY100, "1111111111111111111", "Big one 1111111111111111111", "src-big")))
    _ok(_apply(long_inst, "bot", _clm("clm-u2", KEY100, "2222222222222222222", "Big two 2222222222222222222", "src-big",
                                      locator="para 2")))
    cut = f"Disputed {KEY100} (now): 11111... GB, document 2026-09-01 | 22222... GB, document 2026-09-01. Ask a person."
    assert _disputes(long_inst) == [cut]
    assert len(cut.encode()) == 199
    # Long values: 2,000 characters, ASCII and 3-byte, and a 120-character key with a 40-character label.
    # A strict text fact's value sits inside its quote of at most 500 characters,
    # so the long values are 480 characters. A shown value is cut at 40 bytes, so no line changes.
    long_inst = _inst(tmp_path / "wide", truth_config(keys=LONG_KEYS))
    ascii_values = [("alpha " * 400)[:480], ("bravo " * 400)[:480]]
    wide_values = ["中" * 480, "文" * 480]
    _ok(_apply(long_inst, "alex",
               _src("src-call-1", f"Call one. {ascii_values[0]}. {wide_values[0]} said here.",
                    kind="meeting_transcript", date="2026-09-30"),
               _src("src-call-2", f"Call two. {ascii_values[1]}. {wide_values[1]} said here.",
                    kind="meeting_transcript", date="2026-09-30"),
               _src("src-wide", "Wide values. " + "中" * 300 + " is the value. " + "文" * 300 + " is the value.")))
    for key, values in ((KEY60, ascii_values), (KEY60W, wide_values)):
        quotes = values if key == KEY60 else [f"{value} said here" for value in values]
        _ok(_apply(long_inst, "notes", _put(long_inst, key, values[0], "src-call-1", as_of="2026-09-30",
                                            quote=quotes[0], raw=values[0])))
        _ok(_apply(long_inst, "bot", _put(long_inst, key, values[1], "src-call-2", as_of="2026-09-29",
                                          quote=quotes[1], raw=values[1])))
    _ok(_apply(long_inst, "notes", _clm("clm-w1", KEY120, "中" * 300, "中" * 300 + " is the value", "src-wide",
                                        scope=LABEL40)))
    _ok(_apply(long_inst, "bot", _clm("clm-w2", KEY120, "文" * 300, "文" * 300 + " is the value", "src-wide",
                                      scope=LABEL40, locator="para 2")))
    found = _disputes(long_inst)
    print("long dispute lines (bytes): " + ", ".join(str(len(line.encode())) for line in found))
    lines = {line.split(" ", 2)[1]: line for line in found}
    for key, values in ((KEY60, ascii_values), (KEY60W, wide_values), (KEY120[:114] + "...", ["中" * 300, "文" * 300])):
        line = lines[key]
        assert len(line.encode()) <= 200, (key, len(line.encode()))
        assert line.endswith("Ask a person.")
        body = line[: -len(" Ask a person.")]
        shown = _split_line(body if body.endswith("...") else body[:-1])
        assert len(shown) == 2, line
        for part, value in zip(shown, values):
            text = part.split(", meeting_transcript")[0]
            assert text.endswith("...") and value.startswith(text[:-3]) and len(text) > 3, (part, value[:10])
    assert len(lines[KEY60].encode()) == 199 and len(lines[KEY60W].encode()) == 199
    assert lines[KEY120[:114] + "..."] == f"Disputed {KEY120[:114]}... ({LABEL40}): 中... | 文... Ask a person."
    assert len(lines[KEY120[:114] + "..."].encode()) == 200


def test_brief_change_line(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path / "one")
    observations = "Observations. Context 100 tokens. Context 200 tokens. The count is 1 now. The count is 2 now."
    _ok(_apply(inst, "alex", _src("src-obs", observations, kind="observation")))
    _ok(_apply(inst, "notes", _put(inst, CTX, 100, "src-obs", quote="Context 100 tokens", raw="100")))
    assert not any(line.startswith("Changed in") for line in _brief(inst))
    _ok(_apply(inst, "notes", _put(inst, CTX, 200, "src-obs", quote="Context 200 tokens", raw="200")))
    assert "Changed in the last 7 days: 1 fact (excloud.max_context)." in _brief(inst)
    assert not any(line.startswith("Changed in") for line in _brief(inst, now="2026-10-20T00:00:00Z"))
    keys = [CTX, "excloud.gpu_memory", "exunits.count", "exunits.seats", "exunits.bytes"]
    five = _inst(tmp_path / "five")
    _ok(_apply(five, "alex", _src("src-obs", observations, kind="observation")))
    for hour, key in enumerate(keys):
        clock.at(f"2026-10-09T0{hour}:00:00Z")
        _ok(_apply(five, "notes", _put(five, key, 1, "src-obs", quote="The count is 1 now", raw="1")))
        _ok(_apply(five, "notes", _put(five, key, 2, "src-obs", quote="The count is 2 now", raw="2")))
    assert "Changed in the last 7 days: 5 facts (exunits.bytes, exunits.seats, exunits.count, +2 more)." in _brief(five)
    # A better-ranked supporting claim that writes the same value differently isn't a change.
    motto = _inst(tmp_path / "motto")
    _ok(_apply(motto, "alex", _src("src-sl", "Thread: the motto is small steps.", kind="slack"),
               _src("src-doc", "Doc: the motto is Small Steps!")))
    _ok(_apply(motto, "notes", _clm("clm-1", "exunits.motto", "small steps", "the motto is small steps", "src-sl")))
    clock.at("2026-10-09T06:00:00Z")
    _ok(_apply(motto, "bot", _clm("clm-2", "exunits.motto", "Small Steps!", "the motto is Small Steps", "src-doc")))
    assert _history(motto, "exunits.motto")[-1][:4] == (2, "updated", "current", "Small Steps!")
    assert not any(line.startswith("Changed in") for line in _brief(motto))
    disputed = _inst(tmp_path / "dispute")
    _two_sides(disputed)
    _alex_note(disputed)
    assert not any(line.startswith("Changed in") for line in _brief(disputed))
    _ok(_apply(disputed, "alex", _resolve(CTX, ["clm-a"], ["clm-b"],
                                          "the docs are right, the context window is 128K tokens", "src-note-alex-1003")))
    assert _fact(disputed, CTX)["status"] == "current"
    assert not any(line.startswith("Changed in") for line in _brief(disputed))


def test_brief_budget_with_truth_lines(tmp_path: Path, clock: Clock) -> None:
    from tests import test_phase7 as p7

    workstreams = [
        {"id": "general", "title": "General", "description": "", "sort_order": 1},
        *[{"id": f"w{index:02d}", "title": "W", "description": "", "sort_order": index} for index in range(2, 13)],
    ]
    bulk = [{"key": f"bulk.f{index}", "value_type": "text", "stale_after_days": 1} for index in range(9)]
    disputed = [{"key": f"dsp{index}." + "d" * 115, "value_type": "text"} for index in range(6)]
    changed = [{"key": f"chg{index}." + "c" * 115, "value_type": "text"} for index in range(5)]
    assert {len(item["key"]) for item in disputed + changed} == {120}
    agents = {**p7.AGENTS, "other": {"operations": "*"}}
    instance = p7._instance(tmp_path, workstreams=workstreams, fact_keys=[*bulk, *disputed, *changed], agents=agents)
    # The disputed values are 480 characters, inside their quotes; a dispute line
    # cuts each shown value at 40 bytes, so the lines are the same as with 2,000.
    lefts = [(f"left {index} " * 400)[:480] for index in range(len(disputed))]
    rights = [(f"right {index} " * 400)[:480] for index in range(len(disputed))]
    firsts = [f"The value is first {index}" for index in range(len(changed))]
    seconds = [f"The value is second {index}" for index in range(len(changed))]
    _ok(_apply(instance, "tester", _src("src-t1", "Truth one. " + " ".join(f"{text}." for text in [*lefts, *firsts, *seconds]),
                                        kind="meeting_transcript"),
               _src("src-t2", "Truth two. " + " ".join(f"{text}." for text in rights), kind="meeting_transcript")))
    for index, item in enumerate(disputed):
        _ok(_apply(instance, "tester", _put(instance, item["key"], lefts[index], "src-t1", quote=lefts[index],
                                            raw=lefts[index])))
        _ok(_apply(instance, "other", _put(instance, item["key"], rights[index], "src-t2", quote=rights[index],
                                           raw=rights[index])))
    for index, item in enumerate(changed):
        _ok(_apply(instance, "tester", _put(instance, item["key"], f"first {index}", "src-t1", quote=firsts[index],
                                            raw=f"first {index}")))
        _ok(_apply(instance, "tester", _put(instance, item["key"], f"second {index}", "src-t1", quote=seconds[index],
                                            raw=f"second {index}")))
    title = ("Example Co regional rollout " * 5)[:120]
    ops = p7._crowded_ops(lambda prefix, index: f"{prefix}{index:02d}", lambda index: f"proj-p{index}", title)
    p7._crowd(instance, ops, bulk)
    text = p7._brief(instance)
    lines = text.splitlines()
    assert len(lines) <= 40
    assert all(len(line.encode()) <= 200 for line in lines)
    assert len(text.encode()) <= 4096
    assert sum(1 for line in lines if line.startswith("Disputed ")) == 3
    assert all(line.endswith("Ask a person.") for line in lines if line.startswith("Disputed "))
    assert "3 more disputed. records_query on signal with kind conflict lists them." in lines
    assert any(line.startswith("Changed in the last 7 days: 5 facts (") for line in lines)
    print(f"brief with truth lines: {len(lines)} lines, {len(text.encode())} bytes")


def test_check_reports_headline_drift(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    _ok(_apply(inst, "alex", _src("src-misc", "Misc: 80GB of VRAM. We have 12 desks. We have 40 seats. "
                                              "It is 4096 bytes. A$49.50 or A$55.")))
    _ok(_apply(inst, "notes",
               _clm("clm-gpu", "excloud.gpu_memory", "80GB", "80GB of VRAM", "src-misc"),
               _clm("clm-count", "exunits.count", "12", "We have 12 desks", "src-misc", locator="para 2"),
               _clm("clm-seats", "exunits.seats", "40", "We have 40 seats", "src-misc", locator="para 3"),
               _clm("clm-bytes", "exunits.bytes", "4096", "It is 4096 bytes", "src-misc", locator="para 4"),
               _clm("clm-price1", "exbasic.price", "A$49.50", "A$49.50 or A$55", "src-misc", locator="para 5")))
    _ok(_apply(inst, "bot", _clm("clm-price2", "exbasic.price", "A$55", "A$49.50 or A$55", "src-misc", locator="para 6")))
    assert store.check_instance(inst) == []
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        assert store.truth_problems(con) == []
    finally:
        con.close()
    con = context_connection(inst, foreign_keys=False)
    try:
        con.execute("DELETE FROM facts WHERE key = 'exunits.count'")
        con.execute(f"UPDATE facts SET status = 'current', version = version + 1 WHERE key = '{CTX}'")
        con.execute("UPDATE facts SET value_int = 1, version = version + 1 WHERE key = 'excloud.gpu_memory'")
        con.execute("UPDATE signals SET status = 'dismissed', resolved_at = ? WHERE subject_id = 'exbasic.price'", (NOW,))
        con.execute(
            "INSERT INTO signals (id, kind, subject_table, subject_id, basis, source_id, locator, version, created_at, "
            "updated_at, updated_by) VALUES ('sig-conflict-0000000000000000-1', 'conflict', 'facts', 'exunits.seats', "
            "'observed', 'src-misc', 'x', 1, ?, ?, 'tester:x')",
            (NOW, NOW),
        )
        con.execute("UPDATE fact_keys SET value_type = 'text' WHERE key = 'exunits.bytes'")
        con.commit()
    finally:
        con.close()
    expected = [
        "fact exbasic.price: now is disputed but has no open conflict signal",
        "fact excloud.gpu_memory: value does not match its claims",
        "fact excloud.max_context: status is current, claims give disputed",
        "claim clm-bytes: value_type differs from fact key exunits.bytes",
        "fact exunits.count: claims are in force but there is no fact row",
        "conflict signal sig-conflict-0000000000000000-1 is open but its bucket agrees",
    ]
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        assert store.truth_problems(con) == expected
    finally:
        con.close()
    problems = store.check_instance(inst)
    assert all(message in problems for message in expected)


_APPLIER_TESTS = (
    "test_same_value_from_two_sources_is_support",
    "test_different_scopes_never_conflict",
    "test_current_version_moves_the_headline",
    "test_scoped_version_claim_is_refused",
    "test_disputed_version_leaves_only_unscoped_in_force",
    "test_planned_never_conflicts",
    "test_rule_1_update_both_orders",
    "test_rule_1b_older_version_late_both_orders",
    "test_rule_2_own_supersede_both_orders",
    "test_rule_3_other_writer_supersede_is_held",
    "test_supersedes_id_from_another_agent_is_held",
    "test_upsert_fact_is_shorthand",
    "test_collector_reverify_updates_last_verified_at_only",
    "test_conflict_sets_disputed_opens_one_signal_and_a_brief_line",
    "test_packets_cannot_create_or_change_conflict_signals",
    "test_ranking_suggests_but_never_resolves",
    "test_resolve_fact_refusals",
    "test_resolve_fact_settles_and_closes_the_signal",
    "test_resolve_fact_race_exits_3",
    "test_resolve_fact_from_another_agent_is_held",
    "test_claim_matching_a_rejected_claim_is_refused",
)


def test_only_the_derivation_writes_facts(tmp_path: Path, monkeypatch) -> None:
    violations: list[tuple] = []
    compiled: list[tuple] = []
    original = Applier.__init__
    original_run = Applier._run_ops
    connect = sqlite3.connect

    # No statement cache, so the authoriser sees every statement each time it runs.
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connect(*args, **{**kwargs, "cached_statements": 0}))

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._signal_ops = False

        def authorizer(action, table, column, _db, _trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE) and table in ("facts", "signals"):
                compiled.append((action, table))
                if not self._deriving and (table == "facts" or not self._signal_ops):
                    violations.append((action, table, column))
            return sqlite3.SQLITE_OK

        self.con.set_authorizer(authorizer)

    def run_ops(self, packet, decided_at):
        self._signal_ops = any(op.get("op") == "upsert_signal" for op in packet["ops"])
        return original_run(self, packet, decided_at)

    monkeypatch.setattr(Applier, "__init__", init)
    monkeypatch.setattr(Applier, "_run_ops", run_ops)
    clock = Clock(monkeypatch)
    for index, name in enumerate(_APPLIER_TESTS):
        clock.at(NOW)
        globals()[name](tmp_path / f"t{index:02d}", clock)
    assert {table for _action, table in compiled} == {"facts", "signals"}, "no write to facts or signals was seen"
    assert violations == []
    inst = _inst(tmp_path / "write")
    writer = Applier(inst)
    try:
        with pytest.raises(RuntimeError, match="facts are written only by the derivation"):
            writer._write(schemas.OPS["upsert_fact"], {"key": CTX}, None,
                          {"source": {"source_id": "src-x", "locator": "x"}}, "alex", "alex-x", NOW)
    finally:
        writer.close()


def test_version_and_schema(tmp_path: Path) -> None:
    code, out, _stderr = _cli("--version")
    assert (code, out.strip()) == (0, version_banner())
    code, out, stderr = _cli("init", "demo", "--dir", tmp_path)
    assert code == 0, stderr
    assert json.loads(out)["schema_version"] == migrate.latest_version()
    assert ro_rows(tmp_path / "demo", "PRAGMA user_version") == [(migrate.latest_version(),)]
    assert len(render_router("demo").encode("utf-8")) == router_bytes("demo")
    assert len(render_router("a" + "0" * 39).encode("utf-8")) == router_bytes("a" + "0" * 39)
    assert len((tmp_path / "demo" / "ROUTER.md").read_bytes()) == router_bytes("demo")


# -- more behaviour ------------------------------------------------------------------------------


def test_add_claim_refusals(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _two_sides(inst)
    _ok(_apply(inst, "alex", _src("src-gpu", "Each node has 80GB of GPU memory.")))
    _ok(_apply(inst, "notes", _clm("clm-g", "excloud.gpu_memory", "80GB", "Each node has 80GB of GPU memory", "src-gpu")))
    quote = "Context window: 128K tokens"
    cases = [
        ({"op": "add_claim", "row": {"id": "clm-x", "key": CTX, "value_raw": "128K", "basis": "reported"},
          "source": {"source_id": "src-docs-ec-limits", "locator": "x"}}, "quote is required to create"),
        (_clm("clm-x", "excloud.nothing", "128K", quote, "src-docs-ec-limits"), "unknown fact key excloud.nothing"),
        (_clm("clm-x", CTX, "128K", quote, "src-missing"), advice.source_missing_error("src-missing")),
        (_clm("clm-a", CTX, "128K", quote, "src-docs-ec-limits", locator="para 2"),
         "claims never change: clm-a already exists with different content; use a new id"),
        (_clm("clm-m5-1", CTX, "128K", quote, "src-docs-ec-limits"),
         "claim ids clm-m5-* and clm-<24 hex> are reserved for the engine"),
        (_clm("clm-0123456789abcdef01234567", CTX, "128K", quote, "src-docs-ec-limits"),
         "claim ids clm-m5-* and clm-<24 hex> are reserved for the engine"),
        (_clm("clm-x", CTX, "128K", "Context window: 256K tokens", "src-docs-ec-limits"),
         "quote is not in source src-docs-ec-limits"),
        (_clm("clm-x", CTX, "129K", quote, "src-docs-ec-limits"), "value 129K is not in the quote"),
        (_clm("clm-x", CTX, "128K", quote, "src-docs-ec-limits", planned=True, supersedes=["clm-a"]),
         "only a live claim can supersede"),
        (_clm("clm-x", CTX, "128K", quote, "src-docs-ec-limits", supersedes=["clm-x"]),
         "supersedes names the claim itself"),
        (_clm("clm-x", CTX, "128K", quote, "src-docs-ec-limits", supersedes=["clm-g"]),
         "supersedes clm-g, a claim on another key"),
    ]
    for op, message in cases:
        outcome = _apply(inst, "notes", op)
        assert outcome.exit_code == 1, message
        assert _err(outcome) == f"op 0 (add_claim): {message}"
    same = _ok(_apply(inst, "notes", _clm("clm-a", CTX, "128K", quote, "src-docs-ec-limits")))
    assert same.receipt["targets"] == []
    # A writer's supersede cycle is refused at the link that closes it, in any arrival order.
    _ok(_apply(inst, "alex", _src("src-cyc", "Cycle notes: 10K tokens, 20K tokens and 30K tokens.")))
    _ok(_apply(inst, "notes", _clm("clm-p", "exunits.count", "10K", "Cycle notes: 10K tokens", "src-cyc",
                                   supersedes=["clm-n"])))
    two = _apply(inst, "notes", _clm("clm-n", "exunits.count", "20K", "20K tokens and 30K tokens", "src-cyc",
                                     supersedes=["clm-p"], locator="para 2"))
    assert _err(two) == "op 0 (add_claim): supersedes would form a cycle through clm-p"
    import itertools

    for order in itertools.permutations("pqr"):
        cyc = _inst(tmp_path / ("cycle-" + "".join(order)))
        _ok(_apply(cyc, "alex", _src("src-cyc", "Cycle notes: 10K tokens, 20K tokens and 30K tokens.")))
        links = {"p": ("10K", "clm-q"), "q": ("20K", "clm-r"), "r": ("30K", "clm-p")}
        quotes = {"10K": "Cycle notes: 10K tokens", "20K": "20K tokens and", "30K": "and 30K tokens"}
        outcomes = []
        for name in order:
            raw, target = links[name]
            outcomes.append(_apply(cyc, "notes", _clm(f"clm-{name}", "exunits.count", raw, quotes[raw], "src-cyc",
                                                      supersedes=[target], locator=name)))
        assert [outcome.receipt["status"] for outcome in outcomes] == ["applied", "applied", "rejected"], order
        last = order[-1]
        assert _err(outcomes[-1]) == f"op 0 (add_claim): supersedes would form a cycle through {links[last][1]}"
        assert _fact(cyc, "exunits.count")["status"] == "current"


def _key_op(inst: Path, row: dict) -> dict:
    rows = ro_rows(inst, "SELECT version FROM fact_keys WHERE key = ?", (row["key"],))
    return upsert_op("upsert_fact_key", row, "src-keys", base=rows[0][0] if rows else None)


def test_upsert_fact_key_refuses_what_claims_use(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-keys", "Keys: launch on 2026-11-02 in v1. 80GB of VRAM. Region au, later eu.")))
    _ok(_apply(inst, "notes",
               _clm("clm-launch", "exunits.launch", "2026-11-02", "launch on 2026-11-02 in v1", "src-keys", scope="v1"),
               _clm("clm-gpu", "excloud.gpu_memory", "80GB", "80GB of VRAM", "src-keys", locator="para 2"),
               _clm("clm-au", "vendor.compliance.instance", "au", "Region au, later eu", "src-keys", locator="para 3"),
               _clm("clm-eu", "vendor.compliance.instance", "eu", "Region au, later eu", "src-keys", locator="para 4",
                    planned=True)))
    assert _fact(inst, "exunits.launch") is None
    cases = [
        ({"key": "exunits.launch", "value_type": "text"}, "value_type cannot change while a claim uses this key"),
        ({"key": "excloud.gpu_memory", "unit": "MB"}, "unit cannot change while a claim uses this key"),
        ({"key": "vendor.compliance.instance", "allowed_values": ["au", "us"]},
         "allowed_values would drop a value a claim uses"),
        ({"key": "exnew.current_version", "value_type": "int"},
         'fact key exnew.current_version must have value_type = "text"'),
        ({"key": "vendor.compliance.current_version", "value_type": "int"},
         'fact key vendor.compliance.current_version must have value_type = "text"'),
    ]
    for row, message in cases:
        outcome = _apply(inst, "brain", _key_op(inst, row), reserved=True)
        assert _err(outcome) == f"op 0 (upsert_fact_key): {message}", row
    keys = [*KEYS, {"key": "exold.current_version", "value_type": "int"}]
    old = _inst(tmp_path / "old", truth_config(keys=[*KEYS]))
    (old / "brain.toml").write_text(truth_config(keys=keys), encoding="utf-8")
    con = context_connection(old)
    try:
        source = con.execute("SELECT id FROM sources WHERE kind = 'config' LIMIT 1").fetchone()[0]
        con.execute(
            "INSERT INTO fact_keys (key, value_type, unit, source_id, locator, version, created_at, updated_at, "
            "updated_by) VALUES ('exold.current_version', 'int', '', ?, 'fact_keys.exold.current_version', 1, ?, ?, "
            "'brain:old')",
            (source, NOW, NOW),
        )
        con.commit()
    finally:
        con.close()
    code, out, stderr = _cli("check", "--instance", old)
    assert "brain: fact key exold.current_version is not text, so it sets no version" in stderr.splitlines()
    assert json.loads(out) == {"ok": True, "problems": []}
    assert code == 0


def test_truth_config_errors(tmp_path: Path) -> None:
    def block(text: str) -> str:
        if not text.startswith("\n["):  # a top-level key goes before the first table
            return _config_error(tmp_path, text.lstrip("\n") + truth_config(truth_block=""))
        return _config_error(tmp_path, truth_config(truth_block=text))

    long_alias = "a" * 81
    many = json.dumps([f"alias {index}" for index in range(21)])
    kinds = ", ".join(json.dumps(kind) for kind in schemas.SOURCE_KINDS)
    cases = [
        ("\ntruth = 5\n", "truth must be a table"),
        ("\n[truth]\nother = 1\n", "unknown key truth.other"),
        ("\n[truth]\nentities = 5\n", "truth.entities must be a table"),
        ("\n[truth]\nattributes = 5\n", "truth.attributes must be a table"),
        ('\n[truth.entities]\nexcloud = "EC"\n',
         "truth.entities.excloud must be a list of 1 to 20 aliases, each 1 to 80 characters on one line"),
        ("\n[truth.entities]\nexcloud = []\n",
         "truth.entities.excloud must be a list of 1 to 20 aliases, each 1 to 80 characters on one line"),
        (f"\n[truth.entities]\nexcloud = {many}\n",
         "truth.entities.excloud must be a list of 1 to 20 aliases, each 1 to 80 characters on one line"),
        (f'\n[truth.attributes]\ngpu_memory = ["{long_alias}"]\n',
         "truth.attributes.gpu_memory must be a list of 1 to 20 aliases, each 1 to 80 characters on one line"),
        ('\n[truth.attributes]\ngpu_memory = ["two\\nlines"]\n',
         "truth.attributes.gpu_memory must be a list of 1 to 20 aliases, each 1 to 80 characters on one line"),
        ('\n[truth.entities]\nexcloud = ["' + "İ" * 80 + '"]\n',
         "truth.entities.excloud must be a list of 1 to 20 aliases, each 1 to 80 characters on one line"),
        ('\n[truth.entities]\nnope = ["Nope"]\n', "truth.entities.nope names no fact key's entity"),
        ('\n[truth.attributes]\nnope = ["Nope"]\n', "truth.attributes.nope names no fact key's attribute"),
        ('\n[truth.entities]\nexcloud = ["!!!"]\n', 'truth.entities.excloud: alias "!!!" has no words'),
        ('\n[truth.attributes]\ngpu_memory = ["--"]\n', 'truth.attributes.gpu_memory: alias "--" has no words'),
        ('\n[truth.entities]\nexcloud = ["EC"]\nexbasic = ["ec"]\n',
         'truth.entities: "EC" is an alias of both exbasic and excloud'),
        ('\n[truth.source_rank]\nkinds = ["document", "document"]\n',
         "truth.source_rank.kinds must be a list of unique source kinds"),
        ('\n[truth.source_rank]\nkinds = ["nope"]\n', "truth.source_rank.kinds must be a list of unique source kinds"),
        ('\n[truth.source_rank]\nuri_prefixes = ["ftp://example.com/"]\n',
         "truth.source_rank.uri_prefixes must be a list of up to 20 http(s) prefixes"),
        ("\n[truth.source_rank]\nuri_prefixes = " + json.dumps([f"https://example.com/{i}" for i in range(21)]) + "\n",
         "truth.source_rank.uri_prefixes must be a list of up to 20 http(s) prefixes"),
    ]
    for text, message in cases:
        assert block(text) == message, text
    path = tmp_path / "ok.toml"
    path.write_text(truth_config(truth_block=f"\n[truth.source_rank]\nkinds = [{kinds}]\n"), encoding="utf-8")
    assert load_config(path).truth.rank_kinds == schemas.SOURCE_KINDS
    granted = render_config(principal=PRINCIPAL, agents={**AGENTS, "keys": {"operations": ["set_aliases"]}},
                            fact_keys=KEYS)
    assert _config_error(tmp_path, granted) == "set_aliases cannot be granted"


def test_set_aliases_refusals(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-alias", "Alias source.")))

    def op(*items) -> dict:
        return {"op": "set_aliases", "row": {"aliases": list(items)}, "source": {"source_id": "src-alias", "locator": "x"}}

    cases = [
        (op({"kind": "attribute", "alias": "VRAM", "target": "gpu_memory"}),
         'op 0 (set_aliases): alias "VRAM" is not in normal form'),
        (op({"kind": "attribute", "alias": "vram", "target": "gpu_memory"},
            {"kind": "attribute", "alias": "vram", "target": "max_context"}),
         'op 0 (set_aliases): alias "vram" is listed twice for attribute'),
        (op({"kind": "attribute", "alias": "vram"}), "packet is invalid: /ops/0/row/aliases/0/target is required"),
    ]
    for packet_op, message in cases:
        assert _err(_apply(inst, "brain", packet_op, reserved=True)) == message
    assert _err(_apply(inst, "alex", op())) == "op 0 (set_aliases): not allowed for agent alex"


def test_new_ops_never_raise(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _people(inst, ("per-sam-example", "Sam Example"))
    _two_sides(inst)
    _ok(_apply(inst, "alex", _src("src-sam", "Sam says the context window is 128K tokens for everyone.",
                                  kind="user_note", author="per-sam-example"),
               _src("src-site", "The site is https:// for now.")))
    blank = {"source_id": "src-docs-ec-limits", "locator": " "}
    claim = dict(_clm("clm-blank", CTX, "128K", "Context window: 128K tokens", "src-docs-ec-limits"), source=blank)
    resolve = dict(_resolve(CTX, ["clm-a"], ["clm-b"], "the context window is 128K tokens", "src-sam"),
                   source={"source_id": "src-sam", "locator": " "})
    url = _clm("clm-url", "exunits.site", "https://", "The site is https:// for now", "src-site")
    alias = {"op": "set_aliases", "row": {"aliases": [{"kind": "entity", "alias": "excloud", "target": "Excloud"}]},
             "source": {"source_id": "src-sam", "locator": "x"}}
    for agent, op, reserved in (("alex", claim, False), ("alex", resolve, False), ("alex", url, False),
                                ("brain", alias, True)):
        outcome = _apply(inst, agent, op, reserved=reserved)
        assert outcome.exit_code == 1, outcome.receipt
    # The envelope now refuses the blank locator, so a duplicate-id insert reaches the
    # IntegrityError catch instead.
    writer = Applier(inst)
    insert = writer._insert_claim
    writer._insert_claim = lambda columns: (insert(columns), insert(columns))
    try:
        twice = writer.apply(json.dumps(make_packet("alex", dict(claim, source={**blank, "locator": "p9"}))).encode())
    finally:
        writer.close()
    assert (twice.exit_code, _err(twice)) == (1, "op 0 (add_claim): " + database_refusal("UNIQUE constraint failed: claims.id", "claims"))
    before = _fact(inst, CTX)
    history = ro_rows(inst, "SELECT count(*) FROM fact_history")
    bare = _ok(_apply(inst, "bot", upsert_op("upsert_fact", {"key": CTX}, "src-docs-ec-limits", base=before["version"])))
    assert bare.exit_code == 0 and bare.receipt["targets"] == []
    assert _fact(inst, CTX) == before
    assert ro_rows(inst, "SELECT count(*) FROM fact_history") == history


def test_resolved_version_moves_entity_headlines(tmp_path: Path, clock: Clock) -> None:
    from brain import migrate
    from tests.test_migrate import _v4_config, v4_store

    inst = v4_store(tmp_path, _v4_config())
    migrate.migrate(inst / "brain.sqlite3")
    version_rowid = ro_rows(inst, "SELECT rowid FROM facts WHERE key = 'excloud.current_version'")[0][0]
    _people(inst, ("per-sam-example", "Sam Example"))
    _ok(_apply(inst, "alex", _src("src-rel", "Release 1.3: max context is 128K tokens."),
               _src("src-sam", "Sam: the current version is 1.3, confirmed today.", kind="user_note",
                    author="per-sam-example")))
    _ok(_apply(inst, "notes", _clm("clm-r13", CTX, "128K", "max context is 128K tokens", "src-rel", scope="1.3")))
    assert _fact(inst, CTX) is None
    settled = _ok(_apply(inst, "alex", _resolve("excloud.current_version", [f"clm-m5-{version_rowid}"], [],
                                                "the current version is 1.3, confirmed today", "src-sam")))
    assert {"table": "facts", "id": CTX, "action": "insert"} in settled.receipt["targets"]
    assert _fact(inst, "excloud.current_version")["status"] == "current"
    fact = _fact(inst, CTX)
    assert (fact["status"], fact["value_int"]) == ("current", 128000)


def test_migrated_version_value_is_kept_but_named(tmp_path: Path, clock: Clock) -> None:
    """A version value migrated from 0.6.1 that no scope can match is never refused on an
    unchanged re-send, a new such value is refused, and brain check names the stored one."""
    from brain import migrate
    from tests.test_migrate import V4_FACTS, _v4_config, v4_store

    facts = tuple(item for item in V4_FACTS if item[0] != "excloud.current_version") + (
        ("excloud.current_version", "value_text", "1.3 beta", "current", "notes:notes-seed-3"),
    )
    inst = v4_store(tmp_path, _v4_config(), facts=facts)
    assert migrate.migrate(inst / "brain.sqlite3") == (4, migrate.latest_version())
    # The strict writes quote a source that names the values.
    _ok(_apply(inst, "alex", _src("src-beta", "The current version is 1.3 beta. The next one is 1.4 beta.")))
    _ok(_apply(inst, "notes", _put(inst, "excloud.current_version", "1.3 beta", "src-beta", basis="reported",
                                   quote="The current version is 1.3 beta", raw="1.3 beta")))
    assert _fact(inst, "excloud.current_version")["value_text"] == "1.3 beta"
    refused = _apply(inst, "notes", _put(inst, "excloud.current_version", "1.4 beta", "src-beta", basis="reported",
                                         quote="The next one is 1.4 beta", raw="1.4 beta"))
    assert _err(refused) == "op 0 (upsert_fact): a current_version value must be a scope label"
    _code, _out, stderr = _cli("check", "--instance", inst)
    assert 'brain: fact key excloud.current_version holds "1.3 beta", which no claim scope can match' in stderr.splitlines()


def test_wrong_scope_fixed_by_supersede(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-rel", "Release 1.3: max context is 128K tokens. Release 1.3: 96 GB of VRAM.")))
    _ok(_apply(inst, "notes", _clm("clm-wrong", CTX, "128K", "max context is 128K tokens", "src-rel", scope="1.2"),
               _clm("clm-wrong2", "excloud.gpu_memory", "96 GB", "96 GB of VRAM", "src-rel", scope="1.2",
                    locator="para 2")))
    _ok(_apply(inst, "notes", _clm("clm-right", CTX, "128K", "max context is 128K tokens", "src-rel", scope="1.3",
                                   supersedes=["clm-wrong"], locator="para 3")))
    assert _claim_states(inst, CTX) == {"clm-right": ("live", None), "clm-wrong": ("superseded", "clm-right")}
    held = _apply(inst, "bot", _clm("clm-right2", "excloud.gpu_memory", "96 GB", "96 GB of VRAM", "src-rel",
                                    scope="1.3", supersedes=["clm-wrong2"], locator="para 4"))
    assert held.exit_code == 4
    assert held.warning == "op 0 (add_claim): superseding clm-wrong2 by notes needs a person's approval"
    alone = _apply(inst, "alex", _resolve("excloud.gpu_memory", [], ["clm-wrong2"], "96 GB of VRAM", "src-rel",
                                          scope="1.2"))
    assert _err(alone) == "packet is invalid: /ops/0/row/keep " + advice.size_error(
        "items", "minimum", 1
    )
    assert _claims(inst, "excloud.gpu_memory")["clm-wrong2"]["status"] == "live"


def test_fact_get_retired_item(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-price", "Price list: A$49.50 a seat.")))
    _ok(_apply(inst, "notes", _clm("clm-price", "exbasic.price", "A$49.50", "A$49.50 a seat", "src-price")))
    _ok(_apply(inst, "notes", _put(inst, "exbasic.price", None, "src-price", status="retired")))
    item = _fact_get(inst, "exbasic.price")["facts"][0]
    assert (item["status"], item["value"], item["basis"]) == ("retired", 49.5, "reported")
    assert item["claims"] == {"scope": "now", "confidence": "low"}
    assert list(item)[-1] == "claims" and "source" in item
    hidden = _fact_get(inst, "exbasic.*")
    assert (hidden["facts"], hidden["total"]) == ([], 0)
    shown = _fact_get(inst, "exbasic.*", include_retired=True)
    assert [entry["key"] for entry in shown["facts"]] == ["exbasic.price"]
    assert shown["facts"][0]["claims"] == {"scope": "now", "confidence": "low"}


# -- the stateful fuzz --------------------------------------------------------------------

FUZZ_RAWS = [
    "80GB", "80 GB", "80 gigabytes", "1e400", "-5", "1,000", "128K", "128k", "1.5M", "A$5", "€5", "US$5", "$5",
    "5 AUD", "AUD5", "45%", "45 %", "90 min", "1.5 hours", "0.27 TB", "270 GB", "9" * 400, "0." + "0" * 300 + "1",
    "-0", "+7", "١٢", "１２", "12 seats", "1.2K seats", "12 tokens", "true", "yes", "no", "2026-02-30", "2026-10-09",
    "https://x.example", "http://", "eu", "EU", "eu.", "au", "us", "1.3", "1-3", "1.2", "now", "NOW", "1,2345",
    "5 k", "5k GB", "1 GiB", "1 TiB", "0", "0.0", "-0.0", "3.14159265358979323846264338327950288", "1 ms", "1 day",
    "x", "Seed", "9223372036854775807", "9223372036854775808", "-9223372036854775807", "1" + "0" * 30, "12 SEATS",
    "128 Ktokens", "¥5", "£5", "5 £", "5 JPY", "C$5", "NZ$5", "5 eur", "1.3 beta", "1.3 ",
]
FUZZ_SCOPES = ["", "", "", "1.2", "1.3", "1-3", "2.0", "now", "A" * 40, "x.y_z-1", "Now"]
# The raws upsert_fact renders for the fuzz's values, so a resolution can quote them too.
FUZZ_RENDERED = ["80", "-1", "9007199254740993", "49.5", "0.1", "100000000000000000000", "Seed", "y", "false",
                 "https://x.example/a", "eu."]
# A legacy importer for the fuzz's legacy claims.
FUZZ_IMPORTER = {"kind": "collector", "manual_sources": True, "legacy_import": True, "operations": "*"}
# Raws each value type takes, so most legacy claims parse and their links resolve.
FUZZ_TYPED_RAWS = {
    "int": ["80", "128K", "12", "0"], "number": ["49.5", "0.1", "45"], "text": ["Seed", "1.3", "x"],
    "bool": ["true", "false", "yes"], "date": ["2026-10-09"], "url": ["https://x.example"], "enum": ["au", "us", "EU"],
}


def test_new_ops_never_raise_under_a_stateful_fuzz(tmp_path: Path, clock: Clock) -> None:
    """Valid packets of the new ops reach the deep paths: units, supersedes, races, holds and the
    derivation. Nothing raises, no exit 5, the store never pauses, and the headlines stay consistent."""
    import random

    from brain.applier import NotAllowed

    from brain.applier import verified_id

    rng = random.Random(1501)
    faults = random.Random(64)  # its own stream, so the op mix stays the same
    keys = [item["key"] for item in KEYS] + ["vendor.current_version"]
    types = {item["key"]: item["value_type"] for item in KEYS}
    inst = _inst(tmp_path, truth_config(keys=[*KEYS, {"key": "vendor.current_version", "value_type": "text"}],
                                        agents={**AGENTS, "importer": FUZZ_IMPORTER}))
    quotes = {raw: f"the value reads {raw} in this line" for raw in FUZZ_RAWS + FUZZ_RENDERED}
    content = " ".join(f"{quote}." for quote in quotes.values())
    writers = {"plain": Applier(inst), "reserved": Applier(inst, allow_reserved_agent=True)}
    statuses: dict[str, int] = {}
    reached = {"verified": 0, "linked": 0}

    def send(agent, *ops, mode="plain", fault=False):
        writer = writers[mode]
        if fault:
            # The envelope refuses a blank locator, so the fuzz reaches each new op's
            # IntegrityError catch by a duplicate-id insert below it: the claim inserted twice, or after
            # the derivation (resolve_fact inserts no claim).
            insert, derive = writer._insert_claim, writer._derive
            writer._insert_claim = lambda columns: (insert(columns), insert(columns))
            writer._derive = lambda *args, **kw: (
                derive(*args, **kw), writer.con.execute("INSERT INTO claims SELECT * FROM claims LIMIT 1"))
        try:
            outcome = writer.apply(json.dumps(make_packet(agent, *ops)).encode())
        finally:
            if fault:
                del writer._insert_claim, writer._derive
        assert outcome.exit_code in (0, 1, 3, 4), (outcome.exit_code, outcome.integrity, ops)
        assert not (inst / "PAUSED").exists(), (inst / "PAUSED").read_text()
        statuses[outcome.receipt["status"]] = statuses.get(outcome.receipt["status"], 0) + 1
        return outcome

    def rows(sql, params=()):
        return ro_rows(inst, sql, params)

    try:
        send("alex", _src("src-f0", content), _src("src-person", "Alex says " + content, kind="user_note",
                                                   author="per-alex-example"),
             _src("src-sam", "Sam says " + content, kind="email"))
        sources = ["src-f0"]
        # A blank locator on either new write path is refused by the envelope.
        for op in (_clm("clm-blank", keys[0], "80", quotes["80"], "src-f0", locator=" "),
                   _resolve(keys[0], ["clm-nope"], [], quotes["80"], "src-person", locator=" ")):
            assert send("alex", op).receipt["error"] == (
                "packet is invalid: /ops/0/source/locator does not match the required pattern")
        for i in range(1500):
            clock.at(f"2026-10-09T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z")
            roll, agent, key = rng.random(), rng.choice(["alex", "notes", "bot", "solo"]), rng.choice(keys)
            claim_ids = [row[0] for row in rows("SELECT id FROM claims ORDER BY id")]
            if roll < 0.36:
                raw = rng.choice(FUZZ_RAWS)
                row = {"id": f"clm-f{i}", "key": key, "value_raw": raw,
                       "basis": rng.choice(["observed", "reported", "inferred"]),
                       "quote": rng.choice([quotes[raw], quotes[raw], "the value reads nothing in this line"])}
                scope = rng.choice(FUZZ_SCOPES)
                if scope or rng.random() < 0.2:
                    row["scope"] = scope
                if rng.random() < 0.15:
                    row["planned"] = rng.random() < 0.7
                if rng.random() < 0.3:
                    pool = claim_ids[-10:] + [f"clm-f{i + 1}", f"clm-f{i + 2}", f"clm-f{i}"]
                    row["supersedes"] = sorted(set(rng.sample(pool, k=min(len(pool), rng.randint(1, 3)))))
                if rng.random() < 0.1 and claim_ids:
                    row["id"] = rng.choice(claim_ids)
                locator = rng.choice(["p1", "p2", "case", " "])
                # The envelope refuses " " (sent below), so a blank becomes a fault below it.
                fault = locator == " "
                send(agent, {"op": "add_claim", "row": row,
                             "source": {"source_id": rng.choice(sources), "locator": "p1" if fault else locator}},
                     fault=fault)
            elif roll < 0.50:
                fact = rows("SELECT version FROM facts WHERE key = ?", (key,))
                base = fact[0][0] if fact and rng.random() < 0.9 else None
                value = {
                    "int": rng.choice([80, 0, -1, 2**53 + 1]), "number": rng.choice([49.5, 0.1, 1e20, -0.0, 1.7e308]),
                    "text": rng.choice(["Seed", "1.3", "1-3", "x" * 2000, "  y "]), "bool": rng.choice([True, False]),
                    "date": "2026-10-09", "url": "https://x.example/a", "enum": rng.choice(["au", "eu", "EU", "eu."]),
                }[types.get(key, "text")]
                row = {"key": key}
                if rng.random() < 0.6:
                    row["value"] = value
                    # A quote and raw from a source that holds them, mostly right.
                    raw = units.render(types.get(key, "text"), value)
                    if rng.random() < 0.9:
                        row["value_raw"] = raw
                        row["quote"] = quotes.get(raw, "the value reads nothing in this line")
                if rng.random() < 0.15:
                    row["status"] = rng.choice(["current", "retired", "disputed"])
                if rng.random() < 0.7:
                    row["last_verified_at"] = clock.now
                if rng.random() < 0.6:
                    row["basis"] = "observed"
                if rng.random() < 0.6:
                    row["as_of"] = "2026-10-01"
                send(agent, upsert_op("upsert_fact", row, rng.choice(sources), base=base))
            elif roll < 0.56:
                # A legacy claim in any status, with or without a quote and a link.
                raw = rng.choice(FUZZ_RAWS) if rng.random() < 0.4 else rng.choice(FUZZ_TYPED_RAWS[types.get(key, "text")])
                status = rng.choice(["live", "planned", "superseded", "rejected", None])
                row = {"id": f"clm-l{i}", "key": key, "value_raw": raw, "basis": rng.choice(["reported", "observed"])}
                if status is not None:
                    row["status"] = status
                if rng.random() < 0.3 and raw in quotes:
                    row["quote"] = quotes[raw]
                ops = [{"op": "add_claim", "row": row, "source": {"source_id": rng.choice(sources), "locator": "lg"}}]
                if status == "superseded" or rng.random() < 0.05:
                    others = claim_ids[-10:] + [f"clm-l{i}", "clm-nope"]
                    target = f"clm-l{i}b" if rng.random() < 0.7 else rng.choice(others)
                    row["superseded_by"] = target
                    if target == f"clm-l{i}b":
                        ops.append({"op": "add_claim", "row": {"id": target, "key": key, "value_raw": raw,
                                                               "basis": "reported"},
                                    "source": {"source_id": "src-f0", "locator": "lg2"}})
                if status == "rejected" and rng.random() < 0.9:
                    row["resolution_source_id"] = rng.choice(["src-person", "src-missing"])
                    row["resolution_locator"] = "r1"
                if rng.random() < 0.15:
                    row["supersedes"] = [rng.choice(claim_ids)] if claim_ids else ["clm-nope"]
                outcome = send("importer", *ops)
                if outcome.receipt["status"] == "applied" and "superseded_by" in row:
                    found = rows("SELECT superseded_by FROM claims WHERE id = ?", (row["id"],))
                    if found and found[0][0] == row["superseded_by"]:
                        reached["linked"] += 1
            elif roll < 0.61:
                # Verify an unchecked claim, mostly with its own source and value.
                unchecked = rows("SELECT id, source_id, value_raw FROM claims WHERE checked = 0 ORDER BY id")
                pick = rng.choice(unchecked) if unchecked and rng.random() < 0.9 else (
                    rng.choice(claim_ids) if claim_ids else "clm-nope", "src-f0", "x")
                source = pick[1] if rng.random() < 0.9 else rng.choice(sources)
                quote = quotes.get(pick[2], "the value reads nothing in this line") if rng.random() < 0.85 else (
                    "the value reads nothing in this line")
                op = {"op": "verify_claim", "row": {"claim_id": pick[0], "quote": quote},
                      "source": {"source_id": source, "locator": rng.choice(["v1", "v2"])}}
                outcome = send(agent, op)
                if outcome.receipt["status"] == "applied" and any(
                    item["action"] == "insert" and item["table"] == "claims" for item in outcome.receipt["targets"]
                ):
                    copy = verified_id(pick[0], source, op["source"]["locator"], quote)
                    if rows("SELECT 1 FROM claims WHERE id = ? AND checked = 1", (copy,)):
                        reached["verified"] += 1
            elif roll < 0.72:
                live = rows("SELECT id, scope FROM claims WHERE key = ? AND status = 'live' ORDER BY id", (key,))
                scope = rng.choice(["", "now", "1.3", "1.2", "2.0"] + [item[1] for item in live] * 3)
                reader = store.connect_readonly(inst / "brain.sqlite3")
                try:
                    bucket = truth.state(reader, key).bucket_for(scope)
                finally:
                    reader.close()
                person = rng.choice(["src-person", "src-person", "src-sam", "src-f0"])
                if bucket.claims and rng.random() < 0.6:
                    # Well formed: name the whole bucket, keep the best value's claims, reject the rest and
                    # quote a person, so resolutions reach their writes (the deep path the fuzz exists for).
                    best = bucket.claims[0]
                    keep = [claim["id"] for claim in bucket.claims if claim["canonical"] == best["canonical"]][:40]
                    reject = [claim["id"] for claim in bucket.claims if claim["id"] not in keep][:40 - len(keep)]
                    person = "src-person"
                else:
                    ids = [item[0] for item in live if rng.random() < 0.2 or item[1] == scope]
                    if rng.random() < 0.2 and ids:
                        ids = ids[:-1]
                    if rng.random() < 0.1 and claim_ids:
                        ids.append(rng.choice(claim_ids))
                    ids = sorted(set(ids))[:40] or ["clm-nope"]
                    cut = rng.randint(1, len(ids))
                    keep, reject = ids[:cut], ids[cut:]
                    if rng.random() < 0.3:
                        keep = [rng.choice(ids)]
                        reject = [item for item in ids if item not in keep]
                found = rows(f"SELECT value_raw FROM claims WHERE id IN ({','.join('?' * len(keep))})", tuple(keep))
                quote = quotes.get(found[0][0] if found else "x", "the value reads nothing in this line")
                locator = rng.choice(["m1", " "])
                fault = locator == " " and faults.random() < 0.5  # as for add_claim
                send(agent, _resolve(key, keep, reject, quote, person, scope=scope, locator="m1" if fault else locator),
                     fault=fault)
            elif roll < 0.78:
                sid = f"src-fv{i}"
                outcome = send(agent, {"op": "add_source", "row": {
                    "id": sid, "kind": rng.choice(["document", "slack", "web"]), "title": "Synthetic",
                    "source_date": f"2026-09-0{rng.randint(1, 9)}", "author_person_id": None, "content": content,
                    "supersedes_id": rng.choice(sources + ["src-missing"])}})
                if outcome.receipt["status"] == "applied":
                    sources.append(sid)
            elif roll < 0.82:
                aliases = [
                    {"kind": rng.choice(["entity", "attribute"]),
                     "alias": rng.choice(["vram", "ec", "Example Cloud", "x y", "gpu memory", "a.b"]),
                     "target": rng.choice(["excloud", "gpu_memory", "Excloud", "nope", "max_context"])}
                    for _ in range(rng.randint(0, 4))
                ]
                send("brain", {"op": "set_aliases", "row": {"aliases": aliases},
                               "source": {"source_id": "src-f0", "locator": "fz"}}, mode="reserved")
            elif roll < 0.87:
                signals = rows("SELECT id, version FROM signals ORDER BY id")
                row = {"id": rng.choice([item[0] for item in signals] + ["sig-conflict-x-1", "sig-mine"]),
                       "kind": rng.choice(["conflict", "other"]), "subject_table": "facts", "subject_id": key,
                       "detail": "x", "status": rng.choice(["open", "dismissed"]), "basis": "observed"}
                current = [item for item in signals if item[0] == row["id"]]
                send(agent, upsert_op("upsert_signal", row, "src-f0", base=current[0][1] if current else None))
            elif roll < 0.91:
                current = rows("SELECT version FROM fact_keys WHERE key = ?", (key,))
                row: dict = {"key": key}
                choice = rng.random()
                if choice < 0.3:
                    row["value_type"] = rng.choice(["text", "int", "number", "enum"])
                elif choice < 0.6:
                    row["unit"] = rng.choice(["", "GB", "tokens", "AUD"])
                else:
                    row["allowed_values"] = rng.choice([["au"], ["au", "eu", "us"], ["EU"], None])
                if row.get("value_type") == "enum" and "allowed_values" not in row:
                    row["allowed_values"] = ["au", "eu"]
                send("brain", upsert_op("upsert_fact_key", row, "src-f0", base=current[0][0] if current else None),
                     mode="reserved")
            else:
                held = rows("SELECT change_id FROM write_log WHERE status = 'held' ORDER BY change_id")
                if held:
                    try:
                        outcome = writers["plain"].approve(rng.choice(held)[0], "alex")
                    except NotAllowed:
                        continue
                    assert outcome.exit_code in (0, 1, 3, 4), outcome.integrity
                    assert not (inst / "PAUSED").exists()
    finally:
        for writer in writers.values():
            writer.close()
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        assert store.truth_problems(con) == []
    finally:
        con.close()
    assert {"applied", "rejected", "conflict", "held"} <= set(statuses), statuses
    assert rows("SELECT count(*) FROM signals WHERE kind = 'conflict' AND status = 'resolved'")[0][0] > 0
    # Coverage, so a fuzz that stops reaching the deep paths fails rather than passing quietly: claims
    # rejected by resolutions, blank locators refused by the envelope on both new write paths, and a
    # fault caught by each path's IntegrityError catch.
    assert rows("SELECT count(*) FROM claims WHERE status = 'rejected'")[0][0] > 0
    # The fuzz reached a verified copy and a resolved superseded_by link.
    assert reached["verified"] > 0 and reached["linked"] > 0, reached
    print(f"1a test 64 reached: {reached}, statuses {statuses}")
    for name, message in (("add_claim", "/ops/0/source/locator does not match the required pattern"),
                          ("resolve_fact", "/ops/0/source/locator does not match the required pattern"),
                          ("add_claim", "op 0 (add_claim): " + database_refusal("UNIQUE constraint failed: claims.id", "claims")),
                          ("resolve_fact", "op 0 (resolve_fact): " + database_refusal("UNIQUE constraint failed: claims.id", "claims"))):
        caught = rows("SELECT count(*) FROM write_log WHERE status = 'rejected' AND instr(error, ?) > 0 "
                      "AND json_extract(packet_json, '$.ops[0].op') = ?", (message, name))
        assert caught[0][0] > 0, (name, message)
