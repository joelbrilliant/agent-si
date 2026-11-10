# agent-si

agent-si gives your AI agents a memory they can check. Every fact they read comes with the source it was quoted from, and what was said in past agent sessions is recorded word for word by code, not by a model. One install, one search, all in SQLite files on your own machine. Built by Joel Brilliant, with the help of my agents. Apache-2.0.

## Install

One command, on macOS or Linux (x86_64 or arm64):

```sh
curl -LO https://github.com/joelbrilliant/agent-si/releases/latest/download/install-agent-si.sh && sh install-agent-si.sh
```

It asks one question, your name. Then it installs [uv](https://docs.astral.sh/uv/) if you don't have it, installs agent-si, makes your first brain, connects every agent tool it finds (Claude Code, Codex CLI, Gemini CLI, Grok Build, Hermes, Cursor) and records your past agent sessions (Claude Code, Codex, Grok and Hermes) in a file beside the brain. It prints how to undo each step. Read the file first if you like: the top is plain `sh`, and below a marker line it carries the wheel and its SHA-256 sum, which it checks before it installs anything. Flags: `--yes` (ask nothing), `--principal NAME`, `--name NAME`, `--dir FOLDER`, `--timezone ZONE`, `--no-connect` (change no agent tool's settings), `--no-sessions` (don't record past sessions).

**Or tell your agent.** Paste this into your agent:

```text
Read https://raw.githubusercontent.com/joelbrilliant/agent-si/main/AGENT-INSTALL.md and follow it to install agent-si on this machine. Ask me before you run anything that changes my machine.
```

[AGENT-INSTALL.md](AGENT-INSTALL.md) is written for the agent: download the installer, check its SHA-256, run it, confirm the brain answers, say what was connected and how to undo each part.

To install a wheel you already hold, without the one-file installer: `sh install.sh --from <agent-si wheel>` ([install.sh](install.sh) is under 100 lines of POSIX `sh`). Nothing here installs agent-si by name from a package index, because it isn't published on one.

Then ask your agent: **read my brain brief.**

## Why

An agent usually starts each session by reading a pile of notes, and has to trust whatever it finds there. agent-si holds the same knowledge as typed, sourced rows, behind six read tools, so an agent reads only what a question needs, and a fact without a real quote from a real source never gets in.

On the invented Example Co benchmark (24 questions about a 40-person company; Tier A, which calls no model; measured on this release, in bytes, not tokens):
- **Startup:** an agent starts with 11,139 bytes from the brain (the router file, the MCP handshake and the brief), against 117,185 bytes for the same company written as markdown.
- **Answers:** each of the 24 questions took one or two tool calls and 180 to 6,216 bytes of tool output.
- **Growth:** with four times the history (scale 4), the markdown grew 4.08 times, to 477,986 bytes, while the brain's startup and every answer stayed exactly the same size.
- **Reads** are deterministic, byte for byte, and the slowest question's 95th percentile was between 5 and 6 ms on the Mac laptop that measured it (it varies by machine; the test allows 50 ms). Both are tested on every release.

Reproduce them: `python -m bench.harness measure` and `python tools/scale_check.py` print every figure, and `python -m pytest -q tests/test_bench_gate.py` checks them. Whether agents *answer better* with it is a separate measurement (Tier B: real agent sessions, brain against markdown), which needs model calls and isn't published here, so this README makes no claim about it.

## Make a brain

```sh
brain init
```

It asks for the brain's name (default `work`), its folder (default `~/Brains`), your name, your time zone, and which of your agent tools to connect. To skip the questions: `brain init work --principal "Your Name" --timezone Australia/Sydney`.

## Connect your agent tools

```sh
brain connect all            # or: brain connect claude cursor
```

It uses each tool's own command where it has one (Claude Code, Codex CLI, Gemini CLI, Grok Build, Hermes), merges strict JSON for Cursor (with a backup, after you say yes), and prints the entry where it can't write safely. Then it starts the brain exactly as the tool will and checks it answers, and tells you to restart the tool. `--print` writes nothing; `--remove` takes it out again. [docs/COMPAT.md](docs/COMPAT.md) records what was checked, per tool and version. For any other tool, `brain connect <name>` prints the generic MCP entry.

Then ask your agent: **read my brain brief.**

## Try it

Put a document in, then try to record one fact it says and one it doesn't. Here a folder collector has captured `pricing.md`, which says *The Pro plan's context window is 128k tokens.*

```text
$ brain apply --instance work claim-128k.json
{"status": "applied", ...}

$ brain apply --instance work claim-1m.json
{"status": "rejected", "error": "op 0 (add_claim): quote is not in source src-cap-4030afad23810dd1-1009f9569bdbdfd6", ...}
```

The second claim quoted a sentence that isn't in the source, so the evidence gate refused it. `brain read fact_get --args '{"key": "pro.max_context"}'` now answers `128k tokens`, with the source, its date and the locator.

## Past sessions

Your agent tools already keep what you and they said. agent-si files that dialogue, word for word, in `sessions.sqlite3` beside the brain: Claude Code, Codex, Grok and Hermes sessions, the user's and the assistant's text, without tool calls or tool output. Code does it, never a model, and nothing leaves your machine.

- **One search.** `search` returns the brain's hits and, beside them, a short `sessions` list with the lines that matched: three sessions as leads, or five with more lines when the brain itself has nothing. `since` and `until` keep to those days, and with no query they list that period's sessions. `get` with `entity: "session"` opens one at the line that matched, a small page at a time.
- **It keeps what the tool deletes.** A message is added once and never rewritten, so a session stays after the tool removes its own copy (Claude Code's terminal app deletes transcripts after 30 days by default). `brain sessions forget <session id>` removes one and stops it coming back.
- **A fact can quote a session.** `brain sessions excerpt <session id> --from 12 --to 14` prints a source holding those messages. The applier checks it against the record, so a quote that was really said passes the evidence gate, and a made-up or edited one is refused. An excerpt has no author: a "user" turn can be a pasted email or a system notice, so it never counts as your own words.
- **One switch.** `[sessions] record = false` in `brain.toml` turns it off, and `--no-sessions` starts a new brain that way. `brain sessions status` shows what is held.
- **Limits.** A message that looks like it holds a secret (an API key, a private key, `TOKEN=...`) is never stored. The file is readable by your account only. `brain backup` doesn't carry it yet, so copy `sessions.sqlite3` yourself. A folder token and a hub connection never see it.

## Bring your work in

`brain-fetch` captures text from a documents folder, Gmail, Slack, Google Calendar and GitHub, with read-only access, into sources. `brain-fetch setup gmail` (or `slack`, `calendar`, `github`, `documents`) prints the steps for each. For email and Slack, addresses and phone numbers are replaced before anything is stored, and quoted history, signatures and bot posts are dropped. The engine itself never touches the network.

## Updates

New releases are on the [releases page](https://github.com/joelbrilliant/agent-si/releases). Nothing in agent-si looks for them, schedules a job or fetches a package by name, so an update happens when you choose. Run `install-agent-si.sh` from the newest release again (it upgrades agent-si and keeps your brain and its session record), or download the agent-si wheel and run `brain update --from-wheel <wheel>`, which stops writes, takes a backup, upgrades, migrates, checks, and rolls back by itself if anything fails. A running agent tool keeps the old version until you restart it.

`brain update` doesn't check a signature. Check the installer against the `SHA256SUMS` file on the release page before you run it.

## Using it with agent memory

[Agent memory](https://github.com/joelbrilliant/agent-memory) is a separate tool, with its own repository. Its session search is where agent-si's past sessions came from, and from 1.15.0 the brain records and searches sessions itself, so the installer above installs agent-si alone.

You don't need agent memory for past sessions any more. It still searches a list of your own markdown notes, which agent-si doesn't. If an earlier installer put it on your machine it keeps working, and the installer tells you how to remove it: `agent-memory connect all --remove`, then `uv tool uninstall agent-memory`.

If agent memory is installed, `brain usage report` also shows each tool's model tokens per session, from agent memory's counts. Without it, those numbers say "unknown".

## Troubleshooting

- **`brain --version` fails** after a Python or uv change: run `install-agent-si.sh` again, or `uv tool install --reinstall --managed-python --python 3.13 <wheel>` with the agent-si wheel from the releases page.
- **Your agent doesn't see the brain:** restart the tool; then `brain connect <tool> --print` shows exactly what it should have.
- **The brief says `Intake: ... failing (auth)`:** a fetch credential stopped working. `brain-fetch check --instance <dir>` says which, and `brain-fetch setup <service>` how to renew it. A Google app left in "Testing" status gets tokens that expire after 7 days.
- **The brief says `PAUSED`:** writes stopped so a person can look. The `PAUSED` file says why.
- **`Updating; writes resume shortly.` that doesn't go away:** an update stopped part way. See `brain update --status` and [the update runbook](docs/REFERENCE.md#updates).

## Privacy

Everything stays on your machine: the store, the session record, the config, the fetch exports (kept 14 days) and the backups. There's no telemetry and no account. The usage log in `<instance>/usage/` holds counts and enums only, never what was asked or answered, and `[usage] record = false` turns it off. To remove something for good, `brain redact <source id>` replaces the source and scrubs every copy derived from it, then compacts the file. [docs/REDACTION.md](docs/REDACTION.md) lists every other place a copy can live (Time Machine, synced folders, offsite buckets, agent session logs) and how to clear each.

## Uninstall

```sh
brain connect all --remove --yes --instance ~/Brains/work
brain schedule off      # only if an earlier version installed a nightly job
uv tool uninstall agent-si
```

Your brains stay where they are (`~/Brains/<name>`), each with its session record (`sessions.sqlite3`). Take a `brain backup` first, then delete the folder, `~/.config/agent-si` and `~/.local/state/agent-si` if you want them gone.

## How it works

Each brain is one SQLite file with one writer, the applier, and a second file beside it for past sessions, which code fills and no packet writes. Agents send change packets; the applier checks each op against the config and the evidence gate (a claim's quote must be in its source) and applies, holds for approval, or refuses it, with a receipt. Collectors turn exported files into sources; extraction is a model running in your own agent tool, whose candidates the brain verifies the same way. Reads go through six tools over MCP (`brief`, `search`, `get`, `fact_get`, `records_query` and `signals`), and the same question always gets the same bytes. [docs/REFERENCE.md](docs/REFERENCE.md) has every command and rule.

## Build the installer yourself

The one-file installer is built from this checkout:

```sh
python3 tools/bundle.py --out dist/    # prints the file's path, size and SHA-256
```

It writes `install-agent-si.sh` (and a copy named for the version) and a `SHA256SUMS` beside it.

## Licence

Apache-2.0. Copyright 2026 Joel Brilliant. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
