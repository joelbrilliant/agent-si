from __future__ import annotations

import json
import re
import sqlite3
import traceback
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from brain import advice, registry, schemas, scope, store, strict, truth, units, versions
from brain.config import ConfigError, load_config
from brain.ingress import has_lone_surrogate
from brain.migrate import latest_version
from brain.text import ELLIPSIS, single_line
from brain.validate import json_pointer, json_type

SCHEMA_KEYWORDS = frozenset({
    "type",
    "enum",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "minimum",
    "maximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "items",
    "properties",
    "required",
    "additionalProperties",
    "maxProperties",
    "default",
})
# Proposed actions aren't open work: the brief counts them apart.
OPEN_ACTION_STATUSES = (
    "open",
    "prepared",
    "scheduled",
    "sent",
    "waiting",
    "blocked",
)
LIST_BUDGET = 60_000
GET_BUDGET = 65_536
BRIEF_BYTES = 4_096

TOOLS: list[dict] = [
    {
        "name": "brief",
        "title": "Brief",
        "description": "Start here, once per session: workstreams, open work, what is due and waiting, project progress, stale facts, signals and anything blocking writes. under (a project id) keeps one folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "under": {
                    "type": "string",
                    "maxLength": 40
                }
            },
            "additionalProperties": False
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False
        }
    },
    {
        "name": "search",
        "title": "Search",
        "description": "Full-text search over every entity. Returns ids and snippets; get has the full row. Any word counts; more and rarer words rank first. Closed items need include_closed. Without entities, at most 3 sources rank among the first hits. under (a project id) keeps one folder.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200
                },
                "entities": {
                    "type": "array",
                    "items": {
                        "enum": ["record", "decision", "action", "checklist_item", "project", "person", "system", "fact", "thread", "source"]
                    },
                    "minItems": 1,
                    "maxItems": 10,
                    "uniqueItems": True
                },
                "include_closed": {
                    "type": "boolean",
                    "default": False
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "default": 10
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 200,
                    "default": 0
                },
                "under": {
                    "type": "string",
                    "maxLength": 40
                }
            },
            "additionalProperties": False,
            "required": ["query"]
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False
        }
    },
    {
        "name": "get",
        "title": "Get",
        "description": "One row by entity and id, with its sources and related rows; a source lists the rows citing it. A record body or source content comes in pages: pass field and offset.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "enum": ["action", "checklist_item", "decision", "record", "project", "person", "system", "fact", "claim", "thread", "thread_link", "meeting", "signal", "source"]
                },
                "id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 120
                },
                "field": {
                    "enum": ["body", "content"]
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2000000,
                    "default": 0
                },
                "include_history": {
                    "type": "boolean",
                    "default": False
                }
            },
            "additionalProperties": False,
            "required": ["entity", "id"]
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False
        }
    },
    {
        "name": "fact_get",
        "title": "Get a fact",
        "description": "A typed fact with its source, as_of date and staleness. Pass a key, alias words, or a prefix ending in .* for up to 25 facts. A disputed fact has no value; claims lists each side with its source. An unknown key is an error.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "minLength": 3,
                    "maxLength": 200,
                    "pattern": "^([a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)*\\.([a-z][a-z0-9_]*|\\*)|[^.*]+)$"
                },
                "scope": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 40
                },
                "claims": {
                    "enum": ["auto", "all"],
                    "default": "auto"
                },
                "include_retired": {
                    "type": "boolean",
                    "default": False
                }
            },
            "additionalProperties": False,
            "required": ["key"]
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string"
                },
                "resolved": {
                    "type": "string"
                },
                "facts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string"
                            },
                            "value": {
                                "type": ["string", "integer", "number", "boolean"]
                            },
                            "value_type": {
                                "enum": ["text", "int", "number", "date", "bool", "url", "enum"]
                            },
                            "unit": {
                                "type": "string"
                            },
                            "status": {
                                "enum": ["current", "disputed", "retired"]
                            },
                            "basis": {
                                "enum": ["observed", "reported", "inferred"]
                            },
                            "as_of": {
                                "type": "string",
                                "format": "date"
                            },
                            "last_verified_at": {
                                "type": "string",
                                "format": "date-time"
                            },
                            "age_days": {
                                "type": "integer"
                            },
                            "stale_after_days": {
                                "type": ["integer", "null"]
                            },
                            "stale": {
                                "type": "boolean"
                            },
                            "version": {
                                "type": "integer"
                            },
                            "source": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string"
                                    },
                                    "kind": {
                                        "type": "string"
                                    },
                                    "title": {
                                        "type": "string"
                                    },
                                    "uri": {
                                        "type": "string"
                                    },
                                    "source_date": {
                                        "type": "string"
                                    },
                                    "locator": {
                                        "type": "string"
                                    }
                                },
                                "additionalProperties": False,
                                "required": ["id", "kind", "title", "uri", "source_date", "locator"]
                            },
                            "claims": {
                                "type": "object"
                            }
                        },
                        "additionalProperties": False,
                        "required": ["key", "value_type", "unit", "status"]
                    }
                },
                "total": {
                    "type": "integer"
                },
                "truncated": {
                    "type": "boolean"
                },
                "unset": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    }
                }
            },
            "additionalProperties": False,
            "required": ["query", "facts", "total", "truncated", "unset"]
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False
        }
    },
    {
        "name": "records_query",
        "title": "Query records",
        "description": "Typed query over one entity. filters maps a field to a value or to an operator object using eq, in, lt, lte, gt, gte, is_null or contains, or under (a project and every project in it). fields picks the columns. No SQL; an unknown field's error lists the allowed ones. cites takes a source id, as primary or link. For actions, open: true matches every open status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {
                    "enum": ["action", "checklist_item", "decision", "record", "project", "person", "system", "fact", "thread", "thread_link", "meeting", "signal", "source"]
                },
                "filters": {
                    "type": "object",
                    "maxProperties": 20
                },
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "string"
                    },
                    "minItems": 1,
                    "maxItems": 40,
                    "uniqueItems": True
                },
                "order_by": {
                    "type": "string"
                },
                "desc": {
                    "type": "boolean",
                    "default": False
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 25
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "default": 0
                },
                "format": {
                    "enum": ["objects", "rows"],
                    "default": "objects"
                }
            },
            "additionalProperties": False,
            "required": ["entity"]
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False
        }
    },
    {
        "name": "signals",
        "title": "Signals",
        "description": "What needs attention. reply, new_message and owes_reply are worked out per linked thread from message times, never by matching people. Stored open signals are included.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "kinds": {
                    "type": "array",
                    "items": {
                        "enum": ["reply", "new_message", "owes_reply", "overdue", "stale", "drift", "other"]
                    },
                    "minItems": 1,
                    "maxItems": 7,
                    "uniqueItems": True
                },
                "project_id": {
                    "type": "string",
                    "minLength": 2,
                    "maxLength": 40
                },
                "subject": {
                    "type": "object",
                    "properties": {
                        "entity": {
                            "enum": ["action", "checklist_item", "decision", "fact", "project", "record"]
                        },
                        "id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 120
                        }
                    },
                    "additionalProperties": False,
                    "required": ["entity", "id"]
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 50
                }
            },
            "additionalProperties": False
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": False
        }
    }
]

TOOL_NAMES: tuple[str, ...] = tuple(tool["name"] for tool in TOOLS)
_TOOLS = {tool["name"]: tool for tool in TOOLS}


class UnknownTool(Exception):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


