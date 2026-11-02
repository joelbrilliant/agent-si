"""A seeded fuzz of every reader. Mutated Markdown, text, Word, PDF
and transcript files must never raise out of brain-fetch: each is read or is one unreadable file, and a run over
a folder of them still writes its export and its collector_runs row. Every byte here is invented."""
from __future__ import annotations

import json
import random
import zlib
from pathlib import Path

import pytest

from brain_fetch import fileio, readers
from brain_fetch.cli import main as fetch_main
from tests import fetch_files as ff
from tests.conftest import context_connection, ro_rows
from tests.phase2a import custom_instance
from tests.test_brain_fetch import NAME, bf, go, inbox, write
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

DIRECT = 1000  # mutated files per reader in the direct pass
ROUNDS = 5  # folders of mutated files in the run pass
PER_ROUND = 28

BODY = (
    ff.para("Pricing", "Heading1")
    + ff.para("The Pro plan costs 49 AUD a seat.")
    + ff.table([["Plan", "Seats"], ["Pro", "25"]])
    + "<w:sdt><w:sdtContent>" + ff.para("Inside a content control") + "</w:sdtContent></w:sdt>"
)
XML = ff.docx_xml(BODY).encode()
SEEDS = {
    "text": b"# Notes\n\nThe Pro plan costs 49 AUD a seat. Caf\xc3\xa9 menu.\n",
    "pdf": ff.make_pdf([["Hello Example world", "second line"], ["Page two text"]]),
    "vtt": ff.VTT.encode(),
    "srt": ff.SRT.encode(),
    "ttext": b"Alex Example: first line\nKit Example: second line\n",
    "tjson": b'{"segments": [{"text": "Hi there", "speaker": "Alex Example", "start": 61.9}, {"text": "Bye", "start": "1:02:03"}]}',
}
SUFFIX = {"text": ".md", "docx": ".docx", "pdf": ".pdf", "vtt": ".vtt", "srt": ".srt", "ttext": ".transcript.txt", "tjson": ".transcript.json"}
DECLARATIONS = [b"UTF-9", b"UTF-16", b"nonsense", b"ISO-8859-1", b"cp037", b""]


def mutate(blob: bytes, rng: random.Random) -> bytes:
    data = bytearray(blob)
    for _ in range(rng.randint(1, 4)):
        if not data:
            data = bytearray(rng.randbytes(rng.randint(1, 8)))
        kind = rng.randrange(8)
        spot = rng.randrange(len(data))
        if kind == 0:
            for _ in range(rng.randint(1, 6)):
                data[rng.randrange(len(data))] = rng.randrange(256)
        elif kind == 1:
            del data[spot:]
        elif kind == 2:
            data[spot:spot] = rng.randbytes(rng.randint(1, 40))
        elif kind == 3:
            del data[spot : spot + rng.randint(1, 60)]
        elif kind == 4:
            data[spot:spot] = data[spot : spot + rng.randint(1, 80)] * rng.randint(1, 3)
        elif kind == 5:
            data[spot : spot + rng.randint(1, 30)] = b"\x00" * rng.randint(1, 30)
        elif kind == 6:
            data[spot] ^= 1 << rng.randrange(8)
        else:
            data = bytearray(data[::-1] if rng.random() < 0.1 else data[: max(1, spot)] + data[spot:])
    return bytes(data)


def mutate_xml(rng: random.Random) -> bytes:
    xml = bytearray(XML)
    if rng.random() < 0.3:
        xml = bytearray(xml.replace(b'encoding="UTF-8"', b'encoding="' + rng.choice(DECLARATIONS) + b'"'))
    return mutate(bytes(xml), rng)


def docx_sample(rng: random.Random) -> bytes:
    """Three kinds: a damaged zip (stored, so the damage reaches the XML), a damaged deflated zip, and a sound zip
    around damaged XML."""
    pick = rng.randrange(3)
    if pick == 0:
        return mutate(ff.make_docx(BODY, stored=True), rng)
    if pick == 1:
        return mutate(ff.make_docx(BODY), rng)
    return ff.make_docx(raw_xml=mutate_xml(rng), stored=rng.random() < 0.5)


def sample(tag: str, rng: random.Random) -> bytes:
    return docx_sample(rng) if tag == "docx" else mutate(SEEDS[tag], rng)


@pytest.fixture(autouse=True)
def short_limit(monkeypatch):
    monkeypatch.setattr(readers, "TIME_LIMIT", 5)


@pytest.mark.parametrize("tag", ["text", "docx", "pdf", "vtt", "srt", "ttext", "tjson"])
def test_no_reader_raises_on_a_mutated_file(tag: str, tmp_path: Path) -> None:
    rng = random.Random(f"2a-direct-{tag}")
    path = tmp_path / f"sample{SUFFIX[tag]}"
    outcomes = {"read": 0, "unreadable": 0}
    for _ in range(DIRECT):
        path.write_bytes(sample(tag, rng))
        outcome, got_tag, sha, _stat = fileio.read_file(str(path), tag)
        assert isinstance(outcome, (str, readers.ReadError)) and len(sha) == 64
        outcomes["unreadable" if isinstance(outcome, readers.ReadError) else "read"] += 1
    print(f"{tag}: {outcomes}")
    assert outcomes["read"] > 0 and outcomes["unreadable"] > 0  # the mutations reach both outcomes


def test_a_run_over_mutated_files_still_writes_its_export_and_run_row(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path / "inst", table={"live_folder": str(tmp_path / "docs"), "kinds": ["document", "meeting_transcript"]})
    folder = tmp_path / "docs"
    folder.mkdir()
    tags = list(SUFFIX)
    for round_number in range(ROUNDS):
        rng = random.Random(f"2a-run-{round_number}")
        for old in folder.iterdir():
            old.unlink()
        write(folder, "control.md", f"A sound file for round {round_number}.")
        for number in range(PER_ROUND):
            tag = tags[number % len(tags)]
            write(folder, f"f{number:02d}{SUFFIX[tag]}", sample(tag, rng))
        clock.at(f"2026-10-09T0{round_number}:00:00Z")
        exports, runs = len(inbox(instance)), ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0]
        code, result, err = go(instance, "--allow-mass-change")
        assert code == 0 and result["status"] in ("ok", "partial"), (round_number, result, err)
        assert "Traceback" not in err
        assert len(inbox(instance)) == exports + 1 and ro_rows(instance, "SELECT count(*) FROM collector_runs")[0][0] == runs + 1
        assert json.loads(inbox(instance)[-1].read_text())["format"] == "agent-brain.capture.v1"
        assert ro_rows(instance, "SELECT count(*) FROM sources WHERE external_ref = ?", (f"document:{NAME}/control.md",))[0][0] >= 1
        code, payload, err = bf("check", "--instance", str(instance))
        assert payload is not None and code in (0, 1) and "Traceback" not in err, err
        assert not (instance / "PAUSED").exists()
    assert zlib.crc32(b"") == 0  # (keeps the zlib import honest for the mutations above)
