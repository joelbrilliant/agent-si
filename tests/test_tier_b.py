"""The Tier B runner, without a model. Reading a session, the auth and tool gate, and the commands
each arm gets. The run itself uses the runner's own model subscription and is started by hand."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from bench import run


def _transcript(tmp_path: Path, tools: list[str], key_source: str = "none") -> Path:
    events = [
        {"type": "system", "subtype": "init", "model": "claude-haiku-5-5", "apiKeySource": key_source, "tools": tools},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__brain__fact_get"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 120}]}},
        {"type": "result", "result": "Example Pro costs 29 AUD per seat.", "total_cost_usd": 0.002, "duration_ms": 4900,
         "usage": {"input_tokens": 8, "cache_read_input_tokens": 23507, "cache_creation_input_tokens": 8824, "output_tokens": 328}},
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events))
    return path


def test_a_session_is_read_with_total_and_uncached_input(tmp_path) -> None:
    found = run._read(_transcript(tmp_path, ["mcp__brain__brief", "mcp__brain__fact_get"]), "brain")
    assert (found["calls"], found["result_bytes"], found["total_input"], found["problems"]) == (1, 120, 32339, [])
    assert found["answer"] == "Example Pro costs 29 AUD per seat." and found["seconds"] == 4.9


def test_the_gate_catches_api_keys_and_extra_tools(tmp_path) -> None:
    assert "api-key auth" in run._read(_transcript(tmp_path, ["mcp__brain__brief"], "ANTHROPIC_API_KEY"), "brain")["problems"]
    assert run._read(_transcript(tmp_path, ["Bash", "mcp__brain__brief"]), "brain")["problems"]
    assert run._read(_transcript(tmp_path, ["mcp__brain__brief"]), "markdown")["problems"]  # markdown has no tools
    assert run._read(_transcript(tmp_path, ["mcp__brain__brief"]), "brain", "claude-opus-5-5")["problems"] == ["model claude-haiku-5-5"]


def test_each_arm_gets_only_its_tools(tmp_path) -> None:
    ns = SimpleNamespace(model="claude-opus-5-5", effort="high", budget=1.0)
    brain = run._command("brain", "Q?", ns, tmp_path, tmp_path / "cfg.json")
    markdown = run._command("markdown", "Q?", ns, tmp_path, tmp_path / "cfg.json")
    for argv in (brain, markdown):
        assert argv[1:3] == ["-p", "Q?"] and "--strict-mcp-config" in argv and argv[argv.index("--tools") + 1] == ""
        assert "--setting-sources" in argv and argv[argv.index("--setting-sources") + 1] == "project,local"
    assert brain[brain.index("--allowedTools") + 1] == "mcp__brain" and "--allowedTools" not in markdown
    assert json.loads((tmp_path / "no-mcp.json").read_text()) == {"mcpServers": {}}


def test_the_grader_reads_number_words_plain_abstentions_and_figures_about_the_subject() -> None:
    from bench import grading

    count = {"question": "Which facts are in dispute right now?", "kind": "lookup", "answer_must": ["10"], "answer_must_not": []}
    assert grading.grade(count, "Ten facts are in dispute.")["strict"]
    assert not grading.grade(count, "One fact is in dispute.")["strict"]
    phrases = [["does not hold", "doesn't hold", "not recorded", "no record"]]
    abstain = {"question": "What is Example Co's annual revenue?", "kind": "abstain", "answer_must": phrases, "answer_must_not": []}
    for answer in ("The notes don't say what it is.", "The brain doesn't have it.", 'It is marked "Not yet recorded."'):
        assert grading.grade(abstain, answer)["strict"], answer
    # A count of what was searched, or a figure for something else, isn't an invented answer; a figure for the subject is.
    assert grading.grade(abstain, "I don't have it. All 80 fact keys are product settings. Example Cloud is 159 AUD.")["strict"]
    assert not grading.grade(abstain, "I don't have it, but annual revenue was about 12 million.")["strict"]
    assert not grading.grade(abstain, "Example Co's annual revenue is 12 million.")["strict"]  # no abstention at all


def test_quotes_are_checked_word_for_word_against_the_source_on_their_line() -> None:
    from bench import quotes

    corpus = {"src-sheet": "Sheet\nExample Studio support replies within 48 hours.", "src-chat": "Example Studio support replies within 8 hours."}
    good = 'Per `src-sheet`: "Example Studio support replies within 48 hours."'
    assert quotes.check(good, corpus)["not_verbatim"] == []
    wrong_source = 'Per `src-sheet`: "Example Studio support replies within 8 hours."'
    assert len(quotes.check(wrong_source, corpus)["not_verbatim"]) == 1
    reworded = 'Per `src-chat`: "Example Studio support answers within 8 hours."'
    assert len(quotes.check(reworded, corpus)["not_verbatim"]) == 1
    # A table row names its own source, so a row without an id is checked against every source.
    table = '| 8 hours | Slack | "Example Studio support replies within 8 hours" |\n| 48 hours | `src-sheet` | "Example Studio support replies within 48 hours" |'
    assert quotes.check(table, corpus)["verbatim"] == 2
    # Outside an attribution question, a quote counts as evidence only beside a source id; a span the question holds isn't one.
    assert quotes.check('The note says "probably waiting on Drew Example now".', corpus)["quotes"] == 0
    asked = 'Who wrote "Example Studio support replies within 8 hours"?'
    assert quotes.check('They wrote "Example Studio support replies within 8 hours".', corpus, True, asked)["quotes"] == 0
    assert all(key in quotes.sources() for key in ("src-doc-sheet-exstudio",))


def test_criteria_are_worked_out_by_script() -> None:
    questions = [{"id": f"q{i}", "group": "conflicts" if i == 1 else "lookups", "kind": "abstain" if i == 2 else "lookup"}
                 for i in range(1, 4)]

    def row(arm, qid, run_, strict, calls=2, total=100):
        return {"arm": arm, "question": qid, "group": next(q["group"] for q in questions if q["id"] == qid), "run": run_,
                "strict": strict, "calls": calls, "total_input": total, "input_tokens": 10, "cache_write": 0,
                "quotes": 1, "quotes_verbatim": 1}
    rows = [row("brain", q["id"], r, True) for q in questions for r in (1, 2, 3)]
    rows += [row("markdown", q["id"], r, q["id"] != "q3", 0, 200) for q in questions for r in (1, 2, 3)]
    lines = "\n".join(run.criteria(rows, questions))
    assert "| B2 | **Passed** | 3 of 3 conflict sessions |" in lines and "| B3 | **Passed** | 3 of 3 abstention" in lines
    assert "| B4 | **Passed** | 9 of 9 evidence quotes" in lines and "| B6 | **Passed** | Median 2" in lines
    assert "| B1 | **Not passed** | 3 of 3 questions" in lines  # 22 of 24 needed: three questions can't pass B1
    assert "| B8 | Not applicable" in lines


def test_uncached_input_and_cost_are_read_on_first_asks_only() -> None:
    questions = [{"id": "q1", "group": "lookups", "kind": "lookup"}]

    def row(arm, run_, fresh, cost):
        return {"arm": arm, "question": "q1", "group": "lookups", "run": run_, "strict": True, "calls": 1, "total_input": 100,
                "input_tokens": fresh, "cache_write": 0, "cost_usd": cost, "quotes": 0, "quotes_verbatim": 0}
    # The markdown arm's repeats read the first ask's cache; its first ask pays for the whole notes.
    rows = [row("brain", 1, 4000, 0.06), row("brain", 2, 3000, 0.05), row("markdown", 1, 46000, 0.38),
            row("markdown", 2, 660, 0.02), row("markdown", 3, 660, 0.02)]
    b5 = next(line for line in run.criteria(rows, questions) if line.startswith("| B5"))
    assert "passed on uncached, first ask" in b5 and "lookups 0.09" in b5
    assert "brain US$0.06, markdown US$0.38" in b5
