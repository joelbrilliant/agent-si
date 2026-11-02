from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from brain.cli import main
from brain.evidence_gate import (
    attributes_to,
    contains,
    join_wrapped,
    match_key,
    normalise,
    quoted_spans,
    sentences,
)
from brain.init import init_instance
from tests.collector_fixtures import collector_instance
from tests.conftest import attribution_refusal, config_with, make_packet, ro_rows, seed, source_op, upsert_op


def _record(body: str, source_id: str, record_id: str = "rec-quote") -> dict:
    return upsert_op(
        "upsert_record",
        {"id": record_id, "kind": "note", "title": "Note", "body": body},
        source_id,
    )


def test_gate_rejects_a_quote_that_is_not_in_its_cited_source(instance, apply) -> None:
    before_sources = ro_rows(instance, "SELECT count(*) FROM sources")[0][0]
    before_records = ro_rows(instance, "SELECT count(*) FROM records")[0][0]
    outcome = apply(
        make_packet(
            "tester",
            source_op("src-a", "Dana asked whether the launch review can move to Thursday."),
            _record('Dana wrote "the launch review is cancelled for good".', "src-a"),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "is not in the cited source src-a" in outcome.receipt["error"]
    assert ro_rows(instance, "SELECT count(*) FROM sources")[0][0] == before_sources
    assert ro_rows(instance, "SELECT count(*) FROM records")[0][0] == before_records


def test_gate_rejects_a_quote_found_only_in_an_uncited_source(instance, apply) -> None:
    seed(apply)
    from tests.conftest import ro_rows

    before_records = ro_rows(instance, "SELECT count(*) FROM records")[0][0]
    outcome = apply(
        make_packet(
            "tester",
            _record('Dana asked "move the launch review to Thursday afternoon".', "src-note-anon"),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert ro_rows(instance, "SELECT count(*) FROM records")[0][0] == before_records


def test_gate_accepts_a_verbatim_quote_from_the_cited_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            _record('Dana asked "move the launch review to Thursday afternoon".', "src-email-dana"),
        )
    )
    assert outcome.receipt["status"] == "applied"


def test_gate_rejects_jade_said_without_a_quote(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            _record("Jade said we should drop the access form.", "src-note-jade"),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "attributes words or a decision to Jade" in outcome.receipt["error"]


def test_gate_rejects_jade_said_with_fewer_than_five_quoted_words(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            _record('Jade said "cut the access form".', "src-note-jade"),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "attributes words or a decision to Jade" in outcome.receipt["error"]


def test_gate_rejects_jade_said_quoting_someone_elses_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            source_op(
                "src-email-dana2",
                "We will cut the access form from the onboarding pack this week.",
                kind="email",
                author="per-dana-okafor",
            ),
            _record(
                'Jade said "cut the access form from the onboarding pack".',
                "src-email-dana2",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"


def test_gate_rejects_jade_said_quoting_an_unattributed_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            _record(
                'Jade said "cut the access form from the onboarding pack".',
                "src-note-anon",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"


def test_gate_accepts_jade_said_with_five_words_from_his_own_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            _record(
                'Jade said "cut the access form from the onboarding pack".',
                "src-note-jade",
            ),
        )
    )
    assert outcome.receipt["status"] == "applied"


def test_attributed_principal_is_configurable_per_instance(tmp_path: Path) -> None:
    from brain.applier import Applier
    import json

    cfg = config_with(
        principal={
            "person_id": "per-morgan-vale",
            "display_name": "Morgan Vale",
            "names": ["Morgan"],
            "aliases": [],
        }
    )
    path = Path(init_instance("demo", tmp_path, config_text=cfg)["instance"])
    writer = Applier(path)
    try:
        def apply(packet):
            return writer.apply(json.dumps(packet).encode())

        apply(
            make_packet(
                "tester",
                source_op(
                    "src-note-anon",
                    "We will cut the access form from the onboarding pack this week.",
                ),
            )
        )
        rejected = apply(
            make_packet(
                "tester",
                _record("Morgan decided we ship on Friday.", "src-note-anon", "rec-m"),
            )
        )
        accepted = apply(
            make_packet(
                "tester",
                _record("Jade said we ship on Friday.", "src-note-anon", "rec-j"),
            )
        )
    finally:
        writer.close()
    assert rejected.receipt["status"] == "rejected"
    assert accepted.receipt["status"] == "applied"


def test_rule_two_is_off_when_no_principal_is_configured(tmp_path: Path) -> None:
    from brain.applier import Applier
    import json

    cfg = config_with(principal=False)
    path = Path(init_instance("demo", tmp_path, config_text=cfg)["instance"])
    writer = Applier(path)
    try:
        def apply(packet):
            return writer.apply(json.dumps(packet).encode())

        apply(
            make_packet(
                "tester",
                source_op("src-note-anon", "We will cut the access form from the onboarding pack this week."),
            )
        )
        accepted = apply(
            make_packet("tester", _record("Jade said we ship on Friday.", "src-note-anon", "rec-j"))
        )
        rejected = apply(
            make_packet(
                "tester",
                _record('Someone wrote "this quote is not in the source at all".', "src-note-anon", "rec-b"),
            )
        )
    finally:
        writer.close()
    assert accepted.receipt["status"] == "applied"
    assert rejected.receipt["status"] == "rejected"


def test_decision_quote_must_appear_in_the_cited_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_decision",
                {
                    "id": "dec-one",
                    "title": "Move the review",
                    "quote": "this quote is not in any source at all",
                    "decided_on": "2026-10-01",
                    "decided_by_person_id": "per-dana-okafor",
                    "status": "in_effect",
                    "project_id": "general",
                },
                "src-email-dana",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "decision quote is not in the cited source" in outcome.receipt["error"]


def test_principal_decision_needs_a_principal_authored_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_decision",
                {
                    "id": "dec-two",
                    "title": "Cut the form",
                    "quote": "cut the access form from the onboarding pack",
                    "decided_on": "2026-10-01",
                    "decided_by_person_id": "per-jade",
                    "status": "in_effect",
                    "project_id": "general",
                },
                "src-note-anon",
            ),
        )
    )
    assert outcome.receipt["status"] == "rejected"
    assert "needs its quote from a source authored by" in outcome.receipt["error"]


def test_agent_without_author_principal_cannot_add_a_principal_source(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "plain",
            source_op("src-jade-fake", "Jade wrote this himself in a note.", author="per-jade"),
        )
    )
    assert outcome.receipt["status"] == "rejected"


