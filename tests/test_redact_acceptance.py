"""Acceptance 1 to 4 on the redaction fixture.
Every name and number here is invented."""
from __future__ import annotations

import json
import re
import sqlite3
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from brain import backup, redact, scrub, store
from brain.applier import Applier
from tests.conftest import context_connection, ro_rows, upsert_op
from tests.phase2a import custom_instance, export, item, run_collect, sources
from tests.phase2b import (
    CONTROLS, EXTRA_PLACES, K, KEYS2, M, OPERATOR, PATH, PLACES, REASON, REF, bundle_members_hit, controls_now,
    find_in, places_before, redact_now, scan_instance, seed, windows,
)
from tests.test_carry_forward import AGENTS
from tests.test_truth import Clock, _apply, _ok, clock  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def test_acceptance_1_every_byte(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    before = scan_instance(instance)
    assert "brain.sqlite3" in before and sum(1 for name in before if name.startswith("backups/")) == 2
    seeded = places_before(instance)
    assert all(count > 0 for count in seeded.values()), seeded
    kept = controls_now(instance)
    assert all(count > 0 for count in kept.values()), kept
    assert len(windows()) == 55
    result = redact_now(fixture)
    assert result["status"] == "redacted"
    assert scan_instance(instance) == {}
    assert not (instance / "REDACTING").exists()
    assert ro_rows(instance, "PRAGMA freelist_count") == [(0,)]
    # the bundle outside the instance is the control: it keeps the text
    assert any(bundle_members_hit(path) for path in fixture.outside.iterdir())
    assert controls_now(instance) == kept
    assert store.check_instance(instance) == []


def run_variant(tmp_path, clock, monkeypatch, *, optimize: bool, vacuum: bool, exports: bool = True, bundles: bool = True):
    fixture = seed(tmp_path, clock)
    if not optimize:
        monkeypatch.setattr(redact, "_optimize", lambda con: None)
    if not vacuum:
        monkeypatch.setattr(redact, "_vacuum", lambda con, source_id: None)
        monkeypatch.setattr(redact, "_freelist", lambda con: 0)
    if not exports:
        monkeypatch.setattr(redact, "delete_exports", lambda *a, **k: [])
    if not bundles:
        monkeypatch.setattr(redact, "replace_bundles", lambda folder, name, keep, own: ([], []))
    redact_now(fixture)
    hits = scan_instance(fixture.instance)
    db = hits.get("brain.sqlite3", [])
    bundle_hits = {name: found for name, found in hits.items() if name.startswith("backups/")}
    return {
        "exact": M.encode() in db,
        "lower": M.lower().encode() in db,
        "windows": len(db),
        "post_bundle": any("post-redaction" in name for name in bundle_hits),
        "old_bundle": any("post-redaction" not in name for name in bundle_hits),
        "export": any(name.startswith("fetch/") for name in hits),
    }


def test_acceptance_1_each_step_is_needed(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    rows = {}
    for name, (optimize, vacuum) in {"none": (False, False), "vacuum": (False, True), "optimize": (True, False), "both": (True, True)}.items():
        with monkeypatch.context() as patch:
            rows[name] = run_variant(tmp_path / name, clock, patch, optimize=optimize, vacuum=vacuum)
    print("TABLE 1", json.dumps(rows, sort_keys=True))
    assert rows["none"]["exact"] and rows["none"]["lower"] and rows["none"]["windows"] == 55 and rows["none"]["post_bundle"]
    assert not rows["vacuum"]["exact"] and rows["vacuum"]["lower"] and 0 < rows["vacuum"]["windows"] < 55 and rows["vacuum"]["post_bundle"]
    assert rows["optimize"]["exact"] and rows["optimize"]["lower"] and rows["optimize"]["windows"] == 55 and rows["optimize"]["post_bundle"]
    assert not rows["both"]["exact"] and not rows["both"]["lower"] and rows["both"]["windows"] == 0 and not rows["both"]["post_bundle"]
    with monkeypatch.context() as patch:
        skipped = run_variant(tmp_path / "noexport", clock, patch, optimize=True, vacuum=True, exports=False)
    assert skipped["export"]
    with monkeypatch.context() as patch:
        skipped = run_variant(tmp_path / "nobundle", clock, patch, optimize=True, vacuum=True, bundles=False)
    assert skipped["old_bundle"]


def test_acceptance_2_refetch_and_held(tmp_path: Path, clock: Clock) -> None:
    from brain.extract import held as held_list

    fixture = seed(tmp_path, clock)
    instance = fixture.instance
    before = ro_rows(instance, "SELECT status FROM write_log WHERE change_id = ?", (fixture.held,))
    assert before == [("held",)]
    redact_now(fixture)
    row = ro_rows(instance, "SELECT status, error, hold_reason, applied_seq FROM write_log WHERE change_id = ?", (fixture.held,))[0]
    assert row == ("rejected", "redacted on 2026-10-09", "", None)
    listing = json.dumps(held_list(instance, 50))
    assert fixture.held not in listing and fixture.held_other in listing
    app = Applier(instance)
    try:
        from brain.applier import NotAllowed

        for call in (lambda: app.approve(fixture.held, "tester"), lambda: app.refuse(fixture.held, "tester", "no")):
            with pytest.raises(NotAllowed) as err:
                call()
            assert str(err.value) == f"change {fixture.held} is already rejected"
        assert app.approve(fixture.held_other, "tester").receipt["status"] == "applied"
    finally:
        app.close()
    doc = export(
        [item(PATH, TEXT_OF_ITEM, date="2026-10-07", uri=f"https://example.com/{M}/page"), item("new/doc.md", "A brand new document about nothing.")],
        fetched_at="2026-10-09T05:00:00Z",
    )
    clock.at("2026-10-09T06:00:00Z")
    code, payload, err = run_collect(instance, doc)
    assert code == 0 and payload["status"] == "ok", (payload, err)
    assert len(sources(instance)) == 2
    assert store.check_instance(instance) == []
    assert scan_instance(instance) == {}


from tests.phase2b import TEXT as TEXT_OF_ITEM  # noqa: E402


def test_acceptance_3_unrelated_audit(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS2)
    doc = export([item("a/secret.md", f'Contact "{M}" for the vault.'), item("b/plain.md", "The Pro plan includes 25 seats.")])
    code, payload, err = run_collect(instance, doc)
    assert code == 0, (payload, err)
    sid = sources(instance, "document:work-docs/a/secret.md")[-1][0]
    before = ro_rows(instance, "SELECT change_id, packet_json, payload_sha256 FROM write_log WHERE instr(packet_json, ?) > 0", (sid,))
    assert len(before) == 1
    original_ops = json.loads(before[0][1])["ops"]
    redact.run(instance, sid, REASON, OPERATOR)
    after = ro_rows(instance, "SELECT packet_json, payload_sha256 FROM write_log WHERE change_id = ?", (before[0][0],))[0]
    packet = json.loads(after[0])
    mine = [i for i, op in enumerate(original_ops) if op.get("row", {}).get("id") == sid]
    assert packet["redacted_ops"] == mine and len(packet["ops"]) == len(original_ops)
    for index, (was, now) in enumerate(zip(original_ops, packet["ops"])):
        if index in mine:
            assert now["row"]["content"] == "[redacted]" and now["row"]["title"] == "redacted"
        else:
            assert json.dumps(now, sort_keys=True) == json.dumps(was, sort_keys=True), index
    assert after[1] == before[0][2]
    assert store.check_instance(instance) == []


def test_acceptance_4_check_gate_freelist_and_triggers(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    redact_now(fixture)
    instance = fixture.instance
    assert store.check_instance(instance) == []
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
    assert ro_rows(instance, "PRAGMA freelist_count") == [(0,)]
    con = sqlite3.connect(instance / "brain.sqlite3", isolation_level=None)
    sql = con.execute("SELECT sql FROM sqlite_schema WHERE name = 'sources_never_change'").fetchone()[0]
    con.execute("DROP TRIGGER sources_never_change")
    con.execute(sql.replace("WHEN NOT EXISTS", "WHEN 0 AND NOT EXISTS"))
    con.close()
    assert store.check_instance(instance) == ["schema: trigger sources_never_change differs from the engine's schema"]


def test_every_scrubbed_column_is_seeded() -> None:
    """The fixture seeds exactly the token and spans columns of the inventory, and the kept controls
    are the places that stay."""
    inventory = {key for key, kind in scrub.DISPOSITION.items() if kind in ("token", "spans")}
    assert set(PLACES) == inventory
    assert set(EXTRA_PLACES) == {"search_index", "records.edited_by_other"}
    assert set(CONTROLS) == {
        "claims.scope", "claims.locator", "write_log.intent", "checklist_items.section",
        "checklist_items.external_ref", "people.display_name", "systems.name", "records.body(unquoted)",
    }
    for key in CONTROLS:
        assert scrub.DISPOSITION.get(key, "kept") == "kept"


def test_kept_controls_hold_the_marker_before_and_after(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    before = controls_now(fixture.instance)
    assert before and all(count > 0 for count in before.values())
    redact_now(fixture)
    assert controls_now(fixture.instance) == before


def test_redaction_time(tmp_path: Path, clock: Clock) -> None:
    """A 10 MiB store with 2,000 claims on 200 keys redacts in under 30 s."""
    from tests.test_truth import _clm

    keys = [{"key": f"bulk.k{i:03d}", "value_type": "text"} for i in range(200)]
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=keys)
    sentences = [f"Item {i} reads number {i} in the vault ledger today." for i in range(2000)]
    filler = " ".join(f"Filler line {i} says nothing about the ledger or anything else here." for i in range(20000))
    docs = [item("bulk/ledger.md", " ".join(sentences))] + [item(f"bulk/filler{n}.md", filler) for n in range(5)]
    code, payload, err = run_collect(instance, export(docs))
    assert code == 0, (payload, err)
    sid = sources(instance, "document:work-docs/bulk/ledger.md")[-1][0]
    for start in range(0, 2000, 250):
        ops = [
            _clm(f"clm-bulk-{i:05d}", f"bulk.k{i % 200:03d}", f"number {i}", sentences[i], sid)
            for i in range(start, start + 250)
        ]
        _ok(_apply(instance, "bot", *ops))
    size = (instance / "brain.sqlite3").stat().st_size
    started = time.perf_counter()
    redact.run(instance, sid, "privacy request 2026-11-02", OPERATOR)
    elapsed = time.perf_counter() - started
    print(f"REDACTION TIME {elapsed:.2f} s, store {size / 1048576:.1f} MiB before, 2000 claims on 200 keys")
    assert size > 10 * 1024 * 1024
    assert elapsed < 30, elapsed
    assert ro_rows(instance, "SELECT count(*) FROM claims WHERE status = 'live'") == [(0,)]
    assert store.check_instance(instance) == []


# -- the runbook ------------------------------------------------------------------------------------

def _runbook() -> str:
    return (ROOT / "docs" / "REDACTION.md").read_text(encoding="utf-8")


def test_the_runbook_names_every_location() -> None:
    text = _runbook()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needle in ("brain redact", "tmutil addexclusion", "Unverified", "--rescan"):
        assert needle in text, needle
    locations = (
        "<instance>/backups/", "<instance>/fetch/inbox/", "A folder you gave to `brain backup --out`", "Hub `/backups`",
        "Hub offsite bucket", "Bucket versioning or object lock", "VM disk snapshots", "Time Machine and other machine backups",
        "A synced folder", "Agent harness session logs", "Another brain, or agent memory",
    )
    for location in locations:
        assert any(line.startswith("|") and location in line for line in text.splitlines()), location
    assert "docs/REDACTION.md" in readme
    for sentence in (
        "Restoring a bundle made before the redaction brings the text back",
        "To abandon a redaction that was stopped before it committed",
        "restore the latest `post-redaction` bundle",
        "brain read --args",
        "journal_mode = DELETE",
        "could not be checked and may hold the text",
    ):
        assert sentence in " ".join(text.split()), sentence
    commands = [line for line in text.splitlines() if "prune-offsite" in line]
    assert commands and all("--remote NAME:PATH" in line for line in commands)
    assert not any(line.lstrip().startswith("./hub prune-offsite") for line in text.splitlines())


def test_the_runbook_self_check_sees_the_database(tmp_path: Path) -> None:
    text = _runbook()
    section = text.split("## How to check yourself", 1)[1]
    lines = [line.strip() for line in section.splitlines() if line.startswith("    ")]
    assert len(lines) == 2
    grep_line, loop_line = lines
    phrase = "a phrase from the removed text"
    fixture = tmp_path / "Brains" / "work"
    (fixture / "backups").mkdir(parents=True)
    (fixture / "brain.sqlite3").write_bytes(b"SQLite format 3\x00junk A PHRASE from the removed text more\x00")
    (fixture / "plain.txt").write_text("nothing here")
    command = grep_line.replace("~/Brains/work", str(fixture))
    found = subprocess.run(command, shell=True, capture_output=True, text=True).stdout
    assert str(fixture / "brain.sqlite3") in found and "plain.txt" not in found
    assert " -I" not in grep_line and "-rIl" not in grep_line
    import io
    import tarfile

    bundle = fixture / "backups" / "work-20261009T000000Z-seq1.tar.gz"
    with tarfile.open(bundle, "w:gz") as tar:
        data = b"SQLite format 3\x00junk the PHRASE here " + phrase.encode() + b"\x00"
        info = tarfile.TarInfo("brain.sqlite3")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    command = loop_line.replace("~/Brains/work", str(fixture))
    out = subprocess.run(command, shell=True, capture_output=True, text=True, executable=shutil.which("zsh") or "/bin/sh").stdout
    assert out.strip() == "1"
