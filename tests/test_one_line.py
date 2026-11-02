"""Free text shown on one line is cleaned by one helper.

str.splitlines() splits on more than the newline and carriage return: U+2028, U+2029, U+0085, vertical tab, form feed and
U+001C to U+001E. A blank value, a cut that splits a combining sequence, a double full stop after a cut and a blank name all need
handling the same way everywhere. brain.text.single_line decides what one line is. This file holds the helper to its
contract and fails on any new hand-rolled cleaning of text for one line.

Text that is only matched or normalised, not shown on one line, ends its line with `# not one-line: <reason>`.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from brain.text import ELLIPSIS, NotOneLine, has_line_break, single_line

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src/brain", "src/brain_fetch", "src/brain_hub")
OWNER = "src/brain/text.py"
EXEMPT = re.compile(r"#\s*not one-line:\s*\S")
LINE_BREAK_CHARS = set("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
# Every character str.splitlines() splits on, found by asking Python over all of Unicode, so the list above is held
# to Python and not the other way round.
SPLITTERS = {chr(code) for code in range(0x110000) if len(("x" + chr(code) + "x").splitlines()) != 1}


# -- the lint ---------------------------------------------------------------------------------------------------


def _break_literal(node: ast.AST) -> bool:
    """A string constant made only of line break characters, such as "\\n" or "\\r\\n"."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value != ""
        and set(node.value) <= SPLITTERS
    )


def _is_call(node: ast.AST, attr: str) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr


def _found(node: ast.AST) -> str | None:
    """What this node does by hand that brain.text.single_line does, or None."""
    if _is_call(node, "isprintable"):
        return "checks isprintable() by hand"
    if _is_call(node, "replace") and node.args and _break_literal(node.args[0]):
        return "replaces a line break by hand"
    if isinstance(node, ast.Compare):
        if isinstance(node.ops[0], (ast.In, ast.NotIn)) and _break_literal(node.left):
            return "tests for a line break by hand"
        if any(_is_call(sub, "splitlines") for part in (node.left, *node.comparators) for sub in ast.walk(part)):
            return "tests splitlines() by hand"
    if (
        _is_call(node, "join")
        and isinstance(node.func.value, ast.Constant)
        and node.func.value.value == " "
        and len(node.args) == 1
        and _is_call(node.args[0], "split")
        and not node.args[0].args
    ):
        return 'collapses whitespace with " ".join(....split()) by hand'
    return None


