"""Slack message text as people read it: mentions as names, links as URLs, and which
messages are kept at all."""
from __future__ import annotations

import html
import re

from brain.text import single_line
from brain_fetch import net

# Plain messages, broadcasts of a thread reply and file shares are what people wrote. Every other subtype
# (joins, topic changes, bot and app posts, ...) is dropped.
KEPT_SUBTYPES = ("", "thread_broadcast", "file_share")
_TOKEN = re.compile(r"<([^<>]{1,1000})>")
_TS = re.compile(r"[0-9]{10}\.[0-9]{6}")


def kept(message) -> bool:
    if not isinstance(message, dict) or not isinstance(message.get("ts"), str) or not _TS.fullmatch(message["ts"]):
        return False
    if message.get("bot_id") or message.get("app_id") or message.get("bot_profile"):
        return False
    return str(message.get("subtype") or "") in KEPT_SUBTYPES and isinstance(message.get("user"), str)


class Names:
    """users.info, once per person per run. A user that can't be read is "(unknown)"."""

    def __init__(self, call):
        self.call = call
        self.cache: dict[str, str] = {}

    def __call__(self, user_id: str) -> str:
        if user_id not in self.cache:
            payload = self.call("users.info", user=user_id, soft=("user_not_found", "user_not_visible"))
            profile = net.field(payload, "user", "profile", default={}) or {}
            name = ""
            for key in ("display_name", "real_name"):
                if isinstance(profile.get(key), str) and profile[key].strip():
                    name = profile[key]
                    break
            if not name:
                name = str(net.field(payload, "user", "name", default="") or "(unknown)")
            self.cache[user_id] = single_line(name, max_chars=120) or "(unknown)"
        return self.cache[user_id]


def render(text: str, names: Names) -> str:
    def swap(match: re.Match) -> str:
        body = match.group(1)
        target, _, label = body.partition("|")
        if target.startswith("@"):
            return "@" + (label or names(target[1:]))
        if target.startswith("#"):
            return "#" + (label or target[1:])
        if target.startswith("!subteam^"):
            return label or "@team"
        if target.startswith("!"):
            return "@" + (label or target[1:].split("^", 1)[0])
        if target.startswith("mailto:"):
            return label or target[len("mailto:"):]
        return target

    return html.unescape(_TOKEN.sub(swap, text or ""))


def files(message: dict) -> list[str]:
    found = []
    for item in message.get("files") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            size = item.get("size") if isinstance(item.get("size"), int) else 0
            found.append(f"{single_line(item['name'], max_chars=150)} ({max(1, round(size / 1024))} KB)")
    return found[:20]
