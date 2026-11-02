"""Release checks, run on an exported tree before anything is made public, and as CI steps on every
future release: a denylist scan and a secret scan. Both print only where a hit is, never the text that hit.

    python tools/release_check.py TREE --denylist FILE

The denylist (names, emails, home paths, company names: one per line, # for comments) is private and never
committed. In CI it comes from a repository secret written to a file. A line `allow: PHRASE` names text that
is fine to publish (the author's credited name, the public repository addresses): the phrase is removed from
a path or a line before the other entries are matched against it.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SECRETS = {
    "aws access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "google key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "google oauth token": re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}\b"),
    "anthropic or openai key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_\-]{30,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "pypi token": re.compile(r"\bpypi-AgE[A-Za-z0-9_\-]{50,}\b"),
}
# The tests and the scanner itself spell these shapes on purpose, as refused inputs; a line marked
# "release-check: allow" is skipped, and nothing else is.
ALLOW = "release-check: allow"
# Binary files are searched as raw bytes: an image or PDF keeps its metadata (author, comments, paths) as
# plain text, so a byte search finds it. Archives and databases are not searched, so they are listed instead.
BYTE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".webp", ".svgz"}
SKIP_SUFFIXES = {".whl", ".gz", ".zip", ".sqlite3", ".tar", ".bz2", ".xz"}
ALLOW_PREFIX = "allow:"


class Denylist(list):
    """The entries to refuse (lowercase), with the phrases to allow in `.allowed`."""

    allowed: tuple[str, ...] = ()


def denylist(path: Path) -> Denylist:
    words, allowed = Denylist(), []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(ALLOW_PREFIX):
            phrase = line[len(ALLOW_PREFIX):].strip().lower()
            if phrase:
                allowed.append(phrase)
        else:
            words.append(line.lower())
    # Longest first, so an allowed address is removed whole before a shorter allowed name inside it.
    words.allowed = tuple(sorted(allowed, key=len, reverse=True))
    return words


def _without_allowed(text: str, allowed: tuple[str, ...]) -> str:
    for phrase in allowed:
        text = text.replace(phrase, " ")
    return text


def binary_files(tree: Path) -> list[str]:
    """Every file that is not readable text, for a person to look at when the scan cannot read it."""
    listed = []
    for path in sorted(item for item in tree.rglob("*") if item.is_file() and ".git" not in item.parts):
        if path.suffix.lower() in BYTE_SUFFIXES | SKIP_SUFFIXES:
            listed.append(path.relative_to(tree).as_posix())
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            listed.append(path.relative_to(tree).as_posix())
    return listed


def scan(tree: Path, words: list[str], skip: tuple[str, ...] = ()) -> list[str]:
    import fnmatch

    allowed = getattr(words, "allowed", ())
    found = []
    for path in sorted(item for item in tree.rglob("*") if item.is_file() and ".git" not in item.parts):
        rel = path.relative_to(tree).as_posix()
        if any(fnmatch.fnmatch(rel, pattern) for pattern in skip):
            continue
        clean_path = _without_allowed(rel.lower(), allowed)
        for word in words:
            if word in clean_path:
                found.append(f"{rel}: denylisted name in the path")
        suffix = path.suffix.lower()
        if suffix in SKIP_SUFFIXES:
            continue
        if suffix in BYTE_SUFFIXES:
            try:
                data = path.read_bytes().lower()
            except OSError:
                continue
            for allowed_phrase in allowed:
                data = data.replace(allowed_phrase.encode("utf-8"), b" ")
            for index, word in enumerate(words):
                if word.encode("utf-8") in data:
                    found.append(f"{rel}: denylist entry {index + 1} in the file's bytes")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if ALLOW in line:
                continue
            lower = _without_allowed(line.lower(), allowed)
            for index, word in enumerate(words):
                if word in lower:
                    found.append(f"{rel}:{number}: denylist entry {index + 1}")
            for kind, pattern in SECRETS.items():
                if pattern.search(line):
                    found.append(f"{rel}:{number}: looks like a {kind}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tree")
    parser.add_argument("--denylist", required=True)
    parser.add_argument("--skip", action="append", default=[], help="a glob of files the release leaves out")
    ns = parser.parse_args(argv)
    found = scan(Path(ns.tree), denylist(Path(ns.denylist)), tuple(ns.skip))
    for line in found:
        print(line)
    binaries = binary_files(Path(ns.tree))
    if binaries:
        print(f"release check: {len(binaries)} binary or unreadable files to look at by hand: " + ", ".join(binaries))
    print(f"release check: {len(found)} problem{'s' if len(found) != 1 else ''}")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
