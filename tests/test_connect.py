"""Brain connect through fake harness commands in a fake home, the guided init, the registry,
install.sh, and the MCP era rule. No test reads or changes a real harness config."""
from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from brain import connect, guided, registry, server
from tests.phase2a import custom_instance
from tests.update_world import World

ROOT = Path(__file__).resolve().parents[1]

FAKE_HARNESS = r'''#!PYTHON
import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
home = Path(os.environ["HOME"])
with open(home / "harness-calls.jsonl", "a") as log:
    log.write(json.dumps([name, *args]) + "\n")
store = home / f".fake-{name}.json"
if name == "gemini":
    store = home / ".gemini" / "settings.json"
    store.parent.mkdir(exist_ok=True)
data = json.loads(store.read_text()) if store.exists() else {}
servers = data.setdefault("mcpServers", {})
def save():
    store.write_text(json.dumps(data))
mode = os.environ.get("FAKE_MODE", "")
if args[:2] == ["mcp", "add"]:
    rest = [a for a in args[2:]]
    if name == "hermes":
        server = rest[0]; command = rest[rest.index("--command") + 1]; argv = rest[rest.index("--args") + 1:]
    else:
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] in ("-s", "--scope", "--transport") else rest[1:]
        server = rest[0]
        tail = rest[1:]
        if tail and tail[0] == "--":
            tail = tail[1:]
        command, argv = tail[0], tail[1:]
        if "--" in argv and not (name == "gemini" and mode == "buggy"):
            argv = [a for a in argv if a != "--"]
    servers[server] = {"command": command, "args": argv}
    save()
    sys.exit(0)
if args[:2] in (["mcp", "remove"], ["mcp", "rm"]):
    servers.pop(args[-1], None)
    save()
    sys.exit(0)
if args[:2] == ["mcp", "get"]:
    entry = servers.get(args[2])
    if entry is None:
        sys.exit(1)
    print(f"{args[2]}:\n  Command: {entry['command']}\n  Args: {' '.join(entry['args'])}")
    sys.exit(0)
if args[:2] == ["mcp", "list"]:
    print("\n".join(f"{key}  {value['command']}  {' '.join(value['args'])}" for key, value in servers.items()))
    sys.exit(0)
if args[:2] == ["mcp", "doctor"]:
    entry = servers.get(args[2])
    # The shape Grok Build 1.0.51 prints: a target string, not command and args fields.
    print(json.dumps({"servers": [{"name": args[2], "transport": "stdio", "target": " ".join([entry["command"], *entry["args"]]),
                                   "source": "config", "healthy": True}] if entry else []}))
    sys.exit(0)
if args[:2] == ["mcp", "test"]:
    sys.exit(0 if args[2] in servers else 1)
sys.exit(2)
'''


