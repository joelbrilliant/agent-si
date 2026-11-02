from __future__ import annotations

import contextlib
import io
import json
import os
import threading
from pathlib import Path

import pytest

from brain import evidence_gate, store
from brain.cli import main
from brain.collectors import REGISTRY
from brain.init import init_instance
from tests.collector_fixtures import (
    COLLECTORS,
    PRINCIPAL,
    SAMPLES,
    collect_file,
    collector_instance,
    gmail_doc,
)
from tests.conftest import attribution_refusal, file_sha, make_packet, render_config, ro_rows, source_op, upsert_op
from tests.extract_fixtures import candidate, extract_instance

LIMIT = 1024 * 1024
WRITER_CAP = 8 * LIMIT
TOO_BIG = "brain: candidate is larger than 1 MiB\n"
LONE = "brain: fixture holds a lone surrogate escape\n"
STRAIGHT_NAME = "Alex O'Example"
CURLY_NAME = "Alex O" + chr(0x2019) + "Example"
needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs os.mkfifo")


def _run(argv: list[str]) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _feed(fifo: Path, data: bytes | None, outcome: dict) -> threading.Thread:
    """Write data (or up to WRITER_CAP bytes of filler) into the FIFO on a thread."""

    def write() -> None:
        sent = 0
        try:
            with open(fifo, "wb", buffering=0) as out:
                if data is not None:
                    sent += out.write(data)
                else:
                    chunk = b"x" * 65536
                    while sent < WRITER_CAP:
                        sent += out.write(chunk)
        except BrokenPipeError:
            outcome["cut_off"] = True
        outcome["sent"] = sent

    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


@needs_fifo
def test_extract_refuses_an_endless_fifo(tmp_path: Path) -> None:
    instance = extract_instance(tmp_path / "inst")
    before = file_sha(instance / "brain.sqlite3")
    fifo = tmp_path / "endless.fifo"
    os.mkfifo(fifo)
    outcome: dict = {}
    writer = _feed(fifo, None, outcome)
    code, stdout, stderr = _run(
        ["extract", "--instance", str(instance), "--as", "scan", str(fifo)]
    )
    writer.join(timeout=60)
    assert not writer.is_alive()
    assert (code, stdout, stderr) == (2, "", TOO_BIG)
    assert outcome.get("cut_off") is True
    assert outcome["sent"] < WRITER_CAP
    assert file_sha(instance / "brain.sqlite3") == before


@needs_fifo
def test_extract_reads_a_fifo_under_the_limit(tmp_path: Path) -> None:
    data = json.dumps(candidate("decision_candidate")).encode()
    from_file = extract_instance(tmp_path / "file")
    path = tmp_path / "candidate.json"
    path.write_bytes(data)
    want_code, want_out, _ = _run(
        ["extract", "--instance", str(from_file), "--as", "scan", str(path)]
    )
    from_fifo = extract_instance(tmp_path / "fifo")
    fifo = tmp_path / "candidate.fifo"
    os.mkfifo(fifo)
    outcome: dict = {}
    writer = _feed(fifo, data, outcome)
    code, stdout, _ = _run(
        ["extract", "--instance", str(from_fifo), "--as", "scan", str(fifo)]
    )
    writer.join(timeout=60)
    assert not writer.is_alive()
    assert "cut_off" not in outcome
    assert code == want_code
    got, want = json.loads(stdout), json.loads(want_out)
    assert got["change_id"] == want["change_id"]
    assert got["receipt"]["status"] == want["receipt"]["status"]


_CASES = {
    "value-high": lambda text: text.replace('"fetched_at":"', '"fetched_at":"\\ud800', 1),
    "value-low": lambda text: text.replace('"fetched_at":"', '"fetched_at":"\\udc00', 1),
    "reversed-pair": lambda text: text.replace(
        '"fetched_at":"', '"fetched_at":"\\udc00\\ud800', 1
    ),
    "key": lambda text: '{"\\ud800":0,' + text[1:],
}


@pytest.fixture(scope="module")
def every_collector(tmp_path_factory) -> Path:
    return collector_instance(tmp_path_factory.mktemp("surrogate"), *COLLECTORS)


