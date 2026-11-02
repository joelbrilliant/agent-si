"""The redaction fixture: one instance with a marker seeded into every
place a redaction scrubs, eight places it keeps, and three file places. Every name and number is invented."""
from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import NamedTuple

from brain import backup, marks, scrub, store
from brain.applier import Applier
from tests.conftest import context_connection, make_packet, ro_rows, source_op, upsert_op
from tests.phase2a import custom_instance, export, item, run_collect, sources
from tests.test_carry_forward import AGENTS, KEYS, claim
from tests.test_truth import Clock, _apply, _clm, _ok

M = "Zq7MarkerAlexExampleSecret0412555019xYzW"  # 40 characters
K = "Kp4ControlKeptAlexExample7731"  # 29 characters, in places a redaction keeps, never in the source text
assert len(M) == 40 and len(K) == 29
PATH = "vault/notes.md"
REF = f"document:work-docs/{PATH}"
TEXT = (
    f'Contact "{M}" for the vault. The Pro plan includes 25 seats. The Pro plan costs 49 AUD a seat. '
    f'Docs live at https://example.com/{M}/start and nothing else matters here. Jade said "Contact {M} for the vault" today.'
)
KEYS2 = KEYS + [{"key": "demo.site", "value_type": "url"}]
REASON = "privacy request 2026-11-02"
OPERATOR = "Alex Example"
SPAN_SEEDS = (M, K)


def windows(marker: str = M) -> list[bytes]:
    """The 40-character marker and every overlapping 12-character window of it, exact and lower case."""
    out = []
    for text in (marker, marker.lower()):
        out.append(text.encode())
        out += [text[i : i + 12].encode() for i in range(len(text) - 11)]
    return sorted(set(out))


def find_in(blob: bytes) -> list[bytes]:
    return [w for w in windows() if w in blob]


