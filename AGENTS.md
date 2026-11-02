# Rules for agents working on agent-si

Every contributor here is an agent that sees only the files it opened, copies the nearest example and takes the
shortest path that compiles. So each rule below is enforced by the design or by a check whose error names the fix.
A rule with nothing enforcing it is a judgement call and sits in the last table. When an agent is corrected for a
mistake that a rule here already covers, the rule wasn't enough: fix it at a higher level in the same change.

## Run the checks

One command, run locally. CI (`.github/workflows/test.yml`) runs the same suite on Python 3.11 and 3.14:

```sh
python3 -m venv .scratch/venv && .scratch/venv/bin/pip install -e '.[test]'
PATH="$PWD/.scratch/venv/bin:$PATH" .scratch/venv/bin/python -m pytest -q
```

The suite takes about 7 minutes. On macOS, which has no `timeout`, cap a run with
`perl -e 'alarm shift; exec @ARGV' 1200 <command>`.

## Rules and what enforces them

| Rule | Enforced by |
| --- | --- |
| Don't write the current version, the latest schema, or a ROUTER.md or tools/list byte count into a test or tool. Read `brain.__version__`, `brain.migrate.latest_version()`, `tests.conftest.router_bytes()` and `tests.conftest.tools_list_budgets()`. A deliberate pin ends with `# pinned: <reason>`. | `tests/test_no_pinned_versions.py` |
| Byte budgets (tools/list, per-tool and router sizes) change only in `bench/budgets.json`, with a changelog entry and its fingerprint. | `tests/test_bench_gate.py` |
| Check a packet with `brain validate packet FILE --instance DIR` before `brain apply`. It dry-runs the real applier, so it gives apply's verdict and keeps nothing. | The design (`Applier.apply(dry_run=True)`), `tests/test_dry_run.py` |
| Never put words or a decision in the principal's mouth. Quote at least 5 words from an existing source the principal wrote, or reword neutrally. Agents can't create a source in the principal's name. | The evidence gate, whose refusal names both routes; `tests/test_attribution_corpus.py` |
| Every refusal an agent can read is built in `src/brain/advice.py` and names its fix: the argument to use, the allowed values, the limit or an example. | `tests/test_error_fixes.py` |
| An op without `base` creates the row. If the row exists, it's a conflict that names the version to send. | The applier (invariant), `tests/test_error_fixes.py` |
| Outside JSON and bytes are read only through `brain.ingress` (`loads`, `read_bytes`, `load_file`). JSON the engine wrote itself carries `# trusted: <what wrote it>` on the line. | `tests/test_ingress_only.py::test_only_brain_ingress_parses_outside_json`, `tests/test_ingress_behaviour.py` |
| No test reads `/dev/zero` or another endless device. Hostile-input replays use finite inputs under a time limit. | `tests/test_ingress_only.py::test_no_test_reads_an_endless_device` |
| A list that names ops, entities, tables, columns, envelopes or tools is derived from its source of truth or guarded. A missing entry fails, naming the entry, the file and the list. | `tests/test_every_list_is_complete.py`, plus the table, STRICT and column-decision guards |
| One-line text goes through `brain.text.single_line()`. A multi-line normalisation carries `# not one-line: <reason>`. | `tests/test_one_line.py::test_no_hand_rolled_one_line_cleaning_outside_brain_text` |
| Brain instances live outside this repository. | `brain init` refuses (`src/brain/init.py`) |
| Never install, update or fetch agent-si or agent-memory by name from a package index: they aren't published on one, so a name there is free for anyone to register. Every install takes a wheel from the releases page. | `tests/test_no_fetch_by_name.py`, `brain update`'s refusal in `src/brain/advice.py` |
| No real names, companies, emails or paths in a test, fixture, example or comment: invent them (`Alex Example`, `Example Co`, `example.com`). | The release scan (`tools/release_check.py`) before an export |

Search no longer needs a rule: every query ranks by any word, and says when no result has every word
(`tests/test_search_any_word.py`, `tests/test_read_tools.py`).

## Judgement calls (nothing enforces these)

| Rule | Why it can't be enforced |
| --- | --- |
| Prove a fix fails on the real past mistake before it passes: copy the old file with `git show <commit>:<file>`, put it first on `PYTHONPATH` and confirm `brain.__file__`. | Which commit is "the mistake" is a reading of the history. |
| A builder working from a task description edits only the files it allows, and says when it can't. | The allowed list lives in the task description, not the repo. |
| Loosening the evidence gate needs a sentence of the shape (invented, with invented names) and a reason, added to the corpus as a pass case. | Whether a sentence attributes words to the principal is judgement. The corpus records each decision. |
