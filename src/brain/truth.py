"""Product truth, read side: key parts, alias resolution, buckets, rank order and descent.

Everything here reads through a sqlite3.Connection and never writes, so the applier, the reads
and brain check share one definition. It imports units, evidence_gate and schemas only.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date

from brain import evidence_gate, schemas, units

BASIS_ORDER = {"observed": 0, "reported": 1, "inferred": 2}
DEFAULT_RANK_KINDS = (
    "document",
    "api",
    "observation",
    "config",
    "web",
    "email",
    "user_note",
    "slack",
    "meeting_transcript",
    "calendar",
)
LEGACY_PREFIX = "sig-conflict-m5-"
VERSION_ATTRIBUTE = "current_version"
DESCENT_STEPS = 10_000
_FACT_KEY = re.compile(schemas.FACT_KEY)
_SCOPE = re.compile(schemas.SCOPE)

CLAIM_SQL = (
    "SELECT c.id, c.key, c.scope, c.value_type, c.value_text, c.value_int, c.value_real, c.value_date, "
    "c.value_raw, c.status, c.basis, c.as_of, c.source_id, c.locator, c.quote, c.checked, c.supersedes, "
    "c.proposed_by, c.superseded_by, c.resolution_source_id, c.resolution_locator, c.created_at, "
    "c.created_by, c.updated_at, c.updated_by, "
    "s.kind AS source_kind, s.uri AS source_uri, s.source_date AS source_date "
    "FROM claims AS c JOIN sources AS s ON s.id = c.source_id "
)
_DESCENT_SQL = (
    "WITH RECURSIVE chain(id, depth) AS ("
    "SELECT supersedes_id, 1 FROM sources WHERE id = ? AND supersedes_id IS NOT NULL "
    "UNION ALL "
    "SELECT s.supersedes_id, c.depth + 1 FROM sources AS s JOIN chain AS c ON s.id = c.id "
    f"WHERE s.supersedes_id IS NOT NULL AND c.depth < {DESCENT_STEPS}"
    ") SELECT 1 FROM chain WHERE id = ? LIMIT 1"
)


def rows(con, sql: str, params=()) -> list[dict]:
    cursor = con.execute(sql, params)
    names = [item[0] for item in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def key_parts(key: str) -> tuple[str, str]:
    """A key's entity is everything before its last dot, and its attribute everything after."""
    entity, _, attribute = key.rpartition(".")
    return entity, attribute


def words(text) -> str:
    """The normal form of alias words: dots and underscores are spaces, then match_key."""
    return evidence_gate.match_key(str(text).replace(".", " ").replace("_", " "))


def version_key(entity: str) -> str:
    return f"{entity}.{VERSION_ATTRIBUTE}"


def is_version_key(key: str, value_type: str) -> bool:
    return key_parts(key)[1] == VERSION_ATTRIBUTE and value_type == "text"


def scope_label_ok(value) -> bool:
    """Whether a current_version value can match a claim scope: schemas.SCOPE, not empty, not now
."""
    return (
        isinstance(value, str) and value != "" and _SCOPE.fullmatch(value) is not None and value.lower() != "now"
    )


def version_info(con, entity: str) -> tuple[bool, str | None]:
    """(the entity has a version key, its current version or None)."""
    vkey = version_key(entity)
    registered = con.execute("SELECT value_type FROM fact_keys WHERE key = ?", (vkey,)).fetchone()
    if registered is None or registered[0] != "text":
        return False, None
    fact = con.execute("SELECT status, value_text FROM facts WHERE key = ?", (vkey,)).fetchone()
    if fact is not None and fact[0] == "current":
        return True, fact[1]
    return True, None


def canonical(key: str, claim) -> str:
    """Two claims agree when these are equal. A version key compares its exact text."""
    if is_version_key(key, claim["value_type"]):
        return claim["value_text"]
    return units.canonical(claim["value_type"], claim)


def _kinds(truth) -> tuple[str, ...]:
    return DEFAULT_RANK_KINDS if truth is None else tuple(truth.rank_kinds)


def _prefixes(truth) -> tuple[str, ...]:
    return () if truth is None else tuple(truth.rank_uri_prefixes)


def rank(claim, truth=None) -> int:
    uri = claim.get("source_uri") or ""
    if any(uri.startswith(prefix) for prefix in _prefixes(truth)):
        return 0
    kinds = _kinds(truth)
    kind = claim.get("source_kind")
    if kind in kinds:
        return 1 + kinds.index(kind)
    return 1 + len(kinds)


def _newest_first(as_of: str) -> int:
    try:
        return -date.fromisoformat(as_of).toordinal()
    except (TypeError, ValueError):
        return 0


def rank_key(claim, truth=None) -> tuple:
    return (
        BASIS_ORDER.get(claim["basis"], len(BASIS_ORDER)),
        rank(claim, truth),
        _newest_first(claim["as_of"]),
        claim["id"],
    )


def rank_order(claims, truth=None) -> list[dict]:
    """Basis (observed, reported, inferred), then rank, then as_of newest first, then id."""
    return sorted(claims, key=lambda claim: rank_key(claim, truth))


def signal_hash(key: str, label: str) -> str:
    return hashlib.sha256(f"{key}\n{label}".encode()).hexdigest()[:16]


def signal_prefix(key: str, label: str) -> str:
    return f"sig-conflict-{signal_hash(key, label)}-"


def is_legacy(signal_id: str) -> bool:
    return signal_id.startswith(LEGACY_PREFIX)


