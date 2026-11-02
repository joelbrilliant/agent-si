"""GitHub: Markdown files and release notes as a full listing, unchanged heads skipped, a
changed file as a version, a deleted one as a removal, and an incomplete tree removing nothing. Invented data."""
from __future__ import annotations

import base64

from tests import fetch_net as fn
from tests.conftest import ro_rows
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

REPO = r"api\.github\.com/repos/example-co/handbook"
COLLECTORS = {"work-github": {"type": "capture", "agent": "workgithub", "origin": "work-github", "kinds": ["document"],
                              "service": "github", "repo": "example-co/handbook"}}
AGENTS = {"workgithub": {"kind": "collector", "operations": ["add_source", "record_collector_run"]}}
H1, H2 = "1" * 40, "2" * 40
BLOBS = {"a" * 40: "# Example handbook\n\nWelcome.", "b" * 40: "# Pricing\n\nPro is 128k tokens.",
         "c" * 40: "# Old page", "d" * 40: "# Pricing\n\nPro is 200k tokens."}


def blob(sha: str, path: str, size: int = 100) -> dict:
    return {"path": path, "type": "blob", "sha": sha, "size": size}


TREES = {
    H1: {"tree": [blob("a" * 40, "README.md"), blob("b" * 40, "docs/pricing.md"), blob("c" * 40, "docs/old.md"),
                  blob("e" * 40, "src/app.py"), {"path": "docs", "type": "tree", "sha": "f" * 40}], "truncated": False},
    H2: {"tree": [blob("a" * 40, "README.md"), blob("d" * 40, "docs/pricing.md")], "truncated": False},
}
RELEASES = [{"id": 11, "name": "v1.0", "tag_name": "v1.0", "body": "First release.", "published_at": "2026-10-01T00:00:00Z",
             "html_url": "https://github.com/example-co/handbook/releases/tag/v1.0"},
            {"id": 12, "name": "v1.1 draft", "draft": True, "body": "Not yet."}]


def world(tmp_path, monkeypatch):
    fn.secret(monkeypatch, tmp_path, "work-github", "github-token-example")
    fake = fn.FakeNet(monkeypatch)
    head = {"sha": H1}
    fake.route("GET", REPO, {"default_branch": "main"})
    fake.route("GET", REPO + "/branches/main", lambda call: {"commit": {"sha": head["sha"]}})
    fake.route("GET", REPO + r"/git/trees/(\w+)", lambda call: TREES[call["path"].rsplit("/", 1)[1]])
    fake.route("GET", REPO + r"/git/blobs/(\w+)", lambda call: {
        "encoding": "base64", "content": base64.b64encode(BLOBS[call["path"].rsplit("/", 1)[1]].encode()).decode()})
    fake.route("GET", REPO + "/releases", RELEASES)
    return fn.instance(tmp_path, COLLECTORS, AGENTS), fake, head


def current(inst) -> dict:
    return {ref: content.split("\n\n", 1)[1] for ref, content in ro_rows(
        inst, "SELECT external_ref, content FROM sources AS s WHERE id GLOB 'src-cap-*' "
              "AND NOT EXISTS (SELECT 1 FROM sources AS n WHERE n.supersedes_id = s.id)")}


