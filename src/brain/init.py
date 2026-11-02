from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from pathlib import Path

import tomllib
from importlib.resources import files

from brain import config as config_mod
from brain import migrate
from brain import store
from brain import truth
from brain.applier import Applier, NotAllowed, Outcome
from brain.config import ConfigError
from brain.schemas import PERSON_ID, PRINCIPAL_NAME, PROJECT_ID
from brain.text import NotOneLine, single_line

MAX_PACKET_OPS = 500
_BATCH_RETRIES = 20
_NAME = re.compile(PROJECT_ID)
_NON_SLUG = re.compile(r"[^a-z0-9]+")


class InitError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def find_engine_root(start: Path) -> Path | None:
    current = Path(start).resolve()
    for path in [current, *current.parents]:
        candidate = path / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            data = tomllib.loads(candidate.read_text())
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            continue
        if data.get("project", {}).get("name") in ("agent-si", "agent-brain"):
            return path
    return None


def _toml_escape(value: str, label: str) -> str:
    if any(ord(char) < 32 for char in value):
        raise InitError(f"{label} contains a control character", 2)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _principal_slug(display_name: str) -> tuple[str, str]:
    slug = _NON_SLUG.sub("-", display_name.lower()).strip("-")[:60]
    person_id = f"per-{slug}"
    parts = display_name.split()
    first = parts[0] if parts else ""
    if not re.fullmatch(PERSON_ID, person_id) or not re.fullmatch(PRINCIPAL_NAME, first):
        raise InitError("principal display name does not produce a valid person_id or name", 2)
    return person_id, first


def _render_template(name: str, timezone: str, principal: str | None) -> str:
    template = files("brain").joinpath("templates/brain.toml.template").read_text(encoding="utf-8")
    safe_name = _toml_escape(name, "name")
    safe_tz = _toml_escape(timezone, "timezone")
    if principal is None:
        block = (
            "# [principal]\n"
            "# The person whose words evidence-gate rule 2 protects. Fill in, uncomment, then run\n"
            "# brain apply-config.\n"
            '# person_id = "per-alex-example"\n'
            '# display_name = "Alex Example"\n'
            '# names = ["Alex"]\n'
            "# aliases = []\n"
        )
    else:
        try:
            principal = single_line(principal, refuse=True, required=True)
        except NotOneLine as exc:
            raise InitError(f"principal display name {exc}", 2) from exc
        person_id, first = _principal_slug(principal)
        block = (
            "[principal]\n"
            f'person_id = "{_toml_escape(person_id, "person_id")}"\n'
            f'display_name = "{_toml_escape(principal, "principal")}"\n'
            f'names = ["{_toml_escape(first, "principal name")}"]\n'
            "aliases = []\n"
        )
    return (
        template.replace("{name}", safe_name)
        .replace("{timezone}", safe_tz)
        .replace("{principal_block}", block)
    )


ROUTER_NOTES_ON = "- Notes are agent-written and unsourced. Don't answer a fact question from them."
ROUTER_NOTES_OFF = "- Notes are off here: no new notes or records. Existing notes are unsourced."


def render_router(name: str, notes: bool = True) -> str:
    template = files("brain").joinpath("templates/ROUTER.md.template").read_text(encoding="utf-8")
    line = ROUTER_NOTES_ON if notes else ROUTER_NOTES_OFF
    return template.replace("{notes}", line).replace("{name}", name)


def _post_init_checks(instance_dir: Path, name: str) -> list[str]:
    problems = list(store.check_instance(instance_dir))
    try:
        cfg = config_mod.load_config(instance_dir / "brain.toml")
        if cfg.name != name:
            problems.append(f"instance.name is {cfg.name}, expected {name}")
    except ConfigError as exc:
        problems.append(str(exc))
    con = store.connect_readonly(instance_dir / "brain.sqlite3")
    try:
        rows = con.execute("SELECT agent, status FROM write_log").fetchall()
        # A config of more than 500 ops is applied in batches, so there may be several rows.
        if not rows or any(row[0] != "brain" or row[1] != "applied" for row in rows):
            problems.append("write_log must contain only applied brain rows")
    finally:
        con.close()
    return problems


def _upsert(op: str, row: dict, source_id: str, locator: str, base: int | None) -> dict:
    return {
        "op": op,
        "row": row,
        "source": {"source_id": source_id, "locator": locator},
        "base": base,
    }


