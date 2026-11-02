"""The folder walk: which files are new, changed or unchanged, which
can't be read, and which are skipped. Nothing here writes."""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from brain_fetch import fileio, readers

MAX_DEPTH = 20
MAX_FILES = 20000
MAX_REF = 500
MAX_BYTES = fileio.MAX_BYTES
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DATED = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?![0-9])")
FILE_TYPES = (
    (".transcript.txt", "meeting_transcript", "ttext"),
    (".transcript.json", "meeting_transcript", "tjson"),
    (".markdown", "document", "text"),
    (".md", "document", "text"),
    (".txt", "document", "text"),
    (".docx", "document", "docx"),
    (".pdf", "document", "pdf"),
    (".vtt", "meeting_transcript", "vtt"),
    (".srt", "meeting_transcript", "srt"),
)


class TooMany(Exception):
    pass  # more than MAX_FILES readable files


@dataclass
class Result:
    items: list = field(default_factory=list)
    entries: dict = field(default_factory=dict)  # path -> cursor entry, for every file read or kept
    present: list = field(default_factory=list)  # refs of unchanged files, and of unreadable ones with a version
    unreadable: list = field(default_factory=list)
    changed: int = 0
    unchanged: int = 0
    skipped: int = 0
    unsupported: int = 0
    io_failure: bool = False
    io_failed: list = field(default_factory=list)  # paths that failed with an I/O error, for check
    chars: int = 0
    no_text: int = 0
    by_type: dict = field(default_factory=dict)


def classify(name: str):
    lowered = name.lower()
    for suffix, kind, tag in FILE_TYPES:
        if lowered.endswith(suffix):
            return suffix, kind, tag
    return None


def _modified(info, zone: str) -> str:
    stamp = datetime.fromtimestamp(info.st_mtime_ns / 1e9, timezone.utc)
    return stamp.astimezone(ZoneInfo(zone)).date().isoformat()


def scan(root: str, origin: str, cursor: dict, kinds, zone: str, transcripts_on: bool) -> Result:
    result = Result()
    seen: set[str] = set()
    counted = 0
    stack = [(root, "", 0)]
    while stack:
        directory, prefix, depth = stack.pop(0)
        try:
            with os.scandir(directory) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            result.io_failure = True
            result.io_failed.append(prefix.rstrip("/") or ".")
            continue
        subdirs = []
        for entry in entries:
            name = entry.name
            try:
                name.encode("utf-8")
            except UnicodeEncodeError:
                result.skipped += 1
                continue
            if name.startswith((".", "~$")) or _CONTROL.search(name) or entry.is_symlink():
                result.skipped += 1
                continue
            relative = unicodedata.normalize("NFC", prefix + name)
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 > MAX_DEPTH:
                        result.skipped += 1
                    else:
                        subdirs.append((entry.path, relative + "/", depth + 1))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    result.skipped += 1
                    continue
                kind_info = classify(name)
                if kind_info is None:
                    result.skipped += 1
                    result.unsupported += 1
                    continue
                suffix, kind, tag = kind_info
                if kind == "meeting_transcript" and not (transcripts_on and kind in kinds):
                    result.skipped += 1
                    continue
                info = entry.stat(follow_symlinks=False)
            except OSError:
                result.io_failure = True
                result.io_failed.append(unicodedata.normalize("NFC", prefix + name))
                continue
            if info.st_size > MAX_BYTES or relative in seen:
                result.skipped += 1
                continue
            seen.add(relative)
            counted += 1
            if counted > MAX_FILES:
                raise TooMany()
            ref = f"{kind}:{origin}/{relative}"
            if len(ref) > MAX_REF:  # the export's ref limit: this one file is unreadable, it never blocks the rest
                _failed(result, relative, ref, info, "", False)
                continue
            _file(result, entry.path, relative, ref, suffix, kind, tag, info, cursor, zone)
        stack[0:0] = subdirs
    result.items.sort(key=lambda item: item["ref"])
    return result


def _file(result, path, relative, ref, suffix, kind, tag, info, cursor, zone) -> None:
    old = cursor.get(relative) if isinstance(cursor.get(relative), dict) else None
    stats = (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if old is not None and (old.get("size"), old.get("mtime_ns"), old.get("ctime_ns")) == stats:
        if old.get("reader") == "failed":
            _failed(result, relative, ref, info, old.get("sha256", ""), bool(old.get("stored")))
            return
        if not (old.get("reader") == "pdf-none" and tag == "pdf" and readers.pypdf_available()):
            result.entries[relative] = old
            result.present.append(ref)
            result.unchanged += 1
            return
    try:
        text, new_tag, sha, first = fileio.read_file(path, tag)
    except OSError:
        result.io_failure = True
        result.io_failed.append(relative)
        return
    if isinstance(text, readers.ReadError):
        stored = old is not None and (old.get("reader") != "failed" or bool(old.get("stored")))
        _failed(result, relative, ref, first, sha, stored)
        return
    entry = {
        "sha256": sha, "size": first.st_size, "mtime_ns": first.st_mtime_ns,
        "ctime_ns": first.st_ctime_ns, "reader": new_tag,
    }
    # The same bytes under another reader tag need --rescan, except pdf-none to pdf (pypdf has arrived).
    same = old is not None and old.get("sha256") == sha and old.get("reader") != "failed"
    if same and not (old.get("reader") == "pdf-none" and new_tag == "pdf"):
        entry["reader"] = old.get("reader", new_tag)
        result.entries[relative] = entry  # touched or re-saved: the entry moves, no new version
        result.present.append(ref)
        result.unchanged += 1
        return
    result.entries[relative] = entry
    result.changed += 1
    result.chars += len(text)
    result.no_text += text.startswith("(no text layer:")
    result.by_type[suffix] = result.by_type.get(suffix, 0) + 1
    result.items.append(
        {"ref": ref, "kind": kind, "title": relative if len(relative) <= 200 else relative[:197] + "...", "date": _date(kind, relative, info, zone),
         "text": text}
    )


def _failed(result, relative, ref, info, sha, stored) -> None:
    result.unreadable.append(relative)
    result.entries[relative] = {
        "sha256": sha, "size": info.st_size, "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns,
        "reader": "failed", "stored": stored,
    }
    if stored:
        result.present.append(ref)


def _date(kind: str, relative: str, info, zone: str) -> str:
    if kind == "meeting_transcript":
        found = _DATED.match(relative.rsplit("/", 1)[-1])
        if found:
            try:
                datetime.strptime(found.group(1), "%Y-%m-%d")
                return found.group(1)
            except ValueError:
                pass
    return _modified(info, zone)
