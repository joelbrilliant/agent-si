"""Acceptance of brain update and its measured target, with the real uv, offline, from local wheels.

Builds three wheels from this checkout (the current version, the next patch, and the patch after it with a
`brain --version` that fails on purpose), installs the first into a fresh tool folder, fills an instance to the
size asked for, then runs the installed `brain update --from-wheel` the good wheel and then the broken one, which rolls back to a wheel
packed from the installed copy. Nothing
outside the work folder is touched: the tool, config and state folders all point into it.

    python tools/accept_update.py --work /tmp/accept --size-mb 200
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _copy(target: Path, version: str, broken: bool = False) -> Path:
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(".git", "*.egg-info", "__pycache__", ".scratch", "tests",
                                                                "bench", "docs"))
    # pyproject.toml reads its version from brain.__version__, so this is the one place the release is named.
    path = target / "src" / "brain" / "__init__.py"
    path.write_text(re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"', path.read_text(), count=1, flags=re.M))
    if broken:
        cli = target / "src" / "brain" / "cli.py"
        text = cli.read_text().replace('    argv = list(sys.argv[1:] if argv is None else argv)\n',
                                       '    argv = list(sys.argv[1:] if argv is None else argv)\n'
                                       '    if argv[:1] == ["--version"]:\n        return 3  # deliberately broken\n', 1)
        cli.write_text(text)
    return target


def _wheel(source: Path, out: Path) -> Path:
    subprocess.run(["uv", "build", "--wheel", "-q", "--out-dir", str(out), str(source)], check=True)
    return max(out.glob("*.whl"), key=lambda path: path.stat().st_mtime)


def _fill(instance: Path, megabytes: int) -> int:
    """Sources of about 1 MiB each, applied through the engine, until the store is the size asked for."""
    sys.path.insert(0, str(ROOT))
    from brain.applier import Applier
    from tests.conftest import make_packet

    count, block = 0, ("Example Co handbook paragraph. " * 34000)[:1_000_000]
    while (instance / "brain.sqlite3").stat().st_size < megabytes * 1024 * 1024:
        rows = [{"op": "add_source", "row": {"id": f"src-fill-{count + i}", "kind": "document", "title": f"Fill {count + i}",
                                             "content": f"{count + i} {block}", "source_date": "2026-10-09"}} for i in range(4)]
        writer = Applier(instance)
        try:
            assert writer.apply(json.dumps(make_packet("tester", *rows)).encode()).exit_code == 0
        finally:
            writer.close()
        count += 4
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    parser.add_argument("--size-mb", type=int, default=5)
    ns = parser.parse_args()
    work = Path(ns.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    (work / "wheels").mkdir(parents=True)
    current = re.search(r'^__version__ = "([^"]+)"', (ROOT / "src" / "brain" / "__init__.py").read_text(), re.M).group(1)
    major, minor, patch = (int(part) for part in current.split(".")[:3])
    good, bad = f"{major}.{minor}.{patch + 1}", f"{major}.{minor}.{patch + 2}"
    wheels = {}
    for version, broken in ((current, False), (good, False), (bad, True)):
        out = work / "wheels" / version
        out.mkdir()
        wheels[version] = _wheel(_copy(work / "src" / version, version, broken), out)
    env = {**os.environ, "UV_TOOL_DIR": str(work / "tools"), "UV_TOOL_BIN_DIR": str(work / "bin"), "UV_OFFLINE": "1",
           "XDG_CONFIG_HOME": str(work / "config"), "XDG_STATE_HOME": str(work / "state")}
    brain = work / "bin" / "brain"
    done = subprocess.run(["uv", "tool", "install", "--managed-python", "--python", "3.13", str(wheels[current])],
                          env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    sys.path.insert(0, str(ROOT))
    from tests.phase2a import custom_instance

    instance = custom_instance(work / "inst")
    filled = _fill(instance, ns.size_mb)
    size = (instance / "brain.sqlite3").stat().st_size
    results = {"current": current, "store_bytes": size, "fill_sources": filled}

    def run(*argv):
        started = time.monotonic()
        done = subprocess.run([str(brain), "update", "--instance", str(instance), *argv], env=env, capture_output=True,
                              text=True)
        receipt = json.loads(done.stdout) if done.stdout.strip().startswith("{") else {"stdout": done.stdout}
        return done.returncode, receipt, round(time.monotonic() - started, 1), done.stderr[-400:]

    code, receipt, seconds, err = run("--from-wheel", str(wheels[good]))
    version = subprocess.run([str(brain), "--version"], env=env, capture_output=True, text=True).stdout.strip()
    results["good"] = {"exit": code, "result": receipt.get("result"), "seconds": seconds, "version_after": version}
    code, receipt, seconds, err = run("--from-wheel", str(wheels[bad]))  # the wheel to go back to is packed from the installed copy
    version = subprocess.run([str(brain), "--version"], env=env, capture_output=True, text=True).stdout.strip()
    check = subprocess.run([str(brain), "check", "--instance", str(instance)], env=env, capture_output=True, text=True)
    results["broken"] = {"exit": code, "result": receipt.get("result"), "message": receipt.get("message"),
                         "seconds": seconds, "version_after": version, "check_after": check.returncode}
    print(json.dumps(results, indent=2))
    ok = (results["good"]["exit"] == 0 and results["good"]["result"] == "updated"
          and results["good"]["version_after"].startswith(f"agent-si {good} ")
          and results["broken"]["exit"] == 5 and results["broken"]["result"] == "rolled_back"
          and results["broken"]["version_after"].startswith(f"agent-si {good} ") and results["broken"]["check_after"] == 0)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
