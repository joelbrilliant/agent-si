from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import make_packet, seed, source_op, upsert_op, version_banner


def test_console_script_is_installed() -> None:
    result = subprocess.run(
        ["brain", "--version"], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0
    assert result.stdout.strip() == version_banner()
    assert shutil.which("brain")


def _run(args, instance: Path, packet=None, env=None):
    command = ["brain", *args, "--instance", str(instance)]
    return subprocess.run(
        command,
        input=json.dumps(packet) if packet is not None else None,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


@pytest.mark.parametrize(
    "builder,code",
    [
        (
            lambda: make_packet("tester", source_op("src-cli-ok", "CLI applied source text.")),
            0,
        ),
        (
            lambda: make_packet(
                "tester",
                source_op("src-cli-bad", "Dana asked whether the launch review can move."),
                upsert_op(
                    "upsert_record",
                    {
                        "id": "rec-cli-bad",
                        "kind": "note",
                        "title": "Note",
                        "body": 'Dana wrote "the launch review is cancelled for good".',
                    },
                    "src-cli-bad",
                ),
            ),
            1,
        ),
        (
            lambda: make_packet(
                "tester",
                upsert_op(
                    "upsert_person",
                    {"id": "per-missing-cli", "display_name": "Missing"},
                    "src-note-anon",
                    base=1,
                ),
            ),
            3,
        ),
        (
            lambda: make_packet(
                "careful",
                upsert_op(
                    "upsert_action",
                    {
                        "id": "act-cli-hold",
                        "title": "Hold",
                        "status": "open",
                        "status_basis": "observed",
                        "project_id": "general",
                        "last_verified_at": "2026-10-01T00:00:00Z",
                    },
                    "src-note-anon",
                ),
            ),
            4,
        ),
    ],
)
def test_apply_exit_codes(instance, apply, builder, code) -> None:
    seed(apply)
    result = subprocess.run(
        ["brain", "apply", "--instance", str(instance), "-"],
        input=json.dumps(builder()),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == code


def test_unreadable_packet_exits_2_and_writes_nothing(instance, apply) -> None:
    seed(apply)
    from tests.conftest import ro_rows

    count = ro_rows(instance, "SELECT count(*) FROM write_log")[0][0]
    result = subprocess.run(
        ["brain", "apply", "--instance", str(instance), "-"],
        input="not-json",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert ro_rows(instance, "SELECT count(*) FROM write_log")[0][0] == count


def test_instance_comes_from_brain_instance_variable(instance, apply, monkeypatch) -> None:
    seed(apply)
    env = {**dict(**{k: v for k, v in __import__("os").environ.items()}), "BRAIN_INSTANCE": str(instance)}
    result = subprocess.run(
        ["brain", "apply", "-"],
        input=json.dumps(make_packet("tester", source_op("src-env", "From env instance."))),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0


def test_missing_instance_exits_2() -> None:
    env = {k: v for k, v in __import__("os").environ.items() if k != "BRAIN_INSTANCE"}
    result = subprocess.run(
        ["brain", "apply", "-"],
        input="{}",
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 2


def test_receipt_and_base_commands(instance, apply) -> None:
    seed(apply)
    packet = make_packet(
        "tester",
        upsert_op(
            "upsert_person",
            {"id": "per-cli", "display_name": "Cli Person"},
            "src-note-anon",
        ),
    )
    apply(packet)
    receipt = subprocess.run(
        ["brain", "receipt", packet["change_id"], "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    base = subprocess.run(
        ["brain", "base", "people", "per-cli", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert receipt.returncode == 0
    assert json.loads(receipt.stdout)["change_id"] == packet["change_id"]
    assert json.loads(base.stdout) == {"table": "people", "id": "per-cli", "base": 1}


def test_approve_and_refuse_commands(instance, apply) -> None:
    seed(apply)
    held = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-cli-app",
                    "title": "Approve me",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    approved = subprocess.run(
        ["brain", "approve", held.receipt["change_id"], "--as", "tester", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert approved.returncode == 0
    held2 = apply(
        make_packet(
            "careful",
            upsert_op(
                "upsert_action",
                {
                    "id": "act-cli-ref",
                    "title": "Refuse me",
                    "status": "open",
                    "status_basis": "observed",
                    "project_id": "general",
                    "last_verified_at": "2026-10-01T00:00:00Z",
                },
                "src-note-anon",
            ),
        )
    )
    refused = subprocess.run(
        [
            "brain",
            "refuse",
            held2.receipt["change_id"],
            "--as",
            "tester",
            "--note",
            "no",
            "--instance",
            str(instance),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["status"] == "rejected"


def test_check_command_reports_problems(instance) -> None:
    (instance / "PAUSED").write_text("{}")
    result = subprocess.run(
        ["brain", "check", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False


def test_migrate_command(instance) -> None:
    from brain.migrate import latest_version

    result = subprocess.run(
        ["brain", "migrate", "--instance", str(instance)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["from"] == payload["to"] == latest_version()
    assert "search_rebuild" not in payload