def _world(tmp_path, monkeypatch, harnesses=("claude", "codex", "gemini", "grok", "hermes"), cursor=True):
    world = World(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    # Only the fakes: the session PATH holds ~/.local/bin, where this machine's real harness commands live.
    monkeypatch.setenv("PATH", f"{world.fakebin}{os.pathsep}{world.bin}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for name in harnesses:
        path = world.fakebin / name
        path.write_text(FAKE_HARNESS.replace("#!PYTHON", "#!" + sys.executable, 1))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    if cursor:
        (home / ".cursor").mkdir()
    inst = custom_instance(tmp_path / "brains", extra_toml="")
    return world, home, inst


def _calls(home: Path) -> list[list[str]]:
    path = home / "harness-calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_connect_all_uses_each_tools_own_command(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch)
    brain, target = str(world.bin / "brain"), str(inst.resolve())
    (home / ".grok").mkdir()
    (home / ".grok" / "AGENTS.md").write_text("# Grok rules\n")
    results = {item["harness"]: item for item in connect.connect(inst, ["all"], yes=True)}
    assert sorted(results) == ["claude", "codex", "cursor", "gemini", "grok", "hermes"]
    for harness, item in results.items():
        assert (item["status"], item["smoke"]) == ("written", "pass"), (harness, item)
        assert item["message"].endswith(f"restart {harness} to load it")
    adds = {call[0]: call[1:] for call in _calls(home) if call[1:3] == ["mcp", "add"]}
    name = "brain-demo"
    assert adds["claude"] == ["mcp", "add", "--transport", "stdio", "--scope", "user", name, "--", brain, "serve", "--instance", target]
    assert adds["codex"] == ["mcp", "add", name, "--", brain, "serve", "--instance", target]
    assert adds["gemini"] == ["mcp", "add", "-s", "user", name, brain, "--", "serve", "--instance", target]
    assert adds["grok"] == ["mcp", "add", "-s", "user", name, brain, "--", "serve", "--instance", target]
    assert adds["hermes"] == ["mcp", "add", name, "--command", brain, "--args", "serve", "--instance", target]
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert cursor == {"mcpServers": {name: {"type": "stdio", "command": brain, "args": ["serve", "--instance", target]}}}
    assert (results["claude"]["instructions"], results["grok"]["instructions"]) == ("not needed", "added")
    assert results["hermes"]["instructions"].startswith(f"add this line to {home / '.hermes' / 'AGENTS.md'}")
    assert (home / ".grok" / "AGENTS.md").read_text() == (
        "# Grok rules\nCall the brain's brief tool first, and search it before saying you don't remember. "
        "Keep no memory notes in files. <!-- agent-si:brain-demo -->\n"
    )
    # Again: nothing changes, and Grok, which sees the server already, isn't asked to add it twice.
    before = len(_calls(home))
    again = {item["harness"]: item["status"] for item in connect.connect(inst, ["claude", "grok", "cursor"], yes=True)}
    assert again == {"claude": "unchanged", "grok": "unchanged", "cursor": "unchanged"}
    assert not [call for call in _calls(home)[before:] if call[1:3] == ["mcp", "add"]]


def test_remove_takes_out_the_line_an_earlier_version_added(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch)
    rules = home / ".grok" / "AGENTS.md"
    rules.parent.mkdir()
    earlier = "Call the brain's brief tool first. <!-- agent-si:brain-demo -->"
    for before, after in (("# Grok rules\n" + earlier + "\n", "# Grok rules\n"), ("# Grok rules\n" + earlier, "# Grok rules"),
                          ("# Grok rules\n" + earlier + "\nMore rules\n", "# Grok rules\nMore rules\n")):
        rules.write_text(before)
        connect.connect(inst, ["grok"], yes=True)
        assert rules.read_text() == before  # the mark is there, so nothing is added twice
        connect.connect(inst, ["grok"], remove=True, yes=True)
        assert rules.read_text() == after


def test_remove_takes_out_exactly_what_was_added(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch)
    rules = home / ".grok" / "AGENTS.md"
    rules.parent.mkdir()
    for original in (b"# Grok rules\n", b"# no newline at the end", b""):
        rules.write_bytes(original)
        cursor = home / ".cursor" / "mcp.json"
        cursor.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "keep": True}))
        connect.connect(inst, ["grok", "cursor"], yes=True)
        assert rules.read_bytes() != original
        removed = {item["harness"]: (item["status"], item["instructions"]) for item in
                   connect.connect(inst, ["grok", "cursor"], remove=True, yes=True)}
        assert removed == {"grok": ("removed", "removed"), "cursor": ("removed", "")}
        assert rules.read_bytes() == original  # byte-identical
        assert json.loads(cursor.read_text()) == {"mcpServers": {"other": {"command": "x"}}, "keep": True}