def apply_config(instance_dir: Path) -> Outcome | None:
    instance_dir = Path(instance_dir)
    cfg = config_mod.load_config(instance_dir / "brain.toml")
    raw = (instance_dir / "brain.toml").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    source_id = f"src-config-{digest[:16]}"
    warnings: list[str] = []
    ro = store.connect_readonly(instance_dir / "brain.sqlite3")
    try:
        people = {
            row[0]: row
            for row in ro.execute("SELECT id, display_name, aliases, version FROM people")
        }
        projects = {
            row[0]: row
            for row in ro.execute(
                "SELECT id, kind, title, description, sort_order, version FROM projects"
            )
        }
        fact_keys = {
            row[0]: row
            for row in ro.execute(
                "SELECT key, value_type, unit, allowed_values, stale_after_days, "
                "description, version, value_words FROM fact_keys"
            )
        }
        source_exists = (
            ro.execute("SELECT 1 FROM sources WHERE id = ?", (source_id,)).fetchone() is not None
        )
        has_aliases = (
            ro.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'aliases'").fetchone()
            is not None
        )
        stored_aliases = store.stored_alias_rows(ro) if has_aliases else None
        extra_ws = [
            row[0]
            for row in ro.execute("SELECT id FROM projects WHERE kind = 'workstream'")
            if row[0] not in {item.id for item in cfg.workstreams}
        ]
        extra_keys = [key for key in fact_keys if key not in {item.key for item in cfg.fact_keys}]
    finally:
        ro.close()
    warnings.extend(
        f"workstream {item} is in the store but not in brain.toml" for item in extra_ws
    )
    warnings.extend(f"fact key {key} is in the store but not in brain.toml" for key in extra_keys)
    apply_config.last_warnings = warnings
    apply_config.last_batches = []

    ops: list[dict] = []
    if cfg.principal:
        stored = people.get(cfg.principal.person_id)
        aliases = list(cfg.principal.aliases)
        if stored is None:
            ops.append(
                _upsert(
                    "upsert_person",
                    {
                        "id": cfg.principal.person_id,
                        "display_name": cfg.principal.display_name,
                        "aliases": aliases,
                    },
                    source_id,
                    "principal",
                    None,
                )
            )
        elif (stored[1], json.loads(stored[2])) != (cfg.principal.display_name, aliases):  # trusted: the stored people row, written by the applier
            ops.append(
                _upsert(
                    "upsert_person",
                    {
                        "id": cfg.principal.person_id,
                        "display_name": cfg.principal.display_name,
                        "aliases": aliases,
                    },
                    source_id,
                    "principal",
                    stored[3],
                )
            )
    for item in cfg.workstreams:
        stored = projects.get(item.id)
        fields = ("workstream", item.title, item.description, item.sort_order)
        if stored is None:
            ops.append(
                _upsert(
                    "upsert_project",
                    {
                        "id": item.id,
                        "kind": "workstream",
                        "title": item.title,
                        "description": item.description,
                        "sort_order": item.sort_order,
                    },
                    source_id,
                    f"workstreams.{item.id}",
                    None,
                )
            )
        elif (stored[1], stored[2], stored[3], stored[4]) != fields:
            ops.append(
                _upsert(
                    "upsert_project",
                    {
                        "id": item.id,
                        "kind": "workstream",
                        "title": item.title,
                        "description": item.description,
                        "sort_order": item.sort_order,
                    },
                    source_id,
                    f"workstreams.{item.id}",
                    stored[5],
                )
            )
    for item in cfg.fact_keys:
        stored = fact_keys.get(item.key)
        allowed = list(item.allowed_values) if item.allowed_values is not None else None
        words = (
            None
            if item.value_words is None
            else {value: list(item.value_words[value]) for value in sorted(item.value_words)}
        )
        desired = (
            item.value_type,
            item.unit,
            allowed,
            item.stale_after_days,
            item.description,
            words,
        )
        row = {
            "key": item.key,
            "value_type": item.value_type,
            "unit": item.unit,
            "allowed_values": allowed,
            "stale_after_days": item.stale_after_days,
            "description": item.description,
            "value_words": words,
        }
        if stored is None:
            ops.append(_upsert("upsert_fact_key", row, source_id, f"fact_keys.{item.key}", None))
        else:
            stored_allowed = json.loads(stored[3]) if stored[3] is not None else None  # trusted: fact_keys.allowed_values, written by the applier
            stored_words = json.loads(stored[7]) if stored[7] is not None else None  # trusted: fact_keys.value_words, written by the applier
            if (stored[1], stored[2], stored_allowed, stored[4], stored[5], stored_words) != desired:
                ops.append(
                    _upsert("upsert_fact_key", row, source_id, f"fact_keys.{item.key}", stored[6])
                )
    desired_aliases = truth.alias_rows(cfg.truth.entities, cfg.truth.attributes)
    if stored_aliases is not None and desired_aliases != stored_aliases:
        ops.append(
            {
                "op": "set_aliases",
                "row": {
                    "aliases": [
                        {"kind": kind, "alias": alias, "target": target}
                        for kind, alias, target in desired_aliases
                    ]
                },
                "source": {"source_id": source_id, "locator": "truth"},
            }
        )
    if not ops:
        return None
    if not source_exists:
        ops.insert(
            0,
            {
                "op": "add_source",
                "row": {
                    "id": source_id,
                    "kind": "config",
                    "title": "brain.toml",
                    "source_date": store.local_today(cfg.timezone),
                    "author_person_id": None,
                    "content": raw.decode(),
                },
            },
        )
    config_hash = hashlib.sha256(raw).hexdigest()[:16]
    if len(ops) <= MAX_PACKET_OPS and not _resumed(instance_dir, config_hash):
        stamp = store.utc_now().lower().replace("-", "").replace(":", "")
        packet = {
            "packet_version": 1,
            "change_id": f"brain-config-{stamp}-{secrets.token_hex(3)}",
            "agent": "brain",
            "intent": f"apply brain.toml (sha256 {digest[:16]})",
            "ops": ops,
        }
        applier = Applier(instance_dir, allow_reserved_agent=True)
        try:
            outcome = applier.apply(json.dumps(packet).encode())
        finally:
            applier.close()
        apply_config.last_batches = [{"change_id": packet["change_id"], "ops": len(ops), "status": outcome.receipt["status"]}]
        return outcome
    return _apply_batches(instance_dir, ops, digest, config_hash)


