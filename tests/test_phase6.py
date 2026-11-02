from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from brain import config as config_mod
from brain import evidence_gate, store, validate
from brain.applier import Applier, Unreadable
from brain.init import init_instance, render_router
from brain.migrate import latest_version, migrate
from tests.conftest import (
    attribution_refusal,
    router_bytes,
    NOW,
    call,
    context_connection,
    make_packet,
    render_config,
    ro_rows,
    source_op,
    upsert_op,
    version_banner,
)

PRINCIPAL = {
    "person_id": "per-alex-example",
    "display_name": "Alex Example",
    "names": ["Alex"],
    "aliases": [],
}
AGENTS = {
    "legacy": {"operations": "*", "legacy_import": True},
    "plain": {"operations": "*"},
    "tester": {"operations": "*", "author_principal": True},
}
VERIFIED = "2026-10-01T00:00:00Z"
ATTRIBUTION = "Alex Example agreed to the plan."
ATTRIBUTION_ERROR = "evidence gate: " + attribution_refusal(ATTRIBUTION)
CLEAN = "The kettle is descaled monthly."
MARKER_NOTE = (
    "Imported without passing the evidence gate. "
    "Treat these sentences as unverified until a source confirms them."
)


def _key(sentence: str) -> str:
    return hashlib.sha256(evidence_gate.normalise(sentence).encode()).hexdigest()


@pytest.fixture
def p6(tmp_path: Path):
    config = render_config(principal=PRINCIPAL, agents=AGENTS)
    instance = Path(init_instance("demo", tmp_path, config_text=config)["instance"])
    writer = Applier(instance)

    def apply(packet: dict):
        return writer.apply(json.dumps(packet).encode())

    try:
        yield instance, apply
    finally:
        writer.close()


def _source(source_id: str = "src-p6", content: str = "Kettle notes. Descale it every month.") -> dict:
    return source_op(source_id, content)


def _action(row_id: str = "act-p6", **fields) -> dict:
    row = {
        "id": row_id,
        "title": "Descale the kettle",
        "status": "open",
        "status_basis": "observed",
        "project_id": "general",
        "last_verified_at": VERIFIED,
    }
    row.update(fields)
    return row


def _item(row_id: str = "chk-p6", **fields) -> dict:
    row = {
        "id": row_id,
        "project_id": "general",
        "title": "Check the kettle",
        "state": "not_started",
        "state_basis": "observed",
        "last_verified_at": VERIFIED,
    }
    row.update(fields)
    return row


def _person(person_id: str, name: str) -> dict:
    return upsert_op("upsert_person", {"id": person_id, "display_name": name}, "src-p6")


def _legacy_action(apply, row_id="act-p6", detail=f"{ATTRIBUTION} {CLEAN}", source_id="src-p6"):
    return apply(
        make_packet(
            "legacy",
            _source(source_id),
            upsert_op("upsert_action", _action(row_id, detail=detail), source_id),
        )
    )


def _marker(instance: Path, entity: str, ref_id: str):
    rows = ro_rows(
        instance,
        "SELECT sentences, source_id, created_by, updated_by FROM unchecked_text "
        "WHERE entity = ? AND ref_id = ?",
        (entity, ref_id),
    )
    return rows[0] if rows else None


def _targets(outcome) -> list[tuple[str, str, str]]:
    return [(t["table"], t["id"], t["action"]) for t in outcome.receipt["targets"]]


# -- unchecked text --------------------------------------------------------------------------


def _write_config(tmp_path: Path, agents: dict) -> Path:
    path = tmp_path / "brain.toml"
    path.write_text(render_config(principal=PRINCIPAL, agents=agents), encoding="utf-8")
    return path


def test_legacy_import_config_rules(tmp_path: Path) -> None:
    with pytest.raises(config_mod.ConfigError, match=r"agents\.bad\.legacy_import must be a boolean"):
        config_mod.load_config(
            _write_config(tmp_path, {"bad": {"operations": "*", "legacy_import": "yes"}})
        )
    needs = r'agents\.bad\.legacy_import needs kind = "collector" and manual_sources = true'
    with pytest.raises(config_mod.ConfigError, match=needs):
        config_mod.load_config(
            _write_config(tmp_path, {"bad": {"kind": "agent", "operations": "*", "legacy_import": True}})
        )
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(
            _write_config(tmp_path, {"bad": {"kind": "assistant", "operations": "*", "legacy_import": True}})
        )
    with pytest.raises(config_mod.ConfigError, match=needs):
        config_mod.load_config(
            _write_config(
                tmp_path,
                {"bad": {"kind": "collector", "manual_sources": False, "operations": "*", "legacy_import": True}},
            )
        )
    loaded = config_mod.load_config(
        _write_config(
            tmp_path,
            {"good": {"kind": "collector", "manual_sources": True, "operations": "*", "legacy_import": True}},
        )
    )
    assert loaded.agents["good"].legacy_import is True
    other = config_mod.load_config(
        _write_config(tmp_path, {"good": {"operations": "*"}})
    )
    assert other.agents["good"].legacy_import is False


