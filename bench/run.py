"""Tier B: real agent sessions on the Example Co benchmark, two arms, same harness,
model, effort and question text, a fresh session per question, harness tools off.

    python bench/run.py --arms brain,markdown --runs 3 [--questions q01,q02] [--model M] [--effort E] [--dry-run]

- brain: ROUTER.md as the instructions file, and the six read tools over MCP, nothing else.
- markdown: every markdown file in the instructions file, disputed values and notes as teams write them, no tools.

Subscription auth only: API-key variables are unset for every session, and a session whose init line reports
any apiKeySource but "none" stops the run. Each session has a 15-minute alarm and --max-budget-usd. One JSON
line per session goes in sessions.jsonl beside the raw transcripts; record.md summarises x of n correct (strict,
by bench/grading.py, no model judge), with medians and ranges. A failed run stays in the record.
"""
from __future__ import annotations

import argparse
import json
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench import grading, harness, quotes  # noqa: E402

DENY = ["Task", "Agent", "Workflow", "RemoteTrigger", "CronCreate", "ScheduleWakeup", "SendMessage", "Skill"]
CLAUDE = Path.home() / ".local" / "bin" / "claude"


def _commit() -> str:
    done = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    return done.stdout.strip() or "unknown"


def _markdown(dataset: Path) -> str:
    parts = []
    for path in sorted((dataset / "markdown").rglob("*.md")):
        parts.append(f"<!-- {path.relative_to(dataset / 'markdown')} -->\n{path.read_text(encoding='utf-8')}")
    return "# Example Co notes\n\nAnswer from these notes.\n\n" + "\n\n".join(parts)


def _command(arm: str, question: str, ns, workdir: Path, brain_cfg: Path) -> list[str]:
    common = ["--model", ns.model, "--effort", ns.effort, "--setting-sources", "project,local", "--strict-mcp-config",
              "--output-format", "stream-json", "--verbose", "--no-session-persistence", "--max-budget-usd", str(ns.budget),
              "--tools", "", "--disallowedTools", *DENY]
    if arm == "brain":
        common += ["--mcp-config", str(brain_cfg), "--allowedTools", "mcp__brain"]
    else:
        empty = workdir / "no-mcp.json"
        empty.write_text(json.dumps({"mcpServers": {}}))
        common += ["--mcp-config", str(empty)]
    return [str(CLAUDE), "-p", question, *common]


def _session(argv: list[str], cwd: Path, transcript: Path) -> int:
    """One session through an interactive zsh, which holds the subscription login, with API keys unset."""
    inner = "cd " + shlex.quote(str(cwd)) + " && exec env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN perl -e 'alarm 900; exec @ARGV' " \
        + " ".join(shlex.quote(part) for part in argv)
    with open(transcript, "w", encoding="utf-8") as out:
        return subprocess.run(["zsh", "-ic", inner], stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL).returncode


