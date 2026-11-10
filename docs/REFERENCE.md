# agent-si engine reference

The engine's commands, file formats and rules in detail. The [README](../README.md) is the place to start.

**Never commit instance data, real personal or company data, fixtures with real content, or `.env` files.**

## Quick start

`python -m pip install -e ".[test]"` installs the engine and its test dependency.

`brain init demo` creates an instance directory beside the current working directory.

`brain validate packet packet.json --instance demo` checks a change packet without writing, by running the applier's own decision and rolling it back. It prints the verdict `brain apply` would give (status, error, targets) and exits with apply's code: 0 applied, 1 rejected, 3 conflict, 4 held. Without `--instance` it checks the packet's shape only, and its output says so.

`brain apply --instance demo packet.json` submits that packet to the instance. Only the applier writes; a direct write to `brain.sqlite3` is refused.

`brain collect --instance demo --input notes.json notes`

`--fixture` still works as the old name of `--input`. The engine reads exported files only; `brain-fetch` (below) writes the exports.

`brain extract --instance demo --as scan candidate.json`

Extraction reads candidate files only and calls no model. A candidate that passes every check is stored, anything else waits for an approver, and `brain held --instance demo` lists the waiting ones.

`brain read brief --instance demo` prints one read tool's text.

`brain serve --instance demo` runs the read-only MCP server on standard input and output.

`brain search-rebuild --instance demo` rebuilds the search index through the applier.

`brain router --instance demo` restores `ROUTER.md` from the engine template.

```json
{"mcpServers": {"brain": {"command": "brain", "args": ["serve", "--instance", "/absolute/path/to/demo"]}}}
```

## Backup and restore

`brain backup --instance demo --out backups [--label before-import]` writes one gzip bundle, `<name>-<utc>-seq<n>[-<label>].tar.gz`, and prints its receipt. `brain restore backups/<bundle> --dir restored` unpacks it into a new directory.

- **Holds:** the store, `brain.toml`, `ROUTER.md` and any `fetch/*.state.json` cursors, with a manifest of hashes and the write-log digest. It reads the cursors first and snapshots the store second, so a restored cursor is never ahead of the restored store.
- **Leaves out:** `fetch/inbox/` (document text is prunable), `fetch/mark.key` (a secret), `sessions.sqlite3` (the session record, a plain SQLite file to copy yourself), lock files, `PAUSED` and other bundles.
- **Safe to take:** a running instance is copied with SQLite's backup API, checked with `brain check` before anything is kept, written mode 0600, and read back before it counts. Two bundles of one store taken in the same second are byte for byte equal.
- **Pruning:** a `pre-update` label keeps only the newest two bundles of that label, and only after the new one has been read back. No other label is pruned.
- **Restore never overwrites:** the target must be missing or empty. It refuses a bundle member that isn't one of the five allowed names, a link or device, a database that isn't a rollback-journal SQLite file, a schema newer than the engine's, and any schema that differs from the engine's, such as a planted trigger or a `sqlite_stat1` table. It refuses rather than repairs. An older schema migrates after those checks.
- **Restore protects the machine, not the knowledge:** a bundle's rows and `brain.toml` are trusted as your own store. Restore only bundles you made.
- `brain backup` refuses while a redaction is running or unfinished, and brain check reports the same.

## Install and connect

```sh
sh install.sh --from <wheel>      # never by name: agent-si isn't on a package index
```

`install.sh` (about 80 lines of POSIX `sh`; read it first) installs uv if it's missing or older than its pinned minimum, installs the brain as a uv tool on uv's own Python 3.13 (`--managed-python`, so SQLite and FTS5 come with it), checks `brain --version`, and asks five questions to make your first brain. It stops if a `brain` command that uv didn't install is already on `PATH`. With no terminal to ask in (ssh without `-t`, CI, a container) it prints the `brain init` line to run instead and exits 0. Without `--from` (and with the old `--version`) it stops with exit 2 and points at the releases page, because uv would install whoever registers the name on a package index.