def scan(name: str, source: str) -> list[str]:
    """One message per hand-rolled one-line cleaning in `source` not marked `# not one-line:`, naming `name`."""
    lines = source.splitlines()
    found, seen = [], set()
    for node in ast.walk(ast.parse(source)):
        what = _found(node)
        if what is None or (node.lineno, what) in seen:
            continue
        seen.add((node.lineno, what))
        span = lines[node.lineno - 1 : getattr(node, "end_lineno", node.lineno)]
        if any(EXEMPT.search(line) for line in span):
            continue
        found.append(
            f"{name}:{node.lineno}: {what}. Text for one line goes through brain.text.single_line(value, "
            f"max_chars=..., max_bytes=..., ellipsis=..., refuse=..., required=...), and brain.text.has_line_break(value) "
            f"asks only whether a line break is there. If this text is matched or normalised and never shown on "
            f"one line, end the line with `# not one-line: <reason>`."
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


def test_no_hand_rolled_one_line_cleaning_outside_brain_text() -> None:
    found = scan_tree(ROOT)
    assert not found, "\n" + "\n".join(found)


def test_the_lint_flags_each_old_copy_and_accepts_the_helper_and_the_marked_exception() -> None:
    bad = (
        'x = "\\n" in value\n',
        'x = "\\r" not in value\n',
        'x = value.replace("\\n", " ")\n',
        'x = value.replace("\\r\\n", " ")\n',
        'x = value.replace("\\u2028", " ")\n',
        "x = value.isprintable()\n",
        "x = len(value.splitlines()) > 1\n",
        'x = " ".join(value.split())[:80]\n',
        'x = " ".join(str(value).split())\n',
    )
    for source in bad:
        assert len(scan("src/brain/example.py", source)) == 1, source
    good = (
        "x = single_line(value, max_chars=80)\n",
        "x = has_line_break(value)\n",
        'x = "a" in value\n',
        'x = value.replace("a", "b")\n',
        'x = full + "\\n" in text\n',  # a line ending in a file being edited, not a check for line breaks
        "x = value.splitlines()\n",
        'x = " ".join(words)\n',
        'x = "-".join(value.split())\n',
        'x = " ".join(value.split())  # not one-line: a matching key\n',
        'x = " ".join(\n    value.split()\n)  # not one-line: a matching key\n',
    )
    for source in good:
        assert scan("src/brain/example.py", source) == [], source
    message = scan("src/brain/example.py", bad[0])[0]
    assert "src/brain/example.py:1" in message and "brain.text.single_line" in message


# -- the helper -------------------------------------------------------------------------------------------------


def test_has_line_break_is_what_splitlines_splits_on() -> None:
    assert SPLITTERS == LINE_BREAK_CHARS
    for ch in SPLITTERS:
        assert has_line_break(f"ab{ch}cd") and has_line_break(ch) and has_line_break(f"ab{ch}")
    assert not has_line_break("") and not has_line_break("a b\tc\u00a0d\u200be")


@pytest.mark.parametrize("ch", sorted(SPLITTERS))
def test_every_line_break_becomes_a_space_or_is_refused(ch: str) -> None:
    assert single_line(f"Robin{ch}PAUSED. Writes are stopped.") == "Robin PAUSED. Writes are stopped."
    assert single_line(f"{ch}a{ch}{ch}b{ch}") == "a b"
    with pytest.raises(NotOneLine, match=rf"U\+{ord(ch):04X}"):
        single_line(f"Example{ch}Co", refuse=True)


def test_non_printable_characters_become_spaces_or_are_refused() -> None:
    for ch in ("\t", "\x00", "\x07", "\x7f", "\x9f", "\u00a0", "\u200b", "\u202e", "\ufeff", "\ud800", "\ue000"):
        assert single_line(f"a{ch}b") == "a b", repr(ch)
        with pytest.raises(NotOneLine):
            single_line(f"a{ch}b", refuse=True)
    assert single_line("  a \t b   c  ") == "a b c"
    assert single_line(None) == "" and single_line(7) == "7"


def test_joiners_stay_so_an_emoji_family_and_a_flag_are_not_torn_apart() -> None:
    family = "\U0001F468\u200d\U0001F469\u200d\U0001F467"
    england = "\U0001F3F4\U000E0067\U000E0062\U000E0065\U000E006E\U000E0067\U000E007F"
    assert single_line(f"{family} {england}") == f"{family} {england}"
    assert single_line(f"{family}", refuse=True) == family
    assert single_line("a\u200cb") == "a\u200cb"


def test_refusing_tidies_only_plain_spaces_and_names_the_character() -> None:
    assert single_line("  Example   Co ", refuse=True) == "Example Co"
    with pytest.raises(NotOneLine, match=r"must be one line of printable text: replace U\+2028 with a space"):
        single_line("Example\u2028Co", refuse=True)
    with pytest.raises(TypeError):
        single_line("a", refuse=True, max_chars=3)


def test_a_blank_value_is_refused_where_one_is_required() -> None:
    for blank in ("", "   ", "\u2028", "\t\n", "\u200b"):
        assert single_line(blank) == ""
        with pytest.raises(NotOneLine, match="must not be blank"):
            single_line(blank, required=True)
        with pytest.raises(NotOneLine):
            single_line(blank, refuse=True, required=True)


def test_a_cut_keeps_graphemes_and_both_budgets_include_the_ellipsis() -> None:
    pad = "x" * 64
    # Characters: 67 kept before "...", and a combining accent that would be split takes its letter with it.
    assert single_line(pad + "xxe\u0301" + "y" * 10, max_chars=70, ellipsis=ELLIPSIS) == pad + "xx..."
    assert single_line(pad + "xe\u0301" + "y" * 10, max_chars=70, ellipsis=ELLIPSIS) == pad + "xe\u0301..."
    # Bytes: a 3-byte character that does not fit is not cut in two, and nor is its accent.
    cut = single_line("ab" + "\u4e2d" * 5, max_bytes=10, ellipsis=ELLIPSIS)
    assert cut == "ab\u4e2d..." and len(cut.encode()) == 8
    assert single_line("abc" + "e\u0301" * 5, max_bytes=8, ellipsis=ELLIPSIS) == "abc..."
    assert single_line("short", max_chars=5, max_bytes=5, ellipsis=ELLIPSIS) == "short"
    # No ellipsis: a plain cut, still at a grapheme boundary.
    assert single_line("abe\u0301cd", max_chars=3) == "ab"
    for limit in range(0, 12):
        for sample in ("a" * 20, "e\u0301" * 10, "\U0001F1E6\U0001F1FA" * 6, "\u4e2d" * 8):
            assert len(single_line(sample, max_chars=limit, ellipsis=ELLIPSIS)) <= max(limit, 3)
            assert len(single_line(sample, max_bytes=limit, ellipsis=ELLIPSIS).encode()) <= max(limit, 3)


def test_a_cut_never_leaves_a_double_full_stop() -> None:
    cut = single_line("Example Co Pty Ltd. Then more words follow here", max_chars=22, ellipsis=ELLIPSIS)
    assert cut == "Example Co Pty Ltd..."
    assert single_line("a....b....c....d", max_chars=8, ellipsis=ELLIPSIS) == "a..."
    # A text that ends where the budget does is not cut, so keeps its own full stop.
    assert single_line("Example Co Pty Ltd.", max_chars=19, ellipsis=ELLIPSIS) == "Example Co Pty Ltd."


# -- the callers ------------------------------------------------------------------------------------------------


def _config(tmp_path: Path, **principal) -> Path:
    from tests.conftest import render_config

    path = tmp_path / "brain.toml"
    path.write_text(render_config(principal={"person_id": "per-alex-example", "names": ["Alex"], "aliases": [],
                                             "display_name": "Alex Example", **principal}), encoding="utf-8")
    return path


@pytest.mark.parametrize("ch", sorted(SPLITTERS))
def test_org_and_the_principal_name_refuse_every_line_break(tmp_path: Path, ch: str) -> None:
    from brain.config import ConfigError, load_config
    from tests.conftest import TEST_CONFIG

    for bad in (f"Example{ch}Co", f"{ch}", f"Example Co{ch}"):
        text = TEST_CONFIG.replace('timezone = "UTC"\n', f'timezone = "UTC"\norg = {json.dumps(bad)}\n', 1)
        (tmp_path / "org.toml").write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match=r"instance\.org must be one line of printable text: replace U\+"):
            load_config(tmp_path / "org.toml")
        with pytest.raises(ConfigError, match=r"principal\.display_name must be one line of printable text"):
            load_config(_config(tmp_path, display_name=f"Alex{ch}Example"))


