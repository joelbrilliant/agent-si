"""Strict writes, labelled notes and verified approval.

Every name, value and host here is invented. Tests go through Applier, extract.run, reads.run_tool,
the CLI (brain.cli.main), config.load_config and store.check_instance, on instances built with
init_instance.
"""
from __future__ import annotations

import contextlib
import io
import json
import random
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from brain import config as config_mod
from brain import advice, migrate, reads, schemas, store, strict, truth, units
from brain.applier import Applier, NotAllowed, verified_id
from brain.cli import main
from brain.config import ConfigError, load_config
from brain.init import apply_config, init_instance, render_router
from tests.conftest import context_connection, database_refusal, make_packet, render_config, ro_rows, tools_list_budgets, router_bytes, upsert_op, version_banner
from tests.test_truth import (  # noqa: F401  (clock is a fixture)
    AGENTS,
    KEYS,
    NOW,
    PRINCIPAL,
    READ_NOW,
    TRUTH_BLOCK,
    Clock,
    _claims,
    _clm,
    _conflicts,
    _fact,
    _resolve,
    _sig,
    clock,
)

CTX = "excloud.max_context"
NOTE_LABEL = "Written by an agent, unsourced. Not a fact."
LIMITS = "Context window: 128K tokens. VRAM is 80 GB per node."
SCAN_OPS = ["apply_candidate", "upsert_decision", "upsert_action", "upsert_thread_link", "add_claim"]
STRICT_AGENTS = {
    **AGENTS,
    "scan": {"kind": "extractor", "operations": SCAN_OPS, "approvers": ["alex"]},
    "careful": {"kind": "extractor", "operations": SCAN_OPS, "approval": "required", "approvers": ["alex"]},
    "importer": {"kind": "collector", "manual_sources": True, "legacy_import": True, "operations": "*"},
    "checker": {"kind": "agent", "operations": ["verify_claim"]},
}
STRICT_KEYS_TOML = """
[[fact_keys]]
key = "exbasic.sso"
value_type = "bool"
value_words = {true = ["included", "enabled"], false = ["not offered", "not included", "not enabled"]}

[[fact_keys]]
key = "exbasic.tier"
value_type = "enum"
allowed_values = ["basic", "pro"]
value_words = {pro = ["professional"]}
"""
EXTRA_KEYS = [
    {"key": "exbasic.sso", "value_type": "bool"},
    {"key": "exbasic.tier", "value_type": "enum", "allowed_values": ["basic", "pro"]},
]
# A source whose sentences the candidates quote: a candidate's quote needs at least 5 words.
PLAN = (
    "The context window is 128K tokens on the Pro plan. Pro plan buyers get 128K tokens with every seat. "
    "Release 1.3 has a context window of 64K tokens. The context window grows to 256K tokens next year. "
    "The context window is 100K tokens on the Basic plan."
)


def strict_config(*, agents: dict | None = None, extra: str = "", writes: str = "") -> str:
    """The base agents and keys plus the strict ones. scan leaves approval out."""
    text = render_config(principal=PRINCIPAL, agents=agents or STRICT_AGENTS, fact_keys=KEYS)
    text = re.sub(r'(\[agents\.scan\]\n(?:[^\[\n].*\n)*?)approval = "none"\n', r"\1", text)
    return text + STRICT_KEYS_TOML + extra + writes + TRUTH_BLOCK


def _inst(tmp_path: Path, config: str | None = None) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    inst = Path(init_instance("demo", tmp_path, config_text=config or strict_config())["instance"])
    _ok(_apply(inst, "alex", _src("src-ec-limits", LIMITS, title="Example Cloud limits"),
               _src("src-ec-plan", PLAN, title="Example Cloud plan notes", date="2026-10-01"),
               upsert_op("upsert_person", {"id": "per-sam-example", "display_name": "Sam Example"}, "src-ec-limits")))
    return inst


def _src(sid, content, *, kind="document", date="2026-09-01", author=None, title="Synthetic source",
         supersedes=None):
    row = {"id": sid, "kind": kind, "title": title, "source_date": date, "author_person_id": author,
           "content": content}
    if supersedes is not None:
        row["supersedes_id"] = supersedes
    return {"op": "add_source", "row": row}


def _apply(inst: Path, agent: str, *ops, reserved: bool = False, change_id: str | None = None):
    writer = Applier(inst, allow_reserved_agent=reserved)
    try:
        return writer.apply(json.dumps(make_packet(agent, *ops, change_id=change_id)).encode())
    finally:
        writer.close()


def _ok(outcome):
    assert outcome.receipt["status"] == "applied", (outcome.receipt["status"], outcome.receipt["error"])
    return outcome


def _err(outcome, status: str = "rejected") -> str:
    assert outcome.receipt["status"] == status, outcome.receipt
    return outcome.receipt["error"]


def _fact_op(key, value, source, *, quote=None, raw=None, status=None, base=None, basis="observed",
             verified="2026-10-01T00:00:00Z", locator="para 1", as_of="2026-10-01"):
    row: dict = {"key": key, "basis": basis, "as_of": as_of, "last_verified_at": verified}
    if value is not None:
        row["value"] = value
    if quote is not None:
        row["quote"] = quote
    if raw is not None:
        row["value_raw"] = raw
    if status is not None:
        row["status"] = status
    return upsert_op("upsert_fact", row, source, locator=locator, base=base)


def _base(inst: Path, table: str, key_col: str, key) -> int | None:
    found = ro_rows(inst, f"SELECT version FROM {table} WHERE {key_col} = ?", (key,))
    return found[0][0] if found else None


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


def _read(inst: Path, tool: str, now: str = READ_NOW, **arguments):
    return reads.run_tool(inst, tool, arguments, now=now)


def _brief(inst: Path, now: str = READ_NOW) -> list[str]:
    result = _read(inst, "brief", now=now)
    assert result.is_error is False, result.text
    return result.text.splitlines()


def _held(inst: Path, now: str = READ_NOW) -> list[str]:
    return [line for line in _brief(inst, now) if line.startswith("Held: ")]


def _extract(inst: Path, doc: dict, agent: str = "scan") -> tuple[int, dict | None, str]:
    raw = store.canonical_json(doc).encode()
    directory = inst.parent / "candidates"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{store.sha256_text(raw.decode())[:16]}.json"
    path.write_bytes(raw)
    code, out, err = _cli("extract", "--instance", inst, "--as", agent, path)
    return code, (json.loads(out) if out.strip() else None), err


def _claim_doc(key: str, raw: str, quote: str, *, source: str = "src-ec-plan", locator: str = "para 1",
               scope=None, supersedes=None) -> dict:
    fields: dict = {"key": key, "value_raw": raw}
    if scope is not None:
        fields["scope"] = scope
    if supersedes is not None:
        fields["supersedes"] = list(supersedes)
    return {"candidate_version": 2, "kind": "claim_candidate", "source_id": source, "locator": locator,
            "quote": quote, "fields": fields}


def _action_doc(title: str, *, basis: str = "reported", detail: str | None = None, next_step: str | None = None,
                due: str | None = None, quote: str = "The context window is 128K tokens on the Pro plan",
                source: str = "src-ec-plan") -> dict:
    fields: dict = {"title": title, "status": "open", "status_basis": basis, "workstream": "general"}
    if detail is not None:
        fields["detail"] = detail
    if next_step is not None:
        fields["next_step"] = next_step
    if due is not None:
        fields["due_date"] = due
        fields["due_kind"] = "deadline"
    return {"candidate_version": 1, "kind": "action_candidate", "source_id": source, "locator": "para 1",
            "quote": quote, "fields": fields}


def _decision_doc(title: str, *, detail: str | None = None) -> dict:
    fields: dict = {"title": title, "decided_on": "2026-10-01", "decided_by": "per-sam-example",
                    "status": "in_effect", "workstream": "general"}
    if detail is not None:
        fields["detail"] = detail
    return {"candidate_version": 1, "kind": "decision_candidate", "source_id": "src-ec-plan", "locator": "para 1",
            "quote": "The context window is 128K tokens on the Pro plan", "fields": fields}


GOOD_QUOTE = "The context window is 128K tokens on the Pro plan"


def _log(inst: Path, change_id: str) -> dict:
    con = store.connect_readonly(inst / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM write_log WHERE change_id = ?", (change_id,)).fetchone()
        return dict(row)
    finally:
        con.close()


def _counts(inst: Path) -> tuple[int, int, int]:
    return tuple(ro_rows(inst, f"SELECT count(*) FROM {table}")[0][0] for table in ("claims", "facts", "signals"))


def _old_claim(inst: Path, claim_id: str, key: str, column: str, value, raw: str, source: str, agent: str,
               *, stamp: str = "2026-10-01T00:00:00Z", fact: bool = True) -> None:
    """An unchecked claim as 0.7.0a1's upsert_fact wrote it, with the fact derived from it, written on a
    context connection."""
    value_type = {"value_int": "int", "value_text": "text", "value_real": "number", "value_date": "date"}[column]
    if key.startswith("eng.repo.ci_state"):
        value_type = "enum"
    con = context_connection(inst)
    try:
        con.execute("BEGIN")
        con.execute(
            f"INSERT INTO claims (id, key, scope, value_type, {column}, value_raw, status, basis, as_of, source_id, "
            "locator, quote, checked, supersedes, proposed_by, created_at, created_by, updated_at, updated_by) "
            "VALUES (?, ?, '', ?, ?, ?, 'live', 'observed', '2026-10-01', ?, 'para 1', '', 0, '[]', ?, ?, ?, ?, ?)",
            (claim_id, key, value_type, value, raw, source, agent, stamp, f"{agent}:old", stamp, f"{agent}:old"),
        )
        if fact:
            con.execute(
                f"INSERT INTO facts (key, value_type, {column}, status, basis, as_of, last_verified_at, source_id, "
                "locator, quote, version, created_at, updated_at, updated_by) "
                "VALUES (?, ?, ?, 'current', 'observed', '2026-10-01', ?, ?, 'para 1', '', 1, ?, ?, ?)",
                (key, value_type, value, stamp, source, stamp, stamp, f"{agent}:old"),
            )
        con.execute("COMMIT")
    finally:
        con.close()


# -- field classes ------------------------------------------------------------------------------


def test_field_classes_cover_every_text_column(tmp_path: Path) -> None:
    inst = Path(init_instance("demo", tmp_path)["instance"])
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        tables = store.tables_to_guard(con)
        columns = {
            f"{table}.{row[1]}"
            for table in tables
            for row in con.execute(f"PRAGMA table_info({table})")
            if row[2].upper() == "TEXT"
        }
    finally:
        con.close()
    assert len(tables) == 29
    assert len(columns) == 302
    assert set(schemas.FIELD_CLASS) == columns
    assert set(schemas.FIELD_CLASS.values()) <= set(schemas.FIELD_CLASSES)
    counts = Counter(schemas.FIELD_CLASS.values())
    assert [counts[name] for name in schemas.FIELD_CLASSES] == [9, 80, 9, 8, 196]


def test_note_fields_match_note_meta(tmp_path: Path) -> None:
    inst = Path(init_instance("demo", tmp_path)["instance"])
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        sql = con.execute("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'note_meta'").fetchone()[0]
        names = {row[1] for table in ("actions", "checklist_items", "projects", "decisions", "records")
                 for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute(sql)
        allowed: dict[str, set[str]] = {}
        for entity in ("action", "checklist_item", "project", "decision", "record"):
            for name in sorted(names):
                try:
                    probe.execute(
                        "INSERT INTO note_meta VALUES (?, 'x', ?, 'alex', '2026-10-01T00:00:00Z')", (entity, name)
                    )
                except sqlite3.IntegrityError:
                    continue
                allowed.setdefault(entity, set()).add(name)
    finally:
        probe.close()
    derived = {schemas.NOTE_ENTITY[op]: set(fields) for op, fields in schemas.NOTE_FIELDS.items()}
    assert derived == allowed
    assert set(schemas.NOTE_FIELDS) == set(schemas.NOTE_ENTITY)
    assert schemas.NOTE_FIELDS["upsert_action"] == ("detail", "next_step", "close_reason")
    assert schemas.NOTE_FIELDS["upsert_project"] == ("description", "goal")


# -- facts --------------------------------------------------------------------------------------


def test_upsert_fact_needs_quote_and_value_raw(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    bare = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits"))
    assert _err(bare) == "op 0 (upsert_fact): a value needs quote and value_raw"
    half = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens"))
    assert _err(half) == "op 0 (upsert_fact): a value needs quote and value_raw"
    path, _change = _packet_file(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", raw="128K"))
    code, out, _stderr = _cli("apply", "--instance", inst, path)
    assert code == 1 and json.loads(out)["error"] == "op 0 (upsert_fact): a value needs quote and value_raw"
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    stray = _fact_op(CTX, None, "src-ec-limits", quote="Context window: 128K tokens", base=_base(inst, "facts", "key", CTX))
    assert _err(_apply(inst, "bot", stray)) == "op 0 (upsert_fact): quote and value_raw go with a value"
    path, _change = _packet_file(inst, "bot", _fact_op(CTX, None, "src-ec-limits", raw="128K",
                                                       base=_base(inst, "facts", "key", CTX)))
    code, out, _stderr = _cli("apply", "--instance", inst, path)
    assert code == 1 and json.loads(out)["error"] == "op 0 (upsert_fact): quote and value_raw go with a value"
    # A retired row's value is only type-checked, and a retired row takes no quote or value_raw.
    retired = _fact_op(CTX, 128000, "src-ec-limits", status="retired", base=_base(inst, "facts", "key", CTX))
    _ok(_apply(inst, "bot", retired))
    assert _fact(inst, CTX)["status"] == "retired"
    quoted = _fact_op(CTX, 128000, "src-ec-limits", status="retired", quote="Context window: 128K tokens",
                      base=_base(inst, "facts", "key", CTX))
    assert _err(_apply(inst, "bot", quoted)) == "op 0 (upsert_fact): a retired fact takes no quote or value_raw"


def test_value_raw_converts_to_the_value(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    claim = next(iter(_claims(inst, CTX).values()))
    assert (claim["value_int"], claim["value_raw"], claim["quote"], claim["checked"]) == (
        128000, "128K", "Context window: 128K tokens", 1)
    base = _base(inst, "facts", "key", CTX)
    other = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens",
                                         raw="131,072", base=base))
    assert _err(other) == "op 0 (upsert_fact): value_raw 131,072 is not the value 128000"
    unit = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens",
                                        raw="128K GB", base=base))
    assert _err(unit) == "op 0 (upsert_fact): value 128K GB uses GB, which doesn't convert to tokens"


# A source whose quotes hold longer values than the fragments the attacks claimed.
BR1_SOURCE = (
    "The Example Cloud context window is 1,280 tokens on the trial. Example Cloud GPU memory is 80.5 GB per node. "
    "Right now we have 1,000 seats. The list price is 12.5 AUD a month. The launch is on 14 Sep 2027. "
    "The beta launch is on 14 Sep. The site moved from https://example.com/docs/old last week. "
    "The site mirror is https://example.com.evil.example now. The site docs are at https://example.com/docs."
)
# key, value, value_raw, quote: each fragment fails C4.
BR1_FRAGMENTS = [
    (CTX, 280, "280 tokens", "The Example Cloud context window is 1,280 tokens on the trial"),
    ("excloud.gpu_memory", 5, "5 GB", "Example Cloud GPU memory is 80.5 GB per node"),
    ("exunits.seats", 1, "1", "Right now we have 1,000 seats"),
    ("exbasic.price", 5.0, "5 AUD", "The list price is 12.5 AUD a month"),
    ("exunits.launch", "2026-09-14", "14 Sep", "The launch is on 14 Sep 2027"),
    ("exunits.site", "https://example.com/docs", "https://example.com/docs",
     "The site moved from https://example.com/docs/old last week"),
    ("exunits.site", "https://example.com", "https://example.com", "The site mirror is https://example.com.evil.example now"),
]
# The true values of the same quotes still pass.
BR1_TRUE = [
    (CTX, 1280, "1,280 tokens", "The Example Cloud context window is 1,280 tokens on the trial"),
    ("exunits.seats", 1000, "1,000", "Right now we have 1,000 seats"),
    ("exbasic.price", 12.5, "12.5 AUD", "The list price is 12.5 AUD a month"),
    ("exunits.launch", "2027-09-14", "14 Sep 2027", "The launch is on 14 Sep 2027"),
    ("exunits.launch", "2026-09-14", "14 Sep", "The beta launch is on 14 Sep"),
    ("exunits.site", "https://example.com/docs", "https://example.com/docs", "The site docs are at https://example.com/docs."),
]


# A value right after a negator counts for no value, on every type C4 reads. The source is dated
# 2026-09-01, so today is 2026-09-01 and tomorrow 2026-09-02.
NR3_SOURCE = (
    "Example Cloud context is 128K tokens, not 64K tokens. Launch is not today but tomorrow. "
    "The launch is not 14 September 2026 but later. The launch is not 2026-09-14 either. "
    "The docs are not https://example.com/docs anymore. The motto is not small steps. "
    "VRAM is 80 GB per node. We have 1,000 seats with no extra cost. Launch on Monday, 14 September 2026."
)
NR3_NEGATED = [
    (CTX, 64000, "64K", "Example Cloud context is 128K tokens, not 64K tokens"),
    ("exunits.launch", "2026-09-01", "today", "Launch is not today but tomorrow"),
    ("exunits.launch", "2026-09-14", "14 September 2026", "The launch is not 14 September 2026 but later"),
    ("exunits.launch", "2026-09-14", "2026-09-14", "The launch is not 2026-09-14 either"),
    ("exunits.site", "https://example.com/docs", "https://example.com/docs",
     "The docs are not https://example.com/docs anymore"),
    ("exunits.motto", "small steps", "small steps", "The motto is not small steps"),
]
# Honest quotes, a negator elsewhere in the sentence included, still pass.
NR3_HONEST = [
    (CTX, 128000, "128K", "Example Cloud context is 128K tokens, not 64K tokens"),
    (CTX, 128000, "128K tokens", "Example Cloud context is 128K tokens, not 64K tokens"),
    ("excloud.gpu_memory", 80, "80 GB", "VRAM is 80 GB per node"),
    ("exunits.seats", 1000, "1,000 seats", "We have 1,000 seats with no extra cost"),
    ("exunits.launch", "2026-09-02", "tomorrow", "Launch is not today but tomorrow"),
    ("exunits.launch", "2026-09-14", "14 September 2026", "Launch on Monday, 14 September 2026"),
]


def test_fact_quote_is_in_the_source_and_holds_the_value(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    c1 = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="The window is 128K tokens", raw="128K"))
    assert _err(c1) == "op 0 (upsert_fact): quote is not in source src-ec-limits"
    c4 = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="VRAM is 80 GB per node", raw="128K"))
    assert _err(c4) == "op 0 (upsert_fact): value 128K is not in the quote"
    # A whitespace-only locator is refused before the claim insert, not by the claims CHECK.
    blank = _apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K",
                                         locator="   "))
    assert _err(blank) == "op 0 (upsert_fact): locator is blank"
    assert _counts(inst) == (0, 0, 0)
    # C4 reads the value by its type: a fragment of a longer number, a yearless date the quote
    # gives another year, or a URL prefix proves nothing, on upsert_fact, add_claim and verify_claim.
    for path, rule in [(path, rule) for rule in ("br1", "nr3") for path in ("upsert_fact", "add_claim", "verify_claim")]:
        inst = _inst(tmp_path / f"{rule}-{path}")
        refused, passing = (BR1_FRAGMENTS, BR1_TRUE) if rule == "br1" else (NR3_NEGATED, NR3_HONEST)
        content = BR1_SOURCE if rule == "br1" else NR3_SOURCE
        _ok(_apply(inst, "alex", _src("src-br1", content, title="exunits and exbasic notes")))
        for index, (key, value, raw, quote) in enumerate(refused + passing):
            if path == "upsert_fact":
                op = _fact_op(key, value, "src-br1", quote=quote, raw=raw, locator=f"f{index}",
                              base=_base(inst, "facts", "key", key))
                outcome = _apply(inst, "bot", op)
            elif path == "add_claim":
                outcome = _apply(inst, "notes", _clm(f"clm-br{index}", key, raw, quote, "src-br1", locator=f"f{index}"))
            else:
                _ok(_apply(inst, "importer", _legacy(f"clm-br{index}", key, raw, source="src-br1", locator=f"f{index}")))
                outcome = _apply(inst, "checker", _verify(f"clm-br{index}", quote, source="src-br1", locator=f"f{index}"))
            if index < len(refused):
                assert _err(outcome) == f"op 0 ({path}): value {raw} is not in the quote", (rule, path, raw)
            else:
                # Applied: C4 runs before the shorthand, which may then create nothing (an equal value).
                assert outcome.receipt["status"] == "applied", (rule, path, raw, outcome.receipt["error"])
    # resolve_fact reads a kept claim's value the same way: a person's "1,280 tokens" doesn't keep
    # a claim of 280, and their "280 tokens" does.
    inst = _inst(tmp_path / "resolve_fact")
    _ok(_apply(inst, "alex", _src("src-trial", "Trial notes. The trial context window is 280 tokens per request."),
               _src("src-alex-1280", "Alex: the context window is 1,280 tokens on every plan.", kind="user_note",
                    author="per-alex-example"),
               _src("src-alex-280", "Alex: the trial context window is 280 tokens per request, as noted.",
                    kind="user_note", author="per-alex-example")))
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    _ok(_apply(inst, "notes", _clm("clm-trial", CTX, "280 tokens", "The trial context window is 280 tokens", "src-trial")))
    bot = next(cid for cid, claim in _claims(inst, CTX).items() if claim["proposed_by"] == "bot")
    assert _fact(inst, CTX)["status"] == "disputed"
    wider = _resolve(CTX, ["clm-trial"], [bot], "the context window is 1,280 tokens on every plan", "src-alex-1280")
    assert _err(_apply(inst, "alex", wider)) == "op 0 (resolve_fact): the quote holds no kept claim's value"
    assert _fact(inst, CTX)["status"] == "disputed"
    exact = _resolve(CTX, ["clm-trial"], [bot], "the trial context window is 280 tokens per request", "src-alex-280")
    _ok(_apply(inst, "alex", exact))
    assert (_fact(inst, CTX)["status"], _fact(inst, CTX)["value_int"]) == ("current", 280)


