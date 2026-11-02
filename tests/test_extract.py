from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from brain import candidates
from brain import schemas
from brain import store
from brain.cli import main
from brain.validate import validate_packet
from tests.collector_fixtures import block_network, collect, gmail_doc
from tests.conftest import attribution_refusal, call, file_sha, make_packet, ro_rows, source_op
from tests.extract_fixtures import SCAN, candidate, extract, extract_instance, held, review

DECISION_ID = "dec-63dfe78e9fcafa59f83ad34d"
ACTION_ID = "act-1d808128fe7566fec33b3dad"
LINK_ID = "lnk-4fb1d41307273e5aec24840d"
PLAN_THREAD = "thr-email-8e3d3b6316b2c9cbd01eb768"
CHANGE_IDS = {
    "decision_candidate": "scan-78a911977dbb2cfc8d5fede3",
    "action_candidate": "scan-66053619efc673f6b99bdda7",
    "claim_candidate": "scan-70550cc5e18843ce8649ce97",
}
# Under approval = "required" a held candidate's hold reason is the verdict
# under verified, and brain extract prints it.
DECISION_HOLD = "brain: op 0 (apply_candidate): a decision candidate always needs a person\n"
STORED_HOLD = "brain: op 0 (apply_candidate): approval is required; under verified it would be stored\n"
HOLDS = {"decision_candidate": DECISION_HOLD, "action_candidate": STORED_HOLD, "claim_candidate": STORED_HOLD}
CLAIM_OPS = (
    {
        "op": "add_claim",
        "row": {
            # The extractor scan is the first part of the id.
            "id": "clm-42c2e1fc74bb45e8b67c174b",
            "key": "widget.pricing",
            "value_raw": "40 dollars",
            "basis": "reported",
            "quote": "Widget pricing starts at 40 dollars a seat",
        },
        "source": {"source_id": "src-note-alex", "locator": "para 2"},
    },
)
DECISION_OPS = (
    {
        "op": "upsert_decision",
        "base": None,
        "source": {"source_id": "src-note-alex", "locator": "para 1"},
        "row": {
            "id": DECISION_ID,
            "title": "Drop the nightly backups from the old server",
            "detail": "",
            "quote": "We will drop the nightly backups from the old server on Friday",
            "decided_on": "2026-10-06",
            "decided_by_person_id": "per-alex-example",
            "status": "in_effect",
            "project_id": "ops",
            "supersedes_id": None,
        },
    },
)
ACTION_OPS = (
    {
        "op": "upsert_action",
        "base": None,
        "source": {"source_id": "src-note-team", "locator": "para 1"},
        "row": {
            "id": ACTION_ID,
            "title": "Send the migration plan",
            "detail": "",
            "next_step": "",
            "status": "waiting",
            "status_basis": "reported",
            "project_id": "ops",
            "owner_person_id": None,
            "waiting_on_person_id": "per-sam-example",
            "due_date": "2026-10-09",
            "due_kind": "deadline",
            "asked_on": "2026-10-06",
            "last_verified_at": "ignored",
        },
    },
    {
        "op": "upsert_thread_link",
        "base": None,
        "source": {"source_id": "src-note-team", "locator": "para 1"},
        "row": {
            "id": LINK_ID,
            "action_id": ACTION_ID,
            "thread_id": PLAN_THREAD,
            "role": "awaiting_reply",
            "since": "2026-10-06T01:00:00Z",
        },
    },
)


def _without_verified(ops) -> list[dict]:
    cleaned = []
    for op in ops:
        row = dict(op["row"])
        row.pop("last_verified_at", None)
        cleaned.append({**op, "row": row})
    return cleaned


def _reorder(value):
    if isinstance(value, dict):
        keys = list(value)
        rotated = keys[1:] + keys[:1] if len(keys) > 1 else keys
        return {key: _reorder(value[key]) for key in rotated}
    if isinstance(value, list):
        return [_reorder(item) for item in value]
    return value


def _empty_targets(instance: Path) -> None:
    for table in ("decisions", "actions", "thread_links"):
        assert ro_rows(instance, f"SELECT count(*) FROM {table}")[0][0] == 0


