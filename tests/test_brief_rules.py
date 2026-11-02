"""The brief's counting rules: overdue split into the principal's own and waits, and
decisions that tie on their date ordered by when each was first recorded."""
from __future__ import annotations

import json
from pathlib import Path

from brain import reads, usage
from tests.conftest import NOW, source_op, upsert_op
from tests.test_phase7 import PRINCIPAL, SAM, _action, _apply, _brief, _instance, _seed
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

ALEX = PRINCIPAL["person_id"]
PAST, FUTURE = "2026-10-01", "2026-10-20"  # conftest NOW is 2026-10-08


def test_overdue_splits_the_principals_own_from_waits_on_others(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    _apply(instance, *_seed(
        _action("act-mine", "Send the plan", "open", due=PAST, owners=(ALEX,)),
        _action("act-mine-blocked", "Fix the build", "blocked", due=PAST, owners=(ALEX,)),
        _action("act-blocked-on-sam", "Sign the form", "blocked", due=PAST, owners=(ALEX,), waiting=(SAM,)),
        _action("act-sent", "Hear back on the quote", "sent", due=PAST, owners=(ALEX,)),
        _action("act-sams", "Book the venue", "open", due=PAST, owners=(SAM,)),
        _action("act-no-lead", "Print the badges", "open", due=PAST),
        _action("act-later", "Order the food", "open", due=FUTURE, owners=(ALEX,)),
        _action("act-done", "Send the invites", "done", due=PAST, owners=(ALEX,)),
    ))
    lines = _brief(instance).splitlines()
    # Yours: led by the principal and waiting on no one. Every other overdue open action waits on someone else.
    assert "Actions: 7 open, 0 waiting, 2 blocked. Overdue: 2 yours, 4 waiting on others." in lines


def test_overdue_without_a_principal_is_one_count(tmp_path: Path) -> None:
    instance = _instance(tmp_path, principal=False)
    _apply(instance, *_seed(_action("act-late", "Send the plan", "open", due=PAST)))
    assert "Actions: 1 open, 0 waiting, 0 blocked. Overdue: 1." in _brief(instance).splitlines()


SRC_DECISIONS = "src-decisions"
DECISION_NOTES = ("Decision notes. We will move the launch to November. We will keep the Sydney office open. "
                  "On 2026-10-04: we will hire two engineers.")


def _decision(did: str, title: str, quote: str, decided_on: str = "2026-10-05", base=None) -> dict:
    row = {"id": did, "title": title, "quote": quote, "decided_on": decided_on, "decided_by_person_id": SAM,
           "status": "in_effect", "project_id": "general"}
    return upsert_op("upsert_decision", row, SRC_DECISIONS, base=base)


def _recent(instance: Path) -> list[str]:
    result = reads.run_tool(instance, "records_query", {"entity": "decision", "order_by": "decided_on", "desc": True},
                            now=NOW)
    assert result.is_error is False, result.text
    return [row["id"] for row in result.structured["rows"]]


def test_decisions_on_one_date_order_by_when_first_recorded(tmp_path: Path, clock: Clock) -> None:
    instance = _instance(tmp_path)
    clock.at("2026-10-05T01:00:00Z")
    _apply(instance, *_seed(source_op(SRC_DECISIONS, DECISION_NOTES, date="2026-10-05"),
                            _decision("dec-a-launch", "Move the launch", "We will move the launch to November.")))
    clock.at("2026-10-05T02:00:00Z")
    _apply(instance, _decision("dec-b-office", "Keep the office", "We will keep the Sydney office open."))
    clock.at("2026-10-06T03:00:00Z")
    _apply(instance, _decision("dec-c-hire", "Hire engineers", "we will hire two engineers.", decided_on="2026-10-04"))
    # Newest date first; on 5 October the later record comes first, though its id sorts after.
    assert _recent(instance) == ["dec-b-office", "dec-a-launch", "dec-c-hire"]
    clock.at("2026-10-07T04:00:00Z")
    _apply(instance, _decision("dec-a-launch", "Move the launch to November", "We will move the launch to November.",
                               base=1))
    # An edit doesn't reorder: the first-recorded time stays.
    assert _recent(instance) == ["dec-b-office", "dec-a-launch", "dec-c-hire"]


def test_a_log_line_from_before_relaxed_was_recorded_still_reports(tmp_path: Path) -> None:
    instance = _instance(tmp_path)
    (instance / "usage").mkdir()
    old = {"minute": NOW[:16] + "Z", "tool": "search", "client": "claude-code", "token": "", "bytes": 10, "rows": 1,
           "outcome": "hit", "ms": 1.0, "session": "s1", "first": False}
    (instance / "usage" / f"{NOW[:7]}.jsonl").write_text(json.dumps(old) + "\n")
    data = usage.report(instance, "week")
    assert data["relaxed_searches"] is None
    assert "- Searches where any word counted: not logged yet" in usage.markdown(data)


def test_only_a_search_that_answered_says_whether_it_relaxed() -> None:
    from types import SimpleNamespace

    assert usage.relaxed("search", SimpleNamespace(is_error=False, structured={"relaxed": True})) is True
    assert usage.relaxed("search", SimpleNamespace(is_error=False, structured={"hits": []})) is False
    # An errored search didn't run a match, so it leaves the relaxed share alone.
    assert usage.relaxed("search", SimpleNamespace(is_error=True, structured=None)) is None
    assert usage.relaxed("get", SimpleNamespace(is_error=False, structured={"relaxed": True})) is None
