"""Brain-fetch, the documents folder, its readers, cursors, credentials and schedule. Every name and text here is invented."""
from __future__ import annotations

import ast
import contextlib
import fcntl
import importlib.metadata
import io
import json
import logging
import os
import shutil
import stat
import struct
import sys
import tomllib
import time
import zipfile
from pathlib import Path

import pytest

from brain import __version__, marks, store
from brain.cli import main as brain_main
from brain_fetch import check as check_mod
from brain_fetch import credentials, documents, fileio, readers, scan, schedule, state, transcripts
from brain_fetch.cli import main as fetch_main
from tests import fetch_files as ff
from tests.conftest import context_connection, ro_rows
from tests.phase2a import custom_instance, sources
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

NAME = "work-docs"
OLD = 1_791_000_000  # a fixed modified time, 2026-10-03


def world(tmp_path: Path, *, folder: Path | None = None, **kwargs) -> tuple[Path, Path]:
    folder = folder or tmp_path / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    table = {"live_folder": str(folder), **kwargs.pop("table", {})}
    return custom_instance(tmp_path / "inst", table=table, **kwargs), folder


def write(folder: Path, relative: str, content, *, mtime: int = OLD) -> Path:
    path = folder / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    os.utime(path, (mtime, mtime))
    return path


def bf(*argv: str) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch_main(list(argv))
    text = out.getvalue().strip()
    return code, (json.loads(text) if text.startswith("{") else None), err.getvalue()


def go(instance: Path, *flags: str) -> tuple[int, dict, str]:
    code, payload, err = bf("run", "--instance", str(instance), *flags)
    return code, payload["collectors"][0] if payload else {}, err


def inbox(instance: Path) -> list[Path]:
    return sorted((instance / "fetch" / "inbox" / NAME).glob("*.json"))


def last_export(instance: Path) -> dict:
    return json.loads(inbox(instance)[-1].read_text())


def cursor(instance: Path) -> dict:
    return json.loads((instance / "fetch" / f"{NAME}.state.json").read_text())


def cursor_bytes(instance: Path) -> bytes | None:
    path = instance / "fetch" / f"{NAME}.state.json"
    return path.read_bytes() if path.exists() else None


def refs(instance: Path) -> list[str]:
    return [row[0] for row in ro_rows(instance, "SELECT DISTINCT external_ref FROM sources WHERE id GLOB 'src-cap-*' ORDER BY external_ref")]


def scan_of(folder: Path, cursor=None, *, gate: bool = False, kinds=("document",)) -> scan.Result:
    return scan.scan(str(folder), NAME, cursor or {}, list(kinds), "UTC", gate)


# -- 66: the scan ----------------------------------------------------------------------------------


