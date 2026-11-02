from __future__ import annotations

from brain.collectors.base import (
    CollectorType,
    DocumentError,
    Group,
    LiveAdapter,
    Option,
    Plan,
    RunContext,
    document_schema,
    ensure_not_future,
    hash_parts,
    local_date,
    one_line,
    plan_note,
    utc_schema,
)

FORMAT = "agent-brain.files.v1"
_LABEL = r"^[a-z][a-z0-9-]{1,39}$"
_CHUNK = 500

LIVE = LiveAdapter(
    type="files",
    config_keys=("live_path",),
    env_vars=(),
    access="Read access to one local directory",
)

SCHEMA = document_schema(
    FORMAT,
    {
        "root": {"type": "string", "pattern": _LABEL},
        "files": {
            "type": "array",
            "min_items": 0,
            "max_items": 5000,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "size", "mtime", "sha256"],
                "properties": {
                    "path": one_line(1, 400),
                    "size": {"type": "integer", "minimum": 0},
                    "mtime": utc_schema(),
                    "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                },
            },
        },
    },
    ("root", "files"),
)


def source_id(collector: str, path: str, sha256: str) -> str:
    return f"src-file-{hash_parts(collector, path)[:16]}-{sha256[:16]}"


def _unsafe(path: str) -> bool:
    if path.startswith("/") or "\\" in path:
        return True
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        return True
    return any(segment in {"", ".", ".."} for segment in path.split("/"))


def _title(path: str) -> str:
    if len(path) <= 200:
        return path
    return path[:197] + "..."


def _content(root: str, path: str, sha256: str, size: int, mtime: str) -> str:
    return (
        f"file: {root}/{path}\n"
        f"sha256: {sha256}\n"
        f"size: {size} bytes\n"
        f"modified: {mtime}"
    )


def known(con, doc: dict, ctx: RunContext) -> dict:
    ids = [source_id(ctx.collector, item["path"], item["sha256"]) for item in doc["files"]]
    found: list[str] = []
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start : start + _CHUNK]
        marks = ",".join("?" for _ in chunk)
        rows = con.execute(f"SELECT id FROM sources WHERE id IN ({marks})", chunk)
        found.extend(row[0] for row in rows)
    return {"ids": found}


def _known(con, doc: dict, ctx: RunContext) -> dict:
    return known(con, doc, ctx)


def plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    ensure_not_future(doc["fetched_at"], ctx.now, "fetched_at is in the future")
    root = ctx.options["root"]
    if doc["root"] != root:
        raise DocumentError(f"document root {doc['root']} doesn't match collector root {root}")
    seen: set[str] = set()
    ordered = sorted(doc["files"], key=lambda item: item["path"])
    present = set(known_rows.get("ids") or ())
    groups: list[Group] = []
    for item in ordered:
        path = item["path"]
        if _unsafe(path):
            raise DocumentError(f"unsafe path {path}")
        if path in seen:
            raise DocumentError(f"duplicate file id {path}")
        seen.add(path)
        ensure_not_future(item["mtime"], doc["fetched_at"], f"{path}: mtime is in the future")
        row_id = source_id(ctx.collector, path, item["sha256"])
        if row_id in present:
            continue
        groups.append(
            Group(
                ops=(
                    {
                        "op": "add_source",
                        "row": {
                            "id": row_id,
                            "kind": ctx.options["source_kind"],
                            "title": _title(path),
                            "uri": "",
                            "source_date": local_date(item["mtime"], ctx.timezone),
                            "author_person_id": None,
                            "content": _content(root, path, item["sha256"], item["size"], item["mtime"]),
                            "external_ref": f"file:{root}/{path}",
                        },
                    },
                )
            )
        )
    return Plan(tuple(groups), len(doc["files"]), plan_note(doc))


def _plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    return plan(doc, known_rows, ctx)


TYPE = CollectorType(
    name="files",
    format=FORMAT,
    ops=("add_source", "record_collector_run"),
    options={
        "root": Option("label", required=True),
        "source_kind": Option(
            "enum",
            default="document",
            allowed=("document", "meeting_transcript", "user_note"),
        ),
    },
    document_schema=SCHEMA,
    live=LIVE,
    known=_known,
    plan=_plan,
    source_kind="document",
)