class ToolError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        allowed: list[str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.path = path
        self.allowed = allowed
        super().__init__(message)

    def payload(self) -> dict:
        error = {"code": self.code, "message": self.message, "path": self.path}
        if self.allowed is not None:
            error["allowed"] = self.allowed
        return {"error": error}


@dataclass(frozen=True)
class ToolResult:
    text: str
    structured: dict | None
    is_error: bool
    code: str | None


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _matches_type(value, expected: str) -> bool:
    actual = json_type(value)
    if expected == "number":
        return actual in {"integer", "number"}
    if expected == "integer":
        return actual == "integer"
    return actual == expected


def _real_date(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.date().isoformat() == value


def _real_stamp(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def check_schema(schema: dict, value, path: str = "") -> list[dict]:
    errors: list[dict] = []

    def add(pointer: str, message: str, allowed: list | None = None) -> None:
        if len(errors) >= 50:
            return
        item = {"path": pointer, "message": message}
        if allowed is not None:
            item["allowed"] = list(allowed)
        errors.append(item)

    expected = schema.get("type")
    if expected is not None:
        allowed_types = expected if isinstance(expected, list) else [expected]
        if not any(_matches_type(value, item) for item in allowed_types):
            add(path, "expected " + " or ".join(allowed_types))
            return errors

    if "enum" in schema and value not in schema["enum"]:
        add(path, advice.enum_error(schema["enum"]), list(schema["enum"]))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            add(path, advice.size_error("characters", "minimum", schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            add(path, advice.size_error("characters", "maximum", schema["maxLength"]))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            add(path, advice.pattern_error(schema["pattern"]))
        fmt = schema.get("format")
        if fmt == "date" and not _real_date(value):
            add(path, advice.date_error())
        elif fmt == "date-time" and not _real_stamp(value):
            add(path, advice.utc_error())

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            add(path, advice.bound_error("minimum", schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            add(path, advice.bound_error("maximum", schema["maximum"]))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            add(path, advice.size_error("items", "minimum", schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            add(path, advice.size_error("items", "maximum", schema["maxItems"]))
        if schema.get("uniqueItems") is True:
            seen = []
            for item in value:
                if item in seen:
                    add(path, "has duplicate items")
                    break
                seen.append(item)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(check_schema(item_schema, item, json_pointer(path, index)))
                if len(errors) >= 50:
                    return errors[:50]

    if isinstance(value, dict):
        if "maxProperties" in schema and len(value) > schema["maxProperties"]:
            add(path, advice.size_error("properties", "maximum", schema["maxProperties"]))
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else None
        for name in schema.get("required") or []:
            if name not in value:
                child = (properties or {}).get(name) or {}
                add(json_pointer(path, name), advice.required_error(child.get("enum")), child.get("enum"))
        if schema.get("additionalProperties") is False:
            allowed_names = set(properties or {})
            for name in value:
                if name not in allowed_names:
                    add(json_pointer(path, name), advice.unknown_property_error(name, properties or ()), list(properties or ()))
        if properties is not None:
            for name, child in value.items():
                if name in properties:
                    errors.extend(check_schema(properties[name], child, json_pointer(path, name)))
                    if len(errors) >= 50:
                        return errors[:50]
    return errors[:50]


def _fail(code: str, message: str, path: str = "", allowed: list[str] | None = None) -> ToolResult:
    error = ToolError(code, message, path=path, allowed=allowed)
    return ToolResult(_compact(error.payload()), None, True, code)


def _invalid(errors: list[dict]) -> ToolResult:
    first = errors[0]
    message = "; ".join(f"{item['path']} {item['message']}".strip() for item in errors[:5])
    return _fail("invalid_arguments", message, first["path"], first.get("allowed"))


def _with_defaults(schema: dict, value: dict) -> dict:
    filled = dict(value)
    properties = schema.get("properties") or {}
    for name, child in properties.items():
        if name not in filled and isinstance(child, dict) and "default" in child:
            filled[name] = child["default"]
    return filled


def _query(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    return con.execute(sql, params).fetchall()


def _ok(structured: dict) -> ToolResult:
    return ToolResult(_compact(structured), structured, False, None)


def _fits(structured: dict, budget: int | None = None) -> bool:
    if budget is None:
        budget = LIST_BUDGET
    return len(_compact(structured).encode()) <= budget


_FACT_SELECT = (
    "SELECT f.key, f.value_type, f.value_text, f.value_int, f.value_real, f.value_date, "
    "f.status, f.basis, f.as_of, f.last_verified_at, f.version, f.locator, "
    "k.unit, k.stale_after_days, "
    "max(0, CAST(julianday(?) - julianday(f.last_verified_at) AS INTEGER)) AS age_days, "
    "s.id AS source_id, s.kind AS source_kind, s.title AS source_title, "
    "s.uri AS source_uri, s.source_date AS source_date "
    "FROM facts AS f "
    "JOIN fact_keys AS k ON k.key = f.key "
    "JOIN sources AS s ON s.id = f.source_id "
)


def _fact_value(row) -> str | int | float | bool:
    kind = row["value_type"]
    if kind == "int":
        return int(row["value_int"])
    if kind == "bool":
        return bool(row["value_int"])
    if kind == "number":
        return row["value_real"]
    if kind == "date":
        return row["value_date"]
    return row["value_text"]


def _fact_item(row) -> dict:
    age = int(row["age_days"])
    stale_after = row["stale_after_days"]
    if stale_after is not None:
        stale_after = int(stale_after)
    stale = row["status"] != "retired" and stale_after is not None and age > stale_after
    return {
        "key": row["key"],
        "value": _fact_value(row),
        "value_type": row["value_type"],
        "unit": row["unit"],
        "status": row["status"],
        "basis": row["basis"],
        "as_of": row["as_of"],
        "last_verified_at": row["last_verified_at"],
        "age_days": age,
        "stale_after_days": stale_after,
        "stale": stale,
        "version": int(row["version"]),
        "source": {
            "id": row["source_id"],
            "kind": row["source_kind"],
            "title": row["source_title"],
            "uri": row["source_uri"],
            "source_date": row["source_date"],
            "locator": row["locator"],
        },
    }


def _fact_result(
    query: str, facts: list[dict], total: int, unset: list[str], resolved: str | None = None
) -> dict:
    facts = list(facts[:25])
    unset = list(unset[:25])
    cut = len(facts) < total
    while True:
        result = {"query": query}
        if resolved is not None:
            result["resolved"] = resolved
        result.update(
            {
                "facts": facts,
                "total": total,
                "truncated": cut,
                "unset": unset,
            }
        )
        if _fits(result) or (not facts and not unset):
            return result
        cut = True
        if unset:
            unset.pop()
        else:
            facts.pop()


_HINT_SQL = (
    "SELECT ref_id FROM search_index WHERE search_index MATCH ? AND entity = 'fact' "
    "ORDER BY bm25(search_index, 0, 0, 0, 4.0, 1.0), ref_id LIMIT 12"
)


def _key_hints(con: sqlite3.Connection, text: str) -> list[str]:
    """Up to 3 fact keys the search index finds for the text's alias words."""
    parts = truth.words(text).split()[:12]
    if not parts:
        return []
    match = " OR ".join(f'"{part}"' for part in parts)
    try:
        found = [row[0] for row in _query(con, _HINT_SQL, (match,))]
    except sqlite3.Error:
        return []
    hints: list[str] = []
    for ref_id in found:
        if ref_id not in hints:
            hints.append(ref_id)
        if len(hints) == 3:
            break
    return hints


def _unknown_key(con: sqlite3.Connection, text: str) -> ToolError:
    hints = _key_hints(con, text)
    closest = f"Closest keys: {', '.join(hints)}. " if hints else ""
    if "." in text:
        prefix = text.split(".", 1)[0]
        message = (
            f"unknown fact key {text}. {closest}Use search with entities [\"fact\"], "
            f"or fact_get with a prefix such as {prefix}.*"
        )
    else:
        message = f"unknown fact key {text}. {closest}Use search with entities [\"fact\"]."
    return ToolError("unknown_key", message)


def _shown_value(value_type: str, unit: str, claim: dict, limit: int) -> str:
    """A value as a dispute line shows it: rendered, cut to limit bytes, then the unit."""
    text = _cut_bytes(units.render(value_type, units.typed(value_type, claim)), limit)
    if value_type in ("int", "number") and unit:
        text += f" {unit}"
    return text


def _registry(con: sqlite3.Connection, key: str):
    return _query(con, "SELECT value_type, unit FROM fact_keys WHERE key = ?", (key,))[0]


def _planned_tail(con: sqlite3.Connection, key: str, state, label: str | None = None) -> str:
    """` Planned: <scope or now>: <value>, ...` for a no_value message, or ''."""
    registry = _registry(con, key)
    entries: list[tuple[str, str]] = []
    for claim in state.planned:
        own = state.label_for(claim["scope"])
        entry = (own, _shown_value(registry["value_type"], registry["unit"], claim, 40))
        if (label is None or own == label) and entry not in entries:
            entries.append(entry)
    if not entries:
        return ""
    entries = sorted(entries, key=lambda item: item[0])[:8]
    return " Planned: " + ", ".join(f"{own}: {value}" for own, value in entries) + "."


def _no_value_message(con: sqlite3.Connection, key: str, state) -> str:
    scoped = list(state.scopes.values())
    if not scoped:
        tail = _planned_tail(con, key, state)
        return f"fact key {key} is registered but has no value" + ("." + tail if tail else "")
    registry = _registry(con, key)
    parts = []
    for bucket in scoped[:8]:
        if bucket.disputed:
            parts.append(f"{bucket.label}: disputed")
        else:
            shown = _shown_value(registry["value_type"], registry["unit"], bucket.claims[0], 40)
            # Only legacy imports make unchecked scoped claims.
            unchecked = "" if any(claim["checked"] for claim in bucket.claims) else " (unchecked)"
            parts.append(f"{bucket.label}: {shown}{unchecked}")
    listed = ", ".join(parts)
    if len(scoped) > 8:
        listed += f", +{len(scoped) - 8} more"
    unknown = "Current version unknown. " if state.has_version_key and state.current_version is None else ""
    return (
        f"fact key {key} has no value now. {unknown}Values by version: {listed}. Pass scope to read one."
        + _planned_tail(con, key, state)
    )


def _has_successor(con: sqlite3.Connection, source_id: str) -> bool:
    return bool(_query(con, "SELECT 1 FROM sources WHERE supersedes_id = ? LIMIT 1", (source_id,)))


def _confidence(bucket, stale: bool, truth_config) -> str:
    if bucket.disputed:
        return "disputed"
    checked = [claim for claim in bucket.claims if claim["checked"]]
    if not stale and any(
        claim["basis"] == "observed" or truth.rank(claim, truth_config) in (0, 1) for claim in checked
    ):
        return "high"
    if not stale and any(claim["basis"] == "reported" for claim in checked):
        return "medium"
    return "low"


def _claim_source(claim: dict) -> dict:
    return {"id": claim["source_id"], "kind": claim["source_kind"], "date": claim["source_date"]}


def _value_entry(claim: dict, bucket, counts: dict, best_id: str) -> dict:
    entry = {
        "id": claim["id"],
        "value": units.typed(claim["value_type"], claim),
        "raw": claim["value_raw"],
    }
    if claim["scope"]:
        entry["scope"] = claim["scope"]
    entry["basis"] = claim["basis"]
    entry["checked"] = bool(claim["checked"])
    entry["agree"] = counts[claim["canonical"]] - 1
    if bucket.disputed and claim["id"] == best_id:
        entry["suggested"] = True
    entry["source"] = _claim_source(claim)
    if claim["quote"]:
        entry["quote"] = _cut(claim["quote"], 200)
    return entry


def _other_scopes_with_sources(state, own: str) -> tuple[dict, dict]:
    """Each other scope's value, and the source id behind each value that isn't disputed, so an agent answering
    about an older version can cite it without a second call."""
    others, sources = {}, {}
    for label, bucket in state.scopes.items():
        if label == own:
            continue
        if len(others) == 8:
            break
        if bucket.disputed:
            others[label] = "disputed"
        else:
            best = bucket.claims[0]
            value = units.typed(best["value_type"], best)
            if not any(claim["checked"] for claim in bucket.claims):
                value = {"value": value, "checked": False}
            others[label] = value
            if best.get("source_id"):
                sources[label] = best["source_id"]
    return others, sources


def _claims_object(con, state, bucket, mode: str, stale: bool, source_id: str, truth_config) -> dict:
    """The claims object of one fact item, keys in a fixed order."""
    out: dict = {"scope": bucket.label, "confidence": _confidence(bucket, stale, truth_config)}
    if state.has_version_key:
        out["current_version"] = state.current_version
    if _has_successor(con, source_id):
        out["source_current"] = False
    if mode == "all" or bucket.disputed or not any(claim["checked"] for claim in bucket.claims):
        counts: dict[str, int] = {}
        for claim in bucket.claims:
            counts[claim["canonical"]] = counts.get(claim["canonical"], 0) + 1
        best_id = bucket.claims[0]["id"] if bucket.claims else ""
        if mode == "all":
            chosen, cap = bucket.claims, 40
        else:
            chosen, cap = [best for _value, best, _count in bucket.values()], 8
        out["values"] = [_value_entry(claim, bucket, counts, best_id) for claim in chosen[:cap]]
        if len(chosen) > cap:
            out["values_more"] = len(chosen) - cap
    others, sources = _other_scopes_with_sources(state, bucket.label)
    if others:
        out["other_scopes"] = others
    if sources:
        out["other_scopes_sources"] = sources
    wanted = {"", state.current_version} if bucket.label == "now" else {bucket.label}
    planned = []
    for claim in [item for item in state.planned if item["scope"] in wanted][:8]:
        entry = {
            "id": claim["id"],
            "value": units.typed(claim["value_type"], claim),
            "raw": claim["value_raw"],
        }
        if claim["scope"]:
            entry["scope"] = claim["scope"]
        if not claim["checked"]:
            entry["checked"] = False
        entry["source"] = _claim_source(claim)
        if claim["quote"]:
            entry["quote"] = _cut(claim["quote"], 200)
        planned.append(entry)
    if planned:
        out["planned"] = planned
    return out


def _scope_label(state, scope: str | None) -> str:
    return "now" if scope is None else state.label_for(scope)


def _last_in_force_status(con, key: str) -> str | None:
    """The status of a fact's newest history row that isn't retired."""
    rows = _query(
        con,
        "SELECT status FROM fact_history WHERE key = ? AND status <> 'retired' ORDER BY version DESC LIMIT 1",
        (key,),
    )
    return rows[0][0] if rows else None


def _now_item(con, key: str, state, row, mode: str, truth_config) -> dict:
    item = _fact_item(row)
    if row["status"] == "retired":
        if _last_in_force_status(con, key) == "disputed":
            # A dispute that ended by its bucket emptying leaves the ranking's pick in the row.
            for name in ("value", "basis", "as_of", "source"):
                item.pop(name)
        claims = {"scope": "now", "confidence": "low"}
        if state.has_version_key:
            claims["current_version"] = state.current_version
        others, sources = _other_scopes_with_sources(state, "now")
        if others:
            claims["other_scopes"] = others
        if sources:
            claims["other_scopes_sources"] = sources
        item["claims"] = claims
        return item
    if row["status"] == "disputed":
        for name in ("value", "basis", "as_of", "source"):
            item.pop(name)
    item["claims"] = _claims_object(con, state, state.now, mode, item["stale"], row["source_id"], truth_config)
    return item


def _scope_item(con, key: str, state, bucket, mode: str, truth_config) -> dict:
    registry = _registry(con, key)
    best = bucket.claims[0]
    item: dict = {"key": key}
    if not bucket.disputed:
        item["value"] = units.typed(best["value_type"], best)
    item["value_type"] = registry["value_type"]
    item["unit"] = registry["unit"]
    item["status"] = "disputed" if bucket.disputed else "current"
    if not bucket.disputed:
        item["basis"] = best["basis"]
        item["as_of"] = best["as_of"]
        cited = _cited_source(con, best["source_id"], best["locator"])
        item["source"] = {name: cited[name] for name in ("id", "kind", "title", "uri", "source_date", "locator")}
    item["claims"] = _claims_object(con, state, bucket, mode, False, best["source_id"], truth_config)
    return item


def _fact_get(con: sqlite3.Connection, arguments: dict, now: str, truth_config=None) -> ToolResult:
    text = arguments["key"]
    include_retired = arguments["include_retired"]
    scope = arguments.get("scope")
    mode = arguments["claims"]
    if text.endswith(".*"):
        return _fact_prefix(con, text, include_retired, now, scope, mode, truth_config)
    found = truth.resolve(con, text)
    if not found:
        raise _unknown_key(con, text)
    if len(found) > 1:
        raise ToolError(
            "ambiguous_key",
            f"{text} matches more than one fact key: {', '.join(found)}. Pass one of them as key.",
            allowed=found,
        )
    key = found[0]
    state = truth.state(con, key, truth_config)
    label = _scope_label(state, scope)
    if label == "now":
        rows = _query(con, _FACT_SELECT + "WHERE f.key = ?", (now, key))
        if not rows:
            raise ToolError("no_value", _no_value_message(con, key, state))
        item = _now_item(con, key, state, rows[0], mode, truth_config)
    else:
        bucket = state.scopes.get(label)
        if bucket is None:
            tail = _planned_tail(con, key, state, label)
            message = f"fact key {key} has no value for scope {scope}"
            raise ToolError("no_value", message + ("." + tail if tail else ""))
        item = _scope_item(con, key, state, bucket, mode, truth_config)
    return _ok(_fact_result(text, [_with_signals(con, item)], 1, [], resolved=None if text == key else key))


def _fact_prefix(
    con: sqlite3.Connection,
    key: str,
    include_retired: bool,
    now: str,
    scope: str | None = None,
    mode: str = "auto",
    truth_config=None,
) -> ToolResult:
    prefix = key[:-1]
    registered = [
        row[0]
        for row in _query(
            con,
            "SELECT key FROM fact_keys WHERE substr(key, 1, length(?)) = ? ORDER BY key",
            (prefix, prefix),
        )
    ]
    if not registered:
        raise ToolError("unknown_key", f"no fact keys start with {prefix}")
    shown: list[tuple[str, object, object]] = []
    unset: list[str] = []
    for name in registered:
        # Without a scope every key answers for now, so only the items shown need their buckets.
        state = None if scope is None else truth.state(con, name, truth_config)
        label = "now" if state is None else _scope_label(state, scope)
        if label == "now":
            rows = _query(con, _FACT_SELECT + "WHERE f.key = ?", (now, name))
            if not rows:
                unset.append(name)
                continue
            if rows[0]["status"] == "retired" and not include_retired:
                continue
            shown.append((name, state, rows[0]))
        else:
            bucket = state.scopes.get(label)
            if bucket is None:
                unset.append(name)
                continue
            shown.append((name, state, bucket))
    items = []
    for name, state, found in shown[:25]:
        state = state or truth.state(con, name, truth_config)
        if isinstance(found, truth.Bucket):
            items.append(_with_signals(con, _scope_item(con, name, state, found, mode, truth_config)))
        else:
            items.append(_with_signals(con, _now_item(con, name, state, found, mode, truth_config)))
    return _ok(_fact_result(key, items, len(shown), unset))


def _with_signals(con: sqlite3.Connection, item: dict) -> dict:
    """A fact's open signals under claims.signals, as get shows an action's. An agent asked about a fact reads
    fact_get and nothing else, so a signal raised on the fact (an importer's discrepancy note, say) has to arrive
    with it. They sit in claims, whose schema is an open object, because tools/list is at its 6 KiB cap. The
    dispute's own conflict signal is left out, since the answer already says disputed and lists each claim. With
    no other signal the field is left out, so every other fact_get answer keeps its size."""
    rows = _mapped(
        con,
        "SELECT id, kind, thread_id, detail FROM signals WHERE status = 'open' AND subject_table = 'facts' "
        "AND subject_id = ? AND kind <> 'conflict' ORDER BY id",
        (item["key"],),
        ("id", "kind", "thread_id", "detail"),
    )
    if rows:
        item["claims"]["signals"] = _listed(rows)
    return item


@dataclass(frozen=True)
class _Field:
    name: str
    kind: str
    expr: str
    enum: tuple[str, ...] = ()
    clocks: int = 0
    people: tuple[str, str, str] | None = None  # list table, list key column, role
    tree: bool = False  # folders (0007): a project id, so "under" takes that folder and every folder inside it
    links: str | None = None  # links (0008): the subject table whose primary source and links "cites" matches


_TREE_FIELDS = frozenset({"project_id", "parent_id"})
FOLDER_DEPTH = 8  # matches the applier's FOLDER_DEPTH_MAX
# A folder and every folder inside it, as one id list (folders, 0007).
BRANCH_SQL = (
    "WITH RECURSIVE branch(id, depth) AS (SELECT id, 0 FROM projects WHERE id = ? "
    "UNION ALL SELECT p.id, b.depth + 1 FROM projects AS p JOIN branch AS b ON p.parent_id = b.id "
    f"WHERE b.depth < {FOLDER_DEPTH}) SELECT id FROM branch"
)
_AGE_SQL = "max(0, CAST(julianday(?) - julianday(t.last_verified_at) AS INTEGER))"
_VALUE_SQL = (
    "CASE t.value_type WHEN 'int' THEN t.value_int WHEN 'bool' THEN t.value_int "
    "WHEN 'number' THEN t.value_real WHEN 'date' THEN t.value_date ELSE t.value_text END"
)
# A disputed fact, or a retired one whose last in-force status was disputed, shows no value.
_FACT_VALUE_SQL = (
    "CASE WHEN t.status = 'disputed' OR (t.status = 'retired' AND (SELECT h.status FROM fact_history AS h "
    "WHERE h.key = t.key AND h.status <> 'retired' ORDER BY h.version DESC LIMIT 1) = 'disputed') "
    f"THEN 'disputed' ELSE {_VALUE_SQL} END"
)
_COMMON = (
    _Field("source_id", "id", "t.source_id"),
    _Field("locator", "text", "t.locator"),
    _Field("version", "int", "t.version"),
    _Field("created_at", "utc", "t.created_at"),
    _Field("updated_at", "utc", "t.updated_at"),
    _Field("updated_by", "text", "t.updated_by"),
)
_COMPARE = {"eq": "=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}
_LOWER = {"gt", "gte"}
_UPPER = {"lt", "lte"}
_FIELD_OPS = {
    "id": {"eq", "in", "is_null"},
    "text": {"eq", "in", "contains", "is_null"},
    "enum": {"eq", "in", "is_null"},
    "int": {"eq", "in", "lt", "lte", "gt", "gte", "is_null"},
    "date": {"eq", "in", "lt", "lte", "gt", "gte", "is_null"},
    "utc": {"eq", "in", "lt", "lte", "gt", "gte", "is_null"},
    "bool": {"eq"},
    "json": set(),
    "value": set(),
    "cites": {"eq", "in"},
}
# Kinds a query can't order by: a list or a typed value.
_UNORDERABLE = frozenset({"json", "value", "cites"})


def _field(name: str, kind: str, expr: str | None = None, enum: tuple[str, ...] = (), clocks: int = 0) -> _Field:
    return _Field(name, kind, expr or f"t.{name}", enum, clocks)


def _person_fields(list_table: str, list_key: str) -> tuple[_Field, ...]:
    """The scalar person columns, which also match any listed person, and the two list fields."""
    fields = []
    for role, scalar, _field_name in schemas.PEOPLE_ROLES:
        fields.append(_Field(scalar, "id", f"t.{scalar}", people=(list_table, list_key, role)))
    for role, _scalar, field_name in schemas.PEOPLE_ROLES:
        fields.append(
            _Field(
                field_name,
                "json",
                f"(SELECT json_group_array(person_id) FROM (SELECT person_id FROM {list_table} "
                f"WHERE {list_key} = t.id AND role = '{role}' ORDER BY position))",
                people=(list_table, list_key, role),
            )
        )
    return tuple(fields)


def _cites_field(table: str) -> _Field:
    """links (0008): every source a row cites, its primary first, then its links' sources by id. A filter takes
    one source id or a list, and matches rows citing it either way."""
    return _Field(
        "cites",
        "cites",
        "(SELECT json_group_array(sid) FROM (SELECT t.source_id AS sid, 0 AS n UNION ALL "
        f"SELECT DISTINCT l.source_id, 1 FROM links AS l WHERE l.subject_table = '{table}' AND l.subject_id = t.id "
        "AND l.source_id <> t.source_id ORDER BY n, sid))",
        links=table,
    )


def _unchecked_field(entity: str, key: str) -> _Field:
    return _Field(
        "unchecked",
        "bool",
        f"EXISTS (SELECT 1 FROM unchecked_text u WHERE u.entity = '{entity}' AND u.ref_id = t.{key})",
    )


def _entity(from_sql: str, key: str, fields: tuple[_Field, ...], defaults: tuple[str, ...], *, common: bool = True):
    fields = tuple(replace(f, tree=True) if f.name in _TREE_FIELDS and f.kind == "id" else f for f in fields)
    body = fields + (_COMMON if common else ())
    return {"from": from_sql, "key": key, "fields": body, "defaults": defaults}


_ENTITIES = {
    "action": _entity(
        "actions AS t",
        "id",
        (
            _field("id", "id"),
            _field("title", "text"),
            _field("detail", "text"),
            _field("next_step", "text"),
            _field("status", "enum", enum=schemas.ACTION_STATUSES),
            _field("status_basis", "enum", enum=schemas.BASIS),
            _field("project_id", "id"),
            *_person_fields("action_people", "action_id"),
            _field("due_date", "date"),
            _field("due_kind", "enum", enum=schemas.DUE_KINDS),
            _field("asked_on", "date"),
            _field("blocked_by", "id"),
            _field("record_id", "id"),
            _field("last_verified_at", "utc"),
            _field("closed_at", "utc"),
            _field("close_reason", "text"),
            _unchecked_field("action", "id"),
            _Field("open", "bool", "t.status IN (" + ", ".join(f"'{s}'" for s in OPEN_ACTION_STATUSES) + ")"),
            _cites_field("actions"),
        ),
        (
            "id", "title", "status", "project_id", "owner_person_id", "waiting_on_person_id",
            "owner_person_ids", "waiting_on_person_ids", "due_date", "version",
        ),
    ),
    "checklist_item": _entity(
        "checklist_items AS t",
        "id",
        (
            _field("id", "id"),
            _field("project_id", "id"),
            _field("section", "text"),
            _field("title", "text"),
            _field("detail", "text"),
            _field("state", "enum", enum=schemas.CHECKLIST_STATES),
            _field("state_basis", "enum", enum=schemas.BASIS),
            *_person_fields("checklist_item_people", "item_id"),
            _field("land_by", "date"),
            _field("action_id", "id"),
            _field("external_ref", "text"),
            _field("sort_order", "int"),
            _field("last_verified_at", "utc"),
            _unchecked_field("checklist_item", "id"),
            _cites_field("checklist_items"),
        ),
        (
            "id", "project_id", "section", "title", "state", "owner_person_id", "waiting_on_person_id",
            "owner_person_ids", "waiting_on_person_ids", "land_by", "version",
        ),
    ),
    "decision": _entity(
        "decisions AS t",
        "id",
        (
            _field("id", "id"),
            _field("title", "text"),
            _field("detail", "text"),
            _field("quote", "text"),
            _field("decided_on", "date"),
            _field("decided_by_person_id", "id"),
            _field("status", "enum", enum=schemas.DECISION_STATUSES),
            _field("governs_from", "date"),
            _field("project_id", "id"),
            _field("supersedes_id", "id"),
            _cites_field("decisions"),
        ),
        ("id", "title", "status", "decided_on", "decided_by_person_id", "project_id", "version"),
    ),
    "record": _entity(
        "records AS t",
        "id",
        (
            _field("id", "id"),
            _field("kind", "enum", enum=schemas.RECORD_KINDS),
            _field("status", "enum", enum=schemas.RECORD_STATUSES),
            _field("title", "text"),
            _field("summary", "text"),
            _field("project_id", "id"),
            _unchecked_field("record", "id"),
            _cites_field("records"),
        ),
        ("id", "kind", "title", "summary", "status", "project_id", "version", "updated_at"),
    ),
    "project": _entity(
        "projects AS t",
        "id",
        (
            replace(_field("id", "id"), tree=True),
            _field("parent_id", "id"),
            _field("kind", "enum", enum=schemas.PROJECT_KINDS),
            _field("title", "text"),
            _field("description", "text"),
            _field("goal", "text"),
            _field("goal_date", "date"),
            _field("status", "enum", enum=schemas.PROJECT_STATUSES),
            _field("sort_order", "int"),
            _unchecked_field("project", "id"),
        ),
        ("id", "kind", "title", "status", "parent_id", "goal_date", "version"),
    ),
    "person": _entity(
        "people AS t",
        "id",
        (
            _field("id", "id"),
            _field("display_name", "text"),
            _field("org", "text"),
            _field("role", "text"),
            _field("aliases", "json"),
        ),
        ("id", "display_name", "org", "role", "version"),
    ),
    "system": _entity(
        "systems AS t",
        "id",
        (
            _field("id", "id"),
            _field("name", "text"),
            _field("kind", "enum", enum=schemas.SYSTEM_KINDS),
            _field("vendor", "text"),
            _field("owner_person_id", "id"),
        ),
        ("id", "name", "kind", "vendor", "owner_person_id", "version"),
    ),
    "fact": _entity(
        "facts AS t JOIN fact_keys AS k ON k.key = t.key",
        "key",
        (
            _field("key", "id"),
            _field("value", "value", _FACT_VALUE_SQL),
            _field("value_type", "enum", enum=schemas.VALUE_TYPES),
            _field("status", "enum", enum=schemas.FACT_STATUSES),
            _field("basis", "enum", enum=schemas.BASIS),
            _field("as_of", "date"),
            _field("last_verified_at", "utc"),
            _field("unit", "text", "k.unit"),
            _field("stale_after_days", "int", "k.stale_after_days"),
            _field("description", "text", "k.description"),
            _field("age_days", "int", _AGE_SQL, clocks=1),
            _field(
                "stale",
                "bool",
                f"(t.status <> 'retired' AND k.stale_after_days IS NOT NULL AND {_AGE_SQL} > k.stale_after_days)",
                clocks=1,
            ),
        ),
        ("key", "description", "value", "value_type", "unit", "status", "as_of", "stale", "version"),
    ),
    "thread": _entity(
        "threads AS t",
        "id",
        (
            _field("id", "id"),
            _field("channel", "enum", enum=schemas.THREAD_CHANNELS),
            _field("account", "text"),
            _field("external_id", "text"),
            _field("subject", "text"),
            _field("last_inbound_at", "utc"),
            _field("last_outbound_at", "utc"),
            _field("outbound_seen", "bool"),
        ),
        ("id", "channel", "account", "subject", "last_inbound_at", "last_outbound_at", "version"),
    ),
    "thread_link": _entity(
        "thread_links AS t LEFT JOIN thread_link_state AS s ON s.link_id = t.id",
        "id",
        (
            _field("id", "id"),
            _field("action_id", "id"),
            _field("checklist_item_id", "id"),
            _field("thread_id", "id"),
            _field("role", "enum", enum=schemas.LINK_ROLES),
            _field("since", "utc"),
            _field("seen_through", "utc"),
            _field("status", "enum", enum=schemas.LINK_STATUSES),
            _field("reply_state", "enum", "s.reply_state", ("replied", "waiting", "unknown")),
            _field("new_message", "bool", "coalesce(s.new_message, 0)"),
        ),
        ("id", "action_id", "checklist_item_id", "thread_id", "role", "reply_state", "new_message", "version"),
    ),
    "meeting": _entity(
        "meetings AS t",
        "id",
        (
            _field("id", "id"),
            _field("thread_id", "id"),
            _field("project_id", "id"),
            _field("transcript_source_id", "id"),
            _field("title", "text"),
            _field("starts_at", "utc"),
            _field("ends_at", "utc"),
            _field("all_day", "bool"),
            _field("status", "enum", enum=schemas.MEETING_STATUSES),
            _field("attendee_count", "int"),
        ),
        ("id", "thread_id", "title", "starts_at", "ends_at", "status", "project_id", "version"),
    ),
    "signal": _entity(
        "signals AS t",
        "id",
        (
            _field("id", "id"),
            _field("kind", "enum", enum=schemas.SIGNAL_KINDS),
            _field("subject_table", "enum", enum=schemas.SIGNAL_SUBJECTS),
            _field("subject_id", "text"),
            _field("thread_id", "id"),
            _field("detail", "text"),
            _field("status", "enum", enum=schemas.SIGNAL_STATUSES),
            _field("basis", "enum", enum=schemas.SIGNAL_BASIS),
            _field("resolved_at", "utc"),
        ),
        ("id", "kind", "subject_table", "subject_id", "thread_id", "status", "version"),
    ),
    "source": _entity(
        "sources AS t",
        "id",
        (
            _field("id", "id"),
            _field("kind", "enum", enum=schemas.SOURCE_KINDS),
            _field("title", "text"),
            _field("uri", "text"),
            _field("source_date", "date"),
            _field("author_person_id", "id"),
            _field("content_sha256", "text"),
            _field("external_ref", "text"),
            _field("captured_at", "utc"),
            _field("captured_by", "text"),
        ),
        ("id", "kind", "title", "source_date", "author_person_id"),
        common=False,
    ),
}


def _clocks(field: _Field, now: str) -> tuple:
    return (now,) * field.clocks


def _reject_filter(path: str, message: str, allowed: list[str] | None = None) -> None:
    raise ToolError("invalid_arguments", message, path=path, allowed=allowed)


def _check_scalar(field: _Field, value, path: str) -> None:
    kind = field.kind
    if kind in {"id", "text", "cites"}:
        if not isinstance(value, str):
            _reject_filter(path, "expected string")
        return
    if kind == "enum":
        if value not in field.enum:
            _reject_filter(path, advice.enum_error(field.enum), list(field.enum))
        return
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            _reject_filter(path, "expected integer")
        return
    if kind == "date":
        if not isinstance(value, str) or not _real_date(value):
            _reject_filter(path, advice.date_error())
        return
    if kind == "utc":
        if not isinstance(value, str) or not _real_stamp(value):
            _reject_filter(path, advice.utc_error())
        return
    if kind == "bool":
        if not isinstance(value, bool):
            _reject_filter(path, "expected boolean")
        return
    _reject_filter(path, f"{kind} fields cannot be filtered")


def _bind_value(field: _Field, value):
    if field.kind == "bool":
        return 1 if value else 0
    return value


def _ops_of(field: _Field) -> list[str]:
    """The operators a filter on this field takes, in the order a message lists them."""
    if field.people is not None and field.kind == "json":
        takes = _LIST_OPS
    else:
        takes = set(_FIELD_OPS[field.kind]) | ({"under"} if field.tree else set())
    return [op for op in advice.OPERATORS if op in takes]


def _filter_ops(field: _Field, raw, path: str) -> list[tuple[str, object]]:
    if field.kind in {"json", "value"} and field.people is None:
        _reject_filter(path, f"{field.kind} fields cannot be filtered")
    if field.people is not None and field.kind == "json" and not isinstance(raw, dict):
        _reject_filter(path, "a people list filter takes an operator object: contains, in or is_null")
    if raw is None:
        return [("is_null", True)]
    if isinstance(raw, list) or not isinstance(raw, (dict, str, int, float, bool)):
        _reject_filter(path, "a filter must be a value, null, or an operator object")
    if not isinstance(raw, dict):
        return [("eq", raw)]
    names = list(raw)
    if len(names) == 2 and len(set(names) & _LOWER) == 1 and len(set(names) & _UPPER) == 1:
        return [(name, raw[name]) for name in names]
    if len(names) != 1 or names[0] not in _COMPARE and names[0] not in {"in", "is_null", "contains", "under"}:
        _reject_filter(path, advice.filter_object_error(_ops_of(field)))
    return [(names[0], raw[names[0]])]


_LIST_OPS = frozenset({"contains", "in", "is_null"})


def _list_filter(field: _Field, op: str, value, path: str) -> tuple[str, list]:
    """A people list field: contains one id, in 1 to 50 ids, or is_null for an empty list."""
    if op not in _LIST_OPS:
        _reject_filter(path, advice.operator_error(op, "a people list", _ops_of(field)))
    list_table, list_key, role = field.people
    listed = f"EXISTS (SELECT 1 FROM {list_table} AS p WHERE p.{list_key} = t.id AND p.role = '{role}'"
    if op == "is_null":
        if not isinstance(value, bool):
            _reject_filter(path, "is_null takes true or false")
        return ("NOT " if value else "") + listed + ")", []
    if op == "in" and (not isinstance(value, list) or not 1 <= len(value) <= 50 or any(item is None for item in value)):
        _reject_filter(path, "in takes 1 to 50 values and no nulls")
    values = [value] if op == "contains" else value
    if any(not isinstance(item, str) for item in values):
        _reject_filter(path, "expected string")
    return listed + f" AND p.person_id IN ({', '.join('?' for _ in values)}))", list(values)


def _cites_values(field: _Field, op: str, value, path: str) -> list[str]:
    if op not in _FIELD_OPS["cites"]:
        _reject_filter(path, advice.operator_error(op, "cites", _ops_of(field)))
    if op == "in" and (not isinstance(value, list) or not 1 <= len(value) <= 50 or any(item is None for item in value)):
        _reject_filter(path, "in takes 1 to 50 values and no nulls")
    values = [value] if op == "eq" else list(value)
    for item in values:
        _check_scalar(field, item, path)
    return values


def _cites_filter(field: _Field, op: str, value, path: str) -> tuple[str, list]:
    """Rows citing a source as their primary or through a link (0008)."""
    values = _cites_values(field, op, value, path)
    marks = ", ".join("?" for _ in values)
    clause = (
        f"(t.source_id IN ({marks}) OR EXISTS (SELECT 1 FROM links AS l WHERE l.subject_table = '{field.links}' "
        f"AND l.subject_id = t.id AND l.source_id IN ({marks})))"
    )
    return clause, values + values


def _compile_filter(field: _Field, raw, path: str, now: str, texts=()) -> tuple[list[str], list]:
    clauses: list[str] = []
    params: list = []
    for op, value in _filter_ops(field, raw, path):
        if field.kind == "json" and field.people is not None:
            clause, bound = _list_filter(field, op, value, path)
            clauses.append(clause)
            params.extend(bound)
            continue
        if field.links is not None:
            clause, bound = _cites_filter(field, op, value, path)
            clauses.append(clause)
            params.extend(bound)
            continue
        if op == "under":
            if not field.tree:
                _reject_filter(path, "under takes a project id, on project_id, parent_id or a project's id")
            _check_scalar(field, value, path)
            clauses.append(f"{field.expr} IN ({BRANCH_SQL})")
            params.append(value)
            continue
        if op not in _FIELD_OPS[field.kind]:
            _reject_filter(path, advice.operator_error(op, field.kind, _ops_of(field), texts))
        if field.people is not None and op in {"eq", "in"}:
            list_table, list_key, role = field.people
            values = [value] if op == "eq" else value
            if op == "in" and (not isinstance(value, list) or not 1 <= len(value) <= 50 or any(item is None for item in value)):
                _reject_filter(path, "in takes 1 to 50 values and no nulls")
            for item in values:
                _check_scalar(field, item, path)
            marks = ", ".join("?" for _ in values)
            clauses.append(
                f"EXISTS (SELECT 1 FROM {list_table} AS p WHERE p.{list_key} = t.id "
                f"AND p.role = '{role}' AND p.person_id IN ({marks}))"
            )
            params.extend(values)
            continue
        if op == "is_null":
            if not isinstance(value, bool):
                _reject_filter(path, "is_null takes true or false")
            clauses.append(f"{field.expr} IS NULL" if value else f"{field.expr} IS NOT NULL")
            params.extend(_clocks(field, now))
            continue
        if op == "in":
            if not isinstance(value, list) or not 1 <= len(value) <= 50 or any(item is None for item in value):
                _reject_filter(path, "in takes 1 to 50 values and no nulls")
            for item in value:
                _check_scalar(field, item, path)
            marks = ", ".join("?" for _ in value)
            clauses.append(f"{field.expr} IN ({marks})")
            params.extend(_clocks(field, now))
            for item in value:
                params.append(_bind_value(field, item))
            continue
        if op == "contains":
            if not isinstance(value, str) or not 1 <= len(value) <= 200:
                _reject_filter(path, "contains takes 1 to 200 characters")
            clauses.append(f"instr(lower({field.expr}), lower(?)) > 0")
            params.extend(_clocks(field, now))
            params.append(value)
            continue
        _check_scalar(field, value, path)
        clauses.append(f"{field.expr} {_COMPARE[op]} ?")
        params.extend(_clocks(field, now))
        params.append(_bind_value(field, value))
    return clauses, params


def _project_cell(field: _Field, raw, value_type: str | None):
    if field.kind == "bool":
        return bool(raw)
    if field.kind == "int":
        return None if raw is None else int(raw)
    if field.kind in {"json", "cites"}:
        return None if raw is None else json.loads(raw)  # trusted: a json column, or json_group_array
    if field.kind == "value":
        if raw is None:
            return None
        if isinstance(raw, str) and value_type in ("bool", "int"):
            return raw
        if value_type == "bool":
            return bool(raw)
        if value_type == "int":
            return int(raw)
        return raw
    return raw


def _note_columns(entity: str) -> set[str]:
    """The entity's note fields."""
    table = _GET_TABLE.get(entity)
    return {
        name.split(".", 1)[1]
        for name, kind in schemas.FIELD_CLASS.items()
        if kind == "note" and table is not None and name.split(".", 1)[0] == table
    }


def _records_query(con: sqlite3.Connection, arguments: dict, now: str) -> ToolResult:
    spec = _ENTITIES[arguments["entity"]]
    by_name = {field.name: field for field in spec["fields"]}
    allowed = [field.name for field in spec["fields"]]
    filters = arguments.get("filters") or {}
    clauses: list[str] = []
    where_params: list = []
    for name, raw in filters.items():
        field = by_name.get(name)
        if field is None:
            raise ToolError("unknown_field", advice.unknown_field_message(name, allowed), path=f"/filters/{name}", allowed=allowed)
        if isinstance(raw, dict) and isinstance(raw.get("under"), str) and field.tree:
            if not _query(con, "SELECT 1 FROM projects WHERE id = ?", (raw["under"],)):
                raise ToolError("not_found", advice.not_found_error("project", raw["under"]), path=f"/filters/{name}/under")
        if field.links is not None:
            cited = raw if not isinstance(raw, dict) else raw.get("eq", raw.get("in"))
            for source_id in cited if isinstance(cited, list) else [cited]:
                if isinstance(source_id, str) and not _query(con, "SELECT 1 FROM sources WHERE id = ?", (source_id,)):
                    raise ToolError("not_found", advice.not_found_error("source", source_id), path=f"/filters/{name}")
        extra, params = _compile_filter(field, raw, f"/filters/{name}", now, advice.text_fields(spec["fields"]))
        clauses.extend(extra)
        where_params.extend(params)
    requested = arguments.get("fields") or list(spec["defaults"])
    chosen: list[_Field] = []
    for index, name in enumerate(requested):
        field = by_name.get(name)
        if field is None:
            raise ToolError("unknown_field", advice.unknown_field_message(name, allowed), path=f"/fields/{index}", allowed=allowed)
        chosen.append(field)
    order_name = arguments.get("order_by") or spec["key"]
    order_field = by_name.get(order_name)
    if order_field is None:
        raise ToolError("unknown_field", advice.unknown_field_message(order_name, allowed), path="/order_by", allowed=allowed)
    if order_field.kind in _UNORDERABLE:
        orderable = [field.name for field in spec["fields"] if field.kind not in _UNORDERABLE]
        raise ToolError(
            "invalid_arguments",
            advice.unorderable_error(order_field.kind, orderable[:2]),
            path="/order_by",
            allowed=orderable,
        )
    key_field = by_name[spec["key"]]
    select_fields = list(chosen)
    hidden = False
    if any(field.kind == "value" for field in chosen) and not any(field.name == "value_type" for field in chosen):
        select_fields.append(by_name["value_type"])
        hidden = True
    select_params: list = []
    for field in select_fields:
        select_params.extend(_clocks(field, now))
    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    direction = " DESC" if arguments["desc"] else ""
    order_sql = f"{order_field.expr}{direction}, {key_field.expr}"
    if arguments["entity"] == "decision" and order_field is not key_field:
        # Decisions that tie break by when each was first recorded, then id, so an edit never reorders them.
        order_sql = f"{order_field.expr}{direction}, t.created_at{direction}, {key_field.expr}"
    limit = arguments["limit"]
    offset = arguments["offset"]
    rows = _query(
        con,
        f"SELECT {', '.join(field.expr for field in select_fields)} FROM {spec['from']}"
        f"{where_sql} ORDER BY {order_sql} LIMIT ? OFFSET ?",
        tuple(select_params + where_params + list(_clocks(order_field, now)) + list(_clocks(key_field, now)) + [limit, offset]),
    )
    total = int(
        _query(con, f"SELECT count(*) FROM {spec['from']}{where_sql}", tuple(where_params))[0][0]
    )
    value_index = next((i for i, field in enumerate(chosen) if field.name == "value_type"), None)
    projected = []
    for row in rows:
        values = list(row)
        value_type = None
        if any(field.kind == "value" for field in chosen):
            if hidden:
                value_type = values.pop()
            elif value_index is not None:
                value_type = values[value_index]
        cells = [_project_cell(field, raw, value_type) for field, raw in zip(chosen, values)]
        if arguments["format"] == "rows":
            projected.append(cells)
        else:
            projected.append(dict(zip((field.name for field in chosen), cells)))
    # A note field named in fields carries the note label.
    labelled = bool(_note_columns(arguments["entity"]) & {field.name for field in chosen})
    kept: list = []
    for item in projected:
        trial = {
            "entity": arguments["entity"],
            "fields": [field.name for field in chosen],
            "rows": kept + [item],
            "total": total,
            "next_offset": None,
            "truncated": False,
        }
        if labelled:
            trial["notes_label"] = strict.NOTE_LABEL
        if len(_compact(trial).encode()) > LIST_BUDGET:
            break
        kept.append(item)
    truncated = len(kept) < len(projected)
    next_offset = offset + len(kept) if truncated or offset + len(kept) < total else None
    result = {
        "entity": arguments["entity"],
        "fields": [field.name for field in chosen],
        "rows": kept,
        "total": total,
        "next_offset": next_offset,
        "truncated": truncated,
    }
    if labelled:
        result["notes_label"] = strict.NOTE_LABEL
    if total == 0:
        # A bare string on a text field is an exact match, which agents expect to be a word match.
        plain = next(((n, v) for n, v in filters.items() if by_name[n].kind == "text" and isinstance(v, str)), None)
        if plain is not None:
            result["hint"] = advice.plain_text_hint(*plain)
    while len(_compact(result).encode()) > LIST_BUDGET and kept:
        kept.pop()
        result["rows"] = kept
        result["truncated"] = True
        result["next_offset"] = offset + len(kept)
    return _ok(result)


_WORD = re.compile(r"[^\W_]+")
_CLOSED = (
    ("record", "superseded"),
    ("record", "archived"),
    ("decision", "reversed"),
    ("action", "done"),
    ("action", "dropped"),
    ("checklist_item", "done"),
    ("checklist_item", "not_applicable"),
    ("project", "done"),
)
_SEARCH_SELECT = (
    "SELECT rowid, entity, ref_id,\n"
    "       snippet(search_index, -1, '[', ']', '...', 12) AS snippet,\n"
    "       bm25(search_index, 0, 0, 0, 4.0, 1.0) AS score\n"
    "FROM search_index\n"
    "WHERE search_index MATCH ?"
)
# Without an entities filter, the 3 best source hits keep their place and every further source
# follows all other hits. bm25() only works in the MATCH query, so the window function ranks its
# rows in an outer query. That query leaves snippet() out: _SNIPPET_SELECT then computes it for
# the returned page's rowids only.
_SEARCH_RANK_SELECT = (
    "SELECT rowid, entity, ref_id,\n"
    "       bm25(search_index, 0, 0, 0, 4.0, 1.0) AS score\n"
    "FROM search_index\n"
    "WHERE search_index MATCH ?"
)
_SOURCE_QUOTA_SELECT = (
    "SELECT rowid, entity, ref_id, score\n"
    "FROM (\n"
    "  SELECT rowid, entity, ref_id, score,\n"
    "         row_number() OVER (PARTITION BY entity = 'source' ORDER BY score, entity, ref_id) AS source_rank\n"
    "  FROM (\n{candidates}\n  )\n"
    ")\n"
    "ORDER BY (entity = 'source' AND source_rank > 3), score, entity, ref_id\n"
    "LIMIT ? OFFSET ?"
)
_SNIPPET_SELECT = (
    "SELECT rowid, snippet(search_index, -1, '[', ']', '...', 12)\n"
    "FROM search_index\n"
    "WHERE search_index MATCH ? AND rowid IN ({marks})"
)


def fts_query(raw: str, any_word: bool = False) -> str | None:
    """The FTS5 match for a query: every word must match, or with any_word, at least one."""
    terms = []
    for token in raw.split():
        words = _WORD.findall(token)
        if not words:
            continue
        prefix = token.endswith("*") and len(words[-1]) >= 2
        terms.append('"' + " ".join(words) + '"' + ("*" if prefix else ""))
        if len(terms) == 12:
            break
    return (" OR " if any_word else " AND ").join(terms) if terms else None


def _search_verified(con: sqlite3.Connection, row) -> sqlite3.Row | None:
    entity, ref_id, rowid = row["entity"], row["ref_id"], row["rowid"]
    mapped = _query(con, "SELECT doc FROM search_keys WHERE entity = ? AND ref_id = ?", (entity, ref_id))
    if not mapped or mapped[0][0] != rowid:
        return None
    docs = _query(
        con,
        "SELECT status, title, body FROM search_docs WHERE entity = ? AND ref_id = ?",
        (entity, ref_id),
    )
    indexed = _query(con, "SELECT status, title, body FROM search_index WHERE rowid = ?", (rowid,))
    if len(docs) != 1 or len(indexed) != 1 or tuple(docs[0]) != tuple(indexed[0]):
        return None
    return docs[0]


def _search_result(
    raw: str, match: str, hits: list, next_offset, truncated: bool, unverified: int, relaxed: bool = False
) -> dict:
    warnings = []
    if relaxed:
        warnings.append("No result had every word; these match some of them.")
    if unverified:
        warnings.append(
            f"{unverified} hits failed verification. Run brain check, then brain search-rebuild."
        )
    result = {"query": raw, "match": match}
    if relaxed:
        result["relaxed"] = True
    result.update(
        hits=hits, next_offset=next_offset, truncated=truncated, unverified=unverified, warnings=warnings
    )
    return result


def _with_snippets(con: sqlite3.Connection, match: str, ranked: list, limit: int) -> list[dict]:
    """Ranked rows with each returned row's snippet, from one MATCH query over the page's rowids."""
    page = [row["rowid"] for row in ranked[:limit]]
    snippets: dict = {}
    if page:
        marks = ", ".join("?" for _ in page)
        snippets = dict(_query(con, _SNIPPET_SELECT.replace("{marks}", marks), (match, *page)))
    return [
        {
            "rowid": row["rowid"],
            "entity": row["entity"],
            "ref_id": row["ref_id"],
            "snippet": snippets.get(row["rowid"]),
            "score": row["score"],
        }
        for row in ranked
    ]


def _fact_hit_disputed(con: sqlite3.Connection, key: str) -> bool:
    """A fact hit is disputed while its key has an open conflict signal, or when the fact was retired
    with its last in-force status disputed."""
    if _query(
        con,
        "SELECT 1 FROM signals WHERE kind = 'conflict' AND status = 'open' "
        "AND subject_table = 'facts' AND subject_id = ? LIMIT 1",
        (key,),
    ):
        return True
    retired = _query(con, "SELECT 1 FROM facts WHERE key = ? AND status = 'retired'", (key,))
    return bool(retired) and _last_in_force_status(con, key) == "disputed"


_NOTE_SEARCH = frozenset(schemas.NOTE_ENTITY.values())


def _fold(text: str) -> str:
    """Lowercase, NFD, and no combining marks."""
    return "".join(ch for ch in unicodedata.normalize("NFD", str(text).lower()) if not unicodedata.combining(ch))


def _search_terms(raw: str) -> list[tuple[list[str], bool]]:
    """The query's terms as fts_query builds them: up to 12 tokens that hold words, each a list of
    words and whether it is a prefix term."""
    terms = []
    for token in raw.split():
        words = _WORD.findall(_fold(token))
        if not words:
            continue
        terms.append((words, token.endswith("*") and len(words[-1]) >= 2))
        if len(terms) == 12:
            break
    return terms


def _non_note_text(con: sqlite3.Connection, entity: str, ref_id: str, title: str) -> list[str]:
    """A hit's text that isn't a note: its title, a checklist item's section, a decision's quote and a record's
    summary."""
    parts = [title or ""]
    if entity == "checklist_item":
        found = _query(con, "SELECT section FROM checklist_items WHERE id = ?", (ref_id,))
        parts.extend(row[0] or "" for row in found)
    elif entity == "decision":
        found = _query(con, "SELECT quote FROM decisions WHERE id = ?", (ref_id,))
        parts.extend(row[0] or "" for row in found)
    elif entity == "record":
        # A record's summary (0008) is gated text, not a note.
        found = _query(con, "SELECT summary FROM records WHERE id = ?", (ref_id,))
        parts.extend(row[0] or "" for row in found)
    return parts


def _matches_terms(terms: list[tuple[list[str], bool]], parts: list[str]) -> bool:
    """Whether every term's words appear consecutively in one of the texts, the last one as a
    prefix for a prefix term."""
    lists = [_WORD.findall(_fold(part)) for part in parts]
    for words, prefix in terms:
        size = len(words)
        found = False
        for tokens in lists:
            for start in range(len(tokens) - size + 1):
                window = tokens[start:start + size]
                if window[:-1] == words[:-1] and (
                    window[-1].startswith(words[-1]) if prefix else window[-1] == words[-1]
                ):
                    found = True
                    break
            if found:
                break
        if not found:
            return False
    return True


def _held_terms(terms: list[tuple[list[str], bool]], doc) -> list[tuple[list[str], bool]]:
    """The query's terms a hit's indexed text holds (1.11.0), since any word counts. Every term if none is found, as
    the all-words match had it."""
    held = [term for term in terms if _matches_terms([term], [doc["title"] or "", doc["body"] or ""])]
    return held or terms


# search and brief under a folder: each searchable entity filed in a project, and the column naming it. A project
# is inside the folder by its own id.
_FILED = {
    entity: (table, "id" if entity == "project" else "project_id")
    for table, (entity, _key, _events) in store.SEARCH_TABLES.items()
    if entity == "project" or any(field.name == "project_id" for field in _ENTITIES.get(entity, {"fields": ()})["fields"])
}


def _in_branch(column: str, branch: list[str] | None) -> tuple[str, tuple]:
    """An AND clause keeping rows whose column names a folder in the branch, or nothing without one."""
    if branch is None:
        return "", ()
    return f" AND {column} IN (SELECT value FROM json_each(?))", (json.dumps(branch),)


def _branch_ids(con: sqlite3.Connection, project_id: str, path: str = "/under") -> list[str]:
    """A folder and every folder inside it, or a refusal naming how to find a project id."""
    ids = [row[0] for row in _query(con, BRANCH_SQL, (project_id,))]
    if not ids:
        raise ToolError("not_found", advice.not_found_error("project", project_id), path=path)
    return ids


def _visible_folders(con: sqlite3.Connection) -> list[str]:
    """Every folder a folder token sees, read through its filtered view of projects."""
    return [row[0] for row in _query(con, "SELECT id FROM projects ORDER BY id")]


def _search_filter(arguments: dict) -> tuple[str, list]:
    """The search's WHERE clauses after the MATCH, and their parameters: entities, the folder and closed items."""
    where = ""
    params: list = []
    entities = arguments.get("entities")
    if entities:
        where += "\n  AND entity IN (" + ", ".join("?" for _ in entities) + ")"
        params.extend(entities)
    branch = arguments.get("_branch")
    if branch is not None:
        inside = " OR ".join(
            f"(entity = '{entity}' AND ref_id IN (SELECT id FROM {table} WHERE {column} IN (SELECT value FROM json_each(?))))"
            for entity, (table, column) in _FILED.items()
        )
        where += f"\n  AND ({inside})"
        params.extend([json.dumps(branch)] * len(_FILED))
    if not arguments["include_closed"]:
        where += "\n  AND (entity, status) NOT IN (VALUES " + ", ".join("(?, ?)" for _ in _CLOSED) + ")"
        for pair in _CLOSED:
            params.extend(pair)
    return where, params


def _search_has_match(con: sqlite3.Connection, match: str, arguments: dict) -> bool:
    """Whether any row the search may show holds the match."""
    where, params = _search_filter(arguments)
    sql = "SELECT 1 FROM search_index\nWHERE search_index MATCH ?" + where + "\nLIMIT 1"
    return bool(_query(con, sql, (match, *params)))


def _search_rows(con: sqlite3.Connection, match: str, arguments: dict, limit: int, offset: int) -> list:
    """One page of ranked rows for a match, plus one more row to show whether a page follows."""
    where, filters = _search_filter(arguments)
    params = [match, *filters, limit + 1, offset]
    if arguments.get("entities"):
        sql = _SEARCH_SELECT + where + "\nORDER BY score, entity, ref_id\nLIMIT ? OFFSET ?"
        return _query(con, sql, tuple(params))
    sql = _SOURCE_QUOTA_SELECT.replace("{candidates}", _SEARCH_RANK_SELECT + where)
    return _with_snippets(con, match, _query(con, sql, tuple(params)), limit)


# Where each search entity lives, so a hit can show its date (1.10.0).
_HIT_TABLE = {entity: (table, key) for table, (entity, key, _events) in store.SEARCH_TABLES.items()}


def _hit_date(con: sqlite3.Connection, entity: str, ref_id: str, zone: str) -> str | None:
    """A hit's date (1.10.0): a source's own date, or the local day any other row last changed, so a reader can tell
    the newest of several hits that share a title."""
    table, key = _HIT_TABLE[entity]
    column = "source_date" if entity == "source" else "updated_at"
    found = _query(con, f"SELECT {column} FROM {table} WHERE {key} = ?", (ref_id,))
    if not found or not found[0][0]:
        return None
    return found[0][0] if entity == "source" else _today(found[0][0], zone)


def _named_project_hits(con: sqlite3.Connection, raw: str, arguments: dict, zone: str) -> list[dict]:
    """Pinned hits (1.10.0): when the whole query names a project, by its id or its title, the first page starts with
    that project and the record in it that changed last, such as its state note. They come on top of the limit."""
    if arguments["offset"]:
        return []
    words = _WORD.findall(_fold(raw))
    entities = arguments.get("entities")
    branch = arguments.get("_branch")
    closed = [] if arguments["include_closed"] else [status for entity, status in _CLOSED if entity == "record"]
    open_only = f" AND status NOT IN ({', '.join('?' for _ in closed)})" if closed else ""
    hits = []
    for project in _query(con, "SELECT id, title, status FROM projects ORDER BY id"):
        if words not in (_WORD.findall(_fold(project["id"])), _WORD.findall(_fold(project["title"]))):
            continue
        if (branch is not None and project["id"] not in branch) or (
            project["status"] == "done" and not arguments["include_closed"]
        ):
            continue
        if not entities or "project" in entities:
            hits.append({"entity": "project", "id": project["id"], "title": project["title"],
                         "status": project["status"], "date": _hit_date(con, "project", project["id"], zone),
                         "snippet": "", "score": None, "pinned": True})
        newest = _query(
            con,
            f"SELECT id, title, status, summary FROM records WHERE project_id = ?{open_only} "
            "ORDER BY updated_at DESC, id LIMIT 1",
            (project["id"], *closed),
        )
        if newest and (not entities or "record" in entities):
            record = newest[0]
            hit = {"entity": "record", "id": record["id"], "title": record["title"], "status": record["status"],
                   "date": _hit_date(con, "record", record["id"], zone), "snippet": (record["summary"] or "")[:300],
                   "score": None, "pinned": True}
            if _query(con, "SELECT 1 FROM unchecked_text WHERE entity = 'record' AND ref_id = ?", (record["id"],)):
                hit["unchecked"] = True
            hits.append(hit)
    return hits


def _search(con: sqlite3.Connection, arguments: dict, zone: str, scoped: bool = False) -> ToolResult:
    raw = arguments["query"]
    # Every query ranks by any word (1.11.0), so a record missing one word still ranks, and bm25 puts hits with more
    # and rarer words first. Needing every word dropped the right record while long notes holding every word led.
    match = fts_query(raw, any_word=True)
    if match is None:
        raise ToolError("bad_query", "query has no searchable words")
    limit = arguments["limit"]
    offset = arguments["offset"]
    if arguments.get("under") is not None:
        arguments = {**arguments, "_branch": _branch_ids(con, arguments["under"])}
    elif scoped:
        # A folder token always searches its branch, so a hit outside it never matches, ranks or counts as unverified.
        arguments = {**arguments, "_branch": _visible_folders(con)}
    pins = _named_project_hits(con, raw, arguments, zone)
    pinned = {(hit["entity"], hit["id"]) for hit in pins}
    rows = _search_rows(con, match, arguments, limit, offset)
    # relaxed still means no result has every word, on every page, so the usage log's share stays comparable.
    every_word = fts_query(raw)
    relaxed = bool(rows) and every_word != match and not _search_has_match(con, every_word, arguments)
    more = len(rows) > limit
    checked = []
    unverified = 0
    terms = _search_terms(raw)
    for index, row in enumerate(rows[:limit]):
        if (row["entity"], row["ref_id"]) in pinned:
            continue
        doc = _search_verified(con, row)
        if doc is None:
            unverified += 1
            continue
        snippet = row["snippet"] or ""
        hit = {
            "entity": row["entity"],
            "id": row["ref_id"],
            "title": doc["title"],
            "status": doc["status"],
            "date": _hit_date(con, row["entity"], row["ref_id"], zone),
            "snippet": snippet[:300],
            "score": round(-row["score"], 3),
        }
        if row["entity"] in _NOTE_SEARCH and not _matches_terms(
            _held_terms(terms, doc), _non_note_text(con, row["entity"], row["ref_id"], doc["title"])
        ):
            # A word the hit matched is only in its note text.
            hit["match"] = "note"
        if row["entity"] == "fact" and _fact_hit_disputed(con, row["ref_id"]):
            # The fact document holds claim quotes, so a snippet would show one side's value.
            hit["snippet"] = "disputed"
            hit["disputed"] = True
        if row["entity"] in _MARKER_ENTITIES and _query(
            con,
            "SELECT 1 FROM unchecked_text WHERE entity = ? AND ref_id = ?",
            (row["entity"], row["ref_id"]),
        ):
            hit["unchecked"] = True
        checked.append((index, hit))
    kept: list[tuple[int, dict]] = []
    truncated = False
    next_offset = offset + limit if more else None
    for index, hit in checked:
        trial = _search_result(raw, match, pins + [item for _, item in kept] + [hit], None, False, unverified, relaxed)
        if not _fits(trial):
            truncated = True
            next_offset = offset + index
            break
        kept.append((index, hit))
    hits = pins + [hit for _, hit in kept]
    result = _search_result(raw, match, hits, next_offset, truncated, unverified, relaxed)
    while not _fits(result) and kept:
        index, _ = kept.pop()
        hits = pins + [hit for _, hit in kept]
        truncated = True
        next_offset = offset + index
        result = _search_result(raw, match, hits, next_offset, truncated, unverified, relaxed)
    return _ok(result)


_PAGE_CHARS = 8000
_PAGE_FIELD = {"record": "body", "source": "content"}
_GET_TABLE = schemas.ENTITY_TABLE
_JSON_FIELDS = {"aliases", "allowed_values"}
_SUBJECT_ENTITY = {
    "actions": "action",
    "checklist_items": "checklist_item",
    "decisions": "decision",
    "facts": "fact",
    "projects": "project",
    "records": "record",
}
_SUBJECT_TITLE = {
    "actions": "SELECT title FROM actions WHERE id = ?",
    "checklist_items": "SELECT title FROM checklist_items WHERE id = ?",
    "decisions": "SELECT title FROM decisions WHERE id = ?",
    "records": "SELECT title FROM records WHERE id = ?",
    "projects": "SELECT title FROM projects WHERE id = ?",
    "facts": "SELECT key FROM fact_keys WHERE key = ?",
}


def _mapped(con: sqlite3.Connection, sql: str, params: tuple, names: tuple[str, ...], bools: frozenset[str] = frozenset()) -> list[dict]:
    items = []
    for row in _query(con, sql, params):
        item = {}
        for name, value in zip(names, row, strict=True):
            item[name] = bool(value) if name in bools else value
        items.append(item)
    return items


def _one(con: sqlite3.Connection, sql: str, params: tuple, names: tuple[str, ...], bools: frozenset[str] = frozenset()) -> dict | None:
    found = _mapped(con, sql, params, names, bools)
    return found[0] if found else None


def _listed(items: list[dict], cap: int = 20) -> dict:
    return {"items": items[:cap], "total": len(items)}


def _count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(_query(con, sql, params)[0][0])


def _cited_source(con: sqlite3.Connection, source_id: str, locator: str) -> dict:
    src = _query(
        con,
        "SELECT id, kind, title, uri, source_date, author_person_id FROM sources WHERE id = ?",
        (source_id,),
    )[0]
    cited = {
        "id": src["id"],
        "kind": src["kind"],
        "title": src["title"],
        "uri": src["uri"],
        "source_date": src["source_date"],
        "author_person_id": src["author_person_id"],
        "locator": locator,
    }
    if _has_successor(con, source_id):
        cited["source_current"] = False  # a newer version of this source exists
    return cited


# links (0008): the entities a link can name, by table, from schemas.LINK_TABLES.
_TABLE_ENTITY = {table: entity for entity, table in schemas.ENTITY_TABLE.items()}
_LINK_ENTITIES = {_TABLE_ENTITY[table]: table for table in schemas.LINK_TABLES}
_SHOWN_LINKS = 20
_LINK_SOURCE_NAMES = ("label", "id", "locator", "title", "kind", "source_date", "author_person_id")


def _row_sources(con: sqlite3.Connection, table: str, row) -> dict:
    """Every source a row cites: its own source first (label primary), then each link by when it was added. The
    primary item holds only its id and locator, since the result's source already describes it in full."""
    primary = {"label": "primary", "id": row["source_id"], "locator": row["locator"]}
    linked = _mapped(
        con,
        "SELECT l.label, s.id, l.locator, s.title, s.kind, s.source_date, s.author_person_id "
        "FROM links AS l JOIN sources AS s ON s.id = l.source_id "
        "WHERE l.subject_table = ? AND l.subject_id = ? ORDER BY l.created_at, l.source_id, l.label LIMIT ?",
        (table, row["id"], _SHOWN_LINKS - 1),
        _LINK_SOURCE_NAMES,
    )
    total = 1 + _count(con, "SELECT count(*) FROM links WHERE subject_table = ? AND subject_id = ?", (table, row["id"]))
    return {"items": [primary, *linked], "total": total}


_CITED_BY_SQL = " UNION ALL ".join(
    [
        f"SELECT '{entity}' AS entity, id, title, 'primary' AS label, 0 AS n FROM {table} WHERE source_id = ?"
        for entity, table in _LINK_ENTITIES.items()
    ]
    + [
        f"SELECT '{entity}', l.subject_id, s.title, l.label, 1 FROM links AS l JOIN {table} AS s "
        f"ON s.id = l.subject_id WHERE l.subject_table = '{table}' AND l.source_id = ?"
        for entity, table in _LINK_ENTITIES.items()
    ]
)


def _cited_by(con: sqlite3.Connection, source_id: str) -> dict:
    """The rows citing a source, as their primary source or through a link (0008)."""
    params = (source_id,) * (2 * len(_LINK_ENTITIES))
    items = _mapped(
        con,
        f"SELECT entity, id, title, label FROM ({_CITED_BY_SQL}) ORDER BY n, entity, id, label LIMIT ?",
        (*params, _SHOWN_LINKS),
        ("entity", "id", "title", "label"),
    )
    return {"items": items, "total": _count(con, f"SELECT count(*) FROM ({_CITED_BY_SQL})", params)}


def _thread_links(con: sqlite3.Connection, column: str, subject_id: str) -> dict:
    rows = _mapped(
        con,
        "SELECT l.id, l.thread_id, l.role, l.status, s.reply_state, "
        "coalesce(s.new_message, 0), t.subject "
        "FROM thread_links AS l "
        "JOIN threads AS t ON t.id = l.thread_id "
        "LEFT JOIN thread_link_state AS s ON s.link_id = l.id "
        f"WHERE l.{column} = ? ORDER BY l.id",
        (subject_id,),
        ("id", "thread_id", "role", "status", "reply_state", "new_message", "thread_subject"),
        frozenset({"new_message"}),
    )
    return _listed(rows)


def _open_signals(con: sqlite3.Connection, table: str, subject_id: str) -> dict:
    rows = _mapped(
        con,
        "SELECT id, kind, thread_id, detail FROM signals "
        "WHERE status = 'open' AND subject_table = ? AND subject_id = ? ORDER BY id",
        (table, subject_id),
        ("id", "kind", "thread_id", "detail"),
    )
    return _listed(rows)


def _project_ref(con: sqlite3.Connection, project_id: str | None) -> dict | None:
    if project_id is None:
        return None
    return _one(
        con,
        "SELECT id, kind, title, status FROM projects WHERE id = ?",
        (project_id,),
        ("id", "kind", "title", "status"),
    )


def _action_ref(con: sqlite3.Connection, action_id: str | None) -> dict | None:
    if action_id is None:
        return None
    return _one(
        con,
        "SELECT id, title, status FROM actions WHERE id = ?",
        (action_id,),
        ("id", "title", "status"),
    )


def _subject_title(con: sqlite3.Connection, table: str, subject_id: str):
    rows = _query(con, _SUBJECT_TITLE[table], (subject_id,))
    return None if not rows else rows[0][0]


def _related(con: sqlite3.Connection, entity: str, row) -> dict:
    if entity == "action":
        return {
            "thread_links": _thread_links(con, "action_id", row["id"]),
            "checklist_items": _listed(
                _mapped(
                    con,
                    "SELECT id, title, state FROM checklist_items WHERE action_id = ? ORDER BY id",
                    (row["id"],),
                    ("id", "title", "state"),
                )
            ),
            "blocked_by": _action_ref(con, row["blocked_by"]),
            "record": _one(
                con,
                "SELECT id, title, status FROM records WHERE id = ?",
                (row["record_id"],),
                ("id", "title", "status"),
            )
            if row["record_id"] is not None
            else None,
            "signals": _open_signals(con, "actions", row["id"]),
        }
    if entity == "checklist_item":
        return {
            "project": _project_ref(con, row["project_id"]),
            "action": _action_ref(con, row["action_id"]),
            "thread_links": _thread_links(con, "checklist_item_id", row["id"]),
            "signals": _open_signals(con, "checklist_items", row["id"]),
        }
    if entity == "decision":
        return {
            "project": _project_ref(con, row["project_id"]),
            "decided_by": _one(
                con,
                "SELECT id, display_name FROM people WHERE id = ?",
                (row["decided_by_person_id"],),
                ("id", "display_name"),
            ),
            "supersedes": _one(
                con,
                "SELECT id, title, status FROM decisions WHERE id = ?",
                (row["supersedes_id"],),
                ("id", "title", "status"),
            )
            if row["supersedes_id"] is not None
            else None,
            "superseded_by": _listed(
                _mapped(
                    con,
                    "SELECT id, title, status FROM decisions WHERE supersedes_id = ? ORDER BY id",
                    (row["id"],),
                    ("id", "title", "status"),
                )
            ),
        }
    if entity == "record":
        return {
            "project": _project_ref(con, row["project_id"]),
            "actions": _listed(
                _mapped(
                    con,
                    "SELECT id, title, status FROM actions WHERE record_id = ? ORDER BY id",
                    (row["id"],),
                    ("id", "title", "status"),
                )
            ),
        }
    if entity == "project":
        marks = ", ".join("?" for _ in OPEN_ACTION_STATUSES)
        return {
            "counts": {
                "open_actions": _count(
                    con,
                    f"SELECT count(*) FROM actions WHERE project_id = ? AND status IN ({marks})",
                    (row["id"], *OPEN_ACTION_STATUSES),
                ),
                "checklist_items": _count(
                    con,
                    "SELECT count(*) FROM checklist_items WHERE project_id = ? AND state <> 'not_applicable'",
                    (row["id"],),
                ),
                "decisions": _count(con, "SELECT count(*) FROM decisions WHERE project_id = ?", (row["id"],)),
                "records": _count(con, "SELECT count(*) FROM records WHERE project_id = ?", (row["id"],)),
            },
            "folder": _folder(con, row["id"]),
            "recent_records": _recent_records(con, row["id"]),
        }
    if entity == "person":
        return {
            "counts": {
                "actions_owned": _count(
                    con,
                    "SELECT count(DISTINCT action_id) FROM action_people WHERE person_id = ? AND role = 'owner'",
                    (row["id"],),
                ),
                "actions_waiting_on": _count(
                    con,
                    "SELECT count(DISTINCT action_id) FROM action_people "
                    "WHERE person_id = ? AND role = 'waiting_on'",
                    (row["id"],),
                ),
                "checklist_items_owned": _count(
                    con,
                    "SELECT count(DISTINCT item_id) FROM checklist_item_people "
                    "WHERE person_id = ? AND role = 'owner'",
                    (row["id"],),
                ),
                "checklist_items_waiting_on": _count(
                    con,
                    "SELECT count(DISTINCT item_id) FROM checklist_item_people "
                    "WHERE person_id = ? AND role = 'waiting_on'",
                    (row["id"],),
                ),
                "decisions_made": _count(
                    con, "SELECT count(*) FROM decisions WHERE decided_by_person_id = ?", (row["id"],)
                ),
                "sources_authored": _count(
                    con, "SELECT count(*) FROM sources WHERE author_person_id = ?", (row["id"],)
                ),
            }
        }
    if entity == "system":
        return {
            "owner": _one(
                con,
                "SELECT id, display_name FROM people WHERE id = ?",
                (row["owner_person_id"],),
                ("id", "display_name"),
            )
            if row["owner_person_id"] is not None
            else None
        }
    if entity == "fact":
        allowed = row["allowed_values"]
        return {
            "registry": {
                "key": row["key"],
                "value_type": row["value_type"],
                "unit": row["unit"],
                "allowed_values": None if allowed is None else json.loads(allowed),  # trusted: fact_keys.allowed_values, written by the applier
                "stale_after_days": row["stale_after_days"],
                "description": row["description"],
                "version": int(row["version"]),
            }
        }
    if entity == "thread":
        rows = _mapped(
            con,
            "SELECT l.id, l.action_id, l.checklist_item_id, l.role, l.status, s.reply_state, "
            "coalesce(s.new_message, 0) "
            "FROM thread_links AS l "
            "LEFT JOIN thread_link_state AS s ON s.link_id = l.id "
            "WHERE l.thread_id = ? ORDER BY l.id",
            (row["id"],),
            ("id", "action_id", "checklist_item_id", "role", "status", "reply_state", "new_message"),
            frozenset({"new_message"}),
        )
        return {"thread_links": _listed(rows)}
    if entity == "thread_link":
        if row["action_id"] is not None:
            subject = {
                "entity": "action",
                "id": row["action_id"],
                "title": _subject_title(con, "actions", row["action_id"]),
            }
        else:
            subject = {
                "entity": "checklist_item",
                "id": row["checklist_item_id"],
                "title": _subject_title(con, "checklist_items", row["checklist_item_id"]),
            }
        state_rows = _query(
            con,
            "SELECT reply_state, new_message FROM thread_link_state WHERE link_id = ?",
            (row["id"],),
        )
        state = None
        if state_rows:
            state = {"reply_state": state_rows[0]["reply_state"], "new_message": bool(state_rows[0]["new_message"])}
        return {
            "thread": _one(
                con,
                "SELECT id, channel, account, subject, last_inbound_at, last_outbound_at, outbound_seen "
                "FROM threads WHERE id = ?",
                (row["thread_id"],),
                ("id", "channel", "account", "subject", "last_inbound_at", "last_outbound_at", "outbound_seen"),
                frozenset({"outbound_seen"}),
            ),
            "subject": subject,
            "state": state,
        }
    if entity == "meeting":
        transcript_id = row["transcript_source_id"]
        return {
            "thread": _one(
                con,
                "SELECT id, channel, account, subject, last_inbound_at, last_outbound_at, outbound_seen "
                "FROM threads WHERE id = ?",
                (row["thread_id"],),
                ("id", "channel", "account", "subject", "last_inbound_at", "last_outbound_at", "outbound_seen"),
                frozenset({"outbound_seen"}),
            ),
            "project": _project_ref(con, row["project_id"]),
            "transcript": None
            if transcript_id is None
            else _one(
                con,
                "SELECT id, kind, title, source_date FROM sources WHERE id = ?",
                (transcript_id,),
                ("id", "kind", "title", "source_date"),
            ),
        }
    if entity == "signal":
        thread = None
        if row["thread_id"] is not None:
            thread = _one(
                con,
                "SELECT id, subject FROM threads WHERE id = ?",
                (row["thread_id"],),
                ("id", "subject"),
            )
        return {
            "subject": {
                "entity": _SUBJECT_ENTITY[row["subject_table"]],
                "id": row["subject_id"],
                "title": _subject_title(con, row["subject_table"], row["subject_id"]),
            },
            "thread": thread,
        }
    author = None
    if row["author_person_id"] is not None:
        author = _one(
            con,
            "SELECT id, display_name FROM people WHERE id = ?",
            (row["author_person_id"],),
            ("id", "display_name"),
        )
    return {"author": author}


def _typed_row(row, page_field: str | None, offset: int, page_size: int) -> tuple[dict, dict | None]:
    data = {}
    for name in row.keys():
        value = row[name]
        if name in {"outbound_seen", "all_day"}:
            value = bool(value)
        elif name in _JSON_FIELDS and value is not None:
            value = json.loads(value)  # trusted: a json column, written by the applier
        data[name] = value
    if page_field is None:
        return data, None
    full = data[page_field]
    total = len(full)
    if offset > total:
        raise ToolError("invalid_arguments", "offset is past the text", path="/offset")
    text = full[offset : offset + page_size]
    data[page_field] = text
    end = offset + len(text)
    return data, {
        page_field: {
            "offset": offset,
            "returned": len(text),
            "total": total,
            "next_offset": end if end < total else None,
        }
    }


_MARKER_ENTITIES = frozenset(entity for entity, _table in store.UNCHECKED_PARENTS)
_UNCHECKED_NOTE = (
    "Imported without passing the evidence gate. "
    "Treat these sentences as unverified until a source confirms them."
)
_PEOPLE_LIST_TABLES = {
    entity: schemas.PEOPLE_LISTS[table] for entity, table in schemas.ENTITY_TABLE.items() if table in schemas.PEOPLE_LISTS
}


def _with_people(con: sqlite3.Connection, entity: str, subject_id: str, typed: dict) -> dict:
    list_table, list_key = _PEOPLE_LIST_TABLES[entity]
    lists = {}
    for role, _scalar, field in schemas.PEOPLE_ROLES:
        lists[field] = [
            row[0]
            for row in _query(
                con,
                f"SELECT person_id FROM {list_table} WHERE {list_key} = ? AND role = ? ORDER BY position",
                (subject_id, role),
            )
        ]
    out = {}
    for name, value in typed.items():
        out[name] = value
        if name == "waiting_on_person_id":
            out.update(lists)
    return out


def _unchecked_summary(con: sqlite3.Connection, entity: str, subject_id: str) -> dict | None:
    rows = _query(
        con,
        "SELECT sentences, source_id, created_at FROM unchecked_text WHERE entity = ? AND ref_id = ?",
        (entity, subject_id),
    )
    if not rows:
        return None
    # brain check reports a malformed marker; get still answers rather than failing on it.
    parsed = json.loads(rows[0]["sentences"])  # trusted: unchecked_text.sentences, written by the applier
    elements = parsed if isinstance(parsed, list) else []
    shown: list[dict] = []
    for item in elements:
        if len(shown) == 10:
            break
        if not isinstance(item, dict):
            continue
        text, reasons = item.get("text"), item.get("reasons")
        shown.append(
            {
                "text": text if isinstance(text, str) else "",
                "reasons": [r for r in reasons if isinstance(r, str)] if isinstance(reasons, list) else [],
            }
        )
    return {
        "count": len(elements),
        "sentences": shown,
        "source_id": rows[0]["source_id"],
        "since": rows[0]["created_at"],
        "note": _UNCHECKED_NOTE,
    }


def _history(con: sqlite3.Connection, entity: str, subject_id: str) -> dict:
    if entity == "record":
        rows = _query(
            con,
            "SELECT version, change, kind, title, status, project_id, source_id, locator, "
            "recorded_at, recorded_by FROM record_history WHERE record_id = ? ORDER BY version DESC",
            (subject_id,),
        )
        names = (
            "version",
            "change",
            "kind",
            "title",
            "status",
            "project_id",
            "source_id",
            "locator",
            "recorded_at",
            "recorded_by",
        )
        items = []
        for row in rows[:20]:
            item = dict(zip(names, row, strict=True))
            item["version"] = int(item["version"])
            items.append(item)
        return {"items": items, "total": len(rows)}
    rows = _query(
        con,
        "SELECT version, change, value_type, value_text, value_int, value_real, value_date, status, "
        "basis, as_of, last_verified_at, source_id, locator, recorded_at, recorded_by "
        "FROM fact_history WHERE key = ? ORDER BY version DESC",
        (subject_id,),
    )
    hidden: set[int] = set()
    in_force = None
    for row in reversed(rows):  # ascending: a retired row after a disputed one hides its value too
        if row["status"] == "retired":
            if in_force == "disputed":
                hidden.add(int(row["version"]))
            continue
        in_force = row["status"]
        if in_force == "disputed":
            hidden.add(int(row["version"]))
    items = []
    for row in rows[:20]:
        items.append(
            {
                "version": int(row["version"]),
                "change": row["change"],
                "value": "disputed" if int(row["version"]) in hidden else _fact_value(row),
                "status": row["status"],
                "basis": row["basis"],
                "as_of": row["as_of"],
                "last_verified_at": row["last_verified_at"],
                "source_id": row["source_id"],
                "locator": row["locator"],
                "recorded_at": row["recorded_at"],
                "recorded_by": row["recorded_by"],
            }
        )
    return {"items": items, "total": len(rows)}


_OPEN_MARKS = ", ".join("?" for _ in OPEN_ACTION_STATUSES)
# Open actions with a due date and open checklist items with a land-by date. Ordered by date,
# then actions before checklist items ('action' sorts first), then id.
_DUE_SQL = (
    "SELECT entity, id, title, date, project_id FROM ("
    "SELECT 'action' AS entity, id, title, due_date AS date, project_id FROM actions "
    f"WHERE status IN ({_OPEN_MARKS}) AND due_date IS NOT NULL "
    "UNION ALL "
    "SELECT 'checklist_item', id, title, land_by, project_id FROM checklist_items "
    "WHERE state NOT IN ('done', 'not_applicable') AND land_by IS NOT NULL"
    ") WHERE {where} ORDER BY date, entity, id LIMIT ?"
)
_PROJECT_WAITING_SQL = (
    "SELECT p.id, p.display_name, count(*) AS items FROM ("
    "SELECT l.person_id FROM action_people AS l JOIN actions AS a ON a.id = l.action_id "
    f"WHERE l.role = 'waiting_on' AND a.project_id = ? AND a.status IN ({_OPEN_MARKS}) "
    "UNION ALL "
    "SELECT l.person_id FROM checklist_item_people AS l JOIN checklist_items AS c ON c.id = l.item_id "
    "WHERE l.role = 'waiting_on' AND c.project_id = ? AND c.state NOT IN ('done', 'not_applicable')"
    ") AS w JOIN people AS p ON p.id = w.person_id "
    "GROUP BY p.id, p.display_name ORDER BY items DESC, p.id LIMIT 10"
)


def _due_rows(con: sqlite3.Connection, where: str, params: tuple, limit: int = -1) -> list:
    return _query(con, _DUE_SQL.replace("{where}", where), (*OPEN_ACTION_STATUSES, *params, limit))


def _project_tallies(con: sqlite3.Connection, project_id: str) -> tuple[dict, dict]:
    """Checklist items by state and actions by status, every value present, in schema order."""
    states = {state: 0 for state in schemas.CHECKLIST_STATES}
    for state, count in _query(
        con, "SELECT state, count(*) FROM checklist_items WHERE project_id = ? GROUP BY state", (project_id,)
    ):
        states[state] = int(count)
    statuses = {status: 0 for status in schemas.ACTION_STATUSES}
    for status, count in _query(
        con, "SELECT status, count(*) FROM actions WHERE project_id = ? GROUP BY status", (project_id,)
    ):
        statuses[status] = int(count)
    return states, statuses


def _folder(con: sqlite3.Connection, project_id: str) -> dict:
    """Folders (0007): where a project sits, the folders inside it, and the open work in it and every folder below."""
    path: list[str] = []
    seen = project_id
    for _ in range(FOLDER_DEPTH):
        parent = _query(con, "SELECT parent_id FROM projects WHERE id = ?", (seen,))
        if not parent or parent[0][0] is None:
            break
        seen = parent[0][0]
        path.insert(0, seen)
    children = _mapped(
        con,
        "SELECT id, kind, title, status FROM projects WHERE parent_id = ? ORDER BY sort_order, id",
        (project_id,),
        ("id", "kind", "title", "status"),
    )
    marks = ", ".join("?" for _ in OPEN_ACTION_STATUSES)
    branch = {
        "folders": _count(con, f"SELECT count(*) - 1 FROM ({BRANCH_SQL})", (project_id,)),
        "open_actions": _count(
            con,
            f"SELECT count(*) FROM actions WHERE project_id IN ({BRANCH_SQL}) AND status IN ({marks})",
            (project_id, *OPEN_ACTION_STATUSES),
        ),
        "records": _count(con, f"SELECT count(*) FROM records WHERE project_id IN ({BRANCH_SQL})", (project_id,)),
    }
    return {"path": path, "children": children, "branch": branch}


def _recent_records(con: sqlite3.Connection, project_id: str) -> dict:
    """The 3 records in a project that changed last, newest first (1.10.0), so opening a project shows its latest
    note, such as where it stands. Superseded and archived records are left out."""
    closed = [status for entity, status in _CLOSED if entity == "record"]
    open_only = f"project_id = ? AND status NOT IN ({', '.join('?' for _ in closed)})"
    items = _mapped(
        con,
        f"SELECT id, kind, title, status, updated_at FROM records WHERE {open_only} ORDER BY updated_at DESC, id LIMIT 3",
        (project_id, *closed),
        ("id", "kind", "title", "status", "updated_at"),
    )
    return {"items": items, "total": _count(con, f"SELECT count(*) FROM records WHERE {open_only}", (project_id, *closed))}


def _project_progress(con: sqlite3.Connection, project_id: str, today: str) -> dict:
    states, statuses = _project_tallies(con, project_id)
    total = sum(states.values())
    applicable = total - states["not_applicable"]
    done = states["done"]
    waiting_on = _mapped(
        con,
        _PROJECT_WAITING_SQL,
        (project_id, *OPEN_ACTION_STATUSES, project_id),
        ("person_id", "display_name", "items"),
    )
    next_due = [
        {
            "entity": row["entity"],
            "id": row["id"],
            "title": row["title"],
            "date": row["date"],
            "overdue": row["date"] < today,
        }
        for row in _due_rows(con, "project_id = ?", (project_id,), 5)
    ]
    return {
        "checklist": {
            "total": total,
            "applicable": applicable,
            "done": done,
            "percent_done": None if applicable == 0 else 100 * done // applicable,
            "by_state": states,
        },
        "actions": {
            "open": sum(statuses[status] for status in OPEN_ACTION_STATUSES),
            "by_status": statuses,
        },
        "waiting_on": waiting_on,
        "next_due": next_due,
    }


def _shrink_lists(related: dict, cap: int) -> None:
    for value in related.values():
        if isinstance(value, dict) and "items" in value:
            value["items"] = value["items"][:cap]


def _shrink_page(result: dict, field: str, size: int) -> None:
    page = result["pages"][field]
    note = (result.get("notes") or {}).get("fields", {}).get(field)
    if note is not None:
        # A record's body page lives in notes.
        text = note["text"][:size]
        note["text"] = text
    else:
        text = (result["row"].get(field) or "")[:size]
        result["row"][field] = text
    page["returned"] = len(text)
    end = page["offset"] + len(text)
    page["next_offset"] = end if end < page["total"] else None


def _fit_get(result: dict, page_field: str | None) -> dict:
    if _fits(result, GET_BUDGET):
        return result
    result["truncated"] = True
    _shrink_lists(result["related"], 5)
    if page_field is not None and not _fits(result, GET_BUDGET):
        _shrink_page(result, page_field, 2000)
    return result


# Each note entity's fields in NOTE_FIELDS order.
_NOTE_GET = {entity: schemas.NOTE_FIELDS[name] for name, entity in schemas.NOTE_ENTITY.items()}


def _notes(con: sqlite3.Connection, entity: str, subject_id: str, typed: dict) -> dict | None:
    """Every note field leaves the row. The non-empty ones come back labelled, with who wrote them
    and when from note_meta (null for a note written before 0.7.0a2)."""
    meta = {
        row[0]: (row[1], row[2])
        for row in _query(
            con,
            "SELECT field, written_by, written_at FROM note_meta WHERE entity = ? AND ref_id = ?",
            (entity, subject_id),
        )
    }
    fields = {}
    for name in _NOTE_GET[entity]:
        text = typed.pop(name, None)
        if text is None or text == "":
            continue
        by, at = meta.get(name, (None, None))
        fields[name] = {"text": text, "by": by, "at": at}
    if not fields:
        return None
    return {"label": strict.NOTE_LABEL, "fields": fields}


def _get(con: sqlite3.Connection, arguments: dict, now: str, zone: str) -> ToolResult:
    entity = arguments["entity"]
    subject_id = arguments["id"]
    if arguments["include_history"] and entity not in {"record", "fact"}:
        raise ToolError(
            "invalid_arguments",
            "include_history is only for records and facts",
            path="/include_history",
        )
    field = arguments.get("field")
    page_field = _PAGE_FIELD.get(entity)
    if field is not None and field != page_field:
        raise ToolError("invalid_arguments", "field is not pageable for this entity", path="/field")
    if arguments["offset"] and field is None:
        raise ToolError("invalid_arguments", "offset needs field", path="/offset")
    if entity == "fact":
        keys = _query(con, "SELECT * FROM fact_keys WHERE key = ?", (subject_id,))
        if not keys:
            raise ToolError("not_found", advice.not_found_error("fact", subject_id))
        facts = _query(con, _FACT_SELECT + "WHERE f.key = ?", (now, subject_id))
        if facts:
            typed = _fact_item(facts[0])
            if typed["status"] == "disputed" or (
                typed["status"] == "retired" and _last_in_force_status(con, subject_id) == "disputed"
            ):
                typed["value"] = "disputed"
            source = _cited_source(con, facts[0]["source_id"], facts[0]["locator"])
        else:
            typed = None
            source = None
        related = _related(con, entity, keys[0])
        pages = None
    elif entity == "claim":
        found = _query(con, "SELECT * FROM claims WHERE id = ?", (subject_id,))
        if not found:
            raise ToolError("not_found", f"no claim {subject_id}")
        claim = found[0]
        typed = {
            "id": claim["id"],
            "key": claim["key"],
            "scope": claim["scope"],
            "value": units.typed(claim["value_type"], claim),
            "value_type": claim["value_type"],
            "value_raw": claim["value_raw"],
            "status": claim["status"],
            "basis": claim["basis"],
            "as_of": claim["as_of"],
            "quote": claim["quote"],
            "checked": bool(claim["checked"]),
            "supersedes": json.loads(claim["supersedes"]),  # trusted: claims.supersedes, written by the applier
            "proposed_by": claim["proposed_by"],
            "superseded_by": claim["superseded_by"],
            "resolution_source_id": claim["resolution_source_id"],
            "resolution_locator": claim["resolution_locator"],
            "created_at": claim["created_at"],
            "created_by": claim["created_by"],
            "updated_at": claim["updated_at"],
            "updated_by": claim["updated_by"],
        }
        source = _cited_source(con, claim["source_id"], claim["locator"])
        fact = _one(con, "SELECT key, status FROM facts WHERE key = ?", (claim["key"],), ("key", "status"))
        resolution = None
        if claim["resolution_source_id"] is not None:
            resolution = _cited_source(con, claim["resolution_source_id"], claim["resolution_locator"])
        related = {"fact": fact, "resolution_source": resolution}
        pages = None
    else:
        table = _GET_TABLE[entity]
        rows = _query(con, f"SELECT * FROM {table} WHERE id = ?", (subject_id,))
        if not rows:
            raise ToolError("not_found", advice.not_found_error(entity, subject_id))
        if field is not None:
            typed, pages = _typed_row(rows[0], page_field, arguments["offset"], _PAGE_CHARS)
            page = pages[page_field]
            paged = {
                "entity": entity,
                "id": subject_id,
                "field": field,
                "text": typed[page_field],
                "offset": page["offset"],
                "returned": page["returned"],
                "total": page["total"],
                "next_offset": page["next_offset"],
            }
            if entity in _NOTE_GET:
                paged["notes_label"] = strict.NOTE_LABEL
            return _ok(paged)
        typed, pages = _typed_row(rows[0], page_field, 0, _PAGE_CHARS)
        if entity in {"action", "checklist_item"}:
            typed = _with_people(con, entity, subject_id, typed)
        notes = _notes(con, entity, subject_id, typed) if entity in _NOTE_GET else None
        # A person's or thread's own source reads as NULL under a folder token when no visible row cites it.
        source = None if entity == "source" or rows[0]["source_id"] is None else _cited_source(con, rows[0]["source_id"], rows[0]["locator"])
        related = _related(con, entity, rows[0])
        if entity in _LINK_ENTITIES:
            related["sources"] = _row_sources(con, _LINK_ENTITIES[entity], rows[0])
        elif entity == "source":
            related["cited_by"] = _cited_by(con, subject_id)
        if entity == "project":
            related["progress"] = _project_progress(con, subject_id, _today(now, zone))
    result = {
        "entity": entity,
        "id": subject_id,
        "row": typed,
        "source": source,
        "related": related,
    }
    if pages is not None:
        result["pages"] = pages
    if arguments["include_history"]:
        result["history"] = _history(con, entity, subject_id)
    result["truncated"] = False
    if entity in _MARKER_ENTITIES:
        unchecked = _unchecked_summary(con, entity, subject_id)
        if unchecked is not None:
            result["unchecked"] = unchecked
    if entity in _NOTE_GET and notes is not None:
        result["notes"] = notes
    return _ok(_fit_get(result, page_field if pages is not None else None))


SIGNALS_SQL = (
    "SELECT s.link_id, s.action_id, s.checklist_item_id, s.thread_id, s.channel, s.role,\n"
    "       s.reply_state, s.new_message, l.since, t.last_inbound_at, t.last_outbound_at,\n"
    "       coalesce(a.title, c.title) AS subject_title,\n"
    "       coalesce(a.project_id, c.project_id) AS project_id\n"
    "FROM thread_link_state AS s\n"
    "JOIN thread_links AS l ON l.id = s.link_id\n"
    "JOIN threads AS t ON t.id = s.thread_id\n"
    "LEFT JOIN actions AS a ON a.id = s.action_id\n"
    "LEFT JOIN checklist_items AS c ON c.id = s.checklist_item_id\n"
    "ORDER BY s.link_id"
)
STORED_SIGNALS_SQL = (
    "SELECT g.id, g.kind, g.subject_table, g.subject_id, g.thread_id, g.detail, g.basis, g.created_at,\n"
    "       coalesce(a.title, c.title, d.title, r.title, p.title, f.key) AS subject_title,\n"
    "       coalesce(a.project_id, c.project_id, d.project_id, r.project_id, p.id) AS project_id\n"
    "FROM signals AS g\n"
    "LEFT JOIN actions AS a ON g.subject_table = 'actions' AND a.id = g.subject_id\n"
    "LEFT JOIN checklist_items AS c ON g.subject_table = 'checklist_items' AND c.id = g.subject_id\n"
    "LEFT JOIN decisions AS d ON g.subject_table = 'decisions' AND d.id = g.subject_id\n"
    "LEFT JOIN records AS r ON g.subject_table = 'records' AND r.id = g.subject_id\n"
    "LEFT JOIN projects AS p ON g.subject_table = 'projects' AND p.id = g.subject_id\n"
    "LEFT JOIN fact_keys AS f ON g.subject_table = 'facts' AND f.key = g.subject_id\n"
    "WHERE g.status = 'open'\n"
    "ORDER BY g.created_at DESC, g.id"
)
_SIGNAL_KIND_ORDER = {"reply": 0, "owes_reply": 1, "new_message": 2}


def _link_subject(row) -> tuple[str, str]:
    if row["action_id"] is not None:
        return "action", row["action_id"]
    return "checklist_item", row["checklist_item_id"]


def _derived_kind(row) -> str | None:
    role = row["role"]
    state = row["reply_state"]
    fresh = row["new_message"] == 1
    if role == "awaiting_reply" and state == "replied" and fresh:
        return "reply"
    if role == "owes_reply" and state == "waiting":
        return "owes_reply"
    if role == "owes_reply" and fresh:
        return "new_message"
    if role == "event" and fresh:
        return "new_message"
    return None


def _signal_payload(signals: list, counts: dict, unknown: int, total: int, truncated: bool) -> dict:
    return {
        "signals": signals,
        "counts": counts,
        "unknown_outbound": unknown,
        "total": total,
        "truncated": truncated,
    }


def _signals(con: sqlite3.Connection, arguments: dict) -> ToolResult:
    links = _query(con, SIGNALS_SQL)
    stored_rows = _query(con, STORED_SIGNALS_SQL)
    project = arguments.get("project_id")
    projects = arguments.get("_projects")  # the brief under a folder: every project in it
    subject = arguments.get("subject")
    unknown = 0
    derived = []
    for row in links:
        entity, subject_id = _link_subject(row)
        if project is not None and row["project_id"] != project:
            continue
        if projects is not None and row["project_id"] not in projects:
            continue
        if subject is not None and (entity, subject_id) != (subject["entity"], subject["id"]):
            continue
        if row["role"] == "owes_reply" and row["reply_state"] == "unknown":
            unknown += 1
        kind = _derived_kind(row)
        if kind is None:
            continue
        derived.append(
            {
                "kind": kind,
                "derived": True,
                "subject": {"entity": entity, "id": subject_id, "title": row["subject_title"]},
                "project_id": row["project_id"],
                "thread_id": row["thread_id"],
                "link_id": row["link_id"],
                "channel": row["channel"],
                "since": row["since"],
                "last_inbound_at": row["last_inbound_at"],
                "last_outbound_at": row["last_outbound_at"],
                "stored_signal_id": None,
            }
        )
    derived.sort(key=lambda item: item["link_id"])
    derived.sort(key=lambda item: item["last_inbound_at"] or "", reverse=True)
    derived.sort(key=lambda item: _SIGNAL_KIND_ORDER[item["kind"]])
    stored = []
    for row in stored_rows:
        entity = _SUBJECT_ENTITY[row["subject_table"]]
        stored.append(
            {
                "kind": row["kind"],
                "derived": False,
                "subject": {"entity": entity, "id": row["subject_id"], "title": row["subject_title"]},
                "project_id": row["project_id"],
                "thread_id": row["thread_id"],
                "signal_id": row["id"],
                "detail": row["detail"],
                "basis": row["basis"],
                "created_at": row["created_at"],
            }
        )
    for item in stored:
        if item["kind"] not in {"reply", "new_message"}:
            continue
        for prior in derived:
            if prior["stored_signal_id"] is not None:
                continue
            if (
                prior["kind"] == item["kind"]
                and prior["subject"]["entity"] == item["subject"]["entity"]
                and prior["subject"]["id"] == item["subject"]["id"]
                and prior["thread_id"] == item["thread_id"]
            ):
                prior["stored_signal_id"] = item["signal_id"]
                item["folded"] = True
                break
    kinds = set(arguments["kinds"]) if arguments.get("kinds") else None
    kept_all = []
    for item in derived + [item for item in stored if not item.get("folded")]:
        if kinds is not None and item["kind"] not in kinds:
            continue
        if project is not None and item["project_id"] != project:
            continue
        if projects is not None and item["project_id"] not in projects:
            continue
        if subject is not None and (item["subject"]["entity"], item["subject"]["id"]) != (
            subject["entity"],
            subject["id"],
        ):
            continue
        if "folded" in item:
            del item["folded"]
        kept_all.append(item)
    counts = {"reply": 0, "new_message": 0, "owes_reply": 0, "stored": 0}
    for item in kept_all:
        if item["derived"]:
            counts[item["kind"]] += 1
        else:
            counts["stored"] += 1
    total = len(kept_all)
    shown = kept_all[: arguments["limit"]]
    truncated = len(shown) < total
    chosen: list = []
    for item in shown:
        trial = _signal_payload(chosen + [item], counts, unknown, total, truncated)
        if not _fits(trial):
            truncated = True
            break
        chosen.append(item)
    result = _signal_payload(chosen, counts, unknown, total, truncated)
    while not _fits(result) and chosen:
        chosen.pop()
        result = _signal_payload(chosen, counts, unknown, total, True)
    return _ok(result)


def _today(now: str, zone: str) -> str:
    stamp = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return stamp.astimezone(ZoneInfo(zone)).date().isoformat()


_LINE_BYTES = 200  # the most a brief line may take, in UTF-8 bytes


def _clip_line(line: str, limit: int = _LINE_BYTES) -> str:
    return single_line(line, max_bytes=limit, ellipsis=ELLIPSIS)


def _fit_brief(lines: list[str], items: set[int]) -> None:
    """Cut the new sections' item lines to one shared byte cap when the brief would pass BRIEF_BYTES.

    The fixed lines are never cut further than _clip_line's 200 bytes. With every id, name and key at
    its schema maximum the brief can pass 4,096 bytes, so the item lines give way, and only then.
    """
    sizes = [len(line.encode()) + 1 for line in lines]
    if sum(sizes) <= BRIEF_BYTES:
        return
    fixed = sum(size for index, size in enumerate(sizes) if index not in items)
    cap = max(
        (cap for cap in range(4, 201) if fixed + sum(min(sizes[i] - 1, cap) + 1 for i in items) <= BRIEF_BYTES),
        default=4,
    )
    for index in items:
        lines[index] = _clip_line(lines[index], cap)


def _cut(value: str, limit: int) -> str:
    """At most limit characters, ending in ... when cut."""
    return single_line(value, max_chars=limit, ellipsis=ELLIPSIS)


def _cut_bytes(value: str, limit: int) -> str:
    """At most limit bytes of UTF-8, ending in ... when cut."""
    return single_line(value, max_bytes=limit, ellipsis=ELLIPSIS)


def _waiting_names(con: sqlite3.Connection, entity: str, row_id: str) -> str:
    list_table, list_key = _PEOPLE_LIST_TABLES[entity]
    # A name that collapses to nothing prints the person id instead.
    names = [
        single_line(row[1]) or row[0]
        for row in _query(
            con,
            f"SELECT p.id, p.display_name FROM {list_table} AS l JOIN people AS p ON p.id = l.person_id "
            f"WHERE l.{list_key} = ? AND l.role = 'waiting_on' ORDER BY l.position",
            (row_id,),
        )
    ]
    shown = ", ".join(names[:3])
    return shown + (f" +{len(names) - 3}" if len(names) > 3 else "")


def _brief_sections(con: sqlite3.Connection, today: str, branch: list[str] | None = None) -> list[str]:
    """The due, waiting and active project sections: each a header line, then item lines."""
    lines: list[str] = []
    horizon = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=7)).date().isoformat()
    inside, inside_params = _in_branch("project_id", branch)
    due = _due_rows(con, "date <= ?" + inside, (horizon, *inside_params))
    if not due:
        lines.append(f"Due by {horizon}: nothing open.")
    else:
        late = sum(1 for row in due if row["date"] < today)
        header = f"Due by {horizon}: {len(due)} open" + (f" ({late} overdue)." if late else ".")
        lines.append(header + (" Showing the first 6." if len(due) > 6 else ""))
    for row in due[:6]:
        flag = " overdue" if row["date"] < today else ""
        line = f"- {row['id']} {row['date']}{flag}: {_cut(row['title'], 70)} [{row['project_id']}]"
        names = _waiting_names(con, row["entity"], row["id"])
        lines.append(line + (f"; waiting on {names}" if names else ""))

    waits = _query(
        con,
        "SELECT id, title, asked_on, status FROM actions WHERE status IN ('waiting', 'sent')" + inside
        + " ORDER BY asked_on IS NULL, asked_on, id",
        inside_params,
    )
    if not waits:
        lines.append("Waiting: none.")
    else:
        sent = sum(1 for row in waits if row["status"] == "sent")
        count = f"{len(waits)} {'action' if len(waits) == 1 else 'actions'}" + (f", {sent} sent." if sent else ".")
        lines.append(f"Waiting: {count}" + (" Oldest 5 first." if len(waits) > 5 else ""))
    for row in waits[:5]:
        names = _waiting_names(con, "action", row["id"]) or "no one named"
        lines.append(
            f"- {row['id']} since {row['asked_on'] or '?'}: {_cut(row['title'], 70)}; waiting on {names}"
        )

    own, own_params = _in_branch("id", branch)
    projects = _query(
        con,
        "SELECT id, title, goal_date FROM projects WHERE kind = 'project' AND status = 'active'" + own
        + " ORDER BY sort_order, id",
        own_params,
    )
    if not projects:
        lines.append("Active projects: none.")
    else:
        lines.append(f"Active projects: {len(projects)}." + (" Showing the first 4." if len(projects) > 4 else ""))
    for row in projects[:4]:
        states, statuses = _project_tallies(con, row["id"])
        applicable = sum(states.values()) - states["not_applicable"]
        if applicable:
            checklist = (
                f"Checklist {states['done']}/{applicable} done, {states['waiting']} waiting, "
                f"{states['blocked']} blocked."
            )
        else:
            checklist = "No checklist."
        open_n = sum(statuses[status] for status in OPEN_ACTION_STATUSES)
        title = _cut(row["title"], 50)
        title += " " if title.endswith((".", "?", "!")) else ". "
        line = f"- {row['id']}: {title}{checklist} {open_n} open action{'' if open_n == 1 else 's'}."
        lines.append(line + (f" Goal {row['goal_date']}." if row["goal_date"] is not None else ""))
    return lines


_DISPUTE_LINE_BYTES = 200
_CONFLICTS_SQL = (
    "SELECT id, subject_id FROM signals WHERE kind = 'conflict' AND status = 'open' "
    "AND subject_table = 'facts' ORDER BY created_at DESC, subject_id, id"
)
# Candidate updates: a current history row after a current one. The value test is done on canonical
# values in Python, so a better-ranked supporting claim that moves value_text isn't a change.
_CHANGED_SQL = (
    "SELECT h.key, h.recorded_at, "
    "h.value_type AS h_value_type, h.value_text AS h_value_text, h.value_int AS h_value_int, "
    "h.value_real AS h_value_real, h.value_date AS h_value_date, "
    "p.value_type AS p_value_type, p.value_text AS p_value_text, p.value_int AS p_value_int, "
    "p.value_real AS p_value_real, p.value_date AS p_value_date "
    "FROM fact_history AS h JOIN fact_history AS p ON p.key = h.key AND p.version = h.version - 1 "
    "WHERE h.change = 'updated' AND h.status = 'current' AND h.recorded_at >= ? AND p.status = 'current'"
)
_HISTORY_VALUE = ("value_type", "value_text", "value_int", "value_real", "value_date")


def _changed_keys(con: sqlite3.Connection, since: str) -> list[str]:
    newest: dict[str, str] = {}
    for row in _query(con, _CHANGED_SQL, (since,)):
        before = {name: row[f"p_{name}"] for name in _HISTORY_VALUE}
        after = {name: row[f"h_{name}"] for name in _HISTORY_VALUE}
        if truth.canonical(row["key"], before) == truth.canonical(row["key"], after):
            continue
        if row["recorded_at"] > newest.get(row["key"], ""):
            newest[row["key"]] = row["recorded_at"]
    keys = sorted(newest)
    keys.sort(key=lambda key: newest[key], reverse=True)  # newest first; stable, so ties stay by key
    return keys


def _dispute_line(key: str, value_type: str, unit: str, bucket) -> str:
    """One dispute line of at most 200 bytes that keeps every shown value and the tail."""
    values = bucket.values()
    shown = [best for _value, best, _count in values[:2]]
    more = len(values) - len(shown)

    def build(n: int, kinds: bool, with_units: bool, key_text: str) -> str:
        parts = []
        for best in shown:
            part = _shown_value(value_type, unit if with_units else "", best, n)
            if kinds:
                part += f", {best['source_kind']} {best['source_date']}"
            parts.append(part)
        body = " | ".join(parts) + (f" | +{more} more" if more > 0 else "")
        text = f"Disputed {key_text} ({bucket.label}): {body}"
        return text + (" Ask a person." if text.endswith("...") else ". Ask a person.")

    for kinds, with_units in ((True, True), (False, True), (False, False)):
        for n in range(40, 7, -1):
            line = build(n, kinds, with_units, key)
            if len(line.encode()) <= _DISPUTE_LINE_BYTES:
                return line
    for size in range(len(key) - 1, 2, -1):
        line = build(8, False, False, _cut_bytes(key, size))
        if len(line.encode()) <= _DISPUTE_LINE_BYTES:
            return line
    return _clip_line(build(8, False, False, key))


def _truth_lines(con: sqlite3.Connection, now: str, truth_config) -> list[str]:
    """The dispute lines, the count line and the change line, after Facts:."""
    lines: list[str] = []
    states: dict = {}
    disputed: list[tuple[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in _query(con, _CONFLICTS_SQL):
        key = row["subject_id"]
        if key not in states:
            registered = _query(con, "SELECT value_type, unit FROM fact_keys WHERE key = ?", (key,))
            states[key] = (truth.state(con, key, truth_config), registered[0]) if registered else None
        if states[key] is None:
            continue
        state, _registry_row = states[key]
        if truth.is_legacy(row["id"]):
            bucket = state.now
        else:
            bucket = next(
                (item for item in state.buckets() if row["id"].startswith(truth.signal_prefix(key, item.label))),
                None,
            )
        if bucket is None or not bucket.disputed or (key, bucket.label) in seen:
            continue
        seen.add((key, bucket.label))
        disputed.append((key, bucket))
    for key, bucket in disputed[:3]:
        registry = states[key][1]
        lines.append(_dispute_line(key, registry["value_type"], registry["unit"], bucket))
    if len(disputed) > 3:
        lines.append(f"{len(disputed) - 3} more disputed. records_query on signal with kind conflict lists them.")
    since = (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    changed = _changed_keys(con, since)
    if changed:
        listed = ", ".join(changed[:3]) + (f", +{len(changed) - 3} more" if len(changed) > 3 else "")
        noun = "fact" if len(changed) == 1 else "facts"
        lines.append(f"Changed in the last 7 days: {len(changed)} {noun} ({listed}).")
    return lines


_HELD_LINE_BYTES = 200


def _held_line(con: sqlite3.Connection, now: str) -> str | None:
    """How many packets wait for a person, and which would open a conflict."""
    rows = _query(
        con,
        "SELECT change_id, decided_at, hold_reason FROM write_log WHERE status = 'held' "
        "ORDER BY decided_at, change_id",
    )
    if not rows:
        return None
    conflicting = 0
    keys: list[str] = []
    for row in rows:
        found = schemas.HELD_CONFLICT.match(row["hold_reason"] or "")
        if found is None:
            continue
        conflicting += 1
        if found.group(1) not in keys:
            keys.append(found.group(1))
    oldest = datetime.strptime(rows[0]["decided_at"], "%Y-%m-%dT%H:%M:%SZ")
    days = max(0, int((datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - oldest).total_seconds() // 86400))
    age = "Oldest under a day." if days == 0 else ("Oldest 1 day." if days == 1 else f"Oldest {days} days.")

    def build(size: int | None) -> str:
        part = ""
        if conflicting:
            shown = [key if size is None else _cut_bytes(key, size) for key in keys[:2]]
            listed = ", ".join(shown) + (f", +{len(keys) - 2} more" if len(keys) > 2 else "")
            part = f" ({conflicting} would open a conflict: {listed})"
        return f"Held: {len(rows)} waiting for a person{part}. {age} Ask the person to review: brain held."

    line = build(None)
    if len(line.encode()) > _HELD_LINE_BYTES:
        for size in range(120, 7, -1):
            line = build(size)
            if len(line.encode()) <= _HELD_LINE_BYTES:
                break
    return line


_INTAKE_CAUSES = ("auth", "network", "folder", "limit", "config", "other")
_INTAKE_PAGE = 200


def _utc(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")


def _recent_runs(con: sqlite3.Connection, name: str, gap: float):
    """A collector's runs, newest first, each (started_at of its first row, status, error prefix of its
    newest failed row). Rows seconds apart belong to one run (brain-fetch writes one row per export);
    a row at least gap minutes after the previous one starts a new run. Read in pages, newest first."""
    offset = 0
    current: list = []
    while True:
        rows = _query(
            con,
            "SELECT started_at, status, substr(error, 1, 20) FROM collector_runs WHERE collector = ? "
            "ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (name, _INTAKE_PAGE, offset),
        )
        for row in rows:
            if current and (_utc(current[-1][0]) - _utc(row[0])).total_seconds() >= gap * 60:
                yield _run_of(current)
                current = []
            current.append(row)
        if len(rows) < _INTAKE_PAGE:
            break
        offset += _INTAKE_PAGE
    if current:
        yield _run_of(current)


def _run_of(rows: list) -> tuple[str, str, str]:
    """rows are newest first: the run starts at the last, is an error if any row is, else has the first's
    status, and reports the error text of its newest error row."""
    failed = [row for row in rows if row[1] == "error"]
    status = "error" if failed else rows[0][1]
    return rows[-1][0], status, failed[0][2] if failed else ""


def _pulled(collector) -> bool:
    """A collector brain-fetch runs on a schedule, so silence is news."""
    options = collector.options
    return bool(options.get("live_folder") or options.get("service", "documents") != "documents"
                or options.get("live_calendar_id"))


def intake_status(con: sqlite3.Connection, now: str, config) -> list[dict]:
    """The collectors that are failing or silent, in name order. The brief's Intake
    line and brain-fetch check both read it, so they can't disagree."""
    found = []
    settings = config.fetch
    gap = min(10, settings.every_minutes / 2)
    window = max(settings.stale_after_runs * settings.every_minutes, 120)
    for name in sorted(config.collectors):
        collector = config.collectors[name]
        newest = None
        failed: list[tuple[str, str, str]] = []
        for run in _recent_runs(con, name, gap):
            if newest is None:
                newest = run
            if run[1] != "error":
                break
            failed.append(run)
        if newest is None:
            continue
        zone = config.timezone
        if len(failed) >= settings.stale_after_runs:
            text = failed[0][2]
            cause = text.split(":", 1)[0] if ":" in text else ""
            found.append(
                {
                    "collector": name,
                    "state": "failing",
                    "since": _today(failed[-1][0], zone),
                    "cause": cause if cause in _INTAKE_CAUSES else "",
                    "runs": len(failed),
                }
            )
        elif _pulled(collector):
            age = (_utc(now) - _utc(newest[0])).total_seconds() / 60
            if age > window:
                found.append(
                    {"collector": name, "state": "silent", "since": _today(newest[0], zone), "cause": "", "runs": 0}
                )
    return found


def _intake_line(entries: list[dict]) -> str | None:
    if not entries:
        return None

    def build(size: int | None) -> str:
        parts = []
        for entry in entries[:3]:
            name = entry["collector"] if size is None else _cut_bytes(entry["collector"], size)
            if entry["state"] == "failing":
                cause = f" ({entry['cause']})" if entry["cause"] else ""
                parts.append(f"{name} failing since {entry['since']}{cause}")
            else:
                parts.append(f"{name} silent since {entry['since']}")
        listed = ", ".join(parts) + (f", +{len(entries) - 3} more" if len(entries) > 3 else "")
        return f"Intake: {listed}. Ask the person to run brain-fetch check."

    line = build(None)
    if len(line.encode()) > _HELD_LINE_BYTES:
        for size in range(40, 7, -1):
            line = build(size)
            if len(line.encode()) <= _HELD_LINE_BYTES:
                break
    return line


def update_line(updates, state: dict) -> str | None:
    """The update notice, from update.json alone, so the brief stays offline and repeatable.
    At most 160 bytes, and only when there is something to say."""
    current, result = state.get("current"), state.get("last_result")
    if result in ("rolled_back", "paused") and isinstance(state.get("last_target"), str):
        return _cut_bytes(f"Last update to {state['last_target']} failed and was rolled back: see brain update --status.", 160)
    if updates.auto:
        return None
    key = "latest_beta" if updates.channel == "beta" else "latest_stable"
    latest = state.get(key)
    if not isinstance(latest, str) or not isinstance(current, str) or not versions.newer(latest, current):
        return None
    channel = "beta" if versions.is_pre(latest) else "stable"
    if updates.policy == "agent":
        return _cut_bytes(f"Update: {latest} available ({channel}). You may run brain update.", 160)
    return _cut_bytes(f"Update: {latest} available ({channel}). Ask the person before running brain update.", 160)


# Yours: open work the principal leads and waits on no one for. Waiting and sent are waits; a blocked action
# counts as a wait once it names a person.
_YOURS_STATUSES = ("open", "prepared", "scheduled", "blocked")


def _overdue_text(con: sqlite3.Connection, today: str, principal: str | None, branch: list[str] | None = None) -> str:
    """Overdue open actions, split into the principal's own and those waiting on others."""
    marks = ", ".join("?" for _ in OPEN_ACTION_STATUSES)
    inside, inside_params = _in_branch("project_id", branch)
    overdue = _query(
        con,
        f"SELECT id, status FROM actions WHERE status IN ({marks}) AND due_date IS NOT NULL AND due_date < ?{inside}",
        (*OPEN_ACTION_STATUSES, today, *inside_params),
    )
    if not overdue:
        return "Overdue: none."
    if principal is None:
        return f"Overdue: {len(overdue)}."
    yours = 0
    for row in overdue:
        if row["status"] not in _YOURS_STATUSES:
            continue
        roles = {role for (role,) in _query(
            con, "SELECT role FROM action_people WHERE action_id = ? AND (role = 'waiting_on' OR person_id = ?)",
            (row["id"], principal),
        )}
        if "owner" in roles and "waiting_on" not in roles:
            yours += 1
    return f"Overdue: {yours} yours, {len(overdue) - yours} waiting on others."


def _unchecked_counts_sql(branch: list[str] | None) -> tuple[str, tuple]:
    """Unchecked rows by entity, across the brain or only those filed in the branch."""
    if branch is None:
        return "SELECT entity, count(*) FROM unchecked_text GROUP BY entity", ()
    filed = " OR ".join(
        f"(u.entity = '{entity}' AND u.ref_id IN (SELECT id FROM {table} WHERE {column} IN (SELECT value FROM json_each(?))))"
        for entity, (table, column) in _FILED.items()
    )
    return f"SELECT u.entity, count(*) FROM unchecked_text AS u WHERE {filed} GROUP BY u.entity", (json.dumps(branch),) * len(_FILED)


def _folder_line(con: sqlite3.Connection, project_id: str, branch: list[str], scoped: bool = False) -> str:
    title = _cut(_query(con, "SELECT title FROM projects WHERE id = ?", (project_id,))[0][0], 60)
    inside = len(branch) - 1
    return (
        f"Folder {project_id}: {title}, with {inside} folder{'' if inside == 1 else 's'} inside. "
        + ("Counts and lists cover only it." if scoped else "Counts and lists cover only it; facts are brain-wide.")
    )


def _folders_line(con: sqlite3.Connection, folders: tuple[str, ...], visible: list[str]) -> str:
    """The folder token's line after the header: the folders it names, how many more sit inside them, and that facts
    aren't shown."""
    titles = dict(_query(con, "SELECT id, title FROM projects"))
    named = [folder for folder in folders if folder in titles]
    inside = len(visible) - len(named)
    tail = f"with {inside} folder{'' if inside == 1 else 's'} inside. This token reads only these folders; facts aren't shown."
    for width in (40, 0):
        listed = ", ".join(f"{folder} ({_cut(titles[folder], width)})" if width else folder for folder in named)
        line = f"Folders: {listed or ', '.join(folders)}, {tail}"
        if len(line.encode()) <= _LINE_BYTES:
            break
    return line


def _brief(
    con: sqlite3.Connection, instance_dir: Path, now: str, under: str | None = None, folders: tuple[str, ...] | None = None
) -> ToolResult:
    config = load_config(instance_dir / "brain.toml")
    scoped = folders is not None
    visible = _visible_folders(con) if scoped else []
    if scoped:
        # Under a folder token every count is over the visible branch, and under must be a folder inside it.
        branch = visible if under is None else _branch_ids(con, under)
    else:
        branch = None if under is None else _branch_ids(con, under)
    version = _query(con, "PRAGMA user_version")[0][0]
    today = _today(now, config.timezone)
    head, tail = f"{config.name} brain", f", schema {version}, today {today} ({config.timezone})."
    # A long org gives way, never the date after it.
    room = _LINE_BYTES - len(f"{head} for {tail}".encode())
    serves = f" for {single_line(config.org, max_bytes=room, ellipsis=ELLIPSIS)}" if config.org else ""
    lines = [head + serves + tail]
    if scoped:
        lines.append(_folders_line(con, folders, visible))
    if under is not None:
        lines.append(_folder_line(con, under, branch, scoped))
    own, own_params = _in_branch("id", branch)
    inside, inside_params = _in_branch("project_id", branch)
    if (instance_dir / "PAUSED").exists():
        lines.append("PAUSED. Writes are stopped until a person investigates and deletes PAUSED.")
    if (instance_dir / "UPDATING").exists():
        lines.append("Updating; writes resume shortly.")
    if not scoped:
        missing = store.missing_search_objects(con)
        if missing:
            lines.append("Search index is damaged. Run brain check.")
        else:
            docs, keys, indexed = store.search_counts(con)
            if not docs == keys == indexed:
                lines.append(f"Search index has {indexed} of {docs} rows indexed. Run brain search-rebuild.")
    workstreams = [
        row[0]
        for row in _query(con, f"SELECT id FROM projects WHERE kind = 'workstream'{own} ORDER BY sort_order, id", own_params)
    ]
    if not workstreams:
        shown = "none"
    elif len(workstreams) > 8:
        shown = ", ".join(workstreams[:8]) + f", +{len(workstreams) - 8} more"
    else:
        shown = ", ".join(workstreams)
    active = _count(con, f"SELECT count(*) FROM projects WHERE kind = 'project' AND status = 'active'{own}", own_params)
    lines.append(f"Workstreams: {shown}. Projects: {active} active.")
    open_n = waiting = blocked = proposed = 0
    for (status,) in _query(con, f"SELECT status FROM actions WHERE 1 = 1{inside}", inside_params):
        if status in OPEN_ACTION_STATUSES:
            open_n += 1
        if status == "waiting":
            waiting += 1
        elif status == "blocked":
            blocked += 1
        elif status == "proposed":
            proposed += 1
    lines.append(
        f"Actions: {open_n} open, {waiting} waiting, {blocked} blocked" + (f", {proposed} proposed. " if proposed else ". ")
        + _overdue_text(con, today, config.principal.person_id if config.principal else None, branch)
    )
    items = done = check_waiting = check_blocked = 0
    for (state,) in _query(con, f"SELECT state FROM checklist_items WHERE 1 = 1{inside}", inside_params):
        if state == "not_applicable":
            continue
        items += 1
        if state == "done":
            done += 1
        elif state == "waiting":
            check_waiting += 1
        elif state == "blocked":
            check_blocked += 1
    lines.append(f"Checklist: {items} items, {done} done, {check_waiting} waiting, {check_blocked} blocked.")
    unchecked = dict(_query(con, *_unchecked_counts_sql(branch)))
    if unchecked:
        total = sum(unchecked.values())
        parts = []
        for entity, singular, plural in (
            ("action", "action", "actions"),
            ("checklist_item", "checklist item", "checklist items"),
            ("record", "record", "records"),
            ("project", "project", "projects"),
        ):
            count = unchecked.get(entity, 0)
            if count:
                parts.append(f"{count} {singular if count == 1 else plural}")
        lines.append(
            f"Unchecked text: {total} {'row' if total == 1 else 'rows'} imported without passing "
            f"the evidence gate ({', '.join(parts)}). get shows the sentences."
        )
    if not scoped:
        lines.extend(_brain_wide_lines(con, now, config))
    result = _signals(con, {"limit": 100, "_projects": None if branch is None else set(branch)}).structured
    counts, unknown = result["counts"], result["unknown_outbound"]
    line = (
        f"Signals: {counts['reply']} reply, {counts['new_message']} new message, "
        f"{counts['owes_reply']} owes reply, {counts['stored']} stored"
    )
    lines.append(line + (f", {unknown} unknown." if unknown else "."))
    first = len(lines)
    lines.extend(_brief_sections(con, today, branch))
    items = {index for index in range(first, len(lines)) if lines[index].startswith("- ")}
    if not scoped:
        last = _query(
            con,
            "SELECT decided_at, agent FROM write_log WHERE status = 'applied' ORDER BY applied_seq DESC LIMIT 1",
        )
        if last:
            lines.append(f"Last change: {last[0][0]} by {last[0][1]}.")
        else:
            lines.append("Last change: none.")
    lines = [_clip_line(line) for line in lines]
    _fit_brief(lines, items)
    text = "".join(line + "\n" for line in lines)
    return ToolResult(text, None, False, None)


def _brain_wide_lines(con: sqlite3.Connection, now: str, config) -> list[str]:
    """The brief's lines about facts, held changes, intake, updates and stale facts: brain-wide, and none of them for a
    folder token. Their order sits between the checklist lines and the Signals line."""
    lines: list[str] = []
    current = disputed = 0
    for (status,) in _query(con, "SELECT status FROM facts"):
        if status == "current":
            current += 1
        elif status == "disputed":
            disputed += 1
    lines.append(f"Facts: {current} current, {disputed} disputed.")
    lines.extend(_truth_lines(con, now, config.truth))
    held = _held_line(con, now)
    if held is not None:
        lines.append(held)
    intake = _intake_line(intake_status(con, now, config)) if config.collectors else None
    if intake is not None:
        lines.append(intake)
    update = update_line(config.updates, registry.read_state())
    if update is not None:
        lines.append(update)
    stale_rows = _query(
        con,
        "SELECT f.key, max(0, CAST(julianday(?) - julianday(f.last_verified_at) AS INTEGER)) AS age "
        "FROM facts AS f JOIN fact_keys AS k ON k.key = f.key "
        "WHERE f.status <> 'retired' AND k.stale_after_days IS NOT NULL "
        "AND max(0, CAST(julianday(?) - julianday(f.last_verified_at) AS INTEGER)) > k.stale_after_days "
        "ORDER BY age DESC, f.key",
        (now, now),
    )
    stale = [(row[0], int(row[1])) for row in stale_rows]
    if not stale:
        lines.append("No stale facts.")
    elif len(stale) == 1:
        lines.append(f"1 fact stale: {stale[0][0]} ({stale[0][1]}d).")
    else:
        shown_facts = stale[:5]
        body = ", ".join(f"{key} ({age}d)" for key, age in shown_facts)
        extra = len(stale) - len(shown_facts)
        if extra:
            body += f", +{extra} more"
        lines.append(f"{len(stale)} facts stale: {body}.")
    return lines


def _finish(con: sqlite3.Connection) -> None:
    try:
        con.execute("COMMIT")
    except sqlite3.OperationalError:
        try:
            con.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass


def run_tool(
    instance_dir: Path,
    name: str,
    arguments: dict | None,
    *,
    now: str | None = None,
    opener=None,
    folders: tuple[str, ...] | None = None,
) -> ToolResult:
    """Run a read tool. With folders (a hub token's folder ids), every read sees only those folders and what is filed
    in them (brain.scope); without, nothing changes."""
    if name not in _TOOLS:
        raise UnknownTool(name)
    if arguments is None:
        arguments = {}
    schema = _TOOLS[name]["inputSchema"]
    errors = check_schema(schema, arguments)
    if errors:
        return _invalid(errors)
    if has_lone_surrogate(arguments):
        return _invalid([{"path": "", "message": "arguments hold a lone surrogate escape"}])
    arguments = _with_defaults(schema, arguments) if isinstance(arguments, dict) else arguments
    now = now or store.utc_now()
    if folders is not None:
        folders = tuple(folders)
        refusal = _folder_refusal(name, arguments, folders)
        if refusal is not None:
            return ToolResult(_compact(refusal.payload()), None, True, refusal.code)
        opener = scope.reader(folders)
    try:
        return _execute(Path(instance_dir), name, arguments, now, opener or store.connect_reader, folders)
    except ToolError as exc:
        return ToolResult(_compact(exc.payload()), None, True, exc.code)
    except sqlite3.OperationalError as exc:
        return _fail("unavailable", str(exc))
    except Exception as exc:
        traceback.print_exc()
        return _fail("internal", f"internal error ({type(exc).__name__})")


# Stores whose search objects were all present, by file and schema cookie. PRAGMA schema_version moves with every
# CREATE and DROP, and a rebuilt store is a new file, so a missing object is still found on the next search.
_SEARCH_OK: set[tuple] = set()


def _search_cookie(db: Path, con: sqlite3.Connection) -> tuple:
    info = db.stat()
    return (str(db), info.st_dev, info.st_ino, _query(con, "PRAGMA schema_version")[0][0])


# What a folder token can't ask about: facts, systems and claims aren't filed in a folder.
_UNFILED = {"fact": "facts", "system": "systems", "claim": "claims"}


def _folder_refusal(name: str, arguments: dict, folders: tuple[str, ...]) -> ToolError | None:
    if name == "fact_get":
        return ToolError("folder_token", advice.folder_token_error(folders, "facts", "fact_get"))
    entity = arguments.get("entity")
    if name in ("get", "records_query") and entity in _UNFILED:
        return ToolError(
            "folder_token", advice.folder_token_error(folders, _UNFILED[entity], f"{name} on {entity}"), path="/entity"
        )
    return None


def _execute(instance_dir: Path, name: str, arguments: dict, now: str, opener, folders=None) -> ToolResult:
    try:
        config = load_config(instance_dir / "brain.toml")
    except ConfigError as exc:
        return _fail("unavailable", f"brain.toml: {exc}")
    db = instance_dir / "brain.sqlite3"
    if not db.is_file():
        return _fail("unavailable", "no store at brain.sqlite3")
    con = opener(db)
    try:
        # A folder token's connection arrives inside the transaction that built its views, so ids and rows share a snapshot.
        if not con.in_transaction:
            con.execute("BEGIN")
        try:
            version = _query(con, "PRAGMA user_version")[0][0]
            expected = latest_version()
            if version != expected:
                return _fail(
                    "unavailable",
                    f"store is at version {version}, engine expects {expected}; run brain migrate",
                )
            if name == "search":
                seen = _search_cookie(db, con)
                if seen not in _SEARCH_OK:
                    missing = store.missing_search_objects(con)
                    if missing:
                        return _fail("unavailable", "missing search objects: " + ", ".join(missing))
                    if len(_SEARCH_OK) >= 64:
                        _SEARCH_OK.clear()
                    _SEARCH_OK.add(seen)
            con.row_factory = sqlite3.Row
            if name == "fact_get":
                return _fact_get(con, arguments, now, config.truth)
            if name == "records_query":
                return _records_query(con, arguments, now)
            if name == "search":
                return _search(con, arguments, config.timezone, folders is not None)
            if name == "get":
                return _get(con, arguments, now, config.timezone)
            if name == "signals":
                return _signals(con, arguments)
            if name == "brief":
                return _brief(con, instance_dir, now, arguments.get("under"), folders)
            raise NotImplementedError(f"{name} at {now}")
        finally:
            _finish(con)
    finally:
        con.close()
