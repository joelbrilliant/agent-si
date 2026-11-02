"""Migration 0006, the gate and the seven gated triggers, `brain check`. Every name and number here is invented."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from brain import backup, migrate, redact, store
from brain.applier import Applier, Unavailable
from tests.conftest import context_connection, ro_rows, source_op, upsert_op
from tests.phase2a import custom_instance
from tests.phase2b import K, M, OPERATOR, REASON, Seeded, gate_conn, hot_journal, refuses, seed
from tests.test_carry_forward import AGENTS, KEYS, claim
from tests.test_truth import Clock, _apply, _ok, clock  # noqa: F401

GATED = (
    "sources_never_change", "claims_content_fixed", "fact_history_no_update", "record_history_no_update",
    "collector_runs_no_update", "write_log_decided_is_final", "write_log_identity_is_fixed",
)
DELETE_TRIGGERS = (
    "sources_never_deleted", "claims_never_deleted", "fact_history_no_delete", "record_history_no_delete",
    "collector_runs_no_delete", "write_log_no_delete",
)
PINNED = {
    "sources_never_change": "499a9d2f764ee628",  # 0009: or a logged author change, every other column fixed
    "claims_content_fixed": "8570098cf5f86740",
    "fact_history_no_update": "9a0a3f067b091955",
    "record_history_no_update": "78a8ef51b843d6d5",  # 0008: the summary, and a record that links the source
    "collector_runs_no_update": "b964d6b40bb060a2",
    "write_log_decided_is_final": "6e18dfad550be028",
    "write_log_identity_is_fixed": "2134c194954d2e31",
}
TOMB = "redacted on 2026-10-12 by Alex Example: privacy request"


def objects(db: Path) -> dict[str, str]:
    con = sqlite3.connect(db)
    try:
        return {name: sql for name, sql in con.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'trigger'")}
    finally:
        con.close()


def test_migration_0006_objects(tmp_path: Path) -> None:
    instance = custom_instance(tmp_path)
    db = instance / "brain.sqlite3"
    con = sqlite3.connect(db)
    try:
        strict = dict(con.execute("SELECT name, strict FROM pragma_table_list WHERE schema = 'main'"))
        assert strict["redaction_gate"] == 1 and strict["redaction_packets"] == 1
        indexes = {row[0] for row in con.execute("SELECT name FROM sqlite_schema WHERE type = 'index'")}
        assert {"decisions_by_source", "sources_redacted"} <= indexes
        triggers = set(objects(db))
    finally:
        con.close()
    assert set(GATED) <= triggers and set(DELETE_TRIGGERS) <= triggers
    for table in ("redaction_gate", "redaction_packets"):
        assert {f"guard_{table}_insert", f"guard_{table}_update", f"guard_{table}_delete"} <= triggers
    assert migrate.latest_version() >= 7  # pinned: a floor, the schema the redaction guards arrived in
    assert store.missing_guards(store.connect_readonly(db)) == []


def test_schema_rows_match_a_fresh_store_at_6(tmp_path: Path) -> None:
    fresh = custom_instance(tmp_path / "fresh")
    old = tmp_path / "old" / "brain.sqlite3"
    assert migrate.migrate(old, create=True, upto=5) == (0, 5)
    assert migrate.migrate(old) == (5, migrate.latest_version())
    con_fresh, con_old = sqlite3.connect(fresh / "brain.sqlite3"), sqlite3.connect(old)
    try:
        rows_fresh = migrate.schema_rows(con_fresh)
        assert rows_fresh == migrate.schema_rows(con_old)
        print("SCHEMA ROWS", len(rows_fresh), "SQLite", sqlite3.sqlite_version)
        assert len(rows_fresh) >= 194  # pinned: a floor, so the comparison above is never vacuous; migrations only add
        assert migrate.schema_differences(con_old, migrate.latest_version()) == [] and migrate.schema_differences(con_fresh, migrate.latest_version()) == []
    finally:
        con_fresh.close()
        con_old.close()
    assert len(migrate.expected_schema(5)) == 183


def test_gated_trigger_text_is_pinned(tmp_path: Path) -> None:
    triggers = objects(custom_instance(tmp_path) / "brain.sqlite3")
    found = {name: hashlib.sha256(triggers[name].encode()).hexdigest()[:16] for name in GATED}
    assert found == PINNED


def held_scrubbed(raw: str) -> dict:
    """The exact scrubbed form of the fixture's held packet: its one op's body loses the quoted marker."""
    packet = json.loads(raw)
    packet["ops"][0]["row"]["body"] = 'Held note "[redacted]".'
    packet["redacted_ops"] = [0]
    return packet


def test_no_gate_no_update(tmp_path: Path, clock: Clock) -> None:
    """With no gate row each of the seven triggers refuses the redaction's exact form, with its existing message."""
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    change_id, packet = decided_row(instance, sid)
    scrubbed = json.loads(json.dumps(packet))
    mine = [i for i, op in enumerate(scrubbed["ops"]) if op.get("row", {}).get("id") == sid][0]
    scrubbed["ops"][mine]["row"].update(content="[redacted]", title="redacted", uri="")
    scrubbed["redacted_ops"] = [mine]
    con = context_connection(instance)
    try:
        assert "sources never change" in refuses(
            con, "UPDATE sources SET title = 'redacted', content = ?, uri = '', author_person_id = NULL WHERE id = ?", (TOMB, sid))
        assert "claims never change" in refuses(
            con, "UPDATE claims SET quote = '[redacted]', value_raw = '[redacted]', value_text = '[redacted]' WHERE id = 'clm-motto'")
        assert "append-only" in refuses(
            con, "UPDATE fact_history SET quote = '[redacted]', value_text = '[redacted]' WHERE source_id = ? AND value_type = 'text'", (sid,))
        assert "append-only" in refuses(con, "UPDATE record_history SET body = body || ' [redacted]' WHERE source_id = ?", (sid,))
        assert "append-only" in refuses(con, "UPDATE record_history SET body = '[redacted]' WHERE source_id = ?", (sid,))
        assert "append-only" in refuses(con, "UPDATE collector_runs SET error = 'redacted on 2026-10-12' WHERE id = 'run-work-docs-err1'")
        assert "final" in refuses(
            con, "UPDATE write_log SET error = 'redacted on 2026-10-12' WHERE status = 'rejected' AND instr(error, ?) > 0", (sid,))
        assert refuses(
            con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(scrubbed), change_id)
        ) in ("write_log: a decided change is final", "write_log: identity columns never change")
        held = ro_rows(instance, "SELECT change_id, packet_json FROM write_log WHERE status = 'held' AND instr(packet_json, ?) > 0", (sid,))[0]
        assert "identity" in refuses(con, "UPDATE write_log SET packet_json = '{}' WHERE change_id = ?", (held[0],))
        # a packet that only gains an empty redacted_ops is a packet change too
        for row_id in (change_id, held[0]):
            raw = ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (row_id,))[0][0]
            assert refuses(
                con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(dict(json.loads(raw), redacted_ops=[])), row_id)
            ) in ("write_log: a decided change is final", "write_log: identity columns never change")
        # a held row is outside write_log_decided_is_final, so the identity trigger alone refuses the exact scrubbed form
        assert "identity" in refuses(
            con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(held_scrubbed(held[1])), held[0]))
    finally:
        con.close()