@pytest.mark.parametrize("case", sorted(_CASES))
def test_lone_surrogate_fixture_is_unreadable(
    every_collector: Path, tmp_path: Path, case: str
) -> None:
    instance = every_collector
    before = file_sha(instance / "brain.sqlite3")
    covered: set[str] = set()
    for name, (table, _agent) in COLLECTORS.items():
        type_name = table["type"]
        covered.add(type_name)
        text = _CASES[case](store.canonical_json(SAMPLES[type_name]()))
        assert "\\u" in text, (name, case)
        path = tmp_path / f"{name}.json"
        path.write_bytes(text.encode("utf-8"))
        code, payload, err = collect_file(instance, name, path)
        assert (code, payload, err) == (2, None, LONE), (name, case)
        assert file_sha(instance / "brain.sqlite3") == before
        assert ro_rows(instance, "SELECT count(*) FROM collector_runs") == [(0,)]
    assert covered == set(REGISTRY)


def test_lone_surrogate_in_a_mail_subject_is_unreadable(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path / "inst", "gmail-work")
    before = file_sha(instance / "brain.sqlite3")
    text = store.canonical_json(gmail_doc()).replace(
        '"Launch review"', '"Launch \\ud800 review"', 1
    )
    assert "\\ud800" in text
    path = tmp_path / "mail.json"
    path.write_bytes(text.encode("utf-8"))
    code, payload, err = collect_file(instance, "gmail-work", path)
    assert (code, payload, err) == (2, None, LONE)
    assert "Traceback" not in err
    assert file_sha(instance / "brain.sqlite3") == before


def test_paired_surrogate_escape_still_collects(tmp_path: Path) -> None:
    instance = collector_instance(tmp_path / "inst", "gmail-work")
    text = store.canonical_json(gmail_doc()).replace(
        '"Launch review"', '"Launch \\ud83d\\ude80 review"', 1
    )
    path = tmp_path / "mail.json"
    path.write_bytes(text.encode("utf-8"))
    code, payload, err = collect_file(instance, "gmail-work", path)
    assert code == 0, err
    assert payload["status"] == "ok"
    assert ro_rows(instance, "SELECT subject FROM threads") == [("Launch \U0001F680 review",)]


@pytest.mark.parametrize(
    ("display_name", "said"),
    [
        pytest.param(STRAIGHT_NAME, STRAIGHT_NAME, id="straight-name-straight-text"),
        pytest.param(STRAIGHT_NAME, CURLY_NAME, id="straight-name-curly-text"),
        pytest.param(CURLY_NAME, STRAIGHT_NAME, id="curly-name-straight-text"),
        pytest.param(CURLY_NAME, CURLY_NAME, id="curly-name-curly-text"),
    ],
)
def test_curly_apostrophe_display_name_is_caught(display_name: str, said: str) -> None:
    principal = evidence_gate.Principal("per-alex-example", display_name, ("Alex",))
    source = evidence_gate.CitedSource("src-note", "Notes from the weekly call.", None)
    sentence = f"{said}: we will drop the nightly backups."
    assert evidence_gate.check_text([sentence], "", source, principal) == [
        attribution_refusal(sentence, display_name)
    ]


def test_apply_rejects_unquoted_words_for_a_curly_display_name(tmp_path: Path) -> None:
    principal = {**PRINCIPAL, "display_name": CURLY_NAME}
    init_instance("demo", tmp_path, config_text=render_config(principal=principal))
    instance = tmp_path / "demo"
    packet = make_packet(
        "tester",
        source_op("src-curly", "Notes from the weekly call."),
        upsert_op(
            "upsert_record",
            {
                "id": "rec-curly",
                "kind": "note",
                "title": "Backups",
                "body": f"{CURLY_NAME}: we will drop the nightly backups.",
            },
            "src-curly",
        ),
        change_id="tester-curly-1",
    )
    path = tmp_path / "packet.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    code, stdout, _ = _run(["apply", "--instance", str(instance), str(path)])
    receipt = json.loads(stdout)
    assert code == 1
    assert receipt["status"] == "rejected"
    assert f"attributes words or a decision to {CURLY_NAME}" in receipt["error"]
    assert ro_rows(instance, "SELECT count(*) FROM records WHERE id = 'rec-curly'") == [(0,)]
