"""Part B: `brain apply-config` in batches of 500 ops. Every key and value here is invented."""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from brain import store
from brain.applier import Applier, Unavailable
from brain.init import apply_config, init_instance
from tests.bundles import cli
from tests.conftest import make_packet, render_config, ro_rows
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

BASE_KEYS = [
    {"key": "demo.region", "value_type": "enum", "allowed_values": ["au", "us"]},
    {"key": "demo.seats", "value_type": "int"},
]


def bulk(count: int, start: int = 0) -> list[dict]:
    return [{"key": f"bulk.k{number:04d}", "value_type": "int"} for number in range(start, start + count)]


def config(extra: int, keys=BASE_KEYS) -> str:
    return render_config(fact_keys=[*keys, *bulk(extra)])


def small(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return Path(init_instance("demo", tmp_path, config_text=config(0))["instance"])


def apply_cli(instance: Path) -> tuple[int, dict, str]:
    code, out, err = cli("apply-config", "--instance", str(instance))
    return code, json.loads(out) if out.strip() else {}, err


def write_toml(instance: Path, text: str) -> bytes:
    (instance / "brain.toml").write_text(text, encoding="utf-8")
    return (instance / "brain.toml").read_bytes()


def log(instance: Path) -> list[tuple]:
    return ro_rows(instance, "SELECT change_id, status, intent, packet_json FROM write_log WHERE agent = 'brain' ORDER BY rowid")


def ops_of(row: tuple) -> list[dict]:
    return json.loads(row[3])["ops"]


def test_a_small_config_keeps_the_single_packet_form(tmp_path: Path, clock: Clock) -> None:
    instance = small(tmp_path)
    raw = write_toml(instance, config(499))  # one add_source plus 499 keys is 500 ops
    code, payload, err = apply_cli(instance)
    assert code == 0, err
    assert set(payload) == {"status", "receipt", "warnings"}
    rows = log(instance)[1:]
    assert len(rows) == 1 and len(ops_of(rows[0])) == 500
    assert re.fullmatch(r"brain-config-[0-9]{8}t[0-9]{6}z-[0-9a-f]{6}", rows[0][0]), rows[0][0]
    assert rows[0][2] == f"apply brain.toml (sha256 {hashlib.sha256(raw).hexdigest()[:16]})"


def test_501_ops_split_500_and_1(tmp_path: Path, clock: Clock) -> None:
    instance = small(tmp_path)
    raw = write_toml(instance, config(500))  # 501 ops
    code, payload, err = apply_cli(instance)
    assert code == 0, err
    digest = hashlib.sha256(raw).hexdigest()[:16]
    rows = log(instance)[1:]
    assert len(rows) == 2
    for index, row in enumerate(rows):
        shape = hashlib.sha256(store.canonical_json(ops_of(row)).encode()).hexdigest()[:8]
        assert row[0] == f"brain-config-{digest}-{index:03d}-{shape}"
        assert row[2] == f"apply brain.toml (sha256 {digest}), batch {index + 1} of 2"
        assert row[1] == "applied"
    assert [len(ops_of(row)) for row in rows] == [500, 1]
    assert [op["op"] for op in ops_of(rows[0])].count("add_source") == 1 and ops_of(rows[0])[0]["op"] == "add_source"
    assert not any(op["op"] == "add_source" for op in ops_of(rows[1]))
    assert [batch["ops"] for batch in payload["batches"]] == [500, 1]


def test_920_keys_apply_in_two_batches(tmp_path: Path, clock: Clock) -> None:
    instance = small(tmp_path)
    write_toml(instance, config(920))
    started = time.perf_counter()
    code, payload, err = apply_cli(instance)
    elapsed = time.perf_counter() - started
    print(f"apply-config of 920 keys: {elapsed:.3f} s in {len(payload['batches'])} batches")
    assert code == 0, err
    assert [batch["ops"] for batch in payload["batches"]] == [500, 421]
    assert all(batch["status"] == "applied" for batch in payload["batches"])
    assert payload["receipt"]["change_id"] == payload["batches"][-1]["change_id"]
    assert ro_rows(instance, "SELECT count(*) FROM fact_keys") == [(922,)]
    code, out, _err = cli("check", "--instance", str(instance))
    assert (code, json.loads(out)) == (0, {"ok": True, "problems": []})


def test_a_rerun_is_a_noop(tmp_path: Path, clock: Clock) -> None:
    instance = small(tmp_path)
    write_toml(instance, config(520))
    assert apply_cli(instance)[0] == 0
    before = ro_rows(instance, "SELECT count(*) FROM write_log")
    started = time.perf_counter()
    code, payload, _err = apply_cli(instance)
    print(f"apply-config re-run: {time.perf_counter() - started:.3f} s")
    assert (code, payload) == (0, {"status": "nothing to apply", "warnings": []})
    assert ro_rows(instance, "SELECT count(*) FROM write_log") == before
    assert apply_config.last_batches == []


def test_batch_ids_are_deterministic(tmp_path: Path, clock: Clock) -> None:
    ids = []
    for name in ("one", "two"):
        instance = small(tmp_path / name)
        write_toml(instance, config(700))
        assert apply_cli(instance)[0] == 0
        ids.append([row[0] for row in log(instance)[1:]])
    assert ids[0] == ids[1] and len(ids[0]) == 2


def test_a_failed_batch_resumes(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = small(tmp_path)
    write_toml(instance, config(1200))  # 1201 ops: 500, 500, 201
    real = Applier.apply
    calls = []

    def flaky(self, raw):
        calls.append(1)
        if len(calls) == 2:
            raise Unavailable("instance is busy")
        return real(self, raw)

    monkeypatch.setattr(Applier, "apply", flaky)
    code, payload, err = apply_cli(instance)
    assert code == 5 and "instance is busy" in err
    applied = [row for row in log(instance)[1:] if row[1] == "applied"]
    assert len(applied) == 1 and len(ops_of(applied[0])) == 500
    first_id = applied[0][0]
    monkeypatch.setattr(Applier, "apply", real)
    code, payload, err = apply_cli(instance)
    assert code == 0, err
    rows = log(instance)[1:]
    assert len(rows) == 3 and all(row[1] == "applied" for row in rows)
    resumed = rows[1:]
    assert [len(ops_of(row)) for row in resumed] == [500, 201]
    assert first_id not in [row[0] for row in resumed]  # new ids, no collision with batch 1's
    assert resumed[0][0].split("-")[3] == "000" and resumed[1][0].split("-")[3] == "001"
    assert resumed[0][2].endswith("batch 1 of 2") and resumed[1][2].endswith("batch 2 of 2")
    assert not any(op["op"] == "add_source" for row in resumed for op in ops_of(row))
    code, out, _err = cli("check", "--instance", str(instance))
    assert json.loads(out)["ok"] is True
    # A resumed run of a single batch numbers its own batches.
    again = small(tmp_path / "two")
    write_toml(again, config(502))
    calls.clear()
    monkeypatch.setattr(Applier, "apply", flaky)
    assert apply_cli(again)[0] == 5
    monkeypatch.setattr(Applier, "apply", real)
    assert apply_cli(again)[0] == 0
    last = log(again)[-1]
    assert last[2].endswith("batch 1 of 1") and len(ops_of(last)) == 3


def _claimed(instance: Path) -> None:
    writer = Applier(instance)
    try:
        packet = make_packet(
            "tester",
            {"op": "add_source", "row": {"id": "src-region", "kind": "user_note", "title": "Region note", "source_date": "2026-10-01",
                                         "author_person_id": None, "content": "The demo region is US."}},
            {"op": "add_claim", "row": {"id": "clm-us", "key": "demo.region", "value_raw": "us", "basis": "reported",
                                        "quote": "The demo region is US"},
             "source": {"source_id": "src-region", "locator": "para 1"}},
        )
        assert writer.apply(json.dumps(packet).encode()).receipt["status"] == "applied"
    finally:
        writer.close()


NARROWED = [{"key": "demo.region", "value_type": "enum", "allowed_values": ["au"]}, BASE_KEYS[1]]


def test_a_rejected_batch_is_retried_under_a_suffix(tmp_path: Path, clock: Clock) -> None:
    instance = small(tmp_path)
    _claimed(instance)
    write_toml(instance, config(600, NARROWED))
    code, payload, err = apply_cli(instance)
    assert code == 1
    assert payload["receipt"]["status"] == "rejected" and "allowed_values would drop the current fact value" in payload["receipt"]["error"]
    assert "batches" not in payload  # the run stopped at the first batch
    first = [row for row in log(instance)[1:]]
    assert len(first) == 1 and first[0][1] == "rejected"
    # Fix the cause in the store: the writer replaces its claim.
    writer = Applier(instance)
    try:
        packet = make_packet(
            "tester",
            {"op": "add_source", "row": {"id": "src-region2", "kind": "user_note", "title": "Region note 2", "source_date": "2026-10-02",
                                         "author_person_id": None, "content": "The demo region is AU now."}},
            {"op": "add_claim", "row": {"id": "clm-au", "key": "demo.region", "value_raw": "au", "basis": "reported",
                                        "quote": "The demo region is AU", "supersedes": ["clm-us"]},
             "source": {"source_id": "src-region2", "locator": "para 1"}},
        )
        assert writer.apply(json.dumps(packet).encode()).receipt["status"] == "applied"
    finally:
        writer.close()
    code, payload, err = apply_cli(instance)
    assert code == 0, err
    brain_rows = [row for row in log(instance)[1:]]
    ids = [row[0] for row in brain_rows]
    assert ids[0] == first[0][0] and ids[1] == ids[0] + "-r2"
    assert [row[1] for row in brain_rows] == ["rejected", "applied", "applied"]
    assert ro_rows(instance, "SELECT count(*) FROM fact_keys") == [(602,)]


def test_the_twenty_first_retry_is_refused(tmp_path: Path, clock: Clock) -> None:
    instance = small(tmp_path)
    _claimed(instance)
    write_toml(instance, config(520, NARROWED))
    for attempt in range(20):
        code, payload, err = apply_cli(instance)
        assert code == 1 and payload["receipt"]["status"] == "rejected", attempt
    assert len([row for row in log(instance)[1:] if row[1] == "rejected"]) == 20
    code, payload, err = apply_cli(instance)
    assert (code, payload) == (1, {})
    assert err.strip() == "brain: apply-config refused: batch 1 was rejected 20 times; fix the cause, or change brain.toml"
    assert len(log(instance)) == 21  # nothing new was written


def test_init_with_a_large_config(tmp_path: Path, clock: Clock) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    result = init_instance("demo", tmp_path, config_text=config(600))
    instance = Path(result["instance"])
    assert result["status"] == "created" and result["receipt"]["status"] == "applied"
    rows = log(instance)
    assert len(rows) == 2 and result["receipt"]["change_id"] == rows[-1][0]
    assert ro_rows(instance, "SELECT count(*) FROM fact_keys") == [(602,)]
    assert store.check_instance(instance) == []
    code, out, _err = cli("check", "--instance", str(instance))
    assert json.loads(out)["ok"] is True