def test_apply_candidate_must_be_alone_in_its_packet() -> None:
    doc = candidate("decision_candidate")
    mixed = make_packet(
        "scan",
        {"op": "apply_candidate", "candidate": doc},
        source_op("src-extra", "Extra source text for the packet test."),
        change_id="scan-alone",
    )
    mixed_errors = validate_packet(mixed)
    assert {
        "path": "/ops/0/op",
        "message": "apply_candidate must be the only op in its packet",
    } in mixed_errors

    rebuild = make_packet(
        "tester",
        {"op": "rebuild_search"},
        {"op": "rebuild_search"},
        change_id="tester-rebuildtwo",
    )
    rebuild_errors = validate_packet(rebuild)
    assert {
        "path": "/ops/0/op",
        "message": "rebuild_search must be the only op in its packet",
    } in rebuild_errors
    assert {
        "path": "/ops/1/op",
        "message": "rebuild_search must be the only op in its packet",
    } in rebuild_errors

    missing = candidate("decision_candidate")
    del missing["quote"]
    bare = make_packet(
        "scan",
        {"op": "apply_candidate", "candidate": missing},
        change_id="scan-noquote",
    )
    bare_errors = validate_packet(bare)
    assert {"path": "/ops/0/candidate/quote", "message": "is required"} in bare_errors


def test_extract_usage_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = extract_instance(tmp_path)
    store_path = instance / "brain.sqlite3"
    before = file_sha(store_path)

    code, _payload, err = extract(instance, candidate("decision_candidate"), agent="tester")
    assert code == 2
    assert err == "brain: agent tester is not an extractor\n"
    assert file_sha(store_path) == before

    code, _payload, err = extract(instance, candidate("decision_candidate"), agent="nobody")
    assert code == 2
    assert err == "brain: agent nobody is not an extractor\n"
    assert file_sha(store_path) == before

    big = tmp_path / "big.json"
    big.write_bytes(b"x" * (1024 * 1024 + 1))
    stdout_big, err_big = _main_capture(
        ["extract", "--instance", str(instance), "--as", "scan", str(big)]
    )
    assert stdout_big == ""
    assert err_big == "brain: candidate is larger than 1 MiB\n"
    assert file_sha(store_path) == before

    missing = tmp_path / "missing.json"
    _code, _payload, err = _main_result(
        ["extract", "--instance", str(instance), "--as", "scan", str(missing)]
    )
    assert _code == 2
    assert err.startswith("brain: candidate is unreadable: ")
    assert file_sha(store_path) == before

    monkeypatch.delenv("BRAIN_INSTANCE", raising=False)
    code, _payload, err = _main_result(["extract", "--as", "scan", str(big)])
    assert code == 2
    assert "Traceback" not in err
    assert file_sha(store_path) == before

    (instance / "PAUSED").write_text("investigate\n", encoding="utf-8")
    code, _payload, err = extract(instance, candidate("decision_candidate"))
    assert code == 5
    assert err == "brain: instance is paused: see PAUSED; investigate, then delete it\n"
    assert file_sha(store_path) == before


def _main_capture(argv: list[str]) -> tuple[str, str]:
    import contextlib
    import io

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        main(argv)
    return stdout.getvalue(), stderr.getvalue()


def _main_result(argv: list[str]) -> tuple[int, dict | None, str]:
    import contextlib
    import io

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    text = stdout.getvalue().strip()
    payload = json.loads(text) if text else None
    return code, payload, stderr.getvalue()