def _read(transcript: Path, arm: str, model: str | None = None) -> dict:
    init, result, calls, result_bytes = {}, {}, 0, 0
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            result = event
        elif event.get("type") == "assistant":
            calls += sum(1 for block in (event.get("message") or {}).get("content") or []
                         if isinstance(block, dict) and block.get("type") == "tool_use")
        elif event.get("type") == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    content = block.get("content")
                    result_bytes += len(json.dumps(content).encode()) if not isinstance(content, str) else len(content.encode())
    tools = init.get("tools") or []
    problems = []
    if init.get("apiKeySource") not in ("none", None):
        problems.append("api-key auth")
    if model and not str(init.get("model") or "").startswith(model):
        problems.append(f"model {init.get('model')}")  # a wrong model is a broken setup
    expected_mcp = arm == "brain"
    other = [tool for tool in tools if not tool.startswith("mcp__")]
    mcp = [tool for tool in tools if tool.startswith("mcp__")]
    if other or (expected_mcp and not mcp) or (not expected_mcp and mcp):
        problems.append(f"tools other {other} mcp {mcp}")
    usage = result.get("usage") or {}
    parts = [usage.get(key) for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
    total = sum(parts) if all(isinstance(part, int) for part in parts) else None
    return {"total_input": total, "answer": result.get("result") or "", "model": init.get("model"), "apiKeySource": init.get("apiKeySource"),
            "problems": problems, "calls": calls, "result_bytes": result_bytes,
            "input_tokens": usage.get("input_tokens"), "cache_read": usage.get("cache_read_input_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cost_usd": result.get("total_cost_usd"), "seconds": round((result.get("duration_ms") or 0) / 1000, 1),
            "is_error": bool(result.get("is_error")) or not result, "models_used": sorted(result.get("modelUsage") or {})}


def _final(transcript: Path) -> str:
    for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "result":
            return event.get("result") or ""
    return ""


def regrade(folder: Path, questions: list[dict]) -> list[dict]:
    """A finished run's sessions, graded again from its transcripts with this grader, so two runs compare like for like."""
    by_id = {q["id"]: q for q in questions}
    rows = []
    for line in (folder / "sessions.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        question = by_id.get(row["question"])
        transcript = folder / "transcripts" / f"{row['question']}-{row['arm']}-{row['run']}.jsonl"
        if question and transcript.is_file():
            graded = grading.grade(question, _final(transcript))
            row = {**row, "strict": graded["strict"], "lenient": graded["lenient"]}
        rows.append(row)
    return rows


def criteria(rows: list[dict], questions: list[dict], previous: list[dict] | None = None, previous_path=None) -> list[str]:
    """B1 to B8 (bench/thresholds.json), by script, for the brain arm against markdown."""
    limits = json.loads((ROOT / "bench" / "thresholds.json").read_text())["numbers"]
    brain = [row for row in rows if row["arm"] == "brain"]
    markdown = [row for row in rows if row["arm"] == "markdown"]
    if not brain:
        return []
    runs = sorted({row["run"] for row in brain})
    kinds = {q["id"]: q.get("kind") for q in questions}

    def right_in(rows_, qid):
        return sum(1 for row in rows_ if row["question"] == qid and row["strict"])

    def verdict(ok: bool) -> str:
        return "**Passed**" if ok else "**Not passed**"

    def med(rows_, key):
        values = [row[key] for row in rows_ if isinstance(row.get(key), (int, float))]
        return statistics.median(values) if values else None

    def fresh(rows_):
        values = [row["input_tokens"] + (row.get("cache_write") or 0) for row in rows_ if isinstance(row.get("input_tokens"), int)]
        return statistics.median(values) if values else None

    out = ["## Criteria (bench/thresholds.json, by script)", "", "| Id | Result | Numbers |", "|---|---|---|"]
    steady = [q["id"] for q in questions if right_in(brain, q["id"]) >= min(limits["B1_min_runs"], len(runs))]
    total_b, total_m = sum(row["strict"] for row in brain), sum(row["strict"] for row in markdown)
    out.append(f"| B1 | {verdict(len(steady) >= limits['B1_min_correct'] and total_b >= total_m)} | {len(steady)} of "
               f"{len(questions)} questions right in at least {limits['B1_min_runs']} of {len(runs)} runs; brain {total_b} "
               f"against markdown {total_m} in all |")
    conflicts = [row for row in brain if row["group"] == "conflicts"]
    out.append(f"| B2 | {verdict(bool(conflicts) and all(row['strict'] for row in conflicts))} | "
               f"{sum(row['strict'] for row in conflicts)} of {len(conflicts)} conflict sessions |")
    abstain = [row for row in brain if kinds.get(row["question"]) == "abstain"]
    out.append(f"| B3 | {verdict(bool(abstain) and all(row['strict'] for row in abstain))} | "
               f"{sum(row['strict'] for row in abstain)} of {len(abstain)} abstention sessions |")
    if all("quotes" in row for row in brain):
        given, exact = sum(row["quotes"] for row in brain), sum(row["quotes_verbatim"] for row in brain)
        out.append(f"| B4 | {verdict(given == exact)} | {exact} of {given} evidence quotes word for word (bench/quotes.py) |")
    else:
        out.append("| B4 | Not measured | This run predates bench/quotes.py; run it on the transcripts |")
    # Uncached input and cost are read on each question's first ask (run 1) only. Runs 2 and 3 repeat the same
    # question text in a fresh session and reuse that first ask's prompt cache. In the markdown arm every first
    # ask writes its notes to the cache again, and every repeat reads them.
    first = runs[0]
    groups, below, below_fresh = sorted({row["group"] for row in brain}), [], []
    for group in groups:
        b, m = [r for r in brain if r["group"] == group], [r for r in markdown if r["group"] == group]
        if med(b, "total_input") is not None and med(m, "total_input"):
            below.append((group, med(b, "total_input") / med(m, "total_input")))
            b1, m1 = [r for r in b if r["run"] == first], [r for r in m if r["run"] == first]
            below_fresh.append((group, (fresh(b1) or 0) / (fresh(m1) or 1)))
    ratios = ", ".join(f"{group} {ratio:.2f}" for group, ratio in below)
    fresh_ratios = ", ".join(f"{group} {ratio:.2f}" for group, ratio in below_fresh)
    cost = lambda rows_: sum(r["cost_usd"] for r in rows_ if r["run"] == first and isinstance(r.get("cost_usd"), (int, float)))
    out.append(f"| B5 | {verdict(bool(below) and all(ratio < 1 for _, ratio in below))} on total input; "
               f"{'passed' if below_fresh and all(r < 1 for _, r in below_fresh) else 'not passed'} on uncached, first ask | "
               f"Median total input, brain over markdown: {ratios}. Uncached on each question's first ask (run {first}): "
               f"{fresh_ratios}. Notional cost of the first asks: brain US${cost(brain):.2f}, markdown US${cost(markdown):.2f}. "
               f"Later runs repeat the question and reuse its prompt cache, so they aren't read for cache or cost |")
    calls = med(brain, "calls")
    out.append(f"| B6 | {verdict(calls is not None and calls <= limits['B6_max_median_calls'])} | Median {calls} calls "
               f"(maximum {max(row['calls'] for row in brain)}) |")
    same = [q["id"] for q in questions if len({row["strict"] for row in brain if row["question"] == q["id"]}) == 1]
    out.append(f"| B7 | {verdict(len(same) / len(questions) >= limits['B7_min_share'])} on verdicts | {len(same)} of "
               f"{len(questions)} questions got the same verdict in every run; key values aren't compared by script |")
    if previous:
        before = [row for row in previous if row["arm"] == "brain"]
        was, now = sum(row["strict"] for row in before), total_b
        grew = (med(brain, "total_input") or 0) / (med(before, "total_input") or 1) - 1
        out.append(f"| B8 | {verdict(now >= was and grew <= limits['B8_max_token_growth'])} | Against `{previous_path}`, "
                   f"regraded with this grader: {now} against {was} correct; median input {grew:+.0%} |")
    else:
        out.append("| B8 | Not applicable | No earlier milestone given (--previous) |")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arms", default="brain,markdown")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--questions", default="")
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--budget", type=float, default=1.0)
    parser.add_argument("--out", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--previous", default="", help="the last milestone's results folder, for B8 (regraded with this grader)")
    ns = parser.parse_args(argv)
    arms = [arm.strip() for arm in ns.arms.split(",") if arm.strip()]
    if not set(arms) <= {"brain", "markdown"}:
        parser.error("arms are brain and markdown")
    manifest = harness.load_manifest()
    questions = json.loads((harness.DATASET / "questions.json").read_text())["questions"]
    every_question = list(questions)  # B8 regrades the whole earlier run, whatever --questions picks now
    if ns.questions:
        wanted = set(ns.questions.split(","))
        questions = [q for q in questions if q["id"] in wanted]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = Path(ns.out or ROOT / "bench" / "results" / f"{stamp}-claude-{_commit()}")
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="tierb-"))
    instance = harness.build_instance(harness.DATASET, work)
    brain_cfg = work / "brain-mcp.json"
    # The engine at this commit, never whichever brain is on PATH, so the record's commit is what was measured.
    server = {"type": "stdio", "command": sys.executable, "args": ["-m", "brain", "serve", "--instance", str(instance)],
              "env": {"PYTHONPATH": str(ROOT / "src")}}
    brain_cfg.write_text(json.dumps({"mcpServers": {"brain": server}}))
    dirs = {}
    for arm in arms:
        dirs[arm] = work / f"cwd-{arm}"
        dirs[arm].mkdir()
        text = (instance / "ROUTER.md").read_text() if arm == "brain" else _markdown(harness.DATASET)
        (dirs[arm] / "CLAUDE.md").write_text(text)
    sessions = out / "sessions.jsonl"
    corpus = quotes.sources()
    rows = []
    for run in range(1, ns.runs + 1):
        for question in questions:
            for arm in arms:
                transcript = out / "transcripts" / f"{question['id']}-{arm}-{run}.jsonl"
                transcript.parent.mkdir(exist_ok=True)
                argv = _command(arm, question["question"], ns, dirs[arm], brain_cfg)
                if ns.dry_run:
                    print(" ".join(shlex.quote(part) for part in argv))
                    continue
                started = time.monotonic()
                code = _session(argv, dirs[arm], transcript)
                found = _read(transcript, arm, ns.model)
                if "api-key auth" in found["problems"]:
                    print(f"{question['id']} {arm}: API-key auth reported; stopping", file=sys.stderr)
                    return 3
                if any(problem.startswith("model ") for problem in found["problems"]):
                    print(f"{question['id']} {arm}: {found['problems']}; a broken setup, stopping", file=sys.stderr)
                    return 3
                answer = found.pop("answer")
                graded = grading.grade(question, answer)
                quoted = quotes.check(answer, corpus, question["group"] == "attribution", question["question"])
                row = {"question": question["id"], "group": question["group"], "arm": arm, "run": run, "exit": code,
                       "wall_seconds": round(time.monotonic() - started, 1), **found,
                       "strict": graded.get("strict"), "lenient": graded.get("lenient"), "flagged_trap": graded.get("flagged_trap"),
                       "quotes": quoted["quotes"], "quotes_verbatim": quoted["verbatim"],
                       "quotes_not_verbatim": [item["quote"] for item in quoted["not_verbatim"]]}
                rows.append(row)
                with open(sessions, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                print(f"{question['id']} {arm} run {run}: strict {row['strict']} lenient {row['lenient']} calls {row['calls']} "
                      f"input {row['total_input']} cost {row['cost_usd']}")
    if ns.dry_run:
        return 0
    lines = [f"# Tier B record, {stamp}", "", f"- Commit `{_commit()}`, engine {__import__('brain').__version__}, dataset seed "
             f"{manifest['seed']} scale {manifest['scale']}, harness Claude Code, model {ns.model}, effort {ns.effort}, "
             f"subscription auth, budget US${ns.budget} a session.", f"- {len(questions)} questions, {ns.runs} runs, arms {', '.join(arms)}.",
             "- Graded by bench/grading.py (strict), no model judge. Failed and errored sessions stay in.", ""]
    for arm in arms:
        mine = [row for row in rows if row["arm"] == arm]
        right = sum(1 for row in mine if row["strict"])
        tokens = [row["total_input"] for row in mine if isinstance(row["total_input"], int)]
        fresh = [row["input_tokens"] + (row["cache_write"] or 0) for row in mine if isinstance(row["input_tokens"], int)]
        costs = [row["cost_usd"] for row in mine if isinstance(row["cost_usd"], (int, float))]
        # Total input counts cached reads too; uncached is what the markdown arm's caching hides.
        median = (f"median input tokens {int(statistics.median(tokens))} total (min {min(tokens)}, max {max(tokens)}), "
                  f"{int(statistics.median(fresh))} uncached") if tokens else "input tokens unknown"
        flagged = sum(1 for row in mine if row["problems"] or row["is_error"])
        lines.append(f"- **{arm}:** {right} of {len(mine)} correct; {median}; notional cost US${sum(costs):.2f}; "
                     f"{flagged} sessions errored or broke the tool gate (kept in the count).")
    previous = Path(ns.previous) if ns.previous else None
    lines += ["", *criteria(rows, questions, regrade(previous, every_question) if previous else None, previous)]
    (out / "record.md").write_text("\n".join(lines) + "\n")
    print(f"record: {out / 'record.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
