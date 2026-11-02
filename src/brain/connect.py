"""brain connect: wire an agent tool to a brain, by the tool's own command where it has
one, a strict-JSON merge where it doesn't, and a printed snippet otherwise. After writing, the exact command is
driven through initialize, tools/list and brief, because a configured entry alone is not proof.

Every harness command is looked up on PATH and every file under Path.home(), so the tests replace both.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from brain import config as config_mod, ingress, store
from brain.text import NotOneLine, single_line

HARNESSES = ("claude", "codex", "gemini", "grok", "cursor", "hermes")
# Claude Code, Codex and Gemini CLI document that the model sees MCP server instructions,
# so only the others get the marked "Call brief first" line, and only in a file that already exists.
INSTRUCTIONS = {"grok": ".grok/AGENTS.md", "hermes": ".hermes/AGENTS.md"}
MARK = "<!-- agent-si:{name} -->"


class ConnectError(Exception):
    pass


@dataclass(frozen=True)
class Entry:
    name: str
    command: str
    args: tuple[str, ...]
    instance: Path

    def snippet(self) -> dict:
        return {"mcpServers": {self.name: {"type": "stdio", "command": self.command, "args": list(self.args)}}}


def entry(instance) -> Entry:
    from brain import update

    instance = Path(instance).resolve()
    cfg = config_mod.load_config(instance / "brain.toml")
    try:
        brain = update.tool_bin() / "brain"
    except update.Refused as exc:
        raise ConnectError(str(exc)) from None
    if not brain.is_file():
        raise ConnectError(f"brain isn't installed as a uv tool (no {brain}); run install.sh first")
    return Entry(f"brain-{cfg.name}", str(brain), ("serve", "--instance", str(instance)), instance)


def detected() -> list[str]:
    found = [name for name in HARNESSES if name != "cursor" and shutil.which(name)]
    if (Path.home() / ".cursor").is_dir():
        found.append("cursor")
    return [name for name in HARNESSES if name in found]


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}")


def _result(harness: str, method: str, status: str, message: str = "", text: str = "") -> dict:
    return {"harness": harness, "method": method, "status": status, "smoke": "skipped", "message": message,
            "text": text, "instructions": ""}


def _toml(item: Entry) -> str:
    return (f"[mcp_servers.{item.name}]\ncommand = {json.dumps(item.command)}\n"
            f"args = {json.dumps(list(item.args))}\n")


def _yaml(item: Entry) -> str:
    args = "".join(f"\n      - {json.dumps(arg)}" for arg in item.args)
    return f"mcp_servers:\n  {item.name}:\n    command: {json.dumps(item.command)}\n    args:{args}\n"


# -- the harnesses' own commands ------------------------------------------------------------------------------

def _cli(harness: str, item: Entry, remove: bool) -> dict:
    exe = shutil.which(harness)
    if exe is None:
        text = _toml(item) if harness == "codex" else _yaml(item) if harness == "hermes" else json.dumps(item.snippet(), indent=2)
        return _result(harness, "print", "printed", f"{harness} isn't on PATH; add this by hand", text)
    plans = {
        "claude": (["mcp", "add", "--transport", "stdio", "--scope", "user", item.name, "--", item.command, *item.args],
                   ["mcp", "remove", "--scope", "user", item.name], ["mcp", "get", item.name]),
        "codex": (["mcp", "add", item.name, "--", item.command, *item.args], ["mcp", "remove", item.name], ["mcp", "list"]),
        "gemini": (["mcp", "add", "-s", "user", item.name, item.command, "--", *item.args],
                   ["mcp", "remove", "-s", "user", item.name], None),
        "grok": (["mcp", "add", "-s", "user", item.name, item.command, "--", *item.args],
                 ["mcp", "remove", "-s", "user", item.name], ["mcp", "doctor", item.name, "--json"]),
        "hermes": (["mcp", "add", item.name, "--command", item.command, "--args", *item.args],
                   ["mcp", "remove", item.name], ["mcp", "list"]),
    }
    add, rm, check = plans[harness]
    present, same = _present(harness, exe, item, check)
    if remove:
        if not present:
            return _result(harness, "cli", "unchanged", "not configured")
        done = _run([exe, *rm])
        if done.returncode != 0 and harness == "grok":
            # Grok may see the server through Claude's or Cursor's config: removing it there removes it here.
            return _result(harness, "cli", "unchanged", "grok sees it through another tool's config")
        return _result(harness, "cli", "removed" if done.returncode == 0 else "failed",
                       "" if done.returncode == 0 else f"{harness} mcp remove failed")
    if same:
        # Grok also reads Claude's and Cursor's configs, so it may see the server without its own entry.
        return _result(harness, "cli", "unchanged", "grok already sees this server" if harness == "grok" else "")
    if present and harness != "grok":
        _run([exe, *rm])  # a different entry under our name: replace it, never stack a second
    by_hand = _toml(item) if harness == "codex" else _yaml(item) if harness == "hermes" else json.dumps(item.snippet(), indent=2)
    done = _run([exe, *add])
    if done.returncode != 0:
        return _result(harness, "cli", "failed", f"{harness} mcp add failed ({(done.stderr or done.stdout).strip()[:160]}); "
                       "add this by hand", by_hand)
    if harness == "gemini":
        return _gemini_read_back(item)
    if harness in ("codex", "claude", "hermes"):
        # A zero exit isn't proof: Hermes's discovery-first add can stop without saving when nobody can answer.
        listed = _run([exe, *check])
        if listed.returncode != 0 or item.name not in listed.stdout:
            where = "~/.hermes/config.yaml, then /reload-mcp" if harness == "hermes" else f"{harness}'s config"
            return _result(harness, "print", "printed", f"{harness} didn't save it; add this to {where}", by_hand)
    return _result(harness, "cli", "written")


def _load_config(path: Path):
    """Another tool's JSON settings file. OSError if it can't be read; IngressError if it isn't JSON we can trust."""
    return ingress.load_file(path, limit=ingress.MAX_DOCUMENT_BYTES, what=path.name)