_WORDS_SOURCE = (
    "SSO is included on Pro. SSO is enabled on Pro. SSO is available on Pro. SSO is not included. "
    "Included on Pro, not included on Basic. SSO is enabled with no extra cost. The tier is professional now. "
    "Our data sits with us in the EU region. In short the region is EU. SSO: yes, it is. "
    "SSO is not enabled on Basic. Is SSO true on Pro? It is not true. SSO is never included. "
    "Example Cloud tier is not pro. SSO isn't enabled on Basic."
)


def _key_config(tmp_path: Path, block: str) -> Path:
    path = tmp_path / "keys.toml"
    path.write_text(render_config(fact_keys=[{"key": "demo.seats", "value_type": "int"}]) + block, encoding="utf-8")
    return path


def test_value_words_for_enum_and_bool(tmp_path: Path) -> None:
    # load_config's errors, in order.
    cases = [
        ('key = "ex.count"\nvalue_type = "int"\nvalue_words = {true = ["yes"]}',
         "fact_keys[1].value_words is only for enum and bool keys"),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {true = "included"}',
         "fact_keys[1].value_words must map values to lists of 1 to 20 words, each 1 to 80 characters on one line"),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {true = []}',
         "fact_keys[1].value_words must map values to lists of 1 to 20 words, each 1 to 80 characters on one line"),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {true = ["a\\nb"]}',
         "fact_keys[1].value_words must map values to lists of 1 to 20 words, each 1 to 80 characters on one line"),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {maybe = ["perhaps"]}',
         "fact_keys[1].value_words names maybe, which is not a value of this key"),
        ('key = "ex.tier"\nvalue_type = "enum"\nallowed_values = ["basic", "pro"]\nvalue_words = {gold = ["gold"]}',
         "fact_keys[1].value_words names gold, which is not a value of this key"),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {true = ["on"], false = ["On!"]}',
         'fact_keys[1].value_words: "on" is listed for both false and true'),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {true = ["no"]}',
         'fact_keys[1].value_words: "no" is listed for both false and true'),
        ('key = "ex.tier"\nvalue_type = "enum"\nallowed_values = ["basic", "pro"]\nvalue_words = {pro = ["Basic"]}',
         'fact_keys[1].value_words: "Basic" is listed for both basic and pro'),
        ('key = "ex.flag"\nvalue_type = "bool"\nvalue_words = {true = ["!!!"]}',
         'fact_keys[1].value_words: "!!!" has no words'),
    ]
    for block, message in cases:
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_config(_key_config(tmp_path, "\n[[fact_keys]]\n" + block + "\n"))
    loaded = load_config(_key_config(tmp_path, STRICT_KEYS_TOML))
    sso = next(item for item in loaded.fact_keys if item.key == "exbasic.sso")
    assert sso.value_words == {"false": ("not offered", "not included", "not enabled"), "true": ("included", "enabled")}

    inst = _inst(tmp_path / "inst")
    stored = ro_rows(inst, "SELECT value_words FROM fact_keys WHERE key = 'exbasic.sso'")[0][0]
    assert json.loads(stored) == {"false": ["not offered", "not included", "not enabled"],
                                  "true": ["included", "enabled"]}
    code, out, _err_text = _cli("apply-config", "--instance", inst)
    assert (code, json.loads(out)["status"]) == (0, "nothing to apply")
    # A malformed value_words from the reserved agent is refused by _semantic.
    bad = upsert_op("upsert_fact_key", {"key": "exbasic.sso", "value_words": {"true": 3}}, "src-ec-limits",
                    base=_base(inst, "fact_keys", "key", "exbasic.sso"))
    assert _err(_apply(inst, "brain", bad, reserved=True)) == "op 0 (upsert_fact_key): value_words is invalid"

    _ok(_apply(inst, "alex", _src("src-words", _WORDS_SOURCE)))

    def put(key, value, quote, raw):
        return _apply(inst, "bot", _fact_op(key, value, "src-words", quote=quote, raw=raw,
                                             base=_base(inst, "facts", "key", key)))

    included = _ok(put("exbasic.sso", True, "SSO is included on Pro", "included"))
    assert _fact(inst, "exbasic.sso")["value_int"] == 1 and included.exit_code == 0
    _ok(put("exbasic.sso", True, "SSO is included on Pro", "true"))
    assert _err(put("exbasic.sso", True, "SSO is available on Pro", "true")) == (
        "op 0 (upsert_fact): value true is not in the quote")
    assert _err(put("exbasic.sso", True, "SSO is not included", "true")) == (
        "op 0 (upsert_fact): value true is not in the quote")
    _ok(put("exbasic.sso", False, "SSO is not included", "false"))
    both = "Included on Pro, not included on Basic"
    assert _err(put("exbasic.sso", True, both, "true")) == "op 0 (upsert_fact): value true is not in the quote"
    assert _err(put("exbasic.sso", False, both, "false")) == "op 0 (upsert_fact): value false is not in the quote"
    _ok(put("exbasic.sso", True, "SSO is enabled with no extra cost", "true"))
    _ok(put("exbasic.sso", True, "SSO: yes, it is", "true"))
    # A negator right before a value's word or name keeps it from proving that value, and a
    # negated name of the value refuses it. A configured negative form still proves its own value.
    negated = [("exbasic.sso", True, "SSO is not enabled on Basic", "true"),
               ("exbasic.sso", True, "Is SSO true on Pro? It is not true", "true"),
               ("exbasic.sso", True, "SSO is never included", "included"),
               ("exbasic.tier", "pro", "Example Cloud tier is not pro", "pro"),
               ("exbasic.sso", True, "SSO isn't enabled on Basic", "enabled")]
    for key, value, quote, raw in negated:
        assert _err(put(key, value, quote, raw)) == f"op 0 (upsert_fact): value {raw} is not in the quote", quote
    _ok(put("exbasic.sso", False, "SSO is not enabled on Basic", "false"))
    _ok(put("exbasic.sso", True, "SSO is enabled with no extra cost", "true"))
    _ok(put("exbasic.tier", "pro", "The tier is professional now", "professional"))
    assert _fact(inst, "exbasic.tier")["value_text"] == "pro"
    _ok(put("vendor.compliance.instance", "eu", "Our data sits with us in the EU region", "eu"))
    _ok(put("exunits.region", "EU", "In short the region is EU", "EU"))
    # add_claim and verify_claim take value words too.
    _ok(_apply(inst, "notes", _clm("clm-words", "exbasic.sso", "included", "SSO is enabled on Pro", "src-words")))
    assert _claims(inst, "exbasic.sso")["clm-words"]["value_int"] == 1
    _ok(_apply(inst, "importer", {"op": "add_claim", "row": {"id": "clm-legacy-sso", "key": "exbasic.sso",
                                                             "value_raw": "included", "basis": "reported"},
                                  "source": {"source_id": "src-words", "locator": "para 9"}}))
    verified = _ok(_apply(inst, "checker", {"op": "verify_claim",
                                            "row": {"claim_id": "clm-legacy-sso", "quote": "SSO is enabled on Pro"},
                                            "source": {"source_id": "src-words", "locator": "para 9"}}))
    assert {"table": "claims", "id": "clm-legacy-sso", "action": "update"} in verified.receipt["targets"]


def test_fact_dates_from_iso_or_day_words(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-launch", "We launch on 2026-10-09. The launch is today.", date="2026-10-09"),
               _src("src-launch-day", "The launch day is Friday this week.", date="2026-10-09")))
    key = "exunits.launch"

    def put(value, quote, raw, source="src-launch", agent="bot"):
        return _apply(inst, agent, _fact_op(key, value, source, quote=quote, raw=raw,
                                            base=_base(inst, "facts", "key", key)))

    _ok(put("2026-10-09", "We launch on 2026-10-09", "2026-10-09"))
    _ok(put("2026-10-09", "The launch is today", "today", agent="solo"))
    assert _fact(inst, key)["value_date"] == "2026-10-09"
    assert any(claim["value_raw"] == "today" and claim["checked"] == 1 for claim in _claims(inst, key).values())
    _ok(put("2026-10-09", "The launch is today", "2026-10-09"))
    friday = put("2026-10-09", "The launch day is Friday this week", "Friday", source="src-launch-day")
    assert _err(friday) == "op 0 (upsert_fact): value Friday is not an ISO date"
    # The same cases through add_claim.
    _ok(_apply(inst, "notes", _clm("clm-d1", key, "2026-10-09", "We launch on 2026-10-09", "src-launch")))
    _ok(_apply(inst, "notes", _clm("clm-d2", key, "today", "The launch is today", "src-launch", locator="para 2")))
    assert _claims(inst, key)["clm-d2"]["value_date"] == "2026-10-09"
    _ok(_apply(inst, "notes", _clm("clm-d3", key, "2026-10-09", "The launch is today", "src-launch", locator="para 3")))
    refused = _apply(inst, "notes", _clm("clm-d4", key, "Friday", "The launch day is Friday this week", "src-launch-day"))
    assert _err(refused) == "op 0 (add_claim): value Friday is not an ISO date"


def test_strict_write_upgrades_an_unchecked_own_claim(tmp_path: Path) -> None:
    from brain import migrate
    from tests.test_migrate import V4_FACTS, _v4_config, v4_store

    inst = v4_store(tmp_path / "v4", _v4_config())
    assert migrate.migrate(inst / "brain.sqlite3") == (4, migrate.latest_version())
    migrated = next(cid for cid, claim in _claims(inst, "demo.seats").items() if cid.startswith("clm-m5-"))
    assert _claims(inst, "demo.seats")[migrated]["checked"] == 0
    _ok(_apply(inst, "alex", _src("src-seats", "We have 12 seats today.")))
    first = _ok(_apply(inst, "bot", _fact_op("demo.seats", 12, "src-seats", quote="We have 12 seats today", raw="12",
                                             base=_base(inst, "facts", "key", "demo.seats"))))
    claims = _claims(inst, "demo.seats")
    fresh = next(cid for cid, claim in claims.items() if claim["checked"] == 1)
    assert (claims[migrated]["status"], claims[migrated]["superseded_by"]) == ("superseded", fresh)
    assert claims[fresh]["proposed_by"] == "bot" and claims[fresh]["status"] == "live"
    assert {"table": "claims", "id": fresh, "action": "insert"} in first.receipt["targets"]
    again = _ok(_apply(inst, "bot", _fact_op("demo.seats", 12, "src-seats", quote="We have 12 seats today", raw="12",
                                             base=_base(inst, "facts", "key", "demo.seats"),
                                             verified="2026-10-02T00:00:00Z")))
    assert len(_claims(inst, "demo.seats")) == 2
    assert not any(item["table"] == "claims" for item in again.receipt["targets"])
    # An unchecked claim inserted on a context connection, as 0.7.0a1's upsert_fact wrote them.
    (tmp_path / "a1").mkdir()
    other = Path(init_instance("demo", tmp_path / "a1", config_text=strict_config())["instance"])
    _ok(_apply(other, "alex", _src("src-ec-limits", LIMITS, title="Example Cloud limits")))
    _old_claim(other, "clm-a1old", CTX, "value_int", 128000, "128000", "src-ec-limits", "bot")
    _ok(_apply(other, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K",
                                      base=_base(other, "facts", "key", CTX))))
    claims = _claims(other, CTX)
    assert claims["clm-a1old"]["status"] == "superseded"
    assert [claim["checked"] for claim in claims.values() if claim["status"] == "live"] == [1]
    _ok(_apply(other, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K",
                                      base=_base(other, "facts", "key", CTX), verified="2026-10-03T00:00:00Z")))
    assert len(_claims(other, CTX)) == 2
    # A migrated version value no scope can match is upgraded unchanged, and a new one is refused.
    facts = tuple(item for item in V4_FACTS if item[0] != "excloud.current_version") + (
        ("excloud.current_version", "value_text", "1.3 beta", "current", "notes:notes-seed-3"),
    )
    beta = v4_store(tmp_path / "beta", _v4_config(), facts=facts)
    assert migrate.migrate(beta / "brain.sqlite3") == (4, migrate.latest_version())
    _ok(_apply(beta, "alex", _src("src-beta", "The current version is 1.3 beta. The next one is 1.4 beta.")))
    upgraded = _ok(_apply(beta, "notes", _fact_op("excloud.current_version", "1.3 beta", "src-beta", basis="reported",
                                                  quote="The current version is 1.3 beta", raw="1.3 beta",
                                                  base=_base(beta, "facts", "key", "excloud.current_version"))))
    assert any(item["action"] == "insert" and item["table"] == "claims" for item in upgraded.receipt["targets"])
    versions = _claims(beta, "excloud.current_version")
    assert [claim["checked"] for claim in versions.values() if claim["status"] == "live"] == [1]
    refused = _apply(beta, "notes", _fact_op("excloud.current_version", "1.4 beta", "src-beta", basis="reported",
                                             quote="The next one is 1.4 beta", raw="1.4 beta",
                                             base=_base(beta, "facts", "key", "excloud.current_version")))
    assert _err(refused) == "op 0 (upsert_fact): a current_version value must be a scope label"


def test_collector_fact_quotes_are_the_canonical_entry(tmp_path: Path) -> None:
    from tests.collector_fixtures import collect, collector_instance, github_doc, vanta_doc

    inst = collector_instance(tmp_path, "repo", "compliance")
    code, payload, err = collect(inst, "repo", github_doc())
    assert code == 0, (payload, err)
    ci = next(iter(_claims(inst, "eng.repo.ci_state").values()))
    assert (ci["quote"], ci["value_raw"], ci["checked"]) == ('{"fact":"ci_state","value":"success"}', "success", 1)
    code, payload, err = collect(inst, "compliance", vanta_doc())
    assert code == 0, (payload, err)
    failing = next(iter(_claims(inst, "vendor.vanta.failing_tests").values()))
    assert (failing["quote"], failing["value_raw"], failing["checked"]) == (
        '{"fact":"failing_tests","value":1}', "1", 1)
    batch = ro_rows(inst, "SELECT content FROM sources WHERE id = ?", (failing["source_id"],))[0][0]
    entry = next(item for item in json.loads(batch)["items"] if item["fact"] == "failing_tests")
    assert list(entry) == ["fact", "value", "value_names"]
    assert entry["value_names"] == ["MFA on for every account"]
    assert '"value":1,"value_names":["MFA on for every account"]' in batch
    two = vanta_doc(failing_tests=[
        {"id": "tst-0001", "name": "MFA on for every account", "status": "NEEDS_ATTENTION", "due": "2026-10-20",
         "link": "https://app.vanta.example/tests/tst-0001", "controls": ["AC-1"]},
        {"id": "tst-0002", "name": "Backups tested", "status": "NEEDS_ATTENTION", "due": "2026-10-21",
         "link": "https://app.vanta.example/tests/tst-0002", "controls": ["AC-1"]},
    ], fetched_at="2026-10-08T01:00:00Z")
    code, payload, err = collect(inst, "compliance", two)
    assert code == 0, (payload, err)
    live = [claim for claim in _claims(inst, "vendor.vanta.failing_tests").values() if claim["status"] == "live"]
    assert [(claim["quote"], claim["checked"]) for claim in live] == [('{"fact":"failing_tests","value":2}', 1)]
    # A re-run with unchanged values sends re-verify rows: last_verified_at moves, the source stays the claim's.
    before = _fact(inst, "eng.repo.ci_state")
    code, payload, err = collect(inst, "repo", github_doc(fetched_at="2026-10-08T02:00:00Z"))
    assert code == 0, (payload, err)
    after = _fact(inst, "eng.repo.ci_state")
    assert after["last_verified_at"] == "2026-10-08T02:00:00Z"
    assert (after["source_id"], after["quote"], after["value_text"]) == (
        before["source_id"], before["quote"], before["value_text"])
    assert len(_claims(inst, "eng.repo.ci_state")) == 1
    assert after["source_id"] == ci["source_id"]