def _resumed(instance_dir: Path, config_hash: str) -> bool:
    """Whether an earlier run already applied batches of this very brain.toml, so what is left is the rest
    of a batched apply and keeps the batch form even when it is 500 ops or fewer."""
    ro = store.connect_readonly(instance_dir / "brain.sqlite3")
    try:
        found = ro.execute(
            "SELECT 1 FROM write_log WHERE change_id GLOB ? LIMIT 1",
            (f"brain-config-{config_hash}-[0-9][0-9][0-9]-*",),
        ).fetchone()
    finally:
        ro.close()
    return found is not None


def _batch_id(instance_dir: Path, base: str, index: int) -> str:
    """base, or base-r2, base-r3 ... when earlier attempts at this batch were rejected or conflicted."""
    ro = store.connect_readonly(instance_dir / "brain.sqlite3")
    try:
        for attempt in range(1, _BATCH_RETRIES + 1):
            change_id = base if attempt == 1 else f"{base}-r{attempt}"
            row = ro.execute("SELECT status FROM write_log WHERE change_id = ?", (change_id,)).fetchone()
            if row is None or row[0] not in ("rejected", "conflict"):
                return change_id
    finally:
        ro.close()
    raise NotAllowed(
        f"apply-config refused: batch {index + 1} was rejected {_BATCH_RETRIES} times; "
        "fix the cause, or change brain.toml"
    )


def _apply_batches(instance_dir: Path, ops: list[dict], digest: str, config_hash: str) -> Outcome:
    """More than 500 ops: consecutive slices of MAX_PACKET_OPS, each its own packet, in order. The id holds
    the config hash, the index and the slice's shape, so a re-run after a partial failure (which re-splits
    only the remaining ops from index 0) can't collide with a batch already applied."""
    slices = [ops[start : start + MAX_PACKET_OPS] for start in range(0, len(ops), MAX_PACKET_OPS)]
    applier = Applier(instance_dir, allow_reserved_agent=True)
    outcome = None
    try:
        for index, chunk in enumerate(slices):
            shape = hashlib.sha256(store.canonical_json(chunk).encode()).hexdigest()[:8]
            base = f"brain-config-{config_hash}-{index:03d}-{shape}"
            change_id = _batch_id(instance_dir, base, index)
            packet = {
                "packet_version": 1,
                "change_id": change_id,
                "agent": "brain",
                "intent": f"apply brain.toml (sha256 {digest[:16]}), batch {index + 1} of {len(slices)}",
                "ops": chunk,
            }
            outcome = applier.apply(json.dumps(packet).encode())
            apply_config.last_batches.append(
                {"change_id": change_id, "ops": len(chunk), "status": outcome.receipt["status"]}
            )
            if outcome.exit_code != 0:
                break
    finally:
        applier.close()
    return outcome


