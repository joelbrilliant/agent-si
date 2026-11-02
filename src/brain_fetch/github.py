"""GitHub: the Markdown files on the default branch and the release notes of one repository
become capture items. The credential is a fine-grained token with read-only Contents on that repository.

The export is a full listing: unchanged files and releases go in present, so a deleted file gets a removal
version and the mass-change guard applies. A truncated tree or an oversized file makes the export incomplete,
so nothing is removed and the cursor holds.
"""
from __future__ import annotations

import base64
import hashlib
import re

from brain.collectors.base import local_date
from brain_fetch import net
from brain_fetch.common import Pulled, capture_doc, line

API = "https://api.github.com/repos/"
ACCEPT = "application/vnd.github+json"
MAX_FILES = 2000
MAX_BYTES = 16 * 1024 * 1024  # the capture collector cuts the text at 3.5 MiB, with a note
_SHA = re.compile(r"[0-9a-f]{40}")
_SAFE_PATH = re.compile(r"[^\x00-\x1f\x7f]{1,400}")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _get(token: str, path: str, **params):
    status, payload = net.request("GET", API + path, token=token, params=params or None, accept=ACCEPT)
    if status in (404, 410):
        raise net.NetError(f"GitHub answered {status}; check repo and the token's repository access", "config", status)
    return payload


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _decode(payload) -> str | None:
    if net.field(payload, "encoding") != "base64" or not isinstance(payload.get("content"), str):
        return None
    try:
        return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
    except ValueError:
        return None


def _tree(token: str, repo: str, head: str) -> tuple[list[dict], bool]:
    payload = _get(token, f"{repo}/git/trees/{head}", recursive="1")
    tree = net.field(payload, "tree")
    if not isinstance(tree, list):
        # This export is a full listing: reading an odd answer as "no files" would remove every file.
        raise net.NetError("GitHub's tree answer had no file list", "network")
    entries = [entry for entry in tree if isinstance(entry, dict)]
    files = [entry for entry in entries if entry.get("type") == "blob" and isinstance(entry.get("path"), str)
             and entry["path"].lower().endswith(".md") and isinstance(entry.get("sha"), str)]
    return files, net.field(payload, "truncated") is True


def pull(ctx) -> Pulled:
    token, repo, origin = ctx.secret, ctx.options["repo"], ctx.options["origin"]
    day = local_date(ctx.fetched, ctx.cfg.timezone)
    branch = net.field(_get(token, repo), "default_branch")
    if not isinstance(branch, str) or not branch:
        raise net.NetError("GitHub's repository answer had no default branch", "network")
    head = net.field(_get(token, f"{repo}/branches/{net.quote(branch, safe='')}"), "commit", "sha")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        raise net.NetError("GitHub's branch answer had no head commit", "network")
    old_files = ctx.cursor.get("files") if isinstance(ctx.cursor.get("files"), dict) else {}
    old_releases = ctx.cursor.get("releases") if isinstance(ctx.cursor.get("releases"), dict) else {}
    items, present, complete, skipped = [], [], True, 0
    files: dict[str, str] = {}
    if head == ctx.cursor.get("head"):
        files = dict(old_files)
        present += [f"document:{origin}/{path}" for path in files]
    else:
        entries, truncated = _tree(token, repo, head)
        complete = not truncated and len(entries) <= MAX_FILES
        for entry in entries[:MAX_FILES]:
            path = entry["path"]
            if not _SAFE_PATH.fullmatch(path) or any(part in ("", ".", "..") for part in path.split("/")):
                skipped += 1
                continue
            ref = f"document:{origin}/{path}"
            if old_files.get(path) == entry["sha"]:
                files[path] = entry["sha"]
                present.append(ref)
                continue
            text = None
            if isinstance(entry.get("size"), int) and entry["size"] <= MAX_BYTES:
                text = _decode(_get(token, f"{repo}/git/blobs/{entry['sha']}"))
            if text is None:
                # Too big, or not decodable: skipped, not a reason to hold the cursor for good. A stored
                # earlier version stays listed, so it isn't removed.
                skipped += 1
                if path in old_files:
                    files[path] = old_files[path]
                    present.append(ref)
                continue
            files[path] = entry["sha"]
            items.append({"ref": ref, "kind": "document", "title": line(path[-200:], 200) or "(file)", "date": day, "text": text,
                          "uri": f"https://github.com/{repo}/blob/{net.quote(branch)}/{net.quote(path)}"[:2000]})
    releases: dict[str, str] = {}
    listed = _get(token, f"{repo}/releases", per_page="100")
    if not isinstance(listed, list):
        raise net.NetError("GitHub's releases answer wasn't a list", "network")  # as for the tree
    for release in listed:
        if not isinstance(release, dict) or release.get("draft") or not isinstance(release.get("id"), int):
            continue
        key, body = str(release["id"]), str(release.get("body") or "")
        ref = f"document:{origin}/releases/{key}"
        title = line(release.get("name") or release.get("tag_name") or f"release {key}", 200)
        releases[key] = _sha(title + "\n" + body)
        if old_releases.get(key) == releases[key]:
            present.append(ref)
            continue
        published = str(release.get("published_at") or "")
        items.append({"ref": ref, "kind": "document", "title": title or f"release {key}",
                      "date": local_date(published, ctx.cfg.timezone) if _STAMP.fullmatch(published) else day,
                      "text": body, "uri": str(release.get("html_url") or "")[:2000]})
    doc = capture_doc(ctx, items, complete=complete, incremental=False, present=present)
    pulled = Pulled(cursor={"head": head, "files": files, "releases": releases}, complete=complete, items=len(items))
    pulled.docs.append((ctx.collector.name, doc))
    pulled.note = f"{skipped} files were skipped (an unusable path, over 16 MiB, or not text)" if skipped else ""
    return pulled


def probe(ctx) -> str:
    _get(ctx.secret, ctx.options["repo"])
    return "ok"
