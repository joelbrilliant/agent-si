"""The evidence gate refuses text that puts words or a decision in the principal's mouth, and its message tells
the agent what would work, so the same agent does not repeat the sentence.

The corpus below holds invented sentences about a community garden run by Avery Example. Each carries the verdict
the gate should give:

- REFUSE: it states or implies what the principal said, decided, wants, asked, approved or will approve.
- REFUSE_STRICT: arguably a false positive, because the verb belongs to someone else, but the break before it
  is a comma or a bracket, not the colon, semicolon or sentence end this fix narrows. They stay refused on
  purpose, so loosening them is a deliberate change to this list.
- PASS: the principal is the addressee, or the object of a contrast, and someone else's verb follows a colon."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from brain import evidence_gate
from brain.cli import main
from brain.evidence_gate import CitedSource, Principal, attributes_to, flag_text
from brain.init import init_instance
from tests.conftest import config_with, make_packet, source_op, upsert_op

NAMES = ("avery", "avery example")
# brain.toml allows single-word names only; the gate corpus above also tries the two-word one.
AVERY = {"person_id": "per-avery", "display_name": "Avery Example", "names": ["Avery"]}
PRINCIPAL = Principal("per-avery", "Avery Example", NAMES)
PLAIN = CitedSource("src-plain", "Notes from the working party. Nothing here about approvals.", None)

# Invented sentences. Only the grammar that triggers the gate matters here.
REFUSE = [
    "Avery asked Bo on Saturday to water the bean rows.",
    "Avery promised a seed swap, which is now a table at the plant sale.",
    "Avery promised a rota, which is now redrawn around the new beds.",
    "Avery asked Cleo by text to move the compost bins.",
    "Avery told Dev on the phone (Esme, Finn and Gus listening) that the tool shed opens late.",
    "When the seedlings reach 60 of 70 trays, Avery tells Hana in the group chat that planting can start.",
    "Avery told Ivo and Jun at the working party that the fence could take a week.",
    "Needs Avery's call on the gate latch, for example moving the water butt.",
    "Avery asked Kit to sweep the greenhouse path before the open day.",
    "Avery told Finn he'll get Gus to label the rhubarb.",
    "Finn's wheelbarrow has a flat tyre, so Avery asks him to pump it up.",
    "Avery asked Esme for a written list, no call.",
    "Avery's Sunday note asked Hana to chase the delivery along with the trowel order.",
    "Avery told the watering group and edited the earlier message to drop the spare hose.",
    "Sources: Avery's words at the spring working party; Ivo's message about the sheds.",
    # "accepted" is Avery's own verb (the invitation was accepted), and it comes before the semicolon.
    "Open morning rota (Avery optional, accepted; Jun asks to swap shifts).",
    "Avery approved adding them, but the shed padlock won't open either door, so they wait.",
    "Avery asked Cleo on Tuesday to get both bean varieties sorted by the plant sale.",
    "He relayed Avery's view from the tea break that most tomatoes are out for now.",
    "Bo doubts more posters near the bus stop will help and relayed Avery's view on it.",
    "Esme's draft stall map read; feedback is Avery's call, the path widths still open.",
    "Avery approved on Monday and asked Dev for a spare key to Gus's padlock.",
    "Avery asked Hana by text on Wednesday and it was unanswered the next day.",
    "Avery decides whether to raise it with Ivo and Jun, who can say whether the soil supports it.",
    "Avery asked Kit whether the offcut is for the raised bed or the herb spiral.",
    "Avery needs no prep for the open day on Sunday, which is Pat's.",
    "Bed 6: Avery's email asks for the digging to be someone Bo supervises within a week.",
    "On Friday Avery told him in the garden chat group that his assigned bed was done.",
    "Label as is, or ask Cole to add the other herbs first, is Avery's call.",
    "Finn asked for the rota at the Saturday meeting and Avery said he'd share it later.",
    "Avery reported Jun has approved them (the new compost rules).",
    "Avery asked Cleo by text about seed orders and slug traps.",
    "Avery asked for a count of the seed tins after noticing a gap.",
    "BEAN TEST: Avery decided the poles should go up today.",
    "- **Helper.** Avery asked the new volunteer Hana Reed to cover the week.",
    "- **Supplier.** Avery told Finn what the delivery order needs for the compost.",
    "Avery said he was taking no orders unless told otherwise, and the seed tin was locked.",
    # More phrasings built around the attributing verbs.
    "Next: open the stall once Avery approves the plan.",
    "The plant sale goes ahead once Avery approves it.",
    "Avery wants the seed list kept short.",
    "Avery decided to move the tools into the shed.",
    "Start the digging when Avery says the soil is ready.",
    "The sale date is Avery's call.",
    "According to Avery the sowing waits.",
    # NAME_COLON: the name as a label at the start of a line stays refused.
    "Avery: approved the seed order.",
    "Avery Example: approved the seed order.",
    "- Avery: approved",
    "- **Avery:** approved the seed order.",
]

REFUSE_STRICT = [
    # "when a path needs one" is the path's verb, but a clause break by comma or "when" is not narrowed.
    "Other beds are dug by Avery himself when a path needs one, about three a week.",
    # "the printer was asked" is not Avery's, but the break is a comma.
    "A reply to Sunny's flyer email is with Avery, and the printer was asked if it is ready.",
    # "now asks" belongs to the note, but the break is a closing bracket.
    "The follow-up note to them (tin 12, Avery's edits kept) now asks for a date.",
]

# (sentence, why it passes). Only a colon, semicolon or sentence end after the name moves the verb to someone else.
PASS = [
    (
        'Open question for Avery: Bo wrote on Friday "Hi Avery. Rows 1 and 2 have been weeded',
        "Avery is the addressee of the question; 'wrote' is Bo's, after the colon. The only other 'Avery' is "
        "inside Bo's quote and ends its clause at a full stop.",
    ),
    (
        "Her spade goes to Pat, not Avery: Kit cleans it and Pat confirms it's cleaned and returned.",
        "Avery is the object of a contrast; 'confirms' is Pat's, after the colon.",
    ),
    (
        "Waiting on Avery; Cleo approved the rota on Friday.",
        "Synthetic variant of the same shape with a semicolon: 'approved' is Cleo's.",
    ),
    (
        "Open question for Avery: Jun approved the stall map on Friday.",
        "Synthetic variant of the first sentence with a verb from the list: 'approved' is Jun's.",
    ),
]

# What the message tells agents to write instead.
NEUTRAL = [
    "Next: open the stall after approval of the plan.",
    "The plant sale goes ahead pending a decision.",
    "Use the agreed wording on the sign.",
    "Bo said the plan is ready.",
    "Avery is the owner of the shed key.",
]


def _flags(sentence: str, source: CitedSource = PLAIN) -> list[evidence_gate.Flag]:
    return flag_text([sentence], "", source, PRINCIPAL)


def _attributes(sentence: str) -> bool:
    return attributes_to(sentence, NAMES, labels=(PRINCIPAL.display_name,))


@pytest.mark.parametrize("sentence", REFUSE + REFUSE_STRICT)
def test_sentences_that_state_or_imply_the_principals_words_are_refused(sentence: str) -> None:
    assert _attributes(sentence), sentence
    flags = _flags(sentence)
    assert [flag.reasons for flag in flags] == [("unquoted_attribution",)], sentence


@pytest.mark.parametrize("sentence, why", PASS, ids=[sentence[:40] for sentence, _ in PASS])
def test_sentences_whose_verb_belongs_to_someone_else_pass(sentence: str, why: str) -> None:
    assert why
    assert not _attributes(sentence), sentence
    assert _flags(sentence) == []


@pytest.mark.parametrize("sentence", NEUTRAL)
def test_the_neutral_rewordings_the_message_suggests_pass(sentence: str) -> None:
    assert _flags(sentence) == []


def test_quoting_five_words_from_a_source_the_principal_wrote_passes() -> None:
    source = CitedSource("src-email-avery", "Bo, please stack the pots by the shed door before Friday.", "per-avery")
    sentence = 'Avery asked Bo to "stack the pots by the shed door before Friday".'
    assert _flags(sentence) != [] and _flags(sentence, PLAIN) != []
    assert _flags(sentence, source) == []


def test_the_same_words_from_a_source_someone_else_wrote_do_not_pass() -> None:
    other = CitedSource("src-email-bo", "Bo, please stack the pots by the shed door before Friday.", "per-bo")
    sentence = 'Avery asked Bo to "stack the pots by the shed door before Friday".'
    assert [flag.reasons for flag in _flags(sentence, other)] == [("unquoted_attribution",)]


# Speaker lines: a transcript has several speakers, so it has no author, and the principal's own lines in it are a
# speaker-lines source they wrote. The lines are invented: Ari asked and Avery answered.
STANDUP = CitedSource(
    "src-working-party-transcript-1ab8d113",
    'Ari Sample: "So you can\'t split the seed packets with the other garden?" Avery Example: "No, no, '
    'no, because they\'re sealed." and "there is a spare packet" and "they also all have sowing dates too. So that\'s '
    'another thing that you would have to look into."',
    None,
)
HIS_LINES = "No, no, no, because they're sealed.\n\nthere is a spare packet\n\nthey also all have sowing dates too. So that's " \
    "another thing that you would have to look into."


def test_his_own_line_in_a_transcript_passes_through_his_lines_and_nobody_elses_does() -> None:
    lines = CitedSource("src-lines-working-party", HIS_LINES, "per-avery", by_paragraph=True)
    his = 'Avery told Ari "they also all have sowing dates too" about seed packets.'
    andrews = 'Avery said "you can\'t split the seed packets with the other garden".'
    assert [flag.reasons for flag in _flags(his, STANDUP)] == [("unquoted_attribution",)]
    assert flag_text([his], "", [STANDUP, lines], PRINCIPAL) == []
    assert [flag.reasons for flag in flag_text([andrews], "", [STANDUP, lines], PRINCIPAL)] == [("unquoted_attribution",)]


def test_a_quote_cannot_join_two_of_his_lines() -> None:
    """Read as one run of words, his lines would hold "because they're sealed there is a spare packet", which he
    never said in one breath: the transcript has "and" between them."""
    joined = 'Avery said "because they\'re sealed. there is a spare packet".'
    lines = CitedSource("src-lines-working-party", HIS_LINES, "per-avery", by_paragraph=True)
    assert [flag.reasons for flag in flag_text([joined], "", [STANDUP, lines], PRINCIPAL)] == [("quote_not_in_source",)]
    whole = CitedSource("src-lines-working-party", HIS_LINES, "per-avery")
    assert flag_text([joined], "", [STANDUP, whole], PRINCIPAL) == []


# -- the clause break: only a colon, semicolon or sentence end after the name ends its verb window -------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "Avery B. Smith approved the plan.",  # an initial is not a sentence end
        "Avery, i.e. the owner, approved the plan.",  # nor is a lower-case abbreviation
        "Avery at 7.15pm approved the plan.",  # nor a full stop inside a time
        "Avery Jr. Smith approved the plan.",
        "Avery, who said no, replied: approved.",  # a colon after the verb does not hide the verb
        "Bo wrote: Avery approved the plan.",  # a colon before the name changes nothing
        "Avery approved; Bo did not.",
        "Avery: Bo, approved.",  # NAME_COLON at the start of the line
    ],
)
def test_a_clause_break_does_not_hide_a_verb_before_it_or_a_break_that_is_not_one(sentence: str) -> None:
    assert _attributes(sentence), sentence


@pytest.mark.parametrize(
    "sentence",
    [
        "Question for Avery; Bo approved the access.",
        "Sent to Avery. Bo confirmed receipt.",
        "Is this for Avery? Bo said yes.",
        "Ask Avery: Bo decided already.",
    ],
)
def test_each_break_kind_ends_the_window(sentence: str) -> None:
    assert not _attributes(sentence), sentence


# -- the message -----------------------------------------------------------------------------------------------------


def _message(sentence: str) -> str:
    return flag_text([sentence], "", PLAIN, PRINCIPAL)[0].messages[0]


def test_the_refusal_names_both_routes_that_work() -> None:
    sentence = "Next: open the stall once Avery approves the plan."
    message = _message(sentence)
    assert message.startswith(f'"{sentence}" attributes words or a decision to Avery Example. ')
    assert "quote at least 5 consecutive words from an existing source authored by per-avery" in message
    assert "collected email, Slack message or transcript" in message
    assert "reword neutrally" in message
    for example in ('"after approval"', '"pending a decision"', '"the agreed wording"'):
        assert example in message
    assert "name who said it" in message
    assert len(message) - len(sentence) - 2 < 320


# -- the same message through `brain validate packet --instance` and `brain apply` ----------------------------------


def _cli(*argv) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main([str(part) for part in argv])
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def avery(tmp_path: Path) -> Path:
    return Path(init_instance("demo", tmp_path, config_text=config_with(principal=AVERY))["instance"])


def test_validate_gives_an_agent_the_routes_for_a_status_note(avery, tmp_path) -> None:
    sentence = "Next: open the stall once Avery approves the plan."
    packet = make_packet(
        "tester",
        source_op("src-note", "Notes from the working party. Nothing here about approvals."),
        upsert_op("upsert_record", {"id": "rec-cp", "kind": "note", "title": "Status note", "body": sentence}, "src-note"),
    )
    path = tmp_path / "note.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    code, out, _ = _cli("validate", "packet", path, "--instance", avery)
    report = json.loads(out)
    assert code == 1 and report["status"] == "rejected"
    assert report["error"] == "op 1 (upsert_record): evidence gate: " + _message(sentence)
    assert "reword neutrally" in report["error"] and "existing source authored by per-avery" in report["error"]


def test_an_agent_that_adds_a_source_the_principal_wrote_is_told_what_works(avery, tmp_path) -> None:
    packet = make_packet(
        "plain",
        source_op("src-fake", "Please stack the pots by the shed door before Friday.", kind="email", author="per-avery"),
    )
    path = tmp_path / "source.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    code, out, _ = _cli("validate", "packet", path, "--instance", avery)
    error = json.loads(out)["error"]
    assert code == 1
    assert "agent plain may not add a source authored by the principal." in error
    assert "Avery Example (per-avery)" in error and "collected email, Slack message or transcript" in error
    assert "without author_person_id" in error


# A break only ends the window when a new subject starts after it. The first
# build let "Notes, Avery: approved" pass, which reads as Avery approving. These must stay refused.
@pytest.mark.parametrize("sentence", [
    "Notes, Avery: approved the plan.",
    "Notes, Avery: Approved.",
    "Status for Avery; approved by the board after Avery asked.",
    "Summary for Avery: said yes to the draft.",
])
def test_a_label_after_the_name_still_carries_his_verb(sentence: str) -> None:
    assert attributes_to(sentence, NAMES), sentence


# A sentence shape that 1.6.0 stopped flagging, because the capitalised pronoun after the colon looked like a new
# subject. "He" is Avery, so it refuses.
@pytest.mark.parametrize("sentence", [
    "AVERY SEEKS THE CONCRETE ASK: He wants the distinction between the first bed plan and a new layout.",
    "Avery's position; He decided to drop the pumpkins.",
])
def test_a_pronoun_after_the_break_still_carries_his_verb(sentence: str) -> None:
    assert attributes_to(sentence, NAMES), sentence
