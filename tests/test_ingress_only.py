"""Untrusted input is refused, never allowed to crash or hang an entry point.

One module, brain.ingress, owns reading input from outside: JSON with limits on size, nesting and integers, and bounded
reads of files and streams. This test fails on a plain `json.load` or `json.loads` anywhere else in the engine, so a
new entry point that parses with plain json can't be added without the same limits.

JSON the engine wrote itself (a database column, its own marker file) may keep plain `json.loads`. End that line with
`# trusted: <what wrote it>`.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src/brain", "src/brain_hub", "src/brain_fetch")
OWNER = "src/brain/ingress.py"
TRUSTED = re.compile(r"#\s*trusted:\s*\S")
PARSERS = {"load", "loads", "JSONDecoder"}


def _json_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """(names that stand for the json module, names imported from it that parse)."""
    modules, parsers = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update((alias.asname or alias.name) for alias in node.names if alias.name == "json")
        elif isinstance(node, ast.ImportFrom) and node.module == "json":
            parsers.update((alias.asname or alias.name) for alias in node.names if alias.name in PARSERS)
    return modules, parsers


def scan(name: str, source: str) -> list[str]:
    """One message per plain JSON parse in `source` that isn't marked trusted, each naming `name` and the line."""
    tree = ast.parse(source)
    lines = source.splitlines()
    modules, parsers = _json_names(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            hit = node.value.id in modules and node.attr in PARSERS
            what = f"{node.value.id}.{node.attr}"
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            hit = node.id in parsers
            what = node.id
        else:
            continue
        if hit and not TRUSTED.search(lines[node.lineno - 1]):
            found.append(
                f"{name}:{node.lineno}: {what} reads JSON without the limits on size, nesting and integers. "
                f"For input from outside, use brain.ingress.loads(raw, what=...) for bytes or text, "
                f"brain.ingress.load_file(path, limit=...) for a file, and brain.ingress.read_bytes(source, limit) "
                f"for a file or stream. If the engine wrote this JSON itself (a database column, its own marker "
                f"file), end the line with `# trusted: <what wrote it>`."
            )
    return found


def scan_tree(root: Path) -> list[str]:
    found = []
    for folder in SCANNED:
        for path in sorted((root / folder).rglob("*.py")):
            name = str(path.relative_to(root))
            if name != OWNER and "__pycache__" not in path.parts:
                found += scan(name, path.read_text(encoding="utf-8"))
    return found


def test_only_brain_ingress_parses_outside_json() -> None:
    found = scan_tree(ROOT)
    assert not found, "\n" + "\n".join(found)


def test_the_lint_flags_plain_parses_and_accepts_the_two_good_forms() -> None:
    bad = (
        "import json\n"
        "import json as j\n"
        "from json import loads as parse\n"
        "a = json.loads(raw)\n"
        "b = j.load(handle)\n"
        "c = parse(raw)\n"
        "d = list(map(json.loads, lines))\n"
        "e = json.JSONDecoder().raw_decode(raw)\n"
    )
    messages = scan("src/brain/new_entry.py", bad)
    assert [m.split(":")[1] for m in messages] == ["4", "5", "6", "7", "8"], messages
    assert all(m.startswith("src/brain/new_entry.py:") and "brain.ingress.loads" in m for m in messages)
    good = (
        "import json\n"
        "from brain import ingress\n"
        "a = ingress.loads(raw, what='packet')\n"
        "b = json.loads(column)  # trusted: write_log.packet_json, stored by the applier\n"
        "c = json.dumps(value)\n"
    )
    assert scan("src/brain/new_entry.py", good) == []
    assert scan("src/brain/new_entry.py", "import json\na = json.loads(x)  # trusted:\n") != []


def test_no_test_reads_an_endless_device() -> None:
    """A test that feeds /dev/zero (or another endless device) to the engine reads until the machine runs out of memory
    if the cap ever breaks, and a SIGKILL can't land mid-read. Use a finite oversized
    file, or tests/test_ingress_behaviour.py's _EndlessStream for the never-ends case."""
    pattern = re.compile(r"""["']/dev/(zero|urandom|random)["']""")
    found = [
        f"{path.relative_to(ROOT)}:{number}: reads an endless device. Use a finite oversized file under a timeout, "
        "or test_ingress_behaviour._EndlessStream."
        for path in sorted((ROOT / "tests").rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line) and path.name != Path(__file__).name
    ]
    assert not found, "\n" + "\n".join(found)
