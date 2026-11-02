from __future__ import annotations

from brain.store import canonical_json
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
    known_facts,
    observe_facts,
    one_line,
    plan_note,
    redacted_keys,
)

FORMAT = "agent-brain.vanta.v1"
_EXTERNAL = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"
_EVIDENCE_MAX = 4096
FACTS = (
    ("controls_complete", "int", None),
    ("controls_total", "int", None),
    ("documents_ok", "int", None),
    ("documents_total", "int", None),
    ("failing_tests", "int", None),
)

LIVE = LiveAdapter(
    type="vanta",
    config_keys=("live_api_url",),
    env_vars=("BRAIN_<NAME>_CLIENT_ID", "BRAIN_<NAME>_CLIENT_SECRET"),
    access="Vanta API client credentials with a read-only scope",
)

_CONTROL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "external_id", "complete"],
    "properties": {
        "id": one_line(1, 120),
        "external_id": {"type": "string", "pattern": _EXTERNAL},
        "complete": {"type": "boolean"},
    },
}
_DOCUMENT = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "status"],
    "properties": {
        "id": one_line(1, 120),
        "status": one_line(1, 40),
    },
}
_TEST = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "name", "status", "due", "link", "controls"],
    "properties": {
        "id": one_line(1, 120),
        "name": one_line(1, 200),
        "status": one_line(1, 40),
        "due": {"type": ["string", "null"], "format": "date"},
        "link": {"type": "string", "max_length": 500, "pattern": r"^$|^https://\S+$"},
        "controls": {
            "type": "array",
            "min_items": 0,
            "max_items": 50,
            "items": {"type": "string", "pattern": _EXTERNAL},
        },
    },
}

SCHEMA = document_schema(
    FORMAT,
    {
        "controls": {"type": "array", "min_items": 0, "max_items": 2000, "items": _CONTROL},
        "documents": {"type": "array", "min_items": 0, "max_items": 2000, "items": _DOCUMENT},
        "failing_tests": {"type": "array", "min_items": 0, "max_items": 2000, "items": _TEST},
    },
    ("controls", "documents", "failing_tests"),
)


def _unique(items: list[dict], label: str, key: str = "id") -> None:
    seen: set[str] = set()
    for item in items:
        value = item[key]
        if value in seen:
            raise DocumentError(f"duplicate {label} id {value}")
        seen.add(value)


def _values(doc: dict, ctx: RunContext) -> dict[str, int]:
    ok_statuses = ctx.options["document_ok_statuses"]
    return {
        "controls_total": len(doc["controls"]),
        "controls_complete": sum(1 for item in doc["controls"] if item["complete"]),
        "documents_total": len(doc["documents"]),
        "documents_ok": sum(1 for item in doc["documents"] if item["status"] in ok_statuses),
        "failing_tests": len(doc["failing_tests"]),
    }


def _names(tests: list[dict], count: int) -> list[str]:
    chosen: list[str] = []
    for item in sorted(tests, key=lambda row: row["id"]):
        trial = {"fact": "failing_tests", "value": count, "value_names": [*chosen, item["name"]]}
        if len(canonical_json(trial).encode()) > _EVIDENCE_MAX:
            break
        chosen.append(item["name"])
    return chosen


def _with_names(groups: tuple[Group, ...], doc: dict, count: int, withheld: bool = False) -> tuple[Group, ...]:
    names = _names(doc["failing_tests"], count)
    updated: list[Group] = []
    for group in groups:
        evidence = group.evidence
        if withheld:
            # Once a snapshot of this fact has been redacted, the names (free text) are not stored again;
            # the count and the fact stay.
            updated.append(group)
            continue
        if evidence is not None and evidence.get("fact") == "failing_tests":
            # value_names sorts after value, so the fact's quote stays a contiguous run.
            group = Group(group.ops, {**evidence, "value_names": names})
        updated.append(group)
    return tuple(updated)


def _checklist(doc: dict, known_rows: dict) -> tuple[Group, ...]:
    by_external: dict[str, dict] = {}
    for control in doc["controls"]:
        external_id = control["external_id"]
        if external_id in by_external:
            raise DocumentError(f"duplicate control external_id {external_id}")
        by_external[external_id] = control
    fetched_at = doc["fetched_at"]
    groups: list[Group] = []
    for item in known_rows.get("items") or ():
        ref = item["external_ref"] or ""
        if not ref.startswith("vanta:"):
            continue
        external_id = ref[len("vanta:") :]
        control = by_external.get(external_id)
        if control is None or item["last_verified_at"] >= fetched_at:
            continue
        state = item["state"]
        if control["complete"] and state not in {"done", "not_applicable"}:
            new_state = "done"
        elif not control["complete"] and state == "done":
            new_state = "in_progress"
        else:
            continue
        groups.append(
            Group(
                ops=(
                    {
                        "op": "upsert_checklist_item",
                        "base": item["version"],
                        "source": {"locator": f"control {external_id[:180]}"},
                        "row": {
                            "id": item["id"],
                            "state": new_state,
                            "state_basis": "observed",
                            "last_verified_at": fetched_at,
                        },
                    },
                ),
                evidence={"control": external_id, "complete": bool(control["complete"])},
            )
        )
    return tuple(groups)


def known(con, doc: dict, ctx: RunContext) -> dict:
    facts = known_facts(
        con,
        ctx.options["fact_prefix"],
        [name for name, _kind, _allowed in FACTS],
        ctx.agent,
    )
    items = con.execute(
        "SELECT id, external_ref, state, last_verified_at, version FROM checklist_items "
        "WHERE project_id = ? AND external_ref GLOB 'vanta:*' ORDER BY id",
        (ctx.options["project"],),
    )
    return {
        "facts": facts,
        "redacted": redacted_keys(con, ctx.options["fact_prefix"], [name for name, _kind, _allowed in FACTS]),
        "items": [
            {
                "id": row[0],
                "external_ref": row[1],
                "state": row[2],
                "last_verified_at": row[3],
                "version": row[4],
            }
            for row in items
        ],
    }


def _known(con, doc: dict, ctx: RunContext) -> dict:
    return known(con, doc, ctx)


def plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    ensure_not_future(doc["fetched_at"], ctx.now, "fetched_at is in the future")
    _unique(doc["controls"], "control")
    _unique(doc["documents"], "document")
    _unique(doc["failing_tests"], "failing_test")
    items = len(doc["controls"]) + len(doc["documents"]) + len(doc["failing_tests"])
    groups: tuple[Group, ...] = ()
    if doc["complete"]:
        values = _values(doc, ctx)
        groups = observe_facts(
            doc,
            known_rows,
            ctx,
            values,
            tuple(name for name, _kind, _allowed in FACTS),
            {name: kind for name, kind, _allowed in FACTS},
        )
        withheld = f"{ctx.options['fact_prefix']}.failing_tests" in (known_rows.get("redacted") or ())
        groups = _with_names(groups, doc, values["failing_tests"], withheld)
    groups = groups + _checklist(doc, known_rows)
    return Plan(groups, items, plan_note(doc))


def _plan(doc: dict, known_rows: dict, ctx: RunContext) -> Plan:
    return plan(doc, known_rows, ctx)


TYPE = CollectorType(
    name="vanta",
    format=FORMAT,
    ops=("add_source", "upsert_fact", "upsert_checklist_item", "record_collector_run"),
    options={
        "fact_prefix": Option("fact_prefix", required=True),
        "project": Option("workstream", required=True),
        "document_ok_statuses": Option("strings", required=True),
    },
    document_schema=SCHEMA,
    live=LIVE,
    known=_known,
    plan=_plan,
    source_kind="api",
)