- **`brain init`** with no arguments asks: the brain's name (default `work`), its folder (default `~/Brains`), your name, your time zone (from `/etc/localtime`), and which detected agent tools to connect. With arguments it asks nothing: `brain init work --principal "Alex Example" --timezone Australia/Sydney`. Every instance is added to `~/.config/agent-si/instances.toml`, so `brain update` migrates it. `--no-register` leaves it out, for a scripted or throwaway brain such as a test or a scheduled rebuild. If you delete a registered brain's folder, `brain update` skips it and says so in `update.log`.
- **`brain connect claude codex gemini grok cursor hermes`** (or `all`, or any other name for the generic snippet) adds `brain-<instance>`, with the absolute path of the installed `brain` and `serve --instance <dir>`. It uses each tool's own command (`claude mcp add --scope user`, `codex mcp add`, `gemini mcp add -s user`, `grok mcp add -s user`, `hermes mcp add`), a strict-JSON merge for Cursor's `~/.cursor/mcp.json` (with a backup, and only after a yes or `--yes`), and prints the entry where it can't write safely. It never writes a file that isn't strict JSON, or through a link. After writing it runs the exact command through `initialize`, `tools/list` and `brief`, because a configured entry alone is not proof, and reminds you to restart the tool.
- **`brain setup`** (1.13.0) is the one-command path: `brain setup [--yes] [--principal NAME] [--name work] [--dir ~/Brains] [--timezone ZONE] [--no-connect]`. It makes the brain, registers it and connects every detected tool with `--yes` semantics, then prints each tool's result and the command that undoes it. It asks one question at most, "Your name", with the account's full name as the default (`id -F` on macOS, the passwd name elsewhere), read from `/dev/tty`; `--yes` or `--principal` asks nothing, and with no terminal and no `--yes` it takes the defaults and says so. If the brain already exists it is kept and only connected. It schedules nothing (no `launchctl`, no update check) and never installs or checks anything by name. Exit 0 when the brain exists and every connected tool's smoke test passed, 1 when a tool failed, 2 for a refused option, 5 when `brain` isn't installed as a uv tool. `tools/bundle.py` builds the single-file installer that ends in `brain setup`; see the README.
- **`--print`** writes nothing. **`--remove`** takes out our entry, and the marked line if one was added.
- **"Call brief first":** Claude Code, Codex and Gemini CLI give the model the server's instructions. For Grok and Hermes, `connect` offers one marked line in `~/.grok/AGENTS.md` or `~/.hermes/AGENTS.md` (only when the file exists, and never through a link), and `--remove` takes it out byte for byte. `docs/COMPAT.md` records what was checked, per tool and version.

## Usage

`brain serve` keeps a content-free log of read calls in `<instance>/usage/YYYY-MM.jsonl`: the minute, the tool, the agent tool's name, the result's size and rows, an outcome (`hit`, `empty`, `unknown_key`, `ambiguous_key`, `disputed` or `error`) and the time taken. A `search` line also says whether any word counted (`relaxed`, from 1.6.3). Never a query, a title, a name, an id, a key, a value, a quote or a path. `[usage] record = false` turns it off. `brain usage report --period week` (or `month`) writes a Markdown and a JSON summary beside it: hit rate, misses, disputed reads, how often a session started with `brief`, the held queue, read sizes and how many searches fell back to any word. It stays on your machine unless you send it to someone.

## Updates