def test_cursor_never_writes_an_unparseable_file(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=())
    path = home / ".cursor" / "mcp.json"
    for broken in ('{"mcpServers": {"a": 1},}', "// comment\n{}", '{"mcpServers": []}', "[1, 2]"):
        path.write_text(broken)
        result = connect.connect(inst, ["cursor"], yes=True)[0]
        assert result["status"] == "printed" and path.read_text() == broken
        assert json.loads(result["text"])["mcpServers"]["brain-demo"]["type"] == "stdio"
    path.write_text('{"mcpServers": {}}')
    declined = connect.connect(inst, ["cursor"], ask=lambda question: False)[0]
    assert declined["status"] == "declined" and path.read_text() == '{"mcpServers": {}}'
    assert connect.connect(inst, ["cursor"], ask=lambda question: True)[0]["status"] == "written"
    backups = list(path.parent.glob("mcp.json.brain-backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == '{"mcpServers": {}}'
    link = home / ".cursor" / "mcp.json"
    link.unlink()
    (home / "elsewhere.json").write_text("{}")
    link.symlink_to(home / "elsewhere.json")
    assert connect.connect(inst, ["cursor"], yes=True)[0]["status"] == "printed"
    assert (home / "elsewhere.json").read_text() == "{}"


def test_print_writes_nothing_and_missing_tools_print(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=("claude",))
    results = {item["harness"]: item for item in connect.connect(inst, ["claude", "cursor", "hermes"], print_only=True)}
    assert all(item["status"] == "printed" for item in results.values()) and _calls(home) == []
    assert not (home / ".cursor" / "mcp.json").exists()
    assert results["hermes"]["text"].startswith("mcp_servers:\n  brain-demo:\n")
    missing = {item["harness"]: item for item in connect.connect(inst, ["codex", "zed"])}
    assert missing["codex"]["status"] == "printed" and missing["codex"]["text"].startswith("[mcp_servers.brain-demo]\n")
    assert missing["zed"]["message"] == "an unknown tool: this is the generic MCP entry"


def test_the_gemini_read_back_catches_wrong_args(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=("gemini",))
    monkeypatch.setenv("FAKE_MODE", "buggy")
    result = connect.connect(inst, ["gemini"])[0]
    assert result["status"] == "failed" and "gemini stored different args" in result["message"]
    assert json.loads(result["text"])["mcpServers"]["brain-demo"]["args"] == ["serve", "--instance", str(inst.resolve())]


def test_instruction_files_that_are_links_are_never_written(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=("grok",))
    (home / ".grok").mkdir()
    real = home / "contracts-AGENTS.md"
    real.write_text("# shared contract\n")
    (home / ".grok" / "AGENTS.md").symlink_to(real)
    result = connect.connect(inst, ["grok"], yes=True)[0]
    assert result["instructions"].startswith("add this line to ") and real.read_text() == "# shared contract\n"


def test_connect_needs_an_installed_brain(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch)
    (world.bin / "brain").unlink()
    with pytest.raises(connect.ConnectError, match="brain isn't installed as a uv tool"):
        connect.connect(inst, ["claude"])


# -- init, the registry and the guided questions ------------------------------------------------------------


def test_brain_init_registers_the_instance(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    from brain.cli import main

    (tmp_path / "brains").mkdir()
    assert main(["init", "work", "--dir", str(tmp_path / "brains"), "--principal", "Alex Example"]) == 0
    assert registry.instances() == [(tmp_path / "brains" / "work").resolve()]


def test_guided_init_asks_five_questions(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=("claude",), cursor=False)
    from brain import update_schedule

    def forbidden(*args, **kwargs):
        raise AssertionError("the guided init reached launchctl")

    monkeypatch.setattr(update_schedule, "_launchctl", forbidden)
    monkeypatch.setattr(update_schedule, "LAUNCHCTL", "/nonexistent/launchctl")
    answers = io.StringIO("bad name!\nwork\n" + str(tmp_path / "Brains") + "\nAlex Example\nAustralia/Sydney\nall\n")
    out = io.StringIO()
    result = guided.run(answers, out)
    target = (tmp_path / "Brains" / "work").resolve()
    assert Path(result["instance"]).resolve() == target and target / "brain.toml" in list(target.iterdir())
    text = (target / "brain.toml").read_text()
    assert 'display_name = "Alex Example"' in text and 'timezone = "Australia/Sydney"' in text
    assert target in registry.instances()
    assert [item["harness"] for item in result["connected"]] == ["claude"]
    said = out.getvalue()
    assert "Use lowercase letters" in said and said.rstrip().endswith("Ask your agent: read my brain brief.")
    assert "schedule" not in said.lower() and "nightly" not in said.lower() and "report" not in said.lower()
    # --no-register reaches the guided path too.
    answers = io.StringIO("scratch\n" + str(tmp_path / "Brains") + "\nAlex Example\nAustralia/Sydney\nnone\n")
    scratch = guided.run(answers, io.StringIO(), register=False)
    assert Path(scratch["instance"]).resolve() not in registry.instances() and target in registry.instances()


def test_init_with_no_name_and_no_terminal_says_how(monkeypatch) -> None:
    import builtins

    real_open = builtins.open

    def no_tty(path, *args, **kwargs):
        if path == "/dev/tty":
            raise OSError("no terminal")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_tty)
    from brain.cli import main

    err = io.StringIO()
    import contextlib

    with contextlib.redirect_stderr(err):
        assert main(["init"]) == 2
    assert err.getvalue().startswith('brain: brain init needs a terminal to ask, or a name: brain init work --principal "Your Name"')


# -- install.sh -----------------------------------------------------------------------------------------------


def _install(world: World, home: Path, *args: str, path_extra: str = "") -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": f"{path_extra}{world.fakebin}:{world.bin}:/usr/bin:/bin",
           "UV_TOOL_BIN_DIR": str(world.bin), "XDG_CONFIG_HOME": str(home / "config"), "XDG_STATE_HOME": str(home / "state")}
    return subprocess.run(["sh", str(ROOT / "install.sh"), *args], env=env, capture_output=True, text=True,
                          start_new_session=True, stdin=subprocess.DEVNULL, timeout=120)


def test_install_sh_without_a_terminal_prints_the_init_line(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    (world.bin / "brain").unlink()
    done = _install(world, home, "--from", str(world.wheel("0.9.0")))
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[1] == "agent-brain 0.9.0 (schema 6)"
    assert lines[2] == "There's no terminal to ask questions in. Make your brain with:"
    assert lines[3].startswith(f'  {world.bin / "brain"} init work --principal "Your Name" --timezone ')
    assert lines[-1] == "Open a new Terminal window, then ask your agent: read my brain brief."
    install = [call for call in world.uv_calls() if call[:2] == ["tool", "install"]][0]
    assert install[:5] == ["tool", "install", "--managed-python", "--python", "3.13"]


def test_install_sh_stops_for_a_foreign_brain_and_unknown_flags(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    other = tmp_path / "other-bin"
    other.mkdir()
    (other / "brain").write_text("#!/bin/sh\necho someone else's brain\n")
    (other / "brain").chmod(0o755)
    done = _install(world, home, "--from", str(world.wheel("0.9.0")), path_extra=f"{other}:")
    assert done.returncode == 1 and done.stderr.startswith(f"install.sh: a brain command is already installed at {other / 'brain'}")
    assert _install(world, home, "--bogus").returncode == 2
    # Agent memory is a separate tool with its own installer, so this one doesn't offer it.
    for flag in ("--with-memory", "--memory-only"):
        assert _install(world, home, flag).returncode == 2


def test_install_sh_never_installs_agent_si_by_name(tmp_path, monkeypatch) -> None:
    # agent-si isn't on PyPI and its name there is free, so a by-name install would install whoever registers it.
    world, home, inst = _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    (world.bin / "brain").unlink()
    for args in ((), ("--version", "1.0.0")):
        done = _install(world, home, *args)
        assert done.returncode == 2, done.stderr
        assert "--from" in done.stderr and "install-agent-si.sh" in done.stderr and "github.com/joelbrilliant/agent-si/releases" in done.stderr
        assert world.uv_calls() == [] and not (world.bin / "brain").exists()


def test_install_sh_is_short_posix_and_pinned() -> None:
    text = (ROOT / "install.sh").read_text()
    assert text.startswith("#!/bin/sh\n") and len(text.splitlines()) <= 100
    assert 'UV_VERSION="0.11.6"' in text and "--managed-python" in text and "/dev/tty" in text
    from brain import __version__

    assert f'VERSION="{__version__}"' in text  # the installer installs the release it ships with
    assert subprocess.run(["sh", "-n", str(ROOT / "install.sh")]).returncode == 0
    for bashism in ("[[", "local ", "function ", "$'", "echo -e"):
        assert bashism not in text, bashism


# -- the MCP era ----------------------------------------------------------------------------------------


def test_an_unserved_protocol_version_is_minus_32600(tmp_path) -> None:
    inst = custom_instance(tmp_path / "inst")
    message = {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"_meta": {"protocolVersion": "2026-07-28"}}}
    response, _ = server._dispatch(inst, message, server.LATEST_VERSION, None)
    assert response["error"]["code"] == -32600 and "2025-11-25" in response["error"]["message"]
    fine = {**message, "params": {"_meta": {"protocolVersion": "2025-06-18"}}}
    assert "result" in server._dispatch(inst, fine, server.LATEST_VERSION, None)[0]


def test_no_real_harness_command_is_reachable(tmp_path, monkeypatch) -> None:
    import shutil

    world, home, inst = _world(tmp_path, monkeypatch, harnesses=())
    for name in ("claude", "codex", "gemini", "grok", "hermes", "cursor-agent"):
        assert shutil.which(name) is None, name


def test_connecting_again_and_removing_twice_are_quiet(tmp_path, monkeypatch) -> None:
    world, home, inst = _world(tmp_path, monkeypatch)
    first = {item["harness"]: item["status"] for item in connect.connect(inst, ["all"], yes=True)}
    assert set(first.values()) == {"written"}
    again = {item["harness"]: item["status"] for item in connect.connect(inst, ["all"], yes=True)}
    assert set(again.values()) == {"unchanged"}
    adds = [call for call in _calls(home) if call[1:3] == ["mcp", "add"]]
    assert len(adds) == 5  # one each, the first time only
    gone = {item["harness"]: item["status"] for item in connect.connect(inst, ["all"], remove=True, yes=True)}
    assert set(gone.values()) == {"removed"}
    twice = {item["harness"]: (item["status"], item["message"]) for item in connect.connect(inst, ["all"], remove=True, yes=True)}
    assert {status for status, _ in twice.values()} == {"unchanged"}
    # A different entry under our name is replaced, not stacked.
    data = json.loads((home / ".fake-codex.json").read_text())
    data["mcpServers"]["brain-demo"] = {"command": "/old/brain", "args": ["serve"]}
    (home / ".fake-codex.json").write_text(json.dumps(data))
    assert connect.connect(inst, ["codex"], yes=True)[0]["status"] == "written"
    stored = json.loads((home / ".fake-codex.json").read_text())["mcpServers"]["brain-demo"]
    assert stored["command"] == str(world.bin / "brain")