def test_apply_config_accepts_legacy_import(p6) -> None:
    instance, _apply = p6
    result = subprocess.run(
        ["brain", "apply-config", "--instance", str(instance)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["warnings"] == []
    assert "legacy" not in result.stderr


def test_flag_text_matches_check_text() -> None:
    principal = evidence_gate.Principal("per-alex-example", "Alex Example", ("Alex",))
    plain_source = evidence_gate.CitedSource("src-a", "Descale the kettle every month.", None)
    alex_source = evidence_gate.CitedSource(
        "src-b", "Please ship it on Friday afternoon, Alex said.", "per-alex-example"
    )
    samples = [
        (['He said "this quote is nowhere in the source" today.'], "", plain_source, principal),
        ([ATTRIBUTION], "", plain_source, principal),
        ([f"{ATTRIBUTION} The memo says \"nothing here matches the source\"."], "", plain_source, principal),
        ([ATTRIBUTION], ATTRIBUTION, plain_source, principal),
        (["Alex’s words were final."], "", plain_source, principal),
        (["Alex Example: ship it on Friday."], "", plain_source, principal),
        ([CLEAN], "", plain_source, principal),
        (['Alex Example said "ship it on Friday afternoon".'], "", alex_source, principal),
        ([f"{CLEAN}\n- {ATTRIBUTION}", 'Quote: "completely absent words here".'], "", plain_source, None),
    ]
    flagged = 0
    for texts, known, source, who in samples:
        flags = evidence_gate.flag_text(texts, known, source, who)
        flat = list(dict.fromkeys(m for flag in flags for m in flag.messages))
        assert flat == evidence_gate.check_text(texts, known, source, who), texts
        flagged += bool(flags)
    assert flagged >= 6
    first = evidence_gate.flag_text(samples[0][0], "", plain_source, principal)
    assert [flag.reasons for flag in first] == [("quote_not_in_source",)]
    assert first[0].sentence == 'He said "this quote is nowhere in the source" today.'
    second = evidence_gate.flag_text([ATTRIBUTION], "", plain_source, principal)
    assert second == [
        evidence_gate.Flag(
            ATTRIBUTION,
            ("unquoted_attribution",),
            (attribution_refusal(ATTRIBUTION),),
        )
    ]
    assert evidence_gate.flag_text([CLEAN], "", plain_source, principal) == []


def test_legacy_insert_records_marker(p6) -> None:
    instance, apply = p6
    outcome = _legacy_action(apply)
    assert outcome.receipt["status"] == "applied", outcome.receipt
    targets = _targets(outcome)
    assert targets[-2:] == [
        ("actions", "act-p6", "insert"),
        ("unchecked_text", "action:act-p6", "insert"),
    ]
    sentences, source_id, created_by, updated_by = _marker(instance, "action", "act-p6")
    assert source_id == "src-p6"
    assert created_by == updated_by == f"legacy:{outcome.receipt['change_id']}"
    assert json.loads(sentences) == [
        {"key": _key(ATTRIBUTION), "reasons": ["unquoted_attribution"], "text": ATTRIBUTION}
    ]
    assert ro_rows(instance, "SELECT detail FROM actions WHERE id = 'act-p6'") == [
        (f"{ATTRIBUTION} {CLEAN}",)
    ]


def test_legacy_insert_quote_not_in_source(p6) -> None:
    instance, apply = p6
    body = 'The note says "this phrase appears nowhere" in it. Descale monthly.'
    outcome = apply(
        make_packet(
            "legacy",
            _source(),
            upsert_op(
                "upsert_record",
                {"id": "rec-p6", "kind": "note", "title": "Kettle", "body": body},
                "src-p6",
            ),
        )
    )
    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert ("unchecked_text", "record:rec-p6", "insert") in _targets(outcome)
    sentences = json.loads(_marker(instance, "record", "rec-p6")[0])
    assert [item["reasons"] for item in sentences] == [["quote_not_in_source"]]
    assert sentences[0]["text"] == 'The note says "this phrase appears nowhere" in it.'


def test_legacy_update_uses_normal_gate(p6) -> None:
    instance, apply = p6
    assert _legacy_action(apply).receipt["status"] == "applied"
    flagged = "Alex Example wants a new kettle."
    outcome = apply(
        make_packet(
            "legacy",
            upsert_op("upsert_action", {"id": "act-p6", "detail": flagged}, "src-p6", base=1),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == (
        "op 0 (upsert_action): evidence gate: " + attribution_refusal(flagged)
    )
    again = apply(
        make_packet(
            "legacy",
            upsert_op(
                "upsert_action", {"id": "act-p6", "detail": f"{ATTRIBUTION} Changed."}, "src-p6", base=1
            ),
        )
    )
    # A sentence already stored is known text, so re-sending it passes like it does for any agent.
    assert again.receipt["status"] == "applied", again.receipt
    assert not [t for t in _targets(again) if t[0] == "unchecked_text"]
    assert [item["text"] for item in json.loads(_marker(instance, "action", "act-p6")[0])] == [ATTRIBUTION]


def test_non_legacy_agent_rejected_unchanged(p6) -> None:
    instance, apply = p6
    outcome = apply(
        make_packet(
            "plain",
            _source(),
            upsert_op("upsert_action", _action(detail=f"{ATTRIBUTION} {CLEAN}"), "src-p6"),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == f"op 1 (upsert_action): {ATTRIBUTION_ERROR}"
    assert ro_rows(instance, "SELECT count(*) FROM unchecked_text") == [(0,)]
    assert ro_rows(instance, "SELECT count(*) FROM actions") == [(0,)]


def test_legacy_never_for_decisions_or_signals(p6) -> None:
    instance, apply = p6
    setup = apply(
        make_packet(
            "legacy",
            _source(content="Kettle notes. Descale it every month. We chose to descale it monthly."),
            _person("per-robin-example", "Robin Example"),
            upsert_op("upsert_action", _action(), "src-p6"),
        )
    )
    assert setup.receipt["status"] == "applied", setup.receipt
    decision = apply(
        make_packet(
            "legacy",
            upsert_op(
                "upsert_decision",
                {
                    "id": "dec-p6",
                    "title": "Descale monthly",
                    "detail": ATTRIBUTION,
                    "quote": "We chose to descale it monthly.",
                    "decided_on": "2026-10-01",
                    "decided_by_person_id": "per-robin-example",
                    "status": "in_effect",
                    "project_id": "general",
                },
                "src-p6",
            ),
        )
    )
    assert decision.receipt["status"] == "rejected"
    assert decision.receipt["error"] == f"op 0 (upsert_decision): {ATTRIBUTION_ERROR}"
    signal = apply(
        make_packet(
            "legacy",
            upsert_op(
                "upsert_signal",
                {
                    "id": "sig-p6",
                    "kind": "other",
                    "subject_table": "actions",
                    "subject_id": "act-p6",
                    "detail": ATTRIBUTION,
                    "basis": "inferred",
                },
                "src-p6",
            ),
        )
    )
    assert signal.receipt["status"] == "rejected"
    assert signal.receipt["error"] == f"op 0 (upsert_signal): {ATTRIBUTION_ERROR}"
    assert ro_rows(instance, "SELECT count(*) FROM unchecked_text") == [(0,)]


def test_legacy_not_for_candidates(p6) -> None:
    # apply_candidate derives ops through _apply_op; a legacy agent never gets the exception there
    instance, apply = p6
    assert apply(make_packet("legacy", _source())).receipt["status"] == "applied"
    writer = Applier(instance)
    try:
        writer.preconditions()
        writer._begin()
        try:
            err, status, _target = writer._apply_op(
                "0.0",
                "upsert_action",
                upsert_op("upsert_action", _action(detail=ATTRIBUTION), "src-p6"),
                "legacy",
                "legacy-direct",
                "2026-10-08T00:00:00Z",
                writer.config.agents["legacy"],
                evidence_gate.Principal("per-alex-example", "Alex Example", ("Alex",)),
                set(),
                from_packet=False,
            )
        finally:
            writer._end_txn("ROLLBACK")
    finally:
        writer.close()
    assert status == "rejected"
    assert err == f"op 0.0 (upsert_action): {ATTRIBUTION_ERROR}"


def test_legacy_marker_cap(p6) -> None:
    instance, apply = p6
    detail = " ".join(f"Alex Example agreed to plan {i}." for i in range(501))
    outcome = apply(
        make_packet(
            "legacy",
            _source(),
            upsert_op("upsert_record", {"id": "rec-big", "kind": "note", "title": "Big", "body": detail}, "src-p6"),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == "op 1 (upsert_record): more than 500 unchecked sentences"


def test_marker_cap_counts_distinct_sentences(p6) -> None:
    instance, apply = p6
    body = " ".join(f"Alex Example agreed to plan {i}." for i in range(500)) + " Alex Example agreed to plan 0."
    outcome = apply(
        make_packet(
            "legacy",
            _source(),
            upsert_op("upsert_record", {"id": "rec-500", "kind": "note", "title": "Five hundred", "body": body}, "src-p6"),
        )
    )
    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert len(json.loads(_marker(instance, "record", "rec-500")[0])) == 500


def test_resend_of_stored_sentence_passes_across_fields(p6) -> None:
    # The detail has no closing full stop and next_step starts lower-case, so reading the fields
    # together joins them. The stored next_step sentence must still count as known text.
    instance, apply = p6
    sentence = "alex example agreed to go ahead"
    outcome = apply(
        make_packet(
            "legacy",
            _source(),
            upsert_op("upsert_action", _action(detail="Waiting on a reply", next_step=sentence), "src-p6"),
        )
    )
    assert outcome.receipt["status"] == "applied", outcome.receipt
    assert _marker(instance, "action", "act-p6") is not None
    resend = apply(make_packet("plain", upsert_op("upsert_action", {"id": "act-p6", "next_step": sentence}, "src-p6", base=1)))
    assert resend.receipt["status"] == "applied", resend.receipt
    assert _marker(instance, "action", "act-p6") is not None
    new = apply(
        make_packet(
            "plain",
            upsert_op("upsert_action", {"id": "act-p6", "next_step": "alex example agreed to stop"}, "src-p6", base=2),
        )
    )
    assert new.receipt["status"] == "rejected"
    assert "attributes words or a decision to Alex Example" in new.receipt["error"]


def test_lone_surrogate_packet_is_unreadable(p6) -> None:
    instance, apply = p6
    before = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    for agent in ("plain", "legacy"):
        packet = make_packet(agent, _source(), upsert_op("upsert_action", _action(detail="Bad \ud800 text."), "src-p6"))
        with pytest.raises(Unreadable, match="packet holds a lone surrogate escape"):
            apply(packet)
        code, report = validate.validate_command("packet", json.dumps(packet).encode())
        assert code == 1
        assert report["errors"] == [{"path": "", "message": "input holds a lone surrogate escape"}]
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == before
    pair = make_packet("plain", _source("src-pair", "Kettle notes \U0001F600."), upsert_op("upsert_action", _action(detail="Fine \U0001F600 text."), "src-pair"))
    assert apply(pair).receipt["status"] == "applied"


def test_read_tools_refuse_lone_surrogate_arguments(p6) -> None:
    instance, _apply = p6
    for name, arguments in (
        ("search", {"query": "kettle \ud800"}),
        ("get", {"entity": "action", "id": "act-\ud800"}),
        ("records_query", {"entity": "action", "filters": {"title": {"contains": "x\udc00"}}}),
    ):
        result = call(instance, name, **arguments)
        assert result.is_error
        assert result.code == "invalid_arguments", result.text
        assert "arguments hold a lone surrogate escape" in result.text
    assert not call(instance, "search", query="kettle \U0001F600").is_error


def test_marker_shrinks_then_disappears(p6) -> None:
    instance, apply = p6
    second = "Alex Example wants the kettle replaced."
    created = apply(
        make_packet(
            "legacy",
            _source(),
            upsert_op(
                "upsert_record",
                {"id": "rec-p6", "kind": "note", "title": "Kettle", "body": f"{ATTRIBUTION} {second}"},
                "src-p6",
            ),
        )
    )
    assert created.receipt["status"] == "applied", created.receipt
    assert len(json.loads(_marker(instance, "record", "rec-p6")[0])) == 2

    status_only = apply(
        make_packet("plain", upsert_op("upsert_record", {"id": "rec-p6", "status": "archived"}, "src-p6", base=1))
    )
    assert status_only.receipt["status"] == "applied", status_only.receipt
    assert not [t for t in _targets(status_only) if t[0] == "unchecked_text"]
    assert len(json.loads(_marker(instance, "record", "rec-p6")[0])) == 2

    dropped_one = apply(
        make_packet("plain", upsert_op("upsert_record", {"id": "rec-p6", "body": ATTRIBUTION}, "src-p6", base=2))
    )
    assert dropped_one.receipt["status"] == "applied", dropped_one.receipt
    assert ("unchecked_text", "record:rec-p6", "update") in _targets(dropped_one)
    sentences, _source_id, created_by, updated_by = _marker(instance, "record", "rec-p6")
    assert [item["text"] for item in json.loads(sentences)] == [ATTRIBUTION]
    assert created_by == f"legacy:{created.receipt['change_id']}"
    assert updated_by == f"plain:{dropped_one.receipt['change_id']}"

    dropped_all = apply(
        make_packet("plain", upsert_op("upsert_record", {"id": "rec-p6", "body": CLEAN}, "src-p6", base=3))
    )
    assert dropped_all.receipt["status"] == "applied", dropped_all.receipt
    assert ("unchecked_text", "record:rec-p6", "delete") in _targets(dropped_all)
    assert _marker(instance, "record", "rec-p6") is None


def test_get_unchecked_key(p6) -> None:
    instance, apply = p6
    assert _legacy_action(apply).receipt["status"] == "applied"
    clean = apply(make_packet("plain", upsert_op("upsert_action", _action("act-clean"), "src-p6")))
    assert clean.receipt["status"] == "applied", clean.receipt
    marked = call(instance, "get", entity="action", id="act-p6").structured
    # The labelled notes object follows the unchecked marker.
    assert list(marked)[-2:] == ["unchecked", "notes"]
    assert marked["unchecked"] == {
        "count": 1,
        "sentences": [{"text": ATTRIBUTION, "reasons": ["unquoted_attribution"]}],
        "source_id": "src-p6",
        "since": ro_rows(instance, "SELECT created_at FROM unchecked_text")[0][0],
        "note": MARKER_NOTE,
    }
    assert list(marked["unchecked"]) == ["count", "sentences", "source_id", "since", "note"]
    unmarked = call(instance, "get", entity="action", id="act-clean").structured
    assert "unchecked" not in unmarked


def test_get_unchecked_sentences_cap_at_ten(p6) -> None:
    instance, apply = p6
    body = " ".join(f"Alex Example agreed to plan {i}." for i in range(12))
    outcome = apply(
        make_packet(
            "legacy",
            _source(),
            upsert_op("upsert_record", {"id": "rec-many", "kind": "note", "title": "Many", "body": body}, "src-p6"),
        )
    )
    assert outcome.receipt["status"] == "applied", outcome.receipt
    unchecked = call(instance, "get", entity="record", id="rec-many").structured["unchecked"]
    assert unchecked["count"] == 12
    assert len(unchecked["sentences"]) == 10


def test_search_and_records_query_unchecked(p6) -> None:
    instance, apply = p6
    assert _legacy_action(apply, detail=f"{ATTRIBUTION} Kettle plan.").receipt["status"] == "applied"
    assert (
        apply(make_packet("plain", upsert_op("upsert_action", _action("act-clean", title="Clean kettle"), "src-p6"))).receipt[
            "status"
        ]
        == "applied"
    )
    hits = call(instance, "search", query="kettle").structured["hits"]
    by_id = {hit["id"]: hit for hit in hits}
    assert by_id["act-p6"]["unchecked"] is True
    assert list(by_id["act-p6"])[-2:] == ["score", "unchecked"]
    assert "unchecked" not in by_id["act-clean"]
    found = call(instance, "records_query", entity="action", filters={"unchecked": True}).structured
    assert [row["id"] for row in found["rows"]] == ["act-p6"]
    unmarked = call(
        instance, "records_query", entity="action", filters={"unchecked": False}, fields=["id", "unchecked"]
    ).structured
    assert unmarked["rows"] == [{"id": "act-clean", "unchecked": False}]
    default_fields = call(instance, "records_query", entity="action").structured["fields"]
    assert "unchecked" not in default_fields


def test_brief_unchecked_line(p6) -> None:
    instance, apply = p6
    before = call(instance, "brief").text
    assert "Unchecked text" not in before
    assert _legacy_action(apply, "act-one").receipt["status"] == "applied"
    one = call(instance, "brief").text.splitlines()
    assert (
        "Unchecked text: 1 row imported without passing the evidence gate (1 action). "
        "get shows the sentences."
    ) in one
    assert _legacy_action(apply, "act-two", source_id="src-p6b").receipt["status"] == "applied"
    record = apply(
        make_packet(
            "legacy",
            upsert_op(
                "upsert_record",
                {"id": "rec-p6", "kind": "note", "title": "Kettle", "body": ATTRIBUTION},
                "src-p6",
            ),
        )
    )
    assert record.receipt["status"] == "applied", record.receipt
    lines = call(instance, "brief").text.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith("Checklist:"))
    assert lines[index + 1] == (
        "Unchecked text: 3 rows imported without passing the evidence gate (2 actions, 1 record). "
        "get shows the sentences."
    )


def test_check_reports_bad_marker(p6) -> None:
    instance, apply = p6
    assert _legacy_action(apply).receipt["status"] == "applied"
    assert store.check_instance(instance) == []
    now = "2026-10-08T00:00:00Z"
    good = json.dumps(
        [{"key": _key("x y z"), "reasons": ["unquoted_attribution"], "text": "x y z"}], sort_keys=True
    )
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO unchecked_text (entity, ref_id, sentences, source_id, created_at, created_by, "
            "updated_at, updated_by) VALUES ('action', 'act-gone', ?, 'src-p6', ?, 'tester:x', ?, 'tester:x')",
            (good, now, now),
        )
        con.execute(
            "UPDATE unchecked_text SET sentences = ? WHERE entity = 'action' AND ref_id = 'act-p6'",
            (json.dumps([{"key": "nope", "reasons": [], "text": ""}]),),
        )
        con.execute("COMMIT")
    finally:
        con.close()
    problems = store.check_instance(instance)
    assert "unchecked_text action act-gone: row does not exist" in problems
    assert "unchecked_text action act-p6: malformed sentences" in problems


def test_get_tolerates_malformed_marker(p6) -> None:
    instance, apply = p6
    assert _legacy_action(apply).receipt["status"] == "applied"
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute(
            "UPDATE unchecked_text SET sentences = ? WHERE entity = 'action' AND ref_id = 'act-p6'",
            (json.dumps(["junk"] * 10 + [{"key": "x"}, {"text": 7, "reasons": ["quote_not_in_source", 3]}]),),
        )
        con.execute("COMMIT")
    finally:
        con.close()
    result = call(instance, "get", entity="action", id="act-p6")
    assert not result.is_error, result.text
    unchecked = result.structured["unchecked"]
    assert unchecked["count"] == 12
    assert unchecked["sentences"] == [
        {"text": "", "reasons": []},
        {"text": "", "reasons": ["quote_not_in_source"]},
    ]
    assert "unchecked_text action act-p6: malformed sentences" in store.check_instance(instance)


# -- several people per role -----------------------------------------------------------------


def _people_setup(apply, *extra_ops):
    outcome = apply(
        make_packet(
            "plain",
            _source(),
            _person("per-robin-example", "Robin Example"),
            _person("per-sam-example", "Sam Example"),
            _person("per-kim-example", "Kim Example"),
            *extra_ops,
        )
    )
    assert outcome.receipt["status"] == "applied", outcome.receipt
    return outcome


def _list(instance: Path, table: str, key: str, row_id: str, role: str) -> list[str]:
    return [
        row[0]
        for row in ro_rows(
            instance,
            f"SELECT person_id FROM {table} WHERE {key} = ? AND role = ? ORDER BY position",
            (row_id, role),
        )
    ]


def test_people_lists_insert_and_scalar(p6) -> None:
    instance, apply = p6
    _people_setup(
        apply,
        upsert_op(
            "upsert_action",
            _action(
                waiting_on_person_ids=["per-robin-example", "per-sam-example"],
                owner_person_ids=["per-kim-example"],
            ),
            "src-p6",
        ),
        upsert_op(
            "upsert_checklist_item",
            _item(waiting_on_person_ids=["per-sam-example", "per-robin-example"]),
            "src-p6",
        ),
    )
    assert _list(instance, "action_people", "action_id", "act-p6", "waiting_on") == [
        "per-robin-example",
        "per-sam-example",
    ]
    assert _list(instance, "action_people", "action_id", "act-p6", "owner") == ["per-kim-example"]
    assert ro_rows(instance, "SELECT owner_person_id, waiting_on_person_id FROM actions") == [
        ("per-kim-example", "per-robin-example")
    ]
    assert _list(instance, "checklist_item_people", "item_id", "chk-p6", "waiting_on") == [
        "per-sam-example",
        "per-robin-example",
    ]
    assert ro_rows(instance, "SELECT owner_person_id, waiting_on_person_id FROM checklist_items") == [
        (None, "per-sam-example")
    ]


def test_scalar_must_be_first(p6) -> None:
    instance, apply = p6
    _people_setup(apply)
    bad_waiting = apply(
        make_packet(
            "plain",
            upsert_op(
                "upsert_action",
                _action(
                    waiting_on_person_id="per-sam-example",
                    waiting_on_person_ids=["per-robin-example", "per-sam-example"],
                ),
                "src-p6",
            ),
        )
    )
    assert bad_waiting.receipt["status"] == "rejected"
    assert bad_waiting.receipt["error"] == (
        "op 0 (upsert_action): waiting_on_person_id must be the first of waiting_on_person_ids"
    )
    bad_owner = apply(
        make_packet(
            "plain",
            upsert_op(
                "upsert_checklist_item",
                _item(owner_person_id="per-sam-example", owner_person_ids=["per-robin-example"]),
                "src-p6",
            ),
        )
    )
    assert bad_owner.receipt["error"] == (
        "op 0 (upsert_checklist_item): owner_person_id must be the first of owner_person_ids"
    )
    not_null = apply(
        make_packet(
            "plain",
            upsert_op(
                "upsert_action",
                _action(owner_person_id="per-sam-example", owner_person_ids=[]),
                "src-p6",
            ),
        )
    )
    assert not_null.receipt["error"] == (
        "op 0 (upsert_action): owner_person_id must be the first of owner_person_ids"
    )
    fine = apply(
        make_packet(
            "plain",
            upsert_op(
                "upsert_action",
                _action(
                    waiting_on_person_id="per-robin-example",
                    waiting_on_person_ids=["per-robin-example", "per-sam-example"],
                    owner_person_id=None,
                    owner_person_ids=[],
                ),
                "src-p6",
            ),
        )
    )
    assert fine.receipt["status"] == "applied", fine.receipt
    assert ro_rows(instance, "SELECT count(*) FROM actions") == [(1,)]


def test_scalar_only_rules(p6) -> None:
    instance, apply = p6
    _people_setup(
        apply,
        upsert_op(
            "upsert_action",
            _action(waiting_on_person_ids=["per-robin-example", "per-sam-example"]),
            "src-p6",
        ),
    )

    def waiting() -> list[str]:
        return _list(instance, "action_people", "action_id", "act-p6", "waiting_on")

    version = 1

    def update(**fields):
        nonlocal version
        outcome = apply(
            make_packet("plain", upsert_op("upsert_action", {"id": "act-p6", **fields}, "src-p6", base=version))
        )
        assert outcome.receipt["status"] == "applied", outcome.receipt
        version += 1

    update(waiting_on_person_id="per-robin-example")
    assert waiting() == ["per-robin-example", "per-sam-example"]
    update(title="Descale the big kettle")
    assert waiting() == ["per-robin-example", "per-sam-example"]
    update(waiting_on_person_id="per-kim-example")
    assert waiting() == ["per-kim-example"]
    assert ro_rows(instance, "SELECT waiting_on_person_id FROM actions") == [("per-kim-example",)]
    update(waiting_on_person_id=None)
    assert waiting() == []
    assert ro_rows(instance, "SELECT waiting_on_person_id FROM actions") == [(None,)]


def test_unknown_person_rejected(p6) -> None:
    instance, apply = p6
    _people_setup(apply)
    outcome = apply(
        make_packet(
            "plain",
            upsert_op(
                "upsert_action",
                _action(owner_person_ids=["per-robin-example", "per-ghost-example"]),
                "src-p6",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == "op 0 (upsert_action): person per-ghost-example does not exist"
    assert ro_rows(instance, "SELECT count(*) FROM actions") == [(0,)]


def test_people_list_schema_limits(p6) -> None:
    instance, apply = p6
    _people_setup(apply)
    duplicate = apply(
        make_packet(
            "plain",
            upsert_op(
                "upsert_action", _action(owner_person_ids=["per-robin-example", "per-robin-example"]), "src-p6"
            ),
        )
    )
    assert duplicate.receipt["status"] == "rejected"
    too_many = apply(
        make_packet(
            "plain",
            upsert_op("upsert_action", _action(owner_person_ids=[f"per-p{i}" for i in range(21)]), "src-p6"),
        )
    )
    assert too_many.receipt["status"] == "rejected"


def test_get_and_records_query_people(p6) -> None:
    instance, apply = p6
    _people_setup(
        apply,
        upsert_op(
            "upsert_action",
            _action(waiting_on_person_ids=["per-robin-example", "per-sam-example"]),
            "src-p6",
        ),
        upsert_op("upsert_action", _action("act-lone"), "src-p6"),
        upsert_op(
            "upsert_checklist_item",
            _item(owner_person_ids=["per-kim-example", "per-sam-example"]),
            "src-p6",
        ),
    )
    row = call(instance, "get", entity="action", id="act-p6").structured["row"]
    keys = list(row)
    at = keys.index("waiting_on_person_id")
    assert keys[at + 1 : at + 3] == ["owner_person_ids", "waiting_on_person_ids"]
    assert row["owner_person_ids"] == []
    assert row["waiting_on_person_ids"] == ["per-robin-example", "per-sam-example"]
    item = call(instance, "get", entity="checklist_item", id="chk-p6").structured["row"]
    keys = list(item)
    at = keys.index("waiting_on_person_id")
    assert keys[at + 1 : at + 3] == ["owner_person_ids", "waiting_on_person_ids"]
    assert item["owner_person_ids"] == ["per-kim-example", "per-sam-example"]

    listing = call(instance, "records_query", entity="action").structured
    fields = listing["fields"]
    at = fields.index("waiting_on_person_id")
    assert fields[at + 1 : at + 3] == ["owner_person_ids", "waiting_on_person_ids"]
    rows = {r["id"]: r for r in listing["rows"]}
    assert rows["act-p6"]["waiting_on_person_ids"] == ["per-robin-example", "per-sam-example"]
    assert rows["act-p6"]["waiting_on_person_id"] == "per-robin-example"
    assert rows["act-lone"]["waiting_on_person_ids"] == []
    chk_fields = call(instance, "records_query", entity="checklist_item").structured["fields"]
    at = chk_fields.index("waiting_on_person_id")
    assert chk_fields[at + 1 : at + 3] == ["owner_person_ids", "waiting_on_person_ids"]

    second = call(
        instance, "records_query", entity="action", filters={"waiting_on_person_id": "per-sam-example"}
    ).structured
    assert [r["id"] for r in second["rows"]] == ["act-p6"]
    assert second["total"] == 1
    within = call(
        instance,
        "records_query",
        entity="action",
        filters={"waiting_on_person_id": {"in": ["per-sam-example", "per-kim-example"]}},
    ).structured
    assert [r["id"] for r in within["rows"]] == ["act-p6"]
    nobody = call(instance, "records_query", entity="action", filters={"waiting_on_person_id": None}).structured
    assert [r["id"] for r in nobody["rows"]] == ["act-lone"]
    item_hit = call(
        instance, "records_query", entity="checklist_item", filters={"owner_person_id": "per-sam-example"}
    ).structured
    assert [r["id"] for r in item_hit["rows"]] == ["chk-p6"]
    bad = call(instance, "records_query", entity="action", filters={"owner_person_ids": "per-kim-example"})
    assert bad.is_error
    ordered = call(instance, "records_query", entity="action", order_by="owner_person_ids")
    assert ordered.is_error


def test_person_counts_any_position(p6) -> None:
    instance, apply = p6
    _people_setup(
        apply,
        upsert_op(
            "upsert_action",
            _action("act-a", waiting_on_person_ids=["per-robin-example", "per-sam-example"]),
            "src-p6",
        ),
        upsert_op("upsert_action", _action("act-b", waiting_on_person_ids=["per-sam-example"]), "src-p6"),
        upsert_op("upsert_action", _action("act-c", owner_person_ids=["per-kim-example", "per-sam-example"]), "src-p6"),
        upsert_op(
            "upsert_checklist_item",
            _item("chk-a", owner_person_ids=["per-robin-example", "per-sam-example"]),
            "src-p6",
        ),
        upsert_op(
            "upsert_checklist_item",
            _item("chk-b", waiting_on_person_ids=["per-kim-example", "per-sam-example"]),
            "src-p6",
        ),
    )
    counts = call(instance, "get", entity="person", id="per-sam-example").structured["related"]["counts"]
    assert counts["actions_waiting_on"] == 2
    assert counts["actions_owned"] == 1
    assert counts["checklist_items_owned"] == 1
    assert counts["checklist_items_waiting_on"] == 1
    assert list(counts)[:4] == [
        "actions_owned",
        "actions_waiting_on",
        "checklist_items_owned",
        "checklist_items_waiting_on",
    ]


def _v3_instance(tmp_path: Path) -> Path:
    instance = tmp_path / "demo"
    instance.mkdir()
    (instance / "brain.toml").write_text(render_config(principal=PRINCIPAL, agents=AGENTS), encoding="utf-8")
    (instance / "ROUTER.md").write_text(render_router("demo"), encoding="utf-8")
    migrations = tmp_path / "schema-v3"
    migrations.mkdir()
    source = Path(__file__).resolve().parents[1] / "src" / "brain" / "migrations"
    for name in ("0001_core.sql", "0002_search.sql", "0003_meetings.sql"):
        shutil.copy(source / name, migrations / name)
    migrate(instance / "brain.sqlite3", create=True, migrations_dir=migrations)
    now = "2026-10-01T00:00:00Z"
    digest = store.sha256_text("Old kettle notes.")
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO sources (id, kind, title, source_date, content, content_sha256, captured_at, "
            "captured_by) VALUES ('src-old', 'user_note', 'Old', '2026-10-01', 'Old kettle notes.', ?, ?, 'tester:v3')",
            (digest, now),
        )
        for person, name in (("per-robin-example", "Robin Example"), ("per-sam-example", "Sam Example")):
            con.execute(
                "INSERT INTO people (id, display_name, source_id, locator, version, created_at, updated_at, "
                "updated_by) VALUES (?, ?, 'src-old', 'synthetic', 1, ?, ?, 'tester:v3')",
                (person, name, now, now),
            )
        con.execute(
            "INSERT INTO projects (id, kind, title, source_id, locator, version, created_at, updated_at, "
            "updated_by) VALUES ('general', 'workstream', 'General', 'src-old', 'synthetic', 1, ?, ?, 'tester:v3')",
            (now, now),
        )
        for row_id, owner, waiting in (
            ("act-one", "per-robin-example", "per-sam-example"),
            ("act-two", None, "per-robin-example"),
            ("act-three", None, None),
        ):
            con.execute(
                "INSERT INTO actions (id, title, status, status_basis, project_id, owner_person_id, "
                "waiting_on_person_id, last_verified_at, source_id, locator, version, created_at, updated_at, "
                "updated_by) VALUES (?, 'Descale', 'open', 'observed', 'general', ?, ?, ?, 'src-old', "
                "'synthetic', 1, ?, ?, 'tester:v3')",
                (row_id, owner, waiting, now, now, now),
            )
        for row_id, owner, waiting in (
            ("chk-one", "per-sam-example", None),
            ("chk-two", None, "per-robin-example"),
            ("chk-three", None, None),
        ):
            con.execute(
                "INSERT INTO checklist_items (id, project_id, title, state, state_basis, owner_person_id, "
                "waiting_on_person_id, last_verified_at, source_id, locator, version, created_at, updated_at, "
                "updated_by) VALUES (?, 'general', 'Check', 'not_started', 'observed', ?, ?, ?, 'src-old', "
                "'synthetic', 1, ?, ?, 'tester:v3')",
                (row_id, owner, waiting, now, now, now),
            )
        con.execute("COMMIT")
    finally:
        con.close()
    return instance


def test_migrate_3_to_4_backfills(tmp_path: Path) -> None:
    instance = _v3_instance(tmp_path)
    assert ro_rows(instance, "PRAGMA user_version") == [(3,)]
    assert migrate(instance / "brain.sqlite3") == (3, latest_version())
    assert ro_rows(instance, "PRAGMA user_version") == [(latest_version(),)]
    assert ro_rows(
        instance, "SELECT action_id, role, position, person_id FROM action_people ORDER BY action_id, role"
    ) == [
        ("act-one", "owner", 0, "per-robin-example"),
        ("act-one", "waiting_on", 0, "per-sam-example"),
        ("act-two", "waiting_on", 0, "per-robin-example"),
    ]
    assert ro_rows(
        instance, "SELECT item_id, role, position, person_id FROM checklist_item_people ORDER BY item_id"
    ) == [
        ("chk-one", "owner", 0, "per-sam-example"),
        ("chk-two", "waiting_on", 0, "per-robin-example"),
    ]
    assert ro_rows(instance, "SELECT count(*) FROM unchecked_text") == [(0,)]
    assert store.check_instance(instance) == []
    check = subprocess.run(
        ["brain", "check", "--instance", str(instance)], capture_output=True, text=True, timeout=30
    )
    assert check.returncode == 0, check.stdout + check.stderr


def test_check_reports_people_mismatch(p6) -> None:
    instance, apply = p6
    _people_setup(
        apply,
        upsert_op("upsert_action", _action(waiting_on_person_ids=["per-robin-example"]), "src-p6"),
        upsert_op("upsert_checklist_item", _item(), "src-p6"),
    )
    assert store.check_instance(instance) == []
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute("UPDATE action_people SET person_id = 'per-sam-example' WHERE action_id = 'act-p6'")
        con.execute(
            "INSERT INTO checklist_item_people (item_id, role, position, person_id) "
            "VALUES ('chk-p6', 'owner', 0, 'per-kim-example')"
        )
        con.execute("COMMIT")
    finally:
        con.close()
    problems = store.check_instance(instance)
    assert "action_people act-p6 waiting_on: people list does not match waiting_on_person_id" in problems
    assert "checklist_item_people chk-p6 owner: people list does not match owner_person_id" in problems


def test_check_reports_people_gap(p6) -> None:
    instance, apply = p6
    _people_setup(
        apply,
        upsert_op("upsert_action", _action(owner_person_ids=["per-robin-example", "per-sam-example"]), "src-p6"),
    )
    con = context_connection(instance)
    try:
        con.execute("BEGIN")
        con.execute("UPDATE action_people SET position = 2 WHERE action_id = 'act-p6' AND position = 1")
        con.execute("COMMIT")
    finally:
        con.close()
    assert "action_people act-p6 owner: people list does not match owner_person_id" in store.check_instance(instance)


# -- version and router ----------------------------------------------------------------------


def test_version_and_schema(tmp_path: Path) -> None:
    result = subprocess.run(["brain", "--version"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert result.stdout.strip() == version_banner()
    init = subprocess.run(["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stderr
    assert ro_rows(tmp_path / "demo", "PRAGMA user_version") == [(latest_version(),)]


def test_router_bytes(tmp_path: Path) -> None:
    assert len(render_router("demo").encode("utf-8")) == router_bytes("demo")
    assert len(render_router("a" + "0" * 39).encode("utf-8")) == router_bytes("a" + "0" * 39)
    init = subprocess.run(["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert init.returncode == 0, init.stderr
    assert len((tmp_path / "demo" / "ROUTER.md").read_bytes()) == router_bytes("demo")