New releases are on the [releases page](https://github.com/joelbrilliant/agent-si/releases). Nothing in the engine looks for them, fetches a package by name or schedules a job, because agent-si isn't published on a package index. To update, run the installer file from the page again (it upgrades agent-si and keeps your brain and its session record), or give `brain update` a wheel you downloaded:

```sh
brain update --from-wheel PATH-TO-THE-WHEEL
```

`brain update` is safe for an agent to run. It prints one JSON receipt and exits 0 (updated or current), 1 (refused) or 5 (failed: rolled back, paused, or stopped before anything changed).

1. **Preflight:** one update at a time (`~/.local/state/agent-si/update.lock`); the installed `brain --version` must start; every instance passes `brain check`, isn't `PAUSED` and has free disk of at least twice its database. The running version is packed back into a wheel in `~/.local/state/agent-si/previous/` so a failed update can put it back without downloading anything. An editable or source install can't be packed: pass the wheel of the version you run with `--rollback-from`.
2. **Stop writes:** `UPDATING` goes in each instance. Every writer checks it after taking the store's write lock, and the update then takes that lock once itself, so no write can land after the backup. Collectors hold their cursors and catch up next run. The brief says `Updating; writes resume shortly.`
3. **Back up:** a `pre-update` bundle per instance in `<instance>/backups/` (the newest 2 are kept).
4. **Upgrade** with `uv tool install --reinstall --managed-python --python 3.13` from the wheel, then **migrate** each instance with the new engine and **check** it: `brain --version`, `brain check`, `brain router` and an MCP smoke test (`initialize`, `tools/list`, `brief`).
5. **Any failure from the upgrade on** is rolled back in the same run: the old version is reinstalled from the wheel kept in step 1, each migrated database and any `-journal` beside it move aside together as `broken-<utc>.*`, the pre-update bundle is restored and checked with the old engine. If that check fails, `PAUSED` says what was restored and from where.

Instances come from `~/.config/agent-si/instances.toml` and any `--instance DIR`. `brain update` without `--from-wheel`, and `--to` and `--auto`, refuse and name the releases page. `--check` makes no network call and says where the releases are. `--status` shows the last result and any `UPDATING` or `PAUSED`.

**No automatic updates.** `[updates] auto` defaults to false and nothing acts on it. `brain schedule on` refuses; `brain schedule off` removes a nightly job an earlier version installed.

**Checking a download:** the release page lists a SHA-256 for the installer in `SHA256SUMS`; check it with `shasum -a 256 -c SHA256SUMS` (macOS) or `sha256sum -c SHA256SUMS` (Linux) before you run it. The installer checks the two wheels inside it against its own list before it installs anything. `brain update` doesn't check a signature.

**A running agent tool** keeps its old engine until it restarts. After an update, its next tool call answers `unavailable: brain updated to X, restart this agent tool`.

**An update that stopped part way** (a power cut, a killed process) leaves `UPDATING` behind, and writes stay stopped. `brain update --status` names it `updating_stale` once its process has gone. Then run `brain check --instance <dir>`: if it passes, delete `UPDATING` and run `brain update --from-wheel` again; if it doesn't, restore the pre-update bundle as below.

### When a defect turns up later

There's no late rollback command. Fix forward with a patch release. In an emergency:

1. Reinstall the previous version from its wheel: `uv tool install --reinstall --managed-python --python 3.13 PATH-TO-THE-PREVIOUS-WHEEL` (the last one `brain update` replaced is in `~/.local/state/agent-si/previous/`, and every release's wheel is on the releases page).
2. Restore the pre-update bundle into a new folder: `brain restore <instance>/backups/<bundle>.tar.gz --dir <new folder>`, and point the agent tools at it.
3. List the writes made since on the old store with `brain receipt`, then let each writer resend:
   - **Collectors** catch up by themselves, from the cursors in the bundle: run `brain-fetch run --instance <new folder>`. Collect is idempotent, so nothing already held is doubled.
   - **Extractors** run `brain extract` again on the sources captured since.
   - **Agents** resend their direct packets with `brain apply`, which is idempotent by `change_id`.

## Intake

The `capture` collector turns exported text into sources: a changed document becomes a new version of its source, a removed one gets a removal version, and a mass change (more than 20 documents, or at least 5 and more than a quarter of the folder) is refused unless `--allow-mass-change` is given. When a source gets a successor, the applier carries claims whose quote is still in the new text onto it, and opens one `drift` signal for each claim whose quote is gone. Decisions are left alone, and `get` shows `source_current: false` for a cited source that has a newer version.

`brain-fetch` reads one local folder and writes exports to `<instance>/fetch/inbox/<collector>/`, then runs `brain collect` on them:

- `brain-fetch setup documents --instance demo` prints the `brain.toml` to add. `check` lists what each file type read, every unreadable file and any problem. `run` captures; `schedule` installs one launchd job (a Mac) or prints systemd units.
- **Folder rules:** Markdown, text, Word (`.docx`) and PDF are read. Hidden names, `~$` names, links, unsupported types and files over 64 MiB are skipped. A directory or file it can't read (a permission error, a stalled mount) makes the run partial, so nothing is removed for it, and `brain-fetch check` names the path and is not ok. A path whose ref passes 500 characters counts as an unreadable file. An encrypted, corrupt or undecodable file is unreadable until it changes; it never blocks the run.
- **`[pdf]`:** Installing the tool with `--with pypdf==6.19.0` added to the `uv tool install` line adds PDF text (`pypdf`, pinned). Without it a PDF is captured as a stub and read again once the extra is installed.
- **Meeting transcripts** (`.vtt`, `.srt`, `.transcript.txt`, `.transcript.json`) are captured when a collector's kinds includes meeting_transcript. Remove one with brain redact.
- **`[fetch]`:** `stale_after_runs` (default 3) and `every_minutes` (default 30, the `schedule` interval).
- **The Intake line:** `brain read brief` adds `Intake: <name> failing since <date> (<cause>)` after three failed runs in a row, or `silent since <date>` when a collector brain-fetch runs has not run for a while. A collector that stays `partial` is not raised in the brief; `brain-fetch check` names the file.
- **Credentials never go in `brain.toml`:** a credential-shaped key or value is refused. Secrets live in `/run/secrets/<collector>` or the macOS keychain. That promise covers credentials the tool handles: document text is stored as written, and a token inside a document is not scrubbed.
- **The mark:** only `brain-fetch` can say an item is the principal's own, by an HMAC under `fetch/mark.key`. A hand-written export can't. The key is readable by the same user, so an agent with a shell could sign; the mark stops copied flags and edited exports, not a determined local agent.
- `brain apply-config` applies a config of more than 500 operations in batches of 500.

**Email, Slack, calendar and GitHub** (0.8.0). `brain-fetch setup gmail|slack|calendar|github --instance demo` prints the app to create, the read-only scopes, where the credential goes and the `brain.toml` to add. `brain-fetch check` makes one read-only call to test the credential. Nothing else in the engine touches the network: `brain_fetch/net.py` is the only module that does, over HTTPS to the five service hosts, and it never follows a redirect.

- **Gmail** keeps the text of mail matching `live_query` (default: inbox and sent, minus Promotions, Social, Updates and Forums, last 14 days). Quoted history and signatures are dropped, and attachments are listed by name and size only. A `threads` gmail collector with `with_subject = false` keeps reply tracking without subjects. An OAuth app left in Google's "Testing" status gets refresh tokens that expire after 7 days, which shows as `failing (auth)` on the Intake line: use a Workspace-internal app.
- **Slack** reads only the listed channels, and the principal's DMs with `include_dms = true`. Bot and app posts are dropped. A thread becomes one source once it has been quiet for 24 hours, and an edit made within a day becomes a new version.
- **Calendar** feeds the existing `calendar` collector (`live_calendar_id`). No text is captured.
- **GitHub** captures the Markdown files on the default branch and the release notes of one repository, as a full listing, so a deleted file gets a removal version.
- **Privacy:** for email and Slack, addresses become `[email]` and phone numbers `[phone]` before anything is stored or written to the inbox. The patterns are a backstop, not a guarantee: on the test set of unusual formats they miss 10% of addresses and 7.5% of phone numbers. Remove anything they miss with `brain redact`.
- A failed pull (a refused credential, a rate limit, a server error) still writes an export, so the run is recorded and the Intake line can say the collector is failing. The cursor moves only after a complete export is collected.

## Redaction

`brain redact SOURCE_ID --reason "privacy request 2026-11-02" --instance demo [--yes] [--operator NAME] [--backups DIR ...]` removes one captured item and every copy the brain derived. It prints what it will rewrite, delete and keep, and asks you to type the source id (`--yes` skips the question; an agent must pass it). It cannot be undone.

- **Every version of the item goes:** a changed document keeps its older versions, and a claim carries its quote from one version to the next, so the whole chain is redacted together.
- **The scrub:** the source becomes a tombstone (`redacted on <date> by <operator>: <reason>`). Claims, facts and their history, decisions, notes, thread subjects, meeting titles, held and refused packets, errors and the write log's packets lose the text: text, enum and url values become `[redacted]` (a url `https://redacted.invalid/`) and quoted spans the evidence gate proved were in the source become `[redacted]` in notes. Claims become `rejected`, headlines are worked out again, held packets become `rejected`, and each fact and decision it touched opens a drift signal.
- **Before and after:** it refuses, before it changes anything, while `brain check` reports a problem, and the plan lists note rows that still hold the removed text without quote marks (listed, not removed). The same text is refused under any id. A live collector's redacted subject or title stays redacted on its next poll.
- **What it leaves:** numbers, dates and true/false values, the item's name (id, `external_ref`, locators and labels), people and system rows, any note that copies the text without quote marks or restates it, and hashes. A hash can confirm a guess at the removed text.
- **Compaction:** it rebuilds the search index, runs `VACUUM`, deletes the `brain-fetch` export files that hold the item, takes a `post-redaction` backup and replaces every older bundle of this brain in `<instance>/backups/` and in each `--backups` folder.
- **Everything else is the runbook:** Time Machine, synced folders, offsite buckets, snapshots and agent session logs are yours to clear. See [docs/REDACTION.md](REDACTION.md).
- **The original file:** the command warns when the original is still in a collector's folder, and never deletes it. `brain-fetch run --rescan` would copy it back.
- **After a stop:** run the same command again. Until it finishes, `brain check` fails and `brain backup` refuses, because the file still holds free pages.
- **Check:** `brain check` reports a redaction that did not finish, any leftover copy of a redacted source, and any change to the engine's triggers.

## Past sessions (1.15.0, small pages from 1.16.0)

`sessions.sqlite3` in the instance folder is the session record: what was said in past Claude Code, Codex, Grok and Hermes sessions, filed by code. It is not the brain. It has no change packets, no write log and no gate, and it never opens `brain.sqlite3`.

- **What is filed:** the user's and the assistant's text of each session, in order. Tool calls, tool output, subagent transcripts, cron runs and the context a tool writes for itself are left out. A message that matches a secret pattern is never stored, and a message is cut at 32,000 characters.
- **Typed while the assistant is working (from 1.16.1):** Claude Code hands such a message to the model part way through the turn and writes it as an attachment row, not a user row. It is filed in its place as the user's, when Claude Code marks the row as a person's. A finished task, another session's message and a row that names no origin are left out.
- **When:** `brain setup` and `brain connect` build it, every local `search` syncs it first (a sync with nothing new only lists the folders), and `brain sessions sync` does it by hand. One process syncs at a time; another one's search uses the record as it is.
- **A record, not a mirror:** a message is added once under the tool's own key and never rewritten. A session the tool has deleted stays. `brain sessions forget <session id>` removes one and lists it so no later sync brings it back.
- **Where from:** `~/.claude/projects`, `~/.codex/sessions`, `~/.grok/sessions` and `~/.hermes/state.db`, or `CLAUDE_PROJECTS_ROOT`, `CODEX_SESSIONS_ROOT`, `GROK_SESSIONS_ROOT`, `HERMES_HOME` and `HERMES_SESSION_DB`. Extra Hermes profiles go in `brain.toml`: `[sessions]` `hermes_profiles = { side = "~/.hermes/profiles/side/state.db" }`.
- **Reading:** `search` adds `sessions` beside `hits`: up to 3 sessions with one excerpt each, as leads. When the brain has no hit they come deeper, up to 5 sessions with 3 excerpts, and with `entities: ["session"]` up to `limit` sessions with 3. Each excerpt has its message number `n`, who spoke and when. A session's `id` ends in the first excerpt's number (`claude:0f3a...#12`), so `get` with `entity: "session"` and that id opens the session at that message, and the same id with another excerpt's `n` opens it there. A page is 3,000 characters (from 1.16.0, where it was 8,000), each message under a header line such as `[12] user 2026-11-09 14:02`. It starts at the message, or at the earliest message before it that begins within 600 characters, so a short question comes with its answer and a long message before doesn't use the page up. The id without a number opens the session at its start, and `offset` (a page's `next_offset`) pages on. `brain sessions excerpt` and `forget` take the id with or without the number. Times are in the brain's time zone.
- **The asking session is left out** of its own search where the tool names it: Claude Code gives the servers it starts `CLAUDE_CODE_SESSION_ID`.
- **Only this machine reads it:** `brain serve` and `brain read`. A hub connection and a folder token never get sessions, and asking for them is refused with the reason.
- **Quoting:** `brain sessions excerpt <session id> --from N --to M` prints an `add_source` op of kind `document` with `external_ref` `session:<session id>#N-M`, at most 50 messages. The applier re-reads those messages and refuses the source unless its content is exactly theirs, and refuses an author on it. The source then stays in the brain whatever happens to the session.
- **The switch:** `[sessions] record = false`. Then nothing syncs, `search` returns the brain alone, and a read that asks for sessions is refused with how to turn it on.
- **After an update from before 1.15.0:** nothing builds the record until the first local `search`, which then takes as long as a first build (about half a minute for a few thousand sessions). Run `brain sessions sync` first to do it up front.
- **After an update from before 1.16.1:** the first sync reads every Claude Code session again, changed or not, and adds the messages typed mid-turn that the record lacks. They go after the messages a session already holds, each with its own time, because a filed message's number never changes. It is one full read of the transcripts (15 seconds for 2.4 GB where it was measured), so run `brain sessions sync` to do it up front. An older engine sharing the record leaves it as it is.
- **What a tool writes for itself is left out:** Claude Code's task notifications, side-session and CI events and reminder blocks, Codex's process warnings and approval-review transcripts, Hermes' compaction notes. Other tool-written turns are still recorded.
- **Not yet:** the record isn't in `brain backup` bundles or the hub, and it doesn't hold tool events.

## Reading the brain

`brief` ends with three short sections before its `Last change:` line: open actions and checklist items due in the next 7 days (overdue ones flagged, with who they wait on), the oldest waits, and each active project's checklist and open-action counts. `get` on a project adds `related.progress`: checklist items by state and percent done, actions by status, who the open work waits on, and the next 5 dates due. Without an `entities` filter, `search` keeps at most 3 sources among its first hits, and further sources follow all other hits. From 1.10.0, `get` on a project also lists `related.recent_records`: the 3 records in it that changed last, newest first, with superseded and archived ones left out. Every search hit carries `date`, a source's own date or the local day any other row last changed. A query whose words are exactly a project's id or title starts with that project and the record in it that changed last, each marked `pinned` with no score, on top of the limit and only on the first page. From 1.11.0, `search` ranks every query by any word, so a record missing one of the words still ranks, and bm25 puts hits with more and rarer words first. `match` shows the any-word form. `relaxed` still means no result has every word, on every page. A hit carries `"match": "note"` when a word it matched is only in its note. From 1.12.0 a hub token that names folders reads through `brain.scope` and sees only those folders; see Team hub.

## Change packets: `base`, and what a refusal tells you

An upsert op may carry a `base`, the row's `version` from `get` or `records_query`. It is optional. An op with no `base`, or `"base": null`, creates the row when it doesn't exist. When the row exists, the op is a `conflict` that names the current version (`actions act-x already exists at version 3; to update it, send "base": 3`), so send that version. A `base` for a row that isn't there is a `conflict` too: leave `base` out to create it. A `base` that has fallen behind the stored version is a `conflict` that says to re-read the row first. Older packets that send `"base": null` on every op work unchanged.

Every refusal names its fix, whether it came from a read tool, `brain validate packet` or `brain apply`: the argument to use instead (`order_by` with `desc`, not `order`; `filters`, not `where` or `query`), the allowed names or values, the maximum or minimum, an example timestamp such as `2026-10-14T00:00:00Z`, the operators a field takes, every missing required field in one message, and a database rule in words (for example "set closed_at when status is done or dropped, and leave it null otherwise", or "project_id nope does not exist; records_query on project lists the ids"). The wording is built in `src/brain/advice.py`, and `tests/test_error_fixes.py` holds one bad input for each message and fails, naming the input, when a message loses its fix. A bare string on a text field in `records_query` is an exact match, so an empty result says so and shows the `contains` form.

## Importing from another store

Older stores hold text written before the evidence gate. A manual-source collector agent with `legacy_import = true` in `brain.toml` can insert such rows anyway.

- **Allowed:** inserting a new project, record, action or checklist item straight from a packet. The row is stored, and each sentence the gate would have refused is recorded in an `unchecked_text` marker.
- **Not allowed:** updating an existing row past the gate, decisions, signals, and collector or extractor candidates. They keep the normal gate.
- **Marker:** it only shrinks. When a later update drops a flagged sentence, the marker loses it, and it disappears once none remain.
- **Seeing it:** `get` adds an `unchecked` object, `search` hits carry `"unchecked": true`, `records_query` has an `unchecked` boolean field to filter on, and `brief` adds an `Unchecked text:` line while any marker exists.
- **Treat it as unverified:** until a source confirms a flagged sentence, don't rely on it.

Actions and checklist items also hold ordered `owner_person_ids` and `waiting_on_person_ids` lists, and the single-person fields hold the first of each. A write that sends only a single-person field keeps the list when it repeats the current first person, and otherwise replaces the list. This includes collector candidates, so a candidate with no waiting-on person clears that list.

## Product truth

Each fact holds several sourced values, called claims. Only the applier writes `facts`: it works the headline value and status out from the claims in the same transaction.

- **Claims:** `add_claim` stores one quoted value for a key and an optional version `scope`. `upsert_fact` is shorthand for a claim of the sender's own that replaces its earlier one. A newer version of the same source, or a writer correcting itself, updates the value. Any other disagreement sets the fact `disputed` and opens a `conflict` signal.
- **`[truth]`:** `entities` and `attributes` give aliases, so `fact_get` takes words such as "Example Cloud VRAM". `source_rank` orders the claims and suggests one during a dispute, but it never settles one.
- **Signals on a fact:** `fact_get` lists a fact's open signals under `claims.signals`, as `get` does for an action or checklist item. The dispute's own conflict signal is left out, and with no other signal the field is too.
- **Disputes:** `fact_get` leaves out the value and lists each side under `claims`, with its source. `get`, `records_query` and `search` print `disputed` in place of the value, and `brief` adds up to 3 `Disputed ...` lines.
- **Settling:** only `resolve_fact` settles a dispute. It names every live claim of the bucket (read them with `"claims": "all"`) and quotes a source written by a named person. It applies directly from an `author_principal` agent. From any other agent it is held for that agent's approvers, and rejected when it has none. So without an `author_principal` agent or approvers, every resolution is held or rejected, as in the demo. The reserved agent `brain` can't send one in this version.
- **Numbers:** a `number` key holds a float and loses integer precision above 2^53, so whole quantities that large belong in an `int` key.
- **Upgrading:** after `brain migrate` to schema 5, run `brain apply-config` when `brain.toml` has a `[truth]` table, or `brain check` reports that the aliases differ.

## Strict writes

From 0.7.0a2, facts and state dates are checked against their cited source, and notes are labelled for what they are.

- **Facts:** `upsert_fact` with a value needs `quote`, a passage of the cited source, and `value_raw`, the value as the quote writes it (`128K` for 128000). Both are checked, and the claim is stored as checked. Collectors quote their own evidence entry, such as `{"fact":"ci_state","value":"success"}`.
- **Dates:** a packet's `due_date`, `asked_on` and `decided_on` come from the cited source: its date, the ISO date, a day word such as `tomorrow` or `Friday`, or a date in words such as `14 September 2026` or `14/09/2026` (always day first).
- **Field classes:** every text column is a fact, state, label, note, or evidence field. Notes (details, next steps, close reasons, descriptions, goals and record bodies) are capped, recorded with who wrote them and when, labelled "Written by an agent, unsourced. Not a fact." on every read, and never used for facts.
- **`[writes]`:** `note_max_chars` (100 to 2,000, default 500) caps notes. `notes = false` refuses any write that sets a note to text, so no record can be created, though a note can still be cleared or re-sent unchanged.
- **Router:** `[writes] notes` selects the router's notes line (`ROUTER.md` says notes are unsourced, or that they are off). The router is rendered, not live: after changing the switch run `brain router --instance <dir>`, and until then `brain check` reports that `ROUTER.md` differs from the engine router.
- **`value_words`:** a bool or enum key's `[[fact_keys]]` entry may map values to words, such as `{true = ["included"], false = ["not included", "not enabled"]}`, which then count as that value in a raw and in a quote.
- **Extractors:** under `approval = "verified"` (the default) a candidate that passes every check is stored, at most 25 an hour per extractor. Decisions, inferred actions, claims that would open a conflict and anything failing a check are held. `"required"` holds everything but records what `verified` would have done, and the brief's `Held:` line counts held packets and names the keys that would open a conflict.
- **Relay:** an agent relays a person's yes with `brain approve <id> --as <approver> --relay`. `approved_via` records `agent-relay` for anything not typed by a person at an interactive terminal (a relaying agent, a script or a cron job), and `terminal` otherwise.
- **Legacy claims:** a `legacy_import` agent's `add_claim` may leave out the quote and set `status`, `superseded_by` and the resolution fields, and the claim imports unchecked. An imported fact's staleness starts at the import, and a legacy rejection rests on the importer's record, not a person's words. `verify_claim` later checks such a claim against a quote from its own source. It checks the quote, not that the value is still current, so the fact's staleness keeps counting from the import or the last writer's confirmation.
- **Actions:** `proposed` actions aren't open work. The brief counts them apart, and `records_query` on `action` takes `{"open": true}` for every open status.
- **Folders (1.5.0):** a project's `parent_id` puts it inside a workstream or another project, up to 8 deep. `get` on a project shows `related.folder` (its path, the folders inside it and the open work in its whole branch), and `records_query` takes `{"under": "<project id>"}` on `project_id`, `parent_id` or a project's `id`. An unknown folder is an error.
- **Agent friendly (1.6.0):** `brain validate packet FILE --instance DIR` dry-runs apply and gives its verdict. `search` falls back to any word when no result has every word, and marks those hits `relaxed`. Every refusal names its fix. An op without `base` creates the row. One-line fields refuse every line break `splitlines()` knows. The rules for agents working on the engine, and what enforces each, are in `AGENTS.md`.
- **Brief counts (1.6.3):** the `Actions:` line in `brief` ends `Overdue: N yours, M waiting on others.`, or `Overdue: none.` Yours is an open, past-due action the principal leads that waits on no one: status `open`, `prepared`, `scheduled`, or `blocked` without a waiting-on person. Without a principal it reads `Overdue: N.` `records_query` on `decision` breaks a tie on its `order_by` field by `created_at`, when the decision was first recorded, in the same direction, then by id, so an edit never reorders decisions. Each `search` line in the usage log says whether any word counted (`relaxed`), and `brain usage report` counts them.
- **`[extraction]` is refused:** remove it, then register fact keys under `[[fact_keys]]` and their aliases under `[truth]`.

## Links with sources (1.7.0)

Schema 8 (`0008_links.sql`). A record, action, checklist item or decision cites one primary source and any number of others.

- **Writing:** `upsert_record`, `upsert_action`, `upsert_checklist_item` and `upsert_decision` take an optional `sources` list beside `source`: up to 20 items of `source_id`, `locator` and `label` (`cites`, the default, or `status`, `due`, `asked` or `summary`). The envelope's `source` stays the row's primary citation, and each item becomes a row of the append-only `links` table. A source must exist or be added earlier in the packet, and a redacted one is refused. A link that already exists (same row, label and source) is left alone, and new ones appear in the receipt's targets under table `links`. An upsert with only the id and `sources` adds links: it needs `base` like any update, and the row's version doesn't move.
- **The gate:** a new sentence is checked against every cited source. A quote of 3 or more words passes when any cited source holds it. Words put in the principal's mouth need 5 quoted words from a cited source the principal wrote, and a decision's quote must be in a cited source (the principal's own for a decision by the principal). A state date passes when any cited source allows it. A legacy import still flags rather than refuses.
- **`summary`:** `upsert_record` takes a `summary` of up to 1,200 characters. It is gated text like the title, each `record_history` version keeps it, and redaction scrubs it as it does the body.
- **Original times:** a `legacy_import` agent may set `created_at` and `updated_at` on an insert of those four ops. Both are UTC timestamps ending in Z, `updated_at` is not before `created_at`, neither is after the apply time, and one alone stands for both. On an update, or from any other agent, they are refused.
- **`get`:** on those four entities, `related.sources` lists what the row cites: the primary first as `{"label": "primary", "id", "locator"}` (the result's `source` describes it in full), then each link with its label, locator, title, kind, source date and author, at most 20 with a `total`. On a source, `related.cited_by` lists the rows citing it, as their primary or through a link, with entity, id, title and label, at most 20 with a `total`.
- **`records_query`:** a record's `summary` is a default field and a text filter. `cites` on records, actions, checklist items and decisions takes a source id (or `{"in": [...]}`) and matches rows citing it either way; as a field it lists every source the row cites, its primary first. An unknown source is an error that says `records_query on source` lists the ids.
- **`search`:** a record's summary is indexed after its body, so search finds a record by its summary, and such a hit is not marked `"match": "note"`. A record without a summary indexes exactly as before.
- **`under`:** `brief` and `search` take `under`, a project id. The brief then counts and lists only that folder and every folder in it (workstreams, projects, actions, the overdue split, the checklist, unchecked text, signals, what is due, the waits and active projects) and says so on a `Folder ...` line after the first; facts stay brain-wide. Search returns only records, decisions, actions, checklist items and projects filed inside the folder. An unknown project is an error naming `records_query on project`.

## Benchmark and Tier A

- `bench/` holds the invented Example Co benchmark: 40 people, about 300 sources, 24 questions, a markdown copy and the packets that build it. The committed copy is `bench/exampleco/`.
- Regenerate it with `python bench/generate.py --seed 1 --scale 1 --today 2026-10-12 --out <empty directory>`. The output is byte for byte the same on Python 3.11 and 3.14.
- `python -m bench.harness measure` builds the instance and prints the figures (`--no-timing` leaves out read times).
- Tier A is `python -m pytest -q tests/test_bench_gate.py`. It runs with the whole suite, calls no model, and checks the figures against `bench/budgets.json`.
- A budget changes only with a new changelog line and the fingerprint from `python -m bench.harness fingerprint`.
- It supports saying: reads are deterministic on the same data, tested on every release; startup bytes; read speed; per-item prefix bytes are recorded, not capped; and the honesty rules below.
- It doesn't support saying: what an agent answers, that the brain "beats markdown", tokens beyond bytes / 4, or determinism across rebuilds of the whole store.
- Answers are graded by the rules in `bench/grading.py`: a strict and a lenient score. The lenient one forgives a stale mention of a trap, is an upper bound, and is never quoted alone. Traps measure what an agent asserts, not what it mentions, and the brain arm can't see the trap strings. q04 credits the brain for its label and needs the words `Project Heron`; the note traps credit it too.
- Don't quote an answer score without a control arm: the markdown arm gets the same one-line notes instruction as the router and the same date.

## Team hub

One hub serves one brain for one company, from one container, behind Caddy, which handles TLS. A token reads the whole brain unless it names folders (see Folder tokens): capture only what every whole-brain token holder may read, or run a second hub.

- **Serve:** `brain hub serve --instance DIR [--host 127.0.0.1] [--port 8765] [--origin URL]`. The port is never published; Caddy is the only thing in front of it.
- **Tokens:** `brain hub token --instance DIR issue --agent claude --scope write --label "Alex Example laptop, Claude Code"` prints the token once (`abt_<public id>_<secret>`); only its SHA-256 is stored, in `hub.sqlite3`. `list` and `revoke <public id>`. Tokens last 365 days. The reserved agent `brain` never gets one. A token goes only in `Authorization: Bearer`; one in the address is refused.
- **Folder tokens (1.12.0):** `brain hub token --instance DIR issue --agent claude --scope read --folder demo [--folder other] --label "..."` limits a read token to those folders (each must exist; read scope only). The token sees each named folder, every folder inside it (worked out on every request), the rows filed in them (`records`, `decisions`, `actions`, `checklist_items`, `meetings`), the rows that belong to those (people lists, thread links, links, unchecked text, note meta, record history, signals on them), and the sources, people and threads they point at. Facts, systems, the write log and records filed in no folder are hidden, and a column that references a hidden row reads as empty. One module, `brain.scope`, enforces it: `scope.reader(folders)` opens the read connection and shadows every table and view with a filtered temporary view, and `scope.RULES` classifies each table (a test fails naming any that isn't). `reads.run_tool(..., folders=...)` takes the folder ids. `brief` keeps its header, a `Folders:` line, actions, checklist, unchecked text, signals and the due, waiting and project sections over the branch, and drops facts, truth, held, intake, update, stale-fact and last-change lines. `search` always searches the branch. `fact_get`, and `get` or `records_query` on `fact`, `system` or `claim`, are refused with code `folder_token`. The MCP handshake names the folders and points at `search` and `records_query`. `GET /v1/receipts/<id>` and `GET /v1/held` answer 403. `brain serve` and `brain read` take no folders.
- **Your token decides your agent:** a packet under any other agent's name, or whose `change_id` doesn't start with the token's agent, is refused with 403 before anything is logged. Approvals take the approver from the token.
- **Routes:** `POST /mcp` (the six read tools, JSON only; GET and DELETE get 405), `POST /v1/apply`, `/v1/extract` (extractor tokens only), `/v1/validate`, `/v1/changes/<id>/approve` and `/refuse`, `GET /v1/receipts/<id>`, `GET /v1/held`, `POST /v1/read/<tool>`, `GET /v1/whoami`, and `GET /healthz` and `/readyz` with no token. A write's body is byte-identical to `brain apply`'s output, with the exit code in `Brain-Exit-Code` and in the status (0 is 200, 1 is 422, 2 is 400, 3 is 409, 4 is 202, 5 is 503); a resend gets the original receipt and `Brain-Duplicate: true`.
- **Limits:** 16 requests at once (a 17th gets 503 and `Retry-After: 1`); bodies at most 64 KiB for reads and 5 MiB for writes; 20 failed sign-ins per address in 5 minutes (checked after the token, so a valid token is never refused by it, and past it a bad token gets 429 with no audit row), 600 reads and 120 writes per token a minute, and 60 rejected writes per token an hour; an `Origin` header not on the allowlist (empty by default) gets 403. A refusal sent before the body is read closes the connection (`Connection: close`, from 1.12.1), so a proxy that reuses connections never reads that body as the next request.
- **Audit:** every write, edge refusal, failed sign-in and token change is a row in `hub.sqlite3`, kept 400 days, with client addresses cleared after 90. `brain hub audit --instance DIR --since 2026-10-01 [--agent alex]`. Reads go to the usage log with the token's label. Standard output carries the route template, status, token id and duration only.
- **From a laptop:** `export BRAIN_INSTANCE=https://brain.example.co` and `BRAIN_TOKEN=...`, then `brain apply`, `extract`, `held`, `approve`, `refuse`, `receipt` and `read` work as locally (or pass `--remote URL`). `http://` is refused except to this machine's loopback address.
- **Agent tools:** `brain connect claude cursor --url https://brain.example.co/mcp --token-env BRAIN_TOKEN --name brain-work` writes each tool's own form with the token read from the variable: Claude Code and Grok `${BRAIN_TOKEN}`, Cursor `${env:BRAIN_TOKEN}`, Codex `--bearer-token-env-var`. Gemini CLI and Hermes can't read it from a variable, so `connect` prints the entry and the token is pasted into that tool's own settings.
- **Router:** `brain hub router` prints the shared brain's router (under 2,048 bytes).
