"""agent-si and agent-memory are not published on any package index, so a name on one is free for anyone to
register, and anything that installs or updates them by name would install whoever does. Nothing in the shipped
files may do that: every install takes a wheel the person holds, and the release page is where wheels come from.

The scan reads the source, tools, deploy files, docs, examples, README, AGENT-INSTALL.md and install.sh. The phase
specs and proofs under docs/ are internal records and are not shipped, so they are left out. `uv tool uninstall
agent-si` is fine: it removes a tool and fetches nothing."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAMES = r"agent[-_](?:si|memory|brain)"
FORBIDDEN = {
    "a pinned name": re.compile(rf"\b{NAMES}==", re.I),
    # The name is the package to install: it follows the command and its flags, and ends the argument.
    "an install by name": re.compile(
        r"(?:pip3?\s+install|pipx\s+install|uv\s+pip\s+install|uv\s+tool\s+install|uv\s+tool\s+upgrade|uvx|uv\s+tool\s+run)\b"
        rf"(?:\s+[^\s`;&|]+)*?\s+[\"']?{NAMES}(?:\[[^\]]*\])?[\"']?(?=\s*(?:$|[`;&|)#]|--?[a-z]))", re.I),
}
FOLDERS = ("src", "tools", "deploy", "docs", "examples", "bench")
FILES = ("README.md", "AGENT-INSTALL.md", "install.sh")
SKIP = re.compile(r"(?:^|/)(?:SPEC-|PROOF-)[^/]*$|\.egg-info/|__pycache__/|/results/")
SUFFIXES = {".py", ".md", ".sh", ".yml", ".yaml", ".toml", ".txt", ".json", ".example", ".template", ""}


def _files() -> list[Path]:
    found = [ROOT / name for name in FILES if (ROOT / name).is_file()]
    for folder in FOLDERS:
        found += [path for path in sorted((ROOT / folder).rglob("*"))
                  if path.is_file() and path.suffix in SUFFIXES and not SKIP.search(path.relative_to(ROOT).as_posix())]
    return found


def test_the_scan_sees_the_files_it_should() -> None:
    names = {path.relative_to(ROOT).as_posix() for path in _files()}
    for expected in ("README.md", "install.sh", "src/brain/update.py", "tools/bundle-install.sh", "docs/REFERENCE.md",
                     "deploy/hub/hub"):
        assert expected in names, expected
    assert not any("SPEC-" in name or "PROOF-" in name for name in names)


def test_no_shipped_file_installs_or_updates_agent_si_or_agent_memory_by_name() -> None:
    found = []
    for path in _files():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            for kind, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {kind}. Name the wheel instead: "
                                 "download it from the releases page and pass its path")
    assert not found, "\n".join(found)


@pytest.mark.parametrize("line", [
    "agent-si==1.2.3", "pip install agent-si", "pip install 'agent-memory[x]'", "uv tool install --reinstall agent-si",
    'uv tool install --managed-python --python 3.13 "agent-memory"', "uvx agent-si", "pipx install agent-brain",
    "uv tool upgrade agent-si",
])
def test_the_scan_catches_each_shape(line: str) -> None:
    assert any(pattern.search(line) for pattern in FORBIDDEN.values()), line


@pytest.mark.parametrize("line", [
    "uv tool uninstall agent-si", "uv tool install --reinstall PATH-TO-THE-WHEEL", 'uv tool install "$TMP/files/agent_si-1.0.0-py3-none-any.whl"',
    "python -m pip install -e '.[test]'", "pip install ./dist/agent_si-1.0.0-py3-none-any.whl", "agent-memory connect all",
    "uv tool install ~/Downloads/agent_memory-0.2.0-py3-none-any.whl",
])
def test_the_scan_leaves_wheels_and_uninstall_alone(line: str) -> None:
    assert not any(pattern.search(line) for pattern in FORBIDDEN.values()), line
