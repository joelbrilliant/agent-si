"""No test or tool names the current release or the latest schema by hand.

A release bump changes `brain.__version__` and may add a migration. Every literal copy of those numbers in
tests/ or tools/ is a line that bump has to find and rewrite, and a missed one fails far from the cause. Read
them from `brain.__version__` and `brain.migrate.latest_version()` instead. Older numbers are fine: a test of
an old bundle or an old schema names that version because it is the thing under test.

A line that must name the current number carries a comment on the same line: `# pinned: <reason>`.
"""
from __future__ import annotations

import re
from pathlib import Path

from brain import __version__, migrate

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("tests", "tools")
EXEMPT = re.compile(r"#\s*pinned:\s*\S")


def _patterns(version: str, schema: int) -> list[tuple[str, re.Pattern]]:
    n = str(schema)
    return [
        (f"the version {version}", re.compile(rf"(?<![\w.]){re.escape(version)}(?!\w|\.\d)")),
        (f"schema {n}", re.compile(rf"schema\w*[\"']?\W{{1,5}}{n}\b")),
        (f"a migrate result ending at {n}", re.compile(rf"==\s*\(\s*\d+\s*,\s*{n}\s*\)")),
        (f"schema {n} in a message or result", re.compile(rf"\bexpects\s+{n}\b|[\"']to[\"']\s*:\s*{n}\b")),
        (f"user_version {n}", re.compile(rf"\[\(\s*{n}\s*,\s*\)\]|user_version\b[^\n]*\b{n}\b")),
    ]


def scan(name: str, text: str, version: str, schema: int) -> list[str]:
    """One message per line of `text` that pins `version` or schema `schema`, each naming `name` and the line."""
    found = []
    patterns = _patterns(version, schema)
    for number, line in enumerate(text.splitlines(), start=1):
        if EXEMPT.search(line):
            continue
        for what, pattern in patterns:
            if pattern.search(line):
                found.append(
                    f"{name}:{number}: pins {what} by hand: {line.strip()!r}. Use `brain.__version__` or "
                    f"`brain.migrate.latest_version()` instead, or end the line with `# pinned: <reason>`."
                )
                break
    return found


def scan_tree(root: Path, version: str, schema: int) -> list[str]:
    found = []
    for folder in SCANNED:
        for path in sorted((root / folder).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            found += scan(str(path.relative_to(root)), path.read_text(encoding="utf-8"), version, schema)
    return found


def test_no_test_or_tool_pins_the_current_version_or_schema() -> None:
    found = scan_tree(ROOT, __version__, migrate.latest_version())
    assert not found, "\n" + "\n".join(found)


# The scan itself, on invented numbers so these lines never name the real release.
V, S = "8.8.8", 88


def test_scan_flags_each_shape_of_pin_and_names_the_fix() -> None:
    lines = [
        'assert out == "agent-si 8.8.8 (schema 88)"',
        'assert report["engine"] == "8.8.8"',
        'assert "schema": 88',
        'assert migrate.migrate(db) == (4, 88)',
        'assert ro_rows(inst, "PRAGMA user_version") == [(88,)]',
        'assert con.execute("PRAGMA user_version").fetchone()[0] == 88',
        'assert lines[0] == "demo brain, schema 88, today 2026-10-08 (UTC)."',
        'assert json.loads(out)["schema_version"] == 88',
        'assert message == "store is at version 1, engine expects 88; run brain migrate"',
        'assert json.loads(out) == {"from": 2, "to": 88, "search_rebuild": None}',
    ]
    for line in lines:
        found = scan("tests/test_x.py", f"import x\n{line}\n", V, S)
        assert len(found) == 1, line
        assert found[0].startswith("tests/test_x.py:2: pins "), found[0]
        assert "`brain.__version__` or `brain.migrate.latest_version()`" in found[0]


def test_scan_leaves_old_numbers_other_versions_and_marked_lines_alone() -> None:
    clean = [
        'assert out == "agent-si 8.8.7 (schema 87)"',
        'assert migrate.migrate(db) == (4, migrate.latest_version())',
        'assert report["engine"] == __version__',
        'bundle = {"engine": "0.7.0", "schema": 4}',
        'version = "18.8.8"',
        'version = "8.8.80"',
        'assert cut(text, 88) == "ab..."',
        'data = bytearray(rng.randbytes(rng.randint(1, 88)))',
        'for count in (3, 88):',
        'assert "store is at version 1, engine expects 87" in problems',
        'assert out == "agent-si 8.8.8"  # pinned: the installer ships this release',
        'assert (4, 88) == pair  # pinned:   a fixture frozen at this schema',
    ]
    for line in clean:
        assert scan("tests/test_x.py", line + "\n", V, S) == [], line
    assert len(scan("tests/test_x.py", 'x = "8.8.8"  # pinned:\n', V, S)) == 1, "a mark with no reason does not count"


# Router sizes: one commit changed one line of ROUTER.md and had to rewrite the same four
# byte counts in six test files. Those sizes now live in bench/budgets.json a2, read through
# tests.conftest.router_bytes(), so a router change edits that one file with a changelog entry.
ROUTER_PIN = re.compile(r"(render_router\(|ROUTER\.md|router_bytes\b|\brouter\b|\brendered\b|\btemplate\b|\(on, off\))[^\n]*==\s*\(?\s*\d[\d_]{2,}")


def scan_router_pins(name: str, text: str) -> list[str]:
    """One message per line of `text` that pins a ROUTER.md byte count, naming `name`, the line and the fix."""
    return [
        f"{name}:{number}: pins a ROUTER.md byte count by hand: {line.strip()!r}. Use "
        f"`tests.conftest.router_bytes(name)` (sizes live in bench/budgets.json a2), or end the line with "
        f"`# pinned: <reason>`."
        for number, line in enumerate(text.splitlines(), start=1)
        if ROUTER_PIN.search(line) and not EXEMPT.search(line)
    ]


def test_no_test_pins_a_router_byte_count() -> None:
    found = []
    for path in sorted((ROOT / "tests").rglob("*.py")):
        if "__pycache__" not in path.parts and path.name != Path(__file__).name:
            found += scan_router_pins(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
    assert not found, "\n" + "\n".join(found)


def test_router_scan_flags_the_pins_edffe92_rewrote_and_spares_the_helper() -> None:
    pinned = [
        '    assert len(render_router("demo").encode("utf-8")) == 2006',
        '    assert len((tmp_path / "demo" / "ROUTER.md").read_bytes()) == 2006',
        '    assert len(router) == 2006',
        '    assert len(rendered) == 2042',
        '    assert len(template) == 1935',
        '        assert (on, off) == (2002 + length, 1998 + length), name',
        '    assert len(render_router("demo").encode()) == 2_006',
    ]
    for line in pinned:
        found = scan_router_pins("tests/test_x.py", line + "\n")
        assert len(found) == 1 and "router_bytes(name)" in found[0], line
    clean = [
        '    assert len(router) == router_bytes("demo")',
        '    assert len(rendered) <= 2048',
        '    assert payload["router_bytes"] == len(router)',
        '    assert len(router) == 2006  # pinned: a frozen fixture of the 1.5.0 router',
    ]
    for line in clean:
        assert scan_router_pins("tests/test_x.py", line + "\n") == [], line