apply_config.last_warnings = []
apply_config.last_batches = []


def search_rebuild(instance_dir: Path, reason: str = "manual") -> Outcome:
    instance_dir = Path(instance_dir)
    stamp = store.utc_now().lower().replace("-", "").replace(":", "")
    packet = {
        "packet_version": 1,
        "change_id": f"brain-search-{stamp}-{secrets.token_hex(3)}",
        "agent": "brain",
        "intent": f"rebuild the search index ({reason})",
        "ops": [{"op": "rebuild_search"}],
    }
    applier = Applier(instance_dir, allow_reserved_agent=True)
    try:
        return applier.apply(json.dumps(packet).encode())
    finally:
        applier.close()


def init_instance(
    name: str,
    parent: Path,
    *,
    principal: str | None = None,
    timezone: str = "UTC",
    config_text: str | None = None,
) -> dict:
    if not _NAME.fullmatch(name):
        raise InitError("instance name is invalid", 2)
    parent = Path(parent)
    if not parent.is_dir():
        raise InitError(f"{parent} is not an existing directory", 2)
    target = parent / name
    engine = find_engine_root(target)
    if engine is not None:
        raise InitError(f"instances live outside the engine repository ({engine})")
    if target.exists():
        if target.is_file():
            raise InitError(f"{target} exists and is not empty; brain init never overwrites")
        contents = list(target.iterdir())
        if contents:
            toml = target / "brain.toml"
            db = target / "brain.sqlite3"
            router = target / "ROUTER.md"
            if toml.is_file() and db.is_file() and router.is_file():
                try:
                    cfg = config_mod.load_config(toml)
                except ConfigError:
                    cfg = None
                if cfg is not None and cfg.name == name and not store.check_instance(target):
                    return {
                        "status": "already initialised",
                        "instance": str(target.resolve()),
                        "schema_version": migrate.latest_version(),
                        "router_bytes": router.stat().st_size,
                        "principal": cfg.principal.person_id if cfg.principal else None,
                        "receipt": None,
                    }
            raise InitError(f"{target} exists and is not empty; brain init never overwrites")
    temp = parent / f".{name}.init-{secrets.token_hex(4)}"
    try:
        temp.mkdir(parents=True, exist_ok=False)
        text = config_text if config_text is not None else _render_template(name, timezone, principal)
        (temp / "brain.toml").write_text(text, encoding="utf-8")
        migrate.migrate(temp / "brain.sqlite3", create=True)
        try:
            notes_on = config_mod.load_config(temp / "brain.toml").writes.notes
        except ConfigError as exc:
            raise InitError(str(exc)) from exc
        (temp / "ROUTER.md").write_text(render_router(name, notes_on), encoding="utf-8")
        outcome = apply_config(temp)
        if outcome is None or outcome.receipt.get("status") != "applied":
            reason = "apply-config did not apply"
            if outcome is not None:
                reason = outcome.receipt.get("error") or reason
            raise InitError(reason)
        problems = _post_init_checks(temp, name)
        if problems:
            raise InitError(problems[0])
        if target.exists() and target.is_dir() and not any(target.iterdir()):
            os.rmdir(target)
        os.rename(temp, target)
    except Exception as exc:
        if temp.exists():
            shutil.rmtree(temp, ignore_errors=True)
        if isinstance(exc, InitError):
            raise
        raise InitError(str(exc)) from exc
    cfg = config_mod.load_config(target / "brain.toml")
    result = {
        "status": "created",
        "instance": str(target.resolve()),
        "schema_version": migrate.latest_version(),
        "router_bytes": (target / "ROUTER.md").stat().st_size,
        "principal": cfg.principal.person_id if cfg.principal else None,
        "receipt": outcome.receipt,
    }
    if cfg.principal is None:
        result["_notice"] = (
            "no [principal] configured; evidence-gate rule 2 is off until you add one "
            "and run brain apply-config"
        )
    return result
