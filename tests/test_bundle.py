"""The one-file installer: tools/bundle.py builds it, tools/bundle-install.sh is its head.

The bundle is built from this checkout and holds one wheel, agent-si. It then runs in a temporary machine: HOME,
XDG_*, uv's folders and TMPDIR all in a temp folder, UV_OFFLINE=1, only fake agent tools on PATH, and no controlling
terminal. uv and its managed Python 3.13 are used from where they are installed, so the test is skipped without
them. Nothing here touches a real home, a real tool config, a real session folder or launchd.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import textwrap
from pathlib import Path

import pytest

from brain import __version__
from tests.test_connect import FAKE_HARNESS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "bundle-install.sh"
spec = importlib.util.spec_from_file_location("bundle_tool", ROOT / "tools" / "bundle.py")
bundle = importlib.util.module_from_spec(spec)
sys.modules["bundle_tool"] = bundle
spec.loader.exec_module(bundle)

# -- the machine ---------------------------------------------------------------------------------------------------


def _real_env() -> dict:
    """The environment before conftest pointed XDG_* and uv's folders at a temp machine: where uv really keeps Python."""
    return {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "UV_"))}


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        pytest.skip("uv isn't installed")
    done = subprocess.run([found, "python", "find", "--managed-python", "3.13"], capture_output=True, text=True, env=_real_env())
    if done.returncode != 0:
        pytest.skip("uv has no managed Python 3.13 installed, and the bundle runs offline")
    return found


class Machine:
    """A temporary machine for one run of the bundle."""

    def __init__(self, root: Path, uv: str, fakes=("claude", "codex")) -> None:
        self.root = root
        self.home = root / "home"
        self.bin = root / "uv-bin"
        self.tmp = root / "tmp"
        self.fakebin = root / "path"
        for folder in (self.home, self.tmp, self.fakebin, self.bin):
            folder.mkdir(parents=True, exist_ok=True)
        (self.fakebin / "uv").symlink_to(uv)
        for name in fakes:
            path = self.fakebin / name
            path.write_text(FAKE_HARNESS.replace("#!PYTHON", "#!" + sys.executable, 1))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        python_dir = subprocess.run([uv, "python", "dir"], capture_output=True, text=True, check=True, env=_real_env()).stdout.strip()
        self.env = {
            "HOME": str(self.home),
            "PATH": f"{self.fakebin}:/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(root / "xdg" / "config"),
            "XDG_STATE_HOME": str(root / "xdg" / "state"),
            "XDG_DATA_HOME": str(root / "xdg" / "data"),
            "UV_TOOL_DIR": str(root / "uv-tools"),
            "UV_TOOL_BIN_DIR": str(self.bin),
            "UV_CACHE_DIR": str(root / "uv-cache"),
            "UV_PYTHON_INSTALL_DIR": python_dir,  # read only: the Python the real uv already has
            "UV_OFFLINE": "1",
            "TMPDIR": str(self.tmp),
        }

    def run(self, file: Path, *args: str, **env) -> subprocess.CompletedProcess:
        # A new session has no controlling terminal, so the bundle can't open /dev/tty to ask anything.
        return subprocess.run(["sh", str(file), *args], env={**self.env, **env}, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, start_new_session=True, timeout=600)

    def tool(self, name: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.bin / name), *args], env=self.env, capture_output=True, text=True, timeout=120)

    def calls(self, name: str) -> list[list[str]]:
        path = self.home / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    _uv()
    root = tmp_path_factory.mktemp("bundle")
    out = root / "dist"
    done = subprocess.run([sys.executable, str(ROOT / "tools" / "bundle.py"), "--out", str(out)],
                          capture_output=True, text=True, timeout=600)
    if done.returncode != 0:
        if re.search(r"error sending request|dns error|Failed to fetch|offline|Could not resolve", done.stderr, re.I):
            pytest.skip(f"the engine wheel needs its build backend, which couldn't be fetched: {done.stderr[-200:]}")
        pytest.fail(f"bundle.py failed:\n{done.stderr}")
    return {"stdout": done.stdout, "file": out / f"install-agent-si-{__version__}.sh", "root": root}


def _unpack(file: Path) -> dict[str, bytes]:
    data = file.read_bytes()
    offset = int(re.search(rb"^HEADER_BYTES=(\d+)$", data, re.M)[1])
    with tarfile.open(fileobj=io.BytesIO(data[offset:]), mode="r:gz") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers()}


# -- the bundle ----------------------------------------------------------------------------------------------------


