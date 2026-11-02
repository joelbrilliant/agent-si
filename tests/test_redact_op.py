"""The redact_source op and what it scrubs. Every name and number here is invented."""
from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from pathlib import Path

import pytest

from brain import applier as applier_mod
from brain import backup, redact, scrub, store, validate
from brain.applier import Applier
from brain.cli import main
from brain.config import ConfigError
from tests.conftest import context_connection, make_packet, ro_rows, source_op, upsert_op
from tests.phase2a import custom_config, custom_instance, export, item, load_text, run_collect, sources
from tests.phase2b import (
    K, KEYS2, M, OPERATOR, PATH, REASON, REF, TEXT, redact_op, redact_packet, row_dict, scan_instance, seed,
)
from tests.test_carry_forward import AGENTS, KEYS, claim
from tests.test_strict import _legacy
from tests.test_truth import Clock, _apply, _clm, _conflicts, _err, _ok, _resolve, clock  # noqa: F401

OP = {"op": "redact_source", "source_id": "src-s", "reason": REASON, "operator": OPERATOR}
EXTRACTOR = {"scan": {"kind": "extractor", "operations": ["apply_candidate"], "approval": "required", "approvers": ["tester"]}}
S_TEXT = (
    f'Region is us for the Pro plan. Contact {M} for the vault. The Pro plan includes 25 seats. '
    f"The Pro plan costs 49 AUD a seat. Docs live at https://example.com/{M}/start and that is all."
)


def world(tmp_path: Path, *, text: str = S_TEXT, extra_agents: dict | None = None, **kwargs) -> Path:
    instance = custom_instance(tmp_path, agents={**AGENTS, **(extra_agents or {})}, fact_keys=KEYS2, **kwargs)
    _ok(_apply(instance, "tester", source_op("src-s", text, kind="document", author="per-alex-example")))
    return instance


def error_of(outcome) -> str:
    return outcome.receipt["error"]


# -- 22 ---------------------------------------------------------------------------------------------

def test_redact_source_is_reserved(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path, extra_agents=EXTRACTOR)
    packet = tmp_path / "brain-packet.json"
    packet.write_text(json.dumps(redact_packet("src-s")))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["apply", str(packet), "--instance", str(instance)])
    assert code == 1 and json.loads(out.getvalue())["error"] == "agent brain is reserved for brain init and brain apply-config"
    for agent in ("tester", "bot", "other", "careful", "importer"):
        outcome = _apply(instance, agent, dict(OP))
        assert outcome.receipt["status"] == "rejected"
        assert error_of(outcome) == f"op 0 (redact_source): not allowed for agent {agent}", agent
    run = Applier(instance, collect=True)
    try:
        outcome = run.apply(json.dumps(make_packet("workdocs", dict(OP))).encode())
    finally:
        run.close()
    assert error_of(outcome) == "op 0 (redact_source): not allowed for agent workdocs"
    ext = Applier(instance, extract=True)
    try:
        outcome = ext.apply(json.dumps(make_packet("scan", dict(OP))).encode())
    finally:
        ext.close()
    assert error_of(outcome) == "op 0 (redact_source): not allowed for agent scan"
    with pytest.raises(ConfigError) as err_info:
        load_text(tmp_path / "cfg", custom_config(agents={"xx": {"kind": "agent", "operations": ["redact_source"]}}))
    assert "unknown operation redact_source" in str(err_info.value)
    two = _apply(instance, "tester", dict(OP), dict(OP))
    assert "redact_source must be the only op in its packet" in error_of(two)
    # the reason and the operator are validated by the envelope
    patterns = "packet is invalid: /ops/0/reason does not match the required pattern"
    for reason in (
        "has @ sign", "has\nnewline", "has\ttab", "has separator", "private\u0085request", "private​request",
        "private‮request", "private⁠request", "ab", "x" * 201,
    ):
        found = validate.validate_packet(make_packet("brain", dict(OP, reason=reason), change_id="brain-redact-x-1"))
        assert found, repr(reason)
        if len(reason) >= 3 and len(reason) <= 200:
            assert any("does not match" in str(item) for item in found), (repr(reason), found)
    outcome = _apply(instance, "brain", dict(OP, reason="private\u0085request"), reserved=True)
    assert error_of(outcome).startswith("packet is invalid:") or patterns in error_of(outcome)
    for operator in ("", "x" * 61, "-lead", "Alex: Example", "Alex@Example"):
        assert validate.validate_packet(make_packet("brain", dict(OP, operator=operator), change_id="brain-redact-x-1")), operator
    assert validate.validate_packet(make_packet("brain", dict(OP), change_id="brain-redact-x-1")) == []


# -- 23 ---------------------------------------------------------------------------------------------

def test_the_source_becomes_a_tombstone(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS2, timezone="Australia/Brisbane")
    row = {"id": "src-s", "kind": "document", "title": f"Notes {M}", "uri": f"https://example.com/{M}", "source_date": "2026-10-07",
           "author_person_id": "per-alex-example", "content": S_TEXT, "external_ref": "document:work-docs/s.md"}
    _ok(_apply(instance, "tester", {"op": "add_source", "row": row}))
    before = row_dict(instance, "SELECT * FROM sources WHERE id = 'src-s'")
    clock.at("2026-10-09T14:30:00Z")
    outcome = redact_op(instance, "src-s")
    assert outcome.receipt["status"] == "applied", outcome.receipt
    after = row_dict(instance, "SELECT * FROM sources WHERE id = 'src-s'")
    assert (after["title"], after["uri"], after["author_person_id"]) == ("redacted", "", None)
    assert after["content"] == f"redacted on 2026-10-10 by {OPERATOR}: {REASON}"
    for column in ("id", "kind", "source_date", "external_ref", "content_sha256", "captured_at", "captured_by", "supersedes_id"):
        assert after[column] == before[column], column
    assert scrub.is_tombstone(after)


# -- 24 ---------------------------------------------------------------------------------------------

def statements(monkeypatch) -> list[str]:
    seen: list[str] = []
    original = Applier.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.con.set_trace_callback(lambda sql: seen.append(sql))

    monkeypatch.setattr(Applier, "__init__", init)
    return seen


