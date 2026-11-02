from __future__ import annotations

import subprocess
from pathlib import Path


def test_no_instance_database_or_env_files_are_tracked() -> None:
    root = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True)
    except FileNotFoundError:
        return  # no git here, so nothing is tracked
    if result.returncode != 0:
        return
    forbidden = []
    for path in result.stdout.splitlines():
        name = Path(path).name
        if path.endswith((".sqlite3", ".sqlite")) or ".sqlite3-" in path or ".sqlite-" in path:
            forbidden.append(path)
        if name.endswith(".db") or ".db-" in name:
            forbidden.append(path)
        if name.startswith(".env"):
            forbidden.append(path)
        if name == "brain.toml" and path != "src/brain/templates/brain.toml.template":
            forbidden.append(path)
        if name in {"ROUTER.md", "PAUSED"}:
            forbidden.append(path)
        if path.startswith("instances/") or path.startswith("data/"):
            forbidden.append(path)
    assert forbidden == []


def test_gitignore_covers_instance_files() -> None:
    text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
    required = [
        "instances/",
        "data/",
        "*.sqlite3",
        "*.sqlite3-*",
        "*.sqlite",
        "*.sqlite-*",
        "*.db",
        "*.db-*",
        ".env",
        ".env*",
        "brain.toml",
        "ROUTER.md",
        "PAUSED",
    ]
    for line in required:
        assert line in text.splitlines()
    assert not any(line.startswith("!.env") for line in text.splitlines())