def decided_row(instance: Path, sid: str) -> tuple[str, dict]:
    row = ro_rows(
        instance,
        "SELECT change_id, packet_json FROM write_log WHERE status = 'applied' AND instr(packet_json, ?) > 0 "
        "AND agent = 'workdocs' AND instr(packet_json, '\"op\":\"add_source\"') > 0 ORDER BY change_id LIMIT 1",
        (sid,),
    )[0]
    return row[0], json.loads(row[1])


def test_an_open_gate_is_not_enough(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-x", "kind": "note", "title": "Short", "body": "Short."}, "src-other")))
    claim(instance, "other", "clm-region", "demo.region", "au", "Region is au", "src-other")  # a history row of another source
    con = gate_conn(instance, sid)
    # sources
    base = "UPDATE sources SET title = 'redacted', content = ?, uri = ?, author_person_id = NULL{extra} WHERE id = ?"
    assert "sources never change" in refuses(con, base.format(extra=""), (TOMB, "x", sid))
    assert "sources never change" in refuses(con, base.format(extra=""), ("other", "", sid))
    assert "sources never change" in refuses(con, "UPDATE sources SET title = 'redacted', content = ?, uri = '' WHERE id = ?", (TOMB, sid))  # the author stays
    assert "sources never change" in refuses(con, base.format(extra=", source_date = '2026-01-01'"), (TOMB, "", sid))
    assert "sources never change" in refuses(con, base.format(extra=""), (TOMB, "", "src-other"))
    # claims
    assert "claims never change" in refuses(con, "UPDATE claims SET quote = 'x' WHERE id = 'clm-motto'")
    assert "claims never change" in refuses(con, "UPDATE claims SET quote = '[redacted]', key = 'demo.seats' WHERE id = 'clm-motto'")
    assert "claims never change" in refuses(con, "UPDATE claims SET value_text = 'https://redacted.invalid/' WHERE id = 'clm-motto'")
    assert "claims never change" in refuses(con, "UPDATE claims SET value_text = '[redacted]' WHERE id = 'clm-seats'")
    assert "claims never change" in refuses(con, "UPDATE claims SET quote = '[redacted]' WHERE id = 'clm-seats2'")
    # fact_history
    assert "append-only" in refuses(con, "UPDATE fact_history SET quote = 'x' WHERE source_id = ?", (sid,))
    assert "append-only" in refuses(con, "UPDATE fact_history SET quote = '[redacted]', version = 99 WHERE source_id = ? AND quote IS NOT NULL", (sid,))
    assert "append-only" in refuses(con, "UPDATE fact_history SET quote = '[redacted]' WHERE source_id = 'src-other'")
    # record_history
    assert "append-only" in refuses(con, "UPDATE record_history SET body = 'plain' WHERE source_id = ?", (sid,))
    assert "append-only" in refuses(con, "UPDATE record_history SET title = 'plain' WHERE source_id = ?", (sid,))
    assert "append-only" in refuses(con, "UPDATE record_history SET body = '[redacted] Jade approved the wire transfer ' || printf('%080d', 0) WHERE record_id = 'rec-plain'")
    assert "append-only" in refuses(con, "UPDATE record_history SET body = '[redacted]', status = 'archived' WHERE source_id = ?", (sid,))
    assert "append-only" in refuses(con, "UPDATE record_history SET body = '[redacted]' WHERE record_id = 'rec-x'")
    # collector_runs
    run = "UPDATE collector_runs SET error = ?{extra} WHERE id = 'run-work-docs-err1'"
    assert "append-only" in refuses(con, "UPDATE collector_runs SET error = 'redacted on 2026-10-12' WHERE error = ''")
    assert "append-only" in refuses(con, run.format(extra=""), ("redacted on 2026-10-11",))
    assert "append-only" in refuses(con, run.format(extra=", status = 'ok'"), ("redacted on 2026-10-12",))
    # write_log
    assert refuses(con, "UPDATE write_log SET intent = 'x' WHERE status = 'rejected'") in (
        "write_log: a decided change is final", "write_log: identity columns never change")
    assert "identity" in refuses(con, "UPDATE write_log SET payload_sha256 = ? WHERE status = 'held'", ("0" * 64,))
    change_id, packet = decided_row(instance, sid)
    for column, value in (("applied_seq", 999), ("status", "rejected"), ("intent", "other")):
        assert refuses(con, f"UPDATE write_log SET {column} = ? WHERE change_id = ?", (value, change_id)) in (
            "write_log: a decided change is final", "write_log: identity columns never change")
    ops = packet["ops"]
    for forged in (
        dict(packet, change_id="x-1", redacted_ops=[0]),
        dict(packet, ops=ops + ops[:1], redacted_ops=[0]),
        packet,  # unchanged ops, no redacted_ops key
    ):
        if forged is packet:
            forged = dict(packet, ops=[dict(op) for op in ops])
            forged["ops"][0]["row"] = dict(forged["ops"][0]["row"], content="[redacted]")
        message = refuses(con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(forged), change_id))
        assert "final" in message or "identity" in message
    other_id = ro_rows(instance, "SELECT change_id FROM write_log WHERE instr(packet_json, ?) = 0 AND status = 'applied' LIMIT 1", (sid,))[0][0]
    assert "final" in refuses(con, "UPDATE write_log SET error = 'redacted on 2026-10-12' WHERE change_id = ?", (other_id,))
    raw = ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (other_id,))[0][0]
    assert refuses(
        con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(dict(json.loads(raw), redacted_ops=[])), other_id)
    ) in ("write_log: a decided change is final", "write_log: identity columns never change")
    con.execute("ROLLBACK")
    con.close()


