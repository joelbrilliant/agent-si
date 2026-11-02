# Compatibility

What `brain connect` was checked against, with what result. A row is only as good as its date: each harness changes its MCP commands often, so re-run `tools/accept_connect.py` before relying on an old row.

**How these rows were made (12 October 2026, macOS arm64, engine 0.9.0a1 installed with uv 0.11.6 on managed Python 3.13.12):** `tools/accept_connect.py` installed the brain from a wheel into a fresh tool folder, made a throwaway instance, and ran the installed `brain connect <harness> --yes`, then the harness's own MCP check, then `brain connect <harness> --remove --yes`, all with `HOME` pointing at an empty folder. Every config the harnesses wrote landed there; the real configs were hashed before and after and were byte-identical. `connect`'s own smoke test drives the exact configured command through `initialize`, `tools/list` and `brief`.

| Harness | Version | Method | `connect` | Smoke test | The harness's own check | "Call brief first" | MCP era |
|---|---|---|---|---|---|---|---|
| Claude Code | 2.1.294 | `claude mcp add --scope user` | written, removed | pass | `claude mcp list`: ✔ Connected | Server instructions (documented) | 2025-11-25 |
| Codex CLI | 0.154.0 | `codex mcp add` | written, removed | pass | `codex mcp list`: enabled (it lists, it doesn't connect) | Server instructions (documented) | 2025-11-25 |
| Gemini CLI | 0.29.5 | `gemini mcp add -s user`, args read back from `~/.gemini/settings.json` | written, removed | pass | `gemini mcp list`: ✓ Connected | Server instructions (documented) | 2025-11-25 |
| Grok Build | 1.0.51 | `grok mcp doctor` first, then `grok mcp add -s user` | written, removed | pass | `grok mcp doctor --json`: handshake OK, protocol 2025-11-25, 6 tools, healthy | Marked line in `~/.grok/AGENTS.md` when it exists | 2025-11-25 |
| Cursor (agent CLI) | 2026.08.11 | Strict-JSON merge of `~/.cursor/mcp.json` | written, removed | pass | `cursor-agent mcp list-tools`: 6 tools | Printed for the person's own rules (Cursor keeps them in its settings) | 2025-11-25 |
| Hermes Agent | (no `--version` output) | `hermes mcp add`, verified with `hermes mcp list`; printed YAML otherwise | printed | pass | `hermes mcp list`: the add didn't save | Marked line in `~/.hermes/AGENTS.md` when it exists | 2025-11-25 |

**Notes**
- **Gemini CLI** refuses `mcp add` until an auth method is chosen in `settings.json`. A signed-in user has one; the empty test home was given the same setting's shape (`security.auth.selectedType`), no credential.
- **Hermes**'s `mcp add` is discovery-first: in a home with no Hermes setup, and nobody to answer it, it stopped without saving and still exited 0. `connect` checks `hermes mcp list` after adding and prints the `mcp_servers` YAML when the entry isn't there. In a configured Hermes it is **Unverified** until run there.
- **Era:** all six speak the 2025 MCP era that `serve` serves. None needed `2026-07-28`, so dual-era support stays out of v1. A request naming another version gets JSON-RPC `-32600`.

**Not yet verified (needs the person's go, because it uses their own signed-in tools):**
- A `brief` call made by each harness's model in a fresh session, and whether that model saw "Call brief first".
- Each harness against its real, signed-in config.

**Other tools** get the generic snippet from `brain connect <name>`: `{"mcpServers": {"brain-<instance>": {"type": "stdio", "command": "<brain>", "args": ["serve", "--instance", "<dir>"]}}}`. VS Code adds an MCP server to the active profile only. Zed opens its settings with the `zed: open settings file` command.

**Change packets:** `base` on an upsert op became optional. A packet that sends `"base": null` on every op, as every earlier packet did, applies exactly as before, and a packet that leaves `base` out now creates a row that isn't there instead of being refused as invalid. Error texts keep their old opening words and gain the fix after them (`/limit is above the maximum of 100`), except that a raw `CHECK constraint failed` or `FOREIGN KEY constraint failed` is replaced by a plain-words rule.
