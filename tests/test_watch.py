"""Watched facts (brain.watch): a fact that matters shows when newer sources mention it.

watch.toml beside brain.toml lists the watched fact keys, each with words, a list of word groups. A source mentions
the fact when one paragraph holds a phrase from every group. The receipt names the fact as the source is added,
fact_get, search and the brief show it until a claim or a re-verification is newer, and a brain with no watch list
reads exactly as before."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain import reads, store, watch
from brain.applier import Applier
from brain.init import init_instance

from tests.conftest import make_packet, render_config, source_op

RULE = '[["help desk", "support line"], ["hours", "open"]]'
T0, T1, T2, T3 = (f"2026-10-0{day}T00:00:00Z" for day in (1, 2, 3, 4))
NOW = "2026-10-08T00:00:00Z"


class Clock:
    def __init__(self, monkeypatch) -> None:
        self.now = T0
        monkeypatch.setattr(store, "utc_now", lambda: self.now)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    return Clock(monkeypatch)


KEYS = ("support.hours", "support.staff")


def _inst(tmp_path: Path, rule: str | None = RULE) -> Path:
    config = render_config(fact_keys=[{"key": "support.hours", "value_type": "text"}, {"key": "support.staff", "value_type": "int"}])
    inst = Path(init_instance("demo", tmp_path, config_text=config)["instance"])
    if rule is not None:
        (inst / watch.FILE).write_text(f'["support.hours"]\nwords = {rule}\n')
    return inst


def _apply(inst: Path, *ops, agent: str = "tester"):
    writer = Applier(inst)
    try:
        outcome = writer.apply(json.dumps(make_packet(agent, *ops)).encode())
    finally:
        writer.close()
    assert outcome.receipt["status"] == "applied", (outcome.receipt, outcome.warning)
    return outcome


def _claim(claim_id: str, value: str, quote: str, source_id: str, key: str = "support.hours") -> dict:
    return {"op": "add_claim", "row": {"id": claim_id, "key": key, "value_raw": value, "basis": "reported", "quote": quote},
            "source": {"source_id": source_id, "locator": "para 1"}}


def _first(inst: Path, clock: Clock):
    """The fact's first value: open 9am to 5pm, said on 20 September, written at T0."""
    clock.now = T0
    return _apply(
        inst,
        source_op("src-a", "The help desk hours are 9am to 5pm on weekdays.", date="2026-09-20"),
        _claim("clm-a", "9am to 5pm on weekdays", "The help desk hours are 9am to 5pm on weekdays", "src-a"),
    )


def _news(inst: Path, clock: Clock):
    """A later message that changes the answer, added at T1 with no claim."""
    clock.now = T1
    return _apply(
        inst,
        source_op("src-b", "Heads up.\nThe help desk hours change in March. It will stay open until 8pm.\nLunch is at noon.",
                  kind="slack", date="2026-10-02"),
    )


def _fact(inst: Path, key: str = "support.hours") -> dict:
    result = reads.run_tool(inst, "fact_get", {"key": key}, now=NOW)
    assert not result.is_error, result.text
    return json.loads(result.text)["facts"][0]


def _brief(inst: Path) -> str:
    result = reads.run_tool(inst, "brief", {}, now=NOW)
    assert not result.is_error, result.text
    return result.text


# The rule's shape and the words it matches.


def test_parse_keeps_words_and_drops_repeats() -> None:
    assert watch.parse([["Help-Desk", "help desk"], ["Hours", "Opening Hours"]]) == (
        ("help desk",), ("hours", "opening hours"),
    )