def test_near_miss_forms_are_refused(tmp_path: Path, clock: Clock) -> None:
    """With the gate open, a value one character off the redaction's exact form is refused by every trigger."""
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    con = gate_conn(instance, sid)
    for tomb in (TOMB + " ", TOMB.replace("redacted on", "Redacted on"), TOMB.replace("2026-10-12", "2026-10-13"), TOMB[:-1]):
        assert "sources never change" in refuses(
            con, "UPDATE sources SET title = 'redacted', content = ?, uri = '', author_person_id = NULL WHERE id = ?", (tomb, sid))
    assert "sources never change" in refuses(
        con, "UPDATE sources SET title = 'Redacted', content = ?, uri = '', author_person_id = NULL WHERE id = ?", (TOMB, sid))
    for token in ("[redacted] ", "[Redacted]", " [redacted]", "[redacted", "redacted"):
        assert "claims never change" in refuses(con, "UPDATE claims SET quote = ? WHERE id = 'clm-motto'", (token,))
        assert "claims never change" in refuses(con, "UPDATE claims SET value_raw = ? WHERE id = 'clm-motto'", (token,))
        assert "claims never change" in refuses(con, "UPDATE claims SET value_text = ? WHERE id = 'clm-motto'", (token,))
        assert "append-only" in refuses(con, "UPDATE fact_history SET quote = ? WHERE source_id = ? AND quote IS NOT NULL", (token, sid))
        assert "append-only" in refuses(con, "UPDATE fact_history SET value_text = ? WHERE source_id = ? AND value_type = 'text'", (token, sid))
    for url in ("https://redacted.invalid", "https://redacted.invalid/ ", "http://redacted.invalid/"):
        assert "claims never change" in refuses(con, "UPDATE claims SET value_text = ? WHERE id = 'clm-site'", (url,))
        assert "append-only" in refuses(con, "UPDATE fact_history SET value_text = ? WHERE source_id = ? AND value_type = 'url'", (url, sid))
    for body in ("[Redacted]", "[redacted ]", "REDACTED"):
        assert "append-only" in refuses(con, "UPDATE record_history SET body = ? WHERE source_id = ?", (body, sid))
        assert "append-only" in refuses(con, "UPDATE record_history SET title = ? WHERE source_id = ?", (body, sid))
    for error in ("redacted on 2026-10-13", "Redacted on 2026-10-12", "redacted on 2026-10-12 ", "redacted on 2026-10-1", ""):
        assert "append-only" in refuses(con, "UPDATE collector_runs SET error = ? WHERE id = 'run-work-docs-err1'", (error,))
        assert refuses(con, "UPDATE write_log SET error = ? WHERE status = 'rejected' AND instr(error, ?) > 0", (error, sid)) in (
            "write_log: a decided change is final", "write_log: identity columns never change")
    assert refuses(con, "UPDATE write_log SET hold_reason = 'x' WHERE status = 'rejected'") in (
        "write_log: a decided change is final", "write_log: identity columns never change")
    con.execute("ROLLBACK")
    con.close()