def test_bundle_py_prints_the_files_own_sha256_and_builds_the_same_bytes_from_the_same_wheels(built) -> None:
    file = built["file"]
    lines = dict(line.split(": ", 1) for line in built["stdout"].splitlines())
    assert lines["bundle"] == str(file.resolve()) and int(lines["size"].split()[0]) == file.stat().st_size
    assert lines["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert file.stat().st_mode & stat.S_IXUSR
    files = _unpack(file)
    assert sorted(files) == ["SHA256SUMS", f"agent_si-{__version__}-py3-none-any.whl"]
    for line in files["SHA256SUMS"].decode().splitlines():
        digest, name = line.split("  ")
        assert hashlib.sha256(files[name]).hexdigest() == digest
    wheels = {name: data for name, data in files.items() if name != "SHA256SUMS"}
    template = SCRIPT.read_text(encoding="utf-8")
    assert bundle.assemble(template, wheels) == file.read_bytes()  # the same wheels make the same bytes
    data = file.read_bytes()
    offset = int(re.search(rb"^HEADER_BYTES=(\d+)$", data, re.M)[1])
    assert data[:offset].endswith(b"\n__AGENT_SI_BUNDLE_PAYLOAD__\n")  # the header's length is the marker line's end


def test_the_bundle_is_also_written_under_the_stable_release_name_with_its_checksum(built) -> None:
    file = built["file"]
    stable = file.parent / "install-agent-si.sh"
    assert stable.read_bytes() == file.read_bytes() and stable.stat().st_mode & stat.S_IXUSR
    assert (file.parent / "SHA256SUMS").read_text() == f"{hashlib.sha256(stable.read_bytes()).hexdigest()}  install-agent-si.sh\n"
    lines = dict(line.split(": ", 1) for line in built["stdout"].splitlines())
    assert lines["stable name"].startswith(str(stable.resolve())) and lines["checksums"] == str((file.parent / "SHA256SUMS").resolve())


def test_piping_the_bundle_into_sh_says_the_real_command(built, tmp_path) -> None:
    done = subprocess.run(["sh"], input=built["file"].read_bytes(), capture_output=True, cwd=tmp_path, timeout=60,
                          env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert done.returncode == 1 and done.stdout == b""
    assert ("can't be piped into sh; download it first, then run it: curl -LO "
            "https://github.com/joelbrilliant/agent-si/releases/latest/download/install-agent-si.sh && sh install-agent-si.sh"
            ) in done.stderr.decode()


def _session(machine: Machine) -> None:
    """One invented Claude Code session, where Claude Code keeps them in this machine's home."""
    folder = machine.home / ".claude" / "projects" / "-work-garden"
    folder.mkdir(parents=True)
    rows = [
        {"type": "user", "uuid": "u1", "sessionId": "claude-garden", "cwd": "/work/garden",
         "timestamp": "2027-01-15T08:00:00.000Z",
         "message": {"role": "user", "content": "bundlecanary where do the seed trays go"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "claude-garden", "cwd": "/work/garden",
         "timestamp": "2027-01-15T08:01:00.000Z",
         "message": {"role": "assistant", "content": "On the north bench, out of the wind."}},
    ]
    (folder / "claude-garden.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_the_bundle_installs_agent_si_makes_a_brain_connects_and_records_past_sessions(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv())
    (machine.home / ".cursor").mkdir()
    _session(machine)
    done = machine.run(built["file"], "--yes", "--principal", "Test Person", "--timezone", "UTC")
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert f"agent-si {__version__}: the wheel matches its checksum" in out
    target = (machine.home / "Brains" / "work").resolve()
    assert f"Brain work: created at {target}" in out and "Your name: Test Person" in out
    for harness in ("claude", "codex", "cursor"):
        assert f"  {harness}: written, smoke test pass" in out
    assert "agent-memory" not in out and "agent memory" not in out
    assert f"Past sessions: 1 recorded (2 messages from claude), in {target / 'sessions.sqlite3'}." in out
    assert "record = false under [sessions] in brain.toml" in out
    assert out.rstrip().endswith("Open a new terminal, start your agent and ask: read my brain brief.")
    # one search from the installed command finds what the session said
    found = machine.tool("brain", "read", "search", "--instance", str(target), "--args", '{"query": "bundlecanary"}')
    assert found.returncode == 0 and json.loads(found.stdout)["sessions"][0]["id"] == "claude:claude-garden#1"
    # the brain answers brief, from the installed command
    brief = machine.tool("brain", "read", "brief", "--instance", str(target))
    assert brief.returncode == 0 and "work" in brief.stdout
    # each tool is connected through its own command, with the brain alone
    claude = [call for call in machine.calls("harness-calls.jsonl") if call[0] == "claude" and call[1:3] == ["mcp", "add"]]
    assert len(claude) == 1 and claude[0][-4:] == [str(machine.bin / "brain"), "serve", "--instance", str(target)]
    assert "brain-work" in json.dumps(json.loads((machine.home / ".cursor" / "mcp.json").read_text()))
    assert sorted(path.name for path in machine.bin.iterdir()) == ["brain", "brain-fetch"]
    # nothing scheduled, nothing left behind
    assert not (machine.home / "Library" / "LaunchAgents").exists()
    assert not (machine.root / "xdg" / "config" / "systemd").exists()
    assert list(machine.tmp.iterdir()) == []


def test_a_second_run_upgrades_the_tools_and_keeps_the_brain(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv(), fakes=("claude",))
    first = machine.run(built["file"], "--yes", "--principal", "Test Person", "--timezone", "UTC")
    assert first.returncode == 0, first.stdout + first.stderr
    target = machine.home / "Brains" / "work"
    kept = {name: (target / name).read_bytes() for name in ("brain.toml", "ROUTER.md")}
    second = machine.run(built["file"], "--yes", "--principal", "Someone Else", "--timezone", "UTC")
    assert second.returncode == 0, second.stdout + second.stderr
    assert f"Brain work: kept the existing one at {target.resolve()}" in second.stdout
    assert "claude: unchanged, smoke test pass" in second.stdout
    assert {name: (target / name).read_bytes() for name in kept} == kept
    assert len(re.findall(r"\[\[instances\]\]", (tmp_path / "xdg" / "config" / "agent-si" / "instances.toml").read_text())) == 1
    assert machine.tool("brain", "--version").stdout.startswith(f"agent-si {__version__}")


def test_without_yes_and_without_a_terminal_it_carries_on_with_the_defaults_and_says_so(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv(), fakes=())
    done = machine.run(built["file"], "--timezone", "UTC")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "There's no terminal to ask in, so I used the defaults" in done.stdout
    assert "Tools: none found on this machine" in done.stdout
    assert Path(done.stdout.split(" at ")[1].splitlines()[0]).is_dir()


def test_no_connect_changes_no_agent_tool_but_still_records(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv())
    _session(machine)
    done = machine.run(built["file"], "--yes", "--principal", "Test Person", "--timezone", "UTC", "--no-connect")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "not connected (--no-connect)" in done.stdout and "Past sessions: 1 recorded" in done.stdout
    assert machine.calls("harness-calls.jsonl") == []


def test_no_sessions_starts_the_new_brain_with_recording_off(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv(), fakes=("claude",))
    _session(machine)
    done = machine.run(built["file"], "--yes", "--principal", "Test Person", "--timezone", "UTC", "--no-sessions")
    assert done.returncode == 0, done.stdout + done.stderr
    target = machine.home / "Brains" / "work"
    assert "Past sessions: not recorded." in done.stdout
    assert (target / "brain.toml").read_text().endswith("\n[sessions]\nrecord = false\n")
    assert not (target / "sessions.sqlite3").exists()


def test_an_agent_memory_from_an_earlier_install_is_named_and_left_alone(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv(), fakes=())
    earlier = machine.bin / "agent-memory"
    earlier.write_text("#!/bin/sh\necho should never be run >&2\nexit 9\n")
    earlier.chmod(0o755)
    done = machine.run(built["file"], "--yes", "--principal", "Test Person", "--timezone", "UTC")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "agent-memory is also installed. The brain now searches your past sessions itself" in done.stdout
    assert f"to remove it: {earlier} connect all --remove && " in done.stdout and "tool uninstall agent-memory" in done.stdout
    assert earlier.is_file() and "should never be run" not in done.stderr


def _write(path: Path, files: dict[str, bytes], sums: str | None = None) -> Path:
    template = SCRIPT.read_text(encoding="utf-8")
    body = {**files, "SHA256SUMS": (sums if sums is not None else bundle.sums(files)).encode()}
    path.write_bytes(bundle.header(template) + bundle.payload(body))
    return path


def test_a_corrupted_wheel_stops_the_install_before_anything_is_installed(built, tmp_path) -> None:
    files = {name: data for name, data in _unpack(built["file"]).items() if name != "SHA256SUMS"}
    good_sums = bundle.sums(files)
    bad = dict(files)
    wheel = f"agent_si-{__version__}-py3-none-any.whl"
    bad[wheel] = bad[wheel][:-1] + bytes([bad[wheel][-1] ^ 1])
    file = _write(tmp_path / "bad.sh", bad, sums=good_sums)
    machine = Machine(tmp_path / "machine", _uv())
    done = machine.run(file, "--yes", "--principal", "Test Person")
    assert done.returncode == 1
    assert done.stderr.startswith(f"install-agent-si: {wheel} failed its SHA-256 check (expected ")
    assert "so nothing was installed; download the bundle again" in done.stderr
    assert not list(machine.bin.iterdir()) and not (machine.root / "uv-tools").exists()
    assert not (machine.home / "Brains").exists()
    assert list(machine.tmp.iterdir()) == []


def test_a_cut_short_or_wrong_file_says_so(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv())
    data = built["file"].read_bytes()
    short = tmp_path / "short.sh"
    short.write_bytes(data[:-300])
    done = machine.run(short, "--yes")
    assert done.returncode == 1 and "this file is damaged or was cut short" in done.stderr
    assert "compare its SHA-256 with the one you were given" in done.stderr
    # a bundle without its wheel is refused the same way, and an empty SHA256SUMS can't smuggle one past the check
    files = {name: data for name, data in _unpack(built["file"]).items() if name.endswith(".whl")}
    one = _write(tmp_path / "one.sh", {"other-1.0-py3-none-any.whl": b"not the wheel"})
    assert "the bundle is missing its wheel" in machine.run(one, "--yes").stderr
    nested = _write(tmp_path / "nested.sh", files, sums="0" * 64 + "  ../escape.whl\n")
    assert "SHA256SUMS names ../escape.whl" in machine.run(nested, "--yes").stderr
    assert not list(machine.bin.iterdir())


def test_options_are_checked_and_a_foreign_command_is_never_replaced(built, tmp_path) -> None:
    machine = Machine(tmp_path, _uv())
    bad = machine.run(built["file"], "--bogus")
    assert bad.returncode == 2 and "install-agent-si: unknown option --bogus" in bad.stderr
    assert machine.run(built["file"], "--principal").returncode == 2
    assert machine.run(built["file"], "--help").stdout.startswith("usage: sh ")
    other = tmp_path / "other"
    other.mkdir()
    (other / "brain").write_text("#!/bin/sh\necho someone else's brain\n")
    (other / "brain").chmod(0o755)
    foreign = machine.run(built["file"], "--yes", PATH=f"{other}:{machine.env['PATH']}")
    assert foreign.returncode == 1 and f"a brain command is already installed at {other / 'brain'}" in foreign.stderr
    assert not list(machine.bin.iterdir())  # it stopped before installing anything
    refused = machine.run(built["file"], "--yes", "--name", "Bad Name", "--timezone", "UTC")
    assert refused.returncode == 2 and "lowercase letters, digits and dashes" in refused.stderr
    assert "agent-si is installed; run this file again with corrected options" in refused.stderr


# -- the script itself ---------------------------------------------------------------------------------------------


def _uv_lines(text: str, indent: str) -> list[str]:
    """The uv-finding lines between UV=... and the end of its install branch, without their indent."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.lstrip().startswith('UV="$(command -v uv'))
    end = next(i for i, line in enumerate(lines) if i > start and line.startswith(indent + "fi") and "UV" in lines[i - 1] + lines[i - 2] + lines[i - 3])
    return [line[len(indent):] if line.strip() else "" for line in lines[start:end + 1]]


def test_the_scripts_uv_step_is_install_shs_word_for_word() -> None:
    mine, theirs = SCRIPT.read_text(), (ROOT / "install.sh").read_text()
    for pin in re.findall(r'^(?:UV_VERSION|UV_MIN|PYTHON)="[^"]+"', theirs, re.M):
        assert pin in mine, pin
    assert _uv_lines(mine, "  ") == _uv_lines(theirs, "")
    assert "at_least()" in mine and re.search(r"^at_least\(\).*$", mine, re.M)[0] == re.search(r"^at_least\(\).*$", theirs, re.M)[0]


def test_the_script_is_posix_and_never_names_pypi() -> None:
    text = SCRIPT.read_text()
    assert text.startswith("#!/bin/sh\n") and text.endswith("__AGENT_SI_BUNDLE_PAYLOAD__\n")
    assert subprocess.run(["sh", "-n", str(SCRIPT)]).returncode == 0
    for bashism in ("[[", "local ", "function ", "$'", "echo -e", "pipefail"):
        assert bashism not in text, bashism
    assert "pypi" not in text.lower() and "agent-si==" not in text and "--from" not in text
    assert "--managed-python" in text and "--python" in text and "sha256sum" in text and "shasum -a 256" in text


def test_nothing_in_src_brain_gained_a_network_import_or_a_schedule_step() -> None:
    for name in ("setup.py",):
        text = (ROOT / "src" / "brain" / name).read_text()
        assert not re.search(r"^\s*(?:import|from)\s+(?:urllib|http|socket|ssl|requests)", text, re.M), name