def test_scan_rules(tmp_path: Path, monkeypatch) -> None:
    folder = tmp_path / "docs"
    write(folder, "b.md", "Bee")
    write(folder, "a.txt", "Ay")
    write(folder, "sub/c.markdown", "Cee")
    write(folder, "Upper/D.MD", "Dee")
    write(folder, ".hidden.md", "no")
    write(folder, ".git/config.md", "no")
    write(folder, "~$lock.docx", "no")
    write(folder, "~$dir/inner.md", "no")
    write(folder, "pic.png", "png")
    write(folder, "notes.rtf", "rtf")
    write(folder, "tab\there.md", "control")
    (folder / "link.md").symlink_to(folder / "b.md")
    (folder / "linkdir").symlink_to(folder / "sub")
    nfd = "café.md"
    write(folder, nfd, "Nfd name")
    try:
        os.close(os.open(os.fsencode(str(folder)) + b"/bad\xff.md", os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError:
        pass  # this file system refuses invalid names (a Mac does); Linux accepts it and the scan skips it
    result = scan_of(folder)
    assert [item["ref"] for item in result.items] == sorted(
        [f"document:{NAME}/{name}" for name in ("Upper/D.MD", "a.txt", "b.md", "café.md", "sub/c.markdown")]
    )
    assert result.unsupported == 2  # pic.png and notes.rtf
    assert result.skipped >= 2 + 5 + 2  # hidden files, hidden dirs, ~$ names, links, unsupported, control name
    assert result.io_failure is False
    assert all(item["title"] == item["ref"].split("/", 1)[1] for item in result.items)
    # Over-size files are skipped.
    monkeypatch.setattr(scan, "MAX_BYTES", 1)
    big = scan_of(folder)
    assert [item["ref"] for item in big.items] == []
    monkeypatch.undo()
    # A directory deeper than 20 levels is not entered and counts once.
    deep = tmp_path / "deep"
    path = deep
    for level in range(1, 24):
        path = path / f"d{level}"
    write(path, "bottom.md", "too deep")
    write(deep / "d1" / "d2", "ok.md", "shallow")
    result = scan_of(deep)
    assert [item["ref"] for item in result.items] == [f"document:{NAME}/d1/d2/ok.md"]
    assert result.skipped == 1
    # An unlistable subdirectory is an I/O failure, not a skip.
    locked = tmp_path / "locked"
    write(locked, "sealed/secret.md", "hidden")
    write(locked, "open.md", "visible")
    (locked / "sealed").chmod(0)
    try:
        result = scan_of(locked)
    finally:
        (locked / "sealed").chmod(0o755)
    assert result.io_failure is True and [item["ref"] for item in result.items] == [f"document:{NAME}/open.md"]
    # The cap counts only files with a supported extension: 25,000 photos and 10 notes is fine.
    photos = tmp_path / "photos"
    photos.mkdir()
    for number in range(25_000):
        (photos / f"p{number}.png").write_bytes(b"")
    for number in range(10):
        write(photos, f"n{number}.md", f"note {number}")
    assert len(scan_of(photos).items) == 10
    monkeypatch.setattr(scan, "MAX_FILES", 5)
    with pytest.raises(scan.TooMany):
        scan_of(photos)


def test_unlistable_directory_means_no_removal(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    write(folder, "open.md", "visible text")
    write(folder, "sealed/secret.md", "hidden text")
    assert go(instance)[0] == 0
    assert len(refs(instance)) == 2
    (folder / "sealed").chmod(0)
    try:
        clock.at("2026-10-09T01:00:00Z")
        code, result, err = go(instance)
    finally:
        (folder / "sealed").chmod(0o755)
    assert code == 0 and result["status"] == "partial"
    assert last_export(instance)["complete"] is False
    assert len(sources(instance)) == 2  # nothing was removed
    clock.at("2026-10-09T02:00:00Z")
    assert go(instance)[1]["status"] == "ok"


def test_more_than_the_cap_is_an_error_export(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, folder = world(tmp_path)
    for number in range(4):
        write(folder, f"n{number}.md", f"note {number}")
    monkeypatch.setattr(scan, "MAX_FILES", 3)
    code, result, err = go(instance)
    assert code == 1 and result["status"] == "error"
    assert result["message"] == f"limit: the folder holds more than {scan.MAX_FILES} files"
    assert last_export(instance)["error_class"] == "limit" and sources(instance) == []


# -- 67 to 72: the readers ----------------------------------------------------------------------------


def test_text_reader(tmp_path: Path) -> None:
    assert readers.read_text(b"\xef\xbb\xbfHello") == "Hello"
    assert readers.read_text("café".encode("cp1252")) == "café"
    with pytest.raises(readers.ReadError, match="binary file"):
        readers.read_text(b"abc\x00def")
    with pytest.raises(readers.ReadError, match="binary file"):
        readers.read_text("utf sixteen".encode("utf-16"))
    # A 5 MiB UTF-8 file of accented text, cut at 4 MiB + 1 bytes in the middle of a character.
    path = tmp_path / "big.txt"
    path.write_bytes(("é" * 2_700_000).encode())
    assert path.stat().st_size > 5 * 1024 * 1024
    text, tag, _sha, _stat = fileio.read_file(str(path), "text")
    assert tag == "text" and text == "é" * (readers.TEXT_LIMIT // 2) and "Ã" not in text
    # A complete invalid sequence falls back to cp1252.
    assert readers.read_text(b"price \x80 50", True) == "price € 50"
    # A whole file that ends mid-character is not text.
    assert readers.read_text(b"abc\xc3", False) == "abc"
    assert readers.read_text(b"abc\xc3", True) == "abcÃ"  # cp1252 reads it


def test_word_reader() -> None:
    body = (
        ff.para("Pricing", "Heading1")
        + ff.para("Details", "heading 2")
        + ff.para(runs='<w:r><w:t>The Pro plan costs</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t>49 AUD</w:t></w:r>')
        + ff.para(runs='<w:r><w:t>Line one</w:t><w:br/><w:t>line two</w:t><w:cr/><w:t>line three</w:t></w:r>')
        + ff.table([["Plan", "Seats"], ["Pro", "25"]])
        + "<w:sdt><w:sdtPr/><w:sdtContent>" + ff.para("Inside a content control") + "</w:sdtContent></w:sdt>"
        + ff.para(runs='<w:ins><w:r><w:t>Inserted </w:t></w:r></w:ins><w:del><w:r><w:delText>deleted</w:delText></w:r></w:del><w:r><w:t>text</w:t></w:r>')
        + ff.para(runs='<w:r><w:t>Before box</w:t></w:r><w:r><w:pict><w:txbxContent><w:p><w:r><w:t>BOXED</w:t></w:r></w:p></w:txbxContent></w:pict></w:r>')
        + ff.para("")
    )
    extra = {"word/header1.xml": "<w:hdr>HEADER TEXT</w:hdr>", "word/footnotes.xml": "<w:f>NOTE TEXT</w:f>"}
    text = readers.read_docx(ff.make_docx(body, extra=extra))
    assert text == (
        "# Pricing\n\n## Details\n\nThe Pro plan costs\t49 AUD\n\nLine one\nline two\nline three\n\n"
        "Plan\tSeats\nPro\t25\n\nInside a content control\n\nInserted text\n\nBefore box"
    )
    assert "HEADER" not in text and "NOTE" not in text and "BOXED" not in text and "deleted" not in text


def test_word_reader_refuses_hostile_files(tmp_path: Path, monkeypatch) -> None:
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>'
        b'<w:document xmlns:w="' + ff.W.encode() + b'"><w:body><w:p><w:r><w:t>&lol2;</w:t></w:r></w:p></w:body></w:document>'
    )
    calls = []
    real = readers.ET.fromstring
    monkeypatch.setattr(readers.ET, "fromstring", lambda *a, **k: calls.append(1) or real(*a, **k))
    for label, raw in (("plain", bomb), ("comment", b"<?xml version='1.0'?><!--" + b"x" * 70_000 + b"-->" + bomb.split(b"?>", 1)[1])):
        with pytest.raises(readers.ReadError, match="DOCTYPE"):
            readers.read_docx(ff.make_docx(raw_xml=bomb if label == "plain" else raw))
    entity_only = b"<!ENTITY x 'y'>" + ff.docx_xml(ff.para("x")).encode()
    with pytest.raises(readers.ReadError, match="DOCTYPE"):
        readers.read_docx(ff.make_docx(raw_xml=entity_only))
    laughs = bomb.split(b"?>", 1)[1]
    for label, encoded in (
        ("utf-16", ('<?xml version="1.0" encoding="UTF-16"?>' + laughs.decode()).encode("utf-16")),
        ("utf-16le", ('<?xml version="1.0" encoding="UTF-16LE"?>' + laughs.decode()).encode("utf-16-le")),
        ("utf-16be", ('<?xml version="1.0" encoding="UTF-16BE"?>' + laughs.decode()).encode("utf-16-be")),
        ("bom-less utf-16", laughs.decode().encode("utf-16-le")),
        ("cp037", ('<?xml version="1.0" encoding="cp037"?>' + laughs.decode()).encode("cp037")),
        ("declared latin-1", b'<?xml version="1.0" encoding="ISO-8859-1"?>' + ff.docx_xml(ff.para("x")).split("?>", 1)[1].encode()),
        ("not utf-8 bytes", b"\xff\xfe" + ff.docx_xml(ff.para("x")).encode()[2:]),
    ):
        with pytest.raises(readers.ReadError, match="not UTF-8"):
            readers.read_docx(ff.make_docx(raw_xml=encoded)), label
    assert calls == []  # the parser was never called
    with pytest.raises(readers.ReadError, match="encrypted"):
        readers.read_docx(ff.make_docx(ff.para("x"), flag=1))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("other.xml", "<a/>")
    with pytest.raises(readers.ReadError, match="not a word file"):
        readers.read_docx(buffer.getvalue())
    with pytest.raises(readers.ReadError, match="corrupt"):
        readers.read_docx(b"PK\x03\x04 this is not a zip")
    with pytest.raises(readers.ReadError, match="corrupt"):
        readers.read_docx(ff.make_docx(raw_xml=b"<w:document><unclosed>"))
    monkeypatch.setattr(readers, "WORD_LIMIT", 100)
    with pytest.raises(readers.ReadError, match="too large"):
        readers.read_docx(ff.make_docx(ff.para("x" * 500)))
    monkeypatch.undo()
    # A reader that outlasts the time limit is unreadable: 'took too long'.
    monkeypatch.setattr(readers, "TIME_LIMIT", 0.3)
    monkeypatch.setattr(readers, "read_docx", lambda data: time.sleep(5))
    path = tmp_path / "slow.docx"
    path.write_bytes(ff.make_docx(ff.para("x")))
    started = time.perf_counter()
    outcome, tag, sha, _stat = fileio.read_file(str(path), "docx")
    assert isinstance(outcome, readers.ReadError) and str(outcome) == "took too long" and time.perf_counter() - started < 3
    # Deeply nested XML does not crash the reader.
    deep = ("<w:sdt><w:sdtContent>" * 3000) + ff.para("x") + ("</w:sdtContent></w:sdt>" * 3000)
    monkeypatch.undo()
    with pytest.raises(readers.ReadError):
        readers.read_docx(ff.make_docx(deep))


def test_pdf_reader(tmp_path: Path, clock: Clock, monkeypatch, capfd, caplog) -> None:
    pdf = ff.make_pdf([["Hello Example world", "second line"], ["Page two text"]])
    text, tag = readers.read_pdf(pdf)
    assert tag == "pdf" and text.startswith("--- page 1 ---\nHello Example world\nsecond line")
    assert "--- page 2 ---\nPage two text" in text
    assert readers.read_pdf(ff.make_pdf([[]])) == ("(no text layer: 1 pages)", "pdf")
    assert readers.read_pdf(ff.make_pdf([[], []])) == ("(no text layer: 2 pages)", "pdf")
    with pytest.raises(readers.ReadError, match="pdf is encrypted"):
        readers.read_pdf(ff.encrypted_pdf())
    for junk in (b"%PDF-1.4 not really", b"", pdf[: len(pdf) // 2]):
        with pytest.raises(readers.ReadError, match="pdf is unreadable"):
            readers.read_pdf(junk)
    caplog.set_level(logging.DEBUG)
    damaged = pdf.replace(b"startxref\n", b"startxref\n99999")  # a damaged xref is rebuilt, quietly
    assert readers.read_pdf(damaged)[1] == "pdf"
    assert [record for record in caplog.records if record.name.startswith("pypdf")] == []
    captured = capfd.readouterr()
    assert captured.err == "" and captured.out == ""
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "pypdf", None)
        assert readers.read_pdf(pdf) == (readers.NO_PDF, "pdf-none")
        assert readers.pypdf_available() is False
    # A PDF captured with pypdf, then a run without it, makes no new version and keeps the tag.
    instance, folder = world(tmp_path)
    write(folder, "spec.pdf", pdf)
    assert go(instance)[1]["status"] == "ok"
    assert cursor(instance)["files"]["spec.pdf"]["reader"] == "pdf"
    first = sources(instance)
    clock.at("2026-10-09T01:00:00Z")
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "pypdf", None)
        assert go(instance)[1]["files"]["unchanged"] == 1
        os.utime(folder / "spec.pdf", (OLD + 1, OLD + 1))  # touched: read again, still the same hash
        clock.at("2026-10-09T02:00:00Z")
        assert go(instance)[1]["status"] == "ok"
    assert sources(instance) == first and cursor(instance)["files"]["spec.pdf"]["reader"] == "pdf"
    # pdf-none becomes pdf and is read again when pypdf returns.
    other, other_folder = world(tmp_path / "second")
    write(other_folder, "spec.pdf", pdf)
    with monkeypatch.context() as patch:
        patch.setitem(sys.modules, "pypdf", None)
        assert go(other)[1]["status"] == "ok"
        assert cursor(other)["files"]["spec.pdf"]["reader"] == "pdf-none"
        assert "pdf text needs the pdf extra" in sources(other)[0][2]
    clock.at("2026-10-09T03:00:00Z")
    result = go(other)[1]
    assert result["files"]["changed"] == 1
    assert cursor(other)["files"]["spec.pdf"]["reader"] == "pdf"
    versions = sources(other)
    assert len(versions) == 2 and "Hello Example world" in versions[1][2] and versions[1][1] == versions[0][0]


def test_pypdf_pin_mirrors_pyproject() -> None:
    root = Path(__file__).resolve().parents[1]
    data = tomllib.loads((root / "pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert extras["pdf"] == [readers.PYPDF_PIN] == ["pypdf==6.19.0"]
    assert readers.PYPDF_PIN in extras["test"]
    try:
        installed = importlib.metadata.version("pypdf")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("pypdf is not installed")
    assert f"pypdf=={installed}" == readers.PYPDF_PIN


def test_transcript_readers() -> None:
    assert transcripts.read_vtt(ff.VTT) == (
        "[00:00:01] Alex Example: Welcome everyone to the review.\n[00:00:05] No speaker on this cue.\n"
        "[01:02:03] Kit Example: Speaker from a prefix."
    )
    assert transcripts.read_srt(ff.SRT) == "[00:00:02] Sam Example: First line continues here.\n[01:15:00] No speaker"
    assert transcripts.read_plain("  first line  \n\nsecond line\n") == "  first line\nsecond line"
    shapes = {
        "segments": {"segments": [{"text": "Hi there", "speaker": "Alex Example", "start": 61.9}, {"text": "Bye", "start": "1:02:03"}]},
        "transcript": {"transcript": [{"content": "Only text", "name": "Kit Example", "start_time": "05:07"}]},
        "list": [{"text": "No time"}, {"text": "Named", "speaker": "Sam Example"}],
    }
    assert transcripts.read_json(json.dumps(shapes["segments"])) == "[00:01:01] Alex Example: Hi there\n[01:02:03] Bye"
    assert transcripts.read_json(json.dumps(shapes["transcript"])) == "[00:05:07] Kit Example: Only text"
    assert transcripts.read_json(json.dumps(shapes["list"])) == "No time\nSam Example: Named"
    for bad in ('"just a string"', "{}", '{"segments": "no"}', '[{"x": 1}]', "[]", "not json"):
        with pytest.raises(readers.ReadError, match="unrecognised transcript JSON"):
            transcripts.read_json(bad)
    with pytest.raises(readers.ReadError, match="not a WebVTT file"):
        transcripts.read_vtt("00:00:01.000 --> 00:00:02.000\nNo header\n")
    assert transcripts.read_vtt("﻿WEBVTT - title\n\n00:00:00.999 --> 00:00:01.000\nFloored\n") == "[00:00:00] Floored"
    assert transcripts.read_transcript(ff.VTT.encode(), "vtt").startswith("[00:00:01] Alex Example")


def test_transcripts_run_once_redaction_exists(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path, table={"kinds": ["document", "meeting_transcript"]})
    write(folder, "2026-10-02 kickoff.vtt", ff.VTT)
    write(folder, "notes.md", "Plain notes.")
    code, result, err = go(instance)
    assert code == 0, err
    assert refs(instance) == [f"document:{NAME}/notes.md", f"meeting_transcript:{NAME}/2026-10-02 kickoff.vtt"]
    row = ro_rows(instance, "SELECT kind, source_date, content FROM sources WHERE external_ref LIKE 'meeting_transcript:%'")[0]
    assert row[0] == "meeting_transcript" and row[1] == "2026-10-02" and "[00:00:01] Alex Example: Welcome" in row[2]
    code, payload, err = bf("check", "--instance", str(instance))
    assert payload["collectors"][0]["transcripts"] == "on"
    # Transcripts need the kind in the collector's kinds as well.
    plain, plain_folder = world(tmp_path / "plain")
    write(plain_folder, "meeting.vtt", ff.VTT)
    assert go(plain)[1]["files"]["skipped"] == 1 and sources(plain) == []


def test_the_transcript_gate_reads_the_schema_and_the_table(tmp_path: Path) -> None:
    import sqlite3

    def store_at(name: str, version: int, table: bool) -> Path:
        folder = tmp_path / name
        folder.mkdir()
        con = sqlite3.connect(folder / "brain.sqlite3")
        if table:
            con.execute("CREATE TABLE redaction_gate (x TEXT) STRICT")
        con.execute(f"PRAGMA user_version = {version}")
        con.commit()
        con.close()
        return folder

    assert state.transcripts_allowed(store_at("a", 6, True)) is True
    assert state.transcripts_allowed(store_at("b", 5, False)) is False
    assert state.transcripts_allowed(store_at("c", 5, True)) is False  # a hand-made table does not open it
    assert state.transcripts_allowed(store_at("d", 6, False)) is False
    assert state.transcripts_allowed(tmp_path / "missing") is False


# -- 73 to 79: runs, cursors and the inbox -------------------------------------------------------------


def test_export_items_present_and_the_unchanged_run(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    for number in range(300):
        write(folder, f"docs/d{number:03d}.md", f"Document {number}. " + "word " * 40)
    code, result, err = go(instance)
    assert code == 0 and result["files"] == {"changed": 300, "unchanged": 0, "unreadable": 0, "skipped": 0}
    first = last_export(instance)
    assert len(first["items"]) == 300 and first["complete"] is True and first.get("present", []) == []
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["files"]["unchanged"] == 300 and result["exports"] == 1
    second = last_export(instance)
    path = inbox(instance)[-1]
    assert second["items"] == [] and len(second["present"]) == 300 and second["complete"] is True
    print(f"an unchanged run of 300 files writes {path.stat().st_size} bytes (an export with every text: {inbox(instance)[0].stat().st_size})")
    assert path.stat().st_size < 20 * 1024
    assert len(sources(instance)) == 300


def test_cursor_advances_only_on_a_complete_export_and_exit_zero(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    for number in range(30):
        write(folder, f"d{number:02d}.md", f"Document {number}.")
    assert go(instance)[0] == 0
    start = cursor_bytes(instance)
    # Exit 1 from collect: a mass change (25 of 30 edited) in one export.
    for number in range(25):
        write(folder, f"d{number:02d}.md", f"Document {number}. Edited.", mtime=OLD + 5)
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert code == 1 and result["status"] == "error" and result["cursor"] == "held"
    assert result["message"].startswith("mass change: 25 documents would change")
    assert cursor_bytes(instance) == start
    # Exit 5: the instance is paused.
    (instance / "PAUSED").write_text("investigating")
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance, "--allow-mass-change")
    assert code == 5 and result["status"] == "unavailable" and "paused" in result["message"]
    assert cursor_bytes(instance) == start
    (instance / "PAUSED").unlink()
    # complete: false (an unreadable subdirectory) with exit 0.
    write(folder, "sealed/x.md", "x")
    (folder / "sealed").chmod(0)
    try:
        clock.at("2026-10-09T03:00:00Z")
        code, result, err = go(instance, "--allow-mass-change")
    finally:
        (folder / "sealed").chmod(0o755)
    assert code == 0 and result["status"] == "partial" and result["cursor"] == "held"
    assert cursor_bytes(instance) == start
    # A partial run that is complete (a truncated document) advances.
    clock.at("2026-10-09T04:00:00Z")
    write(folder, "huge.md", "word " * 500_000, mtime=OLD + 9)
    code, result, err = go(instance, "--allow-mass-change")
    assert code == 0 and result["status"] == "partial" and result["cursor"] == "advanced"
    assert cursor_bytes(instance) != start and "huge.md" in cursor(instance)["files"]


def _break(path: Path):
    path.write_bytes(b"this is not a word file")


def test_content_failures_do_not_block_the_export(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, folder = world(tmp_path)
    write(folder, "a.txt", "A good file.")
    write(folder, "fixme.txt", "utf16 later".encode("utf-16"))
    write(folder, "enc.pdf", ff.encrypted_pdf())
    write(folder, "bad.docx", ff.make_docx(ff.para("Valid for now")))
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok" and result["files"]["unreadable"] == 2
    assert "2 files could not be read" in result["message"]
    assert last_export(instance)["complete"] is True
    assert refs(instance) == [f"document:{NAME}/a.txt", f"document:{NAME}/bad.docx"]
    # bad.docx turns corrupt: it already has a version, so its ref is listed as unchanged.
    _break(folder / "bad.docx")
    os.utime(folder / "bad.docx", (OLD + 3, OLD + 3))
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok" and result["files"]["unreadable"] == 3
    export = last_export(instance)
    assert export["items"] == [] and sorted(export["present"]) == [f"document:{NAME}/a.txt", f"document:{NAME}/bad.docx"]
    assert len(sources(instance)) == 2  # nothing was removed
    entries = cursor(instance)["files"]
    assert {name: (entry["reader"], entry.get("stored")) for name, entry in entries.items() if entry["reader"] == "failed"} == {
        "fixme.txt": ("failed", False), "enc.pdf": ("failed", False), "bad.docx": ("failed", True)}
    assert all(entries[name]["sha256"] for name in ("fixme.txt", "enc.pdf", "bad.docx"))
    # The next run opens none of them again, and the export is small.
    opened = []
    real = fileio.read_file
    monkeypatch.setattr(fileio, "read_file", lambda *a, **k: opened.append(a[0]) or real(*a, **k))
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and opened == [] and result["files"]["unreadable"] == 3 and result["files"]["unchanged"] == 1
    assert last_export(instance)["items"] == [] and inbox(instance)[-1].stat().st_size < 2000
    # check names them.
    code, payload, err = bf("check", "--instance", str(instance))
    named = payload["collectors"][0]["files"]["unreadable"]
    assert code == 1 and named == ["bad.docx", "enc.pdf", "fixme.txt"]
    assert payload["collectors"][0]["problems"][0].startswith("3 files could not be read")
    # Fixing a file makes the next run capture it.
    write(folder, "fixme.txt", "now it is utf eight", mtime=OLD + 7)
    clock.at("2026-10-09T03:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["files"]["changed"] == 1 and result["files"]["unreadable"] == 2
    assert f"document:{NAME}/fixme.txt" in refs(instance)
    assert cursor(instance)["files"]["fixme.txt"]["reader"] == "text"


def test_io_failures_hold_the_export(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    write(folder, "b.md", "Bravo text.")
    assert go(instance)[0] == 0
    start = cursor_bytes(instance)
    count = len(sources(instance))
    # A file that disappears between the listing and the read.
    write(folder, "c.md", "Charlie text.")
    real = fileio.read_file

    def vanish(path, tag):
        if path.endswith("c.md"):
            raise FileNotFoundError(path)
        return real(path, tag)

    clock.at("2026-10-09T01:00:00Z")
    monkeypatch.setattr(fileio, "read_file", vanish)
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "partial" and result["cursor"] == "held"
    assert last_export(instance)["complete"] is False and cursor_bytes(instance) == start
    assert len(sources(instance)) == count
    # A file whose size changes between the two fstat calls.
    monkeypatch.setattr(fileio, "read_file", real)
    calls = {"n": 0}
    real_fstat = os.fstat

    def moving(fd):
        info = real_fstat(fd)
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            values = list(info)
            values[stat.ST_SIZE] = info.st_size + 1
            return os.stat_result(values)
        return info

    monkeypatch.setattr(fileio.os, "fstat", moving)
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "partial" and result["cursor"] == "held"
    assert cursor_bytes(instance) == start and len(sources(instance)) == count
    # The next run retries.
    monkeypatch.setattr(fileio.os, "fstat", real_fstat)
    clock.at("2026-10-09T03:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok" and result["cursor"] == "advanced"
    assert "c.md" in cursor(instance)["files"] and len(sources(instance)) == count + 1
    # An unlistable directory is the third kind.
    write(folder, "sealed/x.md", "x")
    (folder / "sealed").chmod(0)
    try:
        clock.at("2026-10-09T04:00:00Z")
        code, result, err = go(instance)
    finally:
        (folder / "sealed").chmod(0o755)
    assert code == 0 and result["status"] == "partial" and "retries" in result["message"]


def test_cursor_ahead_of_the_store(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path / "one")
    write(folder, "a.md", "Alpha text.")
    write(folder, "b.md", "Bravo text.")
    assert go(instance)[0] == 0
    other, _same = world(tmp_path / "two", folder=folder)
    (other / "fetch").mkdir(exist_ok=True)
    shutil.copy(instance / "fetch" / f"{NAME}.state.json", other / "fetch" / f"{NAME}.state.json")
    # The second instance's cursor lists a ref its store doesn't hold; it reads the same folder.
    code, result, err = go(other)
    assert code == 1 and result["status"] == "error" and result["cursor"] == "held"
    assert result["message"] == f"document:{NAME}/a.md is listed as unchanged but the store has no version of it; rerun with --rescan"
    assert sources(other) == []
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(other, "--rescan")
    assert code == 0 and result["status"] == "ok" and result["files"]["changed"] == 2
    assert len(sources(other)) == 2
    clock.at("2026-10-09T02:00:00Z")
    assert go(other, "--rescan")[0] == 0 and len(sources(other)) == 2  # no duplicate sources


def test_touched_and_resaved_files_make_no_version(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, folder = world(tmp_path)
    note = write(folder, "note.md", "Same size text AAAA.")
    word = write(folder, "spec.docx", ff.make_docx(ff.para("Pricing text")))
    assert go(instance)[0] == 0
    start = cursor(instance)["files"]
    count = len(sources(instance))
    # utime: the ctime changes, the entry is updated, no item, no version.
    os.utime(note, (OLD + 100, OLD + 100))
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert result["files"]["unchanged"] == 2 and last_export(instance)["items"] == []
    moved = cursor(instance)["files"]["note.md"]
    assert moved["sha256"] == start["note.md"]["sha256"] and moved["ctime_ns"] != start["note.md"]["ctime_ns"]
    assert len(sources(instance)) == count
    # A Word file re-zipped with the same text: new bytes, same text, no new version.
    word.write_bytes(ff.make_docx(ff.para("Pricing text"), extra={"customXml/item1.xml": "<x/>"}))
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and len(sources(instance)) == count
    assert len(last_export(instance)["items"]) == 1
    # A file replaced by other text of equal size, with the old time restored, is seen as changed.
    before = os.stat(note)
    note.write_bytes(b"Same size text BBBB.")
    os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert os.stat(note).st_size == before.st_size and os.stat(note).st_mtime_ns == before.st_mtime_ns
    clock.at("2026-10-09T03:00:00Z")
    code, result, err = go(instance)
    assert result["files"]["changed"] == 1 and len(sources(instance)) == count + 1
    # A file changed during the read is not recorded in the cursor.
    write(folder, "late.md", "Late text.")
    real_fstat, seen = os.fstat, {"n": 0}

    def shifting(fd):
        info = real_fstat(fd)
        seen["n"] += 1
        if seen["n"] % 2 == 0:
            values = list(info)
            values[stat.ST_MTIME] = info.st_mtime + 1
            values[stat.ST_SIZE] = info.st_size
            return os.stat_result(values, {"st_mtime_ns": info.st_mtime_ns + 10**9, "st_ctime_ns": info.st_ctime_ns, "st_atime_ns": info.st_atime_ns})
        return info

    monkeypatch.setattr(fileio.os, "fstat", shifting)
    clock.at("2026-10-09T04:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "partial"
    assert "late.md" not in cursor(instance)["files"]


def test_exports_are_chunked(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, folder = world(tmp_path)
    for number in range(7):
        write(folder, f"d{number}.md", f"Document {number}.")
    monkeypatch.setattr(documents, "CHUNK_ITEMS", 3)
    writes = []
    real = state.write_cursor
    monkeypatch.setattr(state, "write_cursor", lambda *a, **k: writes.append(1) or real(*a, **k))
    code, result, err = go(instance)
    assert code == 0 and result["exports"] == 3 and result["status"] == "ok" and result["cursor"] == "advanced"
    docs = [json.loads(path.read_text()) for path in inbox(instance)]
    assert [len(doc["items"]) for doc in docs] == [3, 3, 1]
    assert [doc["complete"] for doc in docs] == [False, False, True]
    assert sorted(docs[2]["present"]) == sorted(item["ref"] for doc in docs[:2] for item in doc["items"])
    assert len(sources(instance)) == 7 and len(writes) == 1
    # By encoded size: the byte cap alone, always at least one item.
    other, other_folder = world(tmp_path / "bytes")
    for number in range(4):
        write(other_folder, f"d{number}.md", f"Document {number}. " + "x" * 200)
    monkeypatch.setattr(documents, "CHUNK_ITEMS", 5000)
    monkeypatch.setattr(documents, "CHUNK_BYTES", 1)
    code, result, err = go(other)
    assert code == 0 and result["exports"] == 4 and len(sources(other)) == 4


def test_a_chunked_run_is_guarded_as_one(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance, folder = world(tmp_path)
    for number in range(100):
        write(folder, f"d{number:03d}.md", f"Document {number}.")
    assert go(instance)[0] == 0
    monkeypatch.setattr(documents, "CHUNK_ITEMS", 20)
    for number in range(100):
        write(folder, f"d{number:03d}.md", f"Document {number}. Edited once.", mtime=OLD + 20)
    start, exports = cursor_bytes(instance), len(inbox(instance))
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert code == 1 and result["status"] == "error" and result["cursor"] == "held"
    assert result["message"] == "mass change: 100 documents would change; rerun with --allow-mass-change if this is expected"
    assert len(inbox(instance)) == exports and cursor_bytes(instance) == start  # nothing was written
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance, "--allow-mass-change")
    assert code == 0 and result["exports"] == 5 and len(sources(instance)) == 200
    # Four edits pass, even over several chunks.
    monkeypatch.setattr(documents, "CHUNK_ITEMS", 1)
    for number in range(4):
        write(folder, f"d{number:03d}.md", f"Document {number}. Edited twice.", mtime=OLD + 40)
    clock.at("2026-10-09T03:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["exports"] == 4 and len(sources(instance)) == 204


def test_inbox_names_modes_and_pruning(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    assert go(instance)[0] == 0
    assert go(instance)[0] == 0  # the same second
    names = [path.name for path in inbox(instance)]
    assert names == ["20261009T000000Z-01.json", "20261009T000000Z-02.json"]
    for path in inbox(instance):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for path in (instance / "fetch", instance / "fetch" / "inbox", instance / "fetch" / "inbox" / NAME):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path
    assert stat.S_IMODE((instance / "fetch" / f"{NAME}.state.json").stat().st_mode) == 0o600
    # More than 99 in one second is a refusal.
    folder_inbox = instance / "fetch" / "inbox" / NAME
    for number in range(3, 100):
        (folder_inbox / f"20261009T000000Z-{number:02d}.json").write_text("{}")
    code, result, err = go(instance)
    assert code == 1 and result["status"] == "error" and result["message"] == "inbox is full for this second"
    # Pruning: 14 days and a second goes, 13 days stays, foreign and newline names stay.
    for path in folder_inbox.iterdir():
        path.unlink()
    clock.at("2026-10-09T00:00:00Z")
    old = folder_inbox / "20260924T235959Z-01.json"  # 14 days and one second before the clock
    exact = folder_inbox / "20260925T000000Z-01.json"  # exactly 14 days
    recent = folder_inbox / "20260926T000000Z-01.json"  # 13 days
    foreign = folder_inbox / "notes.json"
    newline = folder_inbox / "20240101T000000Z-01.json\n"
    for path in (old, exact, recent, foreign, newline):
        path.write_text("{}")
    assert state.prune_inbox(folder_inbox) == 1
    assert not old.exists() and exact.exists() and recent.exists() and foreign.exists() and newline.exists()


def test_overlapping_runs_are_refused(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    (instance / "fetch").mkdir(exist_ok=True)
    handle = open(instance / "fetch" / f".lock-{NAME}", "a+b")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        code, result, err = go(instance)
    finally:
        handle.close()
    assert code == 5 and result["status"] == "busy"
    assert result["message"] == f"collector {NAME} is already running; try again when it finishes"
    assert err.strip() == f"brain-fetch: collector {NAME} is already running; try again when it finishes"
    assert sources(instance) == [] and not (instance / "fetch" / f"{NAME}.state.json").exists()
    code, result, err = go(instance)  # the lock was released
    assert code == 0 and result["status"] == "ok"
    code, result, err = go(instance)
    assert code == 0


# -- 81, 82: credentials ----------------------------------------------------------------------------------


def fake_security(tmp_path: Path, *, value: str | None = "xoxb-example-0001", code: int = 0, noise: bool = True) -> Path:  # release-check: allow (an invented token the test refuses)
    log = tmp_path / "security.args"
    script = tmp_path / "security"
    lines = ["#!/bin/sh", f'printf "%s\\n" "$@" > "{log}"']
    if noise:
        lines.append('echo "keychain chatter on stderr" >&2')
    if value is not None:
        lines.append(f'echo "{value}"')
        lines.append('echo "second line ignored"')
    lines.append(f"exit {code}")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    return script


@contextlib.contextmanager
def on_mac(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        yield


def test_credentials(tmp_path: Path, monkeypatch, capfd) -> None:
    assert credentials.SECRETS_DIR == Path("/run/secrets") and credentials.SECURITY == "/usr/bin/security"
    empty = tmp_path / "secrets"
    empty.mkdir()
    script = fake_security(tmp_path)
    with on_mac(monkeypatch):
        found = credentials.read_secret("demo", NAME, secrets_dir=empty, security=str(script))
    assert found == "xoxb-example-0001"  # release-check: allow (an invented token the test refuses)
    assert (tmp_path / "security.args").read_text().split("\n")[:-1] == [
        "find-generic-password", "-s", "agent-si/demo/work-docs", "-a", "work-docs", "-w"]
    captured = capfd.readouterr()
    assert "chatter" not in captured.err and "chatter" not in captured.out  # stderr was discarded
    # A failing `security` and an absent one give None.
    failing = fake_security(tmp_path, value=None, code=44)
    absent = tmp_path / "no-such-security"
    with on_mac(monkeypatch):
        assert credentials.read_secret("demo", NAME, secrets_dir=empty, security=str(failing)) is None
        assert credentials.read_secret("demo", NAME, secrets_dir=empty, security=str(absent)) is None
    assert credentials.read_secret("demo", NAME, secrets_dir=empty, security=str(script)) is None  # not a Mac: no keychain
    # A secrets directory: a good file wins over the keychain.
    good = empty / NAME
    good.write_text("  file-secret-value  \nsecond line\n")
    good.chmod(0o644)
    with on_mac(monkeypatch):
        assert credentials.read_secret("demo", NAME, secrets_dir=empty, security=str(script)) == "file-secret-value"
    # Group- or world-writable files are refused.
    good.chmod(0o664)
    with pytest.raises(credentials.SecretFileError, match="^secret file work-docs must not be writable by group or other$"):
        credentials.read_secret("demo", NAME, secrets_dir=empty)
    good.chmod(0o602)
    with pytest.raises(credentials.SecretFileError):
        credentials.read_secret("demo", NAME, secrets_dir=empty)
    # A link, an oversized file and a file owned by someone else are ignored.
    good.unlink()
    real = empty / "real"
    real.write_text("linked-secret")
    good.symlink_to(real)
    assert credentials.read_secret("demo", NAME, secrets_dir=empty) is None
    good.unlink()
    good.write_text("x" * 5000)
    assert credentials.read_secret("demo", NAME, secrets_dir=empty) is None
    good.write_text("owned-by-root")
    good.chmod(0o444)
    with monkeypatch.context() as patch:
        patch.setattr(credentials.os, "getuid", lambda: os.stat(good).st_uid + 1)
        assert credentials.read_secret("demo", NAME, secrets_dir=empty) is None
    # A root-owned 0444 file (a container's secret) is accepted: fake the owner.
    real_lstat = Path.lstat

    def as_root(self):
        info = real_lstat(self)
        if self == good:
            values = list(info)
            values[stat.ST_UID] = 0
            return os.stat_result(values)
        return info

    with monkeypatch.context() as patch:
        patch.setattr(credentials.os, "getuid", lambda: os.stat(good).st_uid + 1)
        patch.setattr(Path, "lstat", as_root)
        assert credentials.read_secret("demo", NAME, secrets_dir=empty) == "owned-by-root"
    assert credentials.store_command("demo", NAME) == "security add-generic-password -U -s agent-si/demo/work-docs -a work-docs -w"


def test_no_secret_in_config_store_exports_or_logs(tmp_path: Path, clock: Clock, monkeypatch, capfd) -> None:
    secret = "xoxb-example-0001"  # release-check: allow (an invented token the test refuses)
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    # A token-shaped value in the config is refused (test 39 covers the rules).
    text = (instance / "brain.toml").read_text().replace('origin = "work-docs"', f'origin = "work-docs"\nnote = "{secret}"')
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "brain.toml").write_text(text)
    code, out, err = bf("check", "--instance", str(probe))
    assert code == 2 and "looks like a credential" in err and secret not in err
    # A module that needs a credential: check, run and setup never show it.
    script = fake_security(tmp_path, value=secret)
    monkeypatch.setattr(check_mod, "NEEDS_CREDENTIAL", True)
    monkeypatch.setattr(credentials, "SECURITY", str(script))
    outputs = []
    with on_mac(monkeypatch):
        for argv in (["check", "--instance", str(instance)], ["run", "--instance", str(instance)], ["setup", "documents", "--instance", str(instance)]):
            outputs.append(bf(*argv))
    for code, payload, err in outputs:
        assert secret not in json.dumps(payload) and secret not in err
    assert outputs[0][1]["collectors"][0]["credential"] == "found"
    captured = capfd.readouterr()
    assert secret not in captured.out and secret not in captured.err
    for path in (instance / "fetch").rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes(), path
    con = context_connection(instance)
    try:
        dump = "\n".join(con.iterdump())
    finally:
        con.close()
    assert secret not in dump
    # A missing credential names where it looked, not a value.
    missing = fake_security(tmp_path, value=None, code=44)
    monkeypatch.setattr(credentials, "SECURITY", str(missing))
    with on_mac(monkeypatch):
        code, payload, err = bf("check", "--instance", str(instance))
    shown = payload["collectors"][0]["credential"]
    assert shown.startswith("missing (/run/secrets/work-docs, keychain agent-si/demo/work-docs)")


# -- 83 to 86: setup, check, schedule -----------------------------------------------------------------------


GUIDE = """brain-fetch setup: the documents folder for demo

No sign-in is needed. brain-fetch reads one folder on this machine and never changes it.

1. Add this to {dir}/brain.toml, with your own folder:

[agents.{agent}]
kind = "collector"
operations = ["add_source", "record_collector_run"]

[collectors.{name}]
type = "capture"
agent = "{agent}"
origin = "{name}"
kinds = ["document"]
live_folder = "/Users/example/Documents/Example Co"

2. brain apply-config --instance {dir}
3. brain-fetch check --instance {dir}
4. brain-fetch run --instance {dir} {name}
5. brain-fetch schedule --instance {dir}

It captures Markdown, text, Word and PDF files. PDF text needs pypdf: reinstall the agent-si wheel with uv's --with pypdf==6.19.0.
Meeting transcripts (.vtt, .srt, .transcript.txt, .transcript.json) are captured when kinds includes meeting_transcript. Remove one with brain redact.
"""


def raw_bf(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch_main(list(argv))
    return code, out.getvalue(), err.getvalue()


def test_setup_text(tmp_path: Path) -> None:
    instance, _folder = world(tmp_path)
    where = instance.resolve()
    code, out, err = raw_bf("setup", "documents", "--instance", str(instance))
    assert (code, err) == (0, "") and out == GUIDE.format(dir=where, name="work-docs", agent="workdocs")
    code, out, err = raw_bf("setup", "team-docs", "--instance", str(instance))
    assert code == 0 and out == GUIDE.format(dir=where, name="team-docs", agent="teamdocs")
    code, out, err = raw_bf("setup", "x", "--instance", str(instance))
    assert (code, out) == (2, "") and err.strip() == "brain-fetch: x is not a valid collector name"
    code, out, err = raw_bf("setup", "--instance", str(instance))
    assert code == 2 and out == "" and err.startswith("brain-fetch: ")
    for text in (GUIDE,):
        assert "token" not in text.lower() and "secret" not in text.lower()


def test_check_output(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    code, payload, err = bf("check", "--instance", str(instance))
    # A fresh collector: an empty folder is fine; nothing has run.
    assert code == 0 and payload["ok"] is True and payload["instance"] == str(instance)
    item = payload["collectors"][0]
    assert set(item) == {"collector", "type", "folder", "files", "pdf_text", "transcripts", "credential", "mark_key", "cursor",
                         "last_run", "intake", "problems"}
    assert item["folder"] == {"set": True, "exists": True, "readable": True}
    assert item["files"] == {"documents": 0, "transcripts": 0, "unsupported": 0, "unreadable": [], "io_failed": [], "skipped": 0, "chars": 0, "no_text": 0, "by_type": {}}
    assert (item["collector"], item["type"], item["credential"], item["mark_key"]) == (NAME, "capture", "not needed", "absent")
    assert item["cursor"] == {"present": False, "files": 0} and item["last_run"] is None and item["intake"] is None
    assert item["pdf_text"] is True and item["problems"] == []
    # Healthy, with files of several types.
    write(folder, "a.md", "Alpha text.")
    write(folder, "b.txt", "Bravo text.")
    write(folder, "c.pdf", ff.make_pdf([[]]))
    write(folder, "d.png", "png")
    assert go(instance)[0] == 0
    code, payload, err = bf("check", "--instance", str(instance))
    item = payload["collectors"][0]
    assert code == 0 and item["files"] == {"documents": 3, "transcripts": 0, "unsupported": 1, "unreadable": [], "io_failed": [], "skipped": 1,
                                           "chars": len("Alpha text.") + len("Bravo text.") + len("(no text layer: 1 pages)"),
                                           "no_text": 1, "by_type": {".md": 1, ".pdf": 1, ".txt": 1}}
    assert item["cursor"] == {"present": True, "files": 3}
    assert item["last_run"]["status"] == "ok" and item["last_run"]["items"] == 3 and item["last_run"]["error"] == ""
    # Failing: the folder goes away and three runs fail.
    shutil.rmtree(folder)
    for hour in (1, 2, 3):
        clock.at(f"2026-10-09T0{hour}:00:00Z")
        assert go(instance)[0] == 1
    clock.at("2026-10-09T03:10:00Z")
    code, payload, err = bf("check", "--instance", str(instance))
    item = payload["collectors"][0]
    assert code == 1 and payload["ok"] is False and item["folder"] == {"set": True, "exists": False, "readable": False}
    assert item["intake"]["state"] == "failing" and item["intake"]["cause"] == "folder"
    assert item["last_run"]["status"] == "error" and "folder is missing or unreadable" in item["last_run"]["error"]
    assert "folder is missing or unreadable" in item["problems"] and any(p.startswith("intake failing") for p in item["problems"])
    # Silent: the folder is back, and the last run is long ago.
    folder.mkdir()
    clock.at("2026-10-09T08:00:00Z")
    code, payload, err = bf("check", "--instance", str(instance))
    # (the three failed runs are still the newest runs: failing, not silent)
    assert payload["collectors"][0]["intake"]["state"] == "failing"
    # No collector named, none with a live_folder.
    bare = custom_instance(tmp_path / "bare")
    code, payload, err = bf("check", "--instance", str(bare))
    assert (code, payload["collectors"]) == (0, [])
    code, payload, err = bf("check", "--instance", str(bare), NAME)
    assert code == 1 and payload["collectors"][0]["problems"] == ["no live_folder is set"]
    assert bf("check", "--instance", str(bare), "nope")[0] == 2
    code, out, err = raw_bf("check", "--instance", str(bare), "notes")
    assert code == 2 and err.strip() == "brain-fetch: no collector notes"


def test_run_and_check_usage(tmp_path: Path) -> None:
    from tests.collector_fixtures import COLLECTORS

    instance = custom_instance(tmp_path, extra_collectors={"notes": COLLECTORS["notes"][0]}, agents={"notes": {
        "kind": "collector", "operations": ["add_source", "record_collector_run"]}})
    code, out, err = raw_bf("run", "--instance", str(instance), "notes")
    assert code == 2 and err.strip() == (
        "brain-fetch: collector notes is type files; brain-fetch handles capture and calendar collectors")
    code, out, err = raw_bf("run", "--instance", str(instance))
    assert code == 0 and json.loads(out) == {"collectors": []}
    code, out, err = raw_bf("run", "--instance", str(instance), "ghost")
    assert code == 2 and err.strip() == "brain-fetch: no collector ghost"
    code, out, err = raw_bf("run")
    assert code == 2 and err.strip() == "brain-fetch: no instance given (pass --instance or set BRAIN_INSTANCE)"
    os.environ["BRAIN_INSTANCE"] = str(instance)
    try:
        assert raw_bf("check")[0] == 0
    finally:
        del os.environ["BRAIN_INSTANCE"]
    code, out, err = raw_bf("--version")
    assert (code, out.strip()) == (0, f"agent-si {__version__}")
    assert raw_bf("run", "--instance", str(tmp_path / "missing"))[0] == 2
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "brain.toml").write_text("not = [valid")
    code, out, err = raw_bf("run", "--instance", str(broken))
    assert code == 2 and err.startswith("brain-fetch: brain.toml: ")
    assert raw_bf("bogus")[0] == 2 and raw_bf()[0] == 2


class Launchctl:
    def __init__(self, tmp_path: Path):
        self.calls = tmp_path / "launchctl.calls"
        self.path = tmp_path / "launchctl"
        self.path.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\nexit 0\n')
        self.path.chmod(0o755)

    def recorded(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


def test_schedule_launchd(tmp_path: Path, monkeypatch) -> None:
    import plistlib

    instance, _folder = world(tmp_path)
    home = tmp_path / "home"
    fake = Launchctl(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(schedule, "LAUNCHCTL", str(fake.path))
    uid = os.getuid()
    label = "ai.agent-si.fetch.demo"
    plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        patch.setattr(sys, "argv", ["/opt/venv/bin/brain-fetch"])
        code, out, err = raw_bf("schedule", "--instance", str(instance))
    assert code == 0, err
    assert json.loads(out) == {"status": "installed", "label": label, "path": str(plist_path), "every_minutes": 30}
    plist = plistlib.loads(plist_path.read_bytes())
    where = str(instance.resolve())
    assert plist == {
        "Label": label,
        "ProgramArguments": ["/opt/venv/bin/brain-fetch", "run", "--instance", where],
        "StartInterval": 1800, "RunAtLoad": False, "ProcessType": "Background",
        "StandardOutPath": f"{where}/fetch/run.log", "StandardErrorPath": f"{where}/fetch/run.log",
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    }
    assert fake.recorded() == [f"bootout gui/{uid}/{label}", f"bootstrap gui/{uid} {plist_path}"]
    # Not an installed script: python -m brain_fetch.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        patch.setattr(sys, "argv", ["pytest"])
        assert raw_bf("schedule", "--instance", str(instance), "--every", "30m")[0] == 0
    assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"] == [sys.executable, "-m", "brain_fetch", "run", "--instance", where]
    # --off boots out and deletes the file.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        code, out, err = raw_bf("schedule", "--instance", str(instance), "--off")
    assert code == 0 and json.loads(out)["status"] == "removed" and not plist_path.exists()
    assert fake.recorded()[-1] == f"bootout gui/{uid}/{label}"
    # --every must equal [fetch] every_minutes.
    calls = len(fake.recorded())
    code, out, err = raw_bf("schedule", "--instance", str(instance), "--every", "45m")
    assert (code, out) == (2, "") and err.strip() == "brain-fetch: --every 45m disagrees with [fetch] every_minutes = 30; change brain.toml first"
    assert raw_bf("schedule", "--instance", str(instance), "--every", "soon")[0] == 2
    assert len(fake.recorded()) == calls and not plist_path.exists()
    hourly = custom_instance(tmp_path / "hourly", extra_toml="[fetch]\nevery_minutes = 60\n")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        assert raw_bf("schedule", "--instance", str(hourly), "--every", "1h")[0] == 0
    assert plistlib.loads(plist_path.read_bytes())["StartInterval"] == 3600


def test_schedule_prints_systemd_elsewhere(tmp_path: Path, monkeypatch) -> None:
    instance, _folder = world(tmp_path)
    home = tmp_path / "home"
    fake = Launchctl(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(schedule, "LAUNCHCTL", str(fake.path))
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        code, out, err = raw_bf("schedule", "--instance", str(instance))
    assert code == 0, err
    payload = json.loads(out)
    assert payload["status"] == "printed" and payload["every_minutes"] == 30
    assert "OnBootSec=5min" in payload["text"] and "OnUnitActiveSec=30min" in payload["text"]
    assert "Persistent=true" in payload["text"] and "systemctl --user enable --now" in payload["text"]
    assert not home.exists() and fake.recorded() == []
    assert not (instance / "fetch" / "run.log").exists()


# -- 87: the package stays small and offline -------------------------------------------------------------------


def test_brain_fetch_stays_standard_library() -> None:
    root = Path(__file__).resolve().parents[1]
    package = root / "src" / "brain_fetch"
    forbidden = {"socket", "urllib", "http", "ssl", "ftplib", "smtplib", "poplib", "imaplib", "xmlrpc", "requests", "httpx",
                 "anthropic", "openai", "asyncio", "selectors", "socketserver", "webbrowser"}
    needles = ("fet" + "ch" + "(", "." + "live")
    files = sorted(package.glob("*.py"))
    assert len(files) >= 8
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert len(text.splitlines()) <= 220, path.name
        for needle in needles:
            assert needle not in text, (path.name, needle)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                names = [node.module or ""]
            for name in names:
                top = name.split(".")[0]
                # Net.py is the one module that reaches the network.
                assert top not in forbidden or (path.name == "net.py" and top in {"urllib", "http", "ssl"}), (
                    path.name, name)
                assert top in sys.stdlib_module_names or top in {"brain", "brain_fetch", "pypdf"}, (path.name, name)
                if top == "pypdf":
                    inside = [fn for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and node in ast.walk(fn)]
                    assert inside, f"{path.name} imports pypdf at module level"
    # capture.py uses none of the identifiers the existing collector test forbids.
    banned = {"open", "os", "environ", "getenv", "socket", "subprocess", "urllib", "http", "ssl"}
    tree = ast.parse((root / "src" / "brain" / "collectors" / "capture.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id not in banned
        elif isinstance(node, ast.Attribute):
            assert node.attr not in banned
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            assert node.name not in banned
        elif isinstance(node, ast.arg):
            assert node.arg not in banned
    assert shutil.which("brain-fetch"), "brain-fetch is not an installed console script"


# -- 88 to 91: the whole path ------------------------------------------------------------------------------------


WORDS = ("seat plan price limit region window launch review budget policy quarter report vendor tenant owner draft "
         "ticket cluster budget forecast metric sample record").split()


def big_text(number: int, extra: str = "") -> str:
    body = " ".join(WORDS[(number + step) % len(WORDS)] for step in range(1960))
    return f"Document {number} says seats are {number + 100}. {extra}{body}"


def test_end_to_end_300_documents(tmp_path: Path, clock: Clock) -> None:
    keys = [{"key": "demo.seats", "value_type": "int"}, {"key": "demo.price", "value_type": "number", "unit": "AUD"}]
    agents = {"bot": {"kind": "agent", "operations": "*"}}
    instance, folder = world(tmp_path, agents=agents, fact_keys=keys)
    for number in range(300):
        write(folder, f"corpus/d{number:03d}.md", big_text(number))
    started = time.perf_counter()
    code, result, err = go(instance)
    first = time.perf_counter() - started
    assert code == 0 and result["files"]["changed"] == 300 and len(sources(instance)) == 300
    clock.at("2026-10-09T01:00:00Z")
    started = time.perf_counter()
    code, result, err = go(instance)
    second = time.perf_counter() - started
    assert code == 0 and result["files"]["unchanged"] == 300 and len(sources(instance)) == 300
    print(f"300 documents of 2,000 words: first run {first:.2f} s, second run {second:.2f} s")
    assert first < 60
    from tests.test_truth import _apply, _clm, _ok

    def source_of(number: int) -> str:
        return sources(instance, f"document:{NAME}/corpus/d{number:03d}.md")[-1][0]

    _ok(_apply(instance, "bot", _clm("clm-keep", "demo.seats", "100", "Document 0 says seats are 100", source_of(0))))
    _ok(_apply(instance, "bot", _clm("clm-lose", "demo.price", "101", "Document 1 says seats are 101", source_of(1))))
    # Three edits make three versions; one keeps its claim's quote, one drops it.
    write(folder, "corpus/d000.md", big_text(0, "A paragraph was added. "), mtime=OLD + 50)
    write(folder, "corpus/d001.md", "Document 1 was rewritten entirely. " + "other " * 100, mtime=OLD + 50)
    write(folder, "corpus/d002.md", big_text(2, "A line was added. "), mtime=OLD + 50)
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["files"]["changed"] == 3 and len(sources(instance)) == 303
    carried = ro_rows(instance, "SELECT id, proposed_by FROM claims WHERE created_by LIKE 'workdocs:%'")
    assert len(carried) == 1 and carried[0][1] == "bot"
    drift = ro_rows(instance, "SELECT subject_id, status FROM signals WHERE kind = 'drift'")
    assert drift == [("demo.price", "open")]
    # The writer supersedes the carried claim (rule 2): its claim names the carried one.
    # 25 removals are refused by the guard, and removed with --allow-mass-change.
    for number in range(100, 125):
        (folder / f"corpus/d{number:03d}.md").unlink()
    clock.at("2026-10-09T03:00:00Z")
    code, result, err = go(instance)
    assert code == 1 and "mass change: 25 documents would change" in result["message"]
    assert len(sources(instance)) == 303
    clock.at("2026-10-09T04:00:00Z")
    code, result, err = go(instance, "--allow-mass-change")
    assert code == 0 and len(sources(instance)) == 328
    live = ro_rows(instance, "SELECT count(*) FROM sources s WHERE s.id GLOB 'src-cap-*' AND NOT EXISTS (SELECT 1 FROM sources n WHERE n.supersedes_id = s.id) AND s.content NOT LIKE '%removed from work-docs%'")
    assert live == [(275,)]
    # A run with an unreadable file removes nothing.
    write(folder, "corpus/broken.docx", b"not a word file")
    clock.at("2026-10-09T05:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["files"]["unreadable"] == 1 and len(sources(instance)) == 328
    assert store.check_instance(instance) == []


def test_end_to_end_cursor_rerun(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    (instance / "PAUSED").write_text("investigating")
    code, result, err = go(instance)
    assert code == 5 and not (instance / "fetch" / f"{NAME}.state.json").exists()
    (instance / "PAUSED").unlink()
    code, result, err = go(instance)
    assert code == 0 and result["cursor"] == "advanced" and len(sources(instance)) == 1
    start = cursor_bytes(instance)
    # A failed collect leaves an existing cursor byte-identical, and a rerun applies.
    write(folder, "b.md", "Bravo text.")
    (instance / "PAUSED").write_text("again")
    clock.at("2026-10-09T01:00:00Z")
    assert go(instance)[0] == 5 and cursor_bytes(instance) == start
    (instance / "PAUSED").unlink()
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and len(sources(instance)) == 2 and cursor_bytes(instance) != start
    # A successful run followed by a rerun is a no-op.
    clock.at("2026-10-09T03:00:00Z")
    done = cursor(instance)["files"]
    code, result, err = go(instance)
    assert code == 0 and result["files"]["changed"] == 0 and len(sources(instance)) == 2 and cursor(instance)["files"] == done


def test_end_to_end_intake_recovery(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    assert go(instance)[0] == 0
    shutil.rmtree(folder)
    for hour in (1, 2, 3):
        clock.at(f"2026-10-09T0{hour}:00:00Z")
        code, result, err = go(instance)
        assert code == 1 and result["status"] == "error"
    clock.at("2026-10-09T03:05:00Z")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert brain_main(["read", "brief", "--instance", str(instance)]) == 0
    lines = [line for line in out.getvalue().splitlines() if line.startswith("Intake:")]
    assert lines == ["Intake: work-docs failing since 2026-10-09 (folder). Ask the person to run brain-fetch check."]
    folder.mkdir()
    write(folder, "a.md", "Alpha text.")
    clock.at("2026-10-09T04:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        brain_main(["read", "brief", "--instance", str(instance)])
    assert not any(line.startswith("Intake:") for line in out.getvalue().splitlines())


def test_documents_never_claim_the_principal(tmp_path: Path, clock: Clock) -> None:
    instance, folder = world(tmp_path)
    for number in range(5):
        write(folder, f"d{number}.md", f"Document {number}. Written by Alex Example.")
    write(folder, "e.docx", ff.make_docx(ff.para("A Word file.")))
    assert go(instance)[0] == 0
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id GLOB 'src-cap-*'") == [(6,)]
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE id GLOB 'src-cap-*' AND author_person_id IS NOT NULL") == [(0,)]
    assert marks.key_path(instance).exists() is False  # brain-fetch signs principal items only, and 2a has none
    export = last_export(instance)
    assert "mark" not in export and not any(item.get("from_principal") for item in export["items"])


# -- Further reader checks ----------------------------------------------------------------------------


def _checked(instance: Path, *names: str) -> dict:
    code, payload, err = bf("check", "--instance", str(instance), *names)
    assert payload is not None, err
    return payload["collectors"][0]


def test_a_corrupt_document_is_one_unreadable_file(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    """Whatever a reader raises, the file is unreadable and the rest of the folder is still captured."""
    utf9 = ff.make_docx(raw_xml=b'<?xml version="1.0" encoding="UTF-9"?><w:document xmlns:w="' + ff.W.encode() + b'"/>')
    with pytest.raises(readers.ReadError):
        readers.read_docx(utf9)
    for error in (ValueError("negative seek value -35"), LookupError("unknown encoding"), OverflowError("cannot convert"),
                  UnicodeError("bad"), RecursionError("deep"), ZeroDivisionError("zero")):
        path = tmp_path / "x.docx"
        path.write_bytes(ff.make_docx(ff.para("x")))
        with monkeypatch.context() as patch:
            patch.setattr(readers, "read_docx", lambda data, error=error: (_ for _ in ()).throw(error))
            outcome, tag, sha, _stat = fileio.read_file(str(path), "docx")
        assert isinstance(outcome, readers.ReadError) and tag == "docx" and len(sha) == 64, error
    # The Word reader itself turns what zipfile and expat raise on damaged input into ReadError.
    good = ff.make_docx(ff.para("x"))
    for patched, error in (("ZipFile", ValueError("negative seek value -35")), ("ZipFile", struct.error("bad")),
                           ("ZipFile", OverflowError("too big")), ("ZipFile", LookupError("unknown encoding"))):
        with monkeypatch.context() as patch:
            patch.setattr(readers.zipfile, patched, lambda *a, error=error, **k: (_ for _ in ()).throw(error))
            with pytest.raises(readers.ReadError, match="corrupt"):
                readers.read_docx(good)
    with monkeypatch.context() as patch:
        patch.setattr(readers.ET, "fromstring", lambda *a, **k: (_ for _ in ()).throw(LookupError("unknown encoding: UTF-9")))
        with pytest.raises(readers.ReadError, match="corrupt"):
            readers.read_docx(good)
    instance, folder = world(tmp_path / "w")
    write(folder, "good.md", "Good text.")
    write(folder, "bad.docx", utf9)
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok" and result["files"]["unreadable"] == 1, err
    assert refs(instance) == [f"document:{NAME}/good.md"]
    assert _checked(instance)["files"]["unreadable"] == ["bad.docx"]


def test_a_long_ref_is_an_unreadable_file(tmp_path: Path, clock: Clock) -> None:
    """A path whose ref passes 500 characters is unreadable; it never makes the export invalid."""
    instance, folder = world(tmp_path)
    write(folder, "short.md", "Short text.")
    deep = "/".join(["d" * 100] * 5) + "/long.md"
    assert len(f"document:{NAME}/{deep}") > 500
    write(folder, deep, "A document under a very long path.")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok" and result["cursor"] == "advanced", (result, err)
    assert result["files"]["unreadable"] == 1 and result["files"]["changed"] == 1
    export = last_export(instance)
    assert [item["ref"] for item in export["items"]] == [f"document:{NAME}/short.md"]
    assert all(len(ref) <= 500 for ref in export.get("present", []))
    assert refs(instance) == [f"document:{NAME}/short.md"]
    assert cursor(instance)["files"][deep]["reader"] == "failed"
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["files"]["unreadable"] == 1 and result["files"]["unchanged"] == 1
    assert _checked(instance)["files"]["unreadable"] == [deep]
    # The longest ref that fits (500 characters) is still captured.
    exact = "/".join(["e" * 100] * 3) + "/" + "f" * (500 - len(f"document:{NAME}/") - 303 - 3) + ".md"
    assert len(f"document:{NAME}/{exact}") == 500
    write(folder, exact, "Exactly at the limit.")
    clock.at("2026-10-09T02:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and len(refs(instance)) == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode 000 file")
def test_a_file_or_folder_that_cannot_be_read_is_named_by_check(tmp_path: Path, clock: Clock) -> None:
    """A permission error holds the cursor, and check says which path and is not ok."""
    instance, folder = world(tmp_path)
    write(folder, "ok.md", "Fine text.")
    sealed = write(folder, "sealed.md", "Cannot be read.")
    write(folder, "closed/inner.md", "Hidden text.")
    sealed.chmod(0)
    (folder / "closed").chmod(0)
    try:
        code, result, err = go(instance)
        assert code == 0 and result["status"] == "partial" and result["cursor"] == "held"
        assert "could not be read" in result["message"]
        code, payload, err = bf("check", "--instance", str(instance))
        item = payload["collectors"][0]
        assert code == 1 and payload["ok"] is False
        assert item["files"]["io_failed"] == ["closed", "sealed.md"]
        assert item["problems"] == ["2 files or folders could not be read: closed, sealed.md"]
    finally:
        sealed.chmod(0o644)
        (folder / "closed").chmod(0o755)
    clock.at("2026-10-09T01:00:00Z")
    assert go(instance)[1]["status"] == "ok"
    assert _checked(instance)["problems"] == []


def test_a_damaged_cursor_entry_is_read_again(tmp_path: Path, clock: Clock) -> None:
    """An entry without the shape brain-fetch writes is dropped, never a crash."""
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    write(folder, "b.md", "Bravo text.")
    assert go(instance)[0] == 0
    path = instance / "fetch" / f"{NAME}.state.json"
    data = json.loads(path.read_text())
    good = data["files"]["a.md"]
    data["files"]["a.md"] = {key: value for key, value in good.items() if key != "reader"}
    data["files"]["b.md"] = dict(data["files"]["b.md"], size="big")
    data["files"]["c.md"] = "not a dict"
    path.write_text(json.dumps(data))
    assert sorted(state.read_cursor(instance, NAME)) == []
    clock.at("2026-10-09T01:00:00Z")
    code, result, err = go(instance)
    assert code == 0 and result["status"] == "ok" and result["files"]["changed"] == 2 and len(sources(instance)) == 2  # read again, no new version
    assert sorted(state.read_cursor(instance, NAME)) == ["a.md", "b.md"]


def test_timestamps_that_cannot_be_numbers_are_unreadable(tmp_path: Path) -> None:
    """Huge clocks and infinite JSON times never escape the transcript readers as another exception."""
    huge = "9" * 5000
    for read, source in (
        (transcripts.read_vtt, f"WEBVTT\n\n{huge}:00:01.000 --> 00:00:02.000\nHi\n"),
        (transcripts.read_srt, f"1\n{huge}:00:01,000 --> 00:00:02,000\nHi\n"),
        (transcripts.read_json, f'{{"segments":[{{"text":"x","start":"{huge}:00:00"}}]}}'),
    ):
        with pytest.raises(readers.ReadError, match="unrecognised timestamp"):
            read(source)
    for source in ('{"segments":[{"text":"x","start":Infinity}]}', '{"segments":[{"text":"x","start":1e400}]}',
                   '{"segments":[{"text":"x","start":' + huge + "}]}", '{"segments":[{"text":"x","start":-Infinity}]}'):
        try:
            assert transcripts.read_json(source) == "x"  # the time is dropped, the text stays
        except readers.ReadError:
            pass  # a number too large for the JSON reader to parse at all


def test_a_stalled_read_is_an_io_failure(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    """The time limit covers the open and the read too; a stall is transient, not a content failure."""
    instance, folder = world(tmp_path)
    write(folder, "a.md", "Alpha text.")
    write(folder, "b.md", "Bravo text.")
    monkeypatch.setattr(readers, "TIME_LIMIT", 0.3)
    real_read = os.read

    def stall(fd, size):
        time.sleep(5)
        return real_read(fd, size)

    path = folder / "a.md"
    with monkeypatch.context() as patch:
        patch.setattr(fileio.os, "read", stall)
        started = time.perf_counter()
        with pytest.raises(OSError, match="took too long reading"):
            fileio.read_file(str(path), "text")
        assert time.perf_counter() - started < 3
        code, result, err = go(instance)
    assert code == 0 and result["status"] == "partial" and result["cursor"] == "held"
    assert result["files"]["unreadable"] == 0 and sources(instance) == []
    clock.at("2026-10-09T01:00:00Z")
    assert go(instance)[1]["status"] == "ok"


def test_schedule_refuses_a_second_instance_of_the_same_name(tmp_path: Path, monkeypatch) -> None:
    """A job for another instance named the same is not overwritten; a failed bootstrap leaves no job file."""
    import plistlib

    one, _folder = world(tmp_path / "one")
    two, _folder = world(tmp_path / "two")
    home = tmp_path / "home"
    fake = Launchctl(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(schedule, "LAUNCHCTL", str(fake.path))
    plist_path = home / "Library" / "LaunchAgents" / "ai.agent-si.fetch.demo.plist"
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "darwin")
        assert raw_bf("schedule", "--instance", str(one))[0] == 0
        before = plist_path.read_bytes()
        calls = len(fake.recorded())
        code, out, err = raw_bf("schedule", "--instance", str(two))
        assert (code, out) == (2, "")
        assert err.strip() == (f"brain-fetch: a job named ai.agent-si.fetch.demo is already installed for {one.resolve()}; "
                               "remove it with brain-fetch schedule --off on that instance first")
        assert plist_path.read_bytes() == before and len(fake.recorded()) == calls
        assert raw_bf("schedule", "--instance", str(one))[0] == 0  # the same instance may install again
        assert plistlib.loads(plist_path.read_bytes())["ProgramArguments"][-1] == str(one.resolve())
        # A bootstrap that fails removes the file it wrote.
        assert raw_bf("schedule", "--instance", str(one), "--off")[0] == 0
        fake.path.write_text(f'#!/bin/sh\necho "$@" >> "{fake.calls}"\n[ "$1" = bootstrap ] && exit 5\nexit 0\n')
        code, out, err = raw_bf("schedule", "--instance", str(one))
        assert code == 1 and "launchctl bootstrap failed" in err and not plist_path.exists()
    # The systemd command quotes a path with spaces.
    spaced, _folder = world(tmp_path / "with space")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "platform", "linux")
        code, out, err = raw_bf("schedule", "--instance", str(spaced))
    text = json.loads(out)["text"]
    assert f"'{spaced.resolve()}'" in text and "ExecStart=" in text
