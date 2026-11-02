"""The pure helpers of brain.scrub.
Every name and number here is invented."""
from __future__ import annotations

import copy
import hashlib
import random
import re
import sqlite3
from pathlib import Path

import pytest

from brain import evidence_gate, schemas, scrub, store
from tests.phase2b import M, seed
from tests.test_truth import Clock, clock  # noqa: F401

SRC = f"Contact {M} for the vault. Alex said yes. The Pro plan includes 25 seats."
KEY = scrub.source_key(SRC)
SID = "src-cap-aaaa-bbbb"


def test_tokens_and_tombstone_text() -> None:
    assert (scrub.REDACTED, scrub.REDACTED_URL) == ("[redacted]", "https://redacted.invalid/")
    assert (scrub.TOMBSTONE_TITLE, scrub.TOMBSTONE_PREFIX) == ("redacted", "redacted on ")
    assert scrub.tombstone_text("2026-10-12", "Alex Example", "privacy request") == "redacted on 2026-10-12 by Alex Example: privacy request"
    assert scrub.redaction_error("2026-10-12") == "redacted on 2026-10-12"
    assert [scrub.value_token(t) for t in ("text", "enum", "url", "int", "number", "date", "bool")] == [
        "[redacted]", "[redacted]", "https://redacted.invalid/", None, None, None, None]
    own = "redacted on elsewhere"
    agent_source = {"title": "redacted", "content": own, "content_sha256": store.sha256_text(own)}
    assert scrub.is_tombstone(agent_source) is False  # an agent's own source titled redacted
    real = {"title": "redacted", "content": "redacted on 2026-10-12 by A: b", "content_sha256": store.sha256_text("the removed text")}
    assert scrub.is_tombstone(real) is True
    assert scrub.is_tombstone(dict(real, title="Redacted")) is False
    assert scrub.is_tombstone(dict(real, content="gone on 2026-10-12")) is False
    from brain import applier

    assert (applier.TOMBSTONE_TITLE, applier.TOMBSTONE_PREFIX) == ("redacted", "redacted on ")


def sp(text, keys=(KEY,), limit=None):
    return scrub.scrub_spans(text, list(keys), limit)


def test_scrub_spans(monkeypatch) -> None:
    q = f"Contact {M} for the vault"
    assert sp(f'He wrote "{q}" today.') == 'He wrote "[redacted]" today.'
    assert sp(f"He wrote “{q}” today.") == "He wrote “[redacted]” today."
    assert sp(f"He wrote '{q}' today.") == "He wrote '[redacted]' today."
    assert sp(f"He wrote ‘{q}’ today.") == "He wrote ‘[redacted]’ today."
    assert sp(f'He wrote "{q.upper()}" today.', [scrub.source_key(q.upper())]) == 'He wrote "[redacted]" today.'
    assert sp(f'He wrote "{q}" today.') == sp(f'He wrote "{q}" today.'.replace("Contact", "Contact"))
    # a quote wrapped over a line break still matches, and the line break goes with it
    wrapped = f'He wrote "Contact {M}\nfor the vault" today.'
    assert sp(wrapped) == 'He wrote "[redacted]" today.'
    # a one-word span of the source is scrubbed
    assert sp('He named "yes" to it.') == 'He named "[redacted]" to it.'
    # an apostrophe inside a span
    other = scrub.source_key("It's the vault key that matters.")
    assert sp('See "It\'s the vault key" here.', [other]) == 'See "[redacted]" here.'
    # a span that is not in the source is untouched, and the same words outside quote marks too
    assert sp('He wrote "something else entirely" today.') == 'He wrote "something else entirely" today.'
    assert sp(f"He wrote {q} today.") == f"He wrote {q} today."
    assert sp(f'He wrote "{q}" and later {q} again.') == f'He wrote "[redacted]" and later {q} again.'
    # edge spaces stay
    assert sp(f'He wrote " {q} " today.') == 'He wrote " [redacted] " today.'
    # a result longer than the limit becomes the token
    title = 'Why "yes" now ' + "x" * 70
    assert len(title) <= 90 and len(sp(title, limit=90)) <= 90
    assert sp('"yes"', limit=2) == "[redacted]"
    assert sp(title, limit=len(title) + 7) == sp(title)  # room for the token: the text is kept, scrubbed
    # idempotent
    once = sp(f'He wrote "{q}" and "yes".')
    assert sp(once) == once
    # it uses the gate's quoting rules
    monkeypatch.setattr(evidence_gate, "quoted_spans", lambda sentence: [])
    assert sp(f'He wrote "{q}" today.') == f'He wrote "{q}" today.'


