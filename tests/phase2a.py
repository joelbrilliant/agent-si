"""Shared helpers for the capture tests: capture exports, collect runs and store reads. Invented data only."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path

from brain import store
from brain.cli import main
from tests.collector_fixtures import collector_instance
from tests.conftest import ro_rows

ORIGIN = "work-docs"
FETCHED = "2026-10-08T00:00:00Z"


def ref_of(path: str, kind: str = "document") -> str:
    return f"{kind}:{ORIGIN}/{path}"


def item(path: str, text: str, *, kind: str = "document", date: str | None = "2026-10-07", **extra) -> dict:
    row = {"ref": ref_of(path, kind), "kind": kind, "title": path, "text": text}
    if date is not None:
        row["date"] = date
    row.update(extra)
    return row


def export(items=(), *, present=(), complete: bool = True, fetched_at: str = FETCHED, origin: str = ORIGIN, **extra) -> dict:
    doc = {
        "format": "agent-brain.capture.v1",
        "fetched_at": fetched_at,
        "complete": complete,
        "origin": origin,
        "items": list(items),
    }
    if present:
        doc["present"] = list(present)
    doc.update(extra)
    return doc


def capture_instance(tmp_path: Path, **kwargs) -> Path:
    return collector_instance(tmp_path, "work-docs", **kwargs)


def write_export(directory: Path, doc: dict) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    raw = store.canonical_json(doc).encode()
    path = directory / f"{hashlib.sha256(raw).hexdigest()[:16]}.json"
    path.write_bytes(raw)
    return path


def run_collect(instance: Path, doc_or_path, *extra: str, name: str = "work-docs", flag: str = "--input"):
    path = doc_or_path if isinstance(doc_or_path, Path) else write_export(Path(instance).parent / "exports", doc_or_path)
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(["collect", name, "--instance", str(instance), flag, str(path), *extra])
    text = stdout.getvalue().strip()
    return code, (json.loads(text) if text else None), stderr.getvalue()


def sources(instance: Path, ref: str | None = None) -> list[tuple]:
    """(id, supersedes_id, content) of every capture source, oldest first."""
    sql = "SELECT id, supersedes_id, content FROM sources WHERE id GLOB 'src-cap-*'"
    params: tuple = ()
    if ref is not None:
        sql += " AND external_ref = ?"
        params = (ref,)
    return ro_rows(instance, sql + " ORDER BY captured_at, rowid", params)


def runs(instance: Path) -> list[tuple]:
    return ro_rows(instance, "SELECT status, items, error FROM collector_runs ORDER BY started_at, rowid")


def source_count(instance: Path) -> int:
    return ro_rows(instance, "SELECT count(*) FROM sources")[0][0]


def custom_config(
    *,
    table: dict | None = None,
    agent: dict | None = None,
    agents: dict | None = None,
    extra_collectors: dict | None = None,
    fact_keys: list[dict] | None = None,
    extra_toml: str = "",
    top_toml: str = "",
    timezone: str = "UTC",
) -> str:
    """The brain.toml text of an instance with the work-docs capture collector, its table and agent
    adjustable, plus extra toml at the end (tables) or the start (root keys)."""
    from tests.collector_fixtures import PRINCIPAL, _default_agents, _default_fact_keys
    from tests.conftest import render_config

    ops = ["add_source", "record_collector_run"]
    merged = _default_agents()
    merged["workdocs"] = {"kind": "collector", "operations": ops, "author_principal": True, **(agent or {})}
    merged.update(agents or {})
    collectors = {"work-docs": {"type": "capture", "agent": "workdocs", "origin": "work-docs", **(table or {})}}
    collectors.update(extra_collectors or {})
    text = render_config(
        timezone=timezone,
        principal=PRINCIPAL,
        fact_keys=list(_default_fact_keys() if fact_keys is None else fact_keys),
        agents=merged,
        collectors=collectors,
    )
    return top_toml + text + "\n" + extra_toml


def custom_instance(tmp_path: Path, **kwargs) -> Path:
    from brain.init import init_instance

    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    return Path(init_instance("demo", Path(tmp_path), config_text=custom_config(**kwargs))["instance"])


def load_text(tmp_path: Path, text: str):
    """load_config of a brain.toml text, without building a store."""
    from brain.config import load_config

    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    path = Path(tmp_path) / "brain.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)