def test_collector_resends_a_value_its_claim_does_not_check(tmp_path: Path) -> None:
    from tests.collector_fixtures import collect, collector_instance, github_doc

    inst = collector_instance(tmp_path, "repo")
    writer = Applier(inst)
    try:
        _ok(writer.apply(json.dumps(make_packet("tester", _src("src-old-run", "Old run notes.", kind="api"))).encode()))
    finally:
        writer.close()
    _old_claim(inst, "clm-old-ci", "eng.repo.ci_state", "value_text", "success", "success", "src-old-run", "repo")
    code, payload, err = collect(inst, "repo", github_doc())
    assert code == 0, (payload, err)
    claims = _claims(inst, "eng.repo.ci_state")
    assert claims["clm-old-ci"]["status"] == "superseded"
    fresh = next(claim for claim in claims.values() if claim["status"] == "live")
    assert (fresh["checked"], fresh["proposed_by"]) == (1, "repo")
    assert _fact(inst, "eng.repo.ci_state")["quote"] == '{"fact":"ci_state","value":"success"}'
    code, payload, err = collect(inst, "repo", github_doc(fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0, (payload, err)
    assert len(_claims(inst, "eng.repo.ci_state")) == 2
    # Another writer's checked claim heads the fact; the collector still re-sends until its own is checked.
    other = collector_instance(tmp_path / "other", "repo")
    writer = Applier(other)
    try:
        _ok(writer.apply(json.dumps(make_packet(
            "tester",
            _src("src-old-run", "Old run notes.", kind="api"),
            _src("src-ci-doc", "Release notes. CI state is success on main."),
        )).encode()))
        _ok(writer.apply(json.dumps(make_packet("tester", _fact_op(
            "eng.repo.ci_state", "success", "src-ci-doc", quote="CI state is success on main", raw="success",
        ))).encode()))
    finally:
        writer.close()
    _old_claim(other, "clm-old-ci", "eng.repo.ci_state", "value_text", "success", "success", "src-old-run", "repo",
               fact=False)
    assert _fact(other, "eng.repo.ci_state")["source_id"] == "src-ci-doc"
    code, payload, err = collect(other, "repo", github_doc())
    assert code == 0, (payload, err)
    claims = _claims(other, "eng.repo.ci_state")
    assert claims["clm-old-ci"]["status"] == "superseded"
    assert sorted((claim["proposed_by"], claim["checked"]) for claim in claims.values() if claim["status"] == "live") == [
        ("repo", 1), ("tester", 1)]
    count = len(claims)
    code, payload, err = collect(other, "repo", github_doc(fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0, (payload, err)
    assert len(_claims(other, "eng.repo.ci_state")) == count


# -- state dates ---------------------------------------------------------------------------------

DATES = "Plan for the week: the review is due 2026-10-12, send the report tomorrow, and the team agreed today."
FRIDAY = "Plan for next week: book the meeting room by Friday at the latest."


def _action_op(aid: str, source: str, *, base=None, **fields) -> dict:
    row = {"id": aid}
    if base is None:
        row.update({"title": f"Action {aid}", "status": "open", "status_basis": "reported", "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z"})
    row.update(fields)
    return upsert_op("upsert_action", row, source, base=base)


def test_state_dates_follow_the_candidate_rules(tmp_path: Path, monkeypatch) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-dates", DATES, date="2026-10-01"), _src("src-friday", FRIDAY, date="2026-10-05")))

    def due(aid, date, source="src-dates", agent="notes"):
        return _apply(inst, agent, _action_op(aid, source, due_date=date, due_kind="deadline"))

    _ok(due("act-same", "2026-10-01"))
    _ok(due("act-iso", "2026-10-12"))
    _ok(due("act-tomorrow", "2026-10-02"))
    _ok(due("act-friday", "2026-10-09", source="src-friday"))
    assert _err(_apply(inst, "notes", _action_op("act-asked", "src-dates", asked_on="2026-10-05"))) == (
        "op 0 (upsert_action): asked_on 2026-10-05 is after the source date 2026-10-01")
    assert _err(due("act-unknown", "2026-10-20")) == (
        "op 0 (upsert_action): due_date 2026-10-20 is neither the source date nor in the source")
    # An update that re-sends its stored date unchanged isn't checked, against any source.
    _ok(_apply(inst, "notes", _action_op("act-iso", "src-friday", base=1, due_date="2026-10-12", title="Renamed")))
    # A legacy insert is checked like any other.
    assert _err(due("act-legacy", "2026-10-20", agent="importer")) == (
        "op 0 (upsert_action): due_date 2026-10-20 is neither the source date nor in the source")
    _ok(due("act-legacy", "2026-10-12", agent="importer"))
    decision = {"id": "dec-dates", "title": "Agree the plan", "quote": "the team agreed today",
                "decided_by_person_id": "per-sam-example", "status": "in_effect", "project_id": "general"}
    late = _apply(inst, "notes", upsert_op("upsert_decision", {**decision, "decided_on": "2026-10-03",
                                                                "quote": "send the report tomorrow, and the team agreed today"},
                                           "src-dates"))
    assert _err(late) == "op 0 (upsert_decision): decided_on 2026-10-03 is after the source date 2026-10-01"
    unknown = _apply(inst, "notes", upsert_op("upsert_decision", {**decision, "decided_on": "2026-09-20",
                                                                   "quote": "send the report tomorrow, and the team agreed today"},
                                              "src-dates"))
    assert _err(unknown) == (
        "op 0 (upsert_decision): decided_on 2026-09-20 is neither the source date nor in the source")
    # A candidate-derived action isn't checked again: its planner resolved the date by the same rules.
    labels: list[str] = []
    original = strict.date_error

    def recorder(label, name, row, current, source):
        labels.append(label)
        return original(label, name, row, current, source)

    monkeypatch.setattr(strict, "date_error", recorder)
    doc = _action_doc("Book the meeting room", due="friday", quote="book the meeting room by Friday at the latest",
                      source="src-friday")
    code, payload, err = _extract(inst, doc)
    assert (code, err) == (0, ""), payload
    stored = ro_rows(inst, "SELECT due_date FROM actions WHERE title = 'Book the meeting room'")
    assert stored == [("2026-10-09",)]
    assert labels == []
    _ok(due("act-after", "2026-10-12"))
    assert labels == ["0"]


# -- notes ---------------------------------------------------------------------------------------


def _record_op(rid: str, body: str, source: str = "src-ec-limits", *, base=None, title: str = "A note") -> dict:
    row = {"id": rid, "body": body}
    if base is None:
        row.update({"kind": "note", "title": title})
    elif title:
        row["title"] = title
    return upsert_op("upsert_record", row, source, base=base)


def test_note_cap(tmp_path: Path) -> None:
    inst = _inst(tmp_path / "one")
    long_detail = "Plan the venue walk-through carefully. " * 13 + "x" * 10  # 517 characters
    assert len(long_detail) > 500
    assert _err(_apply(inst, "notes", _action_op("act-long", "src-ec-limits", detail=long_detail))) == (
        "op 0 (upsert_action): detail is a note and is at most 500 characters")
    _ok(_apply(inst, "notes", _action_op("act-fits", "src-ec-limits", detail="x" * 500)))
    wider = _inst(tmp_path / "wider", strict_config(writes="\n[writes]\nnote_max_chars = 1200\n"))
    assert _err(_apply(wider, "notes", _action_op("act-long", "src-ec-limits", detail="y" * 1201))) == (
        "op 0 (upsert_action): detail is a note and is at most 1200 characters")
    _ok(_apply(wider, "notes", _action_op("act-long", "src-ec-limits", detail="y" * 1200)))
    # A legacy insert is exempt; an update that doesn't send the note, or re-sends it unchanged, applies.
    body = "z" * 1500
    _ok(_apply(inst, "importer", _record_op("rec-long", body)))
    _ok(_apply(inst, "notes", _record_op("rec-long", body, base=1, title="Renamed note")))
    renamed = upsert_op("upsert_record", {"id": "rec-long", "title": "Renamed again"}, "src-ec-limits", base=2)
    _ok(_apply(inst, "notes", renamed))
    assert _err(_apply(inst, "notes", _record_op("rec-long", body + "!", base=3, title=""))) == (
        "op 0 (upsert_record): body is a note and is at most 500 characters")
    assert _err(_apply(inst, "notes", _record_op("rec-new", "w" * 501))) == (
        "op 0 (upsert_record): body is a note and is at most 500 characters")
    # brain apply-config writes a 900-character workstream description (the reserved agent is exempt).
    workstreams = [{"id": "general", "title": "General", "description": "d" * 900, "sort_order": 100},
                   {"id": "ops", "title": "Ops", "description": "Operations.", "sort_order": 200}]
    config = render_config(principal=PRINCIPAL, agents=STRICT_AGENTS, fact_keys=KEYS, workstreams=workstreams)
    config = re.sub(r'(\[agents\.scan\]\n(?:[^\[\n].*\n)*?)approval = "none"\n', r"\1", config)
    (tmp_path / "ws").mkdir()
    described = Path(init_instance("demo", tmp_path / "ws", config_text=config + STRICT_KEYS_TOML + TRUTH_BLOCK)["instance"])
    assert ro_rows(described, "SELECT length(description) FROM projects WHERE id = 'general'") == [(900,)]
    # Candidates see the cap first: the planners report it, and the verdict holds them.
    code, payload, err = _extract(inst, _action_doc("Plan the walk-through", detail=long_detail))
    assert code == 4
    assert err == ("brain: op 0 (apply_candidate): candidate is invalid: /fields/detail detail is a note and is at "
                   "most 500 characters\n")
    code, payload, err = _extract(inst, _decision_doc("Walk the venue", detail=long_detail))
    reason = ("op 0 (apply_candidate): candidate is invalid: /fields/detail detail is a note and is at most 500 "
              "characters")
    assert (code, err) == (4, f"brain: {reason}\n")
    assert _log(inst, payload["change_id"])["hold_reason"] == reason
    code, out, _stderr = _cli("held", "--instance", inst)
    item = next(entry for entry in json.loads(out)["held"] if entry["change_id"] == payload["change_id"])
    assert item["preview"]["errors"] == [
        {"path": "/fields/detail", "message": "detail is a note and is at most 500 characters"}]
    off = _inst(tmp_path / "off", strict_config(writes="\n[writes]\nnotes = false\n"))
    code, payload, err = _extract(off, _action_doc("Chase the venue", next_step="Call Sam about the venue."))
    assert (code, err) == (4, "brain: op 0 (apply_candidate): candidate is invalid: /fields/next_step notes are off "
                              "in this instance: next_step\n")


def test_writes_config_errors(tmp_path: Path) -> None:
    base = render_config()
    assert load_config(_key_config(tmp_path, "")).writes == config_mod.Writes(True, 500)
    cases = [
        ("writes = 3\n", "writes must be a table"),
        ("[writes]\nextra = 1\n", "unknown key writes.extra"),
        ('[writes]\nnotes = "yes"\n', "writes.notes must be true or false"),
        ("[writes]\nnote_max_chars = 99\n", "writes.note_max_chars must be a whole number from 100 to 2000"),
        ("[writes]\nnote_max_chars = 2001\n", "writes.note_max_chars must be a whole number from 100 to 2000"),
        ("[writes]\nnote_max_chars = 1.5\n", "writes.note_max_chars must be a whole number from 100 to 2000"),
        ("[writes]\nnote_max_chars = true\n", "writes.note_max_chars must be a whole number from 100 to 2000"),
    ]
    for block, message in cases:
        path = tmp_path / "writes.toml"
        text = base.replace("[instance]", block + "\n[instance]", 1) if block.startswith("writes =") else base + "\n" + block
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match=re.escape(message)):
            load_config(path)
    path = tmp_path / "writes.toml"
    path.write_text(base + "\n[writes]\nnotes = false\nnote_max_chars = 2000\n", encoding="utf-8")
    assert load_config(path).writes == config_mod.Writes(False, 2000)


def test_notes_off(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "notes", _action_op("act-noted", "src-ec-limits", detail="Ask Sam first.", next_step="Chase Sam.")))
    _ok(_apply(inst, "notes", _record_op("rec-noted", "A stored note body.")))
    config = (inst / "brain.toml").read_text(encoding="utf-8")
    (inst / "brain.toml").write_text(config.replace(TRUTH_BLOCK, "\n[writes]\nnotes = false\n" + TRUTH_BLOCK),
                                     encoding="utf-8")
    assert load_config(inst / "brain.toml").writes.notes is False
    cases = [
        ("upsert_action", "detail", _action_op("act-x1", "src-ec-limits", detail="x")),
        ("upsert_action", "next_step", _action_op("act-x2", "src-ec-limits", next_step="x")),
        ("upsert_action", "close_reason", _action_op("act-x3", "src-ec-limits", close_reason="x")),
        ("upsert_checklist_item", "detail", upsert_op("upsert_checklist_item", {
            "id": "chk-x", "project_id": "general", "title": "Item", "state": "not_started", "state_basis": "reported",
            "last_verified_at": "2026-10-01T00:00:00Z", "detail": "x"}, "src-ec-limits")),
        ("upsert_project", "description", upsert_op("upsert_project", {
            "id": "proj-x", "kind": "project", "title": "Project", "description": "x"}, "src-ec-limits")),
        ("upsert_project", "goal", upsert_op("upsert_project", {
            "id": "proj-y", "kind": "project", "title": "Project", "goal": "x"}, "src-ec-limits")),
        ("upsert_decision", "detail", upsert_op("upsert_decision", {
            "id": "dec-x", "title": "Decide", "detail": "x", "quote": "VRAM is 80 GB per node",
            "decided_on": "2026-09-01", "decided_by_person_id": "per-sam-example", "status": "in_effect",
            "project_id": "general"}, "src-ec-limits")),
        ("upsert_record", "body", _record_op("rec-x", "x")),
    ]
    covered = []
    for name, field, op in cases:
        covered.append((name, field))
        for agent in ("notes", "importer"):
            assert _err(_apply(inst, agent, op)) == f"op 0 ({name}): notes are off in this instance: {field}"
    assert covered == [(name, field) for name, fields in schemas.NOTE_FIELDS.items() for field in fields] or sorted(
        covered) == sorted((name, field) for name, fields in schemas.NOTE_FIELDS.items() for field in fields)
    # Clearing a note and re-sending a stored one still apply.
    _ok(_apply(inst, "notes", _action_op("act-noted", "src-ec-limits", base=1, detail="", next_step="Chase Sam.")))
    assert ro_rows(inst, "SELECT field FROM note_meta WHERE ref_id = 'act-noted'") == [("next_step",)]
    _ok(_apply(inst, "notes", _record_op("rec-noted", "A stored note body.", base=1, title="Kept")))
    shown = _read(inst, "get", entity="action", id="act-noted").structured
    assert shown["notes"]["label"] == NOTE_LABEL
    assert shown["notes"]["fields"]["next_step"]["text"] == "Chase Sam."
    # The switch changes the rendered router, so check says so until brain router runs.
    code, out, err = _cli("check", "--instance", inst)
    assert (code, json.loads(out)) == (1, {"ok": False, "problems": [
        "ROUTER.md differs from the engine router; run brain router"]})
    code, out, err = _cli("router", "--instance", inst)
    assert (code, json.loads(out)["status"]) == (0, "updated")
    code, out, err = _cli("check", "--instance", inst)
    assert (code, json.loads(out)) == (0, {"ok": True, "problems": []})
    # The action, the record and the two workstreams' descriptions.
    assert "brain: notes are off, and 4 rows hold note text" in err.splitlines()


def test_note_meta_records_who_and_when(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    clock.at("2026-10-09T01:00:00Z")
    created = _ok(_apply(inst, "notes", _action_op("act-meta", "src-ec-limits", detail="Ask Sam.", next_step="Chase.")))
    meta = lambda: ro_rows(inst, "SELECT entity, ref_id, field, written_by, written_at FROM note_meta "  # noqa: E731
                                 "WHERE ref_id = 'act-meta' ORDER BY field")
    assert meta() == [("action", "act-meta", "detail", "notes", "2026-10-09T01:00:00Z"),
                      ("action", "act-meta", "next_step", "notes", "2026-10-09T01:00:00Z")]
    assert not any(item["table"] == "note_meta" for item in created.receipt["targets"])
    clock.at("2026-10-09T02:00:00Z")
    updated = _ok(_apply(inst, "bot", _action_op("act-meta", "src-ec-limits", base=1, detail="Ask Sam again.",
                                                 next_step="Chase.")))
    assert meta() == [("action", "act-meta", "detail", "bot", "2026-10-09T02:00:00Z"),
                      ("action", "act-meta", "next_step", "notes", "2026-10-09T01:00:00Z")]
    assert not any(item["table"] == "note_meta" for item in updated.receipt["targets"])
    clock.at("2026-10-09T03:00:00Z")
    _ok(_apply(inst, "bot", _action_op("act-meta", "src-ec-limits", base=2, next_step="")))
    assert meta() == [("action", "act-meta", "detail", "bot", "2026-10-09T02:00:00Z")]
    # An approved held packet records the packet's agent, not the approver.
    code, payload, err = _extract(inst, _decision_doc("Agree the window", detail="Sam signs it off."))
    assert code == 4
    writer = Applier(inst)
    try:
        approved = writer.approve(payload["change_id"], "alex")
    finally:
        writer.close()
    assert approved.receipt["status"] == "applied", approved.receipt
    decision_id = next(item["id"] for item in approved.receipt["targets"] if item["table"] == "decisions")
    assert ro_rows(inst, "SELECT written_by, written_at FROM note_meta WHERE ref_id = ?", (decision_id,)) == [
        ("scan", "2026-10-09T03:00:00Z")]
    # The reserved agent's workstream descriptions and a legacy insert write note_meta too.
    assert ro_rows(inst, "SELECT entity, field, written_by FROM note_meta WHERE ref_id = 'general'") == [
        ("project", "description", "brain")]
    _ok(_apply(inst, "importer", _record_op("rec-imported", "Imported body.")))
    assert ro_rows(inst, "SELECT entity, field, written_by FROM note_meta WHERE ref_id = 'rec-imported'") == [
        ("record", "body", "importer")]


def test_get_returns_notes_last(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    clock.at("2026-10-09T03:00:00Z")
    _ok(_apply(inst, "notes", _action_op("act-two", "src-ec-limits", detail="Ask Sam first.",
                                         next_step="Chase Sam on the pricing sheet.")))
    got = _read(inst, "get", entity="action", id="act-two").structured
    assert list(got)[-1] == "notes"
    assert got["notes"] == {
        "label": NOTE_LABEL,
        "fields": {
            "detail": {"text": "Ask Sam first.", "by": "notes", "at": "2026-10-09T03:00:00Z"},
            "next_step": {"text": "Chase Sam on the pricing sheet.", "by": "notes", "at": "2026-10-09T03:00:00Z"},
        },
    }
    assert not {"detail", "next_step", "close_reason"} & set(got["row"])
    # A row written before 0.7.0a2 has no note_meta row.
    con = context_connection(inst)
    try:
        con.execute("DELETE FROM note_meta WHERE ref_id = 'act-two' AND field = 'detail'")
        con.commit()
    finally:
        con.close()
    old = _read(inst, "get", entity="action", id="act-two").structured["notes"]["fields"]["detail"]
    assert old == {"text": "Ask Sam first.", "by": None, "at": None}
    # A record's body pages through notes.
    body = "".join(f"line {index:05d}. " for index in range(2000))
    _ok(_apply(inst, "importer", _record_op("rec-pages", body)))
    record = _read(inst, "get", entity="record", id="rec-pages").structured
    assert "body" not in record["row"]
    assert record["notes"]["fields"]["body"]["text"] == body[:8000]
    assert record["pages"]["body"] == {"offset": 0, "returned": 8000, "total": len(body), "next_offset": 8000}
    page = _read(inst, "get", entity="record", id="rec-pages", field="body", offset=8000).structured
    assert list(page) == ["entity", "id", "field", "text", "offset", "returned", "total", "next_offset", "notes_label"]
    assert page["text"] == body[8000:16000] and page["notes_label"] == NOTE_LABEL
    assert {name: page[name] for name in ("entity", "id", "field", "offset", "returned", "total", "next_offset")} == {
        "entity": "record", "id": "rec-pages", "field": "body", "offset": 8000, "returned": 8000,
        "total": len(body), "next_offset": 16000}
    content = _read(inst, "get", entity="source", id="src-ec-limits", field="content").structured
    assert list(content) == ["entity", "id", "field", "text", "offset", "returned", "total", "next_offset"]
    # With an unchecked marker, the last two keys are unchecked and notes.
    _ok(_apply(inst, "importer", _action_op("act-marked", "src-ec-limits", detail="Alex said the venue is fine.")))
    marked = _read(inst, "get", entity="action", id="act-marked").structured
    assert list(marked)[-2:] == ["unchecked", "notes"]
    # A row with every note empty has no notes.
    _ok(_apply(inst, "notes", _action_op("act-bare", "src-ec-limits", detail="")))
    assert "notes" not in _read(inst, "get", entity="action", id="act-bare").structured


def test_records_query_notes_label(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "notes", _action_op("act-q", "src-ec-limits", detail="Ask Sam about it.")))
    labelled = _read(inst, "records_query", entity="action", fields=["id", "detail"]).structured
    assert list(labelled)[-1] == "notes_label" and labelled["notes_label"] == NOTE_LABEL
    assert list(labelled).count("notes_label") == 1
    assert labelled["rows"] == [{"id": "act-q", "detail": "Ask Sam about it."}]
    plain = _read(inst, "records_query", entity="action").structured
    assert "notes_label" not in plain
    filtered = _read(inst, "records_query", entity="action", filters={"detail": {"contains": "Sam"}}).structured
    assert "notes_label" not in filtered and [row["id"] for row in filtered["rows"]] == ["act-q"]
    project = _read(inst, "records_query", entity="project", fields=["id", "goal"]).structured
    assert project["notes_label"] == NOTE_LABEL


def test_search_match_note(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-tanks", "Aquarium notes: we keep zebrafish in the aquarium room.")))
    _ok(_apply(inst, "notes",
               _action_op("act-note-only", "src-tanks", title="Book the venue",
                          next_step="Chase the zebrafish supplier."),
               _action_op("act-titled", "src-tanks", title="Zebrafish tank cleaning"),
               upsert_op("upsert_decision", {"id": "dec-tank", "title": "Keep the fish",
                                             "quote": "we keep zebrafish in the aquarium room",
                                             "decided_on": "2026-09-01", "decided_by_person_id": "per-sam-example",
                                             "status": "in_effect", "project_id": "general"}, "src-tanks"),
               upsert_op("upsert_checklist_item", {"id": "chk-tank", "project_id": "general", "section": "Zebrafish",
                                                   "title": "Clean the filter", "state": "not_started",
                                                   "state_basis": "reported",
                                                   "last_verified_at": "2026-10-01T00:00:00Z"}, "src-tanks")))
    hits = {hit["id"]: hit for hit in _read(inst, "search", query="zebrafish", limit=25).structured["hits"]}
    assert list(hits["act-note-only"]) == ["entity", "id", "title", "status", "date", "snippet", "score", "match"]
    assert hits["act-note-only"]["match"] == "note"
    for plain in ("act-titled", "dec-tank", "chk-tank", "src-tanks"):
        assert "match" not in hits[plain], plain
    prefixed = {hit["id"]: hit for hit in _read(inst, "search", query="zebra*", limit=25).structured["hits"]}
    assert prefixed["act-note-only"]["match"] == "note"
    assert "match" not in prefixed["act-titled"] and "match" not in prefixed["chk-tank"]
    # Two terms: the title holds one, the note the other, so the title alone doesn't match.
    split = {hit["id"]: hit for hit in _read(inst, "search", query="venue zebrafish").structured["hits"]}
    assert split["act-note-only"]["match"] == "note"


def test_notes_never_answer_facts(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    before = (_read(inst, "fact_get", key=CTX).text, _read(inst, "get", entity="fact", id=CTX).text,
              [line for line in _brief(inst) if "max_context" in line or line.startswith("Facts:")])
    _ok(_apply(inst, "notes", _action_op("act-claims", "src-ec-limits", detail="The context window is 512K tokens.")))
    after = (_read(inst, "fact_get", key=CTX).text, _read(inst, "get", entity="fact", id=CTX).text,
             [line for line in _brief(inst) if "max_context" in line or line.startswith("Facts:")])
    assert after == before
    assert "512" not in after[0] + after[1] + "".join(after[2])
    assert "512" not in "\n".join(_brief(inst))


def test_check_reports_note_meta_problems(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "notes", _action_op("act-empty", "src-ec-limits", detail="Soon cleared.")))
    con = context_connection(inst)
    try:
        con.execute("BEGIN")
        con.execute("INSERT INTO note_meta VALUES ('action', 'act-gone', 'detail', 'notes', '2026-10-01T00:00:00Z')")
        con.execute("UPDATE actions SET detail = '' WHERE id = 'act-empty'")  # as an 0.7.0a1 writer clears a note
        con.execute("COMMIT")
    finally:
        con.close()
    code, out, err = _cli("check", "--instance", inst)
    assert (code, json.loads(out)) == (0, {"ok": True, "problems": []})
    lines = [line for line in err.splitlines() if line.startswith("brain: note_meta")]
    assert lines == [
        "brain: note_meta action act-empty detail: the note is empty",
        "brain: note_meta action act-gone detail: row does not exist",
    ]
    _ok(_apply(inst, "notes", _action_op("act-after", "src-ec-limits")))
    assert not (inst / "PAUSED").exists()


# -- candidates and verified approval ------------------------------------------------------------

KEY_WORDS = "Example Cloud context window"
V1_CLAIM = {
    "candidate_version": 1,
    "kind": "claim_candidate",
    "source_id": "src-ec-plan",
    "locator": "para 2",
    "quote": GOOD_QUOTE,
    "fields": {"product": "widget", "area": "pricing", "topic": "seat-price", "value": "128K", "status": "reported"},
}


def _passing(locator: str = "para 1", **extra) -> dict:
    return _claim_doc(KEY_WORDS, "128K", GOOD_QUOTE, locator=locator, **extra)


def test_candidate_versions(tmp_path: Path) -> None:
    from brain.validate import validate_candidate

    errors = validate_candidate(V1_CLAIM)
    assert errors[0] == {"path": "/candidate_version", "message": "must be 2"}
    assert {"path": "/fields/key", "message": "is required"} in errors
    claim_fields = schemas.CANDIDATE_KINDS["claim_candidate"]["properties"]
    assert {"path": "/fields/product", "message": advice.unknown_property_error("product", claim_fields)} in errors
    assert len(errors) > 2
    decision = _decision_doc("Agree the plan")
    decision["candidate_version"] = 2
    assert validate_candidate(decision) == [{"path": "/candidate_version", "message": "must be 1"}]
    unknown = {**_passing(), "kind": "meeting_candidate"}
    assert validate_candidate(unknown) == [{"path": "/kind", "message": "unknown candidate kind meeting_candidate"}]
    path = tmp_path / "claim.json"
    path.write_text(json.dumps(_passing()), encoding="utf-8")
    code, out, err = _cli("validate", "candidate", path)
    assert (code, json.loads(out), err) == (0, {"errors": [], "kind": "candidate", "status": "valid"}, "")
    # A whitespace-only locator is refused at the candidate envelope, as CLAIM_ENVELOPE refuses one
    #, and never reaches a table's CHECK: brain extract writes nothing, and a direct packet
    # is rejected by validation.
    pattern = {"path": "/locator", "message": "does not match the required pattern"}
    for doc in (_passing(locator=" "), _action_doc("Price the Pro plan"), _decision_doc("Keep the Pro plan")):
        doc["locator"] = "  "
        assert validate_candidate(doc) == [pattern], doc["kind"]
    inst = _inst(tmp_path / "blank")
    before = ro_rows(inst, "SELECT count(*) FROM write_log")
    code, payload, err = _extract(inst, _passing(locator=" "))
    assert (code, payload, err) == (1, {"errors": [pattern], "kind": "candidate", "status": "held"}, "")
    assert ro_rows(inst, "SELECT count(*) FROM write_log") == before
    direct = _apply(inst, "scan", {"op": "apply_candidate", "candidate": _passing(locator="\t")})
    assert _err(direct) == "packet is invalid: /ops/0/candidate/locator does not match the required pattern"
    assert _counts(inst) == (0, 0, 0)


def test_extraction_table_is_refused(tmp_path: Path) -> None:
    message = ("extraction is replaced by [truth] in 0.7.0: remove [extraction], then register fact keys under "
               "[[fact_keys]] and their aliases under [truth]")
    inst = _inst(tmp_path)
    text = (inst / "brain.toml").read_text(encoding="utf-8")
    (inst / "brain.toml").write_text(text + '\n[extraction]\nproducts = ["widget"]\nclaim_areas = ["pricing"]\n',
                                     encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config(inst / "brain.toml")
    assert str(caught.value) == message
    code, out, _err_text = _cli("check", "--instance", inst)
    assert code == 1
    assert f"brain.toml: {message}" in json.loads(out)["problems"]
    path, _change = _packet_file(inst, "notes", _record_op("rec-any", "Anything."))
    code, out, err = _cli("apply", "--instance", inst, path)
    assert (code, out, err) == (5, "", f"brain: {message}\n")


def test_c1_to_c6_each_failing_alone(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    cases = [
        (_claim_doc(KEY_WORDS, "128K", "The context window is 128K tokens on the Max plan", locator="c1"),
         "/quote quote is not in source src-ec-plan"),
        (_claim_doc("Example Cloud unicorn power", "128K", GOOD_QUOTE, locator="c2"),
         "/fields/key unknown fact key Example Cloud unicorn power"),
        (_claim_doc(KEY_WORDS, "80 GB", GOOD_QUOTE, locator="c3"),
         "/fields/value_raw value 80 GB uses GB, which doesn't convert to tokens"),
        (_claim_doc(KEY_WORDS, "64K", GOOD_QUOTE, locator="c4"), "/fields/value_raw value 64K is not in the quote"),
        (_claim_doc(KEY_WORDS, "128K", "Pro plan buyers get 128K tokens with every seat", locator="c5"),
         "/quote the quote doesn't name max context or one of its aliases"),
        (_claim_doc(KEY_WORDS, "128K", GOOD_QUOTE, locator="c6", scope="2.0"),
         "/fields/scope scope 2.0 is in neither the quote nor the source title"),
    ]
    entity = _claim_doc("excloud max context", "128K", "Pro plan buyers get 128K tokens with every seat",
                        locator="c5e", source="src-ec-untitled")
    _ok(_apply(inst, "alex", _src("src-ec-untitled", "Context notes: The context window is 128K tokens for everyone.",
                                  title="Untitled notes")))
    entity["quote"] = "The context window is 128K tokens for everyone"
    cases.append((entity, "/quote neither the quote nor the source title names excloud or one of its aliases"))
    for doc, error in cases:
        code, payload, err = _extract(inst, doc)
        reason = f"op 0 (apply_candidate): candidate is invalid: {error}"
        assert (code, err) == (4, f"brain: {reason}\n"), (doc, err)
        assert payload["preview"]["errors"] == [{"path": error.split(" ", 1)[0], "message": error.split(" ", 1)[1]}]
        log = _log(inst, payload["change_id"])
        assert (log["status"], log["hold_reason"]) == ("held", reason)
        assert _counts(inst) == (0, 0, 0)
    code, payload, err = _extract(inst, _passing())
    assert (code, err) == (0, ""), payload
    assert _log(inst, payload["change_id"])["status"] == "applied"
    stored = list(_claims(inst, CTX).values())
    assert [(claim["value_int"], claim["basis"], claim["proposed_by"], claim["checked"]) for claim in stored] == [
        (128000, "reported", "scan", 1)]
    # One that passes all six but would open a conflict runs its derived op in the savepoint, and the hold
    # rolls it back: no claim, fact or signal row of its own is left.
    after = _counts(inst)
    code, payload, err = _extract(inst, _claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year",
                                                   locator="c7"))
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would open a conflict on excloud.max_context (now)\n")
    assert _counts(inst) == after and _fact(inst, CTX)["status"] == "current"
    # C4 by type, through a claim candidate. Each fragment is held as invalid; each true value is stored.
    inst = _inst(tmp_path / "br1")
    _ok(_apply(inst, "alex", _src("src-br1", BR1_SOURCE, title="exunits and exbasic notes")))
    words = {CTX: KEY_WORDS, "excloud.gpu_memory": "Example Cloud GPU memory", "exunits.seats": "exunits seats",
             "exbasic.price": "exbasic price", "exunits.launch": "exunits launch", "exunits.site": "exunits site"}
    for index, (key, _value, raw, quote) in enumerate(BR1_FRAGMENTS):
        code, payload, err = _extract(inst, _claim_doc(words[key], raw, quote, source="src-br1", locator=f"f{index}"))
        reason = f"op 0 (apply_candidate): candidate is invalid: /fields/value_raw value {raw} is not in the quote"
        assert (code, err) == (4, f"brain: {reason}\n"), (raw, err)
        assert _log(inst, payload["change_id"])["hold_reason"] == reason
    assert _counts(inst) == (0, 0, 0)
    stored = {}
    for index, (key, value, raw, quote) in enumerate(BR1_TRUE):
        if key not in stored:
            code, payload, err = _extract(inst, _claim_doc(words[key], raw, quote, source="src-br1", locator=f"t{index}"))
            assert (code, err) == (0, ""), (raw, err)
            stored[key] = value
    for key, value in stored.items():
        fact = _fact(inst, key)
        assert units.typed(fact["value_type"], fact) == value and fact["status"] == "current", key


def _settled(inst: Path) -> tuple[str, str]:
    """bot's strict 128K, notes' 100K from the plan's Basic line, and alex's resolution rejecting it."""
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    bot = next(cid for cid, claim in _claims(inst, CTX).items() if claim["proposed_by"] == "bot")
    _ok(_apply(inst, "notes", _clm("clm-basic", CTX, "100K", "The context window is 100K tokens on the Basic plan",
                                   "src-ec-plan", locator="basic")))
    _ok(_apply(inst, "alex", _src("src-alex-ctx", "Alex: the limits page is right, the context window is 128K tokens.",
                                  kind="user_note", author="per-alex-example"),
               _resolve(CTX, [bot], ["clm-basic"], "the limits page is right, the context window is 128K tokens",
                        "src-alex-ctx")))
    return bot, "clm-basic"


def test_verified_approval_table(tmp_path: Path) -> None:
    agents = {**STRICT_AGENTS, "narrow": {"kind": "extractor", "operations": ["apply_candidate", "upsert_action"],
                                          "approval": "verified", "approvers": ["alex"]},
              "scan2": {"kind": "extractor", "operations": SCAN_OPS, "approval": "verified", "approvers": ["alex"]}}
    inst = _inst(tmp_path, strict_config(agents=agents))
    bot, rejected = _settled(inst)
    rows = []

    def run(doc, agent="scan"):
        code, payload, err = _extract(inst, doc, agent)
        log = _log(inst, payload["change_id"])
        rows.append((code, log["status"], log["hold_reason"], err))
        return code, payload, log

    # A claim passing C1 to C6, opening no conflict, superseding no other writer's claim: stored.
    code, payload, log = run(_passing("support"))
    assert (code, log["status"], log["hold_reason"]) == (0, "applied", "")
    stored = next(claim for claim in _claims(inst, CTX).values() if claim["proposed_by"] == "scan")
    assert (stored["basis"], stored["status"], stored["checked"]) == ("reported", "live", 1)
    # A second extractor's identical candidate is its own claim, recorded as support.
    code, payload, log = run(_passing("support"), agent="scan2")
    assert (code, log["status"], log["hold_reason"], log["error"]) == (0, "applied", "", "")
    support = {claim["proposed_by"]: claim["id"] for claim in _claims(inst, CTX).values() if claim["locator"] == "support"}
    assert sorted(support) == ["scan", "scan2"] and support["scan"] != support["scan2"]
    assert all(_claims(inst, CTX)[cid]["status"] == "live" for cid in support.values())
    assert _fact(inst, CTX)["status"] == "current"
    # A claim that would open a conflict: held, and the Held line names its key.
    grows = _claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year", locator="grows")
    code, held_payload, log = run(grows)
    opened = "op 0.0 (add_claim): would open a conflict on excloud.max_context (now)"
    assert (code, log["status"], log["hold_reason"]) == (4, "held", opened)
    assert "(1 would open a conflict: excloud.max_context)" in _held(inst)[0]
    # A claim superseding another writer's claim, with no conflict: the rule 3 hold.
    code, payload, log = run(_passing("supersede", supersedes=[bot]))
    assert (code, log["status"], log["hold_reason"]) == (
        4, "held", f"op 0.0 (add_claim): superseding {bot} by bot needs a person's approval")
    # A claim matching a rejected claim: rejected.
    again = _claim_doc(KEY_WORDS, "100K", "The context window is 100K tokens on the Basic plan", locator="basic")
    code, payload, log = run(again)
    assert (code, log["status"]) == (1, "rejected")
    assert log["error"] == (f"op 0.0 (add_claim): a claim with this key, scope, source, locator and value was "
                            f"rejected ({rejected})")
    # A derived op the extractor isn't allowed: rejected.
    code, payload, log = run(_passing("narrow"), agent="narrow")
    assert (code, log["status"], log["error"]) == (1, "rejected", "op 0.0 (add_claim): not allowed for agent narrow")
    # An action passing every check, status_basis reported: stored.
    code, payload, log = run(_action_doc("Price the Pro plan"))
    assert (code, log["status"]) == (0, "applied")
    assert ro_rows(inst, "SELECT status_basis FROM actions WHERE title = 'Price the Pro plan'") == [("reported",)]
    # An action whose status_basis is inferred: held.
    code, payload, log = run(_action_doc("Guess the Basic plan", basis="inferred"))
    assert (code, log["hold_reason"]) == (4, "op 0 (apply_candidate): an action with status_basis inferred needs a person")
    # Any decision: held.
    code, payload, log = run(_decision_doc("Keep the Pro plan"))
    assert (code, log["hold_reason"]) == (4, "op 0 (apply_candidate): a decision candidate always needs a person")
    # Anything else failing a check: a plan error, or a derived op's error, is held with its message.
    code, payload, log = run(_claim_doc(KEY_WORDS, "64K", GOOD_QUOTE, locator="bad"))
    assert (code, log["hold_reason"]) == (
        4, "op 0 (apply_candidate): candidate is invalid: /fields/value_raw value 64K is not in the quote")
    code, payload, log = run(_action_doc("Ask Alex", detail="Alex said the Pro plan is final."))
    assert code == 4 and log["hold_reason"].startswith("op 0.0 (upsert_action): evidence gate: ")
    assert all(err == (f"brain: {reason}\n" if reason else "") for _code, status, reason, err in rows
               if status != "rejected")
    # Approving the held conflicting claim applies it and disputes the key.
    writer = Applier(inst)
    try:
        approved = writer.approve(held_payload["change_id"], "alex")
    finally:
        writer.close()
    assert approved.receipt["status"] == "applied", approved.receipt
    assert _fact(inst, CTX)["status"] == "disputed"
    assert len(_conflicts(inst, CTX)) == 1


def _plan_source(inst: Path) -> None:
    _ok(_apply(inst, "alex", _src("src-ec-version", "Example Cloud release notes. The current version of Example Cloud "
                                                    "is 1.3 for every region.", title="Example Cloud release notes")))


VERSION_DOC = _claim_doc("Example Cloud current version", "1.3",
                         "The current version of Example Cloud is 1.3 for every region", source="src-ec-version")


def test_verified_conflict_test(tmp_path: Path) -> None:
    inst = _inst(tmp_path / "dispute")
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    _ok(_apply(inst, "notes", _clm("clm-basic", CTX, "100K", "The context window is 100K tokens on the Basic plan",
                                   "src-ec-plan", locator="basic")))
    assert _fact(inst, CTX)["status"] == "disputed"
    code, payload, err = _extract(inst, _passing("support"))
    assert (code, err) == (0, "")  # it agrees with one side
    code, payload, err = _extract(inst, _claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year",
                                                   locator="third"))
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would widen the conflict on excloud.max_context (now)\n")
    # Superseding another writer's claim while a third writer disagrees: the conflict reason, not rule 3's.
    code, payload, err = _extract(inst, _claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year",
                                                   locator="replace", supersedes=["clm-basic"]))
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would widen the conflict on excloud.max_context (now)\n")
    assert "excloud.max_context" in _held(inst)[0]
    # A version move that disputes another key of the entity names that key.
    moved = _inst(tmp_path / "version")
    _plan_source(moved)
    _ok(_apply(moved, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    _ok(_apply(moved, "notes", _clm("clm-r13", CTX, "64K", "Release 1.3 has a context window of 64K tokens",
                                    "src-ec-plan", scope="1.3", locator="r13")))
    code, payload, err = _extract(moved, VERSION_DOC)
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would open a conflict on excloud.max_context (now)\n")
    assert _fact(moved, "excloud.current_version") is None
    # A version move that adds a third value to another key's already-disputed now bucket: widen.
    third = _inst(tmp_path / "third")
    _plan_source(third)
    _ok(_apply(third, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    _ok(_apply(third, "notes", _clm("clm-basic", CTX, "100K", "The context window is 100K tokens on the Basic plan",
                                    "src-ec-plan", locator="basic"),
               _clm("clm-r13", CTX, "64K", "Release 1.3 has a context window of 64K tokens", "src-ec-plan",
                    scope="1.3", locator="r13")))
    code, payload, err = _extract(third, VERSION_DOC)
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would widen the conflict on excloud.max_context (now)\n")
    # A claim superseded on arrival by rule 1b is stored: it never joins the bucket.
    late = _inst(tmp_path / "late")
    _ok(_apply(late, "alex", _src("src-limits-old", "Old limits. The context window is 100K tokens on every plan.",
                                  title="Example Cloud limits, old"),
               _src("src-limits-new", "New limits. Context window: 128K tokens.", supersedes="src-limits-old",
                    title="Example Cloud limits, new")))
    _ok(_apply(late, "bot", _fact_op(CTX, 128000, "src-limits-new", quote="Context window: 128K tokens", raw="128K")))
    code, payload, err = _extract(late, _claim_doc(KEY_WORDS, "100K", "The context window is 100K tokens on every plan",
                                                   source="src-limits-old"))
    assert (code, err) == (0, "")
    arrived = next(claim for claim in _claims(late, CTX).values() if claim["proposed_by"] == "scan")
    assert arrived["status"] == "superseded"
    assert _fact(late, CTX)["status"] == "current"


def test_run_window_holds_the_26th(tmp_path: Path, clock: Clock) -> None:
    from datetime import datetime, timedelta

    inst = _inst(tmp_path)
    start = datetime(2026, 10, 9)

    def at(seconds: int) -> None:
        clock.at((start + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    for n in range(24):
        at(60 * n)
        code, payload, err = _extract(inst, _passing(f"w{n}"))
        assert (code, err) == (0, ""), (n, err)
    # A person's approval isn't counted.
    at(60 * 24)
    code, payload, err = _extract(inst, _decision_doc("Keep the Pro plan"))
    writer = Applier(inst)
    try:
        assert writer.approve(payload["change_id"], "alex").receipt["status"] == "applied"
    finally:
        writer.close()
    at(60 * 25)
    code, payload, err = _extract(inst, _passing("w24"))
    assert (code, err) == (0, "")
    at(60 * 26)
    code, payload, err = _extract(inst, _passing("w25"))
    guard = "op 0 (apply_candidate): mass-change guard: scan stored 25 candidates in the last hour"
    assert (code, err) == (4, f"brain: {guard}\n")
    assert _log(inst, payload["change_id"])["hold_reason"] == guard
    assert len(_claims(inst, CTX)) == 25
    at(3601)
    code, payload, err = _extract(inst, _passing("w26"))
    assert (code, err) == (0, "")
    assert len(_claims(inst, CTX)) == 26


def test_verified_config_rules(tmp_path: Path) -> None:
    def load(agents: dict) -> config_mod.Config:
        path = tmp_path / "agents.toml"
        path.write_text(render_config(agents=agents), encoding="utf-8")
        return load_config(path)

    tester = {"tester": {"operations": "*"}}
    ops = ["apply_candidate", "upsert_action"]
    inst = _inst(tmp_path / "inst")
    assert load_config(inst / "brain.toml").agents["scan"].approval == "verified"
    cases = [
        ({**tester, "scan": {"kind": "extractor", "operations": ops, "approval": "none", "approvers": ["tester"]}},
         'agents.scan: an extractor must have approval = "verified" or "required"'),
        ({**tester, "bot": {"kind": "agent", "operations": "*", "approval": "verified", "approvers": ["tester"]}},
         'agents.bot: approval = "verified" is only for extractors'),
        ({**tester, "scan": {"kind": "extractor", "operations": ops, "approval": "verified"}},
         "agents.scan requires approvers"),
        ({**tester, "scan": {"kind": "extractor", "operations": ops, "approval": "verified", "approvers": ["nobody"]}},
         "agents.scan has unknown or self approver nobody"),
        ({**tester, "scan": {"kind": "extractor", "operations": ops, "approval": "verified", "approvers": ["scan"]}},
         "agents.scan has unknown or self approver scan"),
    ]
    for agents, message in cases:
        with pytest.raises(ConfigError, match=re.escape(message)):
            load(agents)


def test_required_holds_and_records_the_verdict(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    _bot, rejected = _settled(inst)
    before = _counts(inst)
    cases = [
        (_claim_doc(KEY_WORDS, "128K", "The context window is 128K tokens on the Max plan", locator="c1"),
         "op 0 (apply_candidate): candidate is invalid: /quote quote is not in source src-ec-plan"),
        (_passing("ok"), "op 0 (apply_candidate): approval is required; under verified it would be stored"),
        (_claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year", locator="grows"),
         "op 0.0 (add_claim): would open a conflict on excloud.max_context (now)"),
        (_claim_doc(KEY_WORDS, "100K", "The context window is 100K tokens on the Basic plan", locator="basic"),
         f"op 0.0 (add_claim): a claim with this key, scope, source, locator and value was rejected ({rejected})"),
    ]
    logs = ro_rows(inst, "SELECT count(*) FROM write_log")[0][0]
    for doc, reason in cases:
        for attempt in ("first", "resubmitted"):
            code, payload, err = _extract(inst, doc, agent="careful")
            assert (code, err) == (4, f"brain: {reason}\n"), (attempt, err)
            assert payload["duplicate"] is (attempt == "resubmitted")
            log = _log(inst, payload["change_id"])
            assert (log["status"], log["hold_reason"]) == ("held", reason)
        assert _counts(inst) == before
    assert ro_rows(inst, "SELECT count(*) FROM write_log")[0][0] == logs + len(cases)
    assert "(1 would open a conflict: excloud.max_context)" in _held(inst)[0]
    # A non-extractor agent with approval = "required" is held before its ops run, with an empty reason.
    agents = {**STRICT_AGENTS, "slow": {"kind": "agent", "operations": "*", "approval": "required",
                                        "approvers": ["alex"]}}
    other = _inst(tmp_path / "slow", strict_config(agents=agents))
    held = _apply(other, "slow", _record_op("rec-slow", "Held before its ops run."))
    assert (held.exit_code, held.warning, _log(other, held.receipt["change_id"])["hold_reason"]) == (4, "", "")


def test_held_version_1_claim_candidates(tmp_path: Path) -> None:
    from brain import migrate
    from tests.test_migrate import _v4_config, v4_store

    old = v4_store(tmp_path / "v4", _v4_config())
    assert migrate.migrate(old / "brain.sqlite3") == (4, migrate.latest_version())
    assert ro_rows(old, "SELECT status, error FROM write_log WHERE change_id = 'scan-held-2'") == [
        ("rejected", "candidate format changed in 0.7.0")]
    inst = _inst(tmp_path / "a1")
    packet = {"packet_version": 1, "change_id": "scan-v1held", "agent": "scan", "intent": "held",
              "ops": [{"op": "apply_candidate", "candidate": V1_CLAIM}]}
    text = store.canonical_json(packet)
    con = context_connection(inst)
    try:
        con.execute(
            "INSERT INTO write_log (change_id, agent, intent, status, decided_at, payload_sha256, packet_json) "
            "VALUES ('scan-v1held', 'scan', 'held', 'held', '2026-10-09T00:00:00Z', ?, ?)",
            (store.sha256_text(text), text),
        )
        con.commit()
    finally:
        con.close()
    from brain.validate import validate_candidate

    errors = validate_candidate(json.loads(text)["ops"][0]["candidate"])  # as stored: keys sorted
    code, out, err = _cli("held", "--instance", inst)
    item = next(entry for entry in json.loads(out)["held"] if entry["change_id"] == "scan-v1held")
    assert item["kind"] == "claim_candidate"
    assert item["preview"] == {"valid": False, "appliable": False, "errors": errors, "ops": []}
    writer = Applier(inst)
    try:
        outcome = writer.approve("scan-v1held", "alex")
    finally:
        writer.close()
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"].startswith(
        "packet is invalid: /ops/0/candidate/candidate_version must be 2; /ops/0/candidate/fields/")


def test_verified_baseline_shares(tmp_path: Path, capsys) -> None:
    inst = _inst(tmp_path)
    _settled(inst)
    docs = [_passing(f"base{n}") for n in range(8)]
    docs += [
        _claim_doc(KEY_WORDS, "128K", "The context window is 128K tokens on the Max plan", locator="f1"),
        _claim_doc("Example Cloud unicorn power", "128K", GOOD_QUOTE, locator="f2"),
        _claim_doc(KEY_WORDS, "64K", GOOD_QUOTE, locator="f3"),
        _claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year", locator="x1"),
        _claim_doc(KEY_WORDS, "64K", "Release 1.3 has a context window of 64K tokens", locator="x2"),
        _claim_doc(KEY_WORDS, "100K", "The context window is 100K tokens on the Basic plan", locator="basic"),
        _action_doc("Price the Pro plan"),
        _action_doc("Price the Basic plan", quote="The context window is 100K tokens on the Basic plan"),
        _action_doc("Plan next year", quote="The context window grows to 256K tokens next year"),
        _action_doc("Guess the seat price", basis="inferred"),
        _decision_doc("Keep the Pro plan"),
        _decision_doc("Keep the Basic plan"),
    ]
    assert len(docs) == 20
    outcomes = Counter()
    for doc in docs:
        _code, payload, _err = _extract(inst, doc)
        outcomes[payload["receipt"]["status"]] += 1
    assert dict(outcomes) == {"applied": 11, "held": 8, "rejected": 1}
    with capsys.disabled():
        print(f"\nverified baseline shares: stored {outcomes['applied']}/20 ({outcomes['applied'] * 5}%), "
              f"held {outcomes['held']}/20 ({outcomes['held'] * 5}%), rejected {outcomes['rejected']}/20 "
              f"({outcomes['rejected'] * 5}%)")


# -- the Held line --------------------------------------------------------------------------------

PRICES = ("exbasic price list. The exbasic price is 49.5 AUD a seat. The exbasic price is 55 AUD a seat for teams. "
          "The exbasic price is 60 AUD a seat next year.")


def _held_rows(inst: Path, rows: list[tuple[str, str, str, str]]) -> None:
    """Held write_log rows (change_id, agent, decided_at, hold_reason), written on a context connection."""
    con = context_connection(inst)
    try:
        con.execute("BEGIN")
        for change_id, agent, decided_at, reason in rows:
            packet = store.canonical_json({"packet_version": 1, "change_id": change_id, "agent": agent,
                                           "intent": "held", "ops": [{"op": "rebuild_search"}]})
            con.execute(
                "INSERT INTO write_log (change_id, agent, intent, status, decided_at, payload_sha256, packet_json, "
                "hold_reason) VALUES (?, ?, 'held', 'held', ?, ?, ?, ?)",
                (change_id, agent, decided_at, store.sha256_text(packet), packet, reason),
            )
        con.execute("COMMIT")
    finally:
        con.close()


def _reason(key: str, verb: str = "open") -> str:
    return schemas.held_conflict_reason("0.0", verb, key, "now")


LONG_KEYS = ["k" + "a" * 58 + "." + "b" * 60, "k" + "c" * 58 + "." + "d" * 60]


def _worst_held_rows(agent: str) -> list[tuple[str, str, str, str]]:
    """The Held line's worst case: two shown keys of 120 characters, and the held count, the
    conflict count, the +<m> more count and the age all at five digits."""
    rows = [(f"{agent}-w{n:05d}", agent, "1999-01-01T00:00:00Z", _reason(key, "widen")) for n, key in enumerate(LONG_KEYS)]
    rows += [(f"{agent}-x{n:05d}", agent, "1999-01-02T00:00:00Z", _reason(f"exk{n:05d}.value")) for n in range(10_000)]
    return rows


def test_brief_held_line(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path / "example")
    assert _held(inst) == []
    _ok(_apply(inst, "alex", _src("src-prices", PRICES, title="exbasic price list")))
    clock.at("2026-10-06T12:00:00Z")
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    code, _payload, err = _extract(inst, _claim_doc(KEY_WORDS, "256K", "The context window grows to 256K tokens next year"))
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would open a conflict on excloud.max_context (now)\n")
    clock.at("2026-10-07T12:00:00Z")
    _ok(_apply(inst, "bot", _fact_op("exbasic.price", 49.5, "src-prices", quote="The exbasic price is 49.5 AUD a seat",
                                     raw="49.5 AUD")))
    _ok(_apply(inst, "notes", _clm("clm-p55", "exbasic.price", "55 AUD", "The exbasic price is 55 AUD a seat for teams",
                                   "src-prices")))
    code, _payload, err = _extract(inst, _claim_doc("exbasic price", "60 AUD", "The exbasic price is 60 AUD a seat next year",
                                                    source="src-prices"))
    assert (code, err) == (4, "brain: op 0.0 (add_claim): would widen the conflict on exbasic.price (now)\n")
    clock.at("2026-10-08T12:00:00Z")
    _extract(inst, _decision_doc("Keep the Pro plan"))
    _extract(inst, _decision_doc("Keep the Basic plan"))
    example = ("Held: 4 waiting for a person (2 would open a conflict: excloud.max_context, exbasic.price). "
               "Oldest 3 days. Ask the person to review: brain held.")
    assert _held(inst) == [example]
    assert len(example.encode()) == 144
    lines = _brief(inst)
    held_at = lines.index(example)
    assert lines[held_at + 1].startswith(("No stale facts.", "1 fact stale", "2 facts stale"))
    assert lines[held_at - 1].startswith(("Facts:", "Disputed ", "Changed in the last"))
    # More than two keys, ages, a non-extractor's hold, and a free-text key quoted in another message.
    more = _inst(tmp_path / "more")
    keys = ["exa.one", "exb.two", "exc.three", "exd.four"]
    _held_rows(more, [(f"scan-k{n}", "scan", "2026-10-09T00:00:00Z", _reason(key)) for n, key in enumerate(keys)])
    assert _held(more) == ["Held: 4 waiting for a person (4 would open a conflict: exa.one, exb.two, +2 more). "
                           "Oldest under a day. Ask the person to review: brain held."]
    ages = _inst(tmp_path / "ages")
    _held_rows(ages, [("slow-old", "slow", "2026-09-30T12:00:00Z", "")])
    assert _held(ages) == ["Held: 1 waiting for a person. Oldest 9 days. Ask the person to review: brain held."]
    assert _held(ages, now="2026-10-01T12:00:00Z") == [
        "Held: 1 waiting for a person. Oldest 1 day. Ask the person to review: brain held."]
    assert _held(ages, now="2026-09-30T13:00:00Z") == [
        "Held: 1 waiting for a person. Oldest under a day. Ask the person to review: brain held."]
    assert _held(ages, now="2026-09-29T00:00:00Z") == [
        "Held: 1 waiting for a person. Oldest under a day. Ask the person to review: brain held."]
    # The free-text key quotes a whole conflict reason, so only the anchored match keeps it out.
    free = "op 0.0 (add_claim): would open a conflict on excloud.price (now)"
    code, _payload, err = _extract(ages, _claim_doc(free, "128K", GOOD_QUOTE))
    assert err == f"brain: op 0 (apply_candidate): candidate is invalid: /fields/key unknown fact key {free}\n"
    assert _held(ages, now="2026-10-09T12:00:00Z") == [
        "Held: 2 waiting for a person. Oldest 9 days. Ask the person to review: brain held."]
    # The worst case: two 120-character keys and five-digit counts still fit in 200 bytes, at b 31.
    wide = _inst(tmp_path / "wide")
    assert [len(key) for key in LONG_KEYS] == [120, 120]
    _held_rows(wide, _worst_held_rows("scan"))
    line = _held(wide)[0]
    assert len(line.encode()) == 199
    assert line.startswith("Held: 10002 waiting for a person (10002 would open a conflict: ")
    assert line.endswith(", +10000 more). Oldest 10143 days. Ask the person to review: brain held.")
    assert all(reads._cut_bytes(key, 31) in line for key in LONG_KEYS)
    assert reads._cut_bytes(LONG_KEYS[0], 31).endswith("...")
    longer = line.replace(reads._cut_bytes(LONG_KEYS[0], 31), reads._cut_bytes(LONG_KEYS[0], 32)).replace(
        reads._cut_bytes(LONG_KEYS[1], 31), reads._cut_bytes(LONG_KEYS[1], 32))
    assert len(longer.encode()) == 201  # b 32 doesn't fit
    print(f"Held line worst case: {len(line.encode())} bytes: {line}")


def worst_brief_instance(tmp_path: Path, clock: Clock, extra_agents=None, collectors=None) -> Path:
    """1a test 44's fixture (tests/test_truth.py::test_brief_budget_with_truth_lines) plus a Held line at its
    cap. Shared with tests/test_bench_gate.py (A2's worst-case brief)."""
    from tests import test_phase7 as p7
    from tests.test_truth import _apply as truth_apply, _put

    workstreams = [
        {"id": "general", "title": "General", "description": "", "sort_order": 1},
        *[{"id": f"w{index:02d}", "title": "W", "description": "", "sort_order": index} for index in range(2, 13)],
    ]
    bulk = [{"key": f"bulk.f{index}", "value_type": "text", "stale_after_days": 1} for index in range(9)]
    disputed = [{"key": f"dsp{index}." + "d" * 115, "value_type": "text"} for index in range(6)]
    changed = [{"key": f"chg{index}." + "c" * 115, "value_type": "text"} for index in range(5)]
    agents = {**p7.AGENTS, "other": {"operations": "*"}, **(extra_agents or {})}
    extra = {} if collectors is None else {"collectors": collectors}
    instance = p7._instance(
        tmp_path, workstreams=workstreams, fact_keys=[*bulk, *disputed, *changed], agents=agents, **extra
    )
    lefts = [(f"left {index} " * 400)[:480] for index in range(len(disputed))]
    rights = [(f"right {index} " * 400)[:480] for index in range(len(disputed))]
    firsts = [f"The value is first {index}" for index in range(len(changed))]
    seconds = [f"The value is second {index}" for index in range(len(changed))]
    _ok(truth_apply(instance, "tester", _src("src-t1", "Truth one. " + " ".join(f"{text}." for text in [*lefts, *firsts, *seconds]),
                                             kind="meeting_transcript"),
                    _src("src-t2", "Truth two. " + " ".join(f"{text}." for text in rights), kind="meeting_transcript")))
    for index, item in enumerate(disputed):
        _ok(truth_apply(instance, "tester", _put(instance, item["key"], lefts[index], "src-t1", quote=lefts[index],
                                                 raw=lefts[index])))
        _ok(truth_apply(instance, "other", _put(instance, item["key"], rights[index], "src-t2", quote=rights[index],
                                                raw=rights[index])))
    for index, item in enumerate(changed):
        _ok(truth_apply(instance, "tester", _put(instance, item["key"], f"first {index}", "src-t1", quote=firsts[index],
                                                 raw=f"first {index}")))
        _ok(truth_apply(instance, "tester", _put(instance, item["key"], f"second {index}", "src-t1",
                                                 quote=seconds[index], raw=f"second {index}")))
    title = ("Example Co regional rollout " * 5)[:120]
    ops = p7._crowded_ops(lambda prefix, index: f"{prefix}{index:02d}", lambda index: f"proj-p{index}", title)
    p7._crowd(instance, ops, bulk)
    _held_rows(instance, _worst_held_rows("tester"))
    return instance


def assert_worst_brief(instance: Path) -> list[str]:
    """The worst-case brief's limits; returns its lines."""
    from tests import test_phase7 as p7

    text = p7._brief(instance)
    lines = text.splitlines()
    assert len(lines) <= 40
    assert all(len(line.encode()) <= 200 for line in lines)
    assert len(text.encode()) <= 4096
    held = [line for line in lines if line.startswith("Held: 10002 waiting for a person")]
    assert len(held) == 1 and len(held[0].encode()) == 199
    return lines


def test_brief_budget_with_the_held_line(tmp_path: Path, clock: Clock) -> None:
    """The worst-case brief fixture, then 99,999 proposed actions."""
    instance = worst_brief_instance(tmp_path, clock)
    check = lambda: assert_worst_brief(instance)  # noqa: E731
    lines = check()
    print(f"brief with truth lines and the Held line: {len(lines)} lines, {len(chr(10).join(lines).encode()) + 1} bytes")
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.executemany(
            "INSERT INTO actions (id, title, status, status_basis, project_id, last_verified_at, source_id, locator, "
            "version, created_at, updated_at, updated_by) VALUES (?, 'P', 'proposed', 'reported', 'general', "
            "'2026-10-01T00:00:00Z', 'src-p7', 'x', 1, '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', 'tester:bulk')",
            [(f"act-prop{n:05d}",) for n in range(99_999)],
        )
        con.execute("COMMIT")
    finally:
        con.close()
    lines = check()
    actions = next(line for line in lines if line.startswith("Actions: "))
    assert ", 99999 proposed. Overdue: " in actions


# -- relay ----------------------------------------------------------------------------------------


class _Stdin:
    def __init__(self, tty) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        if isinstance(self.tty, Exception):
            raise self.tty
        return self.tty


def test_relayed_approval_records_agent_relay(tmp_path: Path, monkeypatch) -> None:
    from brain import cli

    inst = _inst(tmp_path)
    held = []
    for n in range(6):
        code, payload, _err = _extract(inst, _decision_doc(f"Decide the plan {n}"))
        assert code == 4
        held.append(payload["change_id"])
    receipt_keys = None
    for change_id, tty, extra, expected in (
        (held[0], False, [], "agent-relay"),
        (held[1], True, [], "terminal"),
        (held[2], True, ["--relay"], "agent-relay"),
        (held[3], OSError("no terminal"), [], "agent-relay"),
    ):
        monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty))
        code, out, _err_text = _cli("approve", "--instance", inst, change_id, "--as", "alex", *extra)
        assert code == 0, out
        assert _log(inst, change_id)["approved_via"] == expected
        receipt = json.loads(out)
        receipt_keys = receipt_keys or list(receipt)
        assert list(receipt) == receipt_keys and "approved_via" not in receipt
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(True))
    code, out, _err_text = _cli("refuse", "--instance", inst, held[4], "--as", "alex", "--relay", "--note", "not now")
    assert code == 1 and json.loads(out)["error"] == "refused by alex: not now"
    assert _log(inst, held[4])["approved_via"] == "agent-relay"
    # An approval that fails records how it came too.
    code, payload, _err = _extract(inst, _decision_doc("Decide the plan 0", detail="A second reading."))
    assert code == 4
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(False))
    code, out, _err_text = _cli("approve", "--instance", inst, payload["change_id"], "--as", "alex")
    assert code == 1 and json.loads(out)["error"].startswith("approved by alex but not applied: ")
    assert _log(inst, payload["change_id"])["approved_via"] == "agent-relay"
    # Packets decided without a review keep ''.
    assert ro_rows(inst, "SELECT DISTINCT approved_via FROM write_log WHERE reviewed_by = ''") == [("",)]


def test_held_lists_approvers_and_the_relay_rule(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    code, payload, _err = _extract(inst, _decision_doc("Decide the plan"))
    _held_rows(inst, [("ghost-old", "ghost", "2026-10-09T00:00:00Z", "")])
    code, out, err = _cli("held", "--instance", inst)
    assert (code, err) == (0, "")
    body = json.loads(out)
    assert list(body)[-1] == "relay"
    assert body["relay"] == (
        "Show each item to the person. Run brain approve <change_id> --as <approver> --relay only on their clear "
        "yes, and brain refuse <change_id> --as <approver> --relay --note <why> on a no.")
    by_id = {item["change_id"]: item for item in body["held"]}
    assert by_id[payload["change_id"]]["approvers"] == ["alex"]
    assert by_id["ghost-old"]["approvers"] == []
    assert by_id[payload["change_id"]]["reason"] == "op 0 (apply_candidate): a decision candidate always needs a person"


# -- legacy claims and verify_claim ----------------------------------------------------------------

LEGACY = ("Legacy notes. The context window is 128K tokens. Planned: 256K tokens. It was 64K tokens once. "
          "Basic gets 100K tokens. VRAM is 80 GB on 1.3. We have 40 seats. We had 41 seats. We have 12 desks. "
          "We had 13 desks.")


def _legacy(cid: str, key: str, raw: str, *, source: str = "src-legacy", locator: str = "lg", **fields) -> dict:
    row = {"id": cid, "key": key, "value_raw": raw, "basis": "reported", **fields}
    return {"op": "add_claim", "row": row, "source": {"source_id": source, "locator": locator}}


def _verify(claim_id: str, quote: str, source: str = "src-legacy", locator: str = "lg") -> dict:
    return {"op": "verify_claim", "row": {"claim_id": claim_id, "quote": quote},
            "source": {"source_id": source, "locator": locator}}


def _legacy_inst(tmp_path: Path) -> Path:
    inst = _inst(tmp_path)
    _ok(_apply(inst, "alex", _src("src-legacy", LEGACY, title="Legacy import")))
    return inst


def test_legacy_claims_import_unchecked(tmp_path: Path, clock: Clock) -> None:
    inst = _legacy_inst(tmp_path)
    resolution = {"resolution_source_id": "src-ec-plan", "resolution_locator": "basic"}
    _ok(_apply(inst, "importer",
               _legacy("clm-l1", CTX, "128K", status="live"),
               _legacy("clm-l2", CTX, "256K", status="planned", locator="lg2"),
               _legacy("clm-l3", CTX, "64K", status="superseded", superseded_by="clm-l1", locator="lg3"),
               _legacy("clm-l4", CTX, "100K", status="rejected", locator="lg4", **resolution),
               _legacy("clm-l5", CTX, "64K", scope="1.3", locator="lg5"),
               _legacy("clm-l6", "excloud.gpu_memory", "80 GB", scope="1.3")))
    claims = _claims(inst, CTX)
    assert {cid: (claim["status"], claim["quote"], claim["checked"]) for cid, claim in claims.items()} == {
        "clm-l1": ("live", "", 0), "clm-l2": ("planned", "", 0), "clm-l3": ("superseded", "", 0),
        "clm-l4": ("rejected", "", 0), "clm-l5": ("live", "", 0)}
    assert claims["clm-l3"]["superseded_by"] == "clm-l1"
    item = _read(inst, "fact_get", key=CTX).structured["facts"][0]
    assert item["value"] == 128000 and item["claims"]["confidence"] == "low"
    assert [(entry["id"], entry["checked"]) for entry in item["claims"]["values"]] == [("clm-l1", False)]
    assert [(entry["id"], entry["checked"], list(entry)[-1]) for entry in item["claims"]["planned"]] == [
        ("clm-l2", False, "source")]
    assert item["claims"]["other_scopes"] == {"1.3": {"value": 64000, "checked": False}}
    code, message = (lambda r: (r.code, json.loads(r.text)["error"]["message"]))(
        _read(inst, "fact_get", key="excloud.gpu_memory"))
    assert code == "no_value"
    assert "Values by version: 1.3: 80 GB (unchecked)." in message
    got = _read(inst, "get", entity="claim", id="clm-l4").structured
    assert (got["row"]["resolution_source_id"], got["row"]["resolution_locator"], got["row"]["checked"]) == (
        "src-ec-plan", "basic", False)
    assert got["related"]["resolution_source"]["id"] == "src-ec-plan"
    # A rejected claim named in advance by a live claim of the same writer stays rejected.
    _ok(_apply(inst, "importer", _legacy("clm-l7", "exunits.count", "12", supersedes=["clm-l8"]),
               _legacy("clm-l8", "exunits.count", "13", status="rejected", locator="lg8", **resolution)))
    assert _claims(inst, "exunits.count")["clm-l8"]["status"] == "rejected"
    # A legacy claim adding an existing fact's value leaves its last_verified_at alone.
    _ok(_apply(inst, "bot", _fact_op("exunits.seats", 40, "src-legacy", quote="We have 40 seats", raw="40",
                                     verified="2026-10-01T00:00:00Z")))
    clock.at("2026-10-09T05:00:00Z")
    _ok(_apply(inst, "importer", _legacy("clm-l9", "exunits.seats", "40", locator="lg9"),
               _legacy("clm-l10", "exunits.seats", "41", status="rejected", locator="lg10", **resolution)))
    assert _fact(inst, "exunits.seats")["last_verified_at"] == "2026-10-01T00:00:00Z"
    # A legacy rejected claim's writer re-sending that value has it recorded and judged, not suppressed.
    resent = _ok(_apply(inst, "importer", _fact_op("exunits.seats", 41, "src-legacy", quote="We had 41 seats",
                                                   raw="41", locator="lg11", base=_base(inst, "facts", "key", "exunits.seats"))))
    assert resent.warning == ""
    assert _fact(inst, "exunits.seats")["status"] == "disputed"


def test_legacy_superseded_by_links(tmp_path: Path) -> None:
    inst = _legacy_inst(tmp_path)
    _ok(_apply(inst, "importer", _legacy("clm-a", CTX, "64K", status="superseded", superseded_by="clm-b"),
               _legacy("clm-b", CTX, "128K", locator="lg2")))
    assert _claims(inst, CTX)["clm-a"]["superseded_by"] == "clm-b"
    _ok(_apply(inst, "importer", _legacy("clm-c", CTX, "100K", status="superseded", superseded_by="clm-b",
                                         locator="lg3")))
    assert _claims(inst, CTX)["clm-c"]["superseded_by"] == "clm-b"
    refusals = [
        ([_legacy("clm-d", CTX, "64K", status="superseded", superseded_by="clm-nope", locator="lg4")],
         "op 0 (add_claim): superseded_by clm-nope is not a claim in this packet or the store"),
        ([_legacy("clm-d", CTX, "64K", status="superseded", superseded_by="clm-g", locator="lg4"),
          _legacy("clm-g", "exunits.seats", "40")],
         "op 0 (add_claim): superseded_by clm-g, a claim on another key"),
        ([_legacy("clm-e", CTX, "64K", status="superseded", superseded_by="clm-f", locator="lg5"),
          _legacy("clm-f", CTX, "128K", status="superseded", superseded_by="clm-e", locator="lg6")],
         "op 1 (add_claim): superseded_by would form a cycle through clm-e"),
        ([_legacy("clm-d", CTX, "64K", status="superseded", superseded_by="clm-d", locator="lg4")],
         "op 0 (add_claim): superseded_by names the claim itself"),
        ([_legacy("clm-d", CTX, "64K", status="superseded", superseded_by="clm-later", locator="lg4")],
         "op 0 (add_claim): superseded_by clm-later is not a claim in this packet or the store"),
    ]
    for ops, message in refusals:
        assert _err(_apply(inst, "importer", *ops)) == message
    assert "clm-d" not in _claims(inst, CTX) and "clm-e" not in _claims(inst, CTX)
    _ok(_apply(inst, "importer", _legacy("clm-later", CTX, "64K", locator="lg7")))
    # An identical re-import is a no-op and records no link.
    again = _ok(_apply(inst, "importer", _legacy("clm-c", CTX, "100K", status="superseded", superseded_by="clm-b",
                                                 locator="lg3")))
    assert again.receipt["targets"] == []
    # A re-import can't change a stored claim's status or links.
    resolution = {"resolution_source_id": "src-ec-plan", "resolution_locator": "basic"}
    _ok(_apply(inst, "importer", _legacy("clm-l1", CTX, "128K", locator="lg8", status="live")))
    changes = [
        (_legacy("clm-c", CTX, "100K", status="superseded", superseded_by="clm-later", locator="lg3"),
         "op 0 (add_claim): claim clm-c is stored as superseded; a re-import can't change it"),
        (_legacy("clm-l1", CTX, "128K", locator="lg8", status="rejected", **resolution),
         "op 0 (add_claim): claim clm-l1 is stored as live; a re-import can't change it"),
        (_legacy("clm-l1", CTX, "128K", locator="lg8", status="superseded", superseded_by="clm-zz"),
         "op 0 (add_claim): claim clm-l1 is stored as live; a re-import can't change it"),
        (_legacy("clm-l1", CTX, "128K", locator="lg8", status="planned"),
         "op 0 (add_claim): claim clm-l1 is stored as live; a re-import can't change it"),
    ]
    for op, message in changes:
        assert _err(_apply(inst, "importer", op)) == message
    claims = _claims(inst, CTX)
    assert (claims["clm-c"]["superseded_by"], claims["clm-l1"]["status"]) == ("clm-b", "live")
    # The same rejection again is a no-op, and a changed resolution is refused.
    rejected = _legacy("clm-r", CTX, "100K", locator="lg9", status="rejected", **resolution)
    _ok(_apply(inst, "importer", rejected))
    assert _ok(_apply(inst, "importer", rejected)).receipt["targets"] == []
    moved = _legacy("clm-r", CTX, "100K", locator="lg9", status="rejected", **{**resolution, "resolution_locator": "pro"})
    assert _err(_apply(inst, "importer", moved)) == (
        "op 0 (add_claim): claim clm-r is stored as rejected; a re-import can't change it")
    # Live for a claim the engine has since superseded is a no-op: the engine moved it on.
    _ok(_apply(inst, "importer", _legacy("clm-l2", CTX, "128K", locator="lg10", supersedes=["clm-l1"])))
    assert _claims(inst, CTX)["clm-l1"]["status"] == "superseded"
    resent = _apply(inst, "importer", _legacy("clm-l1", CTX, "128K", locator="lg8", status="live"))
    assert _ok(resent).receipt["targets"] == []
    # Re-running an import after a person rejected one of its claims is a no-op for that claim.
    inst = _legacy_inst(tmp_path / "rerun")
    run = [_legacy("clm-i1", CTX, "64K", locator="i1"), _legacy("clm-i2", CTX, "128K", locator="i2")]
    _ok(_apply(inst, "importer", *run))
    assert _fact(inst, CTX)["status"] == "disputed"
    _ok(_apply(inst, "alex", _src("src-alex-ctx", "Alex: the limits page is right, the context window is 128K tokens.",
                                  kind="user_note", author="per-alex-example"),
               _resolve(CTX, ["clm-i2"], ["clm-i1"], "the limits page is right, the context window is 128K tokens",
                        "src-alex-ctx")))
    assert _claims(inst, CTX)["clm-i1"]["status"] == "rejected"
    rerun = _apply(inst, "importer", *run)
    assert _ok(rerun).receipt["targets"] == []
    assert ({cid: claim["status"] for cid, claim in _claims(inst, CTX).items()}, _fact(inst, CTX)["status"]) == (
        {"clm-i1": "rejected", "clm-i2": "live"}, "current")
    planned = _legacy("clm-i1", CTX, "64K", locator="i1", status="planned")
    assert _ok(_apply(inst, "importer", planned)).receipt["targets"] == []
    superseded = _legacy("clm-i1", CTX, "64K", locator="i1", status="superseded", superseded_by="clm-i2")
    assert _err(_apply(inst, "importer", superseded)) == (
        "op 0 (add_claim): claim clm-i1 is stored as rejected; a re-import can't change it")


def test_legacy_claims_keep_conflicts_units_and_keys(tmp_path: Path) -> None:
    inst = _legacy_inst(tmp_path)
    assert _err(_apply(inst, "importer", _legacy("clm-x", "excloud.nope", "128K"))) == (
        "op 0 (add_claim): unknown fact key excloud.nope")
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "80 GB"))) == (
        "op 0 (add_claim): value 80 GB uses GB, which doesn't convert to tokens")
    assert _err(_apply(inst, "importer", _legacy("clm-x", "excloud.current_version", "1.3", scope="1.2"))) == (
        "op 0 (add_claim): excloud.current_version is a current_version key and must be unscoped")
    _ok(_apply(inst, "bot", _fact_op(CTX, 128000, "src-ec-limits", quote="Context window: 128K tokens", raw="128K")))
    _ok(_apply(inst, "importer", _legacy("clm-l1", CTX, "100K")))
    assert _fact(inst, CTX)["status"] == "disputed"
    assert len(_conflicts(inst, CTX)) == 1
    assert any(line.startswith("Disputed excloud.max_context (now): ") for line in _brief(inst))
    # E39 refuses a live legacy claim matching a rejected one.
    bot = next(cid for cid, claim in _claims(inst, CTX).items() if claim["proposed_by"] == "bot")
    _ok(_apply(inst, "alex", _src("src-alex-ctx", "Alex: the limits page is right, the context window is 128K tokens.",
                                  kind="user_note", author="per-alex-example"),
               _resolve(CTX, [bot], ["clm-l1"], "the limits page is right, the context window is 128K tokens",
                        "src-alex-ctx")))
    again = _apply(inst, "importer", _legacy("clm-l2", CTX, "100K"))
    assert _err(again) == (
        "op 0 (add_claim): a claim with this key, scope, source, locator and value was rejected (clm-l1)")


def test_legacy_fields_are_refused_from_other_agents(tmp_path: Path) -> None:
    inst = _legacy_inst(tmp_path)
    fields = [("status", "live"), ("superseded_by", "clm-a"), ("resolution_source_id", "src-ec-plan"),
              ("resolution_locator", "basic")]
    for field, value in fields:
        op = _clm("clm-x", CTX, "128K", "The context window is 128K tokens", "src-legacy")
        op["row"][field] = value
        assert _err(_apply(inst, "bot", op)) == f"op 0 (add_claim): {field} is only for a legacy_import agent"
    op = _clm("clm-x", CTX, "128K", "The context window is 128K tokens", "src-legacy")
    op["row"].update(dict(fields))
    assert _err(_apply(inst, "bot", op)) == "op 0 (add_claim): status is only for a legacy_import agent"
    bare = _legacy("clm-x", CTX, "128K")
    assert _err(_apply(inst, "bot", bare)) == "op 0 (add_claim): quote is required to create"
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "128K", status="live", planned=True))) == (
        "op 0 (add_claim): send status or planned, not both")
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "128K", superseded_by="clm-a"))) == (
        "op 0 (add_claim): superseded_by needs status superseded")
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "128K", resolution_locator="basic"))) == (
        "op 0 (add_claim): resolution_source_id and resolution_locator need status rejected")
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "128K", status="rejected"))) == (
        "op 0 (add_claim): a rejected claim needs resolution_source_id and resolution_locator")
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "128K", status="rejected",
                                                 resolution_source_id="src-nope", resolution_locator="x"))) == (
        "op 0 (add_claim): resolution source src-nope does not exist")
    assert _err(_apply(inst, "importer", _legacy("clm-x", CTX, "128K", status="planned", supersedes=["clm-a"]))) == (
        "op 0 (add_claim): only a live claim can supersede")
    assert _counts(inst) == (0, 0, 0)