def test_a_forged_packet_json_is_refused(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    change_id, packet = decided_row(instance, sid)
    con = gate_conn(instance, sid)
    ops = packet["ops"]
    n = len(ops)
    mine = [i for i, op in enumerate(ops) if op.get("row", {}).get("id") == sid][0]

    def attempt(candidate: dict) -> str:
        return refuses(con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(candidate), change_id))

    wiped = dict(packet, ops=[{"op": "rebuild_search"} for _ in range(n)], redacted_ops=list(range(n)))
    assert attempt(wiped)  # every op replaced by rebuild_search, every index listed
    copy = json.loads(json.dumps(ops))
    copy[mine]["op"] = "upsert_record"
    copy[mine]["row"]["content"] = "[redacted]"
    assert attempt(dict(packet, ops=copy, redacted_ops=[mine]))  # one op changed to a different op
    assert attempt(dict(packet, agent="other", redacted_ops=[]))  # a changed agent
    others = [i for i in range(n) if i != mine]
    assert others
    copy = json.loads(json.dumps(ops))
    copy[others[0]]["row"]["items"] = 99
    assert attempt(dict(packet, ops=copy, redacted_ops=[others[0]]))  # an unrelated op listed and changed
    copy = json.loads(json.dumps(ops))
    copy[mine]["row"]["content"] = "[redacted]"
    assert attempt(dict(packet, ops=copy, redacted_ops={"0": 1}))  # redacted_ops that is an object
    assert attempt(dict(packet, ops=copy, redacted_ops=[n + 5]))  # an index outside the list
    # a held row (outside write_log_decided_is_final) with a changed top-level key, an index outside its ops and
    # an op swapped for another are refused by the identity trigger alone
    held = ro_rows(instance, "SELECT change_id, packet_json FROM write_log WHERE status = 'held' AND instr(packet_json, ?) > 0", (sid,))[0]
    held_packet = json.loads(held[1])
    scrubbed = held_scrubbed(held[1])
    for forged in (dict(scrubbed, agent="other"), dict(scrubbed, redacted_ops=[5]), dict(scrubbed, extra="x"),
                   dict(scrubbed, ops=[{"op": "rebuild_search"}], redacted_ops=[0])):
        assert refuses(con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(forged), held[0])) == "write_log: identity columns never change"
    # the exact scrubbed form of the same row passes
    good = json.loads(json.dumps(ops))
    good[mine]["row"].update(content="[redacted]", title="redacted", uri="")
    con.execute(
        "UPDATE write_log SET packet_json = ? WHERE change_id = ?",
        (store.canonical_json(dict(packet, ops=good, redacted_ops=[mine])), change_id),
    )
    con.execute("ROLLBACK")
    con.close()


