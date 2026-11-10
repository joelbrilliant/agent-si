"""brain setup: a brain and every detected tool in one command, one question at most, nothing scheduled.

The harness commands are the fakes of tests/test_connect.py in a fake home. A guard fails any test that reaches
launchctl, because launchctl acts on the real GUI domain whatever HOME says.
"""
from __future__ import annotations

import builtins
import io
import subprocess
from pathlib import Path

import pytest

from brain import registry, setup, update_schedule
from brain.cli import main
from brain.init import InitError
from tests.test_connect import _calls, _world

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_launchctl(monkeypatch):
    """Anything that runs launchctl fails the test that did it."""
    real_run = subprocess.run

    def run(argv, *args, **kwargs):
        assert "launchctl" not in str(argv[0]), f"brain setup reached launchctl: {argv}"
        return real_run(argv, *args, **kwargs)

    def scheduled(*args, **kwargs):
        raise AssertionError("brain setup reached launchctl")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(update_schedule, "LAUNCHCTL", "/nonexistent/launchctl")
    monkeypatch.setattr(update_schedule, "_launchctl", scheduled)
    monkeypatch.setattr(setup, "default_principal", lambda: "Alex Example")


def _no_tty(monkeypatch) -> list[str]:
    """/dev/tty cannot be opened, and every attempt is recorded."""
    real_open = builtins.open
    tried: list[str] = []

    def no_tty(path, *args, **kwargs):
        if path == "/dev/tty":
            tried.append(path)
            raise OSError("no terminal")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", no_tty)
    return tried


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return ["setup", "--dir", str(tmp_path / "my-brains"), "--timezone", "Australia/Sydney", *extra]


def test_yes_asks_nothing_and_connects_every_tool(tmp_path, monkeypatch, capsys) -> None:
    world, home, _ = _world(tmp_path, monkeypatch)
    tried = _no_tty(monkeypatch)
    assert main(_args(tmp_path, "--yes")) == 0
    out = capsys.readouterr().out
    assert tried == []  # no question, so no terminal was opened
    target = (tmp_path / "my-brains" / "work").resolve()
    assert f"Brain work: created at {target}" in out and "Your name: Alex Example" in out
    text = (target / "brain.toml").read_text()
    assert 'display_name = "Alex Example"' in text and 'timezone = "Australia/Sydney"' in text
    assert registry.instances() == [target]
    for harness in ("claude", "codex", "gemini", "grok", "cursor", "hermes"):
        assert f"  {harness}: written, smoke test pass (" in out and f"restart {harness} to load it)" in out
        assert f"undo: {world.bin / 'brain'} connect {harness} --remove --yes --instance {target}" in out
    assert sorted({call[0] for call in _calls(home)}) == ["claude", "codex", "gemini", "grok", "hermes"]
    assert not (home / "Library" / "LaunchAgents").exists()


def test_the_undo_command_removes_what_setup_connected(tmp_path, monkeypatch, capsys) -> None:
    world, home, _ = _world(tmp_path, monkeypatch, harnesses=("claude", "codex"), cursor=True)
    assert main(_args(tmp_path, "--yes")) == 0
    capsys.readouterr()
    target = (tmp_path / "my-brains" / "work").resolve()
    assert (home / ".cursor" / "mcp.json").is_file()
    assert main(["connect", "cursor", "claude", "--remove", "--yes", "--instance", str(target)]) == 0
    assert "brain-work" not in (home / ".cursor" / "mcp.json").read_text()


def test_one_question_without_yes_and_return_keeps_the_default(tmp_path, monkeypatch) -> None:
    _world(tmp_path, monkeypatch, harnesses=("claude",), cursor=False)
    reader, prompt, out = io.StringIO("\nnot read\n"), io.StringIO(), io.StringIO()
    result = setup.run(setup.Options(dir=str(tmp_path / "my-brains"), timezone="UTC"), out=out, terminal=(reader, prompt))
    assert prompt.getvalue() == "Your name [Alex Example]: "
    assert reader.read() == "not read\n"  # one line read, no more
    assert 'display_name = "Alex Example"' in (Path(result["instance"]) / "brain.toml").read_text()


def test_a_typed_name_wins_and_a_bad_one_is_asked_for_again(tmp_path, monkeypatch) -> None:
    _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    reader, prompt = io.StringIO("!!!\nSam Example\n"), io.StringIO()
    result = setup.run(setup.Options(dir=str(tmp_path / "my-brains")), out=io.StringIO(), terminal=(reader, prompt))
    assert "Use your full name written with letters" in prompt.getvalue()
    assert 'display_name = "Sam Example"' in (Path(result["instance"]) / "brain.toml").read_text()


