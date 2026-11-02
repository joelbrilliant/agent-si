"""brain setup (1.13.0): the last step of the one-command installer, and a command on its own.

It makes a brain, registers it and connects every agent tool it finds, with one question at most (your name, with
the account's full name as the default). Like the guided `brain init` it schedules nothing, because nothing in agent-si looks for releases on its own
(agent-si isn't on a package index). It never installs or checks anything by name.

The question is read from the terminal itself, as the guided init does, so it works under `sh file.sh` with stdin
closed. With no terminal and no --yes it carries on with the defaults and says so; it never waits.
"""
from __future__ import annotations

import os
import pwd
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from brain import advice, connect as connect_mod, init as init_mod, registry
from brain.guided import local_zone
from brain.init import InitError, init_instance
from brain.schemas import PROJECT_ID
from brain.text import NotOneLine, single_line

DEFAULT_NAME = "work"
DEFAULT_DIR = "~/Brains"
_NAME = re.compile(PROJECT_ID)
_TERMINAL = object()  # "open /dev/tty if a question has to be asked"


@dataclass(frozen=True)
class Options:
    yes: bool = False
    principal: str | None = None  # None: not given. "": given, and none wanted.
    name: str = DEFAULT_NAME
    dir: str = DEFAULT_DIR
    timezone: str | None = None
    no_connect: bool = False


def _valid_principal(name: str) -> bool:
    try:
        init_mod._principal_slug(name)  # the check init itself makes
    except InitError:
        return False
    return True


def default_principal() -> str | None:
    """The account's full name: `id -F` on macOS, the passwd comment field elsewhere. None when there isn't a usable one."""
    full = ""
    if sys.platform == "darwin":
        try:
            done = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=10, check=False,
                                  stdin=subprocess.DEVNULL)
            full = done.stdout if done.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            full = ""
    else:
        try:
            full = pwd.getpwuid(os.getuid()).pw_gecos.split(",", 1)[0]
        except (KeyError, OSError):
            full = ""
    full = single_line(full)
    return full if full and _valid_principal(full) else None


def _terminal():
    try:
        return open("/dev/tty", encoding="utf-8"), open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        return None


def _ask_name(default: str | None, reader, writer) -> str | None:
    """The one question. Return keeps the default; a name that can't make a person id is asked for again."""
    for _ in range(3):
        writer.write(f"Your name [{default}]: " if default else "Your name (Return for none): ")
        writer.flush()
        answer = reader.readline()
        if answer == "":  # the terminal closed
            return default
        try:
            answer = single_line(answer.strip(), refuse=True)
        except NotOneLine:
            answer = None
        if answer == "":
            return default
        if answer is not None and _valid_principal(answer):
            return answer
        writer.write("Use your full name written with letters, such as Alex Example.\n")
    raise InitError(advice.setup_principal_error(answer or ""), 2)


def _check_zone(zone: str) -> str:
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InitError(advice.setup_timezone_error(zone), 2) from None
    return zone


def run(options: Options, *, out=None, terminal=_TERMINAL) -> dict:
    """Make the brain, register it and connect the tools found. `terminal` is a (reader, writer) pair, None for no
    terminal, or left alone to open /dev/tty only when the question has to be asked."""
    out = out or sys.stdout
    if not _NAME.fullmatch(options.name):
        raise InitError(advice.setup_name_error(options.name), 2)
    notes: list[str] = []
    if options.timezone:
        zone = _check_zone(options.timezone)
    else:
        detected = local_zone()
        try:
            zone = _check_zone(detected)
        except InitError:
            zone = _check_zone("UTC")  # with no zone data at all, this refusal says to install it
            notes.append(f"This machine's time zone, {detected}, has no zone data here, so the brain uses UTC. "
                         "Change timezone in brain.toml, then run brain apply-config.")
    parent = Path(os.path.expanduser(options.dir))
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise InitError(advice.setup_folder_error(options.dir), 2) from None
    exists = (parent / options.name / "brain.toml").is_file()

    principal: str | None = None
    if not exists:
        if options.principal is not None:
            principal = options.principal or None
            if principal is not None:
                try:
                    principal = single_line(principal, refuse=True)
                except NotOneLine:
                    raise InitError(advice.setup_principal_error(options.principal), 2) from None
                if not _valid_principal(principal):
                    raise InitError(advice.setup_principal_error(options.principal), 2)
        else:
            principal = default_principal()
            if not options.yes:
                pair = _terminal() if terminal is _TERMINAL else terminal
                if pair is None:
                    notes.append("There's no terminal to ask in, so I used the defaults. "
                                 'Pass --principal "Your Name" to set your name.')
                else:
                    reader, writer = pair
                    try:
                        principal = _ask_name(principal, reader, writer)
                    finally:
                        if terminal is _TERMINAL:
                            reader.close()
                            writer.close()

    result = init_instance(options.name, parent, principal=principal, timezone=zone)
    result.pop("_notice", None)
    kept = result["status"] == "already initialised"
    instance = result["instance"]
    registry.add(instance)

    out.write(f"Brain {options.name}: {'kept the existing one' if kept else 'created'} at {instance}\n")
    if not kept:
        out.write(f"  Your name: {principal}\n" if principal else
                  "  No name given, so the evidence gate's rule about your own words is off. Add one later with brain apply-config.\n")
    for note in notes:
        out.write(f"  {note}\n")

    connected: list[dict] = []
    if options.no_connect:
        out.write(f"Tools: not connected (--no-connect). Later: brain connect all --instance {shlex.quote(instance)}\n")
    else:
        tools = connect_mod.detected()
        if not tools:
            out.write("Tools: none found on this machine. Install Claude Code, Codex or another agent tool, then run: "
                      f"brain connect all --instance {shlex.quote(instance)}\n")
        else:
            out.write(f"Connecting {', '.join(tools)}:\n")
            connected = connect_mod.connect(instance, tools, yes=True)
            brain = connect_mod.entry(instance).command
            for item in connected:
                _report(item, brain, instance, out)
    ok = all(item["status"] != "failed" and item["smoke"] != "fail" for item in connected)
    out.flush()
    return {"instance": instance, "created": not kept, "connected": connected, "ok": ok}


def _report(item: dict, brain: str, instance: str, out) -> None:
    line = f"  {item['harness']}: {item['status']}, smoke test {item['smoke']}"
    out.write(line + (f" ({item['message']})" if item["message"] else "") + "\n")
    if item["text"]:
        out.write("".join(f"    {row}\n" for row in item["text"].splitlines()))
    if item["status"] in ("written", "unchanged"):
        if item["instructions"] == "added":
            out.write(f"    added one marked line to {item['harness']}'s instructions file\n")
        out.write(f"    undo: {shlex.quote(brain)} connect {item['harness']} --remove --yes --instance {shlex.quote(instance)}\n")
