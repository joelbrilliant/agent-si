"""An agent's own note is written by that agent.

An agent in brain.toml may name the person row it writes as (person_id). An observation it adds without
author_person_id takes that person, so the brain knows who wrote it. An explicit null stays null, other kinds are left
alone, and re-sending a source without an author matches the author stored. Every name and text here is invented."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brain import config as config_mod
from brain import store
from brain.init import init_instance
from tests.conftest import render_config, ro_rows, source_op, upsert_op
from tests.test_truth import _apply, _err, _ok

AGENTS = {
    "tester": {"operations": "*", "author_principal": True},
    "scribe": {"operations": "*", "person_id": "per-scribe"},
    "plain": {"operations": "*"},
}


def note(sid: str, content: str = "Checked the vendor list; two entries are stale.", *, kind: str = "observation",
         **author) -> dict:
    op = source_op(sid, content, kind=kind)
    del op["row"]["author_person_id"]
    op["row"].update(author)
    return op


def author_of(instance: Path, sid: str):
    return ro_rows(instance, "SELECT author_person_id FROM sources WHERE id = ?", (sid,))[0][0]


@pytest.fixture
def inst(tmp_path: Path) -> Path:
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(agents=AGENTS))["instance"])
    _ok(_apply(instance, "tester",
               source_op("src-note-seed", "The scribe agent writes its own notes."),
               upsert_op("upsert_person", {"id": "per-scribe", "display_name": "Scribe"}, "src-note-seed",
                         locator="seed")))
    return instance


def test_an_observation_without_an_author_is_the_agents_own(inst: Path) -> None:
    _ok(_apply(inst, "scribe", note("src-obs-own")))
    assert author_of(inst, "src-obs-own") == "per-scribe"


def test_an_explicit_null_stays_null(inst: Path) -> None:
    _ok(_apply(inst, "scribe", note("src-obs-null", author_person_id=None)))
    assert author_of(inst, "src-obs-null") is None


def test_other_kinds_and_other_agents_are_left_alone(inst: Path) -> None:
    _ok(_apply(inst, "scribe", note("src-email-forwarded", "Dana: the pack is ready.", kind="email")))
    assert author_of(inst, "src-email-forwarded") is None
    _ok(_apply(inst, "plain", note("src-obs-plain")))
    assert author_of(inst, "src-obs-plain") is None


def test_resending_without_an_author_matches_the_author_stored(inst: Path) -> None:
    """A packet re-sent after a retry, or a daily mark saved under a fixed id, must not read as new content."""
    _ok(_apply(inst, "scribe", note("src-obs-again")))
    _ok(_apply(inst, "scribe", note("src-obs-again")))
    _ok(_apply(inst, "plain", note("src-obs-before", author_person_id=None)))
    _ok(_apply(inst, "scribe", note("src-obs-before")))
    assert author_of(inst, "src-obs-before") is None
    error = _err(_apply(inst, "scribe", note("src-obs-again", author_person_id=None)))
    assert "already stored with author_person_id per-scribe" in error and "leave author_person_id out" in error


def test_a_person_that_isnt_there_is_refused_with_the_fix(tmp_path: Path) -> None:
    """A new instance has no people, so init and check pass and check warns; the write names the fix."""
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(agents=AGENTS))["instance"])
    error = _err(_apply(instance, "scribe", note("src-obs-orphan")))
    assert "per-scribe" in error and "upsert_person" in error and "person_id" in error
    check = subprocess.run(["brain", "check", "--instance", str(instance)], capture_output=True, text=True, timeout=30)
    assert check.returncode == 0, check.stdout
    assert "agents.scribe.person_id per-scribe is not a person yet" in check.stderr
    assert store.check_instance(instance) == []


def test_brain_toml_takes_a_person_id_but_never_the_principals(tmp_path: Path) -> None:
    path = tmp_path / "brain.toml"
    path.write_text(render_config(agents={"scribe": {"operations": "*", "person_id": "scribe"}}), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match=r"agents\.scribe\.person_id is invalid"):
        config_mod.load_config(path)
    path.write_text(render_config(agents={"scribe": {"operations": "*", "person_id": "per-jade"}}), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match=r"agents\.scribe\.person_id is the principal's"):
        config_mod.load_config(path)
    path.write_text(render_config(agents=AGENTS), encoding="utf-8")
    loaded = config_mod.load_config(path)
    assert loaded.agents["scribe"].person_id == "per-scribe" and loaded.agents["plain"].person_id is None