def test_verify_claim_checks_an_unchecked_claim(tmp_path: Path, clock: Clock) -> None:
    inst = _legacy_inst(tmp_path)
    _ok(_apply(inst, "importer", _legacy("clm-l1", CTX, "128K"), _legacy("clm-l2", CTX, "256K", status="planned",
                                                                          locator="lg2")))
    before = _fact(inst, CTX)
    clock.at("2026-10-09T02:00:00Z")
    verified = _ok(_apply(inst, "checker", _verify("clm-l1", "The context window is 128K tokens")))
    copy_id = verified_id("clm-l1", "src-legacy", "lg", "The context window is 128K tokens")
    claims = _claims(inst, CTX)
    copy = claims[copy_id]
    assert (claims["clm-l1"]["status"], claims["clm-l1"]["superseded_by"]) == ("superseded", copy_id)
    assert (copy["value_int"], copy["value_raw"], copy["status"], copy["proposed_by"], copy["checked"]) == (
        128000, "128K", "live", "importer", 1)
    assert copy["created_by"] == f"checker:{verified.receipt['change_id']}"
    assert copy["quote"] == "The context window is 128K tokens" and copy["source_id"] == "src-legacy"
    assert verified.receipt["targets"][:2] == [{"table": "claims", "id": copy_id, "action": "insert"},
                                               {"table": "claims", "id": "clm-l1", "action": "update"}]
    after = _fact(inst, CTX)
    assert (after["value_int"], after["status"]) == (before["value_int"], "current")
    # A verification checks the quote, not that the value is still current, so staleness keeps counting
    # from the import.
    assert after["last_verified_at"] == before["last_verified_at"] != "2026-10-09T02:00:00Z"
    assert not any(line.startswith("Changed in") for line in _brief(inst))
    shown = _read(inst, "get", entity="claim", id=copy_id).structured["row"]
    assert shown["created_by"].startswith("checker:") and shown["proposed_by"] == "importer"
    _ok(_apply(inst, "checker", _verify("clm-l2", "Planned: 256K tokens", locator="lg2")))
    planned = [claim for claim in _claims(inst, CTX).values() if claim["status"] == "planned"]
    assert [(claim["checked"], claim["value_int"]) for claim in planned] == [(1, 256000)]
    rerun = _ok(_apply(inst, "checker", _verify("clm-l1", "The context window is 128K tokens")))
    assert rerun.receipt["targets"] == []
    # After verifying, the writer's correction naming the original id supersedes the copy.
    clock.at("2026-10-09T03:00:00Z")
    _ok(_apply(inst, "importer", _legacy("clm-l3", CTX, "100K", locator="lg3", supersedes=["clm-l1"])))
    claims = _claims(inst, CTX)
    assert (claims[copy_id]["status"], claims[copy_id]["superseded_by"]) == ("superseded", "clm-l3")
    assert (_fact(inst, CTX)["status"], _fact(inst, CTX)["value_int"]) == ("current", 100000)
    assert _conflicts(inst, CTX) == []
    # A claim migrated by 0005 verifies too.
    from brain import migrate
    from tests.test_migrate import _v4_config, v4_store

    old = v4_store(tmp_path / "v4", _v4_config())
    assert migrate.migrate(old / "brain.sqlite3") == (4, migrate.latest_version())
    text = (old / "brain.toml").read_text(encoding="utf-8")
    (old / "brain.toml").write_text(text.replace(TRUTH_BLOCK, '\n[agents.checker]\nkind = "agent"\n'
                                                              'operations = ["verify_claim"]\n' + TRUTH_BLOCK),
                                    encoding="utf-8")
    migrated = next(cid for cid in _claims(old, "demo.seats") if cid.startswith("clm-m5-"))
    source = _claims(old, "demo.seats")[migrated]
    seats_quote = "The current version is 1.3"  # src-old holds no seat count, so C4 refuses it
    refused = _apply(old, "checker", _verify(migrated, seats_quote, source="src-old", locator=source["locator"]))
    assert _err(refused) == "op 0 (verify_claim): value 12 is not in the quote"
    motto = next(cid for cid in _claims(old, "demo.motto") if cid.startswith("clm-m5-"))
    done = _ok(_apply(old, "checker", _verify(motto, "The motto is small steps for the whole team", source="src-old",
                                              locator="synthetic")))
    assert any(item["action"] == "insert" for item in done.receipt["targets"])
    assert _claims(old, "demo.motto")[motto]["status"] == "superseded"