def test_a_name_on_the_command_line_asks_nothing(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, harnesses=(), cursor=False)

    class Forbidden:
        def readline(self):
            raise AssertionError("a question was asked")

        def write(self, text):
            raise AssertionError("a question was asked")

    result = setup.run(setup.Options(dir=str(tmp_path / "my-brains"), principal="Sam Example"), out=io.StringIO(),
                       terminal=(Forbidden(), Forbidden()))
    assert 'display_name = "Sam Example"' in (Path(result["instance"]) / "brain.toml").read_text()
    none = setup.run(setup.Options(dir=str(tmp_path / "my-brains"), name="empty", principal=""), out=io.StringIO(),
                     terminal=(Forbidden(), Forbidden()))
    assert "[principal]" not in (Path(none["instance"]) / "brain.toml").read_text().replace("# [principal]", "")


def test_no_terminal_and_no_yes_carries_on_with_the_defaults(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, harnesses=("claude",), cursor=False)
    tried = _no_tty(monkeypatch)
    assert main(_args(tmp_path)) == 0
    out = capsys.readouterr().out
    assert tried == ["/dev/tty"]
    assert "There's no terminal to ask in, so I used the defaults" in out and "Your name: Alex Example" in out
    assert "claude: written, smoke test pass" in out


def test_an_existing_brain_is_kept_and_asks_nothing(tmp_path, monkeypatch, capsys) -> None:
    world, home, _ = _world(tmp_path, monkeypatch, harnesses=("claude",), cursor=False)
    assert main(_args(tmp_path, "--yes")) == 0
    target = (tmp_path / "my-brains" / "work").resolve()
    before = {path.name: path.read_bytes() for path in target.iterdir() if path.is_file()}
    capsys.readouterr()
    tried = _no_tty(monkeypatch)
    assert main(_args(tmp_path, "--principal", "Someone Else")) == 0
    out = capsys.readouterr().out
    assert tried == []
    assert f"Brain work: kept the existing one at {target}" in out and "claude: unchanged, smoke test pass" in out
    assert {path.name: path.read_bytes() for path in target.iterdir() if path.is_file()} == before
    assert registry.instances() == [target]


def test_no_connect_and_no_tools_found_say_how_to_connect_later(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, harnesses=("claude",), cursor=False)
    assert main(_args(tmp_path, "--yes", "--no-connect")) == 0
    out = capsys.readouterr().out
    target = (tmp_path / "my-brains" / "work").resolve()
    assert f"not connected (--no-connect). Later: brain connect all --instance {target}" in out
    assert _calls(tmp_path / "home") == []
    _world(tmp_path / "second", monkeypatch, harnesses=(), cursor=False)
    assert main(["setup", "--yes", "--dir", str(tmp_path / "second" / "my-brains"), "--timezone", "UTC"]) == 0
    assert "Tools: none found on this machine" in capsys.readouterr().out


def test_a_tool_that_fails_its_smoke_test_or_its_add_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, harnesses=("claude", "gemini"), cursor=False)
    monkeypatch.setenv("FAKE_MODE", "buggy")  # gemini stores different args than it was given
    assert main(_args(tmp_path, "--yes")) == 1
    out = capsys.readouterr().out
    assert "gemini: failed" in out and "claude: written, smoke test pass" in out
    assert f"Brain work: created at" in out  # the brain stays


@pytest.mark.parametrize(
    "extra, fix",
    [
        (["--name", "Bad Name"], "lowercase letters, digits and dashes"),
        (["--principal", "!!!"], 'such as "Alex Example"'),
        (["--timezone", "Mars/Base"], "--timezone with a name from the IANA list"),
    ],
)
def test_a_refusal_names_its_fix_and_exits_2(tmp_path, monkeypatch, capsys, extra, fix) -> None:
    _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    argv = ["setup", "--yes", "--dir", str(tmp_path / "my-brains"), "--timezone", "UTC", *extra]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("brain: ") and fix in err
    assert not (tmp_path / "my-brains" / "work").exists()