def test_only_new_sentences_are_checked(apply) -> None:
    seed(apply)
    first = apply(
        make_packet(
            "tester",
            _record('Dana asked "move the launch review to Thursday afternoon".', "src-email-dana"),
        )
    )
    assert first.receipt["status"] == "applied"
    second = apply(
        make_packet(
            "tester",
            upsert_op(
                "upsert_record",
                {
                    "id": "rec-quote",
                    "body": (
                        'Dana asked "move the launch review to Thursday afternoon". '
                        'Jade said "cut the access form from the onboarding pack".'
                    ),
                },
                "src-note-jade",
                base=1,
            ),
        )
    )
    assert second.receipt["status"] == "applied"


def test_two_sources_in_one_packet_by_successive_ops(apply) -> None:
    seed(apply)
    outcome = apply(
        make_packet(
            "tester",
            _record('Dana asked "move the launch review to Thursday afternoon".', "src-email-dana"),
            upsert_op(
                "upsert_record",
                {
                    "id": "rec-quote",
                    "body": (
                        'Dana asked "move the launch review to Thursday afternoon". '
                        'Jade said "cut the access form from the onboarding pack".'
                    ),
                },
                "src-note-jade",
                base=1,
            ),
        )
    )
    assert outcome.receipt["status"] == "applied"