def test_org_and_the_principal_name_must_not_be_blank_and_are_tidied(tmp_path: Path) -> None:
    from brain.config import ConfigError, load_config
    from tests.conftest import TEST_CONFIG

    for blank in ("", "   ", "\u200b"):
        text = TEST_CONFIG.replace('timezone = "UTC"\n', f'timezone = "UTC"\norg = {json.dumps(blank)}\n', 1)
        (tmp_path / "org.toml").write_text(text, encoding="utf-8")
        with pytest.raises(ConfigError, match=r"instance\.org (length is invalid|must)"):
            load_config(tmp_path / "org.toml")
    with pytest.raises(ConfigError, match=r"principal\.display_name must not be blank"):
        load_config(_config(tmp_path, display_name="   "))
    text = TEST_CONFIG.replace('timezone = "UTC"\n', 'timezone = "UTC"\norg = "  Example   Co "\n', 1)
    (tmp_path / "org.toml").write_text(text, encoding="utf-8")
    assert load_config(tmp_path / "org.toml").org == "Example Co"


def test_options_aliases_and_words_refuse_every_line_break() -> None:
    from brain.collectors.base import Option
    from brain.config import ConfigError, _alias_lists, _option_value, value_words_error

    for ch in sorted(SPLITTERS):
        for kind, value in (("strings", [f"a{ch}b"]), ("query", f"a{ch}b"), ("path", f"/tmp/a{ch}b")):
            with pytest.raises(ConfigError):
                _option_value("x", "k", Option(kind), value, set())
        with pytest.raises(ConfigError, match="on one line"):
            _alias_lists({"excloud": [f"a{ch}b"]}, "entities")
        assert value_words_error("enum", ("a",), {"a": [f"a{ch}b"]}) is not None
    assert _option_value("x", "k", Option("strings"), ["a b"], set()) == ["a b"]


def test_a_long_wide_org_gives_way_and_the_brief_keeps_its_date(tmp_path: Path) -> None:
    from brain.init import init_instance
    from tests.conftest import TEST_CONFIG
    from tests.test_phase7 import _brief

    org = "\u4e2d" * 120  # 360 bytes, over the 200 a brief line may take
    config = TEST_CONFIG.replace('timezone = "UTC"\n', f'timezone = "UTC"\norg = "{org}"\n', 1)
    instance = Path(init_instance("demo", tmp_path, config_text=config)["instance"])
    first = _brief(instance).splitlines()[0]
    assert len(first.encode()) <= 200
    assert first.startswith("demo brain for \u4e2d") and "...," in first
    assert re.search(r", schema [0-9]+, today [0-9]{4}-[0-9]{2}-[0-9]{2} \(UTC\)\.$", first), first


def test_init_refuses_a_principal_name_with_a_line_break_and_keeps_the_rest(tmp_path: Path) -> None:
    from brain.init import InitError, _render_template

    with pytest.raises(InitError, match=r"principal display name must be one line of printable text: replace U\+2028"):
        _render_template("demo", "UTC", "Morgan\u2028Vale")
    with pytest.raises(InitError, match="must not be blank"):
        _render_template("demo", "UTC", "  ")
    assert 'display_name = "Morgan Vale"' in _render_template("demo", "UTC", " Morgan   Vale ")


def test_connect_refuses_a_server_name_that_would_write_two_lines(tmp_path: Path) -> None:
    from brain.connect import ConnectError, remote_connect

    for ch in sorted(SPLITTERS):
        with pytest.raises(ConnectError, match=rf"--name must be one line of printable text: replace U\+{ord(ch):04X}"):
            remote_connect(["claude"], "https://hub.example.com/mcp", "BRAIN_TOKEN", f"hub{ch}extra", print_only=True)
    with pytest.raises(ConnectError, match="--name must not be blank"):
        remote_connect(["claude"], "https://hub.example.com/mcp", "BRAIN_TOKEN", "  ", print_only=True)