@dataclass
class Bucket:
    label: str
    claims: list[dict] = field(default_factory=list)  # live, in rank order
    disputed: bool = False

    def values(self) -> list[tuple[str, dict, int]]:
        """Each distinct canonical value, its best claim and its number of claims, in rank order."""
        seen: dict[str, list] = {}
        for claim in self.claims:
            entry = seen.get(claim["canonical"])
            if entry is None:
                seen[claim["canonical"]] = [claim, 1]
            else:
                entry[1] += 1
        return [(value, best, count) for value, (best, count) in seen.items()]


@dataclass
class KeyState:
    key: str
    entity: str
    has_version_key: bool
    current_version: str | None
    claims: list[dict]  # live and planned
    now: Bucket
    scopes: dict[str, Bucket]  # ascending by label, live claims only
    planned: list[dict]
    legacy_open: bool

    def buckets(self) -> list[Bucket]:
        return [self.now, *self.scopes.values()]

    def label_for(self, scope: str) -> str:
        """A scope's bucket label: '', now or the current version is the now bucket."""
        if scope == "" or scope.lower() == "now" or (
            self.current_version is not None and scope == self.current_version
        ):
            return "now"
        return scope

    def bucket_for(self, scope: str) -> Bucket:
        label = self.label_for(scope)
        if label == "now":
            return self.now
        return self.scopes.get(label) or Bucket(label)


def legacy_open(con, key: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM signals WHERE status = 'open' AND kind = 'conflict' AND subject_table = 'facts' "
            "AND subject_id = ? AND substr(id, 1, ?) = ? LIMIT 1",
            (key, len(LEGACY_PREFIX), LEGACY_PREFIX),
        ).fetchone()
        is not None
    )


def claims_of(con, key: str) -> list[dict]:
    """A key's live and planned claims: the only ones a bucket or a planned list can hold."""
    found = rows(con, CLAIM_SQL + "WHERE c.key = ? AND c.status IN ('live', 'planned') ORDER BY c.id", (key,))
    for claim in found:
        claim["canonical"] = canonical(key, claim)
    return found


def state(con, key: str, truth=None, *, claims: list[dict] | None = None, legacy: bool | None = None,
          version: tuple[bool, str | None] | None = None) -> KeyState:
    """The buckets of one key. The now bucket holds the live unscoped claims and the live claims
    scoped to the current version. Every other scope with live claims is its own bucket."""
    entity, _attribute = key_parts(key)
    has_version, current = version if version is not None else version_info(con, entity)
    claims = claims_of(con, key) if claims is None else claims
    legacy = legacy_open(con, key) if legacy is None else legacy
    now_claims: list[dict] = []
    scoped: dict[str, list[dict]] = {}
    planned: list[dict] = []
    for claim in claims:
        if claim["status"] == "planned":
            planned.append(claim)
            continue
        if claim["status"] != "live":
            continue
        if claim["scope"] == "" or (current is not None and claim["scope"] == current):
            now_claims.append(claim)
        else:
            scoped.setdefault(claim["scope"], []).append(claim)
    now = Bucket("now", rank_order(now_claims, truth))
    now.disputed = len({claim["canonical"] for claim in now_claims}) > 1 or (bool(now_claims) and legacy)
    scopes: dict[str, Bucket] = {}
    for label in sorted(scoped):
        bucket = Bucket(label, rank_order(scoped[label], truth))
        bucket.disputed = len({claim["canonical"] for claim in bucket.claims}) > 1
        scopes[label] = bucket
    return KeyState(
        key, entity, has_version, current, claims, now, scopes, rank_order(planned, truth), legacy
    )


def descends(con, b: str, a: str) -> bool:
    """Source b descends from source a: following sources.supersedes_id from b reaches a."""
    if b == a:
        return False
    return con.execute(_DESCENT_SQL, (b, a)).fetchone() is not None


def resolve(con, text: str) -> list[str]:
    """The sorted registered keys that a key or alias words name. No fuzzy matching."""
    registered = [row[0] for row in con.execute("SELECT key FROM fact_keys")]
    known = set(registered)
    if _FACT_KEY.fullmatch(text) and text in known:
        return [text]
    parts = words(text).split()
    if len(parts) < 2:
        return []
    entities: dict[str, set[str]] = {}
    attributes: dict[str, set[str]] = {}
    for kind, alias, target in con.execute("SELECT kind, alias, target FROM aliases"):
        (entities if kind == "entity" else attributes).setdefault(alias, set()).add(target)
    for key in registered:
        entity, attribute = key_parts(key)
        entities.setdefault(words(entity), set()).add(entity)
        attributes.setdefault(words(attribute), set()).add(attribute)
    found: set[str] = set()
    for split in range(1, len(parts)):
        left = " ".join(parts[:split])
        right = " ".join(parts[split:])
        for entity in entities.get(left, ()):
            for attribute in attributes.get(right, ()):
                key = f"{entity}.{attribute}"
                if key in known:
                    found.add(key)
    return sorted(found)


def alias_rows(entities, attributes) -> list[tuple[str, str, str]]:
    """The aliases rows brain apply-config loads: (kind, normal form, target), sorted."""
    found: set[tuple[str, str, str]] = set()
    for kind, table in (("entity", entities or {}), ("attribute", attributes or {})):
        for target, aliases in table.items():
            for alias in aliases:
                found.add((kind, words(alias), target))
    return sorted(found)