def test_normalise_handles_curly_quotes_and_whitespace() -> None:
    assert normalise("  Hello,\nthere!  ") == "hello, there"
    assert "hello" in normalise("“Hello”")


def test_ellipsis_is_a_boundary_not_an_omission() -> None:
    source = match_key("cut the access request form")
    span = match_key("cut the access ... form")
    assert not contains(source, span)
    assert contains(match_key("cut the access form"), match_key("cut the access ... form"))


def test_quotes_match_whole_words_only() -> None:
    assert not contains(match_key("from the package"), match_key("from the pack"))


def test_quotes_under_three_words_are_not_checked() -> None:
    assert len(match_key("access form").split()) == 2


def test_possessives_do_not_open_or_close_a_quote() -> None:
    assert quoted_spans("Jade's laptop is new") == []


def test_full_stops_inside_quotes_do_not_split_sentences() -> None:
    parts = sentences('She said "stop. now." and left.')
    assert any("stop. now." in part for part in parts)
    assert len(parts) == 1 or any("and left" in part for part in parts)


def test_wrapped_lines_are_joined() -> None:
    joined = join_wrapped("This line continues\non the next line.")
    assert len(joined) == 1
    assert "continues on the next" in joined[0]


@pytest.mark.parametrize(
    "sentence",
    [
        "Jade said we should wait",
        "Jade, after a long call, decided to wait",
        "according to Jade we wait",
        "per Jade we wait",
        "Jade's call",
        "Jade signed off",
    ],
)
def test_attribution_phrases(sentence: str) -> None:
    assert attributes_to(sentence, ["Jade"])


@pytest.mark.parametrize("sentence", ["Jade's laptop is new", "We told Jade"])
def test_non_attributions(sentence: str) -> None:
    assert not attributes_to(sentence, ["Jade"])


def _apply_packet(instance: Path, packet: dict) -> tuple[int, dict, str]:
    path = instance.parent / f"{packet['change_id']}.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["apply", "--instance", str(instance), str(path)])
    return code, json.loads(stdout.getvalue()), stderr.getvalue()


def _gate_error(sentence: str) -> str:
    return "op 0 (upsert_action): evidence gate: " + attribution_refusal(sentence)


def _action(sentence: str, source_id: str, change_id: str, action_id: str) -> dict:
    return make_packet(
        "plain",
        upsert_op(
            "upsert_action",
            {
                "id": action_id,
                "title": "Check the launch plan",
                "detail": sentence,
                "status": "open",
                "status_basis": "reported",
                "project_id": "general",
                "last_verified_at": "2026-10-06T00:00:00Z",
            },
            source_id,
        ),
        change_id=change_id,
    )


@pytest.fixture
def m3_instance(tmp_path: Path) -> Path:
    instance = collector_instance(tmp_path)
    code, payload, err = _apply_packet(
        instance,
        make_packet(
            "tester",
            source_op(
                "src-alex",
                "We will drop the nightly backups from the old server on Friday.",
                author="per-alex-example",
            ),
            source_op("src-other", "Team notes about the launch and the backups."),
            change_id="tester-m3-sources",
        ),
    )
    assert code == 0, (payload, err)
    return instance


_M3_PAIRS = [
    (
        "wrote",
        "Alex wrote that the backups stop on Friday.",
        'Alex wrote that "we will drop the nightly backups" on Friday.',
    ),
    (
        "replied",
        "Alex replied that the backups stop on Friday.",
        'Alex replied that "we will drop the nightly backups" on Friday.',
    ),
    (
        "thinks",
        "Alex thinks the backups can stop on Friday.",
        'Alex thinks "we will drop the nightly backups" on Friday.',
    ),
    (
        "suggested",
        "Alex suggested stopping the backups on Friday.",
        'Alex suggested "we will drop the nightly backups" on Friday.',
    ),
    (
        "requested",
        "Alex requested a stop to the backups on Friday.",
        'Alex requested this: "we will drop the nightly backups" on Friday.',
    ),
    (
        "mentioned",
        "Alex mentioned the backups stop on Friday.",
        'Alex mentioned "we will drop the nightly backups" on Friday.',
    ),
    (
        "name-colon",
        "Alex: drop the backups.",
        'Alex: "we will drop the nightly backups" on Friday.',
    ),
    (
        "preference",
        "Alex's preference is to drop the backups.",
        'Alex\'s preference is "we will drop the nightly backups" on Friday.',
    ),
]


