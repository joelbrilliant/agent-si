"""The release scanner and the export. Every name here is invented: the denylist is a temp file made in the test.

tools/release_check.py is shipped. tools/release_export.py is not (the export leaves it out), so its tests are
skipped on the exported tree."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = ROOT / "tools" / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"tools/{name}.py is left out of the public tree")
    sys.path.insert(0, str(ROOT / "tools"))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


DENYLIST = """# a comment
zebra corp
mallory
allow: Mallory Example
allow: github.com/example/tool
"""


@pytest.fixture
def words(tmp_path: Path):
    check = _load("release_check")
    path = tmp_path / "denylist.txt"
    path.write_text(DENYLIST, encoding="utf-8")
    return check, check.denylist(path)


def test_the_denylist_keeps_its_entries_and_its_allowed_phrases_apart(words) -> None:
    _, found = words
    assert list(found) == ["zebra corp", "mallory"]
    assert found.allowed == ("github.com/example/tool", "mallory example")  # the longest first


def test_an_allowed_phrase_is_removed_before_matching_and_nothing_else_is(words, tmp_path) -> None:
    check, found = words
    tree = tmp_path / "tree"
    (tree / "docs").mkdir(parents=True)
    (tree / "README.md").write_text("By Mallory Example.\nSee https://github.com/example/tool.\n", encoding="utf-8")
    (tree / "docs" / "a.md").write_text("fine\nMallory said hello\nZEBRA CORP and Mallory Example\n", encoding="utf-8")
    assert check.scan(tree, found) == ["docs/a.md:2: denylist entry 2", "docs/a.md:3: denylist entry 1"]


def test_an_allowed_phrase_in_a_path_is_removed_too_and_a_denied_one_is_not(words, tmp_path) -> None:
    check, found = words
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "mallory example.md").write_text("ok\n", encoding="utf-8")
    (tree / "zebra corp.md").write_text("ok\n", encoding="utf-8")
    assert check.scan(tree, found) == ["zebra corp.md: denylisted name in the path"]


def test_the_report_never_holds_the_text_that_hit(words, tmp_path) -> None:
    check, found = words
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("the zebra corp account\n", encoding="utf-8")
    assert all("zebra" not in line.lower() for line in check.scan(tree, found))


def test_images_and_pdfs_are_searched_as_bytes_and_binaries_are_listed(words, tmp_path) -> None:
    check, found = words
    tree = tmp_path / "tree"
    tree.mkdir()
    png = b"\x89PNG\r\n\x1a\n\x00\x00tEXtAuthor\x00Zebra Corp\x00\x00IDAT"
    (tree / "logo.png").write_bytes(png)
    (tree / "clean.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00tEXtAuthor\x00Mallory Example\x00\x00IDAT")
    (tree / "paper.pdf").write_bytes(b"%PDF-1.4\n/Author (mallory)\n")
    (tree / "notes.txt").write_text("plain\n", encoding="utf-8")
    (tree / "blob.bin").write_bytes(b"\xff\xfe\x00\x01")
    assert check.scan(tree, found) == ["logo.png: denylist entry 1 in the file's bytes",
                                       "paper.pdf: denylist entry 2 in the file's bytes"]
    assert check.binary_files(tree) == ["blob.bin", "clean.png", "logo.png", "paper.pdf"]


def test_main_prints_the_binary_list_and_exits_by_the_hits(tmp_path, capsys) -> None:
    check = _load("release_check")
    denylist = tmp_path / "d.txt"
    denylist.write_text(DENYLIST, encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "pic.png").write_bytes(b"\x89PNG\x00")
    (tree / "a.md").write_text("clean\n", encoding="utf-8")
    assert check.main([str(tree), "--denylist", str(denylist)]) == 0
    out = capsys.readouterr().out
    assert "1 binary or unreadable files to look at by hand: pic.png" in out and "release check: 0 problems" in out
    (tree / "a.md").write_text("zebra corp\n", encoding="utf-8")
    assert check.main([str(tree), "--denylist", str(denylist)]) == 1


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    for name, text in {
        "src/brain/__init__.py": '__version__ = "9.9.9"\n',
        "README.md": "A readme.\n",
        "docs/SPEC-x.md": "internal\n",
        "docs/PROOF-x.md": "internal\n",
        "docs/GUIDE.md": "kept\n",
        "bench/results/run1/log.jsonl": "{}\n",
        "bench/budgets.json": "{}\n",
        "tools/rollout_check.py": "# left out of a release\n",
        "tests/test_rollout_check.py": "# its test\n",
        "tools/release_export.py": "# this tool\n",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "-c", "user.name=Someone Else", "-c", "user.email=someone@example.com", "add", "-A")
    _git(repo, "-c", "user.name=Someone Else", "-c", "user.email=someone@example.com", "commit", "-q", "-m", "one")
    (repo / "README.md").write_text("A readme, second.\n", encoding="utf-8")
    _git(repo, "-c", "user.name=Someone Else", "-c", "user.email=someone@example.com", "commit", "-q", "-am", "two")
    return repo


def test_the_export_leaves_out_the_internal_files_and_makes_one_commit_by_the_public_author(tmp_path, capsys, monkeypatch) -> None:
    export = _load("release_export")
    repo = _source_repo(tmp_path)
    denylist = tmp_path / "d.txt"
    denylist.write_text(DENYLIST, encoding="utf-8")
    # A local git identity must not leak into the commit.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Local Person")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "local@example.com")
    out = tmp_path / "out"
    assert export.main(["--repo", str(repo), "--ref", "HEAD", "--out", str(out), "--denylist", str(denylist)]) == 0
    kept = sorted(_git(out, "ls-files").splitlines())
    assert kept == ["README.md", "bench/budgets.json", "docs/GUIDE.md", "src/brain/__init__.py"]
    assert _git(out, "rev-list", "--count", "HEAD") == "1" and _git(out, "rev-list", "--parents", "-n1", "HEAD").count(" ") == 0
    assert _git(out, "log", "-1", "--format=%an <%ae>|%cn <%ce>|%s") == (
        "Joel Brilliant <228707272+joelbrilliant@users.noreply.github.com>|"
        "Joel Brilliant <228707272+joelbrilliant@users.noreply.github.com>|agent-si 9.9.9")
    said = capsys.readouterr().out
    assert "0 binary files" in said and "5 internal files left out" not in said and "internal files left out" in said


def test_the_export_commits_nothing_when_the_scan_finds_something_and_wont_use_a_full_folder(tmp_path, capsys) -> None:
    export = _load("release_export")
    repo = _source_repo(tmp_path)
    (repo / "docs" / "GUIDE.md").write_text("ask zebra corp\n", encoding="utf-8")
    _git(repo, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-q", "-am", "leak")
    denylist = tmp_path / "d.txt"
    denylist.write_text(DENYLIST, encoding="utf-8")
    out = tmp_path / "out"
    assert export.main(["--repo", str(repo), "--out", str(out), "--denylist", str(denylist)]) == 1
    assert "docs/GUIDE.md:1: denylist entry 1" in capsys.readouterr().out and not (out / ".git").exists()
    assert export.main(["--repo", str(repo), "--out", str(out), "--denylist", str(denylist)]) == 2


def test_the_same_export_serves_another_project_by_name_and_version_file(tmp_path, capsys) -> None:
    export = _load("release_export")
    repo = tmp_path / "other"
    (repo / "agent_memory").mkdir(parents=True)
    (repo / "agent_memory" / "__init__.py").write_text('__version__ = "0.4.2"\n', encoding="utf-8")
    (repo / "README.md").write_text("A readme.\n", encoding="utf-8")
    (repo / "notes").mkdir()
    (repo / "notes" / "x.md").write_text("only here\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "work")
    _git(repo, "-c", "user.name=A", "-c", "user.email=a@example.com", "add", "-A")
    _git(repo, "-c", "user.name=A", "-c", "user.email=a@example.com", "commit", "-q", "-m", "one")
    denylist = tmp_path / "d.txt"
    denylist.write_text(DENYLIST, encoding="utf-8")
    out = tmp_path / "out"
    assert export.main(["--repo", str(repo), "--name", "agent-memory", "--version-file", "agent_memory/__init__.py",
                        "--leave-out", "notes/*", "--out", str(out), "--denylist", str(denylist)]) == 0
    assert sorted(_git(out, "ls-files").splitlines()) == ["README.md", "agent_memory/__init__.py"]
    assert _git(out, "log", "-1", "--format=%an|%ae|%s") == "Joel Brilliant|228707272+joelbrilliant@users.noreply.github.com|agent-memory 0.4.2"