def test_a_folder_that_is_a_file_is_refused_with_its_fix(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    (tmp_path / "my-brains").write_text("a file")
    assert main(_args(tmp_path, "--yes")) == 2
    assert "pass --dir with a folder you can write to" in capsys.readouterr().err


def test_a_folder_holding_something_else_called_work_is_never_overwritten(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, harnesses=(), cursor=False)
    (tmp_path / "my-brains" / "work").mkdir(parents=True)
    (tmp_path / "my-brains" / "work" / "mine.txt").write_text("keep")
    assert main(_args(tmp_path, "--yes")) == 1
    assert (tmp_path / "my-brains" / "work" / "mine.txt").read_text() == "keep"
    with pytest.raises(InitError):
        setup.run(setup.Options(dir=str(tmp_path / "my-brains")), out=io.StringIO(), terminal=None)


def test_the_default_name_comes_from_the_account(monkeypatch) -> None:
    monkeypatch.undo()  # the autouse stand-in
    import pwd
    import types

    monkeypatch.setattr(setup.sys, "platform", "darwin")
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="Alex Example\n"))
    assert setup.default_principal() == "Alex Example"
    monkeypatch.setattr(setup.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""))
    assert setup.default_principal() is None
    monkeypatch.setattr(setup.sys, "platform", "linux")
    entry = types.SimpleNamespace(pw_gecos="Sam Example,,,")
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: entry)
    assert setup.default_principal() == "Sam Example"
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_gecos=""))
    assert setup.default_principal() is None
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: types.SimpleNamespace(pw_gecos="12345"))
    assert setup.default_principal() is None  # not a name a person id can be made from


def test_setup_never_touches_the_update_schedule() -> None:
    source = (ROOT / "src" / "brain" / "setup.py").read_text()
    assert "update_schedule" not in source and "launchctl" not in source.replace("launchctl, ", "")


@pytest.mark.parametrize("link, zone", [
    ("/usr/share/zoneinfo/Australia/Brisbane", "Australia/Brisbane"),
    ("/var/db/timezone/zoneinfo/Australia/Brisbane", "Australia/Brisbane"),
    # Ubuntu's tzdata, set up with nobody to answer, links /etc/localtime to zoneinfo//UTC (the clean Linux run).
    ("/usr/share/zoneinfo//UTC", "UTC"),
    ("../usr/share/zoneinfo/Etc/UTC", "Etc/UTC"),
    ("/somewhere/else", "UTC"),
])
def test_the_local_zone_is_a_zone_name(monkeypatch, link, zone) -> None:
    from brain import guided

    monkeypatch.setattr(guided.os, "readlink", lambda path: link)
    assert guided.local_zone() == zone


def test_a_detected_zone_with_no_data_falls_back_to_utc(tmp_path, monkeypatch, capsys) -> None:
    _world(tmp_path, monkeypatch, [])
    monkeypatch.setattr(setup, "local_zone", lambda: "Not/AZone")
    assert main(["setup", "--dir", str(tmp_path / "my-brains"), "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Not/AZone" in out and "UTC" in out
    assert 'timezone = "UTC"' in (tmp_path / "my-brains" / "work" / "brain.toml").read_text()


def test_setup_records_past_sessions_and_says_how_to_stop(tmp_path, monkeypatch, capsys) -> None:
    from tests.test_sessions_record import claude_row, jsonl

    _world(tmp_path, monkeypatch)
    root = tmp_path / "claude-projects"
    jsonl(root / "-work" / "claude-first.jsonl", [
        claude_row("a1", "user", "setupcanary where do the seed trays go", 1_800_000_000, session="claude-first"),
        claude_row("a2", "assistant", "On the north bench, out of the wind.", 1_800_000_060, session="claude-first"),
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(root))
    assert main(_args(tmp_path, "--yes")) == 0
    out = capsys.readouterr().out
    instance = tmp_path / "my-brains" / "work"
    assert f"Past sessions: 1 recorded (2 messages from claude), in {instance / 'sessions.sqlite3'}." in out
    assert "record = false under [sessions] in brain.toml" in out and "brain sessions forget <session id>" in out
    assert (instance / "sessions.sqlite3").is_file()
    assert "[sessions]" not in (instance / "brain.toml").read_text()


def test_setup_no_sessions_starts_with_recording_off(tmp_path, monkeypatch, capsys) -> None:
    from tests.test_sessions_record import claude_row, jsonl

    _world(tmp_path, monkeypatch)
    root = tmp_path / "claude-projects"
    jsonl(root / "-work" / "claude-first.jsonl", [
        claude_row("a1", "user", "setupcanary where do the seed trays go", 1_800_000_000, session="claude-first"),
    ])
    monkeypatch.setenv("CLAUDE_PROJECTS_ROOT", str(root))
    assert main(_args(tmp_path, "--yes", "--no-sessions")) == 0
    out = capsys.readouterr().out
    instance = tmp_path / "my-brains" / "work"
    assert "Past sessions: not recorded." in out and "record = true under [sessions]" in out
    assert (instance / "brain.toml").read_text().endswith("\n[sessions]\nrecord = false\n")
    assert not (instance / "sessions.sqlite3").exists()
    # Run again without the flag: the existing brain's choice stays.
    assert main(_args(tmp_path, "--yes")) == 0
    assert "Past sessions: not recorded." in capsys.readouterr().out
    assert not (instance / "sessions.sqlite3").exists()
