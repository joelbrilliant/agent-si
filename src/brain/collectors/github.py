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
    local_date,
    known_facts,
    observe_facts,
    one_line,
    plan_note,
    utc_schema,
)

FORMAT = "agent-brain.github.v1"
_REPO = r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$"
_SHA = r"^[0-9a-f]{40}$"
_CONCLUSIONS = (
    "success",
    "failure",
    "neutral",
    "cancelled",
    "skipped",
    "timed_out",
    "action_required",
    "startup_failure",
)
_SUCCESS = {"success", "neutral", "skipped"}
FACTS = (
    ("ci_state", "enum", ("success", "failure", "pending", "none")),
    ("last_push_on", "date", None),
    ("open_pull_requests", "int", None),
)

LIVE = LiveAdapter(
    type="github",
    config_keys=("live_api_url",),
    env_vars=("BRAIN_<NAME>_TOKEN",),
    access=(
        "A fine-grained token for one repository with read-only Metadata, "
        "Pull requests and Actions"
    ),
)

_PR = {
    "type": "object",
    "additionalProperties": False,
    "required": ["number", "state", "draft", "updated_at"],
    "properties": {
        "number": {"type": "integer", "minimum": 1},
        "state": {"type": "string", "enum": ["open", "closed", "merged"]},
        "draft": {"type": "boolean"},
        "updated_at": utc_schema(),
    },
}
_RUN = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "branch", "head_sha", "status", "conclusion", "updated_at"],
    "properties": {
        "id": {"type": "integer", "minimum": 1},
        "branch": one_line(1, 255),
        "head_sha": {"type": "string", "pattern": _SHA},
        "status": {"type": "string", "enum": ["queued", "in_progress", "completed"]},
        "conclusion": {
            "type": ["string", "null"],
            "enum": [*_CONCLUSIONS, None],
        },
        "updated_at": utc_schema(),
    },
}
_PUSH = {
    "type": "object",
    "additionalProperties": False,
    "required": ["branch", "head_sha", "at"],
    "properties": {
        "branch": one_line(1, 255),
        "head_sha": {"type": "string", "pattern": _SHA},
        "at": utc_schema(),
    },
}

SCHEMA = document_schema(
    FORMAT,
    {
        "repo": {"type": "string", "pattern": _REPO},
        "default_branch": one_line(1, 255),
        "pull_requests": {"type": "array", "min_items": 0, "max_items": 1000, "items": _PR},
        "runs": {"type": "array", "min_items": 0, "max_items": 1000, "items": _RUN},
        "pushes": {"type": "array", "min_items": 0, "max_items": 1000, "items": _PUSH},
    },
    ("repo", "default_branch", "pull_requests", "runs", "pushes"),
)


def _ci_state(doc: dict) -> str:
    runs = [item for item in doc["runs"] if item["branch"] == doc["default_branch"]]
    if not runs:
        return "none"
    latest = max(runs, key=lambda item: (item["updated_at"], item["id"]))
    if latest["status"] != "completed":
        return "pending"
    if latest["conclusion"] in _SUCCESS:
        return "success"
    return "failure"


def _push_date(doc: dict, ctx: RunContext) -> str | None:
    pushes = [item for item in doc["pushes"] if item["branch"] == doc["default_branch"]]
    if not pushes:
        return None
    latest = max(pushes, key=lambda item: item["at"])
    return local_date(latest["at"], ctx.timezone)


def _values(doc: dict, ctx: RunContext) -> dict[str, object]:
    values: dict[str, object] = {
        "ci_state": _ci_state(doc),
        "open_pull_requests": sum(1 for item in doc["pull_requests"] if item["state"] == "open"),
    }
    pushed = _push_date(doc, ctx)
    if pushed is not None:
        values["last_push_on"] = pushed
    return values


def known(con, doc: dict, ctx: RunContext) -> dict:
    return {
        "facts": known_facts(
            con,
            ctx.options["fact_prefix"],
            [name for name, _kind, _allowed in FACTS],
            ctx.agent,
        )
    }


def _known(con, doc: dict, ctx: RunContext) -> dict:
    return known(con, doc, ctx)


def _check(doc: dict) -> None:
    seen_prs: set[int] = set()
    for item in doc["pull_requests"]:
        number = item["number"]
        if number in seen_prs:
            raise DocumentError(f"duplicate pull_request id {number}")
        seen_prs.add(number)
        ensure_not_future(
            item["updated_at"],
            doc["fetched_at"],
            f"pull request {number} updated_at is in the future",
        )
    seen_runs: set[int] = set()
    for item in doc["runs"]:
        run_id = item["id"]
        if run_id in seen_runs:
            raise DocumentError(f"duplicate run id {run_id}")
        seen_runs.add(run_id)
        completed = item["status"] == "completed"
        if completed == (item["conclusion"] is None):
            raise DocumentError(
                f"run {run_id}: conclusion is null exactly when status is not completed"
            )
        ensure_not_future(
            item["updated_at"],
            doc["fetched_at"],
            f"run {run_id} updated_at is in the future",
        )
    for item in doc["pushes"]:
        ensure_not_future(item["at"], doc["fetched_at"], f"push {item['branch']} at is in the future")


def plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    ensure_not_future(doc["fetched_at"], ctx.now, "fetched_at is in the future")
    repo = ctx.options["repo"]
    if doc["repo"] != repo:
        raise DocumentError(f"document repo {doc['repo']} doesn't match collector repo {repo}")
    _check(doc)
    items = len(doc["pull_requests"]) + len(doc["runs"]) + len(doc["pushes"])
    groups: tuple[Group, ...] = ()
    if doc["complete"]:
        groups = observe_facts(
            doc,
            known_rows,
            ctx,
            _values(doc, ctx),
            tuple(name for name, _kind, _allowed in FACTS),
            {name: kind for name, kind, _allowed in FACTS},
        )
    return Plan(groups, items, plan_note(doc))


def _plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    return plan(doc, known_rows, ctx)


TYPE = CollectorType(
    name="github",
    format=FORMAT,
    ops=("add_source", "upsert_fact", "record_collector_run"),
    options={
        "repo": Option("repo", required=True),
        "fact_prefix": Option("fact_prefix", required=True),
    },
    document_schema=SCHEMA,
    live=LIVE,
    known=_known,
    plan=_plan,
    source_kind="api",
)