def test_scrub_spans_use_every_chain_key() -> None:
    a, b = scrub.source_key("First memo says alpha beta gamma."), scrub.source_key("Second memo says delta epsilon zeta.")
    text = 'One "alpha beta gamma" and two "delta epsilon zeta" and "unrelated words here".'
    assert sp(text, [a, b]) == 'One "[redacted]" and two "[redacted]" and "unrelated words here".'
    assert sp(text, [a]) == 'One "[redacted]" and two "delta epsilon zeta" and "unrelated words here".'


def op(name, row, source=SID, **extra):
    return {"op": name, "row": row, "source": {"source_id": source, "locator": "p1"}, "base": None, **extra}


def packet(*ops, **extra):
    return {"packet_version": 1, "change_id": "tester-1", "agent": "tester", "intent": "test", "ops": list(ops), **extra}


TYPES = {"demo.motto": "text", "demo.site": "url", "demo.seats": "int", "demo.region": "enum"}
Q = f"Contact {M} for the vault"


def run(pkt, keys=None, whole=False, rows=()):
    keys = {SID: KEY} if keys is None else keys
    return scrub.scrub_packet(pkt, keys, TYPES, whole=whole, date="2026-10-12", cited={SID}, rows=rows)


def test_scrub_packet_per_op() -> None:
    quoted = f'See "{Q}" now.'
    unrelated = op("upsert_record", {"id": "rec-o", "kind": "note", "title": "Other", "body": "Plain."}, source="src-other")
    cases = [
        (op("upsert_project", {"id": "pj", "title": quoted, "description": quoted, "goal": quoted}), {"title": 'See "[redacted]" now.'}),
        (op("upsert_record", {"id": "rec-1", "title": quoted, "body": quoted}), {"body": 'See "[redacted]" now.'}),
        (op("upsert_decision", {"id": "dec-1", "title": quoted, "detail": quoted, "quote": Q}), {"quote": "[redacted]", "detail": 'See "[redacted]" now.'}),
        (op("upsert_action", {"id": "act-1", "title": quoted, "detail": quoted, "next_step": quoted, "close_reason": quoted}), {"next_step": 'See "[redacted]" now.'}),
        (op("upsert_checklist_item", {"id": "chk-1", "title": quoted, "detail": quoted}), {"detail": 'See "[redacted]" now.'}),
        (op("upsert_signal", {"id": "sig-1", "detail": quoted}), {"detail": 'See "[redacted]" now.'}),
        (op("upsert_thread", {"id": "thr-1", "subject": "Re: " + M}), {"subject": "[redacted]"}),
        (op("upsert_meeting", {"id": "mtg-1", "title": "Review " + M}), {"title": "[redacted]"}),
        (op("add_claim", {"id": "clm-1", "key": "demo.motto", "quote": Q, "value_raw": M}), {"quote": "[redacted]", "value_raw": "[redacted]"}),
        (op("verify_claim", {"claim_id": "clm-1", "quote": Q}), {"quote": "[redacted]"}),
        (op("resolve_fact", {"key": "demo.motto", "quote": Q}), {"quote": "[redacted]"}),
    ]
    for original, expected in cases:
        result, touched = run(packet(unrelated, original))
        assert touched == [1], original["op"]
        assert result["redacted_ops"] == [1] and result["ops"][0] == unrelated
        for field, value in expected.items():
            assert result["ops"][1]["row"][field] == value, (original["op"], field)
        assert M not in str(result["ops"][1])
        again, touched_again = run(result)
        assert touched_again == [] and again == result
    # upsert_fact: value only for a text, enum or url key
    for key, value, want in (
        ("demo.motto", M, "[redacted]"), ("demo.region", "us", "[redacted]"), ("demo.site", "https://x.example/" + M, "https://redacted.invalid/"),
        ("demo.seats", "25", "25"),
    ):
        result, touched = run(packet(op("upsert_fact", {"key": key, "value": value, "quote": Q, "value_raw": M})))
        assert result["ops"][0]["row"]["value"] == want and result["ops"][0]["row"]["quote"] == "[redacted]"
    # add_source
    source = {"op": "add_source", "row": {"id": SID, "kind": "document", "title": f"Notes {M}", "uri": "https://x/" + M, "source_date": "2026-10-07", "content": SRC, "author_person_id": "per-a"}}
    result, touched = run(packet(source))
    row = result["ops"][0]["row"]
    assert (row["content"], row["title"], row["uri"], row["author_person_id"], row["id"]) == ("[redacted]", "redacted", "", "per-a", SID)
    # apply_candidate
    cand = {"op": "apply_candidate", "candidate": {"source_id": SID, "kind": "claim_candidate", "quote": Q, "locator": "p", "fields": {"value_raw": M, "title": quoted, "detail": quoted, "next_step": quoted}}}
    result, touched = run(packet(cand))
    got = result["ops"][0]["candidate"]
    assert got["quote"] == "[redacted]" and got["fields"]["value_raw"] == "[redacted]" and got["fields"]["title"] == 'See "[redacted]" now.'
    # a record_collector_run whose error names the source, in an applied packet too
    run_op = {"op": "record_collector_run", "row": {"id": "run-1", "error": f"rejected: quote \"contact {M}\" is not in the cited source {SID}."}}
    result, touched = run(packet(run_op))
    assert touched == [0] and result["ops"][0]["row"]["error"] == "redacted on 2026-10-12"
    clean = {"op": "record_collector_run", "row": {"id": "run-2", "error": f"about {SID}-r3 and {SID}.b"}}
    assert run(packet(clean)) == (packet(clean), [])
    # redacted_ops merges with what is there
    pre = packet(unrelated, op("upsert_record", {"id": "rec-1", "title": "t", "body": quoted}), redacted_ops=[0])
    assert run(pre)[0]["redacted_ops"] == [0, 1]
    # an op that cites another source but wrote a changed row: spans and quote only
    edit = op("upsert_record", {"id": "rec-1", "title": "Edited", "body": quoted + " More."}, source="src-other")
    result, touched = run(packet(edit), rows={("records", "rec-1")})
    assert touched == [0] and result["ops"][0]["row"]["body"] == 'See "[redacted]" now. More.'
    assert result["ops"][0]["source"] == edit["source"] and result["ops"][0]["row"]["title"] == "Edited"
    assert run(packet(edit), rows={("records", "rec-9")})[1] == []
    # a chain source whose text is gone: token fields only, spans only for a refused packet
    gone = packet(op("upsert_record", {"id": "rec-1", "title": "t", "body": quoted}))
    assert run(gone, keys={})[1] == []
    assert run(gone, keys={}, whole=True)[0]["ops"][0]["row"]["body"] == "[redacted]"
    gone_claim = packet(op("add_claim", {"id": "clm-1", "key": "demo.motto", "quote": Q, "value_raw": M}))
    assert run(gone_claim, keys={})[0]["ops"][0]["row"]["quote"] == "[redacted]"
    # packet_problems is the check form
    assert scrub.packet_problems(packet(op("add_claim", {"id": "c", "key": "demo.motto", "quote": Q, "value_raw": "[redacted]"})), {SID}) == ["op 0"]
    assert scrub.packet_problems(run(gone_claim, keys={})[0], {SID}) == []
    assert scrub.packet_problems(packet(op("upsert_record", {"id": "r", "title": "t", "body": quoted})), {SID}) == []
    assert scrub.packet_problems(packet(op("upsert_record", {"id": "r", "title": "t", "body": quoted})), {SID}, whole=True) == ["op 0"]