def _present(harness: str, exe: str, item: Entry, check) -> tuple[bool, bool]:
    """(an entry under our name exists, and it's exactly ours), read the harness's own way."""
    if harness == "gemini":
        try:
            stored = _load_config(Path.home() / ".gemini" / "settings.json")["mcpServers"][item.name]
        except (OSError, ValueError, KeyError, TypeError):
            return False, False
        return True, isinstance(stored, dict) and stored.get("command") == item.command and stored.get("args") == list(item.args)
    if harness == "grok":
        seen = _run([exe, *check])
        same = seen.returncode == 0 and _grok_sees(seen.stdout, item)
        return same, same
    probe = [exe, "mcp", "get", item.name] if harness in ("claude", "codex") else [exe, *check]
    done = _run(probe)
    if done.returncode != 0 or item.name not in done.stdout and harness == "hermes":
        return False, False
    return True, item.command in done.stdout and item.args[-1] in done.stdout


def _grok_sees(text: str, item: Entry) -> bool:
    """grok mcp doctor --json lists servers by name with a target, "<command> <args>" (Measured, Grok Build
    1.0.51). Ours is there when the target runs our brain on our instance."""
    try:
        data = ingress.loads(text, what="grok mcp doctor")
    except ingress.IngressError:
        return False
    for server in data.get("servers") or [] if isinstance(data, dict) else []:
        target = server.get("target") if isinstance(server, dict) else None
        if server.get("name") == item.name and isinstance(target, str) and target.startswith(item.command + " ") \
                and target.endswith(" ".join(item.args)):
            return True
    return False


def _gemini_read_back(item: Entry) -> dict:
    """Gemini parses flags after the command as its own, so the stored args are read back and must be exact."""
    path = Path.home() / ".gemini" / "settings.json"
    try:
        stored = _load_config(path)["mcpServers"][item.name]
    except (OSError, ValueError, KeyError, TypeError):
        stored = None
    if isinstance(stored, dict) and stored.get("command") == item.command and stored.get("args") == list(item.args):
        return _result("gemini", "cli", "written")
    return _result("gemini", "cli", "failed", "gemini stored different args; put this in ~/.gemini/settings.json by hand",
                   json.dumps(item.snippet(), indent=2))


# -- Cursor: a strict-JSON merge -------------------------------------------------------------------------------

