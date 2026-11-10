"""The guided brain init: five plain questions, read from the terminal itself, so it works
under `curl | sh`, where standard input is the script. Anything not answered keeps its default."""
from __future__ import annotations

import os
import re
from pathlib import Path

from brain import connect as connect_mod, registry
from brain import sessions as sessions_mod
from brain.config import load_config
from brain.init import init_instance

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


def local_zone() -> str:
    """The zone /etc/localtime points at, or UTC."""
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC"
    marker = "zoneinfo/"
    if marker not in target:
        return "UTC"
    # Ubuntu's tzdata, set up with nobody to answer, links to zoneinfo//UTC, so the slashes are tidied.
    return re.sub("/+", "/", target.split(marker, 1)[1]).strip("/") or "UTC"


def run(reader, writer, *, register: bool = True) -> dict:
    def ask(question: str, default: str) -> str:
        writer.write(f"{question} [{default}]: " if default else f"{question}: ")
        writer.flush()
        answer = reader.readline()
        return answer.strip() or default

    writer.write("Setting up a brain. Press Return to keep the suggestion in brackets.\n\n")
    name = ask("Brain name", "work")
    while not _NAME.fullmatch(name):
        writer.write("Use lowercase letters, digits and dashes, starting with a letter.\n")
        name = ask("Brain name", "work")
    folder = Path(os.path.expanduser(ask("Folder for your brains", "~/Brains")))
    principal = ask("Your name, so the brain knows which writes are yours", "")
    zone = ask("Time zone", local_zone())
    tools = connect_mod.detected()
    chosen: list[str] = []
    if tools:
        picked = ask(f"Connect these agent tools ({', '.join(tools)}): all, none, or a list", "all")
        chosen = tools if picked == "all" else [] if picked == "none" else [t.strip() for t in picked.split(",") if t.strip() in tools]
    folder.mkdir(parents=True, exist_ok=True)
    result = init_instance(name, folder, principal=principal or None, timezone=zone)
    result.pop("_notice", None)
    if register:
        registry.add(result["instance"])

    def confirm(question: str) -> bool:
        return ask(f"{question}? yes or no", "no").lower() in ("y", "yes")

    connected = connect_mod.connect(result["instance"], chosen, ask=confirm) if chosen else []
    for item in connected:
        writer.write(f"  {item['harness']}: {item['status']}, smoke test {item['smoke']}"
                     + (f" ({item['message']})" if item["message"] else "") + "\n")
        if item["text"]:
            writer.write(item["text"] + "\n")
    recorded = sessions_mod.summary(result["instance"], load_config(Path(result["instance"]) / "brain.toml"))
    writer.write(sessions_mod.summary_line(recorded) + "\n")
    writer.write("\nAsk your agent: read my brain brief.\n")
    writer.flush()
    return {**result, "connected": connected}
