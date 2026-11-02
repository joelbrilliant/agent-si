"""A machine for the update tests: its own config, state and tool folders, and a fake uv that installs a
`brain` shim running this checkout's engine under the version asked for. A shim can be told to break in one
way, so each failure brain update must roll back from is real. Nothing here touches the network, the real uv
or the real ~/.config and ~/.local/state."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from brain import registry

FAKE_UV = r'''#!PYTHON
import json, os, sys
from pathlib import Path
SHIM = SHIM_REPR
world = Path(WORLD_REPR)
args = sys.argv[1:]
with open(world / "uv-calls.jsonl", "a") as log:
    log.write(json.dumps(args) + "\n")
if args == ["--version"]:
    print("uv 0.11.6 (fake)")
    sys.exit(0)
if args == ["tool", "dir", "--bin"]:
    print(os.environ["UV_TOOL_BIN_DIR"])
    sys.exit(0)
if args == ["tool", "update-shell"]:
    sys.exit(0)
if args[:2] != ["tool", "install"]:
    sys.exit(2)
positional, skip = [], False
for arg in args[2:]:
    if skip:
        skip = False
    elif arg in ("--python", "--exclude-newer"):
        skip = True
    elif not arg.startswith("-"):
        positional.append(arg)
spec = positional[-1]
version = Path(spec).name.split("-")[1] if spec.endswith(".whl") else spec.split("==", 1)[1]
behaviours = json.loads((world / "behaviours.json").read_text()) if (world / "behaviours.json").exists() else {}
mode = next((m for key, m in behaviours.items() if key in spec), "good")
if mode == "uninstallable":
    print("error: no matching distribution", file=sys.stderr)
    sys.exit(1)
shim = Path(os.environ["UV_TOOL_BIN_DIR"]) / "brain"
shim.write_text(SHIM.replace("PYTHON", sys.executable).replace("VERSION_VALUE", version).replace("MODE_VALUE", mode))
shim.chmod(0o755)
'''

SHIM = r'''#!PYTHON
import sys
VERSION, MODE = "VERSION_VALUE", "MODE_VALUE"
args = sys.argv[1:]
if args[:1] == ["--version"]:
    if MODE == "broken_version":
        sys.exit(1)
    print(f"agent-brain {VERSION} (schema 6)")
    sys.exit(0)
if MODE == "broken_migrate" and args[:1] == ["migrate"]:
    from pathlib import Path
    db = Path(args[args.index("--instance") + 1]) / "brain.sqlite3"
    data = bytearray(db.read_bytes())
    data[100:4096] = b"\xff" * (4096 - 100)
    db.write_bytes(bytes(data))
    (db.parent / "brain.sqlite3-journal").write_bytes(b"\x00" * 512)
    print("migration 0007 failed", file=sys.stderr)
    sys.exit(5)
if MODE == "broken_check" and args[:1] == ["check"]:
    print("store check failed", file=sys.stderr)
    sys.exit(5)
if MODE == "broken_serve" and args[:1] == ["serve"]:
    print("not json")
    sys.exit(0)
from brain.cli import main
sys.exit(main(args))
'''


class World:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path / "machine"
        self.bin = self.root / "tools-bin"
        self.fakebin = self.root / "fakebin"
        for folder in (self.bin, self.fakebin, self.root / "config", self.root / "state"):
            folder.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(self.root / "config"))
        monkeypatch.setenv("XDG_STATE_HOME", str(self.root / "state"))
        monkeypatch.setenv("UV_TOOL_BIN_DIR", str(self.bin))
        monkeypatch.setenv("PATH", f"{self.fakebin}{os.pathsep}{os.environ.get('PATH', '')}")
        uv = self.fakebin / "uv"
        uv.write_text(FAKE_UV.replace("#!PYTHON", "#!" + sys.executable, 1).replace("SHIM_REPR", repr(SHIM))
                      .replace("WORLD_REPR", repr(str(self.root))))
        uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
        self.install("agent-si==0.8.0")

    def install(self, spec: str) -> None:
        import subprocess

        done = subprocess.run([str(self.fakebin / "uv"), "tool", "install", "--reinstall", spec], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        (self.root / "uv-calls.jsonl").unlink()

    def behave(self, **modes: str) -> None:
        """Map a substring of the install spec to how its shim breaks: good, broken_version, broken_migrate,
        broken_check, broken_serve or uninstallable."""
        (self.root / "behaviours.json").write_text(json.dumps(modes))

    def uv_calls(self) -> list[list[str]]:
        path = self.root / "uv-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wheel(self, version: str, folder: str = "wheels", name: str = "agent_brain") -> Path:
        path = self.root / folder / f"{name}-{version}-py3-none-any.whl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not read by the fake uv")
        return path

    def register(self, instance: Path) -> None:
        registry.add(instance)