def _cursor(item: Entry, remove: bool, yes: bool, ask) -> dict:
    path = Path.home() / ".cursor" / "mcp.json"
    if path.is_symlink():
        return _result("cursor", "print", "printed", f"{path} is a link; add this by hand", json.dumps(item.snippet(), indent=2))
    try:
        raw = ingress.read_bytes(path, ingress.MAX_DOCUMENT_BYTES, path.name) if path.exists() else b""
        data = ingress.loads(raw, what=path.name) if raw.strip() else {}
    except (OSError, ingress.IngressError):
        return _result("cursor", "print", "printed", f"{path} isn't strict JSON, so brain connect won't touch it; add this by hand",
                       json.dumps(item.snippet(), indent=2))
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return _result("cursor", "print", "printed", f"{path} has no mcpServers table brain connect can merge into",
                       json.dumps(item.snippet(), indent=2))
    wanted = item.snippet()["mcpServers"][item.name]
    if remove:
        if item.name not in servers:
            return _result("cursor", "json", "unchanged")
        servers = {key: value for key, value in servers.items() if key != item.name}
    elif servers.get(item.name) == wanted:
        return _result("cursor", "json", "unchanged")
    else:
        servers = {**servers, item.name: wanted}
    after = {**data, "mcpServers": servers}
    change = f"{'remove' if remove else 'set'} mcpServers.{item.name} in {path}"
    if not yes and not ask(change):
        return _result("cursor", "json", "declined", change)
    if path.exists():
        backup = path.with_name(f"{path.name}.brain-backup-{store.utc_now().replace(':', '').replace('-', '')}")
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(after, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)
    if json.loads(path.read_text(encoding="utf-8")) != after:  # trusted: read back of the file this function just wrote
        return _result("cursor", "json", "failed", f"{path} didn't read back as written")
    return _result("cursor", "json", "removed" if remove else "written", change)


# -- the marked "Call brief first" line -------------------------------------------------------------------------

def _line(item: Entry) -> str:
    return f"Call the brain's brief tool first. {MARK.format(name=item.name)}"


def instructions(harness: str, item: Entry, remove: bool, yes: bool, ask) -> str:
    """Add or remove the one marked line. Only in a file that already exists and isn't a link; elsewhere the line
    is printed with where it goes. Removing leaves the file byte-identical to before the line was added."""
    if harness not in INSTRUCTIONS:
        if remove or harness in ("claude", "codex", "gemini"):
            return "" if remove else "not needed"
        return f"add this to the tool's own rules: {_line(item)}"
    path = Path.home() / INSTRUCTIONS[harness]
    mark = MARK.format(name=item.name)
    if path.is_symlink() or not path.is_file():
        return f"add this line to {path}: {_line(item)}"
    text = path.read_bytes().decode("utf-8", errors="surrogateescape")
    full = _line(item)
    if remove:
        # Exactly what adding wrote comes out: the line and its newline, or, after a file that didn't end in one,
        # the newline before it. So the file is byte-identical to before.
        if text.endswith("\n" + full):
            new = text[: -len("\n" + full)]
        elif full + "\n" in text:
            new = text.replace(full + "\n", "", 1)
        else:
            return "not present"
        path.write_bytes(new.encode("utf-8", errors="surrogateescape"))
        return "removed"
    if mark in text:
        return "present"
    if not yes and not ask(f"add one line to {path}: {full}"):
        return "declined"
    added = text + (full + "\n" if not text or text.endswith("\n") else "\n" + full)
    path.write_bytes(added.encode("utf-8", errors="surrogateescape"))
    return "added"


# -- the run ---------------------------------------------------------------------------------------------------

def connect(instance, harnesses: list[str], *, print_only=False, remove=False, yes=False, ask=None) -> list[dict]:
    from brain import update

    item = entry(instance)
    ask = ask or (lambda question: False)
    names = detected() if harnesses == ["all"] else harnesses
    results = []
    for harness in names:
        if harness not in HARNESSES:
            result = _result(harness, "print", "printed", "an unknown tool: this is the generic MCP entry",
                             json.dumps(item.snippet(), indent=2))
        elif print_only:
            text = _toml(item) if harness == "codex" else _yaml(item) if harness == "hermes" else json.dumps(item.snippet(), indent=2)
            result = _result(harness, "print", "printed", "", text)
        elif harness == "cursor":
            result = _cursor(item, remove, yes, ask)
        else:
            result = _cli(harness, item, remove)
        if not print_only and result["status"] not in ("failed", "declined"):
            result["instructions"] = instructions(harness, item, remove, yes, ask)
        if not remove and result["status"] in ("written", "unchanged", "printed"):
            result["smoke"] = "fail" if update._smoke(Path(item.command), item.instance) else "pass"
        if result["status"] == "written":
            result["message"] = (result["message"] + "; " if result["message"] else "") + f"restart {harness} to load it"
        results.append(result)
    return results