@pytest.mark.parametrize(
    "value, words",
    [
        ("hours", "must be a list of 1 to 4 word groups"),
        ([], "must be a list of 1 to 4 word groups"),
        ([["a"]] * 5, "holds 5 groups; at most 4"),
        (["hours"], "[0] must be a list of 1 to 24 phrases"),
        ([[]], "[0] must be a list of 1 to 24 phrases"),
        ([["x"] * 25], "[0] holds 25 phrases; at most 24"),
        ([[7]], "[0][0] must be text of at most 60 characters"),
        ([["x" * 61]], "[0][0] must be text of at most 60 characters"),
        ([["--"]], "[0][0] holds no word"),
    ],
)
def test_parse_refusals_name_the_fix(value, words) -> None:
    with pytest.raises(ValueError) as caught:
        watch.parse(value)
    assert words in str(caught.value)


def test_the_list_is_read_from_its_own_file(tmp_path: Path) -> None:
    assert watch.load(tmp_path, KEYS) == ({}, [])  # no file: nothing watched, nothing wrong
    (tmp_path / watch.FILE).write_text(f'["support.hours"]\nwords = {RULE}\n\n[support.staff]\nwords = [["staff"]]\n')
    assert watch.load(tmp_path, KEYS) == (
        {"support.hours": (("help desk", "support line"), ("hours", "open")), "support.staff": (("staff",),)},
        [],
    )


def test_what_is_wrong_with_the_list_is_named_and_the_rest_still_applies(tmp_path: Path) -> None:
    (tmp_path / watch.FILE).write_text(
        'loose = 1\n'
        '["support.hours"]\nwords = [["hours"]]\n'
        '["support.staff"]\nwords = [["staff"], []]\n'
        '["support.hurs"]\nwords = [["hours"]]\n'
    )
    rules, problems = watch.load(tmp_path, KEYS)
    assert rules == {"support.hours": (("hours",),)}
    assert problems == [
        'watch.toml: loose sits outside a table; write ["<fact key>"] then words = [[...]]',
        "watch.toml: support.staff.words [1] must be a list of 1 to 24 phrases, such as "
        '[["help desk", "support line"], ["hours", "open"]]',
        "watch.toml: support.hurs is not a fact key in brain.toml, so it is not watched",
    ]
    (tmp_path / watch.FILE).write_text('["support.hours"]\nwords = [["hours"]]\nnote = "why"\n')
    assert watch.load(tmp_path, KEYS) == (
        {}, ["watch.toml: support.hours.note is not a setting; a watched fact takes words"],
    )
    (tmp_path / watch.FILE).write_text("[support.hours\nwords = ")
    rules, problems = watch.load(tmp_path, KEYS)
    assert rules == {} and len(problems) == 1
    assert problems[0].startswith("watch.toml is not valid TOML (") and problems[0].endswith("so no fact is watched")


