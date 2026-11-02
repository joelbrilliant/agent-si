"""The brain redact command, its lock and marker, its steps after the commit, the export walk
and the bundles. Every name and number here is invented."""
from __future__ import annotations

import fcntl
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from brain import applier as applier_mod
from brain import backup, redact, store
from brain.applier import Applier, NotAllowed, Unavailable, Unreadable
from brain.cli import main
from tests.conftest import context_connection, ro_rows
from tests.phase2a import custom_instance, export, item, run_collect, sources
from tests.phase2b import (
    K, KEYS2, M, OPERATOR, PATH, REASON, REF, TEXT, bundle_members_hit, find_in, hot_journal, scan_instance, seed,
)
from tests.test_carry_forward import AGENTS
from tests.test_truth import Clock, _apply, _ok, clock  # noqa: F401


class Terminal(io.StringIO):
    def __init__(self, text: str = "", tty: bool = True) -> None:
        super().__init__(text)
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def cli(monkeypatch, capsys, *argv: str, stdin: Terminal | None = None):
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", stdin)
    code = main(["redact", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def args(fixture, *extra: str, reason: str = REASON) -> list[str]:
    return [fixture.source_id, "--reason", reason, "--instance", str(fixture.instance), "--operator", OPERATOR, *extra]


# -- 43 ---------------------------------------------------------------------------------------------

def test_the_plan_and_the_question(tmp_path: Path, clock: Clock, monkeypatch, capsys) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    outside = tmp_path / "extra-backups"
    outside.mkdir()
    code, out, err = cli(monkeypatch, capsys, *args(fixture, "--backups", str(outside)), stdin=Terminal("something else\n"))
    assert code == 1 and out == ""
    assert err.endswith("brain: redact refused: the source id typed does not match\n")
    first = err.splitlines()[0]
    assert first == f"brain redact {sid} ({REF}, document, 2026-10-07)"
    assert "It rewrites: " in err and "claims " in err and "write_log " in err and "(1 held)" in err
    assert f"It deletes: 1 export file in fetch/inbox, and every older bundle of this brain in {instance / 'backups'} (2) and {outside} (0)" in err.replace("/private", "") or "every older bundle of this brain in" in err
    assert "It keeps: numbers, dates and true/false values, locators and labels, the item's name, people 1 and systems 1 rows that cite it, and any note that copies the text without quote marks or restates it." in err
    assert "Backups and copies outside those folders still hold the text: see docs/REDACTION.md." in err
    assert "This cannot be undone. Type the source id to confirm:" in err
    assert ro_rows(instance, "SELECT title FROM sources WHERE id = ?", (sid,)) != [("redacted",)]
    # no terminal and no --yes
    code, out, err = cli(monkeypatch, capsys, *args(fixture), stdin=Terminal(sid + "\n", tty=False))
    assert code == 1 and out == "" and err.endswith("brain: redact refused: standard input is not a terminal; pass --yes to confirm\n")
    assert not (instance / "REDACTING").exists() and scan_instance(instance) != {}
    # the typed id accepts
    code, out, err = cli(monkeypatch, capsys, *args(fixture), stdin=Terminal(sid + "\n"))
    assert code == 0, err
    receipt = json.loads(out)
    assert receipt["status"] == "redacted" and receipt["source_id"] == sid
    assert scan_instance(instance) == {}


def test_the_plan_counts_what_the_redaction_changes(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    plan = redact.plan(fixture.instance, fixture.source_id)
    receipt = redact.run(fixture.instance, fixture.source_id, REASON, OPERATOR)
    for table, count in plan["rows"].items():
        if table in ("facts", "fact_history"):
            continue  # the headline work (a new version, a history row) is derived, so the plan counts the rows that cite the item
        assert receipt["rows"][table] == count, (table, plan["rows"], receipt["rows"])
    assert plan["write_log_held"] == receipt["held_rejected"] == 1
    assert plan["exports"] == receipt["exports_deleted"]


# -- 44 ---------------------------------------------------------------------------------------------

def test_arguments(tmp_path: Path, clock: Clock, monkeypatch, capsys) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    sid = fixture.source_id
    run = lambda *a, **k: cli(monkeypatch, capsys, *a, **k)
    code, out, err = run("not-an-id", "--reason", REASON, "--instance", str(fixture.instance), "--yes")
    assert (code, out, err) == (2, "", "brain: source id is invalid\n")
    for reason in ("ab", "x" * 201, "line\nbreak", "mail me@example.com", "tab\there"):
        code, out, err = run(sid, "--reason", reason, "--instance", str(fixture.instance), "--yes")
        assert (code, out) == (2, "")
        assert err == "brain: --reason must be 3 to 200 characters on one line, with no @ and no control characters\n"
    for operator in ("", " lead", "x" * 61, "Alex: Example", "-dash"):
        code, out, err = run(sid, "--reason", REASON, "--instance", str(fixture.instance), f"--operator={operator}", "--yes")
        assert (code, out) == (2, "")
        assert err == "brain: --operator must be 1 to 60 letters, digits, spaces, dots, hyphens or underscores, starting with a letter or digit\n"
    missing = tmp_path / "nowhere"
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    plain = tmp_path / "file.txt"
    plain.write_text("x")
    for bad in (missing, link, plain):
        code, out, err = run(sid, "--reason", REASON, "--instance", str(fixture.instance), "--backups", str(bad), "--yes")
        assert (code, out) == (2, "") and err == f"brain: --backups {bad} must be an existing directory, not a link\n"
        assert not missing.exists()
    code, out, err = run(sid, "--reason", REASON, "--yes")
    assert code == 2 and err == "brain: no instance given (pass --instance or set BRAIN_INSTANCE)\n"
    # the default operator comes from the shell user, and unknown if that fails
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "jade.example")
    code, out, err = run(sid, "--reason", REASON, "--instance", str(fixture.instance), "--yes")
    assert code == 0, err
    assert ro_rows(fixture.instance, "SELECT content FROM sources WHERE id = ?", (sid,)) == [(f"redacted on 2026-10-09 by jade.example: {REASON}",)]
    # exit codes: 1 refused, 5 unavailable
    code, out, err = run("src-nope", "--reason", REASON, "--instance", str(fixture.instance), "--yes")
    assert (code, out, err) == (1, "", "brain: redact refused: no source src-nope\n")
    (fixture.instance / "PAUSED").write_text("{}")
    code, out, err = run(sid, "--reason", REASON, "--instance", str(fixture.instance), "--yes")
    assert code == 5 and out == "" and err.endswith("instance is paused: see PAUSED; investigate, then delete it\n")
    (fixture.instance / "PAUSED").unlink()

    def boom():
        raise OSError("no such user")

    monkeypatch.setattr(getpass, "getuser", boom)
    other = seed(tmp_path / "second", clock, bundles=False)
    code, out, err = run(other.source_id, "--reason", REASON, "--instance", str(other.instance), "--yes")
    assert code == 0, err
    assert ro_rows(other.instance, "SELECT content FROM sources WHERE id = ?", (other.source_id,)) == [(f"redacted on 2026-10-09 by unknown: {REASON}",)]


# -- 45 ---------------------------------------------------------------------------------------------

def untouched(instance: Path, sid: str) -> None:
    assert not (instance / "REDACTING").exists()
    assert ro_rows(instance, "SELECT title FROM sources WHERE id = ?", (sid,)) != [("redacted",)]
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]


def test_preflight_refusals(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    import shutil

    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    with pytest.raises(NotAllowed) as err:
        redact.run(instance, "src-nope", REASON, OPERATOR)
    assert str(err.value) == "redact refused: no source src-nope"
    # disk space
    real = shutil.disk_usage
    monkeypatch.setattr(redact.shutil, "disk_usage", lambda path: real(path)._replace(free=1024))
    size = (instance / "brain.sqlite3").stat().st_size
    need = 2 * size + size * 1 + 64 * 1024 * 1024
    with pytest.raises(NotAllowed) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == f"redact refused: not enough free disk space (need {-(-need // (1 << 20))} MiB)"
    monkeypatch.undo()
    untouched(instance, sid)
    # a database over 2 GiB
    monkeypatch.setattr(redact, "MEMORY_CEILING", size - 1)
    with pytest.raises(NotAllowed) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == "redact refused: the database is over 2 GiB; VACUUM would need that much memory"
    monkeypatch.undo()
    untouched(instance, sid)
    # a marker naming another source
    (instance / "REDACTING").write_text(json.dumps({"source_id": "src-other", "change_id": "x", "backups": []}))
    with pytest.raises(NotAllowed) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == "redact refused: a redaction of src-other did not finish; run brain redact src-other first"
    (instance / "REDACTING").unlink()
    # a held lock
    handle = backup.open_lock(instance)
    fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    with pytest.raises(Unavailable) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == "a backup or another redaction is running; try again when it finishes"
    handle.close()
    untouched(instance, sid)
    # a PAUSED instance
    (instance / "PAUSED").write_text("{}")
    with pytest.raises(Unavailable) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert "paused" in str(err.value)
    (instance / "PAUSED").unlink()
    untouched(instance, sid)


def test_a_capture_between_the_question_and_the_lock(tmp_path: Path, clock: Clock, monkeypatch, capsys) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS2)
    assert run_collect(instance, export([item("n.md", f"Contact {M} for the vault.")], fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    sid = sources(instance)[0][0]
    real = redact._confirm
    calls = []

    def confirm(found, yes, stdin):
        calls.append(len(found["sources"]))
        real(found, yes, stdin)
        if len(calls) == 1:
            clock.at("2026-10-08T02:00:00Z")
            assert run_collect(instance, export([item("n.md", "The vault is audited yearly.")], fetched_at="2026-10-08T02:00:00Z"))[0] == 0

    monkeypatch.setattr(redact, "_confirm", confirm)
    fixture = type("F", (), {"source_id": sid, "instance": instance})
    # under --yes the command refuses
    code = main(["redact", sid, "--reason", REASON, "--instance", str(instance), "--yes"])
    err = capsys.readouterr().err
    assert code == 1 and err.endswith("brain: redact refused: the chain changed after the plan; run it again\n")
    assert not (instance / "REDACTING").exists() and ro_rows(instance, "SELECT count(*) FROM sources WHERE title = 'redacted'") == [(0,)]
    assert calls == [1]
    # asked again with the new plan
    calls.clear()
    monkeypatch.setattr(sys, "stdin", Terminal(f"{sid}\n{sid}\n"))
    clock.at("2026-10-08T03:00:00Z")
    run_collect(instance, export([item("n.md", "The vault is audited every year.")], fetched_at="2026-10-08T03:00:00Z"))
    # the first confirm adds no more versions now (calls > 1): the chain is stable, so one question
    code = main(["redact", sid, "--reason", REASON, "--instance", str(instance)])
    capsys.readouterr()
    assert code == 0


# -- 46 ---------------------------------------------------------------------------------------------

def test_marker_and_lock_lifecycle(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    seen = {}
    real = redact._vacuum

    def spy(con, source_id):
        marker = json.loads((instance / "REDACTING").read_text())
        seen["marker"] = marker
        seen["mode"] = oct((instance / "REDACTING").stat().st_mode & 0o777)
        handle = backup.open_lock(instance)
        try:
            with pytest.raises(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)  # the redaction holds it exclusively
        finally:
            handle.close()
        with pytest.raises(backup.Refused) as err:
            backup.backup(instance, tmp_path / "during")
        seen["during"] = str(err.value)
        real(con, source_id)

    monkeypatch.setattr(redact, "_vacuum", spy)
    redact.run(instance, sid, REASON, OPERATOR)
    assert seen["marker"]["source_id"] == sid and seen["marker"]["change_id"].startswith("brain-redact-") and seen["marker"]["backups"] == []
    assert seen["mode"] == "0o600"
    assert seen["during"] in (
        f"backup refused: a redaction of {sid} did not finish; run brain redact {sid} again",
        "backup refused: a redaction is running; try again when it finishes",
    )
    assert not (instance / "REDACTING").exists()
    monkeypatch.undo()
    # a rejected packet removes the marker
    other = seed(tmp_path / "other", clock, bundles=False)
    with pytest.raises(NotAllowed) as err:
        redact.run(other.instance, other.source_id, M, OPERATOR)
    assert not (other.instance / "REDACTING").exists()
    # a stop after the commit keeps it
    monkeypatch.setattr(redact, "_vacuum", lambda con, source_id: (_ for _ in ()).throw(Unavailable("stopped")))
    with pytest.raises(Unavailable):
        redact.run(other.instance, other.source_id, REASON, OPERATOR)
    assert (other.instance / "REDACTING").exists()
    problems = store.check_instance(other.instance)
    assert f"a redaction did not finish: run brain redact {other.source_id} again" in problems
    with pytest.raises(backup.Refused) as err:
        backup.backup(other.instance, tmp_path / "no")
    assert str(err.value) == f"backup refused: a redaction of {other.source_id} did not finish; run brain redact {other.source_id} again"
    # an unreadable marker names SOURCE_ID
    (other.instance / "REDACTING").write_text("not json")
    with pytest.raises(backup.Refused) as err:
        backup.backup(other.instance, tmp_path / "no")
    assert str(err.value) == "backup refused: a redaction of SOURCE_ID did not finish; run brain redact SOURCE_ID again"
    (other.instance / "REDACTING").unlink()
    # a held exclusive lock makes backup refuse; two shared locks coexist
    first = backup.open_lock(other.instance)
    fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    with pytest.raises(backup.Refused) as err:
        backup.backup(other.instance, tmp_path / "locked")
    assert str(err.value) == "backup refused: a redaction is running; try again when it finishes"
    first.close()
    one, two = backup.open_lock(other.instance), backup.open_lock(other.instance)
    fcntl.flock(one.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    fcntl.flock(two.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    assert backup.backup(other.instance, tmp_path / "shared")["status"] == "backed_up"
    one.close()
    two.close()


# -- 47, 48, 49 -------------------------------------------------------------------------------------

def test_post_commit_order(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    order: list[str] = []
    pragmas = {}
    real_apply = Applier.apply
    monkeypatch.setattr(Applier, "apply", lambda self, raw: (order.append("apply"), real_apply(self, raw))[1])
    real_opt, real_vac, real_free = redact._optimize, redact._vacuum, redact._freelist

    def opt(con):
        pragmas["temp_store"] = con.execute("PRAGMA temp_store").fetchone()[0]
        order.append("optimize")
        real_opt(con)

    monkeypatch.setattr(redact, "_optimize", opt)
    monkeypatch.setattr(redact, "_vacuum", lambda con, s: (order.append("vacuum"), real_vac(con, s))[1])
    monkeypatch.setattr(redact, "_freelist", lambda con: (order.append("freelist"), real_free(con))[1])
    real_exports, real_backup, real_replace = redact.delete_exports, backup.backup, redact.replace_bundles
    monkeypatch.setattr(redact, "delete_exports", lambda *a, **k: (order.append("exports"), real_exports(*a, **k))[1])
    monkeypatch.setattr(backup, "backup", lambda *a, **k: (order.append("backup"), real_backup(*a, **k))[1])
    monkeypatch.setattr(redact, "replace_bundles", lambda *a, **k: (order.append("prune"), real_replace(*a, **k))[1])
    redact.run(instance, sid, REASON, OPERATOR)
    assert order == ["apply", "optimize", "vacuum", "freelist", "exports", "backup", "prune"], order
    assert pragmas["temp_store"] == 2  # MEMORY


class Busy:
    """A connection whose VACUUM fails with a busy error a set number of times."""

    def __init__(self, real, failures: int) -> None:
        self.real, self.failures, self.calls = real, failures, 0

    def execute(self, sql, *params):
        if sql.strip().upper() == "VACUUM":
            self.calls += 1
            if self.calls <= self.failures:
                raise sqlite3.OperationalError("database is locked")
        return self.real.execute(sql, *params)

    def close(self):
        self.real.close()


def test_vacuum_retries_on_busy(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    sleeps: list[float] = []
    monkeypatch.setattr(redact, "_sleep", sleeps.append)
    real_open = redact._open_compact
    holder = {}
    monkeypatch.setattr(redact, "_open_compact", lambda db: holder.setdefault("c", Busy(real_open(db), 2)))
    redact.run(instance, sid, REASON, OPERATOR)
    assert sleeps == [1, 2] and holder["c"].calls == 3
    # six failures: the exit 5 text, the marker kept, and the sleeps 1, 2, 4, 8 and 16 seconds
    other = seed(tmp_path / "other", clock, bundles=False)
    sleeps.clear()
    monkeypatch.setattr(redact, "_open_compact", lambda db: Busy(real_open(db), 6))
    with pytest.raises(Unavailable) as err:
        redact.run(other.instance, other.source_id, REASON, OPERATOR)
    assert str(err.value) == (
        f"redaction of {other.source_id} is committed, but VACUUM could not run (database is locked); "
        f"run brain redact {other.source_id} again")
    assert sleeps == [1, 2, 4, 8, 16]
    assert (other.instance / "REDACTING").exists()


def test_freelist_assertion(tmp_path: Path, clock: Clock, monkeypatch, capsys) -> None:
    fixture = seed(tmp_path, clock)
    monkeypatch.setattr(redact, "_freelist", lambda con: 3)
    code, out, err = cli(monkeypatch, capsys, *args(fixture, "--yes"))
    assert code == 5 and out == ""
    assert err.endswith(f"brain: VACUUM left 3 free pages; run brain redact {fixture.source_id} again\n")
    assert err.startswith(f"brain redact {fixture.source_id} (")  # the plan is printed even with --yes
    assert (fixture.instance / "REDACTING").exists()


# -- 50 ---------------------------------------------------------------------------------------------

def test_resume_after_a_stop(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    real = redact._vacuum
    calls = {"n": 0}

    def stop(con, source_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Unavailable("stopped")
        real(con, source_id)

    monkeypatch.setattr(redact, "_vacuum", stop)
    with pytest.raises(Unavailable):
        redact.run(instance, sid, REASON, OPERATOR)
    assert (instance / "REDACTING").exists() and find_in((instance / "brain.sqlite3").read_bytes())
    again = redact.run(instance, sid, REASON, OPERATOR)
    assert again["change_id"].endswith("-2")
    assert not (instance / "REDACTING").exists() and scan_instance(instance) == {}
    assert store.check_instance(instance) == []
    assert any("post-redaction" in name for name in (p.name for p in (instance / "backups").iterdir()))


def bulk_instance(tmp_path: Path, clock: Clock):
    """The fixture plus enough filler that a VACUUM takes long enough to interrupt."""
    fixture = seed(tmp_path, clock, bundles=False)
    filler = " ".join(f"Filler line {i} says nothing about the vault or anything else here." for i in range(25000))
    docs = [item(f"bulk/filler{n}.md", filler + f" file {n}") for n in range(8)]
    clock.at("2026-10-09T01:00:00Z")
    code, payload, err = run_collect(fixture.instance, export(docs, fetched_at="2026-10-09T01:00:00Z"))
    assert code == 0, (payload, err)
    return fixture


def test_resume_after_a_vacuum_is_killed(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = bulk_instance(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    db = instance / "brain.sqlite3"
    # commit the redaction, then stop before the compaction
    monkeypatch.setattr(redact, "_vacuum", lambda con, source_id: (_ for _ in ()).throw(Unavailable("stopped")))
    with pytest.raises(Unavailable):
        redact.run(instance, sid, REASON, OPERATOR)
    monkeypatch.undo()
    assert (instance / "REDACTING").exists()
    journal = instance / "brain.sqlite3-journal"
    code = (
        "import sqlite3,sys\n"
        "con = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "con.execute('PRAGMA temp_store = MEMORY')\n"
        "con.execute('VACUUM')\n"
    )
    pristine = tmp_path / "pristine.sqlite3"
    pristine.write_bytes(db.read_bytes())
    killed_with = 0
    for attempt in range(40):
        db.write_bytes(pristine.read_bytes())
        child = subprocess.Popen([sys.executable, "-c", code, str(db)])
        deadline = time.time() + 30
        while time.time() < deadline and child.poll() is None:
            if journal.exists() and journal.stat().st_size > 2_000_000:
                child.send_signal(signal.SIGKILL)
                break
            time.sleep(0.005)
        child.wait()
        if journal.exists() and journal.stat().st_size > 2_000_000:
            killed_with = journal.stat().st_size
            break
    assert killed_with, "no hot journal was left by the killed VACUUM"
    print(f"HOT JOURNAL {killed_with} bytes")
    # check and plan neither raise, and backup refuses
    problems = store.check_instance(instance)
    assert f"a redaction did not finish: run brain redact {sid} again" in problems
    assert "store has a hot journal: run brain redact <id> again, or any brain write, to recover it" in problems
    with pytest.raises(backup.Refused):
        backup.backup(instance, tmp_path / "no")
    info = redact.plan(instance, sid)  # the first open recovers the store
    assert info["sources"] and not journal.exists()
    assert find_in(db.read_bytes())  # the old pages, and the text in the free ones, are back in the file
    result = redact.run(instance, sid, REASON, OPERATOR)
    assert result["database"]["freelist_count"] == 0
    assert not journal.exists() and not (instance / "REDACTING").exists()
    assert find_in(db.read_bytes()) == [] and scan_instance(instance) == {}
    assert store.check_instance(instance) == []


def test_an_unexpected_exception_cleans_or_keeps_the_marker(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id

    def boom(self, raw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Applier, "apply", boom)
    with pytest.raises(Unavailable) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == f"redaction of {sid} failed before it committed: disk I/O error"
    assert not (instance / "REDACTING").exists()
    assert ro_rows(instance, "SELECT count(*) FROM redaction_gate") == [(0,)]
    monkeypatch.undo()
    # after the commit the marker stays and the line names the way out
    real = redact.delete_exports
    monkeypatch.setattr(redact, "delete_exports", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("late")))
    with pytest.raises(Unavailable) as err:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err.value) == f"redaction of {sid} is committed; run brain redact {sid} again"
    assert (instance / "REDACTING").exists()
    monkeypatch.setattr(redact, "delete_exports", real)
    redact.run(instance, sid, REASON, OPERATOR)
    assert not (instance / "REDACTING").exists() and scan_instance(instance) == {}


@pytest.mark.parametrize("n", [334, 340, 1000])
def test_a_long_chain_redacts(tmp_path: Path, clock: Clock, n: int) -> None:
    instance = custom_instance(tmp_path, agents=AGENTS, fact_keys=KEYS2)
    con = context_connection(instance)
    prev = None
    for i in range(n):
        body = f"version {i} of the vault note. " + "Lorem ipsum dolor sit amet. " * 36
        con.execute(
            "INSERT INTO sources (id, kind, title, uri, source_date, author_person_id, content, content_sha256, external_ref, captured_at, captured_by, supersedes_id) "
            "VALUES (?, 'document', 'chain note', '', '2026-10-07', NULL, ?, ?, 'document:work-docs/chain.md', '2026-10-08T00:00:00Z', 'tester:x', ?)",
            (f"src-chain-{i:04d}", body, store.sha256_text(body), prev))
        prev = f"src-chain-{i:04d}"
    con.commit()
    con.close()
    started = time.perf_counter()
    result = redact.run(instance, prev, REASON, OPERATOR)
    print(f"LONG CHAIN {n}: {time.perf_counter() - started:.2f} s")
    assert len(result["sources"]) == n and not (instance / "REDACTING").exists()
    assert store.check_instance(instance) == []


# -- 51 ---------------------------------------------------------------------------------------------

def test_export_walk(tmp_path: Path, clock: Clock, capsys) -> None:
    instance = tmp_path / "inst"
    inbox = instance / "fetch" / "inbox"
    (inbox / "work-docs").mkdir(parents=True)
    (inbox / "other").mkdir()
    doc = lambda ref: json.dumps({"items": [{"ref": ref, "text": "x"}], "present": []}).encode()
    name = "20261008T000000Z-{:02d}.json"
    hit = inbox / "work-docs" / name.format(1)
    hit.write_bytes(doc(REF))
    present_only = inbox / "work-docs" / name.format(2)
    present_only.write_bytes(json.dumps({"items": [], "present": [REF]}).encode())
    other_ref = inbox / "work-docs" / name.format(3)
    other_ref.write_bytes(doc("document:work-docs/other.md"))
    odd_name = inbox / "work-docs" / "export.json"
    odd_name.write_bytes(doc(REF))
    bad_json = inbox / "work-docs" / name.format(4)
    bad_json.write_bytes(b"{nope")
    deep = inbox / "work-docs" / name.format(5)
    deep.write_text("[" * 200000)
    tmp_left = inbox / "work-docs" / ".20261008T000000Z-06.json.1234.tmp"
    tmp_left.write_bytes(doc(REF))
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(doc(REF))
    (inbox / "work-docs" / name.format(7)).symlink_to(elsewhere)
    big = inbox / "work-docs" / name.format(8)
    big.write_bytes(doc(REF))
    (inbox / "linked").symlink_to(inbox / "work-docs")
    other_folder = inbox / "other" / name.format(1)
    other_folder.write_bytes(doc(REF))
    unchecked: list[str] = []
    removed = redact.delete_exports(instance, [REF, ""], unchecked)
    assert removed == ["fetch/inbox/other/20261008T000000Z-01.json", "fetch/inbox/work-docs/20261008T000000Z-01.json", "fetch/inbox/work-docs/20261008T000000Z-08.json"]
    for survivor in (present_only, other_ref, odd_name, bad_json, deep, tmp_left, elsewhere):
        assert survivor.exists()
    assert (inbox / "work-docs" / name.format(7)).is_symlink()
    assert sorted(unchecked) == sorted([
        "fetch/inbox/linked", "fetch/inbox/work-docs/.20261008T000000Z-06.json.1234.tmp", "fetch/inbox/work-docs/20261008T000000Z-04.json",
        "fetch/inbox/work-docs/20261008T000000Z-05.json", "fetch/inbox/work-docs/20261008T000000Z-07.json", "fetch/inbox/work-docs/export.json"])
    # a file over the cap is listed, not read
    big.write_bytes(doc(REF))
    other_folder.write_bytes(doc(REF))
    unchecked = []
    import brain.redact as module

    module.EXPORT_READ_MAX = 10
    try:
        assert redact.delete_exports(instance, [REF], unchecked) == []
    finally:
        module.EXPORT_READ_MAX = 256 * 1024 * 1024
    assert "fetch/inbox/work-docs/20261008T000000Z-08.json" in unchecked and big.exists()
    # links: a symlinked fetch, a symlinked inbox, one resolving outside the instance
    for kind in ("fetch", "inbox", "outside"):
        probe = tmp_path / f"probe-{kind}"
        (probe / "fetch" / "inbox" / "work-docs").mkdir(parents=True)
        target = probe / "fetch" / "inbox" / "work-docs" / name.format(1)
        target.write_bytes(doc(REF))
        real_fetch = probe / "fetch"
        if kind == "fetch":
            real_fetch.rename(probe / "real-fetch")
            (probe / "fetch").symlink_to(probe / "real-fetch")
            target = probe / "real-fetch" / "inbox" / "work-docs" / name.format(1)
        elif kind == "inbox":
            (real_fetch / "inbox").rename(real_fetch / "real-inbox")
            (real_fetch / "inbox").symlink_to(real_fetch / "real-inbox")
            target = real_fetch / "real-inbox" / "work-docs" / name.format(1)
        else:
            elsewhere_dir = tmp_path / f"outside-{kind}"
            (elsewhere_dir / "inbox" / "work-docs").mkdir(parents=True)
            (elsewhere_dir / "inbox" / "work-docs" / name.format(1)).write_bytes(doc(REF))
            import shutil

            shutil.rmtree(real_fetch)
            (probe / "fetch").symlink_to(elsewhere_dir)
            target = elsewhere_dir / "inbox" / "work-docs" / name.format(1)
        assert redact.delete_exports(probe, [REF], []) == []
        assert target.exists(), kind
    # the walk never raises on a missing inbox
    assert redact.delete_exports(tmp_path / "empty", [REF], []) == []


# -- 52 ---------------------------------------------------------------------------------------------

def make_bundle(path: Path, instance_name: str) -> None:
    manifest = json.dumps({"format": backup.FORMAT, "instance": instance_name, "label": ""}, sort_keys=True).encode()
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo("manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
        data = b"SQLite format 3\x00" + M.encode()
        info = tarfile.TarInfo("brain.sqlite3")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))


def test_bundles_are_replaced(tmp_path: Path, clock: Clock, capsys) -> None:
    fixture = seed(tmp_path, clock)
    instance, sid = fixture.instance, fixture.source_id
    out = instance / "backups"
    name = "demo"
    extra = tmp_path / "extra-folder"
    extra.mkdir()
    mine = extra / f"{name}-20260101T000000Z-seq1.tar.gz"
    foreign = extra / f"{name}-20260102T000000Z-seq1.tar.gz"
    make_bundle(mine, name)
    make_bundle(foreign, "someone-else")
    (extra / "manual-copy.tar.gz").write_bytes(b"x")
    (extra / ".backup-stale").mkdir()
    other_name = out / "other-20260101T000000Z-seq1.tar.gz"
    make_bundle(other_name, "other")
    (out / "manual-copy.tar.gz").write_bytes(b"x")
    (out / ".backup-x").mkdir()
    (out / ".backup-x" / "brain.sqlite3").write_bytes(M.encode())
    (out / ".tmp-y").write_bytes(M.encode())
    make_bundle(out / f"{name}-20250101T000000Z-seq1-custom.tar.gz", name)
    result = redact.run(instance, sid, REASON, OPERATOR, [extra])
    new = Path(result["bundle"])
    assert new.parent == out.resolve() and new.name.endswith("-post-redaction.tar.gz")
    assert sorted(p.name for p in out.iterdir()) == sorted([new.name, other_name.name, "manual-copy.tar.gz"])
    assert sorted(p.name for p in extra.iterdir()) == sorted([foreign.name, "manual-copy.tar.gz", ".backup-stale", new.name])
    assert not mine.exists() and (extra / new.name).read_bytes() == new.read_bytes()
    assert oct((extra / new.name).stat().st_mode & 0o777) == "0o600"
    pruned = result["pruned"]
    assert set(pruned) == {str(out.resolve()), str(extra.resolve())}
    assert mine.name in pruned[str(extra.resolve())] and len(pruned[str(out.resolve())]) >= 3
    assert result["unrecognised"][str(out.resolve())] == sorted([other_name.name, "manual-copy.tar.gz"])
    assert result["unrecognised"][str(extra.resolve())] == sorted([".backup-stale", foreign.name, "manual-copy.tar.gz"])
    err = capsys.readouterr().err
    assert f"brain: {out.resolve()} holds manual-copy.tar.gz" in err or "which redaction did not remove; they may hold the text" in err
    # a stop after the commit records the folders in the marker, and a rerun without them is refused
    other = seed(tmp_path / "second", clock)
    extra2 = tmp_path / "extra-two"
    extra2.mkdir()
    real_vacuum = redact._vacuum
    stop = pytest.MonkeyPatch()
    stop.setattr(redact, "_vacuum", lambda con, source_id: (_ for _ in ()).throw(Unavailable("stopped")))
    with pytest.raises(Unavailable):
        redact.run(other.instance, other.source_id, REASON, OPERATOR, [extra2])
    stop.undo()
    marker = json.loads((other.instance / "REDACTING").read_text())
    assert marker["backups"] == [str(extra2)] and marker["source_id"] == other.source_id
    with pytest.raises(NotAllowed) as err_info:
        redact.run(other.instance, other.source_id, REASON, OPERATOR)
    assert str(err_info.value) == f"redact refused: the unfinished redaction used --backups {extra2}; pass the same folders"
    redact.run(other.instance, other.source_id, REASON, OPERATOR, [extra2])
    assert not (other.instance / "REDACTING").exists()
    (instance / "REDACTING").write_text(json.dumps({"source_id": sid, "change_id": "x", "backups": [str(extra)]}))
    with pytest.raises(NotAllowed) as err_info:
        redact.run(instance, sid, REASON, OPERATOR)
    assert str(err_info.value) == f"redact refused: the unfinished redaction used --backups {extra}; pass the same folders"
    redact.run(instance, sid, REASON, OPERATOR, [extra])
    assert not (instance / "REDACTING").exists()


def test_a_backups_folder_that_does_not_exist(tmp_path: Path, clock: Clock, monkeypatch, capsys) -> None:
    fixture = seed(tmp_path, clock, bundles=False)
    missing = tmp_path / "typo-folder"
    code, out, err = cli(monkeypatch, capsys, *args(fixture, "--backups", str(missing), "--yes"))
    assert code == 2 and err == f"brain: --backups {missing} must be an existing directory, not a link\n"
    assert not missing.exists() and not (fixture.instance / "REDACTING").exists()


# -- 53, 54 -----------------------------------------------------------------------------------------

def test_the_post_redaction_bundle_is_clean_and_restores(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    result = redact.run(fixture.instance, fixture.source_id, REASON, OPERATOR)
    bundle = Path(result["bundle"])
    assert bundle_members_hit(bundle) == []
    back = tmp_path / "restored"
    assert backup.restore(bundle, back)["status"] == "restored"
    assert store.check_instance(back) == []


def test_digest_ignores_scrubbed_columns(tmp_path: Path, clock: Clock) -> None:
    """The manifest's write_log digest covers decision columns only, so scrubbing packet_json, error and
    hold_reason leaves it alone; a held row that becomes rejected changes it; the post-redaction bundle agrees
    with the store it was taken from."""
    columns = "change_id, agent, status, decided_at, applied_seq, reviewed_by, reviewed_at, payload_sha256"
    # a store whose only row citing the source is decided (applied or refused): the digest of those rows is equal
    other = custom_instance(tmp_path / "decided", agents=AGENTS, fact_keys=KEYS2)
    assert run_collect(other, export([item("a.md", f'Contact "{M}" for the vault.')], fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    sid = sources(other)[0][0]
    refused = _apply(other, "tester", {
        "op": "upsert_record", "row": {"id": "rec-bad", "kind": "note", "title": "Bad", "body": f'He wrote "contact {M} for the wrong vault ever".'},
        "source": {"source_id": sid, "locator": "p"}, "base": None})
    assert refused.receipt["status"] == "rejected"
    rows_before = ro_rows(other, f"SELECT {columns} FROM write_log ORDER BY change_id")
    packets_before = dict(ro_rows(other, "SELECT change_id, packet_json || error FROM write_log"))
    redact.run(other, sid, REASON, OPERATOR)
    rows_after = [r for r in ro_rows(other, f"SELECT {columns} FROM write_log ORDER BY change_id") if not r[0].startswith("brain-redact-")]
    assert rows_after == rows_before
    packets_after = dict(ro_rows(other, "SELECT change_id, packet_json || error FROM write_log"))
    assert any(packets_after[cid] != packets_before[cid] for cid in packets_before)  # the text did change
    # a held row that becomes rejected changes it
    fixture = seed(tmp_path / "held", clock)
    held_before = ro_rows(fixture.instance, f"SELECT {columns} FROM write_log WHERE change_id = ?", (fixture.held,))[0]
    others_before = ro_rows(fixture.instance, f"SELECT {columns} FROM write_log WHERE change_id <> ? ORDER BY change_id", (fixture.held,))
    result = redact.run(fixture.instance, fixture.source_id, REASON, OPERATOR)
    held_after = ro_rows(fixture.instance, f"SELECT {columns} FROM write_log WHERE change_id = ?", (fixture.held,))[0]
    assert held_after != held_before and held_after[2] == "rejected" and held_before[2] == "held"
    others_after = [r for r in ro_rows(fixture.instance, f"SELECT {columns} FROM write_log WHERE change_id <> ? ORDER BY change_id", (fixture.held,))
                    if not r[0].startswith("brain-redact-")]
    assert others_after == others_before
    # the bundle taken after agrees with the store
    con = store.connect_readonly(fixture.instance / "brain.sqlite3")
    try:
        digest = backup.log_digest(con)
    finally:
        con.close()
    with tarfile.open(result["bundle"], "r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert (manifest["applied_seq"], manifest["write_log_rows"], manifest["last_decided_at"], manifest["write_log_digest"]) == digest


# -- 55, 56 -----------------------------------------------------------------------------------------

def test_originals_warning(tmp_path: Path, clock: Clock, capsys) -> None:
    from tests.collector_fixtures import collector_instance

    live = tmp_path / "Documents"
    (live / "vault").mkdir(parents=True)
    original = live / "vault" / "notes.md"
    original.write_text(TEXT)
    instance = custom_instance(tmp_path / "inst", agents=AGENTS, fact_keys=KEYS2, table={"live_folder": str(live)})
    assert run_collect(instance, export([item(PATH, TEXT)]))[0] == 0
    sid = sources(instance, REF)[0][0]
    result = redact.run(instance, sid, REASON, OPERATOR)
    assert result["originals"] == [str(live.resolve() / "vault" / "notes.md")] or result["originals"] == [str(original)]
    err = capsys.readouterr().err
    assert "the original " in err and "is still there; delete or move it, or brain-fetch run --rescan will copy it into the inbox again" in err
    # none when the file is gone; a ref with .. or a link out of the folder is not followed
    original.unlink()
    assert redact.find_originals(instance, [REF]) == []
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    (live / "vault" / "escape.md").symlink_to(outside)
    assert redact.find_originals(instance, ["document:work-docs/vault/escape.md"]) == []
    assert redact.find_originals(instance, ["document:work-docs/vault/../../outside.md"]) == []
    (live / "linkdir").symlink_to(tmp_path)
    assert redact.find_originals(instance, ["document:work-docs/linkdir/outside.md"]) == []
    # a ref of another origin is not read
    (live / "stray.md").write_text("x")
    assert redact.find_originals(instance, ["document:elsewhere/stray.md"]) == []
    assert redact.find_originals(instance, ["document:work-docs/stray.md"]) == [str(live.resolve() / "stray.md")] or redact.find_originals(instance, ["document:work-docs/stray.md"]) == [str(live / "stray.md")]


def test_a_bundle_outside_is_untouched(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    held = [p for p in fixture.outside.iterdir()]
    before = {p.name: p.read_bytes() for p in held}
    redact.run(fixture.instance, fixture.source_id, REASON, OPERATOR)
    assert {p.name: p.read_bytes() for p in fixture.outside.iterdir()} == before
    assert any(bundle_members_hit(p) for p in held)


def test_restoring_an_older_bundle_brings_the_text_back(tmp_path: Path, clock: Clock) -> None:
    fixture = seed(tmp_path, clock)
    redact.run(fixture.instance, fixture.source_id, REASON, OPERATOR)
    older = sorted(fixture.outside.iterdir())[0]
    restored = tmp_path / "restored"
    assert backup.restore(older, restored)["status"] == "restored"
    assert (restored / "brain.sqlite3").read_bytes().count(M.encode()) > 0  # the text returns with the restore
    assert store.check_instance(restored) == []  # nothing in an older store says it was redacted
    redact.run(restored, fixture.source_id, REASON, OPERATOR)  # and the same command removes it again
    assert scan_instance(restored) == {}
    assert store.check_instance(restored) == []