def test_unapplied_packets_lose_whole_fields() -> None:
    near = op("upsert_record", {"id": "rec-1", "title": "Plain", "body": f'He wrote "contact {M} for the wrong vault ever".'})
    result, touched = run(packet(near))
    assert M in str(result)  # the span rule alone cannot tell the near copy from source text
    result, touched = run(packet(near), whole=True)
    assert touched == [0] and result["ops"][0]["row"]["body"] == "[redacted]" and result["ops"][0]["row"]["title"] == "[redacted]"
    unknown = op("upsert_fact", {"key": "demo.unknown", "value": "some words " + M})
    assert run(packet(unknown), whole=True)[0]["ops"][0]["row"]["value"] == "[redacted]"


PACKET_FIELDS_LITERAL = {
    "add_claim": (("quote", "value_raw"), ()),
    "add_source": ((), ()),
    "apply_candidate": ((), ()),
    "rebuild_search": ((), ()),
    "record_collector_run": ((), ()),
    "redact_source": ((), ()),
    "resolve_fact": (("quote",), ()),
    "set_aliases": ((), ()),
    "set_source_author": ((), ()),
    "upsert_action": ((), ("close_reason", "detail", "next_step", "title")),
    "upsert_checklist_item": ((), ("detail", "title")),
    "upsert_decision": (("quote",), ("detail", "title")),
    "upsert_fact": (("quote", "value_raw"), ()),
    "upsert_fact_key": ((), ()),
    "upsert_meeting": (("title",), ()),
    "upsert_person": ((), ()),
    "upsert_project": ((), ("description", "goal", "title")),
    "upsert_record": ((), ("body", "summary", "title")),
    "upsert_signal": ((), ("detail",)),
    "upsert_system": ((), ()),
    "upsert_thread": (("subject",), ()),
    "upsert_thread_link": ((), ()),
    "verify_claim": (("quote",), ()),
}


