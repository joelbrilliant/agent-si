"""Brain backup and brain restore.
Bundles are built, taken apart and attacked by hand. Every name and number here is invented."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest

from brain import __version__, backup, migrate, store
from brain.init import render_router
from tests.bundles import (
    backup_cli,
    cli,
    digest_of,
    info,
    manifest_for,
    read_bundle,
    rebuilt,
    restore_cli,
    write_bundle,
)
from tests.conftest import context_connection, read_instance, render_config, ro_rows
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

STAMP = "20261009T000000Z"


def world(tmp_path: Path) -> Path:
    (tmp_path / "live").mkdir(exist_ok=True)
    return read_instance(tmp_path / "live")


def seq_of(instance: Path) -> int:
    return ro_rows(instance, "SELECT max(applied_seq) FROM write_log")[0][0]


def good(tmp_path: Path, clock: Clock, *extra: str) -> tuple[Path, Path, dict]:
    """(instance, bundle path, receipt)"""
    instance = world(tmp_path)
    code, receipt, err = backup_cli(instance, tmp_path / "out", *extra)
    assert code == 0, err
    return instance, Path(receipt["bundle"]), receipt


def leftovers(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir() if path.name.startswith((".backup-", ".tmp-")))


# -- 1 to 10: backup -------------------------------------------------------------------------------


def test_backup_writes_a_bundle_and_manifest(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    started = time.perf_counter()
    code, receipt, err = backup_cli(instance, tmp_path / "out")
    elapsed = time.perf_counter() - started
    assert code == 0, err
    print(f"backup of the read fixture store: {elapsed:.3f} s, {receipt['bytes']} bytes")
    seq = seq_of(instance)
    bundle = Path(receipt["bundle"])
    assert bundle.name == f"demo-{STAMP}-seq{seq}.tar.gz" and bundle.parent == (tmp_path / "out").resolve()
    assert set(receipt) == {"status", "bundle", "bytes", "label", "schema", "applied_seq", "write_log_rows", "pruned", "skipped"}
    rows = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    assert receipt == {"status": "backed_up", "bundle": str(bundle), "bytes": bundle.stat().st_size, "label": "",
                       "schema": migrate.latest_version(), "applied_seq": seq, "write_log_rows": rows, "pruned": [], "skipped": []}
    with tarfile.open(bundle, "r:gz") as tar:
        assert [member.name for member in tar] == ["manifest.json", "ROUTER.md", "brain.sqlite3", "brain.toml"]
    manifest, members = read_bundle(bundle)
    log = ro_rows(instance, "SELECT change_id, agent, status, decided_at, applied_seq, reviewed_by, reviewed_at, payload_sha256 FROM write_log")
    last = ro_rows(instance, "SELECT max(decided_at) FROM write_log")[0][0]
    assert manifest == {
        "format": "agent-brain.backup.v1", "engine": __version__, "schema": migrate.latest_version(), "created_at": "2026-10-09T00:00:00Z",
        "instance": "demo", "label": "", "applied_seq": seq, "write_log_rows": rows, "last_decided_at": last,
        "write_log_digest": digest_of(log), "paused": False,
        "members": {name: {"sha256": hashlib.sha256(blob).hexdigest(), "size": len(blob)} for name, blob in members.items()},
    }
    assert members["brain.toml"] == (instance / "brain.toml").read_bytes()
    assert members["ROUTER.md"] == (instance / "ROUTER.md").read_bytes()
    assert (instance / "PAUSED").exists() is False and leftovers(tmp_path / "out") == []


def test_backup_bundle_mode_and_verification(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = world(tmp_path)
    out = tmp_path / "out"
    out.mkdir(mode=0o755)
    out.chmod(0o755)
    modes = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *args, **kwargs):
        if Path(str(path)).name.startswith(".tmp-") and flags & os.O_CREAT:
            modes.append(mode)
        return real_open(path, flags, mode, *args, **kwargs)

    old = os.umask(0)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(backup.os, "open", spy)
            code, receipt, err = backup_cli(instance, out)
    finally:
        os.umask(old)
    assert code == 0, err
    assert modes == [0o600]
    bundle = Path(receipt["bundle"])
    assert oct(bundle.stat().st_mode & 0o777) == "0o600"
    assert sorted(path.name for path in out.iterdir()) == [bundle.name]
    # A bundle that fails its own read-back: a truncated write.
    real = backup._write_bundle

    def short(handle, members):
        real(handle, members)
        handle.flush()
        handle.truncate(handle.tell() // 2)

    monkeypatch.setattr(backup, "_write_bundle", short)
    clock.at("2026-10-09T00:00:05Z")
    code, payload, err = backup_cli(instance, out)
    assert code == 1 and payload is None
    assert err.strip() == "brain: backup refused: the bundle failed its own verification"
    assert sorted(path.name for path in out.iterdir()) == [bundle.name]


def test_backup_label_and_same_second_refusal(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    out = tmp_path / "out"
    message = "brain: --label must be 1 to 30 lower-case letters, digits and hyphens, starting with a letter"
    for bad in ("Bad", "9x", "a" * 31, "pre update", "ok\n", "x_y", "x" * 40):
        code, payload, err = backup_cli(instance, out, "--label", bad)
        assert (code, payload, err.strip()) == (2, None, message), bad
    code, receipt, err = backup_cli(instance, out, "--label", "before-import")
    assert code == 0 and receipt["label"] == "before-import"
    assert Path(receipt["bundle"]).name == f"demo-{STAMP}-seq{seq_of(instance)}-before-import.tar.gz"
    code, payload, err = backup_cli(instance, out, "--label", "before-import")
    assert code == 1 and payload is None
    assert err.strip() == f"brain: backup refused: {out / Path(receipt['bundle']).name} already exists"
    assert leftovers(out) == []
    assert len(list(out.iterdir())) == 1


def test_backup_refuses_a_store_that_fails_check(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    con = context_connection(instance)
    try:
        con.execute("DELETE FROM search_keys WHERE rowid = (SELECT min(rowid) FROM search_keys)")
        con.commit()
    finally:
        con.close()
    out = tmp_path / "out"
    code, payload, err = backup_cli(instance, out)
    assert code == 1 and payload is None
    assert err.startswith("brain: backup refused: brain check found problems: search drift, ")
    assert [path.name for path in out.iterdir()] == []


def test_backup_members(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    fetch = instance / "fetch"
    (fetch / "inbox" / "work-docs").mkdir(parents=True)
    (fetch / "inbox" / "work-docs" / "20261008T000000Z-01.json").write_text("{}")
    (fetch / "mark.key").write_text("a" * 64)
    (fetch / ".lock-work-docs").write_text("")
    (fetch / "work-docs.state.json").write_text('{"format":1}')
    (fetch / "real.txt").write_text("{}")
    (fetch / "linked.state.json").symlink_to(fetch / "real.txt")
    (fetch / "x.state.json").write_text("{}")  # one character: not a valid collector name
    (instance / "PAUSED").write_text("test")
    code, receipt, err = backup_cli(instance, tmp_path / "out")
    assert code == 0
    manifest, members = read_bundle(Path(receipt["bundle"]))
    assert sorted(members) == ["ROUTER.md", "brain.sqlite3", "brain.toml", "fetch/work-docs.state.json"]
    assert members["fetch/work-docs.state.json"] == b'{"format":1}'


def test_backup_reads_cursors_before_the_snapshot(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = world(tmp_path)
    events: list[str] = []
    real_cursors, real_connect = backup._read_cursors, store.connect_readonly

    class Spy:
        def __init__(self, con):
            self._con = con

        def backup(self, target):
            events.append("backup")
            return self._con.backup(target)

        def __getattr__(self, name):
            return getattr(self._con, name)

    def cursors(path):
        events.append("cursors")
        return real_cursors(path)

    monkeypatch.setattr(backup, "_read_cursors", cursors)
    monkeypatch.setattr(store, "connect_readonly", lambda db: Spy(real_connect(db)))
    assert backup_cli(instance, tmp_path / "out")[0] == 0
    assert events == ["cursors", "backup"]


def _bundles(out: Path, name: str = "demo") -> set[str]:
    return {path.name for path in out.iterdir() if path.name.startswith(name + "-")}


def test_backup_prunes_pre_update_to_two(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    out = tmp_path / "out"
    made = []
    for hour in range(4):
        clock.at(f"2026-10-09T0{hour}:00:00Z")
        code, receipt, err = backup_cli(instance, out, "--label", "pre-update")
        assert code == 0, err
        made.append(Path(receipt["bundle"]).name)
        expected = made[-2:]
        assert _bundles(out) == set(expected)
    assert receipt["pruned"] == [made[1]]
    # Other labels, another instance's bundles and a name ending in a newline are never pruned.
    clock.at("2026-10-09T05:00:00Z")
    keep = backup_cli(instance, out, "--label", "weekly")[1]["bundle"]
    other = out / f"other-{STAMP}-seq1-pre-update.tar.gz"
    other.write_bytes(b"x")
    newline = out / "demo-20261001T000000Z-seq1-pre-update.tar.gz\n"
    newline.write_bytes(b"x")
    clock.at("2026-10-09T06:00:00Z")
    code, receipt, _err = backup_cli(instance, out, "--label", "pre-update")
    assert code == 0
    left = {path.name for path in out.iterdir()}
    assert Path(keep).name in left and other.name in left and newline.name in left
    assert len([name for name in left if name.endswith("-pre-update.tar.gz") and name.startswith("demo-2026100")]) == 2
    assert receipt["pruned"] == [made[2]]


def test_a_bundle_that_fails_verification_prunes_nothing(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = world(tmp_path)
    out = tmp_path / "out"
    for hour in range(2):
        clock.at(f"2026-10-09T0{hour}:00:00Z")
        assert backup_cli(instance, out, "--label", "pre-update")[0] == 0
    before = _bundles(out)
    real = backup._write_bundle

    def short(handle, members):
        real(handle, members)
        handle.flush()
        handle.truncate(20)

    monkeypatch.setattr(backup, "_write_bundle", short)
    clock.at("2026-10-09T02:00:00Z")
    code, _payload, err = backup_cli(instance, out, "--label", "pre-update")
    assert code == 1 and "failed its own verification" in err
    assert _bundles(out) == before and leftovers(out) == []


def test_backup_of_a_paused_instance(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    (instance / "PAUSED").write_text("investigating")
    code, receipt, err = backup_cli(instance, tmp_path / "out")
    assert code == 0 and "brain: instance is paused; backing up anyway" in err
    manifest, _members = read_bundle(Path(receipt["bundle"]))
    assert manifest["paused"] is True
    assert receipt["status"] == "backed_up"


def test_backup_is_byte_stable(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    first = backup_cli(instance, tmp_path / "one")[1]["bundle"]
    second = backup_cli(instance, tmp_path / "two")[1]["bundle"]
    assert Path(first).read_bytes() == Path(second).read_bytes()
    raw = Path(first).read_bytes()
    assert raw[:2] == b"\x1f\x8b" and raw[8] == 0  # XFL is 0 at level 6
    assert raw[4:8] == b"\x00\x00\x00\x00"  # no gzip mtime
    with tarfile.open(first, "r:gz") as tar:
        for member in tar:
            assert (member.mtime, member.mode, member.uid, member.gid, member.uname, member.gname) == (0, 0o600, 0, 0, "", "")


def test_backup_removes_stale_staging(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    stale_dir, stale_file = out / ".backup-old", out / ".tmp-1-old"
    fresh_dir, fresh_file = out / ".backup-new", out / ".tmp-2-new"
    for path in (stale_dir, fresh_dir):
        path.mkdir()
        (path / "brain.sqlite3").write_text("x")
    for path in (stale_file, fresh_file):
        path.write_text("x")
    now = 1_791_504_000  # 2026-10-09T00:00:00Z
    for path in (stale_dir, stale_file):
        os.utime(path, (now - 2 * 86400, now - 2 * 86400))
    for path in (fresh_dir, fresh_file):
        os.utime(path, (now - 3600, now - 3600))
    assert backup_cli(instance, out)[0] == 0
    assert sorted(path.name for path in out.iterdir() if path.name.startswith(".")) == [".backup-new", ".tmp-2-new"]


def test_backup_and_restore_cli(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    out = tmp_path / "out"
    code, receipt, err = backup_cli(instance, out)
    assert (code, err) == (0, "")
    bundle = receipt["bundle"]
    # 1: refused. Stdout stays empty.
    code, stdout, err = cli("restore", bundle, "--dir", str(instance))
    assert (code, stdout) == (1, "") and err.startswith("brain: restore refused: ")
    # 2: usage and unreadable.
    assert cli("backup", "--instance", str(instance))[0] == 2
    assert cli("backup", "--instance", str(instance), "--out", str(out), "--label", "No")[0] == 2
    code, stdout, err = cli("restore", str(tmp_path / "nothing.tar.gz"), "--dir", str(tmp_path / "r0"))
    assert (code, stdout) == (2, "") and err.strip() == "brain: bundle is unreadable: missing"
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a gzip")
    code, stdout, err = cli("restore", str(junk), "--dir", str(tmp_path / "r1"))
    assert code == 2 and err.startswith("brain: bundle is unreadable: ")
    # 5: no store, broken brain.toml.
    code, stdout, err = cli("backup", "--instance", str(tmp_path / "nowhere"), "--out", str(out))
    assert code == 5 and stdout == ""
    broken = tmp_path / "broken"
    shutil.copytree(instance, broken)
    (broken / "brain.toml").write_text("not = [valid")
    code, stdout, err = cli("backup", "--instance", str(broken), "--out", str(out))
    assert code == 5 and stdout == "" and err.startswith("brain: brain.toml: ")
    missing_db = tmp_path / "nodb"
    missing_db.mkdir()
    shutil.copy(instance / "brain.toml", missing_db / "brain.toml")
    code, stdout, err = cli("backup", "--instance", str(missing_db), "--out", str(out))
    assert code == 5 and err.strip() == f"brain: no store at {missing_db / 'brain.sqlite3'}"
    os.environ["BRAIN_INSTANCE"] = str(instance)
    try:
        clock.at("2026-10-09T00:00:09Z")
        assert cli("backup", "--out", str(out))[0] == 0
    finally:
        del os.environ["BRAIN_INSTANCE"]


# -- 11, 12: restore -------------------------------------------------------------------------------


def test_restore_round_trip(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    fetch = instance / "fetch"
    fetch.mkdir()
    big = json.dumps({"format": 1, "files": {f"f{n}": "x" * 40 for n in range(100_000)}}).encode()
    assert len(big) > 4 * 1024 * 1024
    (fetch / "work-docs.state.json").write_bytes(big + b" " * (5 * 1024 * 1024 - len(big)) if len(big) < 5 * 1024 * 1024 else big)
    (fetch / "team-docs.state.json").write_bytes(b'{"format":1,"files":{}}')
    code, receipt, err = backup_cli(instance, tmp_path / "out")
    assert code == 0, err
    started = time.perf_counter()
    code, restored, err = restore_cli(Path(receipt["bundle"]), tmp_path / "back")
    print(f"restore: {time.perf_counter() - started:.3f} s")
    assert code == 0, err
    back = tmp_path / "back"
    assert restored == {"status": "restored", "dir": str(back), "schema": migrate.latest_version(), "applied_seq": seq_of(instance),
                        "last_decided_at": ro_rows(instance, "SELECT max(decided_at) FROM write_log")[0][0],
                        "migrated_from": None, "follow_up": [], "router_refreshed": False, "fetch_cursors": 2}
    assert store.check_instance(back) == []
    assert sorted(path.name for path in back.iterdir()) == ["ROUTER.md", "brain.sqlite3", "brain.toml", "fetch"]
    sql = "SELECT * FROM write_log ORDER BY change_id"
    assert ro_rows(back, sql) == ro_rows(instance, sql)
    assert ro_rows(back, "PRAGMA user_version") == ro_rows(instance, "PRAGMA user_version")
    for name in ("work-docs", "team-docs"):
        assert (back / "fetch" / f"{name}.state.json").read_bytes() == (fetch / f"{name}.state.json").read_bytes()
    assert (back / "fetch").stat().st_mode & 0o777 == 0o700
    assert not (back / "fetch" / "inbox").exists() and not (back / "fetch" / "mark.key").exists()


def test_restored_store_works(tmp_path: Path, clock: Clock) -> None:
    from tests.phase2a import capture_instance, export, item, run_collect, sources
    from tests.bundles import cli as run_cli

    instance = capture_instance(tmp_path / "live")  # (the live directory is made by the helper)
    doc = export([item("specs/pricing.md", "The Pro plan costs 49 AUD a seat.")])
    code, first, _err = run_collect(instance, doc)
    assert code == 0
    code, receipt, err = backup_cli(instance, tmp_path / "out")
    assert code == 0, err
    code, restored, err = restore_cli(Path(receipt["bundle"]), tmp_path / "back")
    assert code == 0, err
    back = tmp_path / "back"
    # The same export again is a duplicate run, not a second set of sources.
    exports = tmp_path / "live" / "exports"
    path = next(exports.glob("*.json"))
    code, again, _err = run_collect(back, path)
    assert code == 0 and again["duplicate"] is True and again["run_id"] == first["run_id"]
    assert len(sources(back)) == 1
    # And a packet applies.
    packet = {"packet_version": 1, "change_id": "tester-after", "agent": "tester", "intent": "after",
              "ops": [{"op": "add_source", "row": {"id": "src-after", "kind": "user_note", "title": "After", "source_date": "2026-10-09",
                                                    "author_person_id": None, "content": "After the restore."}}]}
    p = tmp_path / "packet.json"
    p.write_text(json.dumps(packet))
    code, out, err = run_cli("apply", str(p), "--instance", str(back))
    assert code == 0, err
    assert store.check_instance(back) == []


# -- 13 to 20: restore refuses ----------------------------------------------------------------------


def refused(bundle: Path, target: Path, text: str, *, code: int = 1, untouched: bool = False) -> None:
    got, stdout, err = cli("restore", str(bundle), "--dir", str(target))
    assert (got, stdout) == (code, ""), (got, err)
    assert err.strip() == f"brain: {text}", err
    siblings = sorted(path.name for path in target.parent.iterdir())
    assert not any(".restore-" in name for name in siblings), siblings
    assert untouched or not target.exists() or not any(target.iterdir())


def test_restore_refuses_a_non_empty_target(tmp_path: Path, clock: Clock) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("mine")
    refused(bundle, full, f"restore refused: {full} is not empty; brain restore never overwrites", untouched=True)
    assert (full / "keep.txt").read_text() == "mine" and [p.name for p in full.iterdir()] == ["keep.txt"]
    afile = tmp_path / "afile"
    afile.write_text("x")
    refused(bundle, afile, f"restore refused: {afile} is not empty; brain restore never overwrites", untouched=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    code, receipt, err = restore_cli(bundle, empty)
    assert code == 0 and store.check_instance(empty) == []


def _members(blobs: dict) -> list:
    return [(info(name, len(blob)), blob) for name, blob in blobs.items()]


def test_restore_refuses_hostile_members(tmp_path: Path, clock: Clock) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    manifest, parts = read_bundle(bundle)
    raw = store.canonical_json(manifest).encode()
    head = [(info("manifest.json", len(raw)), raw)]
    body = {name: blob for name, blob in parts.items()}
    pieces = {name: (info(name, len(blob)), blob) for name, blob in body.items()}

    def make(label, *extra, replace=None, drop=()):
        entries = list(head)
        for name, (entry, blob) in pieces.items():
            if name in drop:
                continue
            entries.append((replace[name] if replace and name in replace else (entry, blob)))
        entries.extend(extra)
        return write_bundle(tmp_path / f"{label}.tar.gz", entries)

    before = sorted(path.name for path in tmp_path.iterdir())
    cases = []
    cases.append(("traversal", make("t1", (info("../x", 1), b"x")), "bundle member ../x is not allowed"))
    cases.append(("absolute", make("t2", (info("/etc/x", 1), b"x")), "bundle member /etc/x is not allowed"))
    cases.append(("journal", make("t3", (info("brain.sqlite3-journal", 1), b"x")), "bundle member brain.sqlite3-journal is not allowed"))
    cases.append(("hub", make("t4", (info("hub.sqlite3", 1), b"x")), "bundle member hub.sqlite3 is not allowed"))
    cases.append(("unlisted", make("t5", (info("fetch/x.txt", 1), b"x")), "bundle member fetch/x.txt is not allowed"))
    cases.append(("newline", make("t6", (info("fetch/ab.state.json\n", 1), b"x")), "bundle member fetch/ab.state.json\n is not allowed"))
    other = (info("brain.toml", 0, tarfile.SYMTYPE, "/etc/passwd"), None)
    cases.append(("symlink", make("t7", replace={"brain.toml": other}), "bundle member brain.toml is not a regular file"))
    cases.append(("hardlink", make("t8", replace={"ROUTER.md": (info("ROUTER.md", 0, tarfile.LNKTYPE, "brain.toml"), None)}),
                  "bundle member ROUTER.md is not a regular file"))
    cases.append(("device", make("t9", replace={"brain.toml": (info("brain.toml", 0, tarfile.CHRTYPE), None)}),
                  "bundle member brain.toml is not a regular file"))
    cases.append(("directory", make("t10", replace={"brain.toml": (info("brain.toml", 0, tarfile.DIRTYPE), None)}),
                  "bundle member brain.toml is not a regular file"))
    cases.append(("sparse", make("t11", replace={"ROUTER.md": (info("ROUTER.md", 5, tarfile.GNUTYPE_SPARSE), b"hello")}),
                  "bundle member ROUTER.md is not a regular file"))
    cases.append(("contiguous", make("t12", replace={"ROUTER.md": (info("ROUTER.md", 5, tarfile.CONTTYPE), b"hello")}),
                  "bundle member ROUTER.md is not a regular file"))
    cases.append(("duplicate", make("t13", (info("brain.toml", len(body["brain.toml"])), body["brain.toml"])), "bundle holds brain.toml twice"))
    big = b"x" * (64 * 1024 + 1)
    cases.append(("oversized", make("t14", replace={"ROUTER.md": (info("ROUTER.md", len(big)), big)}),
                  "bundle member ROUTER.md is larger than its limit"))
    first_over = 100 - (1 + len(body))  # the manifest and the three members come first
    many = [(info(f"fetch/aa{n:03d}.state.json", 1), b"x") for n in range(101)]
    cases.append(("members", make("t15", *many), f"bundle member fetch/aa{first_over:03d}.state.json is larger than its limit"))
    for label, path, text in cases:
        refused(path, tmp_path / f"target-{label}", f"restore refused: {text}")
    # No file outside the staging directory was created, and the targets don't exist.
    created = set(path.name for path in tmp_path.iterdir()) - set(before) - {name for _l, p, _t in cases for name in [p.name]}
    assert created == set()


def test_restore_refuses_a_non_sqlite_database_at_once(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    junk = b"\x00" * (5 * 1024 * 1024)
    path = rebuilt(tmp_path / "junk.tar.gz", bundle, members={"brain.sqlite3": junk})
    read = []
    real = tarfile.TarFile.extractfile

    def counting(self, member):
        handle = real(self, member)
        original = handle.read

        def read_counted(size=-1):
            data = original(size)
            if member.name == "brain.sqlite3":
                read.append(len(data))
            return data

        handle.read = read_counted
        return handle

    monkeypatch.setattr(tarfile.TarFile, "extractfile", counting)
    refused(path, tmp_path / "t", "restore refused: bundle database is not a rollback-journal SQLite file")
    assert sum(read) <= 1024 * 1024  # the first chunk only, not the rest of the 5 MiB


def test_restore_refuses_bad_manifests(tmp_path: Path, clock: Clock) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    manifest, parts = read_bundle(bundle)
    entries = [(info(name, len(blob)), blob) for name, blob in parts.items()]

    def with_manifest(label, raw):
        head = [] if raw is None else [(info("manifest.json", len(raw)), raw)]
        return write_bundle(tmp_path / f"{label}.tar.gz", head + entries)

    refused(with_manifest("m1", None), tmp_path / "a", "restore refused: bundle has no manifest.json")
    invalid = "restore refused: manifest.json is not a valid backup manifest"
    refused(with_manifest("m2", b"{not json"), tmp_path / "b", invalid)
    refused(with_manifest("m3", store.canonical_json({**manifest, "format": "other"}).encode()), tmp_path / "c", invalid)
    missing = {key: value for key, value in manifest.items() if key != "paused"}
    refused(with_manifest("m4", store.canonical_json(missing).encode()), tmp_path / "d", invalid)
    refused(with_manifest("m5", store.canonical_json({**manifest, "applied_seq": "3"}).encode()), tmp_path / "e", invalid)
    refused(with_manifest("m5b", b"[1, 2]"), tmp_path / "e2", invalid)
    refused(rebuilt(tmp_path / "m6.tar.gz", bundle, drop=("brain.toml",), keep_manifest=True), tmp_path / "f",
            "restore refused: bundle is missing brain.toml")
    stray = {**manifest["members"], "fetch/ghost.state.json": {"sha256": "0" * 64, "size": 1}}
    refused(rebuilt(tmp_path / "m7.tar.gz", bundle, manifest={"members": stray}, keep_manifest=True), tmp_path / "g",
            "restore refused: bundle is missing fetch/ghost.state.json")
    refused(rebuilt(tmp_path / "m8.tar.gz", bundle, members={"ROUTER.md": b"changed"}, keep_manifest=True), tmp_path / "h",
            "restore refused: bundle member ROUTER.md doesn't match its manifest entry")
    refused(rebuilt(tmp_path / "m9.tar.gz", bundle, members={"fetch/new.state.json": b"{}"}, keep_manifest=True), tmp_path / "i",
            "restore refused: bundle member fetch/new.state.json doesn't match its manifest entry")


def _tamper(tmp_path: Path, bundle: Path, label: str, steps) -> Path:
    """A bundle whose store was changed by steps(con), with the manifest rewritten to match."""
    _manifest, parts = read_bundle(bundle)
    work = tmp_path / f"work-{label}.sqlite3"
    work.write_bytes(parts["brain.sqlite3"])
    con = sqlite3.connect(work)
    con.create_function("brain_applier_context", 0, lambda: 1)
    try:
        steps(con)
        con.commit()
    finally:
        con.close()
    return rebuilt(tmp_path / f"{label}.tar.gz", bundle, members={"brain.sqlite3": work.read_bytes()})


def test_restore_refuses_a_changed_schema(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    prefix = "restore refused: bundle schema differs from the engine's: "
    ran = []
    monkeypatch.setattr(migrate, "migrate", lambda *a, **k: ran.append("migrate"))
    monkeypatch.setattr(store, "check_instance", lambda *a, **k: ran.append("check") or [])
    cases = {
        "trigger": (lambda c: c.execute("CREATE TRIGGER evil AFTER INSERT ON sources BEGIN DELETE FROM sources; END"),
                    "trigger evil is not in the engine's schema"),
        "table": (lambda c: c.execute("CREATE TABLE loot (x TEXT)"), "table loot is not in the engine's schema"),
        "view": (lambda c: c.execute("CREATE VIEW sneaky AS SELECT load_extension('x')"), "view sneaky is not in the engine's schema"),
        "stat": (lambda c: c.execute("ANALYZE"), "table sqlite_stat1 is not in the engine's schema"),
        "dropped": (lambda c: c.execute("DROP TRIGGER claims_never_deleted"), "trigger claims_never_deleted is missing"),
        "body": (lambda c: (c.execute("DROP TRIGGER claims_never_deleted"),
                            c.execute("CREATE TRIGGER claims_never_deleted BEFORE DELETE ON claims BEGIN SELECT 1; END")),
                 "trigger claims_never_deleted differs from the engine's schema"),
    }
    sql = ro_rows(_instance, "SELECT sql FROM sqlite_schema WHERE name = 'claims_never_deleted'")[0][0]
    cases["whitespace"] = (lambda c: (c.execute("DROP TRIGGER claims_never_deleted"), c.execute(sql.replace("BEFORE DELETE", "BEFORE  DELETE"))),
                           "trigger claims_never_deleted differs from the engine's schema")
    for label, (steps, text) in cases.items():
        path = _tamper(tmp_path, bundle, label, steps)
        refused(path, tmp_path / f"t-{label}", prefix + text)
    assert ran == []  # neither the migration nor the check ever ran: the planted trigger never fired


def _patched_header(tmp_path: Path, bundle: Path, label: str, edit) -> Path:
    _manifest, parts = read_bundle(bundle)
    blob = bytearray(parts["brain.sqlite3"])
    edit(blob)
    return rebuilt(tmp_path / f"{label}.tar.gz", bundle, members={"brain.sqlite3": bytes(blob)})


def test_restore_refuses_wal_and_future_schema(tmp_path: Path, clock: Clock) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)

    def wal(blob):
        blob[18] = blob[19] = 2

    def utf16(blob):
        blob[56:60] = (2).to_bytes(4, "big")

    def future(blob):
        blob[60:64] = (99).to_bytes(4, "big")

    def zero(blob):
        blob[60:64] = (0).to_bytes(4, "big")

    wal_text = "restore refused: bundle database is not a rollback-journal SQLite file"
    refused(_patched_header(tmp_path, bundle, "wal", wal), tmp_path / "a", wal_text)
    refused(_patched_header(tmp_path, bundle, "u16", utf16), tmp_path / "b", "restore refused: bundle database is not UTF-8")
    refused(_patched_header(tmp_path, bundle, "fut", future), tmp_path / "c",
            f"restore refused: bundle is at schema 99, newer than this engine ({migrate.latest_version()})")
    refused(_patched_header(tmp_path, bundle, "zero", zero), tmp_path / "d",
            "restore refused: bundle is at schema 0, not a version this engine reads")
    short = tmp_path / "short.tar.gz"
    refused(rebuilt(short, bundle, members={"brain.sqlite3": b"SQLite format 3\x00" + b"\x00" * 20}), tmp_path / "e", wal_text)


def test_restore_refuses_a_digest_mismatch(tmp_path: Path, clock: Clock) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)

    def edit(con):
        for name in ("write_log_decided_is_final", "write_log_identity_is_fixed"):
            pass
        sql = {row[0]: row[1] for row in con.execute("SELECT name, sql FROM sqlite_schema WHERE name LIKE 'write_log_%' AND type = 'trigger'")}
        con.execute("DROP TRIGGER write_log_decided_is_final")
        con.execute("UPDATE write_log SET decided_at = '2026-01-01T00:00:00Z' WHERE change_id = (SELECT min(change_id) FROM write_log)")
        con.execute(sql["write_log_decided_is_final"])

    path = _tamper(tmp_path, bundle, "digest", edit)
    refused(path, tmp_path / "t", "restore refused: bundle database doesn't match its manifest sequence, count and digest")


def test_restore_leaves_nothing_behind(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    parent = tmp_path / "area"
    parent.mkdir()
    for label, steps in (
        ("a", lambda c: c.execute("CREATE TABLE loot (x TEXT)")),
        ("b", lambda c: c.execute("CREATE TRIGGER evil AFTER INSERT ON sources BEGIN SELECT 1; END")),
    ):
        refused(_tamper(tmp_path, bundle, label, steps), parent / label, f"restore refused: bundle schema differs from the engine's: {'table loot' if label == 'a' else 'trigger evil'} is not in the engine's schema")
    assert list(parent.iterdir()) == []
    # An unexpected exception in the middle also removes the staging directory.
    monkeypatch.setattr(backup, "_inspect_database", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        backup.restore(bundle, parent / "c")
    assert list(parent.iterdir()) == []


def test_restore_checks_free_disk(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    _manifest, parts = read_bundle(bundle)
    need = 2 * len(parts["brain.sqlite3"]) + 64 * 1024 * 1024
    megabytes = -(-need // (1024 * 1024))
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10**12, 10**12 - 1, 1024))
    refused(bundle, tmp_path / "t", f"restore refused: not enough free disk space (need {megabytes} MiB)")


# -- 21, 22: the schema comparison and an older bundle ------------------------------------------------


def test_schema_rows_match_a_fresh_store_at_every_version(tmp_path: Path) -> None:
    counts = []
    for version in range(1, 6):
        db = tmp_path / f"v{version}.sqlite3"
        migrate.migrate(db, create=True, upto=version)
        con = sqlite3.connect(db)
        try:
            rows = migrate.schema_rows(con)
            assert rows == migrate.expected_schema(version)
            assert migrate.schema_differences(con, version) == []
            counts.append(len(rows))
        finally:
            con.close()
    print(f"schema rows at versions 1 to 5 on SQLite {sqlite3.sqlite_version}: {counts}")
    assert counts == [100, 139, 145, 159, 183]
    live = tmp_path / "v5.sqlite3"
    con = sqlite3.connect(live)
    try:
        by_name = {row[1]: row for row in migrate.schema_rows(con)}
        for suffix in ("data", "idx", "content", "docsize", "config"):
            shadow = by_name[f"search_index_{suffix}"]
            assert shadow[0] == "table" and shadow[2] == f"search_index_{suffix}" and shadow[3] == ""
        assert by_name["search_index"][3].startswith("CREATE VIRTUAL TABLE search_index USING fts5")
        sql = by_name["claims_never_deleted"][3]
        con.execute("DROP TRIGGER claims_never_deleted")
        con.execute(sql.replace("BEFORE DELETE", "BEFORE  DELETE"))  # whitespace only
        assert migrate.schema_differences(con, 5) == ["trigger claims_never_deleted differs from the engine's schema"]
        con.execute("DROP TRIGGER claims_never_deleted")
        assert migrate.schema_differences(con, 5) == ["trigger claims_never_deleted is missing"]
        con.execute("CREATE TABLE loot (x)")
        assert migrate.schema_differences(con, 5) == [
            "table loot is not in the engine's schema", "trigger claims_never_deleted is missing"]
    finally:
        con.close()
    # The comparison is exact to the character, and must not depend on a first call in this process.
    migrate._expected.clear()
    assert len(migrate.expected_schema(5)) == 183


def _old_bundle(tmp_path: Path, *, hostile: bool = False, version: int = 4) -> tuple[Path, Path]:
    """A hand-built bundle at an older schema, with a config that has aliases and a stale router."""
    stage = tmp_path / f"old{version}{'-hostile' if hostile else ''}"
    stage.mkdir()
    db = stage / "brain.sqlite3"
    migrate.migrate(db, create=True, upto=version)
    truth = "\n[truth.attributes]\nseats = [\"user count\"]\n"
    text = render_config(fact_keys=[{"key": "demo.seats", "value_type": "int"}]) + truth
    (stage / "brain.toml").write_text(text)
    (stage / "ROUTER.md").write_text("An older router.\n")
    if hostile:
        con = sqlite3.connect(db)
        con.execute("CREATE TRIGGER evil AFTER INSERT ON sources BEGIN SELECT 1; END")
        con.commit()
        con.close()
    con = store.connect_readonly(db)
    seq, count, last, digest = backup.log_digest(con)
    con.close()
    parts = {name: (stage / name).read_bytes() for name in ("ROUTER.md", "brain.sqlite3", "brain.toml")}
    base = {"format": "agent-brain.backup.v1", "engine": "0.7.0", "schema": version, "created_at": "2026-10-01T00:00:00Z",
            "instance": "demo", "label": "", "applied_seq": seq, "write_log_rows": count, "last_decided_at": last,
            "write_log_digest": digest, "paused": False}
    manifest = manifest_for(base, parts)
    raw = store.canonical_json(manifest).encode()
    entries = [(info("manifest.json", len(raw)), raw)] + [(info(n, len(b)), b) for n, b in sorted(parts.items())]
    return write_bundle(tmp_path / f"old{version}{'-hostile' if hostile else ''}.tar.gz", entries), stage


def test_restore_migrates_an_older_bundle(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    bundle, _stage = _old_bundle(tmp_path)
    code, receipt, err = restore_cli(bundle, tmp_path / "back")
    assert code == 0, err
    assert receipt["migrated_from"] == 4 and receipt["schema"] == migrate.latest_version()
    assert receipt["follow_up"] == ["brain apply-config"] and receipt["router_refreshed"] is True
    back = tmp_path / "back"
    assert (back / "ROUTER.md").read_bytes() == render_router("demo").encode()
    assert ro_rows(back, "PRAGMA user_version") == [(migrate.latest_version(),)]
    problems = store.check_instance(back)
    assert problems == ["aliases differ from brain.toml; run brain apply-config"]
    # A hostile trigger in an older bundle is refused before the migration runs.
    hostile, _stage = _old_bundle(tmp_path, hostile=True)
    ran = []
    real = migrate.migrate
    monkeypatch.setattr(migrate, "migrate", lambda *a, **k: ran.append(1) or real(*a, **k))
    refused(hostile, tmp_path / "bad", "restore refused: bundle schema differs from the engine's: trigger evil is not in the engine's schema")
    assert ran == []


def test_restore_rebuilds_the_search_index_of_a_schema_one_bundle(tmp_path: Path, clock: Clock) -> None:
    bundle, _stage = _old_bundle(tmp_path, version=1)
    code, receipt, err = restore_cli(bundle, tmp_path / "back")
    assert code == 0, err
    assert receipt["migrated_from"] == 1
    assert [p for p in store.check_instance(tmp_path / "back") if "aliases" not in p] == []


# -- Further backup and restore checks --------------------------------------------------------


def test_restore_refuses_a_link_as_its_target(tmp_path: Path, clock: Clock) -> None:
    _instance, bundle, _receipt = good(tmp_path, clock)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    refused(bundle, link, f"restore refused: {link} is a link; brain restore never writes through one", untouched=True)
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    refused(bundle, dangling, f"restore refused: {dangling} is a link; brain restore never writes through one", untouched=True)
    assert list(real.iterdir()) == []


def test_backup_says_when_it_leaves_a_cursor_out(tmp_path: Path, clock: Clock) -> None:
    instance = world(tmp_path)
    fetch = instance / "fetch"
    fetch.mkdir()
    (fetch / "work-docs.state.json").write_bytes(b" " * (16 * 1024 * 1024 + 1))
    (fetch / "team-docs.state.json").write_bytes(b'{"format":1}')
    code, receipt, err = backup_cli(instance, tmp_path / "out")
    assert code == 0
    assert "brain: cursor work-docs.state.json is over 16 MiB and was left out of the bundle" in err
    assert receipt["skipped"] == ["work-docs.state.json"]
    _manifest, members = read_bundle(Path(receipt["bundle"]))
    assert sorted(name for name in members if name.startswith("fetch/")) == ["fetch/team-docs.state.json"]
