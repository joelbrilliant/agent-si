"""Carry-forward, drift, the successor rule, the tombstone no-op and `source_current`. Every name and number here is invented."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from brain import applier as applier_mod
from brain import marks, reads, store
from brain.applier import TOMBSTONE_PREFIX, TOMBSTONE_TITLE, Applier
from brain.collectors import capture
from tests.conftest import context_connection, make_packet, ro_rows, source_op, upsert_op
from tests.phase2a import custom_instance, export, item, run_collect, sources
from tests.test_truth import (  # noqa: F401  (clock is a fixture)
    Clock,
    _apply,
    _clm,
    _conflicts,
    _err,
    _fact,
    _ok,
    _resolve,
    clock,
)

REF = "document:work-docs/specs/pricing.md"
PATH = "specs/pricing.md"
KEYS = [
    {"key": "demo.seats", "value_type": "int"},
    {"key": "demo.price", "value_type": "number", "unit": "AUD"},
    {"key": "demo.motto", "value_type": "text"},
    {"key": "demo.region", "value_type": "enum", "allowed_values": ["au", "us"]},
]
AGENTS = {
    "bot": {"kind": "agent", "operations": "*"},
    "other": {"kind": "agent", "operations": "*", "approvers": ["tester"]},
    "careful": {"kind": "collector", "manual_sources": True, "operations": "*", "approval": "required", "approvers": ["tester"]},
    "importer": {"kind": "collector", "manual_sources": True, "legacy_import": True, "operations": "*"},
}
V1 = "The Pro plan includes 25 seats. The Pro plan costs 49 AUD a seat."
V2 = "The Pro plan includes 25 seats. The Pro plan costs 59 AUD a seat."
SEATS = "The Pro plan includes 25 seats"
PRICE = "The Pro plan costs 49 AUD a seat"


def carried_id(old: str, source: str) -> str:
    return "clm-" + hashlib.sha256(f"carry\n{old}\n{source}".encode()).hexdigest()[:24]


def world(tmp_path: Path, text: str = V1, *, date: str = "2026-10-07", path: str = PATH, principal: bool = False, **kwargs):
    """An instance with one captured document. With principal=True the export is marked, so the document's
    source is authored by the principal (a decision needs that for its quote)."""
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS + kwargs.pop("extra_keys", []), **kwargs)
    doc = export([item(path, text, date=date, from_principal=principal or None)] if principal else [item(path, text, date=date)])
    if principal:
        doc["mark"] = marks.sign(marks.ensure_key(instance), doc)
    code, payload, err = run_collect(instance, doc)
    assert code == 0, (payload, err)
    return instance, current(instance, f"document:work-docs/{path}")


def current(instance: Path, ref: str = REF) -> str:
    return sources(instance, ref)[-1][0]


def recapture(instance: Path, text: str, *, date: str = "2026-10-08", path: str = PATH, stamp: str = "2026-10-08T01:00:00Z"):
    code, payload, err = run_collect(instance, export([item(path, text, date=date)], fetched_at=stamp))
    assert code == 0, (payload, err)
    return payload


def claim(instance: Path, agent: str, cid: str, key: str, raw: str, quote: str, source: str, **kwargs):
    return _ok(_apply(instance, agent, _clm(cid, key, raw, quote, source, **kwargs)))


def rows_of(instance: Path, cid: str) -> dict:
    con = store.connect_readonly(instance / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        found = con.execute("SELECT * FROM claims WHERE id = ?", (cid,)).fetchone()
        return None if found is None else dict(found)
    finally:
        con.close()


def drift(instance: Path, status: str | None = None) -> list[tuple]:
    sql = "SELECT id, subject_table, subject_id, detail, status, source_id, locator FROM signals WHERE kind = 'drift'"
    params: tuple = ()
    if status:
        sql += " AND status = ?"
        params = (status,)
    return ro_rows(instance, sql + " ORDER BY id", params)


# -- 42 to 44 ---------------------------------------------------------------------------------------


def test_unchanged_quote_carries_a_claim_forward(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1, basis="observed")
    before = _fact(instance, "demo.seats")
    clock.at("2026-10-09T05:00:00Z")  # the carry happens later than the claim, so a verified time would show
    payload = recapture(instance, V2)
    v2 = current(instance)
    new_id = carried_id("clm-seats", v2)
    old, new = rows_of(instance, "clm-seats"), rows_of(instance, new_id)
    assert (old["status"], old["superseded_by"]) == ("superseded", new_id)
    for column in ("key", "scope", "value_type", "value_text", "value_int", "value_real", "value_date", "value_raw",
                   "status", "basis", "as_of", "locator", "quote", "checked", "proposed_by"):
        assert new[column] == (old[column] if column != "status" else "live"), column
    assert (new["source_id"], new["supersedes"], new["superseded_by"], new["checked"]) == (v2, "[]", None, 1)
    stamp = f"workdocs:{payload['receipt']['change_id']}"
    assert (new["created_by"], new["updated_by"], new["proposed_by"]) == (stamp, stamp, "bot")
    fact = _fact(instance, "demo.seats")
    assert (fact["source_id"], fact["version"], fact["status"]) == (v2, before["version"] + 1, "current")
    assert fact["last_verified_at"] == before["last_verified_at"]  # no verified time: a carry is not a confirmation
    history = ro_rows(instance, "SELECT version, change FROM fact_history WHERE key = 'demo.seats' ORDER BY version")
    assert history == [(1, "created"), (2, "updated")]  # one derivation


def test_a_claim_that_survives_only_by_date_resolution_is_not_carried(tmp_path: Path, clock: Clock) -> None:
    keys = KEYS + [{"key": "demo.launch", "value_type": "date"}]
    text = "The launch is tomorrow. The review is on 2026-11-02."
    instance, v1 = world(tmp_path, text, extra_keys=[keys[-1]], date="2026-10-07")
    claim(instance, "bot", "clm-rel", "demo.launch", "tomorrow", "The launch is tomorrow", v1)
    claim(instance, "bot", "clm-iso", "demo.launch", "2026-11-02", "The review is on 2026-11-02", v1, scope="review")
    assert rows_of(instance, "clm-rel")["value_date"] == "2026-10-08"
    recapture(instance, text + " Billing is monthly.", date="2026-10-08")  # a day later: "tomorrow" is another day now
    v2 = current(instance)
    assert rows_of(instance, carried_id("clm-rel", v2)) is None and rows_of(instance, "clm-rel")["status"] == "live"
    assert rows_of(instance, carried_id("clm-iso", v2))["status"] == "live"  # an ISO date reads the same
    assert [row[2] for row in drift(instance, "open")] == ["demo.launch"]


def test_a_claim_lost_at_b_comes_back_at_a2(tmp_path: Path, clock: Clock) -> None:
    instance, a1 = world(tmp_path)
    claim(instance, "bot", "clm-price", "demo.price", "49", PRICE, a1)
    recapture(instance, V2, stamp="2026-10-08T01:00:00Z")  # B: the price changed, the quote is gone
    assert rows_of(instance, "clm-price")["status"] == "live" and len(drift(instance, "open")) == 1
    recapture(instance, V1, stamp="2026-10-08T02:00:00Z")  # A2: the same text as A
    a2 = current(instance)
    assert a2 != a1 and a2.endswith("-r3")
    new_id = carried_id("clm-price", a2)
    assert rows_of(instance, "clm-price")["superseded_by"] == new_id
    assert rows_of(instance, new_id)["status"] == "live" and rows_of(instance, new_id)["source_id"] == a2
    assert drift(instance, "open") == [] and len(drift(instance, "resolved")) == 1
    assert _fact(instance, "demo.price")["source_id"] == a2


def test_remove_and_return_carries_the_claims(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    other = item("specs/other.md", "Other file stays.")
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    code, _p, _e = run_collect(instance, export([other], fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0 and len(sources(instance, REF)) == 2
    assert len(drift(instance, "open")) == 1  # the removal version doesn't hold the quote
    assert rows_of(instance, "clm-seats")["status"] == "live"
    code, _p, _e = run_collect(instance, export([item(PATH, V1), other], fetched_at="2026-10-08T02:00:00Z"))
    assert code == 0
    back = current(instance)
    new_id = carried_id("clm-seats", back)
    assert rows_of(instance, "clm-seats")["superseded_by"] == new_id
    assert rows_of(instance, new_id)["source_id"] == back
    assert drift(instance, "open") == []
    assert len(sources(instance, REF)) == 3


def test_brain_check_is_clean_after_a_carry_forward(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    claim(instance, "bot", "clm-price", "demo.price", "49", PRICE, v1)
    writer = Applier(instance)
    try:
        packet = make_packet(
            "tester",
            {"op": "add_source", "row": {"id": "src-hand-v2", "kind": "document", "title": "Pricing", "source_date": "2026-10-08",
                                         "author_person_id": None, "content": V2, "external_ref": REF, "supersedes_id": v1}},
        )
        outcome = writer.apply(json.dumps(packet).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied" and outcome.integrity == "ok"
    tables = {(target["table"], target["action"]) for target in outcome.receipt["targets"]}
    assert {("claims", "insert"), ("claims", "update"), ("facts", "update"), ("signals", "insert")} <= tables
    assert store.check_instance(instance) == []
    assert not (instance / "PAUSED").exists()


def test_carried_claim_keeps_its_writer(tmp_path: Path, clock: Clock) -> None:
    text = V1 + " From November the Pro plan includes 30 seats."
    instance, v1 = world(tmp_path, text)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    payload = recapture(instance, text + " Billing is monthly.")
    v2 = current(instance)
    carried = rows_of(instance, carried_id("clm-seats", v2))
    assert carried["proposed_by"] == "bot" and carried["created_by"] == f"workdocs:{payload['receipt']['change_id']}"
    # Another agent's supersede of the carried claim is held (rule 3).
    held = _apply(instance, "other", _clm("clm-other", "demo.seats", "30", "From November the Pro plan includes 30 seats", v2,
                                          supersedes=[carried["id"]]))
    assert held.receipt["status"] == "held", held.receipt["error"]
    assert rows_of(instance, carried["id"])["status"] == "live"
    # The original writer's supersede applies (rule 2).
    _ok(_apply(instance, "bot", _clm("clm-bot", "demo.seats", "30", "From November the Pro plan includes 30 seats", v2,
                                     supersedes=[carried["id"]])))
    assert rows_of(instance, carried["id"])["status"] == "superseded"
    assert _fact(instance, "demo.seats")["value_int"] == 30


def test_changed_quote_opens_one_drift_signal(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-a", "demo.seats", "25", SEATS, v1)
    claim(instance, "bot", "clm-b", "demo.seats", "25", "includes 25 seats", v1, scope="pro")
    recapture(instance, "The Pro plan is now unlimited.", date="2026-10-08")
    v2 = current(instance)
    signals = drift(instance)
    assert len(signals) == 1
    signal_id, table, subject, detail, status, source, locator = signals[0]
    expected = "sig-carry-" + hashlib.sha256(f"drift\nfacts\ndemo.seats\n{v2}".encode()).hexdigest()[:24]
    assert (signal_id, table, subject, status, source) == (expected, "facts", "demo.seats", "open", v2)
    assert detail == f"source {REF} changed on 2026-10-08; the quote is no longer in it"
    assert locator == "claim clm-a"  # the first lost claim, in id order
    assert rows_of(instance, "clm-a")["status"] == "live" and rows_of(instance, "clm-b")["status"] == "live"
    version = ro_rows(instance, "SELECT version, basis, thread_id FROM signals WHERE id = ?", (signal_id,))[0]
    assert version == (1, "observed", None)


# -- 45 -----------------------------------------------------------------------------------------------


DECISION = {
    "id": "dec-ship",
    "title": "Ship the Pro plan",
    "quote": "We will ship the Pro plan in November.",
    "decided_on": "2026-10-07",
    "decided_by_person_id": "per-alex-example",
    "status": "in_effect",
    "project_id": "general",
}


def test_drift_closes_on_update_rejection_recite_and_dismissal(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(
        tmp_path,
        "Alpha says seats are 25. Beta says the price is 49 AUD. We will ship the Pro plan in November. Gamma says motto is Small steps.",
        principal=True,
    )
    _ok(_apply(instance, "tester", source_op("src-note", "Beta also says the price is 59 AUD. We will ship the Pro plan in November.",
                                             kind="document", author="per-alex-example")))
    claim(instance, "bot", "clm-seats", "demo.seats", "25", "seats are 25", v1)
    claim(instance, "bot", "clm-price", "demo.price", "49", "the price is 49 AUD", v1)
    claim(instance, "other", "clm-price-b", "demo.price", "59", "the price is 59 AUD", "src-note")
    claim(instance, "bot", "clm-motto", "demo.motto", "Small steps", "motto is Small steps", v1)
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, v1)))
    manual = upsert_op("upsert_signal", {"id": "sig-manual-drift", "kind": "drift", "subject_table": "facts",
                                         "subject_id": "demo.motto", "basis": "observed"}, v1)
    _ok(_apply(instance, "tester", manual))
    recapture(instance, "Alpha says seats are now unlimited. Beta says nothing. Gamma is quiet.")
    v2 = current(instance)
    open_ids = {row[2]: row[0] for row in drift(instance, "open") if row[0].startswith("sig-carry-")}
    assert set(open_ids) == {"demo.seats", "demo.price", "demo.motto", "dec-ship"}
    # Rule 1: a claim from the new version replaces the old one and ends the drift.
    text = "Alpha says seats are now unlimited. Beta says nothing. Gamma is quiet. Seats are 30 now."
    recapture(instance, text, stamp="2026-10-08T02:00:00Z")
    v3 = current(instance)
    # (the second capture is itself a successor: the first drift moves to the new source with the claims)
    open_now = {row[2]: row[0] for row in drift(instance, "open") if row[0].startswith("sig-carry-")}
    assert "demo.seats" in open_now
    _ok(_apply(instance, "bot", _clm("clm-seats-new", "demo.seats", "30", "Seats are 30 now", v3)))
    assert not any(row[2] == "demo.seats" for row in drift(instance, "open"))
    # A resolve_fact rejection of the lost claim ends the drift (the other claim wins).
    assert any(row[2] == "demo.price" for row in drift(instance, "open"))
    _ok(_apply(instance, "tester", _resolve("demo.price", ["clm-price-b"], ["clm-price"], "the price is 59 AUD", "src-note")))
    assert not any(row[2] == "demo.price" for row in drift(instance, "open"))
    # Re-citing the decision on a source that has no successor ends its drift.
    stored = ro_rows(instance, "SELECT version FROM decisions WHERE id = 'dec-ship'")[0][0]
    assert any(row[2] == "dec-ship" for row in drift(instance, "open"))
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, "src-note", base=stored)))
    assert not any(row[2] == "dec-ship" for row in drift(instance, "open"))
    # An agent may dismiss a drift signal as it would any other.
    motto = [row for row in drift(instance, "open") if row[2] == "demo.motto"][0][0]
    version = ro_rows(instance, "SELECT version FROM signals WHERE id = ?", (motto,))[0][0]
    _ok(_apply(instance, "tester", upsert_op("upsert_signal", {"id": motto, "status": "dismissed", "resolved_at": "2026-10-09T00:00:00Z"},
                                             v3, base=version)))
    assert ro_rows(instance, "SELECT status FROM signals WHERE id = ?", (motto,)) == [("dismissed",)]
    # The older, agent-made drift signal was never touched.
    assert ro_rows(instance, "SELECT status, version FROM signals WHERE id = 'sig-manual-drift'") == [("open", 1)]
    assert v2 != v3


# -- 46, 47 -------------------------------------------------------------------------------------------


def test_decision_with_its_quote_is_left_alone(tmp_path: Path, clock: Clock) -> None:
    text = "We will ship the Pro plan in November. Seats are 25."
    instance, v1 = world(tmp_path, text, principal=True)
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, v1)))
    before = ro_rows(instance, "SELECT version, source_id, updated_at, updated_by FROM decisions WHERE id = 'dec-ship'")
    got = reads.run_tool(instance, "get", {"entity": "decision", "id": "dec-ship"}, now="2026-10-09T00:00:00Z").structured
    assert "source_current" not in got["source"]
    recapture(instance, text + " Billing is monthly.")
    assert ro_rows(instance, "SELECT version, source_id, updated_at, updated_by FROM decisions WHERE id = 'dec-ship'") == before
    got = reads.run_tool(instance, "get", {"entity": "decision", "id": "dec-ship"}, now="2026-10-09T00:00:00Z").structured
    assert got["source"]["source_current"] is False and got["source"]["id"] == v1
    assert drift(instance) == []


def test_decision_whose_quote_is_gone_opens_drift(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path, "We will ship the Pro plan in November.", principal=True)
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, v1)))
    recapture(instance, "The launch is postponed.")
    v2 = current(instance)
    rows = drift(instance)
    assert len(rows) == 1
    signal_id, table, subject, detail, status, source, locator = rows[0]
    assert (table, subject, status, source, locator) == ("decisions", "dec-ship", "open", v2, "decision")
    assert signal_id == "sig-carry-" + hashlib.sha256(f"drift\ndecisions\ndec-ship\n{v2}".encode()).hexdigest()[:24]
    assert detail == f"source {REF} changed on 2026-10-08; the quote is no longer in it"
    assert ro_rows(instance, "SELECT version FROM decisions WHERE id = 'dec-ship'") == [(1,)]


# -- 48, 49, 50, 51 -----------------------------------------------------------------------------------


def test_unchecked_rejected_and_superseded_claims_are_not_carried(tmp_path: Path, clock: Clock) -> None:
    text = "Seats are 25. Price is 49 AUD. Region is AU. Seats are 26."
    instance, v1 = world(tmp_path, text)
    _ok(_apply(instance, "tester", source_op("src-won", "The price is 59 AUD a seat now.", kind="document", author="per-alex-example")))
    # Unchecked: a legacy import without a quote.
    _ok(_apply(instance, "importer", {"op": "add_claim", "row": {"id": "clm-unchecked", "key": "demo.region", "value_raw": "au",
                                                                 "basis": "reported"},
                                      "source": {"source_id": v1, "locator": "para 1"}}))
    # Rejected: the 49 loses to the 59.
    claim(instance, "bot", "clm-lost", "demo.price", "49", "Price is 49 AUD", v1)
    claim(instance, "other", "clm-won", "demo.price", "59", "The price is 59 AUD a seat", "src-won")
    _ok(_apply(instance, "tester", _resolve("demo.price", ["clm-won"], ["clm-lost"], "The price is 59 AUD a seat", "src-won")))
    # Superseded: rule 2.
    claim(instance, "bot", "clm-old", "demo.seats", "25", "Seats are 25", v1)
    claim(instance, "bot", "clm-new", "demo.seats", "26", "Seats are 26", v1, supersedes=["clm-old"])
    assert rows_of(instance, "clm-unchecked")["checked"] == 0
    assert (rows_of(instance, "clm-lost")["status"], rows_of(instance, "clm-old")["status"]) == ("rejected", "superseded")
    recapture(instance, text + " Billing is monthly.")
    v2 = current(instance)
    for cid in ("clm-unchecked", "clm-lost", "clm-old"):
        assert rows_of(instance, carried_id(cid, v2)) is None, cid
    assert rows_of(instance, "clm-unchecked")["status"] == "live" and rows_of(instance, "clm-lost")["status"] == "rejected"
    carried = {row[0] for row in ro_rows(instance, "SELECT id FROM claims WHERE created_by LIKE 'workdocs:%'")}
    assert carried == {carried_id("clm-new", v2)}  # clm-won sits on another source
    assert [row[2] for row in drift(instance)] == []


def test_planned_claim_is_carried_as_planned(tmp_path: Path, clock: Clock) -> None:
    text = "From November the Pro plan will include 30 seats."
    instance, v1 = world(tmp_path, text)
    claim(instance, "bot", "clm-plan", "demo.seats", "30", "the Pro plan will include 30 seats", v1, planned=True)
    assert rows_of(instance, "clm-plan")["status"] == "planned"
    recapture(instance, text + " Billing is monthly.")
    v2 = current(instance)
    new = rows_of(instance, carried_id("clm-plan", v2))
    assert new["status"] == "planned" and new["source_id"] == v2
    assert rows_of(instance, "clm-plan")["status"] == "superseded"


def test_carry_forward_is_not_a_change(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    now = "2026-10-09T06:00:00Z"
    clock.at("2026-10-09T05:00:00Z")
    recapture(instance, V2)
    v2 = current(instance)
    brief = reads.run_tool(instance, "brief", {}, now=now).text
    assert "Changed in the last 7 days" not in brief
    changes = {row[0] for row in ro_rows(instance, "SELECT change FROM fact_history")}
    assert changes <= {"created", "updated"}
    values = ro_rows(instance, "SELECT DISTINCT value_int FROM fact_history WHERE key = 'demo.seats'")
    assert values == [(25,)]
    # Control: a real change to the value does show.
    recapture(instance, V2 + " Seats rise to 30 next year.", stamp="2026-10-08T02:00:00Z")
    v3 = current(instance)
    assert rows_of(instance, carried_id(carried_id("clm-seats", v2), v3))["status"] == "live"
    _ok(_apply(instance, "bot", _clm("clm-30", "demo.seats", "30", "Seats rise to 30", v3, supersedes=[carried_id(carried_id("clm-seats", v2), v3)])))
    brief = reads.run_tool(instance, "brief", {}, now=now).text
    assert "Changed in the last 7 days" in brief and "demo.seats" in brief


def test_carry_forward_does_not_touch_a_sibling_claim(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path, V1 + " Some reports say 30 seats.")
    claim(instance, "bot", "clm-keep", "demo.seats", "25", SEATS, v1)
    claim(instance, "other", "clm-lost", "demo.seats", "30", "Some reports say 30 seats", v1)  # a second value in the same bucket
    assert _fact(instance, "demo.seats")["status"] == "disputed"
    recapture(instance, V2)
    v2 = current(instance)
    assert rows_of(instance, carried_id("clm-keep", v2))["status"] == "live"
    lost = rows_of(instance, "clm-lost")
    assert lost["status"] == "live" and lost["source_id"] == v1  # rule 1 did not run
    assert _fact(instance, "demo.seats")["status"] == "disputed"
    assert [row[2] for row in drift(instance, "open")] == ["demo.seats"]
    assert len(_conflicts(instance, "demo.seats")) == 1


def test_carry_forward_receipt_stamp_and_targets(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    claim(instance, "bot", "clm-price", "demo.price", "49", PRICE, v1)
    path = Path(instance).parent / "exports" / "again.json"
    doc = export([item(PATH, V2, date="2026-10-08")], fetched_at="2026-10-08T01:00:00Z")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.canonical_json(doc))
    code, payload, _e = run_collect(instance, path)
    assert code == 0
    receipt = payload["receipt"]
    stamp = f"workdocs:{receipt['change_id']}"
    v2 = current(instance)
    seats_new = carried_id("clm-seats", v2)
    targets = {(t["table"], t["id"], t["action"]) for t in receipt["targets"]}
    signal = drift(instance)[0][0]
    assert ("claims", seats_new, "insert") in targets and ("claims", "clm-seats", "update") in targets
    assert ("facts", "demo.seats", "update") in targets and ("signals", signal, "insert") in targets
    stamped = ro_rows(instance, "SELECT created_by, updated_by FROM claims WHERE id = ?", (seats_new,))[0]
    assert stamped == (stamp, stamp)
    assert ro_rows(instance, "SELECT updated_by FROM claims WHERE id = 'clm-seats'") == [(stamp,)]
    assert ro_rows(instance, "SELECT updated_by FROM facts WHERE key = 'demo.seats'") == [(stamp,)]
    assert ro_rows(instance, "SELECT updated_by FROM signals WHERE id = ?", (signal,)) == [(stamp,)]
    before = (ro_rows(instance, "SELECT count(*) FROM claims")[0][0], ro_rows(instance, "SELECT count(*) FROM write_log")[0][0])
    code, again, _e = run_collect(instance, path)
    assert code == 0 and again["duplicate"] is True
    assert (ro_rows(instance, "SELECT count(*) FROM claims")[0][0], ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]) == before


def test_a_mass_change_of_twenty_documents_leaves_the_store_clean(tmp_path: Path, clock: Clock) -> None:
    keys = [{"key": f"doc{n:03d}.k{m}", "value_type": "int"} for n in range(20) for m in range(5)]
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=keys)
    docs = []
    for n in range(100):
        text = " ".join(f"Item {m} of doc {n} is {n}{m}." for m in range(5))
        docs.append(item(f"bulk/d{n:03d}.md", text))
    assert run_collect(instance, export(docs))[0] == 0
    for n in range(20):
        ref = docs[n]["ref"]
        ops = [_clm(f"clm-d{n:03d}-{m}", f"doc{n:03d}.k{m}", f"{n}{m}", f"Item {m} of doc {n} is {n}{m}", current(instance, ref)) for m in range(5)]
        _ok(_apply(instance, "bot", *ops))
    edited = []
    for n in range(20):
        # keep claims 0 to 2, lose 3 and 4
        text = " ".join(f"Item {m} of doc {n} is {n}{m}." for m in range(3)) + " The rest was rewritten."
        edited.append(dict(docs[n], text=text))
    started = time.perf_counter()
    code, payload, err = run_collect(instance, export(edited, present=[doc["ref"] for doc in docs[20:]], fetched_at="2026-10-08T05:00:00Z"))
    print(f"carry-forward of 20 documents with 100 claims (60 carried, 40 drift): {time.perf_counter() - started:.3f} s")
    assert code == 0, (payload, err)
    carried = ro_rows(instance, "SELECT count(*) FROM claims WHERE created_by LIKE 'workdocs:%'")[0][0]
    assert carried == 60 and len(drift(instance, "open")) == 40
    assert store.check_instance(instance) == [] and not (instance / "PAUSED").exists()


# -- 53, 54 ---------------------------------------------------------------------------------------------


def _superseder(source_id: str, supersedes: str, content: str) -> dict:
    return {"op": "add_source", "row": {"id": source_id, "kind": "document", "title": "Pricing", "source_date": "2026-10-08",
                                        "author_person_id": None, "content": content, "external_ref": REF, "supersedes_id": supersedes}}


def test_carry_forward_runs_for_any_agent(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    # The principal's agent.
    _ok(_apply(instance, "tester", _superseder("src-by-tester", v1, V2)))
    first = rows_of(instance, carried_id("clm-seats", "src-by-tester"))
    assert first["created_by"].startswith("tester:")
    # The reserved agent.
    writer = Applier(instance, allow_reserved_agent=True)
    try:
        outcome = writer.apply(json.dumps(make_packet("brain", _superseder("src-by-brain", "src-by-tester", V2 + " More."))).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied"
    second = rows_of(instance, carried_id(first["id"], "src-by-brain"))
    assert second["created_by"].startswith("brain:") and second["proposed_by"] == "bot"
    # An approved held packet: carry-forward runs on approval, not before.
    held = _apply(instance, "careful", _superseder("src-by-careful", "src-by-brain", V2 + " Even more."))
    assert held.receipt["status"] == "held"
    assert rows_of(instance, carried_id(second["id"], "src-by-careful")) is None
    writer = Applier(instance)
    try:
        approved = writer.approve(held.receipt["change_id"], "tester")
    finally:
        writer.close()
    assert approved.receipt["status"] == "applied"
    third = rows_of(instance, carried_id(second["id"], "src-by-careful"))
    assert third is not None and third["created_by"].startswith("careful:")


def test_carry_forward_failure_rejects_the_packet(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)

    def boom(self, columns):
        raise sqlite3.IntegrityError("boom")

    monkeypatch.setattr(Applier, "_insert_claim", boom)
    outcome = _apply(instance, "tester", _superseder("src-fails", v1, V2))
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == "op 0 (add_source): boom"
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id = 'src-fails'") == [(0,)]
    assert rows_of(instance, "clm-seats")["status"] == "live"
    assert drift(instance) == []


# -- 55 to 57 -------------------------------------------------------------------------------------------


def _plan(instance: Path, sql: str, params: tuple) -> str:
    con = store.connect_reader(instance / "brain.sqlite3")
    try:
        return " | ".join(row[3] for row in con.execute("EXPLAIN QUERY PLAN " + sql, params))
    finally:
        con.close()


def test_carry_forward_lookups_use_indexes(tmp_path: Path, clock: Clock) -> None:
    instance, _v1 = world(tmp_path)
    claims = _plan(instance, "SELECT id FROM claims WHERE source_id = ? AND status IN ('live', 'planned') AND checked = 1", ("x",))
    assert "claims_by_source" in claims, claims
    params = (801, "document:work-docs/", "document:work-docs0")
    indexed = _plan(instance, capture.KNOWN_SQL, params)
    assert "sources_by_external_ref" in indexed, indexed
    without = capture.KNOWN_SQL.replace(" AND s.external_ref <> ''", "")
    assert without != capture.KNOWN_SQL
    assert "sources_by_external_ref" not in _plan(instance, without, params)
    # Migration 0006 adds decisions_by_source, so the decisions lookup is indexed too.
    decisions = _plan(instance, applier_mod.DECISIONS_BY_CHAIN_SQL, ("x",))
    assert decisions.startswith("SEARCH d USING INDEX decisions_by_source"), decisions


def test_sig_carry_ids_are_reserved(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", SEATS, v1)
    row = {"id": "sig-carry-abc", "kind": "drift", "subject_table": "facts", "subject_id": "demo.seats", "basis": "observed"}
    outcome = _apply(instance, "tester", upsert_op("upsert_signal", row, v1))
    assert _err(outcome) == "op 0 (upsert_signal): signal ids starting sig-carry- are reserved for the applier"
    allowed = _apply(instance, "tester", upsert_op("upsert_signal", dict(row, id="sig-mine-abc"), v1))
    assert allowed.receipt["status"] == "applied"


def _direct(instance: Path, agent: str, *ops):
    writer = Applier(instance, collect=True)
    try:
        return writer.apply(json.dumps(make_packet(agent, *ops)).encode())
    finally:
        writer.close()


def test_collector_supersede_needs_the_same_ref_even_with_author_principal(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS, agent={"approvers": ["tester"]})
    other_ref = "document:work-docs/specs/other.md"
    assert run_collect(instance, export([item(PATH, V1), item("specs/other.md", "Other.")]))[0] == 0
    mine, other = current(instance), current(instance, other_ref)

    def row(rid, ref, supersedes, kind="document"):
        return {"op": "add_source", "row": {"id": rid, "kind": kind, "title": "T", "source_date": "2026-10-08", "author_person_id": None,
                                            "content": "Changed text.", "external_ref": ref, "supersedes_id": supersedes}}

    # A different external_ref: held.
    held = _direct(instance, "workdocs", row("src-cap-x1", other_ref, mine))
    assert held.receipt["status"] == "held", held.receipt["error"]
    assert "supersedes_id needs a person's approval" in ro_rows(
        instance, "SELECT hold_reason FROM write_log WHERE change_id = ?", (held.receipt["change_id"],))[0][0]
    # A non-capture target with a matching ref: held.
    hand_ref = "document:work-docs/hand.md"
    _ok(_apply(instance, "tester", {"op": "add_source", "row": {"id": "src-hand-1", "kind": "document", "title": "Hand", "source_date": "2026-10-01",
                                                                "author_person_id": None, "content": "By hand.", "external_ref": hand_ref}}))
    assert _direct(instance, "workdocs", row("src-cap-x2", hand_ref, "src-hand-1")).receipt["status"] == "held"
    # A target in another namespace, with an id that starts src-cap-: held.
    _ok(_apply(instance, "tester", {"op": "add_source", "row": {"id": "src-cap-evil-1", "kind": "document", "title": "Evil", "source_date": "2026-10-01",
                                                                "author_person_id": None, "content": "Outside.", "external_ref": "document:elsewhere/x.md"}}))
    assert _direct(instance, "workdocs", row("src-cap-x3", "document:elsewhere/x.md", "src-cap-evil-1")).receipt["status"] == "held"
    # The same ref, inside the namespace: applied without a hold.
    assert _direct(instance, "workdocs", row("src-cap-x4", REF, mine)).receipt["status"] == "applied"
    # The principal's own agent may supersede across refs.
    assert _apply(instance, "tester", row("src-by-tester", hand_ref, other)).receipt["status"] == "applied"
    # known() ignores a non-capture source that shares a ref: capture adds a fresh chain.
    assert run_collect(instance, export([item(PATH, V1), item("hand.md", "Captured copy.")], fetched_at="2026-10-08T02:00:00Z"))[0] == 0
    hand_rows = sources(instance, hand_ref)
    assert len(hand_rows) == 1 and hand_rows[0][1] is None


# -- 58 to 60 ----------------------------------------------------------------------------------------------


def test_tombstoned_source_resend_is_a_noop(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    original = ro_rows(instance, "SELECT id, kind, title, uri, source_date, author_person_id, content, external_ref FROM sources WHERE id = ?", (v1,))[0]
    columns = ("id", "kind", "title", "uri", "source_date", "author_person_id", "content", "external_ref")
    resend = dict(zip(columns, original))
    con = context_connection(instance)
    try:
        con.execute("DROP TRIGGER sources_never_change")
        con.execute("UPDATE sources SET title = ?, content = ? WHERE id = ?",
                    (TOMBSTONE_TITLE, f"{TOMBSTONE_PREFIX}2026-10-10 by Alex Example: privacy request", v1))
        con.execute("CREATE TRIGGER sources_never_change BEFORE UPDATE ON sources\nBEGIN SELECT RAISE(ABORT, 'sources never change: add a new source with a new id'); END")
        con.commit()
    finally:
        con.close()
    assert _direct(instance, "workdocs", {"op": "add_source", "row": resend}).receipt["status"] == "applied"
    different = _direct(instance, "workdocs", {"op": "add_source", "row": dict(resend, content="Something else.")})
    assert different.receipt["status"] == "rejected"
    assert different.receipt["error"] == f"op 0 (add_source): source {v1} is redacted"
    dated = _direct(instance, "workdocs", {"op": "add_source", "row": dict(resend, source_date="2026-10-09")})
    assert dated.receipt["status"] == "rejected"
    # The capture plan emits nothing for the tombstoned ref.
    before = len(sources(instance))
    code, _p, _e = run_collect(instance, export([item(PATH, V1)], fetched_at="2026-10-08T03:00:00Z"))
    assert code == 0 and len(sources(instance)) == before
    assert TOMBSTONE_TITLE == "redacted" and TOMBSTONE_PREFIX == "redacted on "


def test_cited_source_shows_source_current_false(tmp_path: Path, clock: Clock) -> None:
    text = "We will ship the Pro plan in November. Seats are 25. The price is 49 AUD."
    instance, v1 = world(tmp_path, text, principal=True)
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, v1)))
    claim(instance, "bot", "clm-seats", "demo.seats", "25", "Seats are 25", v1)
    claim(instance, "bot", "clm-price", "demo.price", "49", "The price is 49 AUD", v1)
    now = "2026-10-09T00:00:00Z"

    def got(entity: str, key: str) -> dict:
        return reads.run_tool(instance, "get", {"entity": entity, "id": key}, now=now).structured

    assert "source_current" not in got("decision", "dec-ship")["source"]
    assert "source_current" not in got("fact", "demo.price")["source"]
    recapture(instance, "We will ship the Pro plan in November. Seats are 25. The price rose.", stamp="2026-10-08T01:00:00Z")
    # The decision keeps citing the old version; the price claim lost its quote and stays on it too.
    assert got("decision", "dec-ship")["source"]["source_current"] is False
    price = got("fact", "demo.price")["source"]
    assert price["source_current"] is False and list(price)[-1] == "source_current" and price["id"] == v1
    # The seats claim was carried, so its fact cites the current version.
    assert "source_current" not in got("fact", "demo.seats")["source"]
    # Any other row that cites the old source shows it as well.
    _ok(_apply(instance, "tester", upsert_op("upsert_signal", {"id": "sig-mine-1", "kind": "other", "subject_table": "facts",
                                                              "subject_id": "demo.seats", "basis": "observed"}, v1)))
    assert got("signal", "sig-mine-1")["source"]["source_current"] is False
    # The source itself has no cited source, and every other read is untouched.
    assert got("source", v1)["source"] is None


def test_benchmark_dataset_opens_and_closes_five_drift_signals(tmp_path: Path) -> None:
    from bench import harness

    dataset = Path(__file__).resolve().parents[1] / "bench" / "exampleco"
    instance = harness.build_instance(dataset, tmp_path)
    rows = ro_rows(instance, "SELECT id, status FROM signals WHERE kind = 'drift' ORDER BY id")
    assert len(rows) == 5 and all(row[0].startswith("sig-carry-") and row[1] == "resolved" for row in rows)
    assert ro_rows(instance, "SELECT count(*) FROM signals WHERE kind = 'conflict' AND status = 'open'") == [(10,)]


# -- Further carry-forward checks --------------------------------------------------------


def test_edits_that_keep_a_quote_out_open_one_drift_signal(tmp_path: Path, clock: Clock) -> None:
    instance, v1 = world(tmp_path)
    claim(instance, "bot", "clm-price", "demo.price", "49", PRICE, v1)
    for number in range(5):
        recapture(instance, f"The Pro plan price is not stated here. Edit {number}.", stamp=f"2026-10-08T0{number}:00:00Z")
    assert len(sources(instance, REF)) == 6
    assert len(drift(instance, "open")) == 1 and len(drift(instance)) == 1
    assert rows_of(instance, "clm-price")["status"] == "live"
    # A dismissed signal may come back on a later edit, because the claim is still unconfirmed.
    signal = drift(instance)[0][0]
    version = ro_rows(instance, "SELECT version FROM signals WHERE id = ?", (signal,))[0][0]
    _ok(_apply(instance, "tester", upsert_op("upsert_signal", {"id": signal, "status": "dismissed", "resolved_at": "2026-10-09T00:00:00Z"},
                                             current(instance), base=version)))
    recapture(instance, "Another edit without the price.", stamp="2026-10-08T07:00:00Z")
    assert len(drift(instance, "open")) == 1 and len(drift(instance)) == 2


def test_decision_drift_walks_the_version_chain(tmp_path: Path, clock: Clock) -> None:
    text = "We will ship the Pro plan in November. Seats are 25."
    instance, v1 = world(tmp_path, text, principal=True)
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, v1)))
    recapture(instance, text + " Billing is monthly.", stamp="2026-10-08T01:00:00Z")  # B keeps the quote
    assert drift(instance) == []
    recapture(instance, "The launch is postponed. Billing is monthly.", stamp="2026-10-08T02:00:00Z")  # C loses it
    v3 = current(instance)
    rows = drift(instance)
    assert len(rows) == 1 and rows[0][1:3] == ("decisions", "dec-ship") and rows[0][5] == v3
    assert ro_rows(instance, "SELECT version, source_id FROM decisions WHERE id = 'dec-ship'") == [(1, v1)]
    # A fourth version without the quote adds no second signal; re-citing the decision on a current source closes it.
    recapture(instance, "Still postponed.", stamp="2026-10-08T03:00:00Z")
    assert len(drift(instance, "open")) == 1
    _ok(_apply(instance, "tester", source_op("src-note", "We will ship the Pro plan in November.", kind="document", author="per-alex-example")))
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", DECISION, "src-note", base=1)))
    assert drift(instance, "open") == []
