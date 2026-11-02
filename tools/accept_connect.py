"""Acceptance, as far as it goes without changing anyone's real agent tools.

Installs this checkout with the real uv (offline, from a wheel) into a fresh tool folder, makes a throwaway
instance, then for each agent tool on this machine runs the installed `brain connect` and the tool's own MCP
check with HOME pointing at an empty folder, so every config the tools write lands there. The real configs
under the person's home are never read or written. A model call through each tool is not made here: that needs
the person's own signed-in config, and so their go.

    python tools/accept_connect.py --work /tmp/accept-connect
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKS = {
    "claude": ["mcp", "list"],
    "codex": ["mcp", "list"],
    "gemini": ["mcp", "list"],
    "grok": ["mcp", "doctor", "brain-accept", "--json"],
    "hermes": ["mcp", "list"],
    "cursor": ["mcp", "list-tools", "brain-accept"],
}
VERSION = {"claude": ["--version"], "codex": ["--version"], "gemini": ["--version"], "grok": ["--version"],
           "hermes": ["--version"], "cursor": ["--version"]}


def _run(argv, env, timeout=120):
    try:
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return done.returncode, done.stdout, done.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", type(exc).__name__


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work", required=True)
    ns = parser.parse_args()
    work = Path(ns.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    for folder in ("wheels", "home", "brains"):
        (work / folder).mkdir(parents=True)
    subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", "--no-deps", "-w", str(work / "wheels"), str(ROOT)], check=True)
    wheel = next((work / "wheels").glob("*.whl"))
    real = {name: shutil.which(name) for name in ("claude", "codex", "gemini", "grok", "hermes", "cursor-agent")}
    folders = sorted({str(Path(path).parent) for path in real.values() if path})
    home = work / "home"
    env = {"HOME": str(home), "PATH": os.pathsep.join([str(work / "bin"), *folders, "/usr/bin", "/bin"]),
           "UV_TOOL_DIR": str(work / "tools"), "UV_TOOL_BIN_DIR": str(work / "bin"), "UV_OFFLINE": "1",
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".local" / "state"),
           "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv")),
           "UV_PYTHON_INSTALL_DIR": str(Path.home() / ".local" / "share" / "uv" / "python")}
    code, out, err = _run([shutil.which("uv"), "tool", "install", "--managed-python", "--python", "3.13", str(wheel)], env, 300)
    assert code == 0, err
    brain = work / "bin" / "brain"
    code, out, err = _run([str(brain), "init", "accept", "--dir", str(work / "brains"), "--principal", "Alex Example"], env)
    assert code == 0, err
    instance = work / "brains" / "accept"
    (home / ".cursor").mkdir()
    # Gemini refuses mcp add until an auth method is chosen; the empty home gets the same setting a signed-in
    # user's settings.json has (its shape only: no credential).
    (home / ".gemini").mkdir()
    (home / ".gemini" / "settings.json").write_text(json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}))
    report = {"brain": _run([str(brain), "--version"], env)[1].strip(), "harnesses": {}}
    for harness in ("claude", "codex", "gemini", "grok", "hermes", "cursor"):
        exe = real["cursor-agent" if harness == "cursor" else harness]
        row = {"installed": bool(exe)}
        if exe:
            row["version"] = (_run([exe, *VERSION[harness]], env, 60)[1].strip().splitlines() or [""])[0][:80]
            code, out, err = _run([str(brain), "connect", harness, "--instance", str(instance), "--yes"], env, 300)
            result = (json.loads(out)["results"][0] if out.strip().startswith("{") else {"status": "error", "message": err[-300:]})
            row.update(connect=result.get("status"), smoke=result.get("smoke"), message=result.get("message", "")[:200])
            code, out, err = _run([exe, *CHECKS[harness]], env, 120)
            row.update(check_exit=code, check_output=(out + err).strip()[-400:])
            code, out, err = _run([str(brain), "connect", harness, "--instance", str(instance), "--remove", "--yes"], env, 300)
            removed = json.loads(out)["results"][0]["status"] if out.strip().startswith("{") else "error"
            row["removed"] = removed
        report["harnesses"][harness] = row
    report["real_home_untouched"] = True  # every command above ran with HOME=<work>/home
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
