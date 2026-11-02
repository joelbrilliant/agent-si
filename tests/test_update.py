"""Brain update from a wheel, its rollback in the same run, the UPDATING stop, serve's restart check,
and the refusals that replace fetching releases by name. Every machine folder, release and wheel here
is invented."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from brain import config as config_mod, reads, registry, server, store, update, versions
from brain.applier import Applier, Unavailable
from tests import fetch_net as fn
from tests.conftest import make_packet, ro_rows
from tests.phase2a import custom_instance
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)
from tests.update_world import World


REAL_SNAPSHOT = update.snapshot_wheel


@pytest.fixture(autouse=True)
def kept_wheel(monkeypatch):
    """The test machine's `brain` is a shim, so there is no installed distribution to pack: stand in for
    update.snapshot_wheel with a wheel named for the version the shim reports (test_snapshot_wheel packs a real one)."""

    def fake(name, folder):
        version = update.installed(update.tool_bin() / "brain")
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"agent_si-{version}-py3-none-any.whl"
        path.write_bytes(b"not read by the fake uv")
        return path

    monkeypatch.setattr(update, "snapshot_wheel", fake)


def _instance(tmp_path: Path, name: str = "inst", extra_toml: str = "") -> Path:
    return custom_instance(tmp_path / name, extra_toml=extra_toml)


def _apply_note(inst: Path, text: str) -> int:
    packet = make_packet("tester", {"op": "add_source", "row": {
        "id": f"src-note-{abs(hash(text)) % 10**8}", "kind": "user_note", "title": text, "content": text,
        "source_date": "2026-10-09"}})
    writer = Applier(inst)
    try:
        return writer.apply(json.dumps(packet).encode()).exit_code
    finally:
        writer.close()


def _brief(inst: Path) -> str:
    return reads.run_tool(inst, "brief", {}).text


def _counts(inst: Path) -> tuple:
    return ro_rows(inst, "SELECT (SELECT count(*) FROM sources), (SELECT count(*) FROM write_log)")[0]


def _run(world: World, *argv: str) -> tuple[int, dict]:
    import contextlib
    import io

    from brain.cli import main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["update", *argv])
    return code, json.loads(out.getvalue())


# -- the good path ------------------------------------------------------------------------------------------


def test_update_from_a_good_wheel(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    assert _apply_note(inst, "Pilot starts Monday") == 0
    before = _counts(inst)
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert (code, receipt["result"], receipt["from"], receipt["to"]) == (0, "updated", "0.8.0", "0.8.1")
    assert receipt["steps"] == ["writes stopped", "backed up", "installed", "migrated", "checked"]
    row = receipt["instances"][0]
    assert row["result"] == "updated" and Path(row["backup"]).is_file() and row["applied_seq"] >= 1
    assert not (inst / "UPDATING").exists() and _counts(inst) == before
    install = world.uv_calls()[0]
    assert install[:6] == ["tool", "install", "--reinstall", "--managed-python", "--python", "3.13"]
    assert install[6].endswith("agent_brain-0.8.1-py3-none-any.whl") and "--exclude-newer" not in install
    state = registry.read_state()
    assert (state["current"], state["last_result"], state["last_target"]) == ("0.8.1", "updated", "0.8.1")
    assert "manual: updated 0.8.0 -> 0.8.1" in (registry.state_dir() / "update.log").read_text()
    assert "Updating" not in _brief(inst)


def test_the_same_version_is_current_and_an_older_one_is_refused(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    code, receipt = _run(world, "--instance", str(inst), "--from-wheel", str(world.wheel("0.8.0")))
    assert (code, receipt["result"]) == (0, "current") and world.uv_calls() == []
    code, receipt = _run(world, "--instance", str(inst), "--from-wheel", str(world.wheel("0.7.9")))
    assert (code, receipt["result"]) == (1, "refused")
    assert receipt["message"] == "0.7.9 is older than 0.8.0; brain update doesn't downgrade"


# -- rollback in the same run --------------------------------------------------------------------------------


@pytest.mark.parametrize("mode, words", [
    ("broken_version", "the installed brain reports nothing, not 0.8.1"),
    ("broken_check", "brain check failed"),
    ("broken_serve", "serve answered with something that isn't JSON"),
    ("uninstallable", "uv tool install failed"),
])
def test_a_broken_wheel_rolls_back_by_itself(tmp_path, monkeypatch, clock: Clock, mode, words) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    assert _apply_note(inst, "Pilot starts Monday") == 0
    before, brief_before = _counts(inst), _brief(inst)
    world.behave(**{"0.8.1": mode})
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert (code, receipt["result"]) == (5, "rolled_back") and words in receipt["message"]
    assert receipt["instances"][0]["result"] == "rolled_back"
    assert update.installed(world.bin / "brain") == "0.8.0"
    assert world.uv_calls()[-1][-1].endswith("agent_si-0.8.0-py3-none-any.whl")  # the old version, from its kept wheel
    assert not (inst / "UPDATING").exists() and not (inst / "PAUSED").exists()
    notice = "Last update to 0.8.1 failed and was rolled back: see brain update --status.\n"
    assert _counts(inst) == before and _brief(inst).replace(notice, "") == brief_before  # it answers as before
    assert _apply_note(inst, "Writes work again") == 0
    state = registry.read_state()
    assert (state["last_result"], state["last_target"]) == ("rolled_back", "0.8.1")
    line = reads.update_line(config_mod.Updates(), state)
    assert line == "Last update to 0.8.1 failed and was rolled back: see brain update --status."


def test_a_broken_migration_moves_the_store_and_journal_aside_and_restores(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    assert _apply_note(inst, "Pilot starts Monday") == 0
    before = _counts(inst)
    world.behave(**{"0.8.1": "broken_migrate"})
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert (code, receipt["result"]) == (5, "rolled_back") and "brain migrate failed" in receipt["message"]
    aside = sorted(path.name for path in inst.glob("broken-*"))
    assert len(aside) == 2 and aside[0].endswith(".sqlite3") and aside[1] == aside[0] + "-journal"
    assert not (inst / "brain.sqlite3-journal").exists()
    assert subprocess.run([str(world.bin / "brain"), "check", "--instance", str(inst)], capture_output=True).returncode == 0
    assert _counts(inst) == before


def test_a_failed_rollback_check_leaves_paused(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    world.behave(**{"0.8.1": "broken_migrate", "brokenold": "broken_check"})
    old = world.wheel("0.8.0", folder="brokenold")
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")), "--rollback-from", str(old))
    assert (code, receipt["result"]) == (5, "paused") and receipt["instances"][0]["result"] == "paused"
    paused = json.loads((inst / "PAUSED").read_text())
    assert paused["reason"].startswith("after rolling back to 0.8.0: brain check failed")
    assert paused["pre_update_backup"] == receipt["instances"][0]["backup"] and len(paused["moved_aside"]) == 2
    assert not (inst / "UPDATING").exists()
    assert "PAUSED. Writes are stopped" in _brief(inst)
    with pytest.raises(Unavailable):
        _apply_note(inst, "refused while paused")


def test_a_failed_reinstall_pauses_every_instance(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    first, second = _instance(tmp_path, "one"), _instance(tmp_path, "two")
    for inst in (first, second):
        world.register(inst)
    world.behave(**{"0.8.1": "broken_version", "agent_si-0.8.0": "uninstallable"})
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert (code, receipt["result"]) == (5, "paused")
    for inst in (first, second):
        assert "the reinstall of 0.8.0 failed" in json.loads((inst / "PAUSED").read_text())["reason"]


def test_a_failure_before_the_upgrade_changes_nothing(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)

    def broken_backup(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(update.backup_mod, "backup", broken_backup)
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert (code, receipt["result"]) == (5, "failed_before_upgrade") and receipt["message"] == "OSError: disk full"
    assert world.uv_calls() == [] and not (inst / "UPDATING").exists()


# -- preflight refusals --------------------------------------------------------------------------------------


def test_preflight_refusals(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    wheel = str(world.wheel("0.8.1"))
    assert _run(world, "--from-wheel", wheel)[1]["message"].startswith("no instances: register one in ")
    inst = _instance(tmp_path)
    world.register(inst)
    (inst / "PAUSED").write_text("{}")
    assert _run(world, "--from-wheel", wheel) == (1, {"result": "refused", "message": f"{inst} is paused: investigate PAUSED first"})
    (inst / "PAUSED").unlink()
    (inst / "UPDATING").write_text("{}")
    assert "has UPDATING" in _run(world, "--from-wheel", wheel)[1]["message"]
    (inst / "UPDATING").unlink()
    monkeypatch.setattr(update.shutil, "disk_usage", lambda path: os.statvfs_result((0,) * 10) and type("U", (), {"free": 10})())
    assert "free disk is under twice the database" in _run(world, "--from-wheel", wheel)[1]["message"]
    monkeypatch.undo()
    world = World(tmp_path / "again", monkeypatch)
    world.register(inst)
    world.behave(**{"0.8.0": "broken_version"})
    world.install("agent-si==0.8.0")
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert code == 1 and receipt["message"] == (
        "the installed brain doesn't start; reinstall it with: "
        f"download the agent-si {update.__version__} wheel from https://github.com/joelbrilliant/agent-si/releases, then run: "
        "uv tool install --reinstall --managed-python --python 3.13 PATH-TO-THE-WHEEL")
    assert world.uv_calls() == []


def test_one_update_at_a_time(tmp_path, monkeypatch, clock: Clock) -> None:
    import fcntl

    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    registry.state_dir().mkdir(parents=True, exist_ok=True)
    with open(registry.state_dir() / "update.lock", "a+b") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        assert _run(world, "--from-wheel", str(world.wheel("0.8.1"))) == (
            1, {"result": "refused", "message": "another brain update is running"})


# -- stopping writes ------------------------------------------------------------------------------------


def test_updating_stops_every_writer(tmp_path, monkeypatch) -> None:
    inst = _instance(tmp_path)
    (inst / "UPDATING").write_text("{}")
    with pytest.raises(Unavailable, match="brain is updating; writes resume shortly"):
        _apply_note(inst, "blocked")
    assert "Updating; writes resume shortly." in _brief(inst)
    from brain.cli import main

    assert main(["search-rebuild", "--instance", str(inst)]) == 5
    assert main(["redact", "src-none", "--reason", "test", "--yes", "--instance", str(inst)]) == 5


WRITER = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    from brain import applier
    from tests.conftest import make_packet
    inst, gate, where = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    original = applier.Applier.preconditions if where == "before" else applier.Applier._begin

    def hold(self):
        original(self)
        (gate / "passed").write_text("")
        while not (gate / "go").exists():
            time.sleep(0.01)

    if where == "before":
        applier.Applier.preconditions = hold
    else:
        applier.Applier._begin = hold
    packet = make_packet("tester", {"op": "add_source", "row": {"id": "src-race-1", "kind": "user_note",
        "title": "race", "content": "race", "source_date": "2026-10-09"}})
    writer = applier.Applier(inst)
    try:
        print(writer.apply(json.dumps(packet).encode()).exit_code)
    except applier.Unavailable as exc:
        print("unavailable:", exc)
''')


@pytest.mark.parametrize("where", ["before", "inside"])
def test_a_writer_in_flight_cant_commit_after_the_backup(tmp_path, where) -> None:
    """Two processes. "before": the writer passed its preconditions, then UPDATING is written: it meets
    UPDATING after taking the lock and commits nothing. "inside": it already holds the lock: the drain waits
    for it, so its write lands before the backup and is in it."""
    inst = _instance(tmp_path)
    gate = tmp_path / "gate"
    gate.mkdir()
    script = tmp_path / "writer.py"
    script.write_text(WRITER)
    root = Path(__file__).resolve().parents[1]
    child = subprocess.Popen([sys.executable, str(script), str(inst), str(gate), where], cwd=root,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env={**os.environ, "PYTHONPATH": str(root)})
    deadline = time.time() + 30
    while not (gate / "passed").exists():
        assert time.time() < deadline and child.poll() is None, child.stderr.read() if child.poll() is not None else ""
        time.sleep(0.01)
    update._write_marker(inst / "UPDATING", {"from": "0.8.0", "to": "0.8.1"})
    import threading

    drained = threading.Event()
    thread = threading.Thread(target=lambda: (update._drain(inst / "brain.sqlite3"), drained.set()))
    thread.start()
    if where == "inside":
        time.sleep(0.3)
        assert not drained.is_set()  # the writer holds the lock, so the drain waits for it
    (gate / "go").write_text("")
    out, err = child.communicate(timeout=30)
    thread.join(timeout=30)
    assert drained.is_set()
    seq_after_drain = update._applied_seq(inst / "brain.sqlite3")
    rows = ro_rows(inst, "SELECT count(*) FROM sources WHERE id = 'src-race-1'")[0][0]
    if where == "before":
        assert out.strip() == "unavailable: brain is updating; writes resume shortly" and rows == 0
    else:
        assert out.strip() == "0" and rows == 1
    from brain import backup

    made = backup.backup(inst, inst / "backups", label="pre-update")
    assert made["applied_seq"] == seq_after_drain == update._applied_seq(inst / "brain.sqlite3")


# -- serve ----------------------------------------------------------------------------------------------


def _call(inst: Path, started) -> dict:
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "brief", "arguments": {}}}
    response, _ = server._dispatch(inst, message, server.LATEST_VERSION, None, started)
    return response["result"]


def test_serve_asks_for_a_restart_after_an_update(tmp_path, monkeypatch) -> None:
    inst = _instance(tmp_path)
    started = server.Started(inst)
    assert not _call(inst, started).get("isError")
    monkeypatch.setattr(server, "code_stamp", lambda: ("new", "files"))
    monkeypatch.setattr(server, "installed_version", lambda: "0.8.1")
    result = _call(inst, started)
    assert result["isError"] is True
    error = json.loads(result["content"][0]["text"])["error"]
    assert (error["code"], error["message"]) == ("unavailable", "brain updated to 0.8.1, restart this agent tool")
    monkeypatch.undo()
    assert not _call(inst, started).get("isError")
    started.schema = 5  # a store migrated under a running process
    assert "restart this agent tool" in _call(inst, started)["content"][0]["text"]


def test_serve_keeps_answering_lists_and_pings_after_an_update(tmp_path, monkeypatch) -> None:
    inst = _instance(tmp_path)
    started = server.Started(inst)
    monkeypatch.setattr(server, "code_stamp", lambda: ("new",))
    response, _ = server._dispatch(inst, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, server.LATEST_VERSION, None, started)
    assert response["result"] == {}


# -- no release source: nothing is fetched, nothing is picked by name ---------------------------------------


RELEASES = "https://github.com/joelbrilliant/agent-si/releases"


@pytest.mark.parametrize("argv, what", [
    ((), "brain update"),
    (("--to", "0.8.1"), "brain update --to"),
    (("--auto",), "brain update --auto"),
    (("--to", "0.8.1", "--from-wheel", "{wheel}"), "brain update --to"),
])
def test_update_without_a_wheel_is_refused_and_touches_nothing(tmp_path, monkeypatch, clock: Clock, argv, what) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    fake = fn.FakeNet(monkeypatch)
    argv = [part.format(wheel=world.wheel("0.8.1")) for part in argv]
    code, receipt = _run(world, *argv)
    assert (code, receipt["result"]) == (1, "refused")
    assert receipt["message"].startswith(f"{what} doesn't look for releases or fetch anything by name")
    assert RELEASES in receipt["message"] and "--from-wheel" in receipt["message"]
    assert fake.calls == [] and world.uv_calls() == []
    assert not (inst / "UPDATING").exists() and registry.read_state().get("last_result") is None


def test_check_makes_no_network_call_and_says_where_releases_are(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path, extra_toml='[updates]\nauto = false\npolicy = "agent"\n')
    world.register(inst)
    fake = fn.FakeNet(monkeypatch)
    code, receipt = _run(world, "--check")
    assert (code, receipt["result"], receipt["current"]) == (0, "not_checked", "0.8.0")
    assert RELEASES in receipt["message"] and fake.calls == []
    assert "Update:" not in _brief(inst) and registry.read_state().get("latest_stable") is None


def test_the_brief_line_only_reads_the_state_file(tmp_path, monkeypatch) -> None:
    state = {"current": "0.8.0", "latest_stable": "0.8.3", "latest_beta": "0.9.0b1"}
    ask = config_mod.Updates(auto=False, policy="ask")
    assert reads.update_line(config_mod.Updates(auto=False, policy="agent"), state) == (
        "Update: 0.8.3 available (stable). You may run brain update.")
    assert reads.update_line(ask, state) == "Update: 0.8.3 available (stable). Ask the person before running brain update."
    assert reads.update_line(config_mod.Updates(auto=True), state) is None
    assert reads.update_line(config_mod.Updates(auto=False, channel="beta"), state).startswith("Update: 0.9.0b1 available (beta).")
    assert reads.update_line(ask, {**state, "current": "0.8.3"}) is None
    assert reads.update_line(ask, {}) is None


def test_schedule_on_is_refused_and_off_still_works(tmp_path, monkeypatch) -> None:
    from brain import update_schedule
    from brain.cli import main

    import contextlib
    import io

    calls = []
    monkeypatch.setattr(update_schedule, "_launchctl", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    plist = tmp_path / "home" / "Library" / "LaunchAgents" / f"{update_schedule.LABEL}.plist"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["schedule", "on"])
    assert code == 1 and json.loads(out.getvalue())["result"] == "refused" and RELEASES in err.getvalue()
    assert calls == [] and not plist.exists()
    plist.parent.mkdir(parents=True)
    plist.write_text("left by an earlier version")
    monkeypatch.setattr(update_schedule.sys, "platform", "darwin")
    assert update_schedule.off()["status"] == "removed" and not plist.exists() and calls[0][0] == "bootout"


def test_snapshot_wheel_packs_the_installed_copy_for_a_rollback(tmp_path, monkeypatch) -> None:
    import importlib.metadata
    import zipfile

    site = tmp_path / "site"
    (site / "brain").mkdir(parents=True)
    (site / "brain" / "__init__.py").write_text('__version__ = "9.1.0"\n')
    (site / "brain" / "__pycache__").mkdir()
    (site / "brain" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    info = site / "agent_si-9.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: agent-si\nVersion: 9.1.0\n")
    (info / "WHEEL").write_text("Wheel-Version: 1.0\nTag: py3-none-any\n")
    (info / "INSTALLER").write_text("uv\n")
    (info / "RECORD").write_text("\n".join([
        "brain/__init__.py,,", "brain/__pycache__/x.pyc,,", "../../bin/brain,,", "agent_si-9.1.0.dist-info/METADATA,,",
        "agent_si-9.1.0.dist-info/WHEEL,,", "agent_si-9.1.0.dist-info/INSTALLER,,", "agent_si-9.1.0.dist-info/RECORD,,"]) + "\n")
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: importlib.metadata.PathDistribution(info))
    kept = REAL_SNAPSHOT("agent-si", tmp_path / "previous")
    assert kept == tmp_path / "previous" / "agent_si-9.1.0-py3-none-any.whl"
    assert versions.from_wheel(kept.name) == "9.1.0"
    with zipfile.ZipFile(kept) as wheel:
        names = sorted(wheel.namelist())
        assert names == ["agent_si-9.1.0.dist-info/METADATA", "agent_si-9.1.0.dist-info/RECORD",
                         "agent_si-9.1.0.dist-info/WHEEL", "brain/__init__.py"]
        record = wheel.read("agent_si-9.1.0.dist-info/RECORD").decode()
        assert "brain/__init__.py,sha256=" in record and record.endswith("agent_si-9.1.0.dist-info/RECORD,,\n")
    # An editable install (a .pth file) or a missing package can't be packed.
    (info / "RECORD").write_text("__editable__.agent_si-9.1.0.pth,,\n")
    assert REAL_SNAPSHOT("agent-si", tmp_path / "again") is None
    monkeypatch.setattr(importlib.metadata, "distribution", lambda name: (_ for _ in ()).throw(importlib.metadata.PackageNotFoundError(name)))
    assert REAL_SNAPSHOT("agent-si", tmp_path / "again") is None


def test_no_kept_wheel_means_no_update(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    monkeypatch.setattr(update, "snapshot_wheel", lambda name, folder: None)
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert (code, receipt["result"]) == (1, "refused") and "--rollback-from" in receipt["message"] and RELEASES in receipt["message"]
    assert world.uv_calls() == [] and not (inst / "UPDATING").exists()
    wrong = world.wheel("0.7.0", folder="elsewhere")
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")), "--rollback-from", str(wrong))
    assert code == 1 and "is the wheel for 0.7.0, but the version you are running is 0.8.0" in receipt["message"]


def test_versions() -> None:
    assert versions.parse("1.2.3") == (1, 2, 3, None) and versions.parse("1.0.0rc2") == (1, 0, 0, (2, 2))
    assert versions.parse("1.2") is None and versions.parse("01.2.3") is None and versions.parse("1.2.3.post1") is None
    ordered = ["0.9.0", "1.0.0a1", "1.0.0b2", "1.0.0rc1", "1.0.0", "1.0.1"]
    assert sorted(reversed(ordered), key=versions.key) == ordered
    assert versions.newer("1.0.0", "1.0.0rc1") and not versions.newer("1.0.0b1", "1.0.0")
    assert versions.from_wheel("agent_brain-0.8.1-py3-none-any.whl") == "0.8.1"
    assert versions.from_wheel("agent_brain-0.8.1.tar.gz") is None


# -- config and registry ---------------------------------------------------------------------------


@pytest.mark.parametrize("table, message", [
    ('channel = "nightly"', 'updates.channel must be "stable" or "beta"'),
    ('auto = "yes"', "updates.auto must be true or false"),
    ('policy = "always"', 'updates.policy must be "ask" or "agent"'),
    ("when = 3", "unknown key updates.when"),
])
def test_updates_config(tmp_path, table, message) -> None:
    from tests.phase2a import custom_config, load_text

    with pytest.raises(config_mod.ConfigError, match=message):
        load_text(tmp_path, custom_config(extra_toml=f"[updates]\n{table}\n"))
    cfg = load_text(tmp_path / "ok", custom_config())
    assert cfg.updates == config_mod.Updates("stable", False, "ask")


def test_registry(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert registry.instances() == [] and registry.registry_path() == tmp_path / "config" / "agent-si" / "instances.toml"
    one, two = tmp_path / "one", tmp_path / "two"
    assert registry.add(one) and registry.add(two) and not registry.add(one)
    assert registry.instances() == [one.resolve(), two.resolve()]
    registry.registry_path().write_text("not toml [")
    assert registry.instances() == []
    assert registry.state_dir() == tmp_path / "state" / "agent-si"


def test_the_runbook_restores_a_bundle_and_collectors_catch_up(tmp_path, monkeypatch, clock: Clock) -> None:
    """Acceptance 6: a defect found later. Restore the pre-update bundle into a new folder, and the collectors
    catch up from the cursors in it: what arrived since is captured once, and nothing already held is doubled."""
    from brain import backup
    from brain_fetch import documents

    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.md").write_text("# Plan\n\nThe pilot starts Monday.")
    inst = custom_instance(tmp_path / "inst", table={"live_folder": str(folder)})
    assert documents.run(inst, "work-docs")["cursor"] == "advanced"
    bundle = backup.backup(inst, inst / "backups", label="pre-update")["bundle"]
    clock.at("2026-10-09T01:00:00Z")
    (folder / "b.md").write_text("# Pricing\n\nPro is 128k tokens.")
    assert documents.run(inst, "work-docs")["cursor"] == "advanced"
    restored = tmp_path / "restored"
    backup.restore(bundle, restored)
    assert (restored / "fetch" / "work-docs.state.json").is_file()  # the cursor came back with the store
    clock.at("2026-10-09T02:00:00Z")
    result = documents.run(restored, "work-docs")
    assert (result["status"], result["files"]["changed"], result["files"]["unchanged"]) == ("ok", 1, 1)
    refs = ro_rows(restored, "SELECT external_ref, count(*) FROM sources WHERE id GLOB 'src-cap-*' GROUP BY external_ref")
    assert refs == [("document:work-docs/a.md", 1), ("document:work-docs/b.md", 1)]


def test_status_names_a_stale_updating_and_brain_must_be_installed(tmp_path, monkeypatch, clock: Clock) -> None:
    world = World(tmp_path, monkeypatch)
    inst = _instance(tmp_path)
    world.register(inst)
    update._write_marker(inst / "UPDATING", {"from": "0.8.0", "to": "0.8.1", "pid": 999_999_999})
    code, payload = _run(world, "--status")
    assert code == 0 and payload["instances"] == [{"instance": str(inst), "updating": True, "paused": False,
                                                   "missing": False, "updating_stale": True}]
    update._write_marker(inst / "UPDATING", {"pid": os.getpid()})
    assert _run(world, "--status")[1]["instances"][0]["updating_stale"] is False
    (inst / "UPDATING").unlink()
    (world.bin / "brain").unlink()
    code, receipt = _run(world, "--from-wheel", str(world.wheel("0.8.1")))
    assert code == 1 and receipt["message"].startswith(f"brain isn't installed as a uv tool (no {world.bin / 'brain'})")


def test_a_deleted_brain_doesnt_stop_updates_for_the_rest(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    kept = custom_instance(tmp_path / "kept", extra_toml="")
    gone = custom_instance(tmp_path / "gone", extra_toml="")
    registry.add(gone)
    registry.add(kept)
    import shutil

    shutil.rmtree(gone)
    assert update._instances(None) == [kept.resolve()]
    assert f"skipped {gone.resolve()}: registered, but the folder is gone" in (registry.state_dir() / "update.log").read_text()
    assert [entry["missing"] for entry in update.status(None)["instances"]] == [True, False]
    # Still strict: a missing --instance, and a registered folder that exists but isn't a brain.
    with pytest.raises(update.Refused, match="isn't an instance"):
        update._instances([str(gone)])
    (tmp_path / "not-a-brain").mkdir()
    registry.add(tmp_path / "not-a-brain")
    with pytest.raises(update.Refused, match="isn't an instance"):
        update._instances(None)


def test_init_no_register_stays_out_of_the_registry(tmp_path) -> None:
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_STATE_HOME": str(tmp_path / "state")}
    for name, extra in (("scratch", ["--no-register"]), ("kept", [])):
        done = subprocess.run(["brain", "init", name, "--dir", str(tmp_path), *extra], env=env, capture_output=True,
                              text=True, timeout=60)
        assert done.returncode == 0, done.stderr
    text = (tmp_path / "config" / "agent-si" / "instances.toml").read_text()
    assert str((tmp_path / "kept").resolve()) in text and "scratch" not in text
