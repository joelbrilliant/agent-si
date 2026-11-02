from __future__ import annotations

from pathlib import Path

import pytest

from brain.config import ConfigError, load_config
from brain.init import apply_config, init_instance
from tests.conftest import TEST_CONFIG, config_with, ro_rows


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "brain.toml"
    path.write_text(text)
    return path


def test_template_config_is_valid(tmp_path: Path) -> None:
    from brain.init import _render_template

    with_principal = _render_template("demo", "UTC", "Morgan Vale")
    without = _render_template("demo", "UTC", None)
    load_config(_write(tmp_path, with_principal))
    other = tmp_path / "other.toml"
    other.write_text(without)
    load_config(other)


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(_write(tmp_path, "extra = 1\n" + TEST_CONFIG))


def test_agent_named_brain_is_rejected(tmp_path: Path) -> None:
    text = TEST_CONFIG + "\n[agents.brain]\noperations = \"*\"\n"
    with pytest.raises(ConfigError, match="reserved"):
        load_config(_write(tmp_path, text))


def test_unknown_operation_is_rejected(tmp_path: Path) -> None:
    text = config_with(
        agents={"tester": {"operations": ["explode"]}}
    )
    with pytest.raises(ConfigError, match="unknown operation"):
        load_config(_write(tmp_path, text))


def test_upsert_fact_key_cannot_be_granted(tmp_path: Path) -> None:
    text = config_with(
        agents={"tester": {"operations": ["upsert_fact_key"]}}
    )
    with pytest.raises(ConfigError, match="upsert_fact_key"):
        load_config(_write(tmp_path, text))


def test_approval_required_needs_existing_approvers(tmp_path: Path) -> None:
    text = config_with(
        agents={"careful": {"operations": "*", "approval": "required", "approvers": []}}
    )
    with pytest.raises(ConfigError, match="approvers"):
        load_config(_write(tmp_path, text))


def test_a_collector_must_be_a_table(tmp_path: Path) -> None:
    text = TEST_CONFIG.replace("[collectors]\n", "[collectors]\nfoo = 1\n")
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(_write(tmp_path, text))


def test_timezone_must_be_known(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, config_with(timezone="Mars/Olympus")))
    load_config(_write(tmp_path, config_with(timezone="UTC")))


def test_principal_names_must_be_single_words(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="single words"):
        load_config(
            _write(
                tmp_path,
                config_with(
                    principal={
                        "person_id": "per-jade",
                        "display_name": "Jade",
                        "names": ["Jade Example"],
                    }
                ),
            )
        )


def test_workstream_ids_and_fact_keys_are_unique(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not unique"):
        load_config(
            _write(
                tmp_path,
                config_with(
                    workstreams=[
                        {"id": "general", "title": "A"},
                        {"id": "general", "title": "B"},
                    ]
                ),
            )
        )
    with pytest.raises(ConfigError, match="not unique"):
        load_config(
            _write(
                tmp_path,
                config_with(
                    fact_keys=[
                        {"key": "demo.region", "value_type": "text"},
                        {"key": "demo.region", "value_type": "int"},
                    ]
                ),
            )
        )


def test_enum_fact_key_needs_allowed_values(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="allowed_values"):
        load_config(
            _write(
                tmp_path,
                config_with(fact_keys=[{"key": "demo.region", "value_type": "enum"}]),
            )
        )


def test_apply_config_adds_a_new_workstream_and_is_a_noop_when_unchanged(tmp_path: Path) -> None:
    instance = Path(init_instance("demo", tmp_path, config_text=TEST_CONFIG)["instance"])
    first = apply_config(instance)
    assert first is None
    text = (instance / "brain.toml").read_text()
    text += '\n[[workstreams]]\nid = "lab"\ntitle = "Lab"\ndescription = "Lab work."\nsort_order = 300\n'
    (instance / "brain.toml").write_text(text)
    added = apply_config(instance)
    assert added is not None
    assert added.receipt["status"] == "applied"
    assert ro_rows(instance, "SELECT id FROM projects WHERE id = 'lab'") == [("lab",)]
    again = apply_config(instance)
    assert again is None


def test_apply_config_never_removes_rows(tmp_path: Path) -> None:
    instance = Path(init_instance("demo", tmp_path, config_text=TEST_CONFIG)["instance"])
    slim = config_with(
        workstreams=[{"id": "general", "title": "General", "description": "Default workstream.", "sort_order": 100}]
    )
    (instance / "brain.toml").write_text(slim)
    outcome = apply_config(instance)
    assert outcome is None
    assert any("ops" in warning for warning in apply_config.last_warnings)
    assert ro_rows(instance, "SELECT 1 FROM projects WHERE id = 'ops'")


def test_instance_org_is_optional_and_checked(tmp_path: Path) -> None:
    assert load_config(_write(tmp_path, TEST_CONFIG)).org == ""
    named = TEST_CONFIG.replace('timezone = "UTC"\n', 'timezone = "UTC"\norg = "Example Co"\n', 1)
    assert load_config(_write(tmp_path, named)).org == "Example Co"
    bad_values = (
        '""', '"   "', '"' + "x" * 121 + '"', '"Example\\nCo"', '"Example\\tCo"',
        '"Example\\u2028Co"', '"ops@example.com"', "3",
    )
    for bad in bad_values:
        text = TEST_CONFIG.replace('timezone = "UTC"\n', f'timezone = "UTC"\norg = {bad}\n', 1)
        with pytest.raises(ConfigError, match=r"instance\.org"):
            load_config(_write(tmp_path, text))
