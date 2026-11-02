"""A Gmail message's text, without its quoted history or signature.

The body is text/plain when there is one, else text/html read through html.parser. Attachment contents are never
read: their names and sizes go in the header.
"""
from __future__ import annotations

import base64
import email.utils
import html.parser
import re

from brain.text import single_line

_QUOTE_START = (
    re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE),
    re.compile(r"^_{10,}$"),
    re.compile(r"^begin forwarded message:?$", re.IGNORECASE),
)
_ON_WROTE = re.compile(r"^on\b.{0,300}\bwrote:$", re.IGNORECASE)
_OUTLOOK_FROM = re.compile(r"^\*?from:\*?\s", re.IGNORECASE)
_OUTLOOK_NEXT = re.compile(r"^\*?(sent|date|to|subject):\*?\s", re.IGNORECASE)
_SIGNATURE = (
    re.compile(r"^--\s?$"),
    re.compile(r"^sent from my \w+", re.IGNORECASE),
    re.compile(r"^get outlook for \w+", re.IGNORECASE),
)
_BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "hr"}
_DROP_CLASSES = ("gmail_quote", "gmail_signature", "gmail_extra", "moz-cite-prefix", "yahoo_quoted")


class _Text(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0  # depth inside a dropped element
        self.stack: list[tuple[str, bool]] = []

    def handle_starttag(self, tag, attrs):
        classes = " ".join(value or "" for name, value in attrs if name == "class")
        dropped = tag in ("script", "style", "head", "blockquote") or any(name in classes for name in _DROP_CLASSES)
        if tag in ("br", "hr", "img", "meta", "link", "input"):
            if not self.skip and tag in ("br", "hr"):
                self.parts.append("\n")
            return
        self.stack.append((tag, dropped))
        self.skip += dropped
        if not self.skip and tag in _BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        # Mail HTML leaves tags open: close back to the matching tag, and ignore an end tag that never opened.
        if tag in ("br", "hr", "img", "meta", "link", "input") or all(name != tag for name, _ in self.stack):
            return
        while self.stack:
            name, dropped = self.stack.pop()
            self.skip -= dropped
            if name == tag:
                break
        if not self.skip and tag in _BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _Text()
    parser.feed(markup)
    parser.close()
    lines = [" ".join(line.split()) for line in "".join(parser.parts).split("\n")]  # not one-line: a body of many lines
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _header(part: dict, name: str) -> str:
    for item in part.get("headers") or ():
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name:
            return str(item.get("value", ""))
    return ""


def _decode(part: dict) -> str:
    data = (part.get("body") or {}).get("data") or ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (ValueError, TypeError):
        return ""
    match = re.search(r"charset=\"?([\w.\-]+)", _header(part, "content-type"), re.IGNORECASE)
    try:
        return raw.decode(match.group(1) if match else "utf-8", errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _walk(part: dict, out: list) -> None:
    if not isinstance(part, dict):
        return
    out.append(part)
    for child in part.get("parts") or ():
        _walk(child, out)


def _size(size) -> str:
    size = size if isinstance(size, int) and size >= 0 else 0
    return f"{max(1, round(size / 1024))} KB" if size < 1024 * 1024 else f"{size / (1024 * 1024):.1f} MB"


def body(payload: dict) -> tuple[str, list[str]]:
    """(text, attachments). Quoted history and the signature are already dropped from the text."""
    parts: list[dict] = []
    _walk(payload, parts)
    attachments, plain, markup = [], None, None
    for part in parts:
        name = single_line(part.get("filename"), max_chars=150)
        if name:
            attachments.append(f"{name} ({_size((part.get('body') or {}).get('size'))})")
            continue
        kind = str(part.get("mimeType", "")).lower()
        if kind == "text/plain" and plain is None:
            plain = _decode(part)
        elif kind == "text/html" and markup is None:
            markup = _decode(part)
    text = plain if plain is not None else html_to_text(markup) if markup is not None else ""
    return strip_quoted(text), attachments[:20]


def _wrote(lines: list[str]) -> bool:
    """An "On <date>, <name> wrote:" line, which mail clients wrap onto up to three lines."""
    joined = ""
    for line in lines:
        joined = f"{joined} {line.strip()}".strip()
        if line.strip().endswith("wrote:"):
            return _ON_WROTE.match(joined) is not None
    return False


def strip_quoted(text: str) -> str:
    """Cut at the first quoted-history marker or signature, and drop any > line."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")  # not one-line: a body of many lines
    kept: list[str] = []
    for index, line in enumerate(lines):
        bare = line.strip()
        if any(pattern.match(bare) for pattern in _QUOTE_START + _SIGNATURE):
            break
        if bare.lower().startswith("on ") and _wrote(lines[index:index + 3]):
            break
        if _OUTLOOK_FROM.match(bare) and any(_OUTLOOK_NEXT.match(item.strip()) for item in lines[index + 1:index + 4]):
            break
        if bare.startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip("\n")


def names(value: str, limit: int = 20) -> list[str]:
    """Display names only: an address is never kept."""
    found = []
    for name, _address in email.utils.getaddresses([value or ""]):
        name = single_line(name.replace('"', ""), max_chars=120)
        if name or _address:
            found.append(name or "(no name)")
    return found[:limit]