def test_listed_ops_keep_their_row_and_source(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS)
    _ok(_apply(instance, "tester", source_op("src-s", f"Memo. Contact {M} for the vault. Nothing else.", kind="document")))
    _ok(_apply(instance, "tester", source_op("src-x", "Memo two. The vault was audited in October.", kind="document")))
    body = f'The vault contact is "Contact {M} for the vault".'
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-1", "kind": "note", "title": "Vault", "body": body}, "src-s")))
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-2", "kind": "note", "title": "Other", "body": "Plain."}, "src-x")))
    edit = _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-1", "body": body + " Audited in October."}, "src-x", base=1)))
    cid = edit.receipt["change_id"]
    raw = ro_rows(instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (cid,))[0][0]
    packet = json.loads(raw)
    con = gate_conn(instance, "src-s")
    con.execute("INSERT INTO redaction_packets (change_id) VALUES (?)", (cid,))

    def attempt(mutate) -> str:
        ops = json.loads(raw)["ops"]
        mutate(ops[0])
        candidate = dict(packet, ops=ops, redacted_ops=[0])
        return refuses(con, "UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(candidate), cid))

    assert attempt(lambda op: op["row"].update(id="rec-2")) == "write_log: identity columns never change"
    assert attempt(lambda op: op.update(source={"source_id": "src-s", "locator": "elsewhere"})) == "write_log: identity columns never change"
    assert attempt(lambda op: op.update(candidate={"source_id": "src-s"})) == "write_log: identity columns never change"
    ops = json.loads(raw)["ops"]
    ops[0]["row"]["body"] = 'The vault contact is "[redacted]". Audited in October.'
    con.execute(
        "UPDATE write_log SET packet_json = ? WHERE change_id = ?",
        (store.canonical_json(dict(packet, ops=ops, redacted_ops=[0])), cid),
    )
    con.execute("ROLLBACK")
    con.close()


def test_the_exact_forms_pass(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    con = gate_conn(instance, sid, date="2026-10-12", operator="Alex Example", reason="privacy request")
    tomb = "redacted on 2026-10-12 by Alex Example: privacy request"
    assert con.execute("UPDATE sources SET title = 'redacted', content = ?, uri = '', author_person_id = NULL WHERE id = ?", (tomb, sid)).rowcount == 1
    assert con.execute("UPDATE claims SET quote = '[redacted]', value_raw = '[redacted]', value_text = '[redacted]' WHERE id = 'clm-motto'").rowcount == 1
    assert con.execute("UPDATE claims SET quote = '[redacted]', value_raw = '[redacted]', value_text = 'https://redacted.invalid/' WHERE id = 'clm-site'").rowcount == 1
    assert con.execute("UPDATE claims SET status = 'rejected', resolution_source_id = source_id, resolution_locator = 'redacted on 2026-10-12' WHERE id = 'clm-seats'").rowcount == 1
    assert con.execute("UPDATE fact_history SET quote = '[redacted]' WHERE source_id = ? AND quote IS NOT NULL", (sid,)).rowcount >= 1
    assert con.execute("UPDATE fact_history SET value_text = '[redacted]' WHERE source_id = ? AND value_type = 'text'", (sid,)).rowcount >= 1
    assert con.execute("UPDATE record_history SET body = '[redacted]' WHERE source_id = ?", (sid,)).rowcount >= 1
    assert con.execute("UPDATE record_history SET title = '[redacted]' WHERE record_id = 'rec-vault'").rowcount >= 1
    assert con.execute("UPDATE collector_runs SET error = 'redacted on 2026-10-12' WHERE id = 'run-work-docs-err1'").rowcount == 1
    assert con.execute("UPDATE write_log SET error = 'redacted on 2026-10-12' WHERE status = 'rejected' AND instr(error, ?) > 0", (sid,)).rowcount >= 1
    change_id, packet = decided_row(instance, sid)
    mine = [i for i, op in enumerate(packet["ops"]) if op.get("row", {}).get("id") == sid][0]
    good = json.loads(json.dumps(packet))
    good["ops"][mine]["row"].update(content="[redacted]", title="redacted", uri="")
    good["redacted_ops"] = [mine]
    assert con.execute("UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(good), change_id)).rowcount == 1
    held = ro_rows(instance, "SELECT change_id, packet_json FROM write_log WHERE status = 'held' AND instr(packet_json, ?) > 0", (sid,))[0]
    assert con.execute("UPDATE write_log SET packet_json = ? WHERE change_id = ?", (store.canonical_json(held_scrubbed(held[1])), held[0])).rowcount == 1
    assert con.execute(
        "UPDATE write_log SET status = 'rejected', error = 'redacted on 2026-10-12', hold_reason = '', applied_seq = NULL WHERE change_id = ?", (held[0],)
    ).rowcount == 1
    con.execute("ROLLBACK")
    con.close()


def test_delete_triggers_are_never_gated(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    con = gate_conn(fixture.instance, fixture.source_id)
    for table in ("sources", "claims", "fact_history", "record_history", "collector_runs", "write_log"):
        message = refuses(con, f"DELETE FROM {table}")
        assert message
    con.execute("ROLLBACK")
    con.close()


def test_check_reports_a_non_empty_gate(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    con = gate_conn(instance, sid)
    con.execute("COMMIT")
    con.close()
    assert store.check_instance(instance)[0] == "redaction_gate is not empty: a redaction did not finish"
    with pytest.raises(Unavailable) as err:
        _apply(instance, "tester", source_op("src-new", "A note.", kind="document"))
    assert str(err.value) == "redaction_gate is not empty: run brain check"
    con = context_connection(instance)
    con.execute("DELETE FROM redaction_gate")
    cid = ro_rows(instance, "SELECT change_id FROM write_log LIMIT 1")[0][0]
    con.execute("INSERT INTO redaction_packets (change_id) VALUES (?)", (cid,))
    con.commit()
    con.close()
    assert "redaction_packets is not empty: a redaction did not finish" in store.check_instance(instance)
    with pytest.raises(Unavailable) as err:
        _apply(instance, "tester", source_op("src-new", "A note.", kind="document"))
    assert str(err.value) == "redaction_gate is not empty: run brain check"
    with pytest.raises(Unavailable):
        redact.run(instance, sid, REASON, OPERATOR)
    # the op's own refusal, reached with the precondition out of the way
    from brain import redact_ops

    app = Applier(instance, allow_reserved_agent=True)
    try:
        app.con.execute("BEGIN IMMEDIATE")
        app._ctx = 1
        op = {"op": "redact_source", "source_id": sid, "reason": REASON, "operator": OPERATOR}
        error, status, targets = redact_ops.redact_source(app, "0", op, "brain", "brain-redact-x-1", "2026-10-12T00:00:00Z")
        assert (error, status, targets) == ("op 0 (redact_source): redaction_gate is not empty: run brain check", "rejected", [])
        app._ctx = 0
        app.con.execute("ROLLBACK")
    finally:
        app.close()


def mutate_trigger(instance: Path, name: str, how: str) -> None:
    con = sqlite3.connect(instance / "brain.sqlite3", isolation_level=None)
    try:
        sql = con.execute("SELECT sql FROM sqlite_schema WHERE name = ?", (name,)).fetchone()[0]
        con.execute(f"DROP TRIGGER {name}")
        if how == "weaken":
            con.execute(sql.replace("WHEN ", "WHEN 0 AND ", 1))
        elif how == "whitespace":
            con.execute(sql.replace("BEGIN SELECT", "BEGIN  SELECT"))
        elif how == "extra":
            con.execute(sql)
            con.execute(sql.replace(name, name + "_copy", 1))
        elif how == "drop":
            pass
    finally:
        con.close()


@pytest.mark.parametrize("name", GATED)
def test_check_fails_on_a_changed_trigger(tmp_path: Path, name: str) -> None:
    for how, expected in (
        ("weaken", f"schema: trigger {name} differs from the engine's schema"),
        ("whitespace", f"schema: trigger {name} differs from the engine's schema"),
        ("drop", f"schema: trigger {name} is missing"),
        ("extra", f"schema: trigger {name}_copy is not in the engine's schema"),
    ):
        instance = custom_instance(tmp_path / how)
        mutate_trigger(instance, name, how)
        problems = store.check_instance(instance)
        assert expected in problems, (how, problems)
        if how in ("weaken", "whitespace", "extra"):
            assert [p for p in problems if p.startswith("schema:")] == [expected]


def test_seven_differences_give_five_lines(tmp_path: Path) -> None:
    instance = custom_instance(tmp_path)
    for name in GATED:
        mutate_trigger(instance, name, "whitespace")
    schema_lines = [p for p in store.check_instance(instance) if p.startswith("schema:")]
    assert len(schema_lines) == 6
    assert schema_lines[-1] == "schema: and 2 more differences"


def test_check_compares_an_older_store_with_its_own_version(tmp_path: Path) -> None:
    instance = custom_instance(tmp_path)
    db = instance / "brain.sqlite3"
    old = tmp_path / "v5" / "brain.sqlite3"
    migrate.migrate(old, create=True, upto=5)
    import shutil

    shutil.copyfile(instance / "brain.toml", old.parent / "brain.toml")
    shutil.copyfile(instance / "ROUTER.md", old.parent / "ROUTER.md")
    problems = store.check_instance(old.parent)
    assert f"store is at version 5, engine expects {migrate.latest_version()}" in problems
    assert not [p for p in problems if p.startswith("schema:")]


def test_backup_and_restore_refuse_a_changed_trigger(tmp_path: Path) -> None:
    instance = custom_instance(tmp_path)
    good = backup.backup(instance, tmp_path / "out")
    mutate_trigger(instance, "sources_never_change", "weaken")
    with pytest.raises(backup.Refused) as err:
        backup.backup(instance, tmp_path / "out2")
    assert str(err.value).startswith("backup refused: brain check found problems: schema: trigger sources_never_change")
    # a hostile bundle: the changed store in a bundle with a consistent manifest cannot be restored
    # (restore compares the schema before it migrates); the good one restores.
    ok = backup.restore(good["bundle"], tmp_path / "restored")
    assert ok["status"] == "restored"
    import gzip
    import io
    import tarfile

    hostile = tmp_path / "hostile.tar.gz"
    with tarfile.open(good["bundle"], "r:gz") as source, tarfile.open(hostile, "w:gz") as target:
        for member in source:
            data = source.extractfile(member).read()
            if member.name == "brain.sqlite3":
                work = tmp_path / "work.sqlite3"
                work.write_bytes(data)
                con = sqlite3.connect(work, isolation_level=None)
                sql = con.execute("SELECT sql FROM sqlite_schema WHERE name = 'sources_never_change'").fetchone()[0]
                con.execute("DROP TRIGGER sources_never_change")
                con.execute(sql.replace("WHEN NOT EXISTS", "WHEN 0 AND NOT EXISTS"))
                con.close()
                data = work.read_bytes()
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            target.addfile(info, io.BytesIO(data))
    with pytest.raises(Exception) as err:
        backup.restore(hostile, tmp_path / "restored2")
    assert "restore refused" in str(err.value)


def test_indexes_serve_their_queries(tmp_path: Path) -> None:
    from brain import applier

    instance = custom_instance(tmp_path)
    con = sqlite3.connect(instance / "brain.sqlite3")
    try:
        plan = " ".join(row[3] for row in con.execute("EXPLAIN QUERY PLAN " + applier.DECISIONS_BY_CHAIN_SQL.replace("?", "'src-x'")))
        assert "SEARCH d USING INDEX decisions_by_source" in plan, plan
        plan = " ".join(row[3] for row in con.execute("EXPLAIN QUERY PLAN SELECT id FROM sources WHERE title = 'redacted'"))
        assert "sources_redacted" in plan, plan
    finally:
        con.close()


def test_gate_table_checks(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    con = context_connection(instance)
    insert = "INSERT INTO redaction_gate (source_id, redacted_on, tombstone) VALUES (?, ?, ?)"
    for values in (
        (sid, "2026-10-12", "redacted on 2026-10-11 by Alex: x"),  # another date
        (sid, "2026-10-12", "redacted on 2026-10-12 Alex: x"),  # no ' by '
        (sid, "2026-10-12", "gone"),
        (sid, "2026-13-45", "redacted on 2026-13-45 by Alex: x"),  # not a date
        (sid, "12 Oct 2026", "redacted on 12 Oct 2026 by Alex: x"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(insert, values)
    con.execute(insert, (sid, "2026-10-12", "redacted on 2026-10-12 by Alex: x"))
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(insert, (sid, "2026-10-12", "redacted on 2026-10-12 by Alex: y"))
    con.rollback()
    con.close()


def test_check_reports_a_hot_journal(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    hot_journal(instance / "brain.sqlite3")
    assert (instance / "brain.sqlite3-journal").exists()
    problems = store.check_instance(instance)
    assert problems == ["store has a hot journal: run brain redact <id> again, or any brain write, to recover it"]
    with pytest.raises((backup.Refused, Unavailable)):
        backup.backup(instance, tmp_path / "out")
    (instance / "REDACTING").write_text(json.dumps({"source_id": fixture.source_id}))
    with pytest.raises(backup.Refused) as err:
        backup.backup(instance, tmp_path / "out")
    assert str(err.value) == f"backup refused: a redaction of {fixture.source_id} did not finish; run brain redact {fixture.source_id} again"
    assert "a redaction did not finish: run brain redact " + fixture.source_id + " again" in store.check_instance(instance)
