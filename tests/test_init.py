from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from brain import migrate, store
from brain.init import init_instance


def test_brain_init_demo_creates_a_working_instance_from_a_clean_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        ["brain", "init", "demo"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "created"
    demo = tmp_path / "demo"
    assert sorted(path.name for path in demo.iterdir()) == [
        "ROUTER.md",
        "brain.sqlite3",
        "brain.toml",
    ]
    assert (demo / "ROUTER.md").stat().st_size <= 2048
    con = store.connect_readonly(demo / "brain.sqlite3")
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrate.latest_version()
        assert store.integrity_problems(con) == []
        log = con.execute("SELECT agent, status FROM write_log").fetchall()
        assert log == [("brain", "applied")]
        assert con.execute("SELECT id, kind FROM projects").fetchall() == [("general", "workstream")]
        assert store.missing_guards(con) == []
    finally:
        con.close()
    import sqlite3

    plain = sqlite3.connect(demo / "brain.sqlite3")
    try:
        with pytest.raises(sqlite3.OperationalError, match="brain_applier_context"):
            plain.execute("INSERT INTO sources DEFAULT VALUES")
    finally:
        plain.close()
    check = subprocess.run(
        ["brain", "check", "--instance", str(demo)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert check.returncode == 0
    assert not list(tmp_path.glob(".demo.init-*"))


def test_init_is_idempotent_on_an_existing_instance(tmp_path: Path) -> None:
    subprocess.run(["brain", "init", "demo"], cwd=tmp_path, check=True, capture_output=True)
    demo = tmp_path / "demo"
    before = {path.name: path.read_bytes() for path in demo.iterdir()}
    result = subprocess.run(
        ["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "already initialised"
    after = {path.name: path.read_bytes() for path in demo.iterdir()}
    assert after == before


def test_init_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    demo.mkdir()
    stray = demo / "stray.txt"
    stray.write_text("keep")
    result = subprocess.run(
        ["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1
    assert stray.read_text() == "keep"
    assert not (demo / "brain.sqlite3").exists()


def test_init_refuses_a_file_at_the_target(tmp_path: Path) -> None:
    (tmp_path / "demo").write_text("nope")
    result = subprocess.run(
        ["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1


def test_init_uses_an_existing_empty_directory(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    result = subprocess.run(
        ["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    assert (tmp_path / "demo" / "brain.toml").is_file()


@pytest.mark.parametrize("name", ["Demo", "1demo", "a", "de mo", "../x", "x" * 41])
def test_init_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    result = subprocess.run(
        ["brain", "init", name], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 2


def test_init_refuses_inside_the_engine_repository(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "agent-brain"\n')
    result = subprocess.run(
        ["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 1


def test_init_failure_leaves_nothing_behind(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "check_instance", lambda path: ["forced problem"])
    with pytest.raises(Exception):
        init_instance("demo", tmp_path)
    assert not (tmp_path / "demo").exists()
    assert not list(tmp_path.glob(".demo.init-*"))


def test_init_with_principal_creates_the_principal_person(tmp_path: Path) -> None:
    result = subprocess.run(
        ["brain", "init", "demo", "--principal", "Morgan Vale"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    text = (tmp_path / "demo" / "brain.toml").read_text()
    assert 'person_id = "per-morgan-vale"' in text
    assert 'names = ["Morgan"]' in text


def test_init_without_principal_says_rule_two_is_off(tmp_path: Path) -> None:
    result = subprocess.run(
        ["brain", "init", "demo"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["principal"] is None
    assert "evidence-gate rule 2 is off" in result.stderr
