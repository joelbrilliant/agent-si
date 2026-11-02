from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from brain import evidence_gate
from brain import schemas
from brain import truth

GUARD_FUNCTION = "brain_applier_context"
GUARD_MESSAGE = "direct writes are refused: submit a change packet with brain apply"
SQLITE_MIN = (3, 38, 0)


def sqlite_too_old_message() -> str | None:
    if sqlite3.sqlite_version_info < SQLITE_MIN:
        return (
            f"SQLite {SQLITE_MIN[0]}.{SQLITE_MIN[1]}.{SQLITE_MIN[2]} or newer is required "
            f"(found {sqlite3.sqlite_version})"
        )
    return None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_today(tz: str) -> str:
    return datetime.now(ZoneInfo(tz)).date().isoformat()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def connect_readonly(db: Path) -> sqlite3.Connection:
    uri = Path(db).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10.0)


def connect_reader(db: Path) -> sqlite3.Connection:
    con = connect_readonly(db)  # Mode=ro, isolation_level=None, timeout=10.0
    con.execute("PRAGMA query_only = ON")
    con.execute("PRAGMA trusted_schema = OFF")
    return con


def tables_to_guard(con: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in con.execute(
            "SELECT name FROM pragma_table_list "
            "WHERE schema = 'main' AND type = 'table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]


def install_guards(con: sqlite3.Connection) -> None:
    tables = tables_to_guard(con)
    for table in tables:
        for event in ("insert", "update", "delete"):
            con.execute(
                f"CREATE TRIGGER IF NOT EXISTS guard_{table}_{event} "
                f"BEFORE {event.upper()} ON {table} "
                f"WHEN {GUARD_FUNCTION}() IS NOT 1 "
                f"BEGIN SELECT RAISE(ABORT, '{GUARD_MESSAGE}'); END"
            )


def missing_guards(con: sqlite3.Connection) -> list[str]:
    tables = tables_to_guard(con)
    triggers = {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_schema WHERE type = 'trigger'")
    }
    missing: list[str] = []
    for table in tables:
        for event in ("insert", "update", "delete"):
            name = f"guard_{table}_{event}"
            if name not in triggers:
                missing.append(name)
    return missing


SEARCH_SINCE = 2

# table -> (entity, key column, sync trigger suffixes)
SEARCH_TABLES = {
    "records": ("record", "id", ("ai", "au", "ad")),
    "decisions": ("decision", "id", ("ai", "au", "ad")),
    "actions": ("action", "id", ("ai", "au", "ad")),
    "checklist_items": ("checklist_item", "id", ("ai", "au", "ad")),
    "projects": ("project", "id", ("ai", "au", "ad")),
    "people": ("person", "id", ("ai", "au", "ad")),
    "systems": ("system", "id", ("ai", "au", "ad")),
    "fact_keys": ("fact", "key", ("ai", "au", "ad")),
    "threads": ("thread", "id", ("ai", "au", "ad")),
    "sources": ("source", "id", ("ai",)),
}

_DRIFT_QUERIES = (
    (
        "not indexed",
        "SELECT entity || ':' || ref_id FROM ("
        "SELECT entity, ref_id FROM search_docs "
        "EXCEPT SELECT entity, ref_id FROM search_keys)",
    ),
    (
        "indexed but gone",
        "SELECT entity || ':' || ref_id FROM ("
        "SELECT entity, ref_id FROM search_keys "
        "EXCEPT SELECT entity, ref_id FROM search_docs)",
    ),
    (
        "index rows without a key",
        "SELECT doc FROM ("
        "SELECT rowid AS doc FROM search_index EXCEPT SELECT doc FROM search_keys)",
    ),
    (
        "index row missing or different",
        "SELECT entity || ':' || ref_id FROM ("
        "SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body "
        "FROM search_docs AS d JOIN search_keys AS k "
        "ON k.entity = d.entity AND k.ref_id = d.ref_id "
        "EXCEPT SELECT rowid, entity, ref_id, status, title, body FROM search_index)",
    ),
)


TRUTH_SINCE = 5
# Migration 0005's search objects: claims and aliases re-index fact documents.
TRUTH_SEARCH_OBJECTS = (
    ("view", "fact_key_parts"),
    ("trigger", "search_claims_ai"),
    ("trigger", "search_claims_au"),
    ("trigger", "search_aliases_ai"),
    ("trigger", "search_aliases_ad"),
)


def required_search_objects(version: int = 4) -> list[tuple[str, str]]:
    objects = [("table", "search_index"), ("table", "search_keys"), ("view", "search_docs")]
    for table, (_entity, _key, events) in SEARCH_TABLES.items():
        objects.extend(("trigger", f"search_{table}_{event}") for event in events)
    if version >= TRUTH_SINCE:
        objects.extend(TRUTH_SEARCH_OBJECTS)
    return objects


def missing_search_objects(con: sqlite3.Connection) -> list[str]:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version < SEARCH_SINCE:
        return []
    present = {(row[0], row[1]) for row in con.execute("SELECT type, name FROM sqlite_schema")}
    return [name for kind, name in required_search_objects(version) if (kind, name) not in present]


def search_counts(con: sqlite3.Connection) -> tuple[int, int, int]:
    docs = con.execute("SELECT count(*) FROM search_docs").fetchone()[0]
    keys = con.execute("SELECT count(*) FROM search_keys").fetchone()[0]
    indexed = con.execute("SELECT count(*) FROM search_index_docsize").fetchone()[0]
    return docs, keys, indexed


def search_targets_drift(con: sqlite3.Connection, targets: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[tuple[str, object]] = set()
    for target in targets:
        table = target.get("table")
        if table == "claims":
            # A claim maps to its key's fact document.
            found = con.execute("SELECT key FROM claims WHERE id = ?", (target.get("id"),)).fetchone()
            if found is None:
                continue
            entity, ref_id = "fact", found[0]
        elif table not in SEARCH_TABLES:
            continue
        else:
            entity, _key, _events = SEARCH_TABLES[table]
            ref_id = target.get("id")
        if (entity, ref_id) in seen:
            continue
        seen.add((entity, ref_id))
        docs = [
            tuple(row)
            for row in con.execute(
                "SELECT entity, ref_id, status, title, body FROM search_docs "
                "WHERE entity = ? AND ref_id = ?",
                (entity, ref_id),
            )
        ]
        indexed = [
            tuple(row)
            for row in con.execute(
                "SELECT s.entity, s.ref_id, s.status, s.title, s.body "
                "FROM search_keys AS k JOIN search_index AS s ON s.rowid = k.doc "
                "WHERE k.entity = ? AND k.ref_id = ?",
                (entity, ref_id),
            )
        ]
        if docs != indexed:
            problems.append(f"search index out of sync for {entity} {ref_id}")
    return problems


def rebuild_search(con: sqlite3.Connection) -> int:
    if not con.in_transaction:
        raise RuntimeError("rebuild_search runs only inside the applier's transaction")
    # search_keys first: its guards refuse a connection without the applier's context
    # before the unguarded FTS table is touched.
    con.execute("DELETE FROM search_keys")
    con.execute("DELETE FROM search_index")
    con.execute(
        "INSERT INTO search_keys (entity, ref_id) "
        "SELECT entity, ref_id FROM search_docs ORDER BY entity, ref_id"
    )
    con.execute(
        "INSERT INTO search_index (rowid, entity, ref_id, status, title, body) "
        "SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body "
        "FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id"
    )
    return con.execute("SELECT count(*) FROM search_keys").fetchone()[0]


def fts5_missing_message() -> str | None:
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE brain_fts5_probe USING fts5(x)")
    except sqlite3.OperationalError:
        return (
            f"SQLite {sqlite3.sqlite_version} was built without FTS5, "
            "which the search index needs"
        )
    finally:
        con.close()
    return None


def search_drift(con: sqlite3.Connection) -> list[str]:
    problems: list[str] = []
    for label, sql in _DRIFT_QUERIES:
        rows = con.execute(sql + " LIMIT 6").fetchall()
        if not rows:
            continue
        shown = ", ".join(str(row[0]) for row in rows[:5])
        text = f"search drift, {label}: {shown}"
        if len(rows) > 5:
            text += " ..."
        problems.append(text)
    return problems


# list table, its key column, the table it lists people for
PEOPLE_TABLES = tuple((list_table, list_key, parent) for parent, (list_table, list_key) in schemas.PEOPLE_LISTS.items())
UNCHECKED_PARENTS = (
    ("project", "projects"),
    ("record", "records"),
    ("action", "actions"),
    ("checklist_item", "checklist_items"),
)
_KEY_HEX = re.compile(r"[0-9a-f]{64}")


def _tables_present(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    }


def people_problems(con: sqlite3.Connection) -> list[str]:
    present = _tables_present(con)
    problems: list[str] = []
    for list_table, list_key, parent in PEOPLE_TABLES:
        if list_table not in present:
            continue
        for role, scalar, _field in schemas.PEOPLE_ROLES:
            rows = con.execute(
                f"""SELECT a.id FROM {parent} AS a
                    LEFT JOIN (
                      SELECT {list_key} AS id, count(*) AS n, max(position) AS mx
                      FROM {list_table} WHERE role = ? GROUP BY {list_key}
                    ) AS l ON l.id = a.id
                    WHERE CASE WHEN a.{scalar} IS NULL THEN coalesce(l.n, 0) > 0
                               ELSE l.n IS NULL OR l.mx <> l.n - 1 OR
                                    (SELECT p.person_id FROM {list_table} AS p
                                     WHERE p.{list_key} = a.id AND p.role = ? AND p.position = 0)
                                    IS NOT a.{scalar} END
                    ORDER BY a.id""",
                (role, role),
            ).fetchall()
            problems.extend(
                f"{list_table} {row[0]} {role}: people list does not match {scalar}" for row in rows
            )
    return problems


def _marker_malformed(raw: str) -> bool:
    try:
        elements = json.loads(raw)  # trusted: unchecked_text.sentences, written by the applier
    except ValueError:
        return True
    if not isinstance(elements, list) or not elements:
        return True
    for element in elements:
        if not isinstance(element, dict) or set(element) != {"key", "reasons", "text"}:
            return True
        key, reasons, text = element["key"], element["reasons"], element["text"]
        if not isinstance(key, str) or _KEY_HEX.fullmatch(key) is None:
            return True
        if (
            not isinstance(reasons, list)
            or not reasons
            or any(reason not in evidence_gate.FLAG_REASONS for reason in reasons)
        ):
            return True
        if not isinstance(text, str) or not 1 <= len(text) <= 300:
            return True
    return False


def unchecked_problems(con: sqlite3.Connection) -> list[str]:
    if "unchecked_text" not in _tables_present(con):
        return []
    problems: list[str] = []
    for entity, ref_id, sentences in con.execute(
        "SELECT entity, ref_id, sentences FROM unchecked_text ORDER BY entity, ref_id"
    ).fetchall():
        table = dict(UNCHECKED_PARENTS)[entity]
        if con.execute(f"SELECT 1 FROM {table} WHERE id = ?", (ref_id,)).fetchone() is None:
            problems.append(f"unchecked_text {entity} {ref_id}: row does not exist")
        if _marker_malformed(sentences):
            problems.append(f"unchecked_text {entity} {ref_id}: malformed sentences")
    return problems


def _value_matches(key: str, fact: dict, canonical: str) -> bool:
    return truth.canonical(key, fact) == canonical


def truth_problems(con: sqlite3.Connection) -> list[str]:
    """Each fact's stored headline against what its claims give, in key order."""
    registry = {row[0]: row[1] for row in con.execute("SELECT key, value_type FROM fact_keys")}
    claims: dict[str, list[dict]] = {}
    for claim in truth.rows(con, truth.CLAIM_SQL + "ORDER BY c.key, c.id"):
        claim["canonical"] = truth.canonical(claim["key"], claim)
        claims.setdefault(claim["key"], []).append(claim)
    facts = {
        fact["key"]: fact
        for fact in truth.rows(
            con,
            "SELECT key, value_type, value_text, value_int, value_real, value_date, status FROM facts",
        )
    }
    signals: dict[str, list[str]] = {}
    for signal_id, subject in con.execute(
        "SELECT id, subject_id FROM signals WHERE kind = 'conflict' AND status = 'open' "
        "AND subject_table = 'facts' ORDER BY id"
    ):
        signals.setdefault(subject, []).append(signal_id)
    versions: dict[str, tuple[bool, str | None]] = {}
    for key, value_type in registry.items():
        entity, attribute = truth.key_parts(key)
        if attribute == truth.VERSION_ATTRIBUTE and value_type == "text":
            fact = facts.get(key)
            current = fact["value_text"] if fact is not None and fact["status"] == "current" else None
            versions[entity] = (True, current)
    problems: list[str] = []
    for key in sorted(set(claims) | set(facts) | set(signals)):
        entity, _attribute = truth.key_parts(key)
        open_ids = signals.get(key, [])
        legacy = any(truth.is_legacy(item) for item in open_ids)
        state = truth.state(
            con,
            key,
            claims=claims.get(key, []),
            legacy=legacy,
            version=versions.get(entity, (False, None)),
        )
        fact = facts.get(key)
        now = state.now
        if now.claims and fact is None:
            problems.append(f"fact {key}: claims are in force but there is no fact row")
        if fact is not None:
            derived = ("disputed" if now.disputed else "current") if now.claims else "retired"
            if fact["status"] != derived:
                problems.append(f"fact {key}: status is {fact['status']}, claims give {derived}")
            elif derived == "current" and not _value_matches(key, fact, now.claims[0]["canonical"]):
                problems.append(f"fact {key}: value does not match its claims")
        prefixes = {}
        for bucket in state.buckets():
            prefix = truth.signal_prefix(key, bucket.label)
            if bucket.disputed:
                prefixes[prefix] = bucket
                covered = any(item.startswith(prefix) for item in open_ids) or (
                    bucket.label == "now" and legacy
                )
                if not covered:
                    problems.append(f"fact {key}: {bucket.label} is disputed but has no open conflict signal")
        for signal_id in open_ids:
            if truth.is_legacy(signal_id):
                if not now.claims:
                    problems.append(f"conflict signal {signal_id} is open but its bucket agrees")
                continue
            if not any(signal_id.startswith(prefix) for prefix in prefixes):
                problems.append(f"conflict signal {signal_id} is open but its bucket agrees")
        for claim in claims.get(key, []):
            if key in registry and claim["value_type"] != registry[key]:
                problems.append(f"claim {claim['id']}: value_type differs from fact key {key}")
    return problems


def nontext_version_keys(con: sqlite3.Connection) -> list[str]:
    """current_version keys that aren't text, so set no version."""
    return [
        row[0]
        for row in con.execute("SELECT key, value_type FROM fact_keys ORDER BY key")
        if truth.key_parts(row[0])[1] == truth.VERSION_ATTRIBUTE and row[1] != "text"
    ]


def unmatchable_version_values(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Version-key facts whose value no claim scope can match, such as one migrated from 0.6.1."""
    found = []
    for key, value_type, value in con.execute(
        "SELECT f.key, k.value_type, f.value_text FROM facts AS f JOIN fact_keys AS k ON k.key = f.key ORDER BY f.key"
    ):
        if truth.is_version_key(key, value_type) and value is not None and not truth.scope_label_ok(value):
            found.append((key, value))
    return found


# note_meta's entities and the tables and fields they name.
NOTE_TABLES = {
    entity: (schemas.ENTITY_TABLE[entity], schemas.NOTE_FIELDS[name]) for name, entity in schemas.NOTE_ENTITY.items()
}


def agent_person_warnings(con: sqlite3.Connection, agents) -> list[str]:
    """1.9.0: agents whose person_id has no people row yet. brain check prints them as warnings, because a new
    instance has no people at init; until the row exists, that agent's observations without an author are refused."""
    return [
        f"agents.{agent.name}.person_id {agent.person_id} is not a person yet; add it with upsert_person, or "
        "observations from that agent without author_person_id are refused"
        for agent in sorted(agents, key=lambda item: item.name)
        if agent.person_id is not None
        and con.execute("SELECT 1 FROM people WHERE id = ?", (agent.person_id,)).fetchone() is None
    ]


def note_meta_warnings(con: sqlite3.Connection) -> list[str]:
    """note_meta rows whose row is gone or whose note is empty, in (entity, ref_id,
    field) order. brain check prints them as warnings: they are metadata drift, never corruption."""
    if "note_meta" not in _tables_present(con):
        return []
    lines: list[str] = []
    for entity, ref_id, field in con.execute(
        "SELECT entity, ref_id, field FROM note_meta ORDER BY entity, ref_id, field"
    ).fetchall():
        table, fields = NOTE_TABLES.get(entity, (None, ()))
        if table is None or field not in fields:
            continue
        found = con.execute(f"SELECT {field} FROM {table} WHERE id = ?", (ref_id,)).fetchone()
        if found is None:
            lines.append(f"note_meta {entity} {ref_id} {field}: row does not exist")
        elif found[0] == "":
            lines.append(f"note_meta {entity} {ref_id} {field}: the note is empty")
    return lines


def rows_with_notes(con: sqlite3.Connection) -> int:
    """Rows of the five note tables with any non-empty note field."""
    total = 0
    for table, fields in NOTE_TABLES.values():
        where = " OR ".join(f"{field} <> ''" for field in fields)
        total += con.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0]
    return total


def stored_alias_rows(con: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return sorted(tuple(row) for row in con.execute("SELECT kind, alias, target FROM aliases"))


# The error a refused packet stores when it cites a redacted source, and the error a scrub leaves.
_REFUSAL = re.compile(
    r"op [0-9]+ \([a-z_]+\): source src-[a-z0-9][a-z0-9._-]* is redacted"
    r"|op [0-9]+ \(add_source\): this text was redacted \(source src-[a-z0-9][a-z0-9._-]*\)"
)
_REDACTION_ERROR = re.compile(r"redacted on [0-9]{4}-[0-9]{2}-[0-9]{2}")
_CLAIM_TEXT = (
    "SELECT id FROM claims WHERE source_id = ? AND (quote NOT IN ('', '[redacted]') OR value_raw <> '[redacted]' "
    "OR (value_type IN ('text', 'enum') AND value_text <> '[redacted]') "
    "OR (value_type = 'url' AND value_text <> 'https://redacted.invalid/')) ORDER BY id"
)
_FACT_TEXT = (
    "SELECT {col} FROM {table} WHERE source_id = ? AND ((quote IS NOT NULL AND quote NOT IN ('', '[redacted]')) "
    "OR (value_type IN ('text', 'enum') AND value_text <> '[redacted]') "
    "OR (value_type = 'url' AND value_text <> 'https://redacted.invalid/')) ORDER BY {col}"
)
# (label in the problem text, query over one source id)
_REDACTION_ROWS = (
    ("claim", _CLAIM_TEXT),
    ("fact", _FACT_TEXT.format(col="key", table="facts")),
    ("fact_history", _FACT_TEXT.format(col="id", table="fact_history")),
    ("decision", "SELECT id FROM decisions WHERE source_id = ? AND quote <> '[redacted]' ORDER BY id"),
)


def named_sources(text: str) -> set[str]:
    """The source ids a text names. The one definition lives in brain.scrub."""
    from brain import scrub

    return scrub.named_sources(text)


def redaction_marker_id(instance_dir: Path) -> str:
    """The source id a REDACTING marker names, or SOURCE_ID if it cannot be read."""
    try:
        found = json.loads((Path(instance_dir) / "REDACTING").read_text(encoding="utf-8")).get("source_id", "")  # trusted: the REDACTING marker, written by brain redact
    except (OSError, ValueError, AttributeError):
        found = ""
    return found if isinstance(found, str) and found else "SOURCE_ID"


def tombstone_ids(con: sqlite3.Connection) -> list[str]:
    """The ids of the redacted sources: candidates from the partial index sources_redacted, each tested with
    scrub.is_tombstone (a title alone proves nothing)."""
    from brain import scrub

    return [
        row[0]
        for row in con.execute("SELECT id, title, content, content_sha256 FROM sources WHERE title = 'redacted' ORDER BY id")
        if scrub.is_tombstone({"title": row[1], "content": row[2], "content_sha256": row[3]})
    ]


def redaction_problems(con: sqlite3.Connection, *, only: set[str] | None = None) -> list[str]:
    """The gate and the packet list are empty, and every redacted source has left nothing
    behind that can be checked. `only` limits the tombstones (the op's own verification)."""
    from brain import scrub

    if "redaction_gate" not in _tables_present(con):
        return []
    problems: list[str] = []
    if con.execute("SELECT 1 FROM redaction_gate LIMIT 1").fetchone():
        problems.append("redaction_gate is not empty: a redaction did not finish")
    if con.execute("SELECT 1 FROM redaction_packets LIMIT 1").fetchone():
        problems.append("redaction_packets is not empty: a redaction did not finish")
    tombs = [sid for sid in tombstone_ids(con) if only is None or sid in only]
    for sid in tombs:
        for cid, status in con.execute(
            "SELECT id, status FROM claims WHERE source_id = ? AND status IN ('live', 'planned') ORDER BY id", (sid,)
        ):
            problems.append(f"source {sid} is redacted, but claim {cid} is still {status}")
        for label, sql in _REDACTION_ROWS:
            for (ref,) in con.execute(sql, (sid,)):
                problems.append(f"source {sid} is redacted, but {label} {ref} still holds its text")
    if not tombs:
        return problems
    wanted = set(tombs)
    parameter = json.dumps(sorted(wanted))
    for change_id, status, packet_json, error, hold in con.execute(
        "SELECT change_id, status, packet_json, error, hold_reason FROM write_log WHERE EXISTS ("
        "SELECT 1 FROM json_each(?) AS j WHERE instr(packet_json, j.value) > 0 OR instr(error, j.value) > 0 "
        "OR instr(hold_reason, j.value) > 0) ORDER BY change_id",
        (parameter,),
    ).fetchall():
        hit = (named_sources(packet_json) | named_sources(error) | named_sources(hold)) & wanted
        if not hit:
            continue
        try:
            packet = json.loads(packet_json)  # trusted: write_log.packet_json, the packet the applier stored
        except ValueError:
            packet = {}
        if isinstance(packet, dict) and packet.get("agent") == "brain" and any(
            isinstance(o, dict) and o.get("op") == "redact_source" for o in packet.get("ops") or ()
        ):
            continue
        bad = bool(scrub.packet_problems(packet, hit, whole=status != "applied")) if isinstance(packet, dict) else False
        bad = bad or any(
            text
            and (named_sources(text) & wanted)
            and not _REDACTION_ERROR.fullmatch(text)
            and not _REFUSAL.fullmatch(text)
            for text in (error, hold)
        )
        if bad:
            problems.append(f"source {sorted(hit)[0]} is redacted, but write_log {change_id} still holds its text")
    for run_id, error in con.execute(
        "SELECT id, error FROM collector_runs WHERE EXISTS (SELECT 1 FROM json_each(?) AS j "
        "WHERE instr(error, j.value) > 0) ORDER BY id",
        (parameter,),
    ).fetchall():
        found = named_sources(error) & wanted
        if found and not _REDACTION_ERROR.fullmatch(error):
            problems.append(f"source {sorted(found)[0]} is redacted, but collector_run {run_id} still holds its text")
    return problems


def integrity_problems(con: sqlite3.Connection) -> list[str]:
    # Read-only (query_only) connections and open transactions skip the lock: a
    # read-only connection can't take it, and a fresh one doesn't see the stale index.
    locked = False
    if not con.in_transaction and not con.execute("PRAGMA query_only").fetchone()[0]:
        try:
            con.execute("BEGIN IMMEDIATE")
            locked = True
        except sqlite3.OperationalError:
            locked = False
    try:
        if con.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'search_index'"
        ).fetchone():
            con.execute("SELECT rowid FROM search_index LIMIT 1").fetchall()
        rows = [tuple(row) for row in con.execute("PRAGMA integrity_check")]
        problems: list[str] = []
        if rows != [("ok",)]:
            problems.extend(row[0] if len(row) == 1 else str(row) for row in rows)
        for row in con.execute("PRAGMA foreign_key_check").fetchall():
            problems.append(" ".join(str(part) for part in row))
        problems.extend(people_problems(con))
        problems.extend(unchecked_problems(con))
        if "claims" in _tables_present(con):
            problems.extend(truth_problems(con))
        return problems
    finally:
        if locked and con.in_transaction:
            con.execute("ROLLBACK")


def check_instance(dir: Path) -> list[str]:
    from brain import config as config_mod
    from brain import migrate as migrate_mod

    instance_dir = Path(dir)
    problems: list[str] = []
    toml_path = instance_dir / "brain.toml"
    db = instance_dir / "brain.sqlite3"
    router = instance_dir / "ROUTER.md"

    if (instance_dir / "PAUSED").exists():
        problems.append("instance is paused: see PAUSED; investigate, then delete it")
    if (instance_dir / "REDACTING").is_file():
        problems.append(f"a redaction did not finish: run brain redact {redaction_marker_id(instance_dir)} again")

    cfg_for_aliases = None
    try:
        cfg_for_aliases = config_mod.load_config(toml_path)
    except (config_mod.ConfigError, OSError) as exc:
        problems.append(f"brain.toml: {exc}")

    if not db.is_file():
        problems.append(f"no store at {db}")
        return problems

    con = connect_readonly(db)
    try:
        con.execute("PRAGMA query_only = ON")
        try:
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.OperationalError:
            if (db.parent / (db.name + "-journal")).exists():
                problems.append("store has a hot journal: run brain redact <id> again, or any brain write, to recover it")
                return problems
            raise
        latest = migrate_mod.latest_version()
        if user_version != latest:
            problems.append(f"store is at version {user_version}, engine expects {latest}")
        problems.extend(integrity_problems(con))
        problems.extend(redaction_problems(con))
        if 1 <= user_version <= latest:
            differences = migrate_mod.schema_differences(con, user_version, ignore_stats=True)
            problems.extend(f"schema: {text}" for text in differences[:5])
            if len(differences) > 5:
                problems.append(f"schema: and {len(differences) - 5} more differences")
        loose = [
            row[0]
            for row in con.execute(
                "SELECT name FROM pragma_table_list "
                "WHERE schema = 'main' AND type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND strict = 0"
            )
        ]
        if loose:
            problems.append("tables are not STRICT: " + ", ".join(loose))
        missing = missing_guards(con)
        if missing:
            problems.append("missing guards: " + ", ".join(missing))
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "delete":
            problems.append(f"journal mode is {mode}, expected delete")
        search_missing = missing_search_objects(con)
        if search_missing:
            problems.append("missing search objects: " + ", ".join(search_missing))
        elif user_version >= SEARCH_SINCE:
            problems.extend(search_drift(con))
        if cfg_for_aliases is not None:
            # A key dropped from brain.toml stays in the store, and resolution uses the store's keys.
            clash = config_mod.truth_clash(
                cfg_for_aliases.truth, [row[0] for row in con.execute("SELECT key FROM fact_keys")]
            )
            if clash:
                problems.append(f"brain.toml: {clash}")
        if cfg_for_aliases is not None and "aliases" in _tables_present(con):
            desired = truth.alias_rows(cfg_for_aliases.truth.entities, cfg_for_aliases.truth.attributes)
            if stored_alias_rows(con) != desired:
                problems.append("aliases differ from brain.toml; run brain apply-config")
        try:
            con.execute("INSERT INTO sources DEFAULT VALUES")
            problems.append("write probe did not refuse a direct insert")
        except sqlite3.OperationalError as exc:
            if "brain_applier_context" not in str(exc):
                problems.append(f"write probe: {exc}")
    finally:
        con.close()

    if not router.is_file():
        problems.append("ROUTER.md is missing")
    elif router.stat().st_size > 2048:
        problems.append(f"ROUTER.md is {router.stat().st_size} bytes (max 2048)")
    try:
        cfg = config_mod.load_config(toml_path)
    except (config_mod.ConfigError, OSError):
        cfg = None
    if cfg is not None:
        from brain.init import render_router

        expected = render_router(cfg.name, cfg.writes.notes).encode("utf-8")
        actual = router.read_bytes() if router.is_file() else None
        if actual != expected:
            problems.append("ROUTER.md differs from the engine router; run brain router")
    return problems