def test_packet_fields_are_computed_from_the_inventory() -> None:
    assert scrub.PACKET_FIELDS == PACKET_FIELDS_LITERAL
    assert set(scrub.PACKET_FIELDS) == set(schemas.OPS) | set(schemas.COMMAND_OPS)
    spans = {name: set(pair[1]) for name, pair in scrub.PACKET_FIELDS.items()}
    for name, fields in evidence_gate.GATE_FIELDS.items():
        assert set(fields) <= spans[name], name


TOKEN_LITERAL = [
    "sources.title", "sources.content", "sources.uri", "sources.author_person_id",
    "claims.quote", "claims.value_raw", "claims.value_text",
    "facts.value_text", "facts.quote", "fact_history.value_text", "fact_history.quote",
    "decisions.quote", "threads.subject", "meetings.title",
    "write_log.packet_json", "write_log.error", "write_log.hold_reason", "collector_runs.error",
]
SPANS_LITERAL = [
    "projects.title", "projects.description", "projects.goal", "records.title", "records.body", "records.summary",
    "record_history.title", "record_history.body", "record_history.summary", "decisions.title", "decisions.detail",
    "actions.title", "actions.detail", "actions.next_step", "actions.close_reason",
    "checklist_items.title", "checklist_items.detail", "signals.detail", "unchecked_text.sentences",
]
MAXIMA = {
    "projects": {"title": 120, "description": 2000, "goal": 500},
    "records": {"title": 200, "body": 100000, "summary": 1200},
    "decisions": {"title": 90, "detail": 2000},
    "actions": {"title": 90, "detail": 2000, "next_step": 500, "close_reason": 500},
    "checklist_items": {"title": 140, "detail": 2000},
    "signals": {"detail": 500},
}


def test_disposition_covers_every_text_column(tmp_path: Path, clock: Clock) -> None:
    assert set(scrub.DISPOSITION) == set(schemas.FIELD_CLASS)
    assert sorted(k for k, v in scrub.DISPOSITION.items() if v == "token") == sorted(TOKEN_LITERAL)
    assert sorted(k for k, v in scrub.DISPOSITION.items() if v == "spans") == sorted(SPANS_LITERAL)
    assert set(scrub.DISPOSITION.values()) == {"token", "spans", "kept"}
    assert scrub.SPAN_COLUMNS == MAXIMA
    fixture = seed(tmp_path, clock)
    rows = {"projects": "vault-project", "records": "rec-plain", "decisions": "dec-vault", "actions": "act-vault",
            "checklist_items": "chk-vault", "signals": "sig-vault"}
    from tests.conftest import context_connection

    con = context_connection(fixture.instance)
    try:
        for table, columns in MAXIMA.items():
            schema_sql = con.execute("SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
            for column, maximum in columns.items():
                found = re.findall(rf"length\((?:trim\()?{column}\)?\)\s*(?:<=\s*(\d+)|BETWEEN\s+\d+\s+AND\s+(\d+))", schema_sql)
                assert [x or y for x, y in found] == [str(maximum)], (table, column, found)
                bump = "version = version + 1"
                con.execute(f"UPDATE {table} SET {column} = ?, {bump} WHERE id = ?", ("x" * maximum, rows[table]))
                with pytest.raises(sqlite3.IntegrityError):
                    con.execute(f"UPDATE {table} SET {column} = ?, {bump} WHERE id = ?", ("x" * (maximum + 1), rows[table]))
        con.rollback()
    finally:
        con.close()


def test_named_sources() -> None:
    n = scrub.named_sources
    assert "src-a" not in n("src-a-r3") and "src-a" not in n("src-a.b") and "src-a" not in n("src-a..a") and "src-a" not in n("xsrc-a")
    for text in ("a source src-a.", "source src-a...", "(src-a)", "src-a, then", "id=src-a;"):
        assert "src-a" in n(text), text
    assert n("src-a-") == {"src-a-"} and "src-a" not in n("src-a-")
    assert scrub.names_source("the cited source src-cap-d91b.", "src-cap-d91b")
    assert not scrub.names_source("src-a-r3", "src-a")
    rng = random.Random(7)
    alphabet = "abc-._ ,;()srcSRC1"
    ids = ["src-a", "src-a-", "src-a.", "src-a.b", "src-ab"]
    for _ in range(5000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 30))) + rng.choice(ids) + "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 5)))
        for ident in ids:
            assert scrub.names_source(text, ident) == (ident in store.named_sources(text))
