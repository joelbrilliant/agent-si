# Install agent-si, for an agent

This file is for an AI agent installing agent-si for a person. It uses only this repository's public release. Do the steps in order, stop at the first one that fails, and say what you saw.

agent-si keeps a checkable memory (facts with their sources) in one SQLite file on the person's machine. The installer also installs agent-memory, a second tool by the same author that searches their notes and past agent sessions. Joel Brilliant built both.

## 1. Ask first

Tell the person, in plain words, that the installer will:
- install `uv` (a Python tool installer) if they don't have it, and agent-si and agent-memory as `uv` tools;
- make a brain in `~/Brains/work`;
- add an MCP entry for the brain, and one for agent memory, to each agent tool it finds (Claude Code, Codex CLI, Gemini CLI, Grok Build, Hermes, Cursor);
- read their past agent session files (Claude Code, Codex, Grok) once, to build a local search index. It never indexes anything else, such as documents, mail, chat or notes folders, and nothing leaves the machine.

Ask for their full name (the brain uses it to know which writes are theirs) and whether they want their agent tools connected. If they don't, you will add `--no-connect`. Don't run the installer before they say yes.

## 2. Check the machine

macOS or Linux, x86_64 or arm64, with `curl` and either `shasum` or `sha256sum`:

```sh
uname -sm
command -v curl shasum sha256sum
```

## 3. Download and check the installer

Work in a new empty folder:

```sh
mkdir -p ~/Downloads/agent-si-install && cd ~/Downloads/agent-si-install
curl -fLO https://github.com/joelbrilliant/agent-si/releases/latest/download/install-agent-si.sh
curl -fLO https://github.com/joelbrilliant/agent-si/releases/latest/download/SHA256SUMS
shasum -a 256 -c SHA256SUMS        # on Linux: sha256sum -c SHA256SUMS
```

It must print `install-agent-si.sh: OK`. If it doesn't, stop and tell the person: don't run the file. The script can't be piped into `sh`, so don't use `curl | sh`.

## 4. Run it

```sh
sh install-agent-si.sh --yes --principal "<the person's full name>"
```

`--yes` asks nothing else. Add `--no-connect` if they didn't want their agent tools connected. Other options are in `sh install-agent-si.sh --help` (`--name`, `--dir`, `--timezone`). It takes a minute or two. It checks its two wheels against the sums inside it before it installs anything, and it never installs anything by name from a package index.

Do not install agent-si or agent-memory any other way, such as `pip`, `pipx`, `uvx` or `uv tool install` with just the name: they aren't published on a package index, and a name there could belong to someone else.

## 5. Confirm it works

A new terminal may not have uv's tool folder on its `PATH` yet, so use the full path:

```sh
"$(uv tool dir --bin)/brain" read brief --instance ~/Brains/work
```

Use the folder the installer printed on its `Brain ...: created at` line if it isn't `~/Brains/work`. It should print a short brief for the new, empty brain: its name, and zero actions, facts, projects and signals. If it fails, show the person the error and stop.

## 6. Tell the person what happened

Report, from the installer's own output:
- the versions installed, and the brain's folder (`~/Brains/work` unless they chose another);
- which agent tools were connected (lines like `claude: written, smoke test pass`), and which weren't;
- that agent tools must be restarted before they see the brain;
- how to undo each part:
  - `brain connect all --remove --yes --instance ~/Brains/work` and `agent-memory connect all --remove` take out the tool entries (the installer printed an undo line for each);
  - `uv tool uninstall agent-si` and `uv tool uninstall agent-memory` remove the tools;
  - the brain folder is the person's data and is left in place. Back it up with `brain backup` before deleting it.

Then suggest they start their agent tool and ask: **read my brain brief.**

## Later

- To update, repeat steps 3 and 4 with the newest release (the installer upgrades both tools and keeps the brain), or run `brain update --from-wheel <agent-si wheel>` with a wheel from https://github.com/joelbrilliant/agent-si/releases.
- To bring in documents, mail, Slack, calendar or GitHub, the person runs `brain-fetch setup <service>` themselves. Don't do that without asking them: each needs a credential only they can give.
