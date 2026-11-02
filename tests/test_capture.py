"""The capture collector, its marks, its config and `brain collect --input`. Every name, ref and text here is invented."""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import re
from pathlib import Path

import pytest

from brain import marks, store
from brain.cli import main
from brain.collectors import capture
from brain.collectors.base import RunContext
from brain.config import ConfigError
from tests.conftest import file_sha, ro_rows
from tests.phase2a import (
    FETCHED,
    capture_instance,
    custom_config,
    custom_instance,
    export,
    item,
    load_text,
    ref_of,
    run_collect,
    runs,
    source_count,
    sources,
    write_export,
)
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)


def _run_error(instance: Path) -> str:
    return ro_rows(instance, "SELECT error FROM collector_runs ORDER BY started_at DESC, rowid DESC LIMIT 1")[0][0]


def _id(ref: str, content: str, origin: str = "work-docs") -> str:
    h16 = hashlib.sha256(f"{origin}\n{ref}".encode()).hexdigest()[:16]
    return f"src-cap-{h16}-{hashlib.sha256(content.encode()).hexdigest()[:16]}"


# -- 23, 24, 24b ----------------------------------------------------------------------------------


def test_normalise() -> None:
    assert capture.normalise("café") == "café"  # NFC
    assert capture.normalise("a\r\nb\rc") == "a\nb\nc"
    assert capture.normalise("a\x00b\x07c\x0bd\x0ce\x1ff\x7fg\th\ni") == "abcdefg\th\ni"
    assert capture.normalise("one  \t\ntwo \t") == "one\ntwo"
    assert capture.normalise("a\n\n\n\nb") == "a\n\n\nb"
    assert capture.normalise("a\n\n\n\n\n\n\nb") == "a\n\n\nb"
    assert capture.normalise("\n\n a \n\n") == " a"
    for sample in ("café\r\nx  \n\n\n\n\ny\n\n", "plain", "\n\n\n\nx\x00\n\n\n\n"):
        once = capture.normalise(sample)
        assert capture.normalise(once) == once


def test_content_header_and_ids(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, table={"kinds": ["document", "email"]})
    document = item("specs/pricing.md", "# Pricing\n\nThe Pro plan costs 49 AUD a seat.", date="2026-10-07")
    email = item(
        "inbox/launch.eml", "Please review the launch plan.", kind="email", date="2026-10-02",
        **{"from": "Sam Example", "to": ["Alex Example", "Kit Example"], "title": "Launch review", "uri": "https://mail.example/1"},
    )
    code, payload, err = run_collect(instance, export([document, email]))
    assert code == 0, (payload, err)
    doc_content = f"kind: document\nref: {document['ref']}\n\n# Pricing\n\nThe Pro plan costs 49 AUD a seat."
    mail_content = (
        "kind: email\nfrom: Sam Example\nto: Alex Example, Kit Example\ndate: 2026-10-02\n"
        f"subject: Launch review\nref: {email['ref']}\n\nPlease review the launch plan."
    )
    rows = ro_rows(
        instance,
        "SELECT id, kind, title, uri, source_date, author_person_id, content, external_ref, supersedes_id "
        "FROM sources WHERE id GLOB 'src-cap-*' ORDER BY external_ref",
    )
    assert rows[1] == (_id(email["ref"], mail_content), "email", "Launch review", "https://mail.example/1",
                       "2026-10-02", None, mail_content, email["ref"], None)
    assert rows[0] == (_id(document["ref"], doc_content), "document", "specs/pricing.md", "", "2026-10-07",
                       None, doc_content, document["ref"], None)
    # No date: the fetched_at date in the instance time zone.
    nodate = item("specs/limits.md", "Limits.", date=None)
    assert run_collect(instance, export([document, email, nodate], fetched_at="2026-10-08T23:30:00Z"))[0] == 0
    assert ro_rows(instance, "SELECT source_date FROM sources WHERE external_ref = ?", (nodate["ref"],)) == [("2026-10-08",)]