def test_verify_claim_refusals(tmp_path: Path) -> None:
    agents = {**STRICT_AGENTS, "scopedchecker": {"kind": "agent", "operations": ["verify_claim"],
                                                 "scope": {"fact_prefixes": ["exunits"]}}}
    inst = _inst(tmp_path, strict_config(agents=agents))
    _ok(_apply(inst, "alex", _src("src-legacy", LEGACY, title="Legacy import")))
    _ok(_apply(inst, "importer", _legacy("clm-l1", CTX, "128K"), _legacy("clm-l2", CTX, "64K", status="superseded",
                                                                          locator="lg2")))
    _ok(_apply(inst, "notes", _clm("clm-checked", "exunits.seats", "40", "We have 40 seats", "src-legacy")))
    cases = [
        (_verify("clm-nope", "The context window is 128K tokens"),
         "op 0 (verify_claim): claim clm-nope does not exist", "checker"),
        (_verify("clm-l1", "The context window is 128K tokens"),
         "op 0 (verify_claim): fact key excloud.max_context is outside agent scopedchecker's fact prefixes [exunits]",
         "scopedchecker"),
        (_verify("clm-l1", "Context window: 128K tokens", source="src-ec-limits"),
         "op 0 (verify_claim): claim clm-l1 cites src-legacy; quote that source", "checker"),
        (_verify("clm-l1", "Context window: 128K tokens", source="src-ec-plan"),
         "op 0 (verify_claim): claim clm-l1 cites src-legacy; quote that source", "checker"),
        (_verify("clm-l2", "It was 64K tokens once", locator="lg2"),
         "op 0 (verify_claim): claim clm-l2 is superseded; only a live or planned claim can be verified", "checker"),
        (_verify("clm-checked", "We have 40 seats"),
         "op 0 (verify_claim): claim clm-checked is already checked", "checker"),
        (_verify("clm-l1", "The window is 128K tokens"),
         "op 0 (verify_claim): quote is not in source src-legacy", "checker"),
        (_verify("clm-l1", "Planned: 256K tokens"),
         "op 0 (verify_claim): value 128K is not in the quote", "checker"),
    ]
    for op, message, agent in cases:
        assert _err(_apply(inst, agent, op)) == message
    # A whitespace-only locator is refused by the envelope of every op that cites into claims.
    blank = "packet is invalid: /ops/0/source/locator does not match the required pattern"
    for agent, op in (("checker", _verify("clm-l1", "The context window is 128K tokens", locator="  ")),
                      ("notes", _clm("clm-blank", CTX, "128K", "The context window is 128K tokens", "src-legacy",
                                     locator=" ")),
                      ("alex", _resolve(CTX, ["clm-l1"], [], "the context window is 128K tokens", "src-legacy",
                                        locator="\t"))):
        assert _err(_apply(inst, agent, op)) == blank
    # E39: a rejected claim with the copy's key, scope, source, locator and value.
    _ok(_apply(inst, "bot", _clm("clm-bot", CTX, "100K", "Basic gets 100K tokens", "src-legacy", locator="v9")))
    _ok(_apply(inst, "alex", _src("src-alex-ctx", "Alex: the limits page is right, the context window is 128K tokens.",
                                  kind="user_note", author="per-alex-example"),
               _resolve(CTX, ["clm-l1"], ["clm-bot"], "the limits page is right, the context window is 128K tokens",
                        "src-alex-ctx")))
    _ok(_apply(inst, "importer", _legacy("clm-l3", CTX, "100K", locator="lg3", status="planned")))
    e39 = _apply(inst, "checker", _verify("clm-l3", "Basic gets 100K tokens", locator="v9"))
    assert _err(e39) == ("op 0 (verify_claim): a claim with this key, scope, source, locator and value was rejected "
                         "(clm-bot)")