def test_files_and_releases_as_a_full_listing(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, head = world(tmp_path, monkeypatch)
    code, result, err = fn.run(inst, "work-github")
    assert (code, result["status"], result["items"], result["cursor"], err) == (0, "ok", 4, "advanced", "")
    assert current(inst) == {"document:work-github/README.md": "# Example handbook\n\nWelcome.",
                             "document:work-github/docs/pricing.md": "# Pricing\n\nPro is 128k tokens.",
                             "document:work-github/docs/old.md": "# Old page",
                             "document:work-github/releases/11": "First release."}
    uris = dict(ro_rows(inst, "SELECT external_ref, uri FROM sources WHERE id GLOB 'src-cap-*'"))
    assert uris["document:work-github/docs/pricing.md"] == "https://github.com/example-co/handbook/blob/main/docs/pricing.md"
    assert all(call["headers"]["Accept"] == "application/vnd.github+json" for call in fake.calls)
    assert "github-token-example" not in fn.written(inst)
    # The same head reads no tree and no blob, and changes nothing.
    fake.calls.clear()
    clock.at("2026-10-09T01:00:00Z")
    code, result, _ = fn.run(inst, "work-github")
    assert (code, result["items"]) == (0, 0)
    assert not [path for path in fake.paths() if "/git/" in path]
    # A new head: the changed file is a new version, the deleted one gets a removal version.
    head["sha"] = H2
    clock.at("2026-10-09T02:00:00Z")
    code, result, _ = fn.run(inst, "work-github")
    assert (code, result["items"], result["cursor"]) == (0, 1, "advanced")
    now = current(inst)
    assert now["document:work-github/docs/pricing.md"] == "# Pricing\n\nPro is 200k tokens."
    assert now["document:work-github/docs/old.md"] == "removed from work-github on 2026-10-09"
    blobs = [path for path in fake.paths() if "/git/blobs/" in path]
    assert blobs[-1].endswith("d" * 40) and not any(path.endswith("a" * 40) for path in blobs[-1:])


def test_an_incomplete_tree_removes_nothing(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, head = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-github")[0] == 0
    good = fn.state_of(inst, "work-github")
    TREES["3" * 40] = {**TREES[H2], "truncated": True}
    head["sha"] = "3" * 40
    clock.at("2026-10-09T01:00:00Z")
    code, result, _ = fn.run(inst, "work-github")
    assert (code, result["cursor"]) == (0, "held") and "incomplete" in result["message"]
    assert current(inst)["document:work-github/docs/old.md"] == "# Old page"  # nothing removed
    assert fn.state_of(inst, "work-github") == good


def test_big_files_are_cut_or_skipped_without_holding_the_cursor(tmp_path, monkeypatch, clock: Clock) -> None:
    """R2: one oversized file once held the cursor for good. Up to 16 MiB is read and cut at 3.5 MiB by the
    collector; above that it's skipped, and a stored earlier version stays listed so it isn't removed."""
    inst, fake, head = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-github")[0] == 0
    BLOBS["7" * 40] = "# Long\n\n" + "word " * 900_000
    TREES["4" * 40] = {"tree": [blob("a" * 40, "README.md"), blob("b" * 40, "docs/pricing.md"),
                               blob("8" * 40, "docs/old.md", size=17_000_000), blob("7" * 40, "docs/long.md", size=4_500_008),
                               blob("6" * 40, "docs/huge.md", size=40_000_000)], "truncated": False}
    head["sha"] = "4" * 40
    clock.at("2026-10-09T01:00:00Z")
    code, result, _ = fn.run(inst, "work-github")
    assert (code, result["cursor"], result["items"]) == (0, "advanced", 1)
    assert result["message"] == "2 files were skipped (an unusable path, over 16 MiB, or not text)"
    now = current(inst)
    assert now["document:work-github/docs/old.md"] == "# Old page"  # grew past 16 MiB: the old version stays
    assert "document:work-github/docs/huge.md" not in now
    long_row = ro_rows(inst, "SELECT content FROM sources WHERE external_ref = 'document:work-github/docs/long.md'")
    assert "truncated at 2,000,000 characters" in long_row[0][0]  # ASCII text: the character cap binds first
    assert not [path for path in fake.paths() if path.endswith("8" * 40) or path.endswith("6" * 40)]


def test_a_missing_repository_is_a_config_error(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, head = world(tmp_path, monkeypatch)
    fake.route("GET", REPO, (404, {"message": "Not Found"}))
    code, result, _ = fn.run(inst, "work-github")
    assert (code, result["cursor"]) == (1, "held")
    assert result["message"] == "config: GitHub answered 404; check repo and the token's repository access"


def test_an_odd_listing_never_reads_as_empty(tmp_path, monkeypatch, clock: Clock) -> None:
    """A full listing read as empty would remove every file or release: an odd answer fails the run instead."""
    inst, fake, head = world(tmp_path, monkeypatch)
    assert fn.run(inst, "work-github")[0] == 0
    before = current(inst)
    for route, answer, words in (("/releases", {"unexpected": "shape"}, "releases answer wasn't a list"),
                                 (r"/git/trees/(\w+)", {"sha": H2}, "tree answer had no file list")):
        fake.route("GET", REPO + route, answer)
        head["sha"] = H2
        clock.at("2026-10-09T01:00:00Z")
        code, result, _ = fn.run(inst, "work-github")
        assert (code, result["cursor"]) == (1, "held") and result["message"] == f"network: GitHub's {words}"
        assert current(inst) == before


def test_a_malformed_answer_is_a_failed_run_not_a_crash(tmp_path, monkeypatch, clock: Clock) -> None:
    inst, fake, head = world(tmp_path, monkeypatch)
    fake.route("GET", REPO + r"/git/blobs/(\w+)", {"encoding": "base64", "content": 12})
    fake.route("GET", REPO + "/branches/main", {"commit": ["not", "a", "dict"]})
    code, result, _ = fn.run(inst, "work-github")
    assert (code, result["status"]) == (1, "error")
    assert result["message"] == "network: GitHub's branch answer had no head commit"
    fake.route("GET", REPO + "/branches/main", lambda call: {"commit": {"sha": head["sha"]}})
    fake.route("GET", REPO + "/releases", [{"id": 11, "name": {"odd": True}, "body": "x"}])
    code, result, _ = fn.run(inst, "work-github")
    assert code in (0, 1) and "Traceback" not in str(result)