def test_content_and_ids_are_pinned(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, table={"kinds": ["document", "email"]})
    items = [
        item("a/crlf.md", "Café menu\r\nPrix: 5 AUD  \r\n"),
        item("b/blank.md", "Top\n\n\n\n\nBottom\n\n\n"),
        item("c/empty.md", ""),
        item("d/mail.eml", "Hello.", kind="email", **{"from": "Sam Example", "to": ["Alex Example"]}),
        item("e/indent.md", "    indented code\n    block\n"),
    ]
    assert run_collect(instance, export(items))[0] == 0
    rest = [items[0], items[1], items[3], items[4]]
    assert run_collect(instance, export(rest, present=[items[2]["ref"]], fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    # The empty document is missing from a complete export: its removal version is the fourth row.
    assert run_collect(instance, export(rest, fetched_at="2026-10-08T02:00:00Z"))[0] == 0
    got = ro_rows(
        instance,
        "SELECT external_ref, id, content_sha256 FROM sources WHERE id GLOB 'src-cap-*' ORDER BY external_ref, rowid",
    )
    assert capture.CONTENT_FORMAT == 1
    assert got == PINNED_SOURCES, got


# Worked out by hand from the formulas (sha256 of "origin\nref" and of the content), not by running
# the collector. A change to normalise, the header or scrub_text fails test 24b on purpose.
PINNED_SOURCES = [
    ('document:work-docs/a/crlf.md', 'src-cap-26dc57b6e56a52e2-66a49af403fe5921', '66a49af403fe5921faa431ad4ba6ab7993e65fc5845a02a6182b1f362a4d8cbf'),
    ('document:work-docs/b/blank.md', 'src-cap-15c87a8994740b51-d4aef3d80eb602ac', 'd4aef3d80eb602ac032fd50e85e2e7faf76e6658a692866ab737fbdef3e12151'),
    ('document:work-docs/c/empty.md', 'src-cap-ddd66cd0c32b2b77-1df001a8905411bd', '1df001a8905411bdfba49e7378e8f2f28607bd55d786812cd7ca19b2d17193dc'),
    ('document:work-docs/c/empty.md', 'src-cap-ddd66cd0c32b2b77-cc3671cd34088e70', 'cc3671cd34088e70f856abf98c47531249a0d88fb6488528538d5008193d0321'),
    ('document:work-docs/e/indent.md', 'src-cap-6af742831a8b2f21-0fc726f8ff57539e', '0fc726f8ff57539ec5180e587aef8ac38c28d68de6cf699620ba9ae7a07fb432'),
    ('email:work-docs/d/mail.eml', 'src-cap-beeced5a7019e7f4-7f01283c34ae3a8c', '7f01283c34ae3a8cb744b35c62a200c6bb229045592af796a9552a6c69791892'),
]


def test_refetch_is_a_noop(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    doc = item("specs/pricing.md", "The Pro plan costs 49 AUD a seat.")
    assert run_collect(instance, export([doc]))[0] == 0
    before = source_count(instance)
    code, payload, _err = run_collect(instance, export([doc], fetched_at="2026-10-08T06:00:00Z"))
    assert code == 0 and source_count(instance) == before
    assert payload["duplicate"] is False and payload["status"] == "ok"
    # A different date for a document makes no version (a document's header has no date).
    moved = dict(doc, date="2026-10-01")
    assert run_collect(instance, export([moved], fetched_at="2026-10-08T07:00:00Z"))[0] == 0
    assert source_count(instance) == before
    # A present ref writes nothing either.
    assert run_collect(instance, export([], present=[doc["ref"]], fetched_at="2026-10-08T08:00:00Z"))[0] == 0
    assert source_count(instance) == before
    assert [row[0] for row in runs(instance)] == ["ok"] * 4


def test_new_version_supersedes_the_current_one(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    first = item("specs/pricing.md", "The Pro plan costs 49 AUD a seat.")
    assert run_collect(instance, export([first]))[0] == 0
    changed = dict(first, text="The Pro plan costs 59 AUD a seat.")
    code, payload, _err = run_collect(instance, export([changed], fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0 and payload["status"] == "ok"
    rows = sources(instance, first["ref"])
    assert len(rows) == 2 and rows[1][1] == rows[0][0] and rows[0][1] is None
    assert ro_rows(instance, "SELECT count(*) FROM write_log WHERE status = 'held'") == [(0,)]
    assert ro_rows(instance, "SELECT count(*) FROM sources WHERE supersedes_id = ?", (rows[0][0],)) == [(1,)]


def test_revert_gets_a_suffixed_id(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    a = item("notes/plan.md", "Plan A: ship in October.")
    b = dict(a, text="Plan B: ship in November.")
    for number, doc in enumerate([a, b, a]):
        stamp = f"2026-10-08T0{number}:00:00Z"
        assert run_collect(instance, export([doc], fetched_at=stamp))[0] == 0
    rows = sources(instance, a["ref"])
    assert len(rows) == 3
    content_a = f"kind: document\nref: {a['ref']}\n\n{a['text']}"
    base = _id(a["ref"], content_a)
    assert rows[0][0] == base and rows[2][0] == base + "-r3"
    assert rows[1][1] == rows[0][0] and rows[2][1] == rows[1][0]
    assert run_collect(instance, export([a], fetched_at="2026-10-08T05:00:00Z"))[0] == 0
    assert len(sources(instance, a["ref"])) == 3  # A again is a no-op


def test_removal_only_from_complete_exports(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    keep, drop, hold = (item(f"docs/{name}.md", f"Text of {name}.") for name in ("keep", "drop", "hold"))
    assert run_collect(instance, export([keep, drop, hold]))[0] == 0
    # An incomplete export removes nothing.
    code, payload, _err = run_collect(instance, export([keep], complete=False, fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0 and payload["status"] == "partial"
    assert len(sources(instance, drop["ref"])) == 1
    # A complete one drops `drop`, keeps `hold` by its present entry.
    assert run_collect(instance, export([keep], present=[hold["ref"]], fetched_at="2026-10-08T02:00:00Z"))[0] == 0
    rows = sources(instance, drop["ref"])
    assert len(rows) == 2
    removed = ro_rows(instance, "SELECT kind, title, content, supersedes_id, source_date FROM sources WHERE id = ?", (rows[1][0],))[0]
    assert removed == (
        "document", "docs/drop.md",
        f"kind: document\nref: {drop['ref']}\nremoved: 2026-10-08\n\nremoved from work-docs on 2026-10-08",
        rows[0][0], "2026-10-08",
    )
    assert len(sources(instance, hold["ref"])) == 1
    # A second complete export adds no second removal.
    assert run_collect(instance, export([keep], present=[hold["ref"]], fetched_at="2026-10-08T03:00:00Z"))[0] == 0
    assert len(sources(instance, drop["ref"])) == 2


def test_a_document_that_looks_like_a_removal_is_ordinary(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    fake = item("odd/removed.md", "removed from work-docs on 2026-10-10")
    assert run_collect(instance, export([fake]))[0] == 0
    code, payload, _err = run_collect(instance, export([fake], fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0 and len(sources(instance, fake["ref"])) == 1
    code, payload, err = run_collect(instance, export([], present=[fake["ref"]], fetched_at="2026-10-08T02:00:00Z"))
    assert code == 0, (payload, err)
    assert len(sources(instance, fake["ref"])) == 1


def test_a_long_ref_is_removed_once(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    path = "/".join(["segment" + "x" * 52] * 7) + "/long-name.md"
    long = item(path, "A document with a very long path.", title="long")
    assert len(long["ref"]) >= 440
    other = item("short.md", "Keep me.")
    assert run_collect(instance, export([long, other]))[0] == 0
    for number in range(1, 4):
        stamp = f"2026-10-08T0{number}:00:00Z"
        code, payload, _err = run_collect(instance, export([other], fetched_at=stamp))
        assert code == 0
    assert len(sources(instance, long["ref"])) == 2


def test_a_returning_document_supersedes_its_removal(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    doc, other = item("docs/back.md", "Back soon."), item("docs/stay.md", "Staying.")
    assert run_collect(instance, export([doc, other]))[0] == 0
    assert run_collect(instance, export([other], fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    assert run_collect(instance, export([doc, other], fetched_at="2026-10-08T02:00:00Z"))[0] == 0
    rows = sources(instance, doc["ref"])
    assert len(rows) == 3 and rows[2][1] == rows[1][0] and rows[1][1] == rows[0][0]
    assert rows[2][0].endswith("-r3")  # the same text as version 1 gets a suffix


def test_remove_return_remove_on_one_date(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    doc, other = item("docs/flip.md", "First text."), item("docs/stay.md", "Staying.")
    assert run_collect(instance, export([doc, other]))[0] == 0
    assert run_collect(instance, export([other], fetched_at="2026-10-08T01:00:00Z"))[0] == 0
    assert run_collect(instance, export([dict(doc, text="Second text."), other], fetched_at="2026-10-08T02:00:00Z"))[0] == 0
    code, payload, err = run_collect(instance, export([other], fetched_at="2026-10-08T03:00:00Z"))
    assert code == 0, (payload, err)
    rows = sources(instance, doc["ref"])
    assert len(rows) == 4 and len({row[0] for row in rows}) == 4
    assert [row[1] for row in rows[1:]] == [rows[0][0], rows[1][0], rows[2][0]]


def _populate(instance: Path, total: int, edited: int = 0, removed: int = 0) -> tuple[list[dict], dict]:
    docs = [item(f"bulk/doc{number:03d}.md", f"Body of document {number}.") for number in range(total)]
    assert run_collect(instance, export(docs))[0] == 0
    changed = [dict(doc, text=doc["text"] + " Edited.") for doc in docs[:edited]]
    kept = docs[edited : total - removed]
    return docs, export(changed, present=[doc["ref"] for doc in kept], fetched_at="2026-10-08T05:00:00Z")


def test_mass_change_guard(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path / "a")
    _docs, second = _populate(instance, 100, edited=11, removed=10)  # 21 moved of 100: over 20
    before = source_count(instance)
    code, payload, _err = run_collect(instance, second)
    assert code == 1 and payload["status"] == "error"
    assert _run_error(instance) == "mass change: 21 documents would change; rerun with --allow-mass-change if this is expected"
    assert source_count(instance) == before
    ok = capture_instance(tmp_path / "b")
    _docs, second = _populate(ok, 100, edited=10, removed=10)  # 20 of 100 pass
    assert run_collect(ok, second)[0] == 0
    small = capture_instance(tmp_path / "c")
    _docs, second = _populate(small, 15, edited=4)  # 4 moved is below the floor of 5, though 4 * 4 > 15
    assert run_collect(small, second)[0] == 0
    refused = capture_instance(tmp_path / "d")
    _docs, second = _populate(refused, 16, edited=5)  # 5 * 4 > 16
    assert run_collect(refused, second)[0] == 1
    assert _run_error(refused).startswith("mass change: 5 documents would change")
    edge = capture_instance(tmp_path / "e")
    _docs, second = _populate(edge, 20, edited=5)  # 5 * 4 is not more than 20
    assert run_collect(edge, second)[0] == 0


def test_allow_mass_change_reruns_the_refused_input(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    _docs, second = _populate(instance, 30, removed=25)
    path = write_export(tmp_path / "x", second)
    code, payload, _err = run_collect(instance, path)
    assert code == 1
    code, again, _err = run_collect(instance, path)
    assert code == 1 and again["duplicate"] is True and again["run_id"] == payload["run_id"]
    code, allowed, _err = run_collect(instance, path, "--allow-mass-change")
    assert code == 0 and allowed["duplicate"] is False and allowed["run_id"] != payload["run_id"]
    removals = ro_rows(instance, "SELECT count(*) FROM sources WHERE content LIKE '%removed from work-docs%'")
    assert removals == [(25,)]
    code, repeat, _err = run_collect(instance, path, "--allow-mass-change")
    assert repeat["duplicate"] is True


def test_truncation_at_both_limits(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    ascii_doc = item("big/ascii.md", "word " * 600_000)
    code, payload, _err = run_collect(instance, export([ascii_doc]))
    assert code == 0 and payload["status"] == "partial"
    content = ro_rows(instance, "SELECT content FROM sources WHERE external_ref = ?", (ascii_doc["ref"],))[0][0]
    assert len(content) == 2_000_000
    assert content.splitlines()[:4] == ["kind: document", f"ref: {ascii_doc['ref']}", "truncated at 2,000,000 characters", ""]
    assert ro_rows(instance, "SELECT error FROM collector_runs")[0][0] == "1 item truncated at the size limit"
    euros = item("big/euro.md", "€" * 1_500_000)
    code, payload, _err = run_collect(instance, export([euros], fetched_at="2026-10-08T01:00:00Z"))
    assert code == 0 and payload["status"] == "partial"
    row = ro_rows(
        instance,
        "SELECT id, kind, title, uri, source_date, author_person_id, content, external_ref FROM sources WHERE external_ref = ?",
        (euros["ref"],),
    )[0]
    columns = ("id", "kind", "title", "uri", "source_date", "author_person_id", "content", "external_ref")
    op = lambda text: len(store.canonical_json({"op": "add_source", "row": dict(zip(columns, row)) | {"content": text}}).encode())  # noqa: E731
    assert row[6].split("\n")[2] == "truncated at 3.5 MiB"
    assert op(row[6]) <= 3_670_016 < op(row[6] + "€")  # exact: one more character would not fit
    assert len(row[6]) < 2_000_000
    whole = item("big/ok.md", "short text")
    code, payload, _err = run_collect(instance, export([whole], fetched_at="2026-10-08T02:00:00Z"))
    assert code == 0 and payload["status"] == "ok"
    both = item("big/two.md", "x" * 2_100_000) | {"title": "two"}
    other = item("big/three.md", "y" * 2_100_000)
    code, payload, _err = run_collect(instance, export([both, other], fetched_at="2026-10-08T03:00:00Z"))
    assert code == 0
    assert ro_rows(instance, "SELECT error FROM collector_runs ORDER BY started_at DESC, rowid DESC LIMIT 1") == [
        ("2 items truncated at the size limit",)
    ]


def test_scrub_hook_runs_between_normalise_and_hash(tmp_path: Path, clock: Clock, monkeypatch) -> None:
    instance = capture_instance(tmp_path)
    seen: list[str] = []

    def scrub(text: str, kind: str) -> str:
        seen.append(text)
        return text.replace("secret", "XXXX")

    plain = item("notes/s.md", "a  \r\nsecret code")
    assert run_collect(instance, export([plain]))[0] == 0
    unscrubbed = sources(instance, plain["ref"])[0]
    monkeypatch.setattr(capture, "scrub_text", scrub)
    other = instance.parent / "other"
    inst2 = capture_instance(other)
    assert run_collect(inst2, export([plain]))[0] == 0
    assert seen == ["a\nsecret code"]  # normalised first
    stored = sources(inst2, plain["ref"])[0]
    assert stored[2].endswith("a\nXXXX code") and stored[0] != unscrubbed[0]


def _error_of(instance: Path, doc: dict) -> str:
    code, payload, _err = run_collect(instance, doc)
    assert code == 1 and payload["status"] == "error", payload
    return _run_error(instance)


def test_bad_exports_are_refused(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    good = item("docs/ok.md", "Fine.")
    assert run_collect(instance, export([good]))[0] == 0
    stamp = iter(f"2026-10-08T{hour:02d}:00:00Z" for hour in range(1, 24))

    def attempt(items=(), **kwargs) -> str:
        return _error_of(instance, export(items, fetched_at=next(stamp), **kwargs))

    email = item("docs/mail.md", "Text.", kind="email")
    assert attempt([email]) == f"{email['ref']}: kind email is not collected here"
    other = dict(good, ref="document:other/docs/ok.md")
    assert attempt([other]) == "document:other/docs/ok.md: ref must start document:work-docs/"
    bare = dict(good, ref="document:work-docs/")
    assert attempt([bare]) == "document:work-docs/: ref must start document:work-docs/"
    control = dict(good, ref="document:work-docs/a\tb.md")
    assert attempt([control]) == "document:work-docs/a\tb.md: ref must start document:work-docs/"
    for bad in ("a/../b.md", "a/./b.md", "a//b.md", "a/"):
        ref = f"document:work-docs/{bad}"
        assert attempt([dict(good, ref=ref)]) == f"{ref}: ref has an unsafe path"
    assert attempt([good, good]) == f"duplicate ref {good['ref']}"
    assert attempt([good], present=[good["ref"]]) == f"{good['ref']} is both an item and listed as unchanged"
    foreign = "document:other/docs/ok.md"
    assert attempt([], present=[foreign]) == f"{foreign}: ref is not under this collector's origin and kinds"
    ghost = "document:work-docs/never/stored.md"
    assert attempt([], present=[ghost]) == f"{ghost} is listed as unchanged but the store has no version of it; rerun with --rescan"
    assert _error_of(instance, export([good], origin="elsewhere", fetched_at=next(stamp))) == (
        "document origin elsewhere doesn't match collector origin work-docs"
    )
    future = export([good], fetched_at="2026-10-09T01:00:00Z")
    assert _error_of(instance, future) == "fetched_at is in the future"
    schema = export([good])
    schema["items"][0]["extra"] = 1
    assert _error_of(instance, schema).startswith("document is invalid: /items/0/extra is not allowed")
    assert len(sources(instance)) == 1


def test_error_export_records_an_error_run(tmp_path: Path, clock: Clock) -> None:
    instance = capture_instance(tmp_path)
    doc = export([], complete=False, error="folder is missing or unreadable", error_class="folder")
    code, payload, _err = run_collect(instance, doc)
    assert code == 1 and payload["status"] == "error"
    assert _run_error(instance) == "folder: folder is missing or unreadable"
    plain = export([], complete=False, error="odd", fetched_at="2026-10-08T01:00:00Z")
    assert run_collect(instance, plain)[0] == 1 and _run_error(instance) == "other: odd"
    partial = export([item("a.md", "Text.")], complete=False, fetched_at="2026-10-08T02:00:00Z")
    code, payload, _err = run_collect(instance, partial)
    assert code == 0 and payload["status"] == "partial"
    assert _run_error(instance) == "the document is incomplete"


# -- 36, 37: authorship and the mark -----------------------------------------------------------------


def _signed(instance: Path, items: list[dict], **extra) -> dict:
    doc = export(items, **extra)
    doc["mark"] = marks.sign(marks.ensure_key(instance), doc)
    return doc


def _authors(instance: Path) -> list:
    return [row[0] for row in ro_rows(instance, "SELECT author_person_id FROM sources WHERE id GLOB 'src-cap-*' ORDER BY external_ref")]


PRINCIPAL_ID = "per-alex-example"


def test_principal_authorship_needs_a_mark_and_the_agent_flag(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path / "a", table={"kinds": ["document", "user_note"]})
    mine = item("notes/mine.md", "My own note.", kind="user_note", from_principal=True, **{"from": "Alex Example"})
    code, payload, err = run_collect(instance, _signed(instance, [mine]))
    assert code == 0 and payload["status"] == "ok", (payload, err)
    assert _authors(instance) == [PRINCIPAL_ID]
    # Not marked: null author, the sender stays in the header, and the run notes it.
    plain = capture_instance(tmp_path / "b")
    unmarked = item("notes/mine.md", "My own note.", from_principal=True, **{"from": "Alex Example"})
    code, payload, _err = run_collect(plain, export([unmarked]))
    assert code == 0 and payload["status"] == "partial"
    assert _authors(plain) == [None]
    assert "from: Alex Example" in sources(plain)[0][2]
    assert "from_principal ignored: the export is not marked by brain-fetch or the agent may not author as the principal" in _run_error(plain)
    # The agent has no flag: a good mark still doesn't make it the principal's.
    noflag = custom_instance(tmp_path / "c", agent={"author_principal": False})
    code, payload, _err = run_collect(noflag, _signed(noflag, [unmarked]))
    assert code == 0 and payload["status"] == "partial" and _authors(noflag) == [None]
    # Any change after signing invalidates the mark.
    edits = {
        "text": "Changed note.", "kind": "document", "date": "2026-10-01", "from": "Someone Else",
        "to": ["Kit Example"], "title": "other title", "uri": "https://example.test/x",
    }
    for number, (field, value) in enumerate(edits.items()):
        target = custom_instance(tmp_path / f"d{number}", table={"kinds": ["document", "user_note"]})
        signed = _signed(target, [dict(mine)])
        signed["items"][0][field] = value
        if field == "kind":
            signed["items"][0]["ref"] = signed["items"][0]["ref"].replace("user_note:", "document:")
        code, payload, _err = run_collect(target, signed)
        assert code == 0 and _authors(target) == [None], field
        assert payload["status"] == "partial", field


def test_marks_verify_rules(tmp_path: Path, monkeypatch) -> None:
    instance = capture_instance(tmp_path)
    doc = export([item("a.md", "Text.", from_principal=True)])
    assert marks.verify(instance, doc) is False  # no key, no mark
    key = marks.ensure_key(instance)
    assert re.fullmatch(r"[0-9a-f]{64}", key) and marks.ensure_key(instance) == key
    path = marks.key_path(instance)
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    doc["mark"] = marks.sign(key, doc)
    assert marks.verify(instance, doc) is True
    assert marks.verify(instance, dict(doc, mark="0" * 64)) is False
    assert marks.verify(instance, {k: v for k, v in doc.items() if k != "mark"}) is False
    path.chmod(0o644)
    assert marks.verify(instance, doc) is False
    path.chmod(0o600)
    monkeypatch.setattr(marks.os, "getuid", lambda: os.stat(path).st_uid + 1)
    assert marks.verify(instance, doc) is False
    monkeypatch.undo()
    real = path.with_name("real.key")
    path.rename(real)
    path.symlink_to(real)
    assert marks.verify(instance, doc) is False


def test_hand_written_export_cannot_claim_the_principal(tmp_path: Path, clock: Clock) -> None:
    instance = custom_instance(tmp_path, table={"kinds": ["document", "user_note"]})
    mine = item("notes/mine.md", "My note.", kind="user_note", from_principal=True)
    # No mark at all.
    assert run_collect(instance, export([mine]))[0] == 0 and _authors(instance) == [None]
    # A mark of the right length under the wrong key (and no key file yet).
    wrong = export([dict(mine, text="My note two.")], fetched_at="2026-10-08T01:00:00Z", mark="a" * 64)
    assert run_collect(instance, wrong)[0] == 0
    assert set(_authors(instance)) == {None}
    # The key is created, but the mark is still the wrong one.
    key = marks.ensure_key(instance)
    assert key != "a" * 64
    again = export([dict(mine, text="My note three.")], fetched_at="2026-10-08T02:00:00Z", mark="a" * 64)
    assert run_collect(instance, again)[0] == 0 and set(_authors(instance)) == {None}
    # The key file is gone: even a mark made with the old key fails.
    path = marks.key_path(instance)
    signed = _signed(instance, [dict(mine, text="My note four.")], fetched_at="2026-10-08T03:00:00Z")
    path.unlink()
    assert run_collect(instance, signed)[0] == 0 and set(_authors(instance)) == {None}
    # Marked by marks.sign after ensure_key: the only way to be the principal.
    path.write_text(key + "\n")
    path.chmod(0o600)
    good = _signed(instance, [dict(mine, text="My note five.")], fetched_at="2026-10-08T04:00:00Z")
    assert run_collect(instance, good)[0] == 0
    authors = ro_rows(instance, "SELECT content, author_person_id FROM sources WHERE id GLOB 'src-cap-*'")
    assert [row[1] for row in authors if row[0].endswith("five.")] == [PRINCIPAL_ID]
    assert [row[1] for row in authors if not row[0].endswith("five.")] == [None] * 4


# -- 38 to 41: config and the flag ------------------------------------------------------------------


def _error(tmp_path: Path, **kwargs) -> str:
    with pytest.raises(ConfigError) as caught:
        load_text(tmp_path, custom_config(**kwargs))
    return str(caught.value)


def test_capture_config(tmp_path: Path) -> None:
    cfg = load_text(tmp_path / "ok", custom_config(table={"kinds": ["document", "email"], "live_folder": "~/Documents/Example"}))
    options = cfg.collectors["work-docs"].options
    assert options == {"origin": "work-docs", "kinds": ["document", "email"], "live_folder": "~/Documents/Example",
                       "service": "documents", "live_query": "", "live_channels": [], "include_dms": False, "repo": "",
                       "threads": ""}
    assert cfg.agents["workdocs"].author_principal is True
    defaults = load_text(tmp_path / "d", custom_config())
    assert defaults.collectors["work-docs"].options == {"origin": "work-docs", "kinds": ["document"], "live_folder": "",
                                                       "service": "documents", "live_query": "", "live_channels": [],
                                                       "include_dms": False, "repo": "", "threads": ""}
    kinds = "collectors.work-docs.kinds must be from document, meeting_transcript, user_note, email, slack"
    assert _error(tmp_path / "k1", table={"kinds": ["document", "wiki"]}) == kinds
    assert _error(tmp_path / "k2", table={"kinds": ["document", "document"]}) == kinds
    assert _error(tmp_path / "k3", table={"kinds": []}) == (
        "collectors.work-docs.kinds must be 1 to 10 one-line strings of 1 to 40 characters"
    )
    for number, bad in enumerate(("relative/path", "", "/a\nb", "x" * 401, 5)):
        assert _error(tmp_path / f"p{number}", table={"live_folder": bad}) == (
            "collectors.work-docs." + "live_folder must be an absolute path"
        )
    assert _error(tmp_path / "o", table={"origin": "Bad Origin"}) == "collectors.work-docs.origin must be a label"
    assert _error(tmp_path / "ops", agent={"operations": ["add_source", "upsert_action", "record_collector_run"]}) == (
        'collectors.work-docs: agent workdocs must have operations exactly ["add_source", "record_collector_run"]'
    )
    # author_principal stays refused on the older types.
    files_agent = {"kind": "collector", "operations": ["add_source", "record_collector_run"], "author_principal": True}
    from tests.collector_fixtures import COLLECTORS

    table = COLLECTORS["notes"][0]
    assert _error(
        tmp_path / "files", agents={"notes": files_agent}, extra_collectors={"notes": table}
    ) == "collectors.notes: agent notes must not have author_principal"
    second = {"type": "capture", "agent": "workdocs2", "origin": "work-docs"}
    agent2 = {"kind": "collector", "operations": ["add_source", "record_collector_run"]}
    assert _error(
        tmp_path / "dup", agents={"workdocs2": agent2}, extra_collectors={"work-docs-b": second}
    ) == "collectors work-docs and work-docs-b both capture origin work-docs"
    distinct = dict(second, origin="team-docs")
    both = custom_config(agents={"workdocs2": agent2}, extra_collectors={"work-docs-b": distinct})
    assert set(load_text(tmp_path / "two", both).collectors) == {"work-docs", "work-docs-b"}


CREDENTIAL = "looks like a credential; credentials never go in brain.toml"


def test_credential_shaped_config_is_refused(tmp_path: Path) -> None:
    from tests.collector_fixtures import COLLECTORS

    counter = iter(range(10_000))

    def error(table: dict, kind: str = "capture") -> str | None:
        kwargs = {"table": table} if kind == "capture" else {"extra_collectors": {"notes": {**COLLECTORS["notes"][0], **table}}}
        try:
            load_text(tmp_path / f"c{next(counter)}", custom_config(**kwargs))
        except ConfigError as exc:
            return str(exc)
        return None

    for kind in ("capture", "files"):
        name = "work-docs" if kind == "capture" else "notes"
        for key in ("token", "Secret", "password", "api_key", "client_secret", "APIKEY", "bearer", "access_token", "refresh_token"):
            assert error({key: "x"}, kind) == f"collectors.{name}.{key} {CREDENTIAL}", (kind, key)
        for value in ("xoxb-example-0001", "XOXB-example", "ghp_example", "Ya29.example", "xox", "GHP_", "ya29."):  # release-check: allow (an invented token the test refuses)
            assert error({"note": value}, kind) == f"collectors.{name}.note {CREDENTIAL}", (kind, value)
            assert error({"note": ["fine", value]}, kind) == f"collectors.{name}.note {CREDENTIAL}", (kind, value)
        longer = ("github_pat_" + "a" * 8, "sk-" + "a" * 14, "AKIA" + "A" * 12, "eyJ" + "a" * 13, "AIza" + "a" * 12,
                  "glpat-" + "a" * 10, "1//" + "a" * 13, "gho_" + "a" * 12, "ghu_" + "a" * 12, "ghs_" + "a" * 12,
                  "ghr_" + "a" * 12)
        for value in longer:
            assert len(value) >= 16
            assert error({"note": value}, kind) == f"collectors.{name}.note {CREDENTIAL}", (kind, value)
    # Harmless values pass the credential rule: the table is then refused only for its unknown key.
    for value in ("sk-docs", "example-team-docs-1", "github_pat_", "sk-" + "a" * 5, "SK-" + "a" * 20, "akia" + "a" * 20):
        assert error({"note": value}) == "unknown key collectors.work-docs.note", value
    assert error({"origin": "sk-docs"}) is None


def test_fetch_table_config(tmp_path: Path) -> None:
    default = load_text(tmp_path / "a", custom_config())
    assert (default.fetch.stale_after_runs, default.fetch.every_minutes) == (3, 30)
    ok = load_text(tmp_path / "b", custom_config(extra_toml="[fetch]\nstale_after_runs = 5\nevery_minutes = 60\n"))
    assert (ok.fetch.stale_after_runs, ok.fetch.every_minutes) == (5, 60)
    cases = {
        "[fetch]\nunknown = 1\n": "unknown key fetch.unknown",
        "[fetch]\nstale_after_runs = 0\n": "fetch.stale_after_runs must be a whole number from 1 to 20",
        "[fetch]\nstale_after_runs = 21\n": "fetch.stale_after_runs must be a whole number from 1 to 20",
        "[fetch]\nstale_after_runs = true\n": "fetch.stale_after_runs must be a whole number from 1 to 20",
        "[fetch]\nstale_after_runs = 2.5\n": "fetch.stale_after_runs must be a whole number from 1 to 20",
        "[fetch]\nevery_minutes = 4\n": "fetch.every_minutes must be a whole number from 5 to 1440",
        "[fetch]\nevery_minutes = 1441\n": "fetch.every_minutes must be a whole number from 5 to 1440",
        "[fetch]\nevery_minutes = true\n": "fetch.every_minutes must be a whole number from 5 to 1440",
        "fetch = 3\n": "fetch must be a table",
    }
    for number, (text, message) in enumerate(cases.items()):
        kwargs = {"top_toml": text} if text.startswith("fetch =") else {"extra_toml": text}
        assert _error(tmp_path / f"e{number}", **kwargs) == message


def _cli_collect(instance: Path, *args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["collect", "work-docs", "--instance", str(instance), *args])
    return code, out.getvalue(), err.getvalue()


def test_collect_input_is_the_fixture_alias(tmp_path: Path, clock: Clock) -> None:
    doc = export([item("specs/pricing.md", "The Pro plan costs 49 AUD a seat.")])
    path = write_export(tmp_path / "x", doc)
    first, second = capture_instance(tmp_path / "a"), capture_instance(tmp_path / "b")
    code_a, out_a, _ = _cli_collect(first, "--input", str(path))
    code_b, out_b, _ = _cli_collect(second, "--fixture", str(path))
    assert (code_a, code_b) == (0, 0) and out_a == out_b and out_a.strip()
    before = file_sha(first / "brain.sqlite3")
    code, out, err = _cli_collect(first)
    assert code == 2 and out == ""
    assert "brain collect needs --input FILE; the engine reads exported files only" in err
    assert file_sha(first / "brain.sqlite3") == before


def test_run_context_keeps_its_five_positional_fields() -> None:
    ctx = RunContext("c", {}, "UTC", "k", FETCHED)
    assert (ctx.agent, ctx.instance_dir, ctx.allow_mass_change, ctx.principal_id, ctx.author_principal, ctx.marked) == (
        "", "", False, "", False, False,
    )
    assert ref_of("a.md") == "document:work-docs/a.md"