def scan_instance(instance: Path) -> dict[str, list[bytes]]:
    """Every file under the instance, and every member of every bundle, searched for the marker's strings."""
    hits: dict[str, list[bytes]] = {}
    for path in sorted(Path(instance).rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        found = find_in(data)
        if path.name.endswith(".tar.gz"):
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                for member in tar:
                    body = tar.extractfile(member).read()
                    found += [w for w in find_in(body) if w not in found]
        if found:
            hits[str(path.relative_to(instance))] = found
    return hits


def bundle_members_hit(path: Path) -> list[bytes]:
    found: list[bytes] = []
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            found += [w for w in find_in(tar.extractfile(member).read()) if w not in found]
    return found


# column key (as in scrub.DISPOSITION) -> (SQL counting rows that hold the marker, uses the marker as its parameter)
PLACES: dict[str, str] = {
    "sources.title": "SELECT count(*) FROM sources WHERE instr(title, ?) > 0",
    "sources.content": "SELECT count(*) FROM sources WHERE instr(content, ?) > 0",
    "sources.uri": "SELECT count(*) FROM sources WHERE instr(uri, ?) > 0",
    "sources.author_person_id": "SELECT count(*) FROM sources WHERE id GLOB 'src-cap-*' AND author_person_id IS NOT NULL AND ? <> ''",
    "claims.quote": "SELECT count(*) FROM claims WHERE instr(quote, ?) > 0",
    "claims.value_raw": "SELECT count(*) FROM claims WHERE instr(value_raw, ?) > 0",
    "claims.value_text": "SELECT count(*) FROM claims WHERE instr(value_text, ?) > 0",
    "facts.value_text": "SELECT count(*) FROM facts WHERE instr(value_text, ?) > 0",
    "facts.quote": "SELECT count(*) FROM facts WHERE instr(quote, ?) > 0",
    "fact_history.value_text": "SELECT count(*) FROM fact_history WHERE instr(value_text, ?) > 0",
    "fact_history.quote": "SELECT count(*) FROM fact_history WHERE instr(quote, ?) > 0",
    "decisions.title": "SELECT count(*) FROM decisions WHERE instr(title, ?) > 0",
    "decisions.detail": "SELECT count(*) FROM decisions WHERE instr(detail, ?) > 0",
    "decisions.quote": "SELECT count(*) FROM decisions WHERE instr(quote, ?) > 0",
    "records.title": "SELECT count(*) FROM records WHERE instr(title, ?) > 0",
    "records.body": "SELECT count(*) FROM records WHERE instr(body, ?) > 0",
    "record_history.title": "SELECT count(*) FROM record_history WHERE instr(title, ?) > 0",
    "record_history.body": "SELECT count(*) FROM record_history WHERE instr(body, ?) > 0",
    "records.summary": "SELECT count(*) FROM records WHERE instr(summary, ?) > 0",
    "record_history.summary": "SELECT count(*) FROM record_history WHERE instr(summary, ?) > 0",
    "actions.title": "SELECT count(*) FROM actions WHERE instr(title, ?) > 0",
    "actions.detail": "SELECT count(*) FROM actions WHERE instr(detail, ?) > 0",
    "actions.next_step": "SELECT count(*) FROM actions WHERE instr(next_step, ?) > 0",
    "actions.close_reason": "SELECT count(*) FROM actions WHERE instr(close_reason, ?) > 0",
    "checklist_items.title": "SELECT count(*) FROM checklist_items WHERE instr(title, ?) > 0",
    "checklist_items.detail": "SELECT count(*) FROM checklist_items WHERE instr(detail, ?) > 0",
    "projects.title": "SELECT count(*) FROM projects WHERE instr(title, ?) > 0",
    "projects.description": "SELECT count(*) FROM projects WHERE instr(description, ?) > 0",
    "projects.goal": "SELECT count(*) FROM projects WHERE instr(goal, ?) > 0",
    "signals.detail": "SELECT count(*) FROM signals WHERE instr(detail, ?) > 0",
    "meetings.title": "SELECT count(*) FROM meetings WHERE instr(title, ?) > 0",
    "threads.subject": "SELECT count(*) FROM threads WHERE instr(subject, ?) > 0",
    "unchecked_text.sentences": "SELECT count(*) FROM unchecked_text WHERE instr(sentences, ?) > 0",
    "write_log.packet_json": "SELECT count(*) FROM write_log WHERE instr(packet_json, ?) > 0",
    "write_log.error": "SELECT count(*) FROM write_log WHERE instr(error, ?) > 0",
    "write_log.hold_reason": "SELECT count(*) FROM write_log WHERE instr(hold_reason, ?) > 0",
    "collector_runs.error": "SELECT count(*) FROM collector_runs WHERE instr(error, ?) > 0",
}
# two places that are not columns of the inventory
EXTRA_PLACES: dict[str, str] = {
    "search_index": "SELECT count(*) FROM search_index WHERE instr(body, ?) > 0 OR instr(title, ?) > 0",
    "records.edited_by_other": "SELECT count(*) FROM records WHERE id = 'rec-vault' AND source_id = 'src-other' AND instr(body, ?) > 0",
}
# the eight places a redaction keeps, seeded with the second marker
CONTROLS: dict[str, str] = {
    "claims.scope": "SELECT count(*) FROM claims WHERE instr(scope, ?) > 0",
    "claims.locator": "SELECT count(*) FROM claims WHERE instr(locator, ?) > 0",
    "write_log.intent": "SELECT count(*) FROM write_log WHERE instr(intent, ?) > 0",
    "checklist_items.section": "SELECT count(*) FROM checklist_items WHERE instr(section, ?) > 0",
    "checklist_items.external_ref": "SELECT count(*) FROM checklist_items WHERE instr(external_ref, ?) > 0",
    "people.display_name": "SELECT count(*) FROM people WHERE instr(display_name, ?) > 0",
    "systems.name": "SELECT count(*) FROM systems WHERE instr(name, ?) > 0",
    "records.body(unquoted)": "SELECT count(*) FROM records WHERE instr(body, ?) > 0",
}


def count_place(instance: Path, sql: str, marker: str = M) -> int:
    params = (marker,) * sql.count("?")
    return ro_rows(instance, sql, params)[0][0]


def places_before(instance: Path) -> dict[str, int]:
    return {key: count_place(instance, sql) for key, sql in {**PLACES, **EXTRA_PLACES}.items()}


def controls_now(instance: Path) -> dict[str, int]:
    return {key: count_place(instance, sql, K) for key, sql in CONTROLS.items()}


def marker_is_unique() -> None:
    """None of the marker's scanned strings occurs in any other text the fixture writes."""
    others = [
        K, REASON, OPERATOR, PATH, REF, "The Pro plan includes 25 seats", "The Pro plan costs 49 AUD a seat",
        "Vault review", "Memo", "work-docs", "privacy request",
    ]
    for text in others:
        for window in windows():
            assert window.decode() not in text and window.decode() not in text.lower()


class Seeded(NamedTuple):
    instance: Path
    source_id: str
    outside: Path
    held: str  # change id of the held packet that cites the source
    held_other: str  # change id of a held packet that does not


def seed(tmp_path: Path, clock: Clock, *, bundles: bool = True) -> Seeded:
    marker_is_unique()
    agents = {**AGENTS, "meta": {"kind": "agent", "operations": ["upsert_thread"]}}
    instance = custom_instance(tmp_path, agents=agents, fact_keys=KEYS2)
    doc = export(
        [item(PATH, TEXT, date="2026-10-07", from_principal=True, uri=f"https://example.com/{M}/page", title=f"Notes {M}")]
    )
    doc["mark"] = marks.sign(marks.ensure_key(instance), doc)
    code, payload, err = run_collect(instance, doc)
    assert code == 0, (payload, err)
    sid = sources(instance, REF)[-1][0]
    quote = f"Contact {M} for the vault"
    claim(instance, "bot", "clm-motto", "demo.motto", M, quote, sid)
    claim(instance, "bot", "clm-site", "demo.site", f"https://example.com/{M}/start", f"Docs live at https://example.com/{M}/start", sid)
    claim(instance, "bot", "clm-seats", "demo.seats", "25", "The Pro plan includes 25 seats", sid)
    # a second source that agrees on seats, so that fact keeps a live claim
    _ok(_apply(instance, "tester", source_op("src-other", "The Pro plan includes 25 seats. Region is au.", kind="document")))
    claim(instance, "other", "clm-seats2", "demo.seats", "25", "The Pro plan includes 25 seats", "src-other")
    dec = {
        "id": "dec-vault", "title": f'Keep "{M}" private', "detail": f'See "{M}" in the vault.', "quote": quote,
        "decided_on": "2026-10-07", "decided_by_person_id": "per-alex-example", "status": "in_effect", "project_id": "general",
    }
    _ok(_apply(instance, "tester", upsert_op("upsert_decision", dec, sid)))
    rec = {"id": "rec-vault", "kind": "note", "title": f'Contact "{M}"', "body": f'The vault contact is "{M}".',
           "summary": f'Contact is "{M}".'}
    _ok(_apply(instance, "tester", upsert_op("upsert_record", rec, sid)))
    rec2 = {"id": "rec-vault", "body": f'The vault contact is "{M}". Rotate it yearly.'}
    _ok(_apply(instance, "tester", upsert_op("upsert_record", rec2, sid, base=1)))
    act = {
        "id": "act-vault", "title": f'Rotate "{M}" code', "detail": f'Code is "{M}".', "next_step": f'Ask for "{M}" again.',
        "close_reason": f'Closed when "{M}" rotated.', "status": "open", "status_basis": "reported",
        "project_id": "general", "last_verified_at": "2026-10-07T00:00:00Z",
    }
    _ok(_apply(instance, "tester", upsert_op("upsert_action", act, sid)))
    chk = {
        "id": "chk-vault", "project_id": "general", "title": f'Vault "{M}"', "section": f"Section {K}",
        "external_ref": f"ref {K}", "detail": f'Check "{M}".', "state": "not_started", "state_basis": "reported",
        "last_verified_at": "2026-10-07T00:00:00Z",
    }
    _ok(_apply(instance, "tester", upsert_op("upsert_checklist_item", chk, sid)))
    proj = {"id": "vault-project", "kind": "project", "title": f'Vault "{M}"', "description": f'Holds "{M}".', "goal": f'Never share "{M}".'}
    _ok(_apply(instance, "tester", upsert_op("upsert_project", proj, sid)))
    sig = {"id": "sig-vault", "kind": "other", "subject_table": "records", "subject_id": "rec-vault", "basis": "observed", "detail": f'Check "{M}".'}
    _ok(_apply(instance, "tester", upsert_op("upsert_signal", sig, sid)))
    cal = {"id": "thr-cal", "channel": "calendar", "account": "work", "external_id": "evt-1", "subject": "Vault review"}
    _ok(_apply(instance, "meta", upsert_op("upsert_thread", cal, sid, locator="event evt-1")))
    mtg = {
        "id": "mtg-vault", "thread_id": "thr-cal", "title": f"Review {M} vault", "starts_at": "2026-10-20T01:00:00Z",
        "ends_at": "2026-10-20T02:00:00Z", "status": "confirmed",
    }
    _ok(_apply(instance, "tester", upsert_op("upsert_meeting", mtg, sid)))
    thr = {"id": "thr-vault", "channel": "email", "account": "work", "external_id": "t-18f2", "subject": f"Re: {M} vault"}
    _ok(_apply(instance, "meta", upsert_op("upsert_thread", thr, sid, locator="thread t-18f2")))
    # held packets from the careful agent: one cites the source, one does not
    held = _apply(instance, "careful", upsert_op("upsert_record", {"id": "rec-held", "kind": "note", "title": "Held", "body": f'Held note "{M}".'}, sid))
    assert held.receipt["status"] == "held", held.receipt
    held_other = _apply(instance, "careful", upsert_op("upsert_record", {"id": "rec-h2", "kind": "note", "title": "Other", "body": "Plain note."}, "src-other"))
    assert held_other.receipt["status"] == "held", held_other.receipt
    # a refused packet whose error holds a gate snippet
    bad = _apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-bad", "kind": "note", "title": "Bad", "body": f'He wrote "contact {M} for the wrong vault ever".'}, sid))
    assert bad.receipt["status"] == "rejected" and "evidence gate" in bad.receipt["error"], bad.receipt
    # a collector run whose error names the source
    run_op = {
        "op": "record_collector_run",
        "row": {
            "id": "run-work-docs-err1", "collector": "work-docs", "started_at": "2026-10-08T00:00:00Z",
            "finished_at": "2026-10-08T00:00:05Z", "status": "error", "items": 1, "input_sha256": "",
            "error": f'packet workdocs-1 was rejected: op 0 (upsert_record): evidence gate: quote "contact {M} for" is not in the cited source {sid}.',
        },
    }
    cap = Applier(instance, collect=True)
    try:
        _ok(cap.apply(json.dumps(make_packet("workdocs", run_op)).encode()))
    finally:
        cap.close()
    # an unchecked sentence (a legacy import): attribution to the principal with the source's own words
    imp = {"id": "rec-legacy", "kind": "note", "title": "Legacy", "body": f'Alex said "Contact {M} for the vault" but "this other quote is not in the source at all".'}
    _ok(_apply(instance, "importer", upsert_op("upsert_record", imp, sid)))
    # SR1: the record is edited by another source, so its source_id is no longer in the chain
    rec3 = {"id": "rec-vault", "body": f'The vault contact is "{M}". Rotate it yearly. Reviewed in October by the audit team.'}
    _ok(_apply(instance, "tester", upsert_op("upsert_record", rec3, "src-other", base=2)))
    # the eight kept controls
    _ok(_apply(instance, "bot", _clm("clm-ctl", "demo.seats", "25", "The Pro plan includes 25 seats", sid, scope=K[:40], locator=f"para {K}")))
    _ok(_apply(instance, "tester", {"op": "upsert_person", "row": {"id": "per-ctl", "display_name": f"Pat {K}"}, "source": {"source_id": sid, "locator": "p"}, "base": None}))
    _ok(_apply(instance, "tester", {"op": "upsert_system", "row": {"id": "sys-ctl", "name": f"System {K}", "kind": "saas"}, "source": {"source_id": sid, "locator": "p"}, "base": None}))
    _ok(_apply(instance, "tester", upsert_op("upsert_record", {"id": "rec-plain", "kind": "note", "title": "Plain", "body": f"Control copy {K} stays."}, sid)))
    ctl = Applier(instance)
    try:
        packet = make_packet(
            "tester",
            upsert_op("upsert_signal", {"id": "sig-ctl", "kind": "other", "subject_table": "records", "subject_id": "rec-plain", "basis": "observed", "detail": "control"}, sid),
            intent=f"note about {K}",
        )
        _ok(ctl.apply(json.dumps(packet).encode()))
    finally:
        ctl.close()
    # no guard writes the marker into a hold reason, so it is set by hand
    hc = context_connection(instance)
    try:
        hc.execute(
            "UPDATE write_log SET hold_reason = ? WHERE change_id = ?",
            (f"conflicts with {sid}: Contact {M} for the vault", held.receipt["change_id"]),
        )
        hc.commit()
    finally:
        hc.close()
    # the three file places: an export file in the inbox and two bundles in backups/
    inbox = instance / "fetch" / "inbox" / "work-docs"
    inbox.mkdir(parents=True)
    (inbox / "20261008T000000Z-01.json").write_bytes(store.canonical_json(export([item(PATH, TEXT)])).encode())
    outside = tmp_path / "elsewhere"
    if bundles:
        out = instance / "backups"
        backup.backup(instance, out, "pre-update")
        clock.at("2026-10-09T00:00:01Z")
        backup.backup(instance, out, "")
        clock.at("2026-10-09T00:00:02Z")
        backup.backup(instance, outside, "")
    return Seeded(instance, sid, outside, held.receipt["change_id"], held_other.receipt["change_id"])


def redact_now(fixture: Seeded, **kwargs):
    from brain import redact

    return redact.run(fixture.instance, fixture.source_id, kwargs.pop("reason", REASON), kwargs.pop("operator", OPERATOR), **kwargs)


# -- helpers shared by the redaction tests ------------------------------------------------------------

def gate_conn(instance: Path, sid: str, date: str = "2026-10-12", operator: str = OPERATOR, reason: str = "privacy request"):
    """A guarded connection inside an open transaction with a gate row for `sid`, as the op holds it."""
    con = context_connection(instance)
    con.isolation_level = None
    con.execute("BEGIN")
    con.execute(
        "INSERT INTO redaction_gate (source_id, redacted_on, tombstone) VALUES (?, ?, ?)",
        (sid, date, f"redacted on {date} by {operator}: {reason}"),
    )
    return con


def refuses(con, sql: str, params=()) -> str:
    import sqlite3

    import pytest

    with pytest.raises(sqlite3.IntegrityError) as err:
        con.execute(sql, params)
    return str(err.value)


def hot_journal(db: Path) -> None:
    """Leave a hot rollback journal beside the store, as a writer killed mid-transaction does."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        f"""
        import sqlite3, os
        con = sqlite3.connect({str(db)!r}, isolation_level=None)
        con.execute("PRAGMA cache_size = 1")
        con.execute("BEGIN IMMEDIATE")
        con.execute("CREATE TABLE junk (a)")
        for i in range(2000):
            con.execute("INSERT INTO junk VALUES (?)", ("x" * 2000,))
        os._exit(0)
        """
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def redact_packet(source_id: str, reason: str = REASON, operator: str = OPERATOR, n: int = 1) -> dict:
    import hashlib

    stem = "brain-redact-" + hashlib.sha256(source_id.encode()).hexdigest()[:20]
    return {
        "packet_version": 1,
        "change_id": f"{stem}-{n}",
        "agent": "brain",
        "intent": f"redact source {source_id}",
        "ops": [{"op": "redact_source", "source_id": source_id, "reason": reason, "operator": operator}],
    }


def redact_op(instance: Path, source_id: str, reason: str = REASON, operator: str = OPERATOR, n: int = 1):
    """Run only the redact_source op, through the applier, as brain redact does (no VACUUM, no bundles)."""
    app = Applier(instance, allow_reserved_agent=True)
    try:
        return app.apply(json.dumps(redact_packet(source_id, reason, operator, n)).encode())
    finally:
        app.close()


def rows(instance: Path, sql: str, params=()):
    return ro_rows(instance, sql, params)


def row_dict(instance: Path, sql: str, params=()) -> dict | None:
    import sqlite3

    con = store.connect_readonly(Path(instance) / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        found = con.execute(sql, params).fetchone()
        return None if found is None else dict(found)
    finally:
        con.close()