# -- proposed actions and the open filter ------------------------------------------------------------


def test_proposed_actions_are_not_open_work(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    plain = [line for line in _brief(inst) if line.startswith("Actions: ")]
    assert plain == ["Actions: 0 open, 0 waiting, 0 blocked. Overdue: none."]
    _ok(_apply(inst, "alex", _src("src-tomorrow", "Plan: book the room tomorrow.", date="2026-10-09")))
    _ok(_apply(inst, "notes",
               upsert_op("upsert_project", {"id": "proj-room", "kind": "project", "title": "Room booking",
                                            "status": "active"}, "src-tomorrow"),
               _action_op("act-p1", "src-tomorrow", status="proposed", due_date="2026-10-10", due_kind="deadline"),
               _action_op("act-p2", "src-tomorrow", status="proposed", project_id="proj-room"),
               _action_op("act-o1", "src-tomorrow", project_id="proj-room")))
    lines = _brief(inst)
    # A proposed action due yesterday is not open work, so nothing is overdue.
    assert "Actions: 1 open, 0 waiting, 0 blocked, 2 proposed. Overdue: none." in lines
    due = [line for line in lines if line.startswith("Due by ") or line.startswith("- act-")]
    assert due == ["Due by 2026-10-16: nothing open."]
    assert any(line.startswith("- proj-room: Room booking. No checklist. 1 open action.") for line in lines)
    project = _read(inst, "get", entity="project", id="proj-room").structured["related"]
    assert project["counts"]["open_actions"] == 1
    assert project["progress"]["actions"]["open"] == 1
    assert project["progress"]["actions"]["by_status"]["proposed"] == 1
    assert project["progress"]["next_due"] == []


def test_records_query_open_filter(tmp_path: Path) -> None:
    inst = _inst(tmp_path)
    for status in schemas.ACTION_STATUSES:
        extra = {"closed_at": "2026-10-02T00:00:00Z"} if status in ("done", "dropped") else {}
        _ok(_apply(inst, "notes", _action_op(f"act-{status}", "src-ec-limits", status=status, **extra)))
    opened = _read(inst, "records_query", entity="action", filters={"open": True}, fields=["status"]).structured
    assert sorted(row["status"] for row in opened["rows"]) == sorted(reads.OPEN_ACTION_STATUSES)
    assert set(reads.OPEN_ACTION_STATUSES) == {"open", "prepared", "scheduled", "sent", "waiting", "blocked"}
    closed = _read(inst, "records_query", entity="action", filters={"open": False}, fields=["status"]).structured
    assert sorted(row["status"] for row in closed["rows"]) == ["done", "dropped", "parked", "proposed"]
    projected = _read(inst, "records_query", entity="action", fields=["id", "open"], limit=100).structured
    assert {row["id"]: row["open"] for row in projected["rows"]} == {
        f"act-{status}": status in reads.OPEN_ACTION_STATUSES for status in schemas.ACTION_STATUSES}
    assert all(type(row["open"]) is bool for row in projected["rows"])
    refused = _read(inst, "records_query", entity="action", filters={"open": {"is_null": True}})
    assert refused.is_error and json.loads(refused.text)["error"]["message"] == advice.operator_error(
        "is_null", "bool", ["eq"]
    )


def test_tools_list_bytes() -> None:
    compact = json.dumps({"tools": reads.TOOLS}, ensure_ascii=False, separators=(",", ":")).encode()
    pins = tools_list_budgets()
    assert len(compact) == pins["tools_list"]
    entry = next(tool for tool in reads.TOOLS if tool["name"] == "records_query")
    assert len(json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode()) == pins["tools"]["records_query"]
    assert entry["description"].endswith(" For actions, open: true matches every open status.")


# -- never raise ------------------------------------------------------------------------------------

FZ_RAWS = ["128K", "128000", "131,072", "80 GB", "64K", "256K", "100K", "1.3", "1-3", "true", "false", "yes",
           "included", "not included", "professional", "pro", "basic", "eu", "EU", "au", "2026-09-14",
           "14 September 2026", "14/09/2026", "09/14/2026", "today", "tomorrow", "Friday", "40", "12", "9" * 30, "x",
           "1e-07", "45%", "A$5", "https://x.example/a"]
FZ_DATES = "Due 14 September 2026. Ship by Friday. Call tomorrow. We met on 2 October 2026, or 01/09/2026. Today."
FZ_KEYS = [item["key"] for item in KEYS] + ["exbasic.sso", "exbasic.tier"]
FZ_TYPES = {**{item["key"]: item["value_type"] for item in KEYS}, "exbasic.sso": "bool", "exbasic.tier": "enum"}
FZ_VALUES = {
    "int": [128000, 40, 12, 80, 0], "number": [49.5, 0.1, 5.0], "text": ["1.3", "1-3", "x", "Seed"],
    "bool": [True, False], "date": ["2026-09-14", "2026-10-01", "2026-10-02"], "url": ["https://x.example/a"],
    "enum": ["eu", "EU", "au", "pro", "basic", "us", "eu."],
}
FZ_NOTE_SIZES = [0, 12, 499, 500, 501, 1500, 2000]


def _fz_text(rng: random.Random) -> str:
    size = rng.choice(FZ_NOTE_SIZES)
    return ("Note text " * 300)[:size]


def _fuzz(tmp_path: Path, seed: int, cases: int = 1200) -> dict:
    from datetime import datetime, timedelta

    from brain import extract as extract_mod

    rng = random.Random(seed)
    inst = _inst(tmp_path)
    quotes = {raw: f"the value reads {raw} in this line" for raw in FZ_RAWS}
    content = " ".join(f"{quote}." for quote in quotes.values()) + " " + FZ_DATES
    _ok(_apply(inst, "alex", _src("src-fz", content, date="2026-10-01"),
               _src("src-fz-alex", "Alex says the context window is 128K tokens, and Alex means it.", kind="user_note",
                    author="per-alex-example")))
    config = (inst / "brain.toml").read_text(encoding="utf-8")
    plain = Applier(inst)
    insert = plain._insert_claim
    clock_start = datetime(2026, 10, 9)
    statuses: Counter = Counter()
    reached: Counter = Counter()
    failures: list[str] = []

    def stamp(i: int) -> str:
        return (clock_start + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def note(outcome, ops) -> None:
        if outcome.exit_code not in (0, 1, 3, 4):
            failures.append(f"exit {outcome.exit_code}: {outcome.integrity} <- {json.dumps(ops)[:300]}")
        if (inst / "PAUSED").exists():
            failures.append("paused: " + (inst / "PAUSED").read_text())
        statuses[outcome.receipt["status"]] += 1
        if outcome.receipt["status"] == "held" and outcome.warning.startswith("op 0 (apply_candidate): mass-change guard"):
            reached["guard-held"] += 1

    def send(agent: str, *ops) -> object:
        packet = make_packet(agent, *ops)
        try:
            outcome = plain.apply(json.dumps(packet).encode())
        except Exception as exc:  # the property: nothing escapes
            failures.append(f"{type(exc).__name__}: {exc} <- {json.dumps(ops)[:300]}")
            return None
        note(outcome, ops)
        return outcome

    def rows(sql, params=()):
        return ro_rows(inst, sql, params)

    original = store.utc_now
    try:
        for i in range(cases):
            store.utc_now = (lambda value: (lambda: value))(stamp(i))
            if i % 300 == 150:  # notes off for a stretch, then on again
                (inst / "brain.toml").write_text(config.replace(TRUTH_BLOCK, "\n[writes]\nnotes = false\n" + TRUTH_BLOCK),
                                                 encoding="utf-8")
            elif i % 300 == 225:
                (inst / "brain.toml").write_text(config, encoding="utf-8")
            roll = rng.random()
            agent = rng.choice(["notes", "bot", "solo", "importer"])
            key = rng.choice(FZ_KEYS)
            claim_ids = [row[0] for row in rows("SELECT id FROM claims ORDER BY id")]
            if roll < 0.16:
                value = rng.choice(FZ_VALUES[FZ_TYPES[key]])
                raw = rng.choice([units.render(FZ_TYPES[key], value)] * 3 + FZ_RAWS)
                row = {"key": key, "basis": "observed", "as_of": "2026-10-01", "last_verified_at": stamp(i)}
                if rng.random() < 0.9:
                    row["value"] = value
                if rng.random() < 0.9:
                    row["value_raw"] = raw
                if rng.random() < 0.9:
                    row["quote"] = quotes.get(raw, "the value reads nothing in this line")
                if rng.random() < 0.08:
                    row["status"] = rng.choice(["retired", "current"])
                base = _base(inst, "facts", "key", key)
                send(agent, upsert_op("upsert_fact", row, rng.choice(["src-fz", "src-ec-limits"]), base=base))
            elif roll < 0.28:
                kind = rng.choice(["action", "record", "project", "checklist", "decision"])
                text = _fz_text(rng)
                if kind == "action":
                    aid = rng.choice(["act-fz1", "act-fz2", f"act-fz{i}"])
                    op = _action_op(aid, "src-fz", base=_base(inst, "actions", "id", aid),
                                    **{rng.choice(["detail", "next_step", "close_reason"]): text})
                elif kind == "record":
                    rid = rng.choice(["rec-fz1", f"rec-fz{i}"])
                    op = _record_op(rid, text or "x", "src-fz", base=_base(inst, "records", "id", rid), title="Fuzz")
                elif kind == "project":
                    pid = rng.choice(["proj-fz", f"proj-fz{i}"])
                    row = {"id": pid, rng.choice(["description", "goal"]): text}
                    if _base(inst, "projects", "id", pid) is None:
                        row.update({"kind": "project", "title": "Fuzz project"})
                    op = upsert_op("upsert_project", row, "src-fz", base=_base(inst, "projects", "id", pid))
                elif kind == "checklist":
                    op = upsert_op("upsert_checklist_item", {"id": f"chk-fz{i}", "project_id": "general", "title": "Fz",
                                                             "state": "not_started", "state_basis": "reported",
                                                             "last_verified_at": stamp(i), "detail": text}, "src-fz")
                else:
                    op = upsert_op("upsert_decision", {"id": f"dec-fz{i}", "title": "Fuzz decision", "detail": text,
                                                       "quote": "Ship by Friday. Call tomorrow. We met",
                                                       "decided_on": "2026-10-01", "decided_by_person_id": "per-sam-example",
                                                       "status": "in_effect", "project_id": "general"}, "src-fz")
                send(agent, op)
            elif roll < 0.38:
                date = rng.choice(["2026-10-01", "2026-09-14", "2026-10-02", "2026-09-01", "2026-11-30", "2026-10-02",
                                   "2026-09-30"])
                if rng.random() < 0.7:
                    aid = rng.choice(["act-dt1", f"act-dt{i}"])
                    field = rng.choice(["due_date", "asked_on"])
                    fields = {field: date}
                    if field == "due_date":
                        fields["due_kind"] = "deadline"
                    send(agent, _action_op(aid, "src-fz", base=_base(inst, "actions", "id", aid), **fields))
                else:
                    send(agent, upsert_op("upsert_decision", {
                        "id": f"dec-dt{i}", "title": "Dated decision", "quote": "Ship by Friday. Call tomorrow. We met",
                        "decided_on": date, "decided_by_person_id": "per-sam-example", "status": "in_effect",
                        "project_id": "general"}, "src-fz"))
            elif roll < 0.50:
                typed_raws = [units.render(FZ_TYPES[key], value) for value in FZ_VALUES[FZ_TYPES[key]]]
                raw = rng.choice(typed_raws) if rng.random() < 0.6 else rng.choice(FZ_RAWS)
                status = rng.choice(["live", "planned", "superseded", "rejected", None])
                row = {"id": f"clm-l{i}", "key": key, "value_raw": raw, "basis": "reported"}
                if status:
                    row["status"] = status
                if rng.random() < 0.3 and raw in quotes:
                    row["quote"] = quotes[raw]
                ops = [{"op": "add_claim", "row": row, "source": {"source_id": "src-fz", "locator": "lg"}}]
                if status == "superseded":
                    others = ["clm-nope", f"clm-l{i}"] + claim_ids[-5:]
                    target = f"clm-l{i}b" if rng.random() < 0.7 else rng.choice(others)
                    row["superseded_by"] = target
                    ops.append({"op": "add_claim", "row": {"id": f"clm-l{i}b", "key": key, "value_raw": raw,
                                                           "basis": "reported"},
                                "source": {"source_id": "src-fz", "locator": "lg2"}})
                if status == "rejected" and rng.random() < 0.9:
                    row.update({"resolution_source_id": "src-fz-alex", "resolution_locator": "r1"})
                outcome = send("importer", *ops)
                if outcome is not None and outcome.receipt["status"] == "applied" and "superseded_by" in row:
                    found = rows("SELECT superseded_by FROM claims WHERE id = ?", (row["id"],))
                    if found and found[0][0] == row["superseded_by"]:
                        reached["linked"] += 1
            elif roll < 0.58:
                unchecked = rows("SELECT id, source_id, locator, value_raw FROM claims WHERE checked = 0 ORDER BY id")
                pick = rng.choice(unchecked) if unchecked else ("clm-nope", "src-fz", "lg", "x")
                quote = quotes.get(pick[3], "the value reads nothing in this line")
                # A blank locator is refused by the envelope.
                op = _verify(pick[0], quote, source=pick[1] if rng.random() < 0.9 else "src-ec-plan",
                             locator=rng.choice([pick[2]] * 7 + ["v2", " ", " "]))
                outcome = send(rng.choice(["checker", "notes"]), op)
                if outcome is not None and any(item["action"] == "insert" for item in outcome.receipt["targets"]):
                    reached["verified"] += 1
            elif roll < 0.86:
                extractor = rng.choice(["scan", "scan", "scan", "careful"])
                pick = rng.random()
                if pick < 0.55:
                    doc = _passing(f"fz{i}")
                    if rng.random() < 0.2:
                        doc["fields"]["value_raw"] = rng.choice(["256K", "64K", "80 GB", "100K"])
                    if rng.random() < 0.1:
                        doc["fields"]["supersedes"] = [rng.choice(claim_ids)] if claim_ids else ["clm-nope"]
                elif pick < 0.75:
                    doc = _action_doc(f"Fuzz action {i % 7}", basis=rng.choice(["reported", "inferred"]),
                                      detail=_fz_text(rng) if rng.random() < 0.5 else None)
                elif pick < 0.85:
                    doc = _decision_doc(f"Fuzz decision {i % 5}", detail=_fz_text(rng) if rng.random() < 0.5 else None)
                else:
                    doc = _claim_doc(rng.choice([KEY_WORDS, "Example Cloud unicorn power", "exbasic sso"]),
                                     rng.choice(FZ_RAWS), rng.choice([GOOD_QUOTE, "The context window is 128K tokens on "
                                                                                  "the Max plan"]), locator=f"zz{i}")
                    if rng.random() < 0.3:
                        doc["candidate_version"] = 1
                try:
                    result = extract_mod.run(inst, extractor, store.canonical_json(doc).encode())
                except Exception as exc:
                    if type(exc).__name__ == "Unreadable":
                        continue
                    failures.append(f"extract {type(exc).__name__}: {exc} <- {json.dumps(doc)[:300]}")
                    continue
                statuses[result.payload["receipt"]["status"] if "receipt" in result.payload else "invalid"] += 1
                if any(warning.startswith("op 0 (apply_candidate): mass-change guard") for warning in result.warnings):
                    reached["guard-held"] += 1
                if (inst / "PAUSED").exists():
                    failures.append("paused after extract")
            else:
                held = rows("SELECT change_id FROM write_log WHERE status = 'held' ORDER BY change_id")
                if not held:
                    continue
                change_id = rng.choice(held)[0]
                via = rng.choice(["terminal", "agent-relay"])
                try:
                    if rng.random() < 0.7:
                        outcome = plain.approve(change_id, "alex", via=via)
                    else:
                        outcome = plain.refuse(change_id, "alex", "not this one", via=via)
                except NotAllowed:
                    continue
                except Exception as exc:
                    failures.append(f"review {type(exc).__name__}: {exc}")
                    continue
                note(outcome, [change_id])
                reached["reviewed"] += 1
        # D8's coverage, now that the envelope refuses a blank locator: a duplicate-id insert of a
        # verified copy reaches verify_claim's IntegrityError catch, which refuses it.
        store.utc_now = lambda: stamp(cases)
        send("importer", _legacy("clm-fz-tail", CTX, "256K", source="src-fz", locator="tail", status="planned"))
        plain._insert_claim = lambda columns: (insert(columns), insert(columns))
        try:
            twice = send("checker", _verify("clm-fz-tail", quotes["256K"], source="src-fz", locator="tail"))
        finally:
            del plain._insert_claim
        if twice is not None and twice.receipt["error"] == "op 0 (verify_claim): " + database_refusal("UNIQUE constraint failed: claims.id", "claims"):
            reached["integrity"] += 1
    finally:
        store.utc_now = original
        plain.close()
    assert not failures, f"{len(failures)} failures; first: " + " | ".join(failures[:3])
    con = store.connect_readonly(inst / "brain.sqlite3")
    try:
        assert store.integrity_problems(con) == []
    finally:
        con.close()
    assert not (inst / "PAUSED").exists()
    vias = {row[0] for row in rows("SELECT approved_via FROM write_log WHERE reviewed_by <> ''")}
    return {"statuses": dict(statuses), "reached": dict(reached), "vias": vias}


@pytest.mark.parametrize("seed", [1502, 7, 42, 2026])
def test_strict_paths_never_raise_under_a_stateful_fuzz(tmp_path: Path, seed: int) -> None:
    result = _fuzz(tmp_path, seed)
    statuses, reached = result["statuses"], result["reached"]
    print(f"seed {seed}: {result}")
    assert {"applied", "rejected", "held"} <= set(statuses), result
    for outcome in ("guard-held", "verified", "linked", "reviewed", "integrity"):
        assert reached.get(outcome, 0) > 0, (outcome, result)
    assert result["vias"] == {"terminal", "agent-relay"}, result


def test_version_and_schema() -> None:
    code, out, err = _cli("--version")
    assert (code, out.strip(), err) == (0, version_banner(), "")
    assert len(render_router("demo").encode()) == router_bytes("demo")
    assert len(render_router("a" * 40).encode()) == router_bytes("a" * 40)


# -- dates written in words ----------------------------------------------------------------------

_STATED = [
    ("Due 14 September 2026.", "2026-10-01", True, {"2026-09-14"}),
    ("Due 14 Sep 2026.", "2026-10-01", True, {"2026-09-14"}),
    ("due Sept 14, 2026", "2026-10-01", True, {"2026-09-14"}),
    ("due 14th of September 2026", "2026-10-01", True, {"2026-09-14"}),
    ("Monday 14 September", "2026-10-01", True, {"2026-09-14"}),
    ("Mon, 14 Sep", "2026-10-01", True, {"2026-09-14"}),
    ("Tuesday 14 September", "2026-10-01", True, set()),
    ("by 20 October", "2026-10-01", True, {"2026-10-20"}),
    ("by 20 March", "2026-10-01", True, {"2027-03-20"}),
    ("on 1 April", "2026-10-01", True, {"2026-04-01"}),
    ("on 31 March", "2026-10-01", True, {"2027-03-31"}),
    ("14/09/2026", "2026-10-01", True, {"2026-09-14"}),
    ("12/10/2026", "2026-10-01", True, {"2026-10-12"}),
    ("09/14/2026", "2026-10-01", True, set()),
    ("31 September 2026", "2026-10-01", True, set()),
    ("29 February", "2026-10-01", True, set()),
    ("29 February", "2027-10-01", True, {"2028-02-29"}),
    # The forward roll compares (year, month, day) with no capping, so the coming leap day reads.
    ("29 February", "2027-08-31", True, {"2028-02-29"}),
    ("29 February", "2027-08-30", True, {"2028-02-29"}),
    ("29 February", "2027-08-29", True, set()),  # exactly six months back: no roll, and 2027 has none
    ("28 February", "2027-08-29", True, {"2028-02-28"}),
    ("decided on 20 December", "2027-01-05", False, {"2026-12-20"}),
    ("decided on 20 December", "2027-01-05", True, {"2027-12-20"}),
    ("14 September2026", "2026-10-01", True, set()),
    ("v114 September 2026", "2026-10-01", True, set()),
    ("Sept 2026 roadmap", "2026-10-01", True, set()),
    ("2026-09-14", "2026-10-01", True, set()),
    ("١٤ September 2026", "2026-10-01", True, set()),
    ("14 September 2026", "2026-10-01", True, {"2026-09-14"}),
]


def test_dates_written_in_words(tmp_path: Path) -> None:
    for text, source_date, forward, expected in _STATED:
        found = {item.isoformat() for item in units.stated_dates(text, source_date, forward=forward)}
        assert found == expected, (text, source_date, forward, found)
    assert units.stated_dates(None, "2026-10-01", forward=True) == frozenset()
    assert units.stated_dates("14 September 2026", "not a date", forward=True) == frozenset()
    # Through the applier: the demo's asked_on dates against its unchanged mail source.
    import shutil

    from tests.test_demo_instance import COPY_CONFIG, DEMO, STEPS, _run

    parent = tmp_path / "proof"
    parent.mkdir()
    demo = parent / "demo"
    for name, argv in STEPS:
        if argv is COPY_CONFIG:
            shutil.copyfile(DEMO / "demo.toml", demo / "brain.toml")
            continue
        code, out, err = _run([part.format(parent=parent, demo=demo, files=DEMO) for part in argv])
        assert code == 0, (name, out, err)
    mail = json.loads((DEMO / "packets" / "1-notes.json").read_text(encoding="utf-8"))
    triage = next(op["row"]["content"] for op in mail["ops"] if op["row"]["id"] == "src-demo-mail-triage")
    assert "2026-09-02" not in triage and "2026-09-14" not in triage
    assert "2 September 2026" in triage and "14 September 2026" in triage
    asked = {row[0] for row in ro_rows(demo, "SELECT asked_on FROM actions WHERE asked_on IS NOT NULL")}
    assert {"2026-09-02", "2026-09-14"} <= asked
    inst = _inst(tmp_path / "inst")
    _ok(_apply(inst, "alex", _src("src-board", "The board pack goes out on 20 October.", date="2026-10-01"),
               _src("src-jan", "Note from January: decided on 20 December to keep the plan.", date="2027-01-05"),
               _src("src-monday", "Plan: Monday 14 September is the cut-off for the venue.", date="2026-09-10"),
               _src("src-sep", "Note: the review was due 14/09/2026 at the latest.", date="2026-10-01")))
    _ok(_apply(inst, "notes", _action_op("act-board", "src-board", due_date="2026-10-20", due_kind="deadline")))
    _ok(_apply(inst, "notes", _action_op("act-jan", "src-jan", asked_on="2026-12-20")))
    refused = _apply(inst, "notes", _action_op("act-monday", "src-monday", due_date="2026-09-15", due_kind="deadline"))
    assert _err(refused) == "op 0 (upsert_action): due_date 2026-09-15 is neither the source date nor in the source"
    planned = candidates_plan(inst, _action_doc("Book the venue by the cut-off", due="2026-09-14",
                                                quote="Monday 14 September is the cut-off for the venue",
                                                source="src-monday"))
    assert planned.errors == () and planned.ops[0]["row"]["due_date"] == "2026-09-14"
    _ok(_apply(inst, "bot", _fact_op("exunits.launch", "2026-09-14", "src-sep", quote="the review was due 14/09/2026",
                                     raw="14 Sep 2026")))
    assert _fact(inst, "exunits.launch")["value_date"] == "2026-09-14"
    _ok(_apply(inst, "solo", _fact_op("exunits.launch", "2026-09-14", "src-sep", quote="the review was due 14/09/2026",
                                      raw="2026-09-14", base=_base(inst, "facts", "key", "exunits.launch"))))


def candidates_plan(inst: Path, doc: dict):
    from brain import candidates

    cfg = load_config(inst / "brain.toml")
    con = store.connect_reader(inst / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        return candidates.plan(con, cfg, doc, "2026-10-09T00:00:00Z")
    finally:
        con.close()