@pytest.mark.parametrize(
    ("form", "rejected_sentence", "applied_sentence"),
    _M3_PAIRS,
    ids=[item[0] for item in _M3_PAIRS],
)
def test_m3_rule_2_catches_every_attribution_form(
    m3_instance: Path, form: str, rejected_sentence: str, applied_sentence: str
) -> None:
    assert attributes_to(rejected_sentence, ["Alex"])
    assert attributes_to(applied_sentence, ["Alex"])
    code, payload, err = _apply_packet(
        m3_instance,
        _action(rejected_sentence, "src-other", f"plain-m3-{form}-reject", f"act-m3-{form}-reject"),
    )
    assert code == 1, (payload, err)
    assert payload["status"] == "rejected"
    assert payload["error"] == _gate_error(rejected_sentence)
    code, payload, err = _apply_packet(
        m3_instance,
        _action(applied_sentence, "src-alex", f"plain-m3-{form}-apply", f"act-m3-{form}-apply"),
    )
    assert code == 0, (payload, err)
    assert payload["status"] == "applied"


_M3_TENSES = [
    "Alex writes that the backups stop.",
    "Alex replies that the backups stop.",
    "Alex thought the backups could stop.",
    "Alex suggests stopping the backups.",
    "Alex requests a stop to the backups.",
    "Alex mentions the backups stop.",
]


@pytest.mark.parametrize("sentence", _M3_TENSES)
def test_m3_other_tenses_are_attributions(m3_instance: Path, sentence: str) -> None:
    assert attributes_to(sentence, ["Alex"])
    slug = "".join(ch if ch.isalnum() else "-" for ch in sentence.lower()).strip("-")
    code, payload, err = _apply_packet(
        m3_instance,
        _action(sentence, "src-other", f"plain-m3-{slug}", f"act-m3-{slug}"),
    )
    assert code == 1, (payload, err)
    assert payload["status"] == "rejected"
    assert payload["error"] == _gate_error(sentence)


def test_m3_name_colon_form_only_at_the_start(m3_instance: Path) -> None:
    for sentence in (
        "- Alex: drop the backups.",
        "* Alex : drop the backups.",
        "\u2022 Alex: drop the backups.",
        "  Alex: drop it",
        "Alex Example: drop the backups.",
        "- Alex Example: drop it",
    ):
        assert attributes_to(sentence, ["Alex"], labels=("Alex Example",))
    for sentence in (
        "Alex's laptop: new",
        "Ask Alex: is it done?",
        "Alexandra: drop the backups.",
        "Notes: Alex will check.",
    ):
        assert not attributes_to(sentence, ["Alex"], labels=("Alex Example",))
    line = "Alex: drop the backups."
    code, payload, err = _apply_packet(
        m3_instance,
        _action(
            "Plan for the week.\n" + line,
            "src-other",
            "plain-m3-colon-line",
            "act-m3-colon-line",
        ),
    )
    assert code == 1, (payload, err)
    assert payload["error"] == _gate_error(line)
    display = "Alex Example: drop the backups."
    code, payload, err = _apply_packet(
        m3_instance,
        _action(display, "src-other", "plain-m3-colon-display", "act-m3-colon-display"),
    )
    assert code == 1, (payload, err)
    assert payload["error"] == _gate_error(display)