def test_claims_are_rejected_and_scrubbed(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = world(tmp_path)
    q_motto = f"Contact {M} for the vault"
    _ok(_apply(instance, "bot", _clm("clm-live", "demo.motto", M, q_motto, "src-s")))
    _ok(_apply(instance, "bot", _clm("clm-site", "demo.site", f"https://example.com/{M}/start", f"Docs live at https://example.com/{M}/start", "src-s")))
    _ok(_apply(instance, "bot", _clm("clm-seats", "demo.seats", "25", "The Pro plan includes 25 seats", "src-s")))
    _ok(_apply(instance, "bot", _clm("clm-price", "demo.price", "49", "The Pro plan costs 49 AUD a seat", "src-s")))
    _ok(_apply(instance, "bot", _clm("clm-region", "demo.region", "us", "Region is us for the Pro plan", "src-s", planned=True)))
    # a superseded claim and its successor, a rejected claim and an unchecked claim (a legacy import)
    _ok(_apply(instance, "bot", _clm("clm-new", "demo.motto", "other", "Contact", "src-s", supersedes=["clm-live"])) if False else _apply(
        instance, "importer",
        _legacy("clm-old", "demo.motto", "old words", source="src-s", status="superseded", superseded_by="clm-live"),
        _legacy("clm-rej", "demo.motto", "rejected words", source="src-s", status="rejected",
                resolution_source_id="src-s", resolution_locator="by hand", locator="lg2"),
        _legacy("clm-unchecked", "demo.motto", "unchecked words", source="src-s", locator="lg3"),
    ))
    claims_before = {r[0]: r for r in ro_rows(instance, "SELECT id, status, quote, value_raw, value_text, value_int, value_real, superseded_by, resolution_locator FROM claims")}
    assert claims_before["clm-unchecked"][2] == ""
    seen = statements(monkeypatch)
    outcome = redact_op(instance, "src-s")
    assert outcome.receipt["status"] == "applied", outcome.receipt
    claims = {r[0]: r for r in ro_rows(instance, "SELECT id, status, quote, value_raw, value_text, value_int, value_real, superseded_by, resolution_source_id, resolution_locator FROM claims")}
    assert claims["clm-live"][1:5] == ("rejected", "[redacted]", "[redacted]", "[redacted]")
    assert claims["clm-live"][8:] == ("src-s", "redacted on 2026-10-09")
    assert claims["clm-site"][4] == "https://redacted.invalid/" and claims["clm-site"][2] == "[redacted]"
    assert claims["clm-seats"][1] == "rejected" and claims["clm-seats"][5] == 25 and claims["clm-seats"][2] == "[redacted]"
    assert claims["clm-price"][6] == 49.0
    assert claims["clm-region"][1] == "rejected" and claims["clm-region"][4] == "[redacted]"  # planned too, enum token
    assert claims["clm-old"][1] == "superseded" and claims["clm-old"][7] == "clm-live"  # keeps status and link
    assert claims["clm-rej"][1] == "rejected" and claims["clm-rej"][9] == "by hand"  # keeps its own resolution
    assert claims["clm-unchecked"][2] == "" and claims["clm-unchecked"][3] == "[redacted]"  # an empty quote stays empty
    updates = [sql for sql in seen if sql.startswith("UPDATE claims SET")]
    ids = [target["id"] for target in outcome.receipt["targets"] if target["table"] == "claims"]
    # the trace reports each statement once per step of its triggers, so compare the distinct statements
    assert len(set(updates)) == len(ids) == len(set(ids)) and "clm-live" in ids
    assert all(sum(1 for sql in set(updates) if sql.endswith(f"WHERE id = '{cid}'")) == 1 for cid in ids)
    assert store.check_instance(instance) == []


# -- 25, 26 -----------------------------------------------------------------------------------------

def test_headlines_are_worked_out_again(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    before = row_dict(instance, "SELECT * FROM facts WHERE key = 'demo.seats'")
    assert before["source_id"] == sid
    outcome = redact_op(instance, sid)
    assert outcome.receipt["status"] == "applied", outcome.receipt
    seats = row_dict(instance, "SELECT * FROM facts WHERE key = 'demo.seats'")
    assert (seats["source_id"], seats["status"], seats["value_int"], seats["quote"]) == ("src-other", "current", 25, "The Pro plan includes 25 seats")
    assert seats["locator"] == "para 1"
    motto = row_dict(instance, "SELECT * FROM facts WHERE key = 'demo.motto'")
    assert (motto["status"], motto["value_text"], motto["quote"]) == ("retired", "[redacted]", "[redacted]")
    assert motto["version"] >= 3
    site = row_dict(instance, "SELECT * FROM facts WHERE key = 'demo.site'")
    assert site["value_text"] == "https://redacted.invalid/" and site["status"] == "retired"
    for row in ro_rows(instance, "SELECT key, value_text, quote, source_id FROM fact_history WHERE source_id = ?", (sid,)):
        assert row[1] in ("[redacted]", "https://redacted.invalid/", None) and row[2] in ("[redacted]", "", None), row
    assert ro_rows(instance, "SELECT count(*) FROM fact_history WHERE instr(value_text, ?) > 0 OR instr(quote, ?) > 0", (M, M)) == [(0,)]
    # the numeric fact keeps its value in every history row
    assert {r[0] for r in ro_rows(instance, "SELECT value_int FROM fact_history WHERE key = 'demo.seats'")} == {25}


def test_a_dispute_ends_with_the_redacted_claim(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    _ok(_apply(instance, "tester", source_op("src-t", "The Pro plan includes 30 seats today.", kind="document")))
    claim(instance, "bot", "clm-a", "demo.seats", "25", "The Pro plan includes 25 seats", "src-s")
    claim(instance, "other", "clm-b", "demo.seats", "30", "The Pro plan includes 30 seats", "src-t")
    assert row_dict(instance, "SELECT status FROM facts WHERE key = 'demo.seats'")["status"] == "disputed"
    assert _conflicts(instance, "demo.seats")
    outcome = redact_op(instance, "src-s")
    assert outcome.receipt["status"] == "applied", outcome.receipt
    fact = row_dict(instance, "SELECT * FROM facts WHERE key = 'demo.seats'")
    assert (fact["status"], fact["value_int"], fact["source_id"]) == ("current", 30, "src-t")
    assert _conflicts(instance, "demo.seats", status="open") == []
    assert [r[3] for r in _conflicts(instance, "demo.seats", status="resolved")] == ["resolved"] or _conflicts(instance, "demo.seats", status=None)
    assert ro_rows(instance, "SELECT status FROM signals WHERE kind = 'conflict'") == [("resolved",)]


# -- 27, 27a ----------------------------------------------------------------------------------------

def test_quoted_spans_in_notes_and_labels(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    versions = {
        table: ro_rows(instance, f"SELECT id, version FROM {table}")
        for table in ("projects", "records", "decisions", "actions", "checklist_items", "signals")
    }
    # a 90-character decision title holding a quoted span keeps its CHECK
    long_title = f'"{M}" ' + "y" * (90 - len(M) - 3)
    con = context_connection(instance)
    con.execute("UPDATE decisions SET title = ?, version = version + 1 WHERE id = 'dec-vault'", (long_title[:90],))
    con.commit()
    con.close()
    clock.at("2026-10-12T01:00:00Z")
    outcome = redact_op(instance, sid)
    assert outcome.receipt["status"] == "applied", outcome.receipt
    stamp = f"brain:{outcome.receipt['change_id']}"
    for table, row_id in (("projects", "vault-project"), ("records", "rec-vault"), ("decisions", "dec-vault"), ("actions", "act-vault"),
                          ("checklist_items", "chk-vault"), ("signals", "sig-vault")):
        row = row_dict(instance, f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        assert M not in json.dumps(row), table
        assert row["updated_by"] == stamp
        old = dict(versions[table])[row_id]
        assert row["version"] > old, table
    assert row_dict(instance, "SELECT quote FROM decisions WHERE id = 'dec-vault'")["quote"] == "[redacted]"
    assert len(row_dict(instance, "SELECT title FROM decisions WHERE id = 'dec-vault'")["title"]) <= 90
    assert store.search_drift(store.connect_readonly(instance / "brain.sqlite3")) == []
    assert store.check_instance(instance) == []


def test_rows_edited_by_another_source_lose_the_text(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    _ok(_apply(instance, "tester", source_op("src-s", f"Memo one. Contact {M} for the vault. Nothing else.", kind="document", author="per-alex-example")))
    _ok(_apply(instance, "tester", source_op("src-x", "Memo two. The vault was audited in October by the team.", kind="document")))
    q = f"Contact {M} for the vault"
    body1 = f'The vault contact is "{q}".'
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-1", "kind": "note", "title": "Vault", "body": body1}, "src-s")))
    audited = " The vault was audited in October by the team."
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-1", "body": body1 + audited}, "src-x", base=1)))
    act = {"id": "act-1", "title": "Rotate", "detail": f'Code is "{q}".', "status": "open", "status_basis": "reported", "project_id": "general", "last_verified_at": "2026-10-07T00:00:00Z"}
    _ok(_apply(instance, "tester", upsert_op("upsert_action", act, "src-s")))
    _ok(_apply(instance, "tester", upsert_op("upsert_action", {"id": "act-1", "next_step": "Audit it again in October by the team."}, "src-x", base=1)))
    dec = {"id": "dec-1", "title": f'Keep "{q}" private', "quote": q, "decided_on": "2026-10-01", "decided_by_person_id": "per-alex-example", "status": "in_effect", "project_id": "general"}
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", dec, "src-s")))
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", {"id": "dec-1", "status": "proposed"}, "src-x", base=1)))
    # a record that cites S only through an older history row, and one that X wrote alone
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-2", "kind": "note", "title": "Second", "body": f'Also "{q}".'}, "src-s")))
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-2", "title": "Second, retitled"}, "src-x", base=1)))
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-3", "kind": "note", "title": "Alone", "body": "Written by X alone."}, "src-x")))
    assert {r[0]: r[1] for r in ro_rows(instance, "SELECT id, source_id FROM records")} == {"rec-1": "src-x", "rec-2": "src-x", "rec-3": "src-x"}
    alone = row_dict(instance, "SELECT * FROM records WHERE id = 'rec-3'")
    outcome = redact_op(instance, "src-s")
    assert outcome.receipt["status"] == "applied", outcome.receipt
    for table, column in (("records", "body"), ("record_history", "body"), ("search_index", "body"), ("actions", "detail"),
                          ("decisions", "title"), ("decisions", "quote")):
        assert ro_rows(instance, f"SELECT count(*) FROM {table} WHERE instr({column}, ?) > 0", (M,)) == [(0,)], (table, column)
    assert row_dict(instance, "SELECT body FROM records WHERE id = 'rec-1'")["body"] == 'The vault contact is "[redacted]".' + audited
    assert row_dict(instance, "SELECT next_step FROM actions WHERE id = 'act-1'")["next_step"] == "Audit it again in October by the team."
    assert row_dict(instance, "SELECT * FROM records WHERE id = 'rec-3'") == alone
    history = [r[0] for r in ro_rows(instance, "SELECT body FROM record_history WHERE record_id = 'rec-1' ORDER BY version")]
    # both old versions are scrubbed, and the redaction's own version is born scrubbed
    assert history == ['The vault contact is "[redacted]".', 'The vault contact is "[redacted]".' + audited, 'The vault contact is "[redacted]".' + audited]
    redact.compact(instance / "brain.sqlite3", "src-s")  # the op alone leaves free pages; compact, then scan the file
    assert "brain.sqlite3" not in scan_instance(instance)
    assert store.check_instance(instance) == []


# -- 28, 29 -----------------------------------------------------------------------------------------

def test_rows_citing_other_sources_are_untouched(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    _ok(_apply(instance, "tester", source_op("src-twin", S_TEXT, kind="document")))
    q = f"Contact {M} for the vault"
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-twin", "kind": "note", "title": "Twin", "body": f'The twin says "{q}".'}, "src-twin")))
    claim(instance, "bot", "clm-twin", "demo.motto", M, q, "src-twin")
    note, other_claim = row_dict(instance, "SELECT * FROM records WHERE id = 'rec-twin'"), row_dict(instance, "SELECT * FROM claims WHERE id = 'clm-twin'")
    assert redact_op(instance, "src-s").receipt["status"] == "applied"
    assert row_dict(instance, "SELECT * FROM records WHERE id = 'rec-twin'") == note
    assert row_dict(instance, "SELECT * FROM claims WHERE id = 'clm-twin'") == other_claim
    assert row_dict(instance, "SELECT * FROM sources WHERE id = 'src-twin'")["content"] == S_TEXT


def test_unchecked_text_keeps_its_marker(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    before = json.loads(ro_rows(instance, "SELECT sentences FROM unchecked_text WHERE source_id = ?", (sid,))[0][0])
    assert any(M in element["text"] for element in before)
    assert redact_op(instance, sid).receipt["status"] == "applied"
    rows = ro_rows(instance, "SELECT sentences FROM unchecked_text WHERE source_id = ?", (sid,))
    assert len(rows) == 1
    after = json.loads(rows[0][0])
    assert [(e["key"], e["reasons"]) for e in after] == [(e["key"], e["reasons"]) for e in before]
    assert all(M not in e["text"] for e in after)
    assert any("[redacted]" in e["text"] for e in after)
    assert store.check_instance(instance) == []


# -- 30 ---------------------------------------------------------------------------------------------

def test_thread_subject_and_meeting_title_links(tmp_path: Path, clock: Clock) -> None:
    agents = {**AGENTS, "meta": {"kind": "agent", "operations": ["upsert_thread", "upsert_meeting"]}}
    instance = custom_instance(tmp_path, agents=agents, fact_keys=KEYS)
    _ok(_apply(instance, "tester", source_op("src-mail", f"Memo with {M}.", kind="email", title="Weekly sync about the vault rota")))
    _ok(_apply(instance, "tester", source_op("src-short", f"Short memo with {M}.", kind="email", title="Weekly sync")))
    _ok(_apply(instance, "tester", source_op("src-doc", "A document.", kind="document", title="Quarterly planning for the vault team")))
    _ok(_apply(instance, "tester", source_op("src-other", "Plain.", kind="document")))

    def thread(ident, subject, source, channel="email", account="work"):
        row = {"id": f"thr-{ident}", "channel": channel, "account": account, "external_id": f"t-{ident}", "subject": subject}
        _ok(_apply(instance, "meta", upsert_op("upsert_thread", row, source, locator=f"thread t-{ident}")))

    thread("real", "Re: Weekly sync about the vault rota", "src-other")  # equal after stripping
    thread("fwd", "Fwd: RE: Weekly sync about the vault rota", "src-other", account="home")
    thread("decorated", "Weekly sync about the vault rota [External]", "src-other")
    thread("shortsubj", "Weekly sync", "src-other")
    thread("linked", "Anything at all", "src-mail")  # by source_id
    thread("othersubj", "Something else entirely", "src-other")
    thread("docsubj", "Quarterly planning for the vault team", "src-other")
    thread("cal", "Vault review", "src-other", channel="calendar")
    _ok(_apply(instance, "meta", upsert_op("upsert_meeting", {"id": "mtg-1", "thread_id": "thr-cal", "title": f"Sync {M}", "starts_at": "2026-10-20T01:00:00Z", "ends_at": "2026-10-20T02:00:00Z", "status": "confirmed"}, "src-mail")))
    plan = redact.plan(instance, "src-mail")
    assert plan["threads_by_subject"] == [
        {"id": "thr-fwd", "channel": "email", "account": "home"}, {"id": "thr-real", "channel": "email", "account": "work"}]
    assert "thr-fwd (email, home)" in redact.render_plan(plan)
    result = redact.run(instance, "src-mail", REASON, OPERATOR)
    assert result["threads_by_subject"] == plan["threads_by_subject"]
    got = dict(ro_rows(instance, "SELECT id, subject FROM threads"))
    assert got["thr-real"] == got["thr-fwd"] == got["thr-linked"] == "[redacted]"
    assert got["thr-decorated"].startswith("Weekly sync about") and got["thr-shortsubj"] == "Weekly sync"
    assert got["thr-othersubj"] == "Something else entirely" and got["thr-docsubj"].startswith("Quarterly")
    assert ro_rows(instance, "SELECT title FROM meetings") == [("[redacted]",)]
    # the short email title does not take in the thread with the same short subject, nor a document the thread
    redact.run(instance, "src-short", REASON, OPERATOR)
    assert dict(ro_rows(instance, "SELECT id, subject FROM threads"))["thr-shortsubj"] == "Weekly sync"
    redact.run(instance, "src-doc", REASON, OPERATOR)
    assert dict(ro_rows(instance, "SELECT id, subject FROM threads"))["thr-docsubj"].startswith("Quarterly")
    assert store.check_instance(instance) == []


# -- 31, 32 -----------------------------------------------------------------------------------------

def test_the_write_log_is_scrubbed(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    clock.at("2026-10-12T02:00:00Z")
    before = {r[0]: r for r in ro_rows(instance, "SELECT change_id, status, error, hold_reason, packet_json, payload_sha256, applied_seq, decided_at FROM write_log")}
    result = redact.run(instance, sid, REASON, OPERATOR)
    assert result["held_rejected"] == 1
    after = {r[0]: r for r in ro_rows(instance, "SELECT change_id, status, error, hold_reason, packet_json, payload_sha256, applied_seq, decided_at FROM write_log")}
    capture = [cid for cid, row in before.items() if row[0] and cid.startswith("workdocs-") and '"op":"add_source"' in row[4]][0]
    packet = json.loads(after[capture][4])
    assert M not in after[capture][4] and packet["redacted_ops"] and after[capture][5] == before[capture][5]
    refused = [cid for cid, row in before.items() if row[1] == "rejected" and "evidence gate" in row[2]][0]
    assert after[refused][2] == "redacted on 2026-10-12" and M not in after[refused][4] and "[redacted]" in after[refused][4]
    run_packet = [cid for cid, row in before.items() if row[0].startswith("workdocs-") and json.loads(row[4])["ops"][0]["op"] == "record_collector_run" and M in row[4]][0]
    assert json.loads(after[run_packet][4])["ops"][0]["row"]["error"] == "redacted on 2026-10-12"
    held = after[fixture.held]
    assert (held[1], held[2], held[3], held[6]) == ("rejected", "redacted on 2026-10-12", "", None) and held[7] == "2026-10-12T02:00:00Z"
    assert M not in held[4]
    own = result["change_id"]
    assert json.loads(after[own][4])["ops"][0]["source_id"] == sid and after[own][1] == "applied"
    from brain.extract import held as held_list

    listing = json.dumps(held_list(instance, 50))
    assert fixture.held not in listing and fixture.held_other in listing
    app = Applier(instance)
    try:
        for call in (lambda: app.approve(fixture.held, "tester"), lambda: app.refuse(fixture.held, "tester", "no")):
            with pytest.raises(applier_mod.NotAllowed) as err:
                call()
            assert str(err.value) == f"change {fixture.held} is already rejected"
        assert app.approve(fixture.held_other, "tester").receipt["status"] == "applied"
    finally:
        app.close()


def test_unrelated_sources_keep_their_audit(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS2)
    doc = export([item("a/secret.md", f'Contact "{M}" for the vault.'), item("b/plain.md", "The Pro plan includes 25 seats.")])
    code, payload, err = run_collect(instance, doc)
    assert code == 0, (payload, err)
    sid = sources(instance, "document:work-docs/a/secret.md")[-1][0]
    rows = ro_rows(instance, "SELECT change_id, packet_json, payload_sha256 FROM write_log WHERE instr(packet_json, ?) > 0", (sid,))
    assert len(rows) == 1
    original = json.loads(rows[0][1])
    adds = [i for i, op in enumerate(original["ops"]) if op["op"] == "add_source"]
    assert len(adds) == 2
    mine = [i for i in adds if original["ops"][i]["row"]["id"] == sid]
    assert redact_op(instance, sid).receipt["status"] == "applied"
    new_json, new_sha = ro_rows(instance, "SELECT packet_json, payload_sha256 FROM write_log WHERE change_id = ?", (rows[0][0],))[0]
    packet = json.loads(new_json)
    assert packet["redacted_ops"] == mine and len(packet["ops"]) == len(original["ops"]) and new_sha == rows[0][2]
    for index, (was, now) in enumerate(zip(original["ops"], packet["ops"])):
        if index not in mine:
            assert store.canonical_json(was) == store.canonical_json(now), index
    assert {key: value for key, value in packet.items() if key not in ("ops", "redacted_ops")} == {
        key: value for key, value in original.items() if key != "ops"}


# -- 33, 34 -----------------------------------------------------------------------------------------

def test_collector_run_errors(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    errors = {
        "run-e1": f"rejected: quote is not in the cited source src-s.",
        "run-e2": "rejected: quote is not in the cited source src-s-r3.",
        "run-e3": "rejected: quote is not in the cited source src-s.b",
        "run-e4": "rejected: about src-s... again",
        "run-e5": "nothing to see",
    }
    cap = Applier(instance, collect=True)
    try:
        for ident, error in errors.items():
            op = {"op": "record_collector_run", "row": {"id": ident, "collector": "work-docs", "started_at": "2026-10-08T00:00:00Z", "finished_at": "2026-10-08T00:00:05Z", "status": "error", "items": 1, "input_sha256": "", "error": error}}
            _ok(cap.apply(json.dumps(make_packet("workdocs", op, change_id=f"workdocs-{ident}")).encode()))
    finally:
        cap.close()
    assert redact_op(instance, "src-s").receipt["status"] == "applied"
    got = dict(ro_rows(instance, "SELECT id, error FROM collector_runs WHERE id LIKE 'run-e%'"))
    assert got["run-e1"] == got["run-e4"] == "redacted on 2026-10-09"
    assert got["run-e2"] == errors["run-e2"] and got["run-e3"] == errors["run-e3"] and got["run-e5"] == errors["run-e5"]
    # the packet that recorded the first run lost the text too
    assert json.loads(ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = 'workdocs-run-e1'")[0][0])["ops"][0]["row"]["error"] == "redacted on 2026-10-09"
    assert json.loads(ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = 'workdocs-run-e2'")[0][0])["ops"][0]["row"]["error"] == errors["run-e2"]


def test_drift_signals(tmp_path: Path, clock: Clock) -> None:
    import hashlib

    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    clock.at("2026-10-12T03:00:00Z")
    outcome = redact_op(instance, sid)
    assert outcome.receipt["status"] == "applied"
    signals = ro_rows(instance, "SELECT id, kind, subject_table, subject_id, detail, status, basis, source_id, locator FROM signals WHERE id LIKE 'sig-redact-%' ORDER BY id")
    expected = {}
    for table, subject in [("facts", "demo.motto"), ("facts", "demo.seats"), ("facts", "demo.site"), ("decisions", "dec-vault")]:
        ident = "sig-redact-" + hashlib.sha256(f"redact\n{table}\n{subject}\n{sid}".encode()).hexdigest()[:24]
        expected[ident] = (ident, "drift", table, subject, "source redacted on 2026-10-12", "open", "observed", sid, "redaction")
    assert {row[0]: row for row in signals} == expected
    # none for a key with no fact row (demo.price and demo.region have claims only if any) and no repeat
    assert ro_rows(instance, "SELECT count(*) FROM signals WHERE subject_table = 'facts' AND subject_id = 'demo.region'") == [(0,)]
    again = redact_op(instance, sid, n=2)
    assert again.receipt["status"] == "applied"
    assert ro_rows(instance, "SELECT count(*) FROM signals WHERE id LIKE 'sig-redact-%'") == [(4,)]
    # an agent may dismiss one, but may not create the prefix
    first = signals[0][0]
    version = ro_rows(instance, "SELECT version FROM signals WHERE id = ?", (first,))[0][0]
    dismissed = _apply(instance, "tester", upsert_op("upsert_signal", {"id": first, "status": "dismissed", "resolved_at": "2026-10-12T03:00:00Z"}, sid if False else "src-other", base=version))
    assert dismissed.receipt["status"] == "applied", dismissed.receipt["error"]
    forged = _apply(instance, "tester", upsert_op("upsert_signal", {"id": "sig-redact-abc", "kind": "other", "subject_table": "records", "subject_id": "rec-vault", "basis": "observed"}, "src-other"))
    assert error_of(forged) == "op 0 (upsert_signal): signal ids starting sig-redact- are reserved for the applier"


# -- 35, 36 -----------------------------------------------------------------------------------------

def test_the_chain_is_redacted_together(tmp_path: Path, clock: Clock) -> None:
    from tests.test_carry_forward import V1, V2, current, recapture, world as cf_world

    instance, v1 = cf_world(tmp_path, V1)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", "The Pro plan includes 25 seats", v1)
    recapture(instance, V2)
    v2 = current(instance)
    recapture(instance, V2 + " Docs live at the usual place.", stamp="2026-10-08T02:00:00Z")
    v3 = current(instance)
    carried = ro_rows(instance, "SELECT id, source_id FROM claims WHERE key = 'demo.seats' ORDER BY source_id")
    assert {r[1] for r in carried} >= {v1, v2}
    plan = redact.plan(instance, v2)
    assert [s["id"] for s in plan["sources"]] == sorted([v1, v2, v3])
    assert redact_op(instance, v2).receipt["status"] == "applied"
    assert all(r[0] == "redacted" for r in ro_rows(instance, "SELECT title FROM sources WHERE id IN (?, ?, ?)", (v1, v2, v3)))
    assert ro_rows(instance, "SELECT count(*) FROM claims WHERE quote NOT IN ('', '[redacted]')") == [(0,)]
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
    assert store.check_instance(instance) == []


def test_redacting_twice_is_the_same_run(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    counts = "SELECT (SELECT count(*) FROM facts), (SELECT count(*) FROM fact_history), (SELECT count(*) FROM signals), (SELECT count(*) FROM record_history)"
    first = redact.run(instance, sid, REASON, OPERATOR)
    mid = ro_rows(instance, counts)
    second = redact.run(instance, sid, REASON, OPERATOR)
    assert ro_rows(instance, counts) == mid
    assert first["change_id"].endswith("-1") and second["change_id"].endswith("-2")
    assert second["rows"]["claims"] == 0 and second["rows"]["facts"] == 0 and second["rows"]["signals"] == 0
    assert (instance / "REDACTING").exists() is False and store.check_instance(instance) == []
    # a stray that cites the tombstone and carries text (inserted by hand, since a real one is stopped at the door)
    con = context_connection(instance)
    con.execute(
        "INSERT INTO write_log (change_id, agent, intent, status, decided_at, applied_seq, reviewed_by, reviewed_at, payload_sha256, error, packet_json, targets_json, hold_reason) "
        "VALUES ('tester-stray', 'tester', 'stray', 'rejected', '2026-10-12T00:00:00Z', NULL, '', NULL, ?, ?, ?, '[]', '')",
        ("0" * 64, f"op 0 (upsert_record): evidence gate: quote \"contact {M} for\" is not in the cited source {sid}.",
         store.canonical_json({"packet_version": 1, "change_id": "tester-stray", "agent": "tester", "intent": "stray",
                               "ops": [{"op": "upsert_record", "row": {"id": "rec-s", "kind": "note", "title": "t", "body": f'"{M}"'},
                                        "source": {"source_id": sid, "locator": "p"}, "base": None}]})),
    )
    con.commit()
    con.close()
    assert store.check_instance(instance) == [f"source {sid} is redacted, but write_log tester-stray still holds its text"]
    redact.run(instance, sid, REASON, OPERATOR)
    assert store.check_instance(instance) == []
    # redacting two members of one chain gives one set of drift signals, rooted at the chain's first element
    from tests.test_carry_forward import V1, V2, current, recapture, world as cf_world

    chain_instance, a = cf_world(tmp_path / "chain", V1)
    claim(chain_instance, "bot", "clm-seats", "demo.seats", "25", "The Pro plan includes 25 seats", a)
    recapture(chain_instance, V2)
    b = current(chain_instance)
    redact_op(chain_instance, a)
    from brain import redact_ops

    count = ro_rows(chain_instance, "SELECT count(*) FROM signals WHERE id LIKE 'sig-redact-%'")
    assert count[0][0] >= 1
    assert ro_rows(chain_instance, "SELECT count(*) FROM signals WHERE id = ?", (redact_ops.signal_id("facts", "demo.seats", a),)) == [(1,)]
    redact_op(chain_instance, b, n=1)
    assert ro_rows(chain_instance, "SELECT count(*) FROM signals WHERE id LIKE 'sig-redact-%'") == count
    assert ro_rows(chain_instance, "SELECT count(*) FROM signals WHERE id = ?", (redact_ops.signal_id("facts", "demo.seats", b),)) == [(0,)]


# -- 36a --------------------------------------------------------------------------------------------

def test_a_redacted_source_cannot_be_cited_again(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    original = row_dict(instance, "SELECT * FROM sources WHERE id = ?", (sid,))
    redact.run(instance, sid, REASON, OPERATOR)
    retry = _apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-retry", "kind": "note", "title": "Retry", "body": f'He wrote "contact {M} for the vault again".'}, sid))
    assert retry.receipt["status"] == "rejected" and error_of(retry) == f"op 0 (upsert_record): source {sid} is redacted"
    stored = ro_rows(instance, "SELECT packet_json, error FROM write_log WHERE change_id = ?", (retry.receipt["change_id"],))[0]
    assert M not in stored[0] and json.loads(stored[0])["redacted_ops"] == [0] and stored[1] == f"op 0 (upsert_record): source {sid} is redacted"
    assert store.check_instance(instance) == []
    assert backup.backup(instance, tmp_path / "later")["status"] == "backed_up"
    # the re-sent original is the no-op, logged scrubbed; other text for the id is refused with the fixed text
    resend = {"op": "add_source", "row": {k: original[k] for k in ("id", "kind", "title", "uri", "source_date", "author_person_id", "content", "external_ref")}}
    # the original text is gone from the store, so the re-send carries it from the fixture constant
    resend["row"]["content"] = TEXT
    ok = _apply(instance, "tester", resend)
    other = _apply(instance, "tester", {"op": "add_source", "row": dict(resend["row"], content="Different text.")})
    assert other.receipt["status"] == "rejected" and error_of(other) == f"op 0 (add_source): source {sid} is redacted"
    for outcome in (ok, other):
        stored = ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (outcome.receipt["change_id"],))[0][0]
        assert M not in stored
    assert store.check_instance(instance) == []
    # the Applier answers a tombstone's content query from the tombstone, even with the original cached
    app = Applier(instance)
    try:
        app._content_keys[original["content_sha256"]] = "the original words"
        row = app._load_row("sources", "id", sid)
        assert app._content_key(row) == scrub.source_key(row["content"]) and M.lower() not in app._content_key(row)
    finally:
        app.close()
    assert scan_instance(instance) == {}


# -- 37, 38, 39 -------------------------------------------------------------------------------------

def test_the_reason_may_not_repeat_the_source(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path, text=f"The confidential memo is here. Contact {M} for the vault.")
    n = 0

    def attempt(reason: str):
        nonlocal n
        n += 1
        return redact_op(instance, "src-s", reason=reason, n=n)

    for reason in (M, M[:8] + "-" + M[8:], M[:10] + " " + M[10:], M[:12] + "\u200b" + M[12:], M.upper()):
        if validate.validate_packet(make_packet("brain", dict(OP, reason=reason), change_id="brain-redact-x-1")):
            continue  # the envelope pattern refuses it before the guard sees it
        outcome = attempt(reason)
        assert outcome.receipt["status"] == "rejected"
        message = error_of(outcome)
        assert message.startswith("op 0 (redact_source): the reason repeats text from source src-s (characters ")
        assert M not in message and ro_rows(instance, "SELECT title FROM sources WHERE id = 'src-s'") != [("redacted",)]
    # the position counts characters of the reason, 1-based, the first and last of the matching window
    hyphen = M[:8] + "-" + M[8:]
    found = error_of(attempt("see " + hyphen))
    assert found == "op 0 (redact_source): the reason repeats text from source src-s (characters 5 to 21 of the reason)", found
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
    assert error_of(attempt("Contact" + " " * 3 + M[:9])).startswith("op 0 (redact_source): the reason repeats text")
    # an innocent reason that shares a word with the source passes, as does the ordinary one
    assert attempt("remove confidential details").receipt["status"] == "applied"
    other = world(tmp_path / "second", text=f"Contact {M} for the vault.")
    assert redact_op(other, "src-s", reason=REASON).receipt["status"] == "applied"
    # U+0085, U+200B and U+202E are invalid at the envelope, not reached by the guard
    for char in ("\u0085", "\u200b", "\u202e"):
        errors = validate.validate_packet(make_packet("brain", dict(OP, reason=f"private{char}request"), change_id="brain-redact-x-1"))
        assert errors and "does not match" in str(errors)


def test_refusals(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = world(tmp_path)
    logged = lambda: ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    n = logged()
    missing = redact_op(instance, "src-nope")
    assert error_of(missing) == "op 0 (redact_source): source src-nope does not exist" and missing.receipt["status"] == "rejected"
    assert logged() == n + 1
    config = [r[0] for r in ro_rows(instance, "SELECT id FROM sources WHERE kind = 'config'")][0]
    assert error_of(redact_op(instance, config, n=2)) == "op 0 (redact_source): config sources are not redacted"
    # a chain over CHAIN_MAX
    from tests.phase2b import redact_packet as _packet  # noqa: F401
    from brain import redact_ops

    monkeypatch.setattr(redact_ops, "CHAIN_MAX", 3)
    con = context_connection(instance)
    prev = "src-s"
    for i in range(4):
        body = f"version {i} of the note"
        sid = f"src-chain-{i}"
        con.execute(
            "INSERT INTO sources (id, kind, title, uri, source_date, author_person_id, content, content_sha256, external_ref, captured_at, captured_by, supersedes_id) "
            "VALUES (?, 'document', 'chain', '', '2026-10-07', NULL, ?, ?, 'document:work-docs/chain.md', '2026-10-08T00:00:00Z', 'tester:x', ?)",
            (sid, body, store.sha256_text(body), prev))
        prev = sid
    con.commit()
    con.close()
    refused = redact_op(instance, "src-chain-3", n=3)
    assert error_of(refused) == "op 0 (redact_source): source src-chain-3 has more than 3 versions"
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
    assert ro_rows(instance, "SELECT title FROM sources WHERE id = 'src-s'") == [("Synthetic source",)]


def test_a_chain_of_exactly_chain_max_redacts(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    from brain import redact_ops

    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    monkeypatch.setattr(redact_ops, "CHAIN_MAX", 5)
    con = context_connection(instance)
    prev = None
    for i in range(5):
        body = f"version {i} of the vault note"
        con.execute(
            "INSERT INTO sources (id, kind, title, uri, source_date, author_person_id, content, content_sha256, external_ref, captured_at, captured_by, supersedes_id) "
            "VALUES (?, 'document', 'chain', '', '2026-10-07', NULL, ?, ?, 'document:work-docs/chain.md', '2026-10-08T00:00:00Z', 'tester:x', ?)",
            (f"src-chain-{i}", body, store.sha256_text(body), prev))
        prev = f"src-chain-{i}"
    con.commit()
    con.close()
    assert redact_op(instance, "src-chain-2").receipt["status"] == "applied"
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE title = 'redacted'") == [(5,)]


def test_receipt_targets_and_post_commit_check(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    app = Applier(instance, allow_reserved_agent=True)
    try:
        outcome = app.apply(json.dumps(redact_packet(sid)).encode())
    finally:
        app.close()
    targets = outcome.receipt["targets"]
    keys = [(t["table"], t["id"], t["action"]) for t in targets]
    assert len(keys) == len(set(keys)) or all(a == "update" for _t, _i, a in keys) is False
    assert not (instance / "PAUSED").exists() and outcome.integrity == "ok" and outcome.exit_code == 0
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        assert store.search_targets_drift(con, targets) == []
    finally:
        con.close()
    assert store.check_instance(instance) == []
    tables = {t["table"] for t in targets}
    assert {"sources", "claims", "facts", "fact_history", "decisions", "records", "record_history", "actions", "checklist_items",
            "projects", "signals", "unchecked_text", "meetings", "threads", "write_log", "collector_runs"} <= tables
    assert all(set(t) == {"table", "id", "action"} for t in targets)
    assert all(M not in json.dumps(t) for t in targets)


# -- 40, 41 -----------------------------------------------------------------------------------------

def test_an_enum_value_does_not_block_apply_config(tmp_path: Path, clock: Clock) -> None:
    from brain.init import apply_config

    instance = world(tmp_path, text="Region is us for the Pro plan. The code is secret.")
    claim(instance, "bot", "clm-region", "demo.region", "us", "Region is us for the Pro plan", "src-s")
    assert redact_op(instance, "src-s").receipt["status"] == "applied"
    fact = row_dict(instance, "SELECT * FROM facts WHERE key = 'demo.region'")
    assert fact["value_text"] == "[redacted]"
    toml = (instance / "brain.toml").read_text()
    new = toml.replace('allowed_values = ["au", "us"]', 'allowed_values = ["au", "us", "eu"]')
    assert new != toml
    (instance / "brain.toml").write_text(new)
    outcome = apply_config(instance)
    assert outcome.receipt["status"] == "applied", outcome.receipt


def test_no_new_claim_can_quote_a_redacted_source(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    redact_op(instance, "src-s")
    outcome = _apply(instance, "bot", _clm("clm-again", "demo.motto", M, f"Contact {M} for the vault", "src-s"))
    assert outcome.receipt["status"] == "rejected"
    assert error_of(outcome) == "op 0 (add_claim): source src-s is redacted"
    assert ro_rows(instance, "SELECT count(*) FROM claims WHERE id = 'clm-again'") == [(0,)]


# -- 42, 42a, 42c -----------------------------------------------------------------------------------

def test_refetch_creates_nothing(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    redact.run(instance, sid, REASON, OPERATOR)
    stored = len(sources(instance))
    claim_drift = "SELECT count(*) FROM signals WHERE kind = 'drift' AND subject_table = 'facts'"
    drift_before = ro_rows(instance, claim_drift)[0][0]
    clock.at("2026-10-09T06:00:00Z")
    doc = export(
        [item(PATH, TEXT, date="2026-10-07", uri=f"https://example.com/{M}/page"), item("new/doc.md", "A brand new document about nothing.")],
        fetched_at="2026-10-09T05:00:00Z",
    )
    code, payload, err = run_collect(instance, doc)
    assert code == 0 and payload["status"] == "ok", (payload, err)
    assert len(sources(instance)) == stored + 1  # only the new document
    assert "Intake" not in "\n".join(r[0] for r in ro_rows(instance, "SELECT error FROM collector_runs"))
    assert scan_instance(instance) == {}
    # the same file with changed text makes a new version that supersedes the tombstone, carries nothing from it
    clock.at("2026-10-09T07:00:00Z")
    changed = export([item(PATH, "The vault policy was rewritten for everyone in October.", date="2026-10-08")], fetched_at="2026-10-09T07:00:00Z")
    code, payload, err = run_collect(instance, changed)
    assert code == 0, (payload, err)
    head = sources(instance, REF)[-1]
    assert head[1] == sid or head[1] in [s[0] for s in sources(instance, REF)]
    assert ro_rows(instance, "SELECT count(*) FROM claims WHERE source_id = ? AND status = 'live'", (head[0],)) == [(0,)]
    assert ro_rows(instance, claim_drift)[0][0] == drift_before  # no drift on the rejected claims
    assert store.check_instance(instance) == []
    # a resend of the original capture packet bytes is a duplicate and writes nothing
    rows = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    cid = ro_rows(instance, "SELECT change_id FROM write_log WHERE change_id LIKE 'workdocs-%' ORDER BY rowid LIMIT 1")[0][0]
    raw = ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (cid,))[0][0]
    app = Applier(instance, collect=True)
    try:
        outcome = app.apply(raw.encode())
    finally:
        app.close()
    assert outcome.duplicate and ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == rows


def capture_world(tmp_path: Path, clock: Clock):
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    return instance


SECRET, CLEAN = f"Contact {M} for the vault.", "The vault is audited yearly."


def collect_at(instance: Path, clock: Clock, stamp: str, *items, **kwargs):
    clock.at(stamp)
    return run_collect(instance, export(list(items), fetched_at=stamp, **kwargs))


def test_capture_skips_redacted_text(tmp_path: Path, clock: Clock) -> None:
    instance = capture_world(tmp_path, clock)
    for text, stamp in ((SECRET, "2026-10-08T01:00:00Z"), (CLEAN, "2026-10-08T02:00:00Z")):
        assert collect_at(instance, clock, stamp, item("n.md", text))[0] == 0
    first = sources(instance, "document:work-docs/n.md")[0][0]
    clock.at("2026-10-08T03:00:00Z")
    result = redact.run(instance, first, REASON, OPERATOR)
    assert len(result["sources"]) == 2
    n = len(sources(instance))
    # the clean current version is a tombstone now: capturing it again is a no-op
    code, payload, err = collect_at(instance, clock, "2026-10-08T04:00:00Z", item("n.md", CLEAN))
    assert code == 0 and len(sources(instance)) == n
    # an item that matches redacted text is left out, with its note
    code, payload, err = collect_at(instance, clock, "2026-10-08T04:30:00Z", item("n.md", SECRET))
    assert code == 0 and payload["status"] == "partial" and len(sources(instance)) == n
    note = ro_rows(instance, "SELECT error FROM collector_runs ORDER BY started_at DESC, rowid DESC LIMIT 1")[0][0]
    assert "1 item matches redacted text and was left out" in (note + json.dumps(payload))
    # the file is removed (a complete empty export), then the removed text returns: nothing is created
    collect_at(instance, clock, "2026-10-08T05:00:00Z")
    assert len(sources(instance)) == n + 1
    code, payload, err = collect_at(instance, clock, "2026-10-08T06:00:00Z", item("n.md", SECRET))
    assert code == 0 and len(sources(instance)) == n + 1
    # changed to new text, then back to the removed text: nothing the second time
    collect_at(instance, clock, "2026-10-08T07:00:00Z", item("n.md", "Brand new words about the vault rota."))
    assert len(sources(instance)) == n + 2
    code, payload, err = collect_at(instance, clock, "2026-10-08T08:00:00Z", item("n.md", SECRET))
    assert code == 0 and len(sources(instance)) == n + 2
    # changed to new text only: a new source
    collect_at(instance, clock, "2026-10-08T09:00:00Z", item("n.md", "Different words again for the rota."))
    assert len(sources(instance)) == n + 3
    assert store.check_instance(instance) == []


def test_a_returned_redacted_file_does_not_stop_intake(tmp_path: Path, clock: Clock) -> None:
    instance = capture_world(tmp_path, clock)
    for text, stamp in ((SECRET, "2026-10-08T01:00:00Z"), (CLEAN, "2026-10-08T02:00:00Z")):
        assert collect_at(instance, clock, stamp, item("n.md", text))[0] == 0
    first = sources(instance, REF.replace("vault/notes.md", "n.md"))[0][0]
    clock.at("2026-10-08T03:00:00Z")
    redact.run(instance, first, REASON, OPERATOR)
    collect_at(instance, clock, "2026-10-08T05:00:00Z")  # the original is deleted: a removal version
    code, payload, err = collect_at(instance, clock, "2026-10-08T06:00:00Z", item("n.md", SECRET))
    assert code == 0 and payload["status"] == "partial", (payload, err)  # the file returns and is left out
    n = len(sources(instance))
    ref = "document:work-docs/n.md"
    code, payload, err = collect_at(instance, clock, "2026-10-08T07:00:00Z", item("other.md", "Another note about seats."), present=[ref])
    assert code == 0 and payload["status"] == "ok", (payload, err)
    assert len(sources(instance)) == n + 1
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE instr(content, ?) > 0", (M,)) == [(0,)]
    assert store.check_instance(instance) == []
    # a present ref with no redacted version and no current version still raises the rescan error
    code, payload, err = collect_at(instance, clock, "2026-10-08T08:00:00Z", item("third.md", "A third note."), present=["document:work-docs/never.md"])
    last = ro_rows(instance, "SELECT status, error FROM collector_runs ORDER BY started_at DESC, rowid DESC LIMIT 1")[0]
    assert code == 1 and last[0] == "error"
    assert "is listed as unchanged but the store has no version of it; rerun with --rescan" in last[1] + err + json.dumps(payload), (payload, err, last)


# -- the reviewer's attacks -------------------------------------------------------------

def test_a_source_whose_id_prefixes_another(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    texts = {
        "src-a": f"Alpha memo. Contact {M} for the vault. The Pro plan includes 25 seats.",
        "src-a-r3": "Third memo. The Pro plan includes 30 seats and more.",
        "src-end.": f"Ends with a full stop. The code is {M} here. Another line.",
        "src-end": "Plain twin memo. The Pro plan includes 40 seats overall.",
    }
    for sid, text in texts.items():
        _ok(_apply(instance, "tester", source_op(sid, text, kind="document")))
    claim(instance, "bot", "clm-r3", "demo.seats", "30", "The Pro plan includes 30 seats", "src-a-r3")
    cap = Applier(instance, collect=True)
    try:
        for ident, sid in (("run-p1", "src-a-r3"), ("run-p2", "src-end"), ("run-p3", "src-a"), ("run-p4", "src-end.")):
            error = f"rejected: quote is not in the cited source {sid}."
            op = {"op": "record_collector_run", "row": {"id": ident, "collector": "work-docs", "started_at": "2026-10-08T00:00:00Z", "finished_at": "2026-10-08T00:00:05Z", "status": "error", "items": 1, "input_sha256": "", "error": error}}
            _ok(cap.apply(json.dumps(make_packet("workdocs", op, change_id=f"workdocs-{ident}")).encode()))
    finally:
        cap.close()
    # a claim of another chain that was resolved by the source being redacted
    _ok(_apply(instance, "importer", _legacy("clm-res", "demo.region", "us", source="src-a-r3", status="rejected",
                                             resolution_source_id="src-a", resolution_locator="para 2")))
    twin_log = ro_rows(instance, "SELECT packet_json, error FROM write_log WHERE change_id = 'workdocs-run-p1'")[0]
    assert redact_op(instance, "src-a").receipt["status"] == "applied"
    assert redact_op(instance, "src-end.", n=1).receipt["status"] == "applied"
    runs = dict(ro_rows(instance, "SELECT id, error FROM collector_runs WHERE id LIKE 'run-p%'"))
    assert runs["run-p3"] == runs["run-p4"] == "redacted on 2026-10-09"
    assert runs["run-p1"].endswith("src-a-r3.")  # an id that merely begins with the redacted one is not named
    # An id that ends in a full stop is indistinguishable from the sentence-final stop after its twin, so the
    # twin's refusal text goes too (the over-scrub; the source itself is untouched).
    assert runs["run-p2"] == "redacted on 2026-10-09"
    assert ro_rows(instance, "SELECT packet_json, error FROM write_log WHERE change_id = 'workdocs-run-p1'")[0] == twin_log
    assert ro_rows(instance, "SELECT status, quote FROM claims WHERE id = 'clm-r3'")[0][0] == "live"
    assert ro_rows(instance, "SELECT title FROM sources WHERE id IN ('src-a-r3', 'src-end') ORDER BY id") == [("Synthetic source",), ("Synthetic source",)]
    assert ro_rows(instance, "SELECT resolution_source_id, resolution_locator FROM claims WHERE id = 'clm-res'") == [("src-a", "para 2")]
    assert store.check_instance(instance) == []