def test_valid_candidates_wait_for_an_approver(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    covered = []
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        doc = candidate(kind)
        code, payload, err = extract(instance, doc)
        assert err == HOLDS[kind]
        assert code == 4
        assert payload["duplicate"] is False
        assert payload["kind"] == kind
        assert payload["change_id"] == CHANGE_IDS[kind]
        assert payload["receipt"]["status"] == "held"
        assert payload["receipt"]["error"] == ""
        preview = payload["preview"]
        assert preview["errors"] == []
        assert preview["valid"] is True
        if kind == "claim_candidate":
            assert preview["appliable"] is True
            assert preview["ops"] == list(CLAIM_OPS)
        elif kind == "decision_candidate":
            assert preview["appliable"] is True
            assert _without_verified(preview["ops"]) == _without_verified(DECISION_OPS)
        else:
            assert preview["appliable"] is True
            assert _without_verified(preview["ops"]) == _without_verified(ACTION_OPS)
        stored = ro_rows(
            instance,
            "SELECT packet_json FROM write_log WHERE change_id = ?",
            (payload["change_id"],),
        )
        packet = json.loads(stored[0][0])
        assert packet["ops"] == [{"op": "apply_candidate", "candidate": doc}]
        _empty_targets(instance)
    assert covered == list(schemas.CANDIDATE_KINDS)
    assert candidates.change_id("scan", candidate("decision_candidate")) == CHANGE_IDS["decision_candidate"]


def test_repeated_extract_is_a_duplicate(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    doc = candidate("decision_candidate")
    first_code, first, first_err = extract(instance, doc)
    assert first_err == DECISION_HOLD
    assert first_code == 4
    second_code, second, second_err = extract(instance, doc)
    assert second_code == 4
    assert second_err == DECISION_HOLD
    assert second["duplicate"] is True
    assert second["change_id"] == first["change_id"]
    assert second["receipt"] == first["receipt"]
    assert second["preview"] is not None
    reordered_code, reordered, reordered_err = extract(instance, _reorder(doc))
    assert reordered_code == 4
    assert reordered_err == DECISION_HOLD
    assert reordered["duplicate"] is True
    assert reordered["change_id"] == first["change_id"]
    assert reordered["receipt"] == first["receipt"]
    assert reordered["preview"] is not None
    assert ro_rows(
        instance,
        "SELECT count(*) FROM write_log WHERE change_id = ?",
        (first["change_id"],),
    )[0][0] == 1
    approved_code, approved, approved_err = review(instance, "approve", first["change_id"])
    assert approved_err == ""
    assert approved_code == 0
    assert approved["status"] == "applied"
    again_code, again, again_err = extract(instance, doc)
    assert again_code == 0
    assert again_err == ""
    assert again["duplicate"] is True
    assert again["preview"] is None
    assert again["change_id"] == first["change_id"]


def test_concurrent_extracts_hold_one_packet(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    raw = store.canonical_json(candidate("decision_candidate")).encode()
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            from brain.extract import run

            results.append(run(instance, "scan", raw))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(results) == 2
    assert [item.exit_code for item in results] == [4, 4]
    assert results[0].payload["change_id"] == results[1].payload["change_id"]
    assert sorted(item.payload["duplicate"] for item in results) == [False, True]
    assert ro_rows(
        instance,
        "SELECT count(*) FROM write_log WHERE change_id = ?",
        (results[0].payload["change_id"],),
    )[0][0] == 1


def _packet_json(instance: Path, change_id: str) -> str:
    return ro_rows(
        instance, "SELECT packet_json FROM write_log WHERE change_id = ?", (change_id,)
    )[0][0]


def test_approving_a_decision_candidate_applies_it(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    doc = candidate("decision_candidate")
    code, payload, err = extract(instance, doc)
    assert code == 4 and err == DECISION_HOLD
    code, receipt, err = review(instance, "approve", payload["change_id"])
    assert code == 0 and err == ""
    assert receipt["status"] == "applied"
    assert receipt["targets"] == [{"table": "decisions", "id": DECISION_ID, "action": "insert"}]
    con = store.connect_reader(instance / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        stored = dict(con.execute("SELECT * FROM decisions WHERE id = ?", (DECISION_ID,)).fetchone())
    finally:
        con.close()
    for key, value in DECISION_OPS[0]["row"].items():
        assert stored[key] == value
    got = call(instance, "get", entity="decision", id=DECISION_ID)
    assert got.is_error is False
    assert got.structured["row"]["id"] == DECISION_ID
    assert got.structured["row"]["title"] == doc["fields"]["title"]
    found = call(instance, "search", query="nightly backups")
    assert DECISION_ID in {hit["id"] for hit in found.structured["hits"]}


def test_approving_an_action_candidate_links_its_thread(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path, "gmail-work")
    with block_network():
        code, payload, err = extract(instance, candidate("action_candidate"))
        assert code == 4 and err == STORED_HOLD
        code, receipt, err = review(instance, "approve", payload["change_id"])
        assert code == 0 and err == ""
        assert receipt["status"] == "applied"
        ids = {item["id"] for item in receipt["targets"]}
        assert ids == {ACTION_ID, LINK_ID}
        link = ro_rows(
            instance,
            "SELECT since, role, status FROM thread_links WHERE id = ?",
            (LINK_ID,),
        )[0]
        assert link == ("2026-10-06T01:00:00Z", "awaiting_reply", "active")
        waiting = call(instance, "get", entity="action", id=ACTION_ID)
        assert waiting.is_error is False
        shown = waiting.structured["related"]["thread_links"]["items"]
        assert shown[0]["id"] == LINK_ID
        assert shown[0]["role"] == "awaiting_reply"
        assert shown[0]["status"] == "active"
        assert shown[0]["reply_state"] == "waiting"
        collected, body, collect_err = collect(
            instance,
            "gmail-work",
            gmail_doc(
                threads=[
                    {
                        "id": "thread-plan",
                        "subject": "Migration plan",
                        "messages": [
                            {"at": "2026-10-06T01:00:00Z", "direction": "outbound"},
                            {"at": "2026-10-07T09:00:00Z", "direction": "inbound"},
                        ],
                    }
                ]
            ),
        )
        assert collected == 0, (body, collect_err)
        replied = call(instance, "get", entity="action", id=ACTION_ID)
        assert replied.structured["related"]["thread_links"]["items"][0]["reply_state"] == "replied"


def test_approval_rechecks_the_store(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    first = candidate("decision_candidate")
    second = candidate("decision_candidate")
    second["fields"]["detail"] = "Kept for the record."
    code, held_first, err = extract(instance, first)
    assert code == 4 and held_first["preview"]["errors"] == [] and held_first["preview"]["valid"] is True
    code, held_second, err = extract(instance, second)
    assert code == 4 and err == DECISION_HOLD
    assert held_second["preview"]["errors"] == [] and held_second["preview"]["valid"] is True
    assert held_first["change_id"] != held_second["change_id"]
    code, applied, err = review(instance, "approve", held_first["change_id"])
    assert code == 0 and applied["status"] == "applied"
    code, rejected, err = review(instance, "approve", held_second["change_id"])
    assert code == 1 and err == ""
    assert rejected["status"] == "rejected"
    assert rejected["error"] == (
        "approved by tester but not applied: op 0 (apply_candidate): candidate is invalid: "
        "/fields/title decision dec-63dfe78e9fcafa59f83ad34d already exists for this source and title"
    )


def test_derived_ops_go_through_the_evidence_gate(tmp_path: Path) -> None:
    action_instance = extract_instance(tmp_path / "action")
    quote_instance = extract_instance(tmp_path / "quote")
    quoted_instance = extract_instance(tmp_path / "quoted")
    action = candidate("action_candidate")
    action["fields"]["detail"] = "Alex suggested a later launch date."
    code, held, err = extract(action_instance, action)
    assert code == 4 and err == (
        "brain: op 0.0 (upsert_action): evidence gate: "
        + attribution_refusal("Alex suggested a later launch date.")
        + "\n"
    )
    code, rejected, err = review(action_instance, "approve", held["change_id"])
    assert code == 1 and err == ""
    assert rejected["error"] == (
        "approved by tester but not applied: op 0.0 (upsert_action): evidence gate: "
        + attribution_refusal("Alex suggested a later launch date.")
    )
    decision = candidate("decision_candidate")
    decision["source_id"] = "src-note-team"
    decision["quote"] = "The security review moves to Thursday"
    decision["fields"]["title"] = "Move the security review to Thursday"
    decision["fields"]["decided_by"] = "per-alex-example"
    code, held, err = extract(quote_instance, decision)
    assert code == 4 and err == DECISION_HOLD
    code, rejected, err = review(quote_instance, "approve", held["change_id"])
    assert code == 1
    assert rejected["error"] == (
        "approved by tester but not applied: op 0.0 (upsert_decision): evidence gate: "
        "a decision by Alex Example needs its quote from a source authored by per-alex-example; "
        "src-note-team is authored by nobody. Cite an existing source they wrote, such as a collected "
        "email, Slack message or transcript, or record who actually decided."
    )
    quoted = candidate("decision_candidate")
    quoted["fields"]["detail"] = (
        'Alex wrote "we will drop the nightly backups from the old server" in the notes.'
    )
    code, held, err = extract(quoted_instance, quoted)
    assert code == 4 and err == DECISION_HOLD
    code, applied, err = review(quoted_instance, "approve", held["change_id"])
    assert code == 0 and err == ""
    assert applied["status"] == "applied"


def test_derived_ops_respect_the_extractors_operations_and_scope(tmp_path: Path) -> None:
    scan2 = dict(SCAN)
    scan2["operations"] = ["apply_candidate", "upsert_decision"]
    scan3 = dict(SCAN)
    scan3["operations"] = ["apply_candidate", "upsert_action"]
    scan3["scope"] = {"projects": ["general"]}
    decisions = extract_instance(tmp_path / "decisions", agents={"scan2": scan2})
    scoped = extract_instance(tmp_path / "scoped", agents={"scan3": scan3})
    general = extract_instance(tmp_path / "general", agents={"scan3": scan3})
    unlinked = extract_instance(tmp_path / "unlinked", agents={"scan3": scan3})
    rewritten = extract_instance(tmp_path / "rewritten")

    action = candidate("action_candidate")
    code, held, err = extract(decisions, action, agent="scan2")
    assert code == 4 and err == "brain: op 0.0 (upsert_action): not allowed for agent scan2\n"
    code, rejected, err = review(decisions, "approve", held["change_id"])
    assert code == 1
    assert rejected["error"] == (
        "approved by tester but not applied: op 0.0 (upsert_action): not allowed for agent scan2"
    )

    code, held, err = extract(scoped, candidate("action_candidate"), agent="scan3")
    assert code == 4
    code, rejected, err = review(scoped, "approve", held["change_id"])
    assert rejected["error"] == (
        "approved by tester but not applied: op 0.0 (upsert_action): "
        "project ops is outside agent scan3's scope [general]"
    )

    moved = candidate("action_candidate")
    moved["fields"]["workstream"] = "general"
    code, held, err = extract(general, moved, agent="scan3")
    assert code == 4
    code, rejected, err = review(general, "approve", held["change_id"])
    assert rejected["error"] == (
        "approved by tester but not applied: "
        "op 0.1 (upsert_thread_link): not allowed for agent scan3"
    )

    bare = candidate("action_candidate")
    bare["fields"]["workstream"] = "general"
    del bare["fields"]["thread"]
    code, held, err = extract(unlinked, bare, agent="scan3")
    assert code == 4
    code, applied, err = review(unlinked, "approve", held["change_id"])
    assert code == 0 and err == ""
    assert applied["status"] == "applied"

    code, held, err = extract(rewritten, candidate("decision_candidate"))
    assert code == 4
    config_path = rewritten / "brain.toml"
    text = config_path.read_text(encoding="utf-8")
    old = '"apply_candidate", "upsert_decision", "upsert_action", "upsert_thread_link"'
    new = '"apply_candidate", "upsert_action", "upsert_thread_link"'
    assert old in text
    config_path.write_text(text.replace(old, new, 1), encoding="utf-8")
    code, rejected, err = review(rewritten, "approve", held["change_id"])
    assert code == 1
    assert rejected["error"].startswith("approved by tester but not applied: ")
    assert rejected["error"].endswith("op 0.0 (upsert_decision): not allowed for agent scan")


def test_refused_and_failed_candidates_keep_their_packet(tmp_path: Path) -> None:
    refused = extract_instance(tmp_path / "refused")
    failed = extract_instance(tmp_path / "failed")
    code, held, err = extract(refused, candidate("decision_candidate"))
    assert code == 4 and err == DECISION_HOLD
    before = _packet_json(refused, held["change_id"])
    code, receipt, err = review(refused, "refuse", held["change_id"], note="not this one")
    assert code == 1 and err == ""
    assert receipt["status"] == "rejected"
    assert receipt["error"] == "refused by tester: not this one"
    assert _packet_json(refused, held["change_id"]) == before
    rec_code, rec, rec_err = _main_result(
        ["receipt", "--instance", str(refused), held["change_id"]]
    )
    assert rec_code == 1 and rec_err == ""
    assert rec["reviewed_by"] == "tester"

    original = candidate("decision_candidate")
    variant = candidate("decision_candidate")
    variant["fields"]["detail"] = "A second reading of the same decision."
    code, first, err = extract(failed, original)
    code, second, err = extract(failed, variant)
    assert code == 4
    before = _packet_json(failed, second["change_id"])
    code, applied, err = review(failed, "approve", first["change_id"])
    assert code == 0 and applied["status"] == "applied"
    code, rejected, err = review(failed, "approve", second["change_id"])
    assert code == 1
    assert rejected["status"] == "rejected"
    assert _packet_json(failed, second["change_id"]) == before
    rec_code, rec, rec_err = _main_result(
        ["receipt", "--instance", str(failed), second["change_id"]]
    )
    assert rec["reviewed_by"] == "tester"


_INSTANCE_FAULTS = {
    "decision_candidate": (
        lambda doc: doc["fields"].__setitem__("decided_by", "per-nobody"),
        {"path": "/fields/decided_by", "message": "person per-nobody doesn't exist"},
    ),
    "action_candidate": (
        lambda doc: doc["fields"].__setitem__("workstream", "sales"),
        {"path": "/fields/workstream", "message": "sales is not a workstream of this instance"},
    ),
    "claim_candidate": (
        lambda doc: doc["fields"].__setitem__("key", "gadget pricing"),
        {"path": "/fields/key", "message": "unknown fact key gadget pricing"},
    ),
}


def _invoke(argv: list[str]) -> tuple[int, str, str]:
    import contextlib
    import io

    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _assert_validate_output(instance: Path, raw: bytes, directory: Path) -> None:
    import hashlib

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{hashlib.sha256(raw).hexdigest()[:16]}.json"
    path.write_bytes(raw)
    before = file_sha(instance / "brain.sqlite3")
    code, out, err = _invoke(
        ["extract", "--instance", str(instance), "--as", "scan", str(path)]
    )
    valid_code, valid_out, valid_err = _invoke(["validate", "candidate", str(path)])
    assert code == 1
    assert code == valid_code
    assert out == valid_out
    assert err == valid_err == ""
    assert file_sha(instance / "brain.sqlite3") == before


def test_acceptance_8_bad_candidate_json_is_held_not_applied(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    _assert_validate_output(instance, b"{", tmp_path / "brace")
    _assert_validate_output(instance, b"[]", tmp_path / "list")
    covered = []
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        doc = candidate(kind)
        del doc["fields"][schemas.CANDIDATE_KINDS[kind]["required"][0]]
        _assert_validate_output(instance, store.canonical_json(doc).encode(), tmp_path / kind)
    assert covered == list(schemas.CANDIDATE_KINDS)

    covered = []
    held_ids = {}
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        doc = candidate(kind)
        change, expected = _INSTANCE_FAULTS[kind]
        change(doc)
        code, payload, err = extract(instance, doc)
        assert code == 4 and err == (
            f"brain: op 0 (apply_candidate): candidate is invalid: {expected['path']} {expected['message']}\n"
        )
        assert payload["receipt"]["status"] == "held"
        assert payload["receipt"]["error"] == ""
        assert payload["preview"]["valid"] is False
        assert payload["preview"]["errors"] == [expected]
        held_ids[kind] = payload["change_id"]
        _empty_targets(instance)
    assert covered == list(schemas.CANDIDATE_KINDS)

    code, rejected, err = review(instance, "approve", held_ids["decision_candidate"])
    assert code == 1 and err == ""
    assert rejected["error"] == (
        "approved by tester but not applied: op 0 (apply_candidate): candidate is invalid: "
        "/fields/decided_by person per-nobody doesn't exist"
    )
    code, rejected, err = review(instance, "approve", held_ids["action_candidate"])
    assert code == 1 and err == ""
    assert rejected["error"].endswith(
        "/fields/workstream sales is not a workstream of this instance"
    )
    # A claim candidate is approved like any other, so its fault rejects it.
    code, rejected, err = review(instance, "approve", held_ids["claim_candidate"])
    assert code == 1 and err == ""
    assert rejected["error"] == (
        "approved by tester but not applied: op 0 (apply_candidate): candidate is invalid: "
        "/fields/key unknown fact key gadget pricing"
    )
    for kind in ("decision_candidate", "action_candidate", "claim_candidate"):
        status = ro_rows(
            instance,
            "SELECT status FROM write_log WHERE change_id = ?",
            (held_ids[kind],),
        )[0][0]
        assert status == "rejected"
    _empty_targets(instance)
    assert ro_rows(instance, "SELECT count(*) FROM claims")[0][0] == 0


def test_claim_candidates_cannot_be_approved(tmp_path: Path) -> None:
    # Claim candidates apply now. The name is kept from 0.7.0a1; the approver
    # rules are unchanged, and an approved claim candidate stores its claim.
    instance = extract_instance(tmp_path)
    code, payload, err = extract(instance, candidate("claim_candidate"))
    assert code == 4 and err == STORED_HOLD
    change_id = "scan-70550cc5e18843ce8649ce97"
    assert payload["change_id"] == change_id
    before = file_sha(instance / "brain.sqlite3")

    code, body, err = review(instance, "approve", change_id, agent="plain")
    assert code == 1
    assert body is None
    assert err == "brain: plain may not review changes from scan\n"
    assert file_sha(instance / "brain.sqlite3") == before
    assert ro_rows(
        instance, "SELECT status FROM write_log WHERE change_id = ?", (change_id,)
    )[0][0] == "held"

    code, body, err = review(instance, "approve", change_id)
    assert code == 0 and err == ""
    assert body["status"] == "applied"
    claim = ro_rows(
        instance,
        "SELECT id, key, value_int, value_raw, basis, checked, proposed_by, status FROM claims",
    )
    assert claim == [
        ("clm-42c2e1fc74bb45e8b67c174b", "widget.pricing", 40, "40 dollars", "reported", 1, "scan", "live")
    ]
    assert ro_rows(instance, "SELECT value_int, status FROM facts WHERE key = 'widget.pricing'") == [(40, "current")]

    code, body, err = review(instance, "refuse", change_id, note="no product module")
    assert code == 1 and body is None
    assert err == f"brain: change {change_id} is already applied\n"


_EXTRACTOR_ONLY = "agent scan is an extractor and submits only through brain extract"


def _apply_packet(instance: Path, packet: dict) -> tuple[int, dict, str]:
    import contextlib
    import io

    path = instance.parent / f"{packet['change_id']}.json"
    path.write_text(store.canonical_json(packet), encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["apply", "--instance", str(instance), str(path)])
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def _extract_packet(agent: str, doc: dict) -> dict:
    return {
        "packet_version": 1,
        "change_id": candidates.change_id(agent, doc),
        "agent": agent,
        "intent": f"extract {doc['kind']} from {doc['source_id']}",
        "ops": [{"op": "apply_candidate", "candidate": doc}],
    }


def test_extractors_submit_only_through_brain_extract(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    built = _extract_packet("scan", candidate("action_candidate"))
    plain = make_packet(
        "scan",
        DECISION_OPS[0],
        change_id="scan-directupsert00000001",
        intent="a direct upsert",
    )
    for packet in (built, plain):
        code, payload, err = _apply_packet(instance, packet)
        assert code == 1, (payload, err)
        assert payload["status"] == "rejected"
        assert payload["error"] == _EXTRACTOR_ONLY

    from brain.applier import Applier

    writer = Applier(instance)
    try:
        outcome = writer.apply(
            store.canonical_json(_extract_packet("scan", candidate("claim_candidate"))).encode()
        )
    finally:
        writer.close()
    assert outcome.exit_code == 1
    assert outcome.receipt["status"] == "rejected"
    assert outcome.receipt["error"] == _EXTRACTOR_ONLY

    code, held, err = extract(instance, candidate("decision_candidate"))
    assert code == 4 and err == DECISION_HOLD
    code, applied, err = review(instance, "approve", held["change_id"])
    assert code == 0 and err == ""
    assert applied["status"] == "applied"
    assert applied["targets"] == [{"table": "decisions", "id": DECISION_ID, "action": "insert"}]


def test_brain_held_lists_previews_and_limits(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path)
    covered = []
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        code, payload, err = extract(instance, candidate(kind))
        assert code == 4 and err == HOLDS[kind]
    assert covered == list(schemas.CANDIDATE_KINDS)

    code, payload, err = _apply_packet(
        instance,
        make_packet(
            "careful",
            DECISION_OPS[0],
            change_id="careful-heldpacket0001",
            intent="wait for a review",
        ),
    )
    assert code == 4, (payload, err)
    assert payload["status"] == "held"

    before = file_sha(instance / "brain.sqlite3")
    code, body, err = held(instance)
    assert code == 0 and err == ""
    assert file_sha(instance / "brain.sqlite3") == before
    items = body["held"]
    assert body["more"] == 0
    assert len(items) == 4
    order = ro_rows(
        instance,
        "SELECT change_id, agent, intent, decided_at FROM write_log "
        "WHERE status = 'held' ORDER BY decided_at, change_id",
    )
    assert [(item["change_id"], item["agent"], item["intent"], item["decided_at"]) for item in items] == [
        tuple(row) for row in order
    ]
    by_id = {item["change_id"]: item for item in items}
    for kind, change_id in CHANGE_IDS.items():
        item = by_id[change_id]
        assert item["kind"] == kind
        assert item["preview"]["errors"] == []
        if kind == "claim_candidate":
            assert item["preview"]["valid"] is True
            assert item["preview"]["appliable"] is True
            assert item["preview"]["ops"] == list(CLAIM_OPS)
        elif kind == "decision_candidate":
            assert item["preview"]["appliable"] is True
            assert item["preview"]["ops"] == list(DECISION_OPS)
        else:
            assert item["preview"]["appliable"] is True
            assert _without_verified(item["preview"]["ops"]) == _without_verified(ACTION_OPS)
    careful = by_id["careful-heldpacket0001"]
    assert careful["kind"] is None
    assert careful["preview"] is None

    code, limited, err = held(instance, "--limit", "2")
    assert code == 0 and err == ""
    assert limited["more"] == 2
    assert limited["held"] == items[:2]
    assert file_sha(instance / "brain.sqlite3") == before

    for bad in ("0", "501"):
        code, payload, err = held(instance, "--limit", bad)
        assert code == 2
        assert payload is None
        assert err == "brain: --limit must be between 1 and 500\n"
        assert file_sha(instance / "brain.sqlite3") == before

    (instance / "PAUSED").write_text("paused\n", encoding="utf-8")
    code, paused, err = held(instance)
    assert code == 0 and err == ""
    assert paused == body
    assert file_sha(instance / "brain.sqlite3") == before

    missing = tmp_path / "missing"
    code, payload, err = held(missing)
    assert code == 5
    assert payload is None
    assert err == f"brain: no store at {missing / 'brain.sqlite3'}\n"


def test_extract_runs_with_network_blocked(tmp_path: Path) -> None:
    prepared = []
    covered = []
    for kind in schemas.CANDIDATE_KINDS:
        covered.append(kind)
        prepared.append(
            (
                kind,
                extract_instance(tmp_path / f"{kind}-approve"),
                extract_instance(tmp_path / f"{kind}-refuse"),
            )
        )
    assert covered == list(schemas.CANDIDATE_KINDS)
    with block_network():
        for kind, approve_at, refuse_at in prepared:
            doc = candidate(kind)
            code, payload, err = extract(approve_at, doc)
            assert code == 4 and err == HOLDS[kind]
            code, body, err = held(approve_at)
            assert code == 0 and err == ""
            assert payload["change_id"] in {item["change_id"] for item in body["held"]}
            code, approved, err = review(approve_at, "approve", payload["change_id"])
            # Claim candidates apply on approval too.
            assert code == 0 and err == ""
            assert approved["status"] == "applied"

            code, payload, err = extract(refuse_at, doc)
            assert code == 4 and err == HOLDS[kind]
            code, body, err = held(refuse_at)
            assert code == 0 and err == ""
            code, refused, err = review(refuse_at, "refuse", payload["change_id"])
            assert code == 1 and err == ""
            assert refused["status"] == "rejected"


def test_extraction_modules_import_no_network_or_model_library() -> None:
    import ast

    allowed = {
        "__future__",
        "dataclasses",
        "json",
        "pathlib",
        "sqlite3",
        "brain.applier",
        "brain.candidates",
        "brain.config",
        "brain.ingress",
        "brain.migrate",
        "brain.store",
        "brain.validate",
    }
    forbidden = {"open", "os", "environ", "getenv", "socket", "subprocess", "urllib", "http", "ssl"}
    path = Path(__file__).resolve().parents[1] / "src" / "brain" / "extract.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed, alias.name
                assert alias.asname not in forbidden
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            assert node.module in allowed, node.module
            for alias in node.names:
                assert alias.name not in forbidden
                assert alias.asname not in forbidden
        elif isinstance(node, ast.Name):
            assert node.id not in forbidden
        elif isinstance(node, ast.Attribute):
            assert node.attr not in forbidden
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            assert node.name not in forbidden
        elif isinstance(node, ast.arg):
            assert node.arg not in forbidden

    banned = {
        "http",
        "urllib",
        "socket",
        "ssl",
        "requests",
        "httpx",
        "aiohttp",
        "openai",
        "anthropic",
    }
    root = Path(__file__).resolve().parents[1] / "src" / "brain"
    modules = list(root.rglob("*.py"))
    assert modules
    for module in modules:
        parsed = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(parsed):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            for name in imported:
                assert name.split(".", 1)[0] not in banned, (module.name, name)