def test_a_broken_list_never_stops_a_read_or_a_write(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    (inst / watch.FILE).write_text('["support.hours"]\nwords = "hours"\n')
    assert _news(inst, clock).warning == ""
    assert "watch" not in _fact(inst)["claims"]
    assert "watch.toml has 1 entry to fix. Run brain check." in _brief(inst)
    from brain import cli

    assert cli.main(["check", "--instance", str(inst)]) == 0


def test_brain_check_names_the_entry(tmp_path: Path, capsys) -> None:
    inst = _inst(tmp_path, rule='"hours"')
    from brain import cli

    assert cli.main(["check", "--instance", str(inst)]) == 0
    captured = capsys.readouterr()
    assert '"ok": true' in captured.out
    assert "brain: watch.toml: support.hours.words must be a list of 1 to 4 word groups" in captured.err


def test_mentions_needs_every_group_in_one_paragraph() -> None:
    rule = watch.parse([["the help desk"], ["hours"]])
    assert watch.mentions(rule, "Note", "When is the help desk open? The hours are on the door.")
    assert not watch.mentions(rule, "Note", "The help desk is friendly.\nThe hours are on the door.")
    # A hard-wrapped paragraph is one paragraph, and the title is one too.
    assert watch.mentions(rule, "Note", "We changed the help desk\nhours last week.")
    assert watch.mentions(rule, "The help desk and its hours", "Nothing else.")


def test_mentions_matches_whole_words_whatever_the_case_or_marks() -> None:
    rule = watch.parse([["host"], ["cafe"]])
    assert watch.mentions(rule, "", "The CAFÉ will host it.")
    assert not watch.mentions(rule, "", "The cafe has a ghost.")
    assert not watch.mentions(rule, "", "Hosting at the cafe.")


def test_a_phrase_reads_as_search_reads_it() -> None:
    for text in ("Help-Desk v2", "opening_hours", "Café's 2nd host", "support   line\thours"):
        assert watch.phrase(text) == " ".join(reads._WORD.findall(reads._fold(text)))


# What a writer and a reader see.


def test_the_receipt_names_the_fact_when_a_source_mentions_it(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    assert _first(inst, clock).warning == ""  # the packet wrote the fact itself
    assert _news(inst, clock).warning == (
        "source src-b mentions watched fact support.hours (now 9am to 5pm on weekdays, as of 2026-09-20): send "
        "add_claim on this key if the source changes the value, or upsert_fact with last_verified_at if the value stands"
    )


def test_a_dry_run_says_the_same_and_keeps_nothing(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    clock.now = T1
    writer = Applier(inst)
    try:
        packet = make_packet("tester", source_op("src-b", "The help desk hours changed.", date="2026-10-02"))
        outcome = writer.apply(json.dumps(packet).encode(), dry_run=True)
    finally:
        writer.close()
    assert outcome.warning.startswith("source src-b mentions watched fact support.hours")
    assert _fact(inst)["claims"]["watch"] == {"newer_sources": 0}


def test_fact_get_lists_the_newer_sources_and_what_to_send(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    assert _fact(inst)["claims"]["watch"] == {"newer_sources": 0}
    _news(inst, clock)
    assert _fact(inst)["claims"]["watch"] == {
        "newer_sources": 1, "latest": "2026-10-02", "sources": ["src-b"], "next": watch.NEXT,
    }
    listed = json.loads(reads.run_tool(inst, "fact_get", {"key": "support.*"}, now=NOW).text)["facts"]
    assert [item["claims"].get("watch", {}).get("newer_sources") for item in listed] == [1]
    # get on the fact, as an agent opens a search hit, says the same.
    opened = json.loads(reads.run_tool(inst, "get", {"entity": "fact", "id": "support.hours"}, now=NOW).text)
    assert opened["related"]["watch"] == _fact(inst)["claims"]["watch"]
    other = json.loads(reads.run_tool(inst, "get", {"entity": "source", "id": "src-b"}, now=NOW).text)
    assert "watch" not in other["related"]


def test_search_marks_the_fact_and_the_brief_lists_it(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    assert "watched" not in _brief(inst)
    _news(inst, clock)
    found = json.loads(reads.run_tool(inst, "search", {"query": "hours", "entities": ["fact"]}, now=NOW).text)
    assert [(hit["id"], hit.get("newer_sources")) for hit in found["hits"]] == [("support.hours", 1)]
    assert "1 watched fact has newer sources: support.hours (1, latest 2026-10-02). fact_get lists them." in _brief(inst)


def test_only_news_counts(tmp_path: Path, clock: Clock) -> None:
    """Not a source in two paragraphs, one dated before the value, or one a claim on the key cites."""
    inst = _inst(tmp_path)
    _first(inst, clock)
    clock.now = T1
    outcome = _apply(
        inst,
        source_op("src-split", "The help desk is friendly.\nThe hours are on the door.", date="2026-10-02"),
        source_op("src-old", "Back then the help desk hours were 10am to 4pm.", date="2026-09-01"),
        source_op("src-other", "The kettle is descaled monthly.", date="2026-10-02"),
    )
    assert outcome.warning == ""
    assert _fact(inst)["claims"]["watch"] == {"newer_sources": 0}


def test_a_claim_on_the_key_clears_it(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    _news(inst, clock)
    clock.now = T2
    outcome = _apply(inst, _claim("clm-b", "open until 8pm", "It will stay open until 8pm", "src-b"))
    assert "watched fact" not in outcome.warning
    assert _fact(inst)["claims"]["watch"] == {"newer_sources": 0}
    assert "watched" not in _brief(inst)


def test_a_reverification_clears_it(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    clock.now = T1
    _apply(inst, source_op("src-same", "Confirmed today: the help desk hours are still 9am to 5pm on weekdays.", date="2026-10-02"))
    fact = _fact(inst)
    assert fact["claims"]["watch"]["sources"] == ["src-same"]
    clock.now = T2
    outcome = _apply(inst, {"op": "upsert_fact", "row": {"key": "support.hours", "last_verified_at": T2},
                            "source": {"source_id": "src-same", "locator": "para 1"}, "base": fact["version"]})
    assert outcome.warning == ""
    after = _fact(inst)
    assert (after["value"], after["last_verified_at"], after["claims"]["watch"]) == (
        "9am to 5pm on weekdays", T2, {"newer_sources": 0},
    )
    # A later message counts again.
    clock.now = T3
    assert "watched fact support.hours" in _apply(
        inst, source_op("src-later", "The help desk hours change again in June.", date="2026-10-04")
    ).warning
    assert _fact(inst)["claims"]["watch"]["sources"] == ["src-later"]


def test_a_source_and_its_claim_in_one_packet_say_nothing(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    clock.now = T1
    outcome = _apply(
        inst,
        source_op("src-b", "The help desk hours are now 8am to 8pm.", date="2026-10-02"),
        _claim("clm-b", "8am to 8pm", "The help desk hours are now 8am to 8pm", "src-b"),
    )
    assert outcome.warning == ""
    assert _fact(inst)["claims"]["watch"] == {"newer_sources": 0}


def test_a_watched_fact_with_no_value_yet_is_still_named(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    clock.now = T1
    outcome = _apply(inst, source_op("src-b", "The help desk will open at 9am.", date="2026-10-02"))
    assert "mentions watched fact support.hours (no value yet)" in outcome.warning
    assert "1 watched fact has newer sources: support.hours (1, latest 2026-10-02)." in _brief(inst)


def test_many_mentions_are_capped_in_the_receipt(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    clock.now = T1
    ops = [source_op(f"src-n{i}", f"Note {i}: the help desk hours.", date="2026-10-02") for i in range(7)]
    notes = _apply(inst, *ops).warning.split("; ")
    assert len(notes) == 6
    assert notes[-1] == "2 more mentions of watched facts are left out here, and the brief lists each fact"


# A brain with no rule, and a folder token, read as before.


def test_without_a_list_nothing_is_added(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path, rule=None)
    _first(inst, clock)
    assert _news(inst, clock).warning == ""
    assert "watch" not in _fact(inst)["claims"]
    assert "watch" not in json.loads(reads.run_tool(inst, "get", {"entity": "fact", "id": "support.hours"}, now=NOW).text)["related"]
    found = json.loads(reads.run_tool(inst, "search", {"query": "hours", "entities": ["fact"]}, now=NOW).text)
    assert all("newer_sources" not in hit for hit in found["hits"])
    assert "watched" not in _brief(inst)


def test_a_folder_token_reads_as_before(tmp_path: Path, clock: Clock) -> None:
    inst = _inst(tmp_path)
    _first(inst, clock)
    _news(inst, clock)
    scoped = reads.run_tool(inst, "search", {"query": "hours"}, now=NOW, folders=("general",))
    assert not scoped.is_error, scoped.text
    assert all("newer_sources" not in hit for hit in json.loads(scoped.text)["hits"])
    assert "watched" not in reads.run_tool(inst, "brief", {}, now=NOW, folders=("general",)).text
