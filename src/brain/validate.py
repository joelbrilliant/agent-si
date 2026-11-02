from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from brain import advice, ingress, schemas
from brain.evidence_gate import match_key
from brain.text import has_line_break


def json_pointer(parent: str, key: str | int) -> str:
    if isinstance(key, int):
        token = str(key)
    else:
        token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{token}" if parent else f"/{token}"


def json_type(value) -> str:
    if value is True or value is False:
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def check(value, schema, path: str = "") -> list[dict]:
    errors: list[dict] = []

    def add(pointer: str, message: str) -> None:
        if len(errors) < 50:
            errors.append({"path": pointer, "message": message})

    def matches_type(val, expected: str) -> bool:
        actual = json_type(val)
        if expected == "number":
            return actual in {"integer", "number"} and not isinstance(val, bool)
        if expected == "integer":
            return actual == "integer"
        return actual == expected

    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(matches_type(value, item) for item in allowed):
            add(path, f"expected {' or '.join(allowed)}")
            return errors

    if "const" in schema and value != schema["const"]:
        add(path, f"must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        add(path, advice.enum_error(schema["enum"]))
    if isinstance(value, str):
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            add(path, advice.pattern_error(schema["pattern"]))
        if "min_length" in schema and len(value) < schema["min_length"]:
            add(path, advice.size_error("characters", "minimum", schema["min_length"]))
        if "max_length" in schema and len(value) > schema["max_length"]:
            add(path, advice.size_error("characters", "maximum", schema["max_length"]))
        fmt = schema.get("format")
        if fmt == "utc":
            try:
                parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
                if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
                    raise ValueError
            except ValueError:
                add(path, advice.utc_error())
        elif fmt == "date":
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d")
                if parsed.date().isoformat() != value:
                    raise ValueError
            except ValueError:
                add(path, advice.date_error())
        elif fmt == "single_line" and has_line_break(value):
            add(path, advice.one_line_error())
        if schema.get("min_words") and len(match_key(value).split()) < schema["min_words"]:
            add(path, f"needs at least {schema['min_words']} words")
        if schema.get("no_at") and "@" in value:
            add(path, "must not contain @")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            add(path, advice.bound_error("minimum", schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            add(path, advice.bound_error("maximum", schema["maximum"]))
    if isinstance(value, list):
        if "min_items" in schema and len(value) < schema["min_items"]:
            add(path, advice.size_error("items", "minimum", schema["min_items"]))
        if "max_items" in schema and len(value) > schema["max_items"]:
            add(path, advice.size_error("items", "maximum", schema["max_items"]))
        if schema.get("unique_items") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            add(path, "has duplicate items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for i, item in enumerate(value):
                errors.extend(check(item, item_schema, json_pointer(path, i)))
                if len(errors) >= 50:
                    return errors[:50]
    if isinstance(value, dict):
        properties = schema.get("properties")
        required = schema.get("required") or []
        for name in required:
            if name not in value:
                add(json_pointer(path, name), advice.required_error((properties or {}).get(name, {}).get("enum")))
        if properties is not None:
            for name in value:
                if name not in properties:
                    add(json_pointer(path, name), advice.unknown_property_error(name, properties))
            for name, child in value.items():
                if name in properties:
                    errors.extend(check(child, properties[name], json_pointer(path, name)))
                    if len(errors) >= 50:
                        return errors[:50]
    return errors[:50]


def _format_packet_errors(errors: list[dict]) -> list[dict]:
    return errors


def _prefix_candidate(index: int, errors: list[dict]) -> list[dict]:
    prefix = f"/ops/{index}/candidate"
    return [{"path": prefix + item["path"], "message": item["message"]} for item in errors]


def validate_packet(obj) -> list[dict]:
    errors = check(obj, schemas.PACKET)
    if not isinstance(obj, dict):
        return errors
    if obj.get("change_id") and obj.get("agent"):
        change_id = obj["change_id"]
        agent = obj["agent"]
        if isinstance(change_id, str) and isinstance(agent, str):
            prefix = change_id.split("-", 1)[0]
            if prefix != agent:
                errors.append({"path": "/change_id", "message": "prefix must equal agent"})
    ops = obj.get("ops")
    if isinstance(ops, list):
        for i, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            name = op.get("op")
            if not isinstance(name, str):
                message = "op is required" if name is None else "op must be a string"
                errors.append({"path": f"/ops/{i}/op", "message": message})
                continue
            if name in schemas.COMMAND_OPS:
                errors.extend(check(op, schemas.COMMAND_OPS[name]["envelope"], f"/ops/{i}"))
                if name == "apply_candidate":
                    errors.extend(
                        _prefix_candidate(i, validate_candidate(op.get("candidate")))
                    )
                if len(ops) > 1:
                    errors.append(
                        {
                            "path": f"/ops/{i}/op",
                            "message": f"{name} must be the only op in its packet",
                        }
                    )
                continue
            if name not in schemas.OPS:
                errors.append(
                    {"path": f"/ops/{i}/op", "message": advice.unknown_op_error(name, [*schemas.OPS, *schemas.COMMAND_OPS])}
                )
                continue
            spec = schemas.OPS[name]
            errors.extend(check(op, spec["envelope"], f"/ops/{i}"))
            row = op.get("row")
            if isinstance(row, dict):
                errors.extend(check(row, spec["row"], f"/ops/{i}/row"))
                if (
                    name == "upsert_thread"
                    and row.get("channel") == "slack"
                    and isinstance(row.get("external_id"), str)
                    and re.fullmatch(schemas.SLACK_EXTERNAL_ID, row["external_id"]) is None
                ):
                    errors.append(
                        {
                            "path": f"/ops/{i}/row/external_id",
                            "message": "must be channel_id:thread_ts",
                        }
                    )
    return errors[:50]


_ISO_DAY = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
# Each candidate kind's day fields: the ones whose schema takes a day, so a new one is checked as soon as it exists.
_CANDIDATE_DAYS = {
    kind: tuple(
        name
        for name, prop in schema["properties"].items()
        if prop.get("pattern") in (schemas.PAST_DAY, schemas.FUTURE_DAY)
    )
    for kind, schema in schemas.CANDIDATE_KINDS.items()
}


def _real_date(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return parsed.date().isoformat() == value


def _candidate_structure(kind: str, fields: dict) -> list[dict]:
    errors: list[dict] = []
    for name in _CANDIDATE_DAYS.get(kind, ()):
        value = fields.get(name)
        if isinstance(value, str) and _ISO_DAY.fullmatch(value) and not _real_date(value):
            errors.append({"path": f"/fields/{name}", "message": advice.date_error()})
    if kind == "action_candidate":
        due_set = fields.get("due_date") is not None
        kind_set = fields.get("due_kind") is not None
        if due_set != kind_set:
            errors.append(
                {"path": "/fields/due_kind", "message": "must be set exactly when due_date is"}
            )
    return errors


def validate_candidate(obj) -> list[dict]:
    errors = check(obj, schemas.CANDIDATE_ENVELOPE)
    if isinstance(obj, dict):
        kind = obj.get("kind")
        fields = obj.get("fields")
        if isinstance(kind, str) and kind not in schemas.CANDIDATE_KINDS:
            errors.append({"path": "/kind", "message": f"unknown candidate kind {kind}"})
            return errors[:50]
        if isinstance(kind, str) and "candidate_version" in obj:
            version = obj["candidate_version"]
            expected = schemas.CANDIDATE_VERSIONS[kind]
            if type(version) is not int or version != expected:
                errors.append({"path": "/candidate_version", "message": f"must be {expected}"})
        if isinstance(kind, str) and isinstance(fields, dict):
            field_errors = check(fields, schemas.CANDIDATE_KINDS[kind], "/fields")
            errors.extend(field_errors)
            if not field_errors:
                errors.extend(_candidate_structure(kind, fields))
    return errors[:50]


def report(kind: str, errors: list[dict]) -> dict:
    return {
        "status": "valid" if not errors else "held",
        "kind": kind,
        "errors": errors,
    }


_CHANGE_ID = re.compile(schemas.CHANGE_ID)
_AGENT = re.compile(schemas.AGENT)
_INTENT_MAX = 500  # the write_log CHECK on intent


def envelope_error(packet: dict) -> tuple[str, str] | None:
    """The first envelope problem, as (path, message), checked in the applier's order:
    agent, change_id prefix, intent. The caller has already checked change_id."""
    agent = packet.get("agent")
    if not isinstance(agent, str) or _AGENT.fullmatch(agent) is None:
        return "/agent", "agent is missing or invalid"
    if packet["change_id"].split("-", 1)[0] != agent:
        return "/change_id", "change_id must start with the agent name and a hyphen"
    intent = packet.get("intent")
    if not isinstance(intent, str) or len(intent) > _INTENT_MAX:
        return "/intent", f"intent is missing or longer than {_INTENT_MAX} characters"
    return None


def change_id_ok(value) -> bool:
    return isinstance(value, str) and _CHANGE_ID.fullmatch(value) is not None


def _load_json(raw: bytes, kind: str) -> tuple[object | None, list[dict]]:
    try:
        return ingress.loads(raw, what="input", max_nesting=ingress.PACKET_NESTING), []
    except ingress.IngressError as exc:
        if exc.malformed:
            message = "input is not UTF-8 JSON"
        elif exc.kind == ingress.NESTING:
            message = f"{kind} {exc.issue}"
        else:
            message = str(exc)
        return None, [{"path": "", "message": message}]


def validate_command(kind: str, raw: bytes) -> tuple[int, dict]:
    obj, errors = _load_json(raw, kind)
    if errors:
        return 1, report(kind, errors)
    if kind == "packet":
        errors = validate_packet(obj)
        if isinstance(obj, dict) and change_id_ok(obj.get("change_id")):
            problem = envelope_error(obj)
            if problem is not None:
                first = {"path": problem[0], "message": problem[1]}
                errors = [first] + [item for item in errors if item["path"] != first["path"]]
                errors = errors[:50]
    else:
        errors = validate_candidate(obj)
    payload = report(kind, errors)
    return (0 if payload["status"] == "valid" else 1), payload


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2 or argv[0] not in {"packet", "candidate"}:
        print("brain: usage: brain validate packet|candidate FILE|-", file=sys.stderr)
        return 2
    kind, source = argv
    limit = ingress.MAX_PACKET_BYTES if kind == "packet" else ingress.MAX_CANDIDATE_BYTES
    try:
        raw = ingress.read_bytes(sys.stdin.buffer if source == "-" else source, limit, kind)
    except (OSError, ingress.IngressError) as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 2
    code, payload = validate_command(kind, raw)
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