# -- a hub ----------------------------------------------------------------------

def remote_connect(harnesses: list[str], url: str, token_env: str, name: str, *, print_only=False, remove=False,
                   yes=False, ask=None) -> list[dict]:
    """Each harness's own form for an HTTPS hub, with the token from an environment variable, never pasted in."""
    from brain_hub import remote

    try:
        name = single_line(name, refuse=True, required=True)
    except NotOneLine as exc:  # the name is written into an instructions file, so it is one line
        raise ConnectError(f"--name {exc}") from None
    try:
        if remote.target(url) is None:
            raise ConnectError("--url must be the hub's https address, ending /mcp")
    except remote.RemoteError as exc:
        raise ConnectError(str(exc)) from None
    ask = ask or (lambda question: False)
    var = token_env
    header = f"Authorization: Bearer ${{{var}}}"
    plans = {
        "claude": ["mcp", "add", "--transport", "http", "--scope", "user", name, url, "--header", header],
        "codex": ["mcp", "add", name, "--url", url, "--bearer-token-env-var", var],
        "grok": ["mcp", "add", "-s", "user", name, url, "--transport", "http", "-H", header],
    }
    removes = {"claude": ["mcp", "remove", "--scope", "user", name], "codex": ["mcp", "remove", name],
               "grok": ["mcp", "remove", "-s", "user", name]}
    snippet = {"mcpServers": {name: {"type": "http", "url": url, "headers": {"Authorization": f"Bearer ${{env:{var}}}"}}}}
    results = []
    for harness in (detected() if harnesses == ["all"] else harnesses):
        exe = shutil.which(harness) if harness in plans else None
        if harness in ("gemini", "hermes") or (harness not in plans and harness != "cursor"):
            text = json.dumps({"mcpServers": {name: {"httpUrl": url, "headers": {"Authorization": "Bearer <paste your token>"}}}}, indent=2)
            results.append(_result(harness, "print", "printed", f"{harness} can't read the token from {var}: paste it into "
                                   "its own settings, never into a shared file", text))
            continue
        if harness == "cursor":
            path = Path.home() / ".cursor" / "mcp.json"
            wanted = snippet["mcpServers"][name]
            item = Entry(name, "", (), Path("."))
            results.append(_cursor_remote(path, item, wanted, remove, yes, ask) if not print_only else
                           _result("cursor", "print", "printed", "", json.dumps(snippet, indent=2)))
            continue
        if print_only or exe is None:
            results.append(_result(harness, "print", "printed", "" if exe else f"{harness} isn't on PATH",
                                   " ".join([harness, *plans[harness]])))
            continue
        done = _run([exe, *(removes[harness] if remove else plans[harness])])
        status = ("removed" if remove else "written") if done.returncode == 0 else "failed"
        results.append(_result(harness, "cli", status, "" if done.returncode == 0 else
                               f"{harness} mcp {'remove' if remove else 'add'} failed ({(done.stderr or done.stdout).strip()[:160]})"))
    token = os.environ.get(var, "")
    for result in results:
        if not remove and result["status"] in ("written", "unchanged", "printed"):
            result["smoke"] = remote.smoke(url, token) if token else "skipped"
    return results


def _cursor_remote(path: Path, item: Entry, wanted: dict, remove: bool, yes: bool, ask) -> dict:
    """The strict-JSON merge of _cursor, with the hub entry in place of the stdio one."""

    class _Remote(Entry):
        def snippet(self) -> dict:
            return {"mcpServers": {self.name: wanted}}

    return _cursor(_Remote(item.name, item.command, item.args, item.instance), remove, yes, ask)
