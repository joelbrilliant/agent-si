from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from brain.applier import Applier, Unreadable
from brain.candidates import PLANNERS, change_id, plan
from brain.config import load_config
from brain.ingress import MAX_CANDIDATE_BYTES, PACKET_NESTING, IngressError, check_size, loads
from brain.store import canonical_json, connect_reader, utc_now
from brain.validate import validate_candidate, validate_command

RELAY_RULE = (
    "Show each item to the person. Run brain approve <change_id> --as <approver> --relay only on their "
    "clear yes, and brain refuse <change_id> --as <approver> --relay --note <why> on a no."
)


@dataclass(frozen=True)
class ExtractResult:
    exit_code: int
    payload: dict
    warnings: tuple[str, ...] = ()


def run(instance_dir: Path, agent: str, raw: bytes) -> ExtractResult:
    instance_dir = Path(instance_dir)
    writer = Applier(instance_dir, extract=True)
    try:
        writer.preconditions()
        policy = writer.config.agents.get(agent)
        if policy is None or policy.kind != "extractor":
            raise Unreadable(f"agent {agent} is not an extractor")
        try:
            check_size(raw, MAX_CANDIDATE_BYTES, "candidate")
        except IngressError as exc:
            raise Unreadable(str(exc)) from None
        code, report = validate_command("candidate", raw)
        if report["status"] == "held":
            return ExtractResult(code, report, ())
        candidate = loads(raw, what="candidate", max_nesting=PACKET_NESTING)
        packet = {
            "packet_version": 1,
            "change_id": change_id(agent, candidate),
            "agent": agent,
            "intent": f"extract {candidate['kind']} from {candidate['source_id']}",
            "ops": [{"op": "apply_candidate", "candidate": candidate}],
        }
        outcome = writer.apply(canonical_json(packet).encode())
    finally:
        writer.close()
    preview = None
    if outcome.receipt["status"] == "held":
        preview = _preview(instance_dir, candidate, agent)
    warnings = (outcome.warning,) if outcome.warning else ()
    payload = {
        "change_id": outcome.receipt["change_id"],
        "kind": candidate["kind"],
        "duplicate": outcome.duplicate,
        "receipt": outcome.receipt,
        "preview": preview,
    }
    return ExtractResult(outcome.exit_code, payload, warnings)


def held(instance_dir: Path, limit: int) -> dict:
    instance_dir = Path(instance_dir)
    cfg = load_config(instance_dir / "brain.toml")
    con = connect_reader(instance_dir / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        try:
            total = con.execute(
                "SELECT count(*) FROM write_log WHERE status = 'held'"
            ).fetchone()[0]
            rows = con.execute(
                "SELECT change_id, agent, intent, decided_at, packet_json, hold_reason "
                "FROM write_log WHERE status = 'held' "
                "ORDER BY decided_at, change_id LIMIT ?",
                (limit,),
            ).fetchall()
            when = utc_now()
            items = []
            for row in rows:
                packet = json.loads(row["packet_json"])  # trusted: write_log.packet_json, the packet the applier stored
                candidate = _candidate_doc(packet)
                if candidate is None:
                    kind, preview = None, None
                else:
                    kind = candidate["kind"]
                    preview = _checked_preview(con, cfg, candidate, when, row["agent"])
                policy = cfg.agents.get(row["agent"])
                items.append(
                    {
                        "change_id": row["change_id"],
                        "agent": row["agent"],
                        "intent": row["intent"],
                        "decided_at": row["decided_at"],
                        "kind": kind,
                        "preview": preview,
                        "reason": row["hold_reason"],
                        "approvers": [] if policy is None else list(policy.approvers),
                    }
                )
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()
    return {"held": items, "more": total - len(items), "relay": RELAY_RULE}


def _candidate_doc(packet: dict) -> dict | None:
    ops = packet.get("ops")
    if not isinstance(ops, list) or len(ops) != 1 or not isinstance(ops[0], dict):
        return None
    op = ops[0]
    candidate = op.get("candidate")
    if op.get("op") != "apply_candidate" or not isinstance(candidate, dict):
        return None
    if candidate.get("kind") not in PLANNERS:
        return None
    return candidate


def _preview(instance_dir: Path, candidate: dict, agent: str) -> dict:
    cfg = load_config(instance_dir / "brain.toml")
    con = connect_reader(instance_dir / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        con.execute("BEGIN")
        try:
            body = _checked_preview(con, cfg, candidate, utc_now(), agent)
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    finally:
        con.close()
    return body


def _checked_preview(con, cfg, candidate: dict, when: str, agent: str) -> dict:
    """A held candidate's preview. One that fails validation, such as a version 1 claim candidate
    held before 0.7.0a2, is never planned."""
    errors = validate_candidate(candidate)
    if errors:
        return {"valid": False, "appliable": False, "errors": [dict(item) for item in errors], "ops": []}
    return _preview_body(plan(con, cfg, candidate, when, agent=agent))


def _preview_body(planned) -> dict:
    return {
        "valid": not planned.errors,
        "appliable": planned.appliable,
        "errors": [dict(item) for item in planned.errors],
        "ops": [] if planned.errors else [dict(op) for op in planned.ops],
    }
