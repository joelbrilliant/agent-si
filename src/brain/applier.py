from __future__ import annotations

import dataclasses
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain import advice
from brain.sessions import cite as session_cite
from brain import candidates
from brain import config as config_mod
from brain import evidence_gate
from brain import ingress
from brain import migrate
from brain import schemas
from brain import scrub as scrub_mod
from brain import store
from brain import strict
from brain import truth
from brain import units
from brain import validate
from brain.collectors import REGISTRY
from brain.text import has_line_break
from brain.collectors.base import local_date
from brain.scrub import TOMBSTONE_PREFIX, TOMBSTONE_TITLE  # noqa: F401
from brain.text import single_line

_CHANGE_ID = re.compile(schemas.CHANGE_ID)
_MAX_PACKET = ingress.MAX_PACKET_BYTES
FULL_CHECK_MAX_BYTES = 16 * 1024 * 1024
FULL_CHECK_EVERY = 25
FOLDER_DEPTH_MAX = 8  # folders (0007): how deep a project can sit under its top section
CHECK_BUDGET_MS = 1000
_FUTURE_SLACK = timedelta(seconds=300)
_BRAIN_OPS = frozenset(
    {"add_source", "upsert_person", "upsert_project", "upsert_fact_key", "rebuild_search", "set_aliases", "redact_source"}
)
_RESERVED_CLAIM_ID = re.compile(r"^clm-m5-|^clm-[0-9a-f]{24}$")
# The claim columns the immutability trigger fixes, as an add_claim compares them.
_CLAIM_FIXED = (
    "key", "scope", "value_type", "value_text", "value_int", "value_real", "value_date", "value_raw",
    "basis", "as_of", "source_id", "locator", "quote", "checked", "supersedes", "proposed_by",
)
_HEADLINE = (
    "value_type", "value_text", "value_int", "value_real", "value_date", "status", "basis", "as_of",
    "last_verified_at", "source_id", "locator", "quote",
)
_EXIT = {"applied": 0, "rejected": 1, "conflict": 3, "held": 4}
_FACT_VALUE_COLUMNS = ("value_text", "value_int", "value_real", "value_date")
# The rows that can carry an unchecked marker: table -> entity. The one list is store.UNCHECKED_PARENTS.
_MARKER_ENTITY = {table: entity for entity, table in store.UNCHECKED_PARENTS}
# A legacy import's ops are the upserts that can store a marker, so what the gate refuses is kept and flagged.
LEGACY_OPS = frozenset(
    name for name, spec in schemas.OPS.items() if spec["kind"] == "upsert" and spec["table"] in _MARKER_ENTITY
)
_MARKER_MAX = 500
_MARKER_TEXT_MAX = 300
_PEOPLE_FIELDS = frozenset(field for _role, _scalar, field in schemas.PEOPLE_ROLES)
# The run window: stored candidates per agent per hour before the guard holds.
RUN_WINDOW_SECONDS = 3600
RUN_WINDOW_STORES = 25
_LEGACY_CLAIM_FIELDS = ("status", "superseded_by", "resolution_source_id", "resolution_locator")
_CHAIN_CTE = (
    "WITH RECURSIVE chain(id, depth) AS ("
    "SELECT ?, 0 UNION ALL "
    "SELECT s.supersedes_id, c.depth + 1 FROM sources AS s JOIN chain AS c ON s.id = c.id "
    f"WHERE s.supersedes_id IS NOT NULL AND c.depth < {truth.DESCENT_STEPS}"
    ") "
)
# The decisions that cite the superseded source or any version before it.
DECISIONS_BY_CHAIN_SQL = (
    _CHAIN_CTE + "SELECT d.id, d.quote FROM decisions AS d WHERE d.source_id IN (SELECT id FROM chain) ORDER BY d.id"
)
_E39_PREFIX = "a claim with this key, scope, source, locator and value was rejected ("
# brain.scrub (imported above) defines the tombstone's title and prefix, and the no-op tests check them, so the two
# cannot drift by a character.


def _sentence_key(sentence: str) -> str:
    return store.sha256_text(evidence_gate.normalise(sentence))



UPDATING = "UPDATING"
UPDATING_MESSAGE = "brain is updating; writes resume shortly"

class Unreadable(Exception):
    pass


class Unavailable(Exception):
    pass


class UnknownChange(Exception):
    pass


class NotAllowed(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    receipt: dict
    exit_code: int
    duplicate: bool = False
    integrity: str | None = None
    warning: str = ""
    check: str = ""            # "full" or "targeted"; "" for a duplicate, which runs no check
    check_ms: int = 0
    dry_run: bool = False      # the verdict of a rehearsal: nothing was kept, and no check ran


def receipt_from_row(row) -> dict:
    data = dict(row)
    targets = data["targets_json"]
    if isinstance(targets, str):
        targets = json.loads(targets)  # trusted: write_log.targets_json, written by the applier
    return {
        "receipt_version": 1,
        "change_id": data["change_id"],
        "agent": data["agent"],
        "status": data["status"],
        "decided_at": data["decided_at"],
        "applied_seq": data["applied_seq"],
        "reviewed_by": data["reviewed_by"],
        "reviewed_at": data["reviewed_at"],
        "payload_sha256": data["payload_sha256"],
        "error": data["error"],
        "targets": targets,
    }


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


class Applier:
    def __init__(
        self,
        instance_dir: Path,
        *,
        allow_reserved_agent: bool = False,
        collect: bool = False,
        extract: bool = False,
        batch: bool = False,
    ) -> None:
        self.instance_dir = Path(instance_dir)
        # Bulk writes, such as an import: each commit keeps its targeted checks, and the full integrity check runs once,
        # when the batch ends (end_batch, or close).
        self.batch = batch
        self._unchecked = 0
        self.allow_reserved_agent = allow_reserved_agent
        self.collect = collect
        self.extract = extract
        self._ctx = 0
        self._deriving = False  # set only while _derive writes facts (test 46)
        self._hold = ""  # the first hold reason met inside a packet's ops
        self._notes: list[str] = []  # warnings for an applied packet, such as a suppressed value
        self._content_keys: dict[str, str] = {}  # match_key of source content, by content_sha256
        self._packets: dict[str, list] | None = None  # parsed decided packets' ops, for one apply call
        self._evaluating = False  # true while an evaluated candidate's verdict is worked out
        self._links: list[tuple[str, str, str, str]] = []  # legacy superseded_by links of one packet
        self._dry_run = False  # true only inside apply(dry_run=True): the decision ends in a rollback
        self.redaction: dict | None = None  # what the last redact_source op did, for the command
        self.db = self.instance_dir / "brain.sqlite3"
        if not self.db.is_file():
            raise Unavailable(f"no store at {self.db}")
        self.con = sqlite3.connect(str(self.db), isolation_level=None, timeout=10.0)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA foreign_keys = ON")
        self.con.execute("PRAGMA trusted_schema = ON")
        self.con.create_function(store.GUARD_FUNCTION, 0, self._context)
        self.config = config_mod.load_config(self.instance_dir / "brain.toml")

    def _context(self) -> int:
        return 1 if self._ctx else 0

    def close(self) -> None:
        if self.batch and self._unchecked:
            self.end_batch()
        self.con.close()

    def end_batch(self) -> list[str]:
        """The batch's one full check. Problems pause the instance, as a failed commit check does."""
        self._unchecked = 0
        problems = store.integrity_problems(self.con)
        if problems:
            payload = {"paused_at": store.utc_now(), "change_id": "", "batch": True, "problems": problems}
            (self.instance_dir / "PAUSED").write_text(store.canonical_json(payload) + "\n")
        return problems

    def preconditions(self) -> None:
        old = store.sqlite_too_old_message()
        if old:
            raise Unavailable(old)
        try:
            self.config = config_mod.load_config(self.instance_dir / "brain.toml")
        except config_mod.ConfigError as exc:
            raise Unavailable(str(exc)) from exc
        if (self.instance_dir / "PAUSED").exists():
            raise Unavailable("instance is paused: see PAUSED; investigate, then delete it")
        version = self.con.execute("PRAGMA user_version").fetchone()[0]
        latest = migrate.latest_version()
        if version != latest:
            if version > latest:
                raise Unavailable(
                    f"store is at version {version}, newer than this engine ({latest})"
                )
            raise Unavailable(
                f"store is at version {version}, engine expects {latest}; run brain migrate"
            )
        missing = store.missing_guards(self.con)
        if missing:
            raise Unavailable("missing guards: " + ", ".join(missing))
        if self.con.execute("SELECT 1 FROM redaction_gate LIMIT 1").fetchone() or self.con.execute(
            "SELECT 1 FROM redaction_packets LIMIT 1"
        ).fetchone():
            raise Unavailable("redaction_gate is not empty: run brain check")
        mode = self.con.execute("PRAGMA journal_mode").fetchone()[0]
        if mode != "delete":
            raise Unavailable(f"journal mode is {mode}, expected delete")
        search_missing = store.missing_search_objects(self.con)
        if search_missing:
            raise Unavailable("missing search objects: " + ", ".join(search_missing))

    def _begin(self) -> None:
        try:
            self.con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise Unavailable(str(exc)) from exc
        # Checked after the write lock is held, not before, so a writer that passed
        # its preconditions before brain update wrote UPDATING can't commit after the pre-update backup.
        if (self.instance_dir / UPDATING).exists():
            self.con.execute("ROLLBACK")
            raise Unavailable(UPDATING_MESSAGE)
        self._ctx = 1

    def _end_txn(self, action: str) -> None:
        self.con.execute(action)
        self._ctx = 0

    def apply(self, raw: bytes, *, as_agent: str | None = None, dry_run: bool = False) -> Outcome:
        """Decide a packet and keep the decision. With dry_run the same decision runs and returns the same Outcome,
        but the transaction rolls back: no write_log row, receipt or change_id is used up, and no integrity check,
        PAUSED file or backup runs. A change_id that already has a receipt answers with that receipt either way."""
        self.preconditions()
        packet = self._parse(raw)
        # On a hub the token decides the agent. A packet under any other name is
        # refused here, before the write lock and the duplicate lookup, so it can't use up that agent's change_id.
        # With as_agent None (local use) nothing changes.
        if as_agent is not None and packet.get("agent") != as_agent:
            raise NotAllowed(f"this token writes as {as_agent}; the packet says {packet.get('agent')}")
        self._begin()
        self._packets = {}  # built per call and dropped at its end, so it can't go stale across packets
        self._dry_run = dry_run
        try:
            existing = self._write_log(packet["change_id"])
            if existing:
                warning = ""
                payload = store.sha256_text(store.canonical_json(packet))
                if existing["payload_sha256"] != payload:
                    warning = (
                        f"change {packet['change_id']} was already decided with a different payload; "
                        "returning its original receipt"
                    )
                if not warning and existing["status"] == "held":
                    warning = existing["hold_reason"]  # a resubmitted held packet still names why
                self._end_txn("ROLLBACK")
                return Outcome(
                    receipt_from_row(existing),
                    _EXIT[existing["status"]],
                    duplicate=True,
                    integrity=None,
                    warning=warning,
                    dry_run=dry_run,
                )
            return self._decide(packet, review=None)
        except (Unreadable, Unavailable, UnknownChange, NotAllowed):
            if self._ctx:
                self._end_txn("ROLLBACK")
            raise
        except Exception:
            if self._ctx:
                self._end_txn("ROLLBACK")
            raise
        finally:
            self._packets = None
            self._dry_run = False

    def approve(self, change_id: str, approver: str, *, via: str = "") -> Outcome:
        return self._review(change_id, approver, refuse_note=None, via=via)

    def refuse(self, change_id: str, approver: str, note: str, *, via: str = "") -> Outcome:
        return self._review(change_id, approver, refuse_note=note, via=via)

    def _review(self, change_id: str, approver: str, refuse_note: str | None, via: str = "") -> Outcome:
        self.preconditions()
        self._begin()
        try:
            existing = self._write_log(change_id)
            if existing is None:
                self._end_txn("ROLLBACK")
                raise UnknownChange(f"no change {change_id}")
            if existing["status"] != "held":
                self._end_txn("ROLLBACK")
                raise NotAllowed(f"change {change_id} is already {existing['status']}")
            packet = json.loads(existing["packet_json"])  # trusted: write_log.packet_json, the packet the applier stored
            agent = packet["agent"]
            policy = self.config.agents.get(agent)
            if (
                policy is None
                or approver not in self.config.agents
                or approver not in policy.approvers
            ):
                self._end_txn("ROLLBACK")
                raise NotAllowed(f"{approver} may not review changes from {agent}")
            if refuse_note is not None:
                decided_at = store.utc_now()
                self._update_log(
                    existing,
                    status="rejected",
                    decided_at=decided_at,
                    error=f"refused by {approver}: {refuse_note}",
                    targets=[],
                    applied_seq=None,
                    reviewed_by=approver,
                    reviewed_at=decided_at,
                    approved_via=via,
                )
                self._end_txn("COMMIT")
                return self._finish(change_id)
            return self._decide(packet, review=approver, existing=existing, via=via)
        except (Unreadable, Unavailable, UnknownChange, NotAllowed):
            if self._ctx:
                self._end_txn("ROLLBACK")
            raise
        except Exception:
            if self._ctx:
                self._end_txn("ROLLBACK")
            raise

    def _decide(self, packet: dict, review: str | None, existing=None, via: str = "") -> Outcome:
        decided_at = store.utc_now()
        errors = validate.validate_packet(packet)
        if errors:
            return self._finish_decision(
                packet, "rejected", self._packet_error(errors), [], decided_at, review, existing, via=via
            )
        agent = packet["agent"]
        policy_error = self._agent_allowed(agent, packet["ops"], review)
        if policy_error:
            return self._finish_decision(
                packet, "rejected", policy_error, [], decided_at, review, existing, via=via
            )
        redacted = self._cites_tombstone(packet["ops"])
        if redacted:
            return self._finish_decision(packet, "rejected", redacted, [], decided_at, review, existing, via=via)
        policy = None if agent == config_mod.RESERVED_AGENT else self.config.agents[agent]
        # An evaluated candidate runs in the savepoint for its verdict.
        evaluating = review is None and _evaluated(policy, packet)
        if review is None and policy is not None and policy.approval in ("required", "verified") and not evaluating:
            return self._finish_decision(packet, "held", "", [], decided_at, None, existing)
        self.con.execute("SAVEPOINT ops")
        self._hold = ""
        self._notes = []
        self._evaluating = evaluating
        try:
            targets, error, status = self._run_ops(packet, decided_at)
        except Exception:
            self.con.execute("ROLLBACK TO ops")
            self.con.execute("RELEASE ops")
            raise
        finally:
            self._evaluating = False
        if error:
            self.con.execute("ROLLBACK TO ops")
            self.con.execute("RELEASE ops")
            if review:
                error = f"approved by {review} but not applied: {error}"
            return self._finish_decision(packet, status, error, [], decided_at, review, existing, via=via)
        hold = self._hold
        if hold and review is None:
            # Every op ran with its hold condition treated as met. Nothing applies until a person
            # approves, and an agent nobody can approve for is refused (rulings R5, R6).
            self.con.execute("ROLLBACK TO ops")
            self.con.execute("RELEASE ops")
            approvers = () if policy is None else policy.approvers
            if not approvers:
                return self._finish_decision(
                    packet, "rejected", f"{hold}, and agent {agent} has no approvers", [], decided_at, None,
                    existing,
                )
            return self._finish_decision(
                packet, "held", "", [], decided_at, None, existing, hold_reason=hold, warning=hold
            )
        self.con.execute("RELEASE ops")
        return self._finish_decision(
            packet, "applied", "", targets, decided_at, review, existing, warning="; ".join(self._notes), via=via
        )

    def _is_tombstone_id(self, source_id) -> bool:
        if not isinstance(source_id, str):
            return False
        found = self.con.execute("SELECT title FROM sources WHERE id = ?", (source_id,)).fetchone()
        if found is None or found[0] != scrub_mod.TOMBSTONE_TITLE:
            return False
        return scrub_mod.is_tombstone(self._load_row("sources", "id", source_id))

    def _redacted_copy_of(self, op) -> str | None:
        """The id of the tombstone whose stored content_sha256 equals the hash of an add_source op's text, when
        the op's own id is not that tombstone. A redacted text is refused under any id."""
        if not isinstance(op, dict) or op.get("op") != "add_source":
            return None
        row = op.get("row") if isinstance(op.get("row"), dict) else {}
        content = row.get("content")
        if not isinstance(content, str) or not content:
            return None
        for ident, title, text, digest in self.con.execute(
            "SELECT id, title, content, content_sha256 FROM sources WHERE title = 'redacted' AND content_sha256 = ? "
            "AND id <> ? ORDER BY id",
            (store.sha256_text(content), row.get("id") if isinstance(row.get("id"), str) else ""),
        ):
            if scrub_mod.is_tombstone({"title": title, "content": text, "content_sha256": digest}):
                return ident
        return None

    def _cites_tombstone(self, ops) -> str:
        """An op other than add_source and redact_source that cites a redacted source is
        refused before the evidence gate can build a snippet of the text it carries. So is an add_source
        of a redacted text under another id."""
        for i, op in enumerate(ops):
            if not isinstance(op, dict) or op.get("op") == "redact_source":
                continue
            linked = next((other for other in _link_source_ids(op) if self._is_tombstone_id(other)), None)
            if linked is not None:
                return f"op {i} ({op.get('op')}): source {linked} is redacted"
            sid = scrub_mod.op_source(op)
            if not self._is_tombstone_id(sid):
                copy_of = self._redacted_copy_of(op)
                if copy_of is not None:
                    return f"op {i} (add_source): this text was redacted (source {copy_of})"
                continue
            if op.get("op") == "add_source":
                # the re-sent original (same hash) is the E3 no-op; any other text for that id is refused
                row = op.get("row") if isinstance(op.get("row"), dict) else {}
                stored = self._load_row("sources", "id", sid)
                if isinstance(row.get("content"), str) and store.sha256_text(row["content"]) == stored["content_sha256"]:
                    continue
            return f"op {i} ({op.get('op')}): source {sid} is redacted"
        return ""

    def _log_tombstones(self, packet) -> set:
        """Every id whose ops the stored copy of a packet must lose the text of: the redacted sources it cites
        (an add_source of one included) and the new ids of add_source ops that repeat a redacted text."""
        found = set()
        ops = packet.get("ops") if isinstance(packet, dict) else None
        for op in ops if isinstance(ops, list) else ():
            if isinstance(op, dict) and op.get("op") != "redact_source":
                found.update(other for other in _link_source_ids(op) if self._is_tombstone_id(other))
                sid = scrub_mod.op_source(op)
                if self._is_tombstone_id(sid):
                    found.add(sid)
                elif self._redacted_copy_of(op) is not None:
                    found.add(sid)
        return found

    def _agent_allowed(self, agent: str, ops: list, review: str | None = None) -> str:
        if agent == config_mod.RESERVED_AGENT:
            if not self.allow_reserved_agent:
                return "agent brain is reserved for brain init and brain apply-config"
            for i, op in enumerate(ops):
                name = op.get("op")
                if name not in _BRAIN_OPS:
                    return f"op {i} ({name}): not allowed for agent {agent}"
            return ""
        policy = self.config.agents.get(agent)
        if policy is None:
            return f"unknown agent {agent}"
        if not self.collect:
            for collector in self.config.collectors.values():
                if collector.agent == agent:
                    return (
                        f"agent {agent} belongs to collector {collector.name} "
                        "and submits only through brain collect"
                    )
        if not self.extract and review is None and policy.kind == "extractor":
            return f"agent {agent} is an extractor and submits only through brain extract"
        for i, op in enumerate(ops):
            name = op.get("op")
            if not policy.allows(name):
                return f"op {i} ({name}): not allowed for agent {agent}"
            # 1.8.0: refused here, before any hold, so no approver is asked about a packet that can't apply.
            if name == "set_source_author" and not policy.author_principal:
                return f"op {i} ({name}): " + advice.author_principal_error()
        return ""

    def _run_ops(self, packet: dict, decided_at: str) -> tuple[list[dict], str, str]:
        agent = packet["agent"]
        change_id = packet["change_id"]
        written: set[tuple[str, str]] = set()
        targets: list[dict] = []
        policy = None if agent == config_mod.RESERVED_AGENT else self.config.agents[agent]
        principal = None
        if self.config.principal:
            principal = evidence_gate.Principal(
                self.config.principal.person_id,
                self.config.principal.display_name,
                self.config.principal.names,
            )
        self._links = []
        for i, op in enumerate(packet["ops"]):
            name = op["op"]
            if name == "apply_candidate":
                err, status, extra = self._apply_candidate(
                    i, op, agent, change_id, decided_at, policy, principal, written
                )
                if err:
                    return [], err, status
                targets.extend(extra)
                continue
            if name in schemas.COMMAND_OPS:
                if name == "rebuild_search":
                    store.rebuild_search(self.con)
                    targets.append({"table": "search_index", "id": "*", "action": "rebuild"})
                    continue
                if name == "redact_source":
                    from brain import redact_ops

                    err, status, extra = redact_ops.redact_source(self, str(i), op, agent, change_id, decided_at)
                    if err:
                        return [], err, status
                    targets.extend(extra)
                    continue
                raise RuntimeError(f"op {i} ({name}): unhandled command op")
            kind = schemas.OPS[name]["kind"]
            if kind == "claim":
                err, status, extra = self._add_claim(
                    str(i), name, op, agent, change_id, decided_at, policy, written
                )
            elif kind == "resolve":
                err, status, extra = self._resolve_fact(
                    str(i), name, op, agent, change_id, decided_at, policy, written
                )
            elif kind == "aliases":
                err, status, extra = self._set_aliases(str(i), name, op, agent, change_id, decided_at)
            elif kind == "author":
                err, status, extra = self._set_source_author(str(i), name, op, agent, change_id, decided_at, policy)
            elif kind == "verify":
                err, status, extra = self._verify_claim(
                    str(i), name, op, agent, change_id, decided_at, policy, written
                )
            else:
                err, status, extra = self._apply_op(
                    str(i), name, op, agent, change_id, decided_at, policy, principal, written,
                    from_packet=True,
                )
            if err:
                return [], err, status
            targets.extend(extra)
        err = self._resolve_links(agent, change_id, decided_at, targets)
        if err:
            return [], err, "rejected"
        return targets, "", "applied"

    def _resolve_links(self, agent, change_id, decided_at, targets) -> str:
        """Each legacy superseded_by link, after the packet's last op, in packet order."""
        stamp = f"{agent}:{change_id}"
        label = ""
        try:
            for label, claim_id, target_id, key in self._links:
                target = self._load_row("claims", "id", target_id)
                if target is None:
                    return (
                        f"op {label} (add_claim): superseded_by {target_id} is not a claim in this packet or "
                        "the store"
                    )
                if target["key"] != key:
                    return f"op {label} (add_claim): superseded_by {target_id}, a claim on another key"
                if self._follows(target_id, claim_id):
                    return f"op {label} (add_claim): superseded_by would form a cycle through {target_id}"
                self._set_claim(claim_id, "superseded", decided_at, stamp, targets, superseded_by=target_id)
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, "add_claim", exc, "claims")
        return ""

    def _follows(self, start: str, target: str) -> bool:
        """Whether following superseded_by from start reaches target, bounded at 10,000 steps."""
        return (
            self.con.execute(
                "WITH RECURSIVE chain(id, depth) AS ("
                "SELECT ?, 0 UNION ALL "
                "SELECT c.superseded_by, chain.depth + 1 FROM chain JOIN claims AS c ON c.id = chain.id "
                "WHERE c.superseded_by IS NOT NULL AND chain.depth < 10000"
                ") SELECT 1 FROM chain WHERE id = ? LIMIT 1",
                (start, target),
            ).fetchone()
            is not None
        )

    def _apply_candidate(
        self, index, op, agent, change_id, decided_at, policy, principal, written
    ) -> tuple[str, str, list[dict]]:
        plan = candidates.plan(self.con, self.config, op["candidate"], decided_at, agent=agent)
        if not self._evaluating:
            if plan.errors:
                return _candidate_invalid(index, plan.errors), "rejected", []
            return self._run_derived(index, plan, agent, change_id, decided_at, policy, principal, written)
        # The verdict, worked out in the ops savepoint. The first reason found stands.
        required = policy is not None and policy.approval == "required"
        if plan.errors:
            return self._verdict(_candidate_invalid(index, plan.errors), [])
        if plan.kind == "decision_candidate":
            return self._verdict(f"op {index} (apply_candidate): a decision candidate always needs a person", [])
        watch = self._conflict_watch(index, plan)
        err, status, targets = self._run_derived(
            index, plan, agent, change_id, decided_at, policy, principal, written
        )
        if err:
            refusal = re.fullmatch(rf"op [0-9.]+ \([a-z_]+\): not allowed for agent {re.escape(agent)}", err)
            e39 = re.match(r"op [0-9.]+ \(add_claim\): " + re.escape(_E39_PREFIX), err)
            if (refusal or e39) and not required:
                return err, "rejected", []
            return self._verdict(err, [])
        conflict = self._conflict_reason(watch, targets)
        if conflict:
            return self._verdict(conflict, targets)
        if self._hold:
            # Rule 3: another writer's claim superseded. Its 1a message stands.
            return self._verdict(self._hold, targets)
        if plan.kind == "action_candidate" and op["candidate"]["fields"].get("status_basis") == "inferred":
            return self._verdict(
                f"op {index} (apply_candidate): an action with status_basis inferred needs a person", targets
            )
        stored = self._run_window(agent, decided_at)
        if stored >= RUN_WINDOW_STORES:
            return self._verdict(
                f"op {index} (apply_candidate): mass-change guard: {agent} stored {stored} candidates in the last hour",
                targets,
            )
        if required:
            return self._verdict(
                f"op {index} (apply_candidate): approval is required; under verified it would be stored", targets
            )
        return "", "applied", targets

    def _verdict(self, reason: str, targets: list[dict]) -> tuple[str, str, list[dict]]:
        """Hold an evaluated candidate: the verdict's reason replaces any reason _note_hold kept."""
        self._hold = reason
        return "", "applied", targets

    def _run_derived(self, index, plan, agent, change_id, decided_at, policy, principal, written):
        if not plan.appliable:
            raise RuntimeError(f"op {index} (apply_candidate): unhandled command op")
        targets: list[dict] = []
        for j, derived in enumerate(plan.ops):
            name = derived["op"]
            label = f"{index}.{j}"
            if policy is None or not policy.allows(name):
                return f"op {label} ({name}): not allowed for agent {agent}", "rejected", []
            if name == "add_claim":
                err, status, extra = self._add_claim(
                    label, name, derived, agent, change_id, decided_at, policy, written, engine_id=True
                )
            else:
                err, status, extra = self._apply_op(
                    label, name, derived, agent, change_id, decided_at, policy, principal, written,
                    from_packet=False,
                )
            if err:
                return err, status, []
            targets.extend(extra)
        return "", "applied", targets

    def _run_window(self, agent: str, decided_at: str) -> int:
        """The agent's candidates stored without a person in the hour up to decided_at."""
        since = (_parse_utc(decided_at) - timedelta(seconds=RUN_WINDOW_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self.con.execute(
            "SELECT count(*) FROM write_log WHERE agent = ? AND decided_at >= ? AND status = 'applied' "
            "AND reviewed_by = '' AND json_extract(packet_json, '$.ops[0].op') = 'apply_candidate'",
            (agent, since),
        ).fetchone()[0]

    def _conflict_watch(self, index, plan) -> list[dict]:
        """Before the op: for each derived add_claim, its bucket's values, and for a
        version key, each other key of the entity's now values."""
        watch: list[dict] = []
        for j, derived in enumerate(plan.ops):
            if derived.get("op") != "add_claim":
                continue
            row = derived["row"]
            key = row["key"]
            if self._load_row("fact_keys", "key", key) is None:
                continue
            state = truth.state(self.con, key, self.config.truth)
            label = state.label_for(row.get("scope", ""))
            bucket = state.bucket_for(row.get("scope", ""))
            others = {}
            entity = truth.key_parts(key)[0]
            registry = self._load_row("fact_keys", "key", key)
            if truth.is_version_key(key, registry["value_type"]):
                for (other,) in self.con.execute("SELECT key FROM fact_keys WHERE key <> ? ORDER BY key", (key,)):
                    if truth.key_parts(other)[0] != entity:
                        continue
                    other_state = truth.state(self.con, other, self.config.truth)
                    others[other] = (
                        {claim["canonical"] for claim in other_state.now.claims},
                        other_state.now.disputed,
                    )
            watch.append(
                {
                    "label": f"{index}.{j}",
                    "id": row["id"],
                    "key": key,
                    "scope": row.get("scope", ""),
                    "bucket": label,
                    "values": {claim["canonical"] for claim in bucket.claims},
                    "disputed": bucket.disputed,
                    "others": others,
                }
            )
        return watch

    def _conflict_reason(self, watch: list[dict], targets: list[dict]) -> str:
        """After the op: would it open or widen a conflict?"""
        inserted = {item["id"] for item in targets if item.get("table") == "claims" and item.get("action") == "insert"}
        for item in watch:
            label = item["label"]
            if item["id"] in inserted:
                claim = self._claim(item["id"])
                if claim is not None and claim["status"] == "live":
                    state = truth.state(self.con, item["key"], self.config.truth)
                    bucket = state.bucket_for(claim["scope"])
                    if bucket.disputed and claim["canonical"] not in item["values"]:
                        verb = "widen" if item["disputed"] else "open"
                        return schemas.held_conflict_reason(label, verb, item["key"], item["bucket"])
            for other, (values, was_disputed) in item["others"].items():
                state = truth.state(self.con, other, self.config.truth)
                if state.now.disputed and any(claim["canonical"] not in values for claim in state.now.claims):
                    verb = "widen" if was_disputed else "open"
                    return schemas.held_conflict_reason(label, verb, other, "now")
        return ""

    def _apply_op(
        self, label, name, op, agent, change_id, decided_at, policy, principal, written,
        from_packet=False,
    ) -> tuple[str, str, list[dict]]:
        spec = schemas.OPS[name]
        row = dict(op["row"])
        table = spec["table"]
        key_col = spec["key"]
        if key_col not in row:
            return f"op {label} ({name}): row is missing {key_col}", "rejected", []
        key = row[key_col]
        current = self._load_row(table, key_col, key)
        err = self._scope(label, name, policy, row, current)
        if err:
            return err, "rejected", []
        if spec["kind"] == "upsert":
            base = op.get("base")
            if (table, str(key)) not in written:
                if base is None and current is not None:
                    return (
                        f"op {label} ({name}): " + advice.base_exists_error(table, key, current["version"])
                    ), "conflict", []
                if base is not None and current is None:
                    return (
                        f"op {label} ({name}): " + advice.base_missing_error(table, key, base)
                    ), "conflict", []
                if base is not None and current is not None and base != current["version"]:
                    return (
                        f"op {label} ({name}): {table} {key} has moved on: base {base}, "
                        f"now {current['version']}; re-read and resubmit"
                    ), "conflict", []
        err = self._semantic(label, name, spec, row, current, op, agent, decided_at, principal)
        if err:
            return err, "rejected", []
        err = self._original_times(label, name, row, current, policy, agent, decided_at)
        if err:
            return err, "rejected", []
        # 0008: an upsert that names only the row and adds links leaves the row, and its version, alone. Read the op's
        # own row: resolving people adds a key to the working copy.
        links_only = (
            name in schemas.LINK_OPS and current is not None and bool(op.get("sources")) and set(op["row"]) <= {key_col}
        )
        reserved = agent == config_mod.RESERVED_AGENT
        legacy_insert = (
            from_packet
            and policy is not None
            and policy.legacy_import
            and current is None
            and name in LEGACY_OPS
        )
        if name in schemas.NOTE_FIELDS:
            # The switch, then the cap.
            err = strict.note_error(
                label, name, row, current, self.config.writes,
                cap_exempt=reserved or legacy_insert, switch_exempt=reserved,
            )
            if err:
                return err, "rejected", []
        if from_packet and not reserved and name in strict.STATE_DATES:
            # A direct op's state dates come from a cited source (0008: any of them).
            err = strict.date_error(label, name, row, current, self._cited_rows(op))
            if err:
                return err, "rejected", []
        set_notes = [field for field in schemas.NOTE_FIELDS.get(name, ()) if strict.sets(row, current, field)]
        marker_sentences: list[dict] | None = None
        if spec["kind"] == "upsert":
            # 0008: the gate reads every source the op cites, the primary first. 1.9.0: then each one's
            # speaker-lines excerpts, so the principal's own lines in a thread count as hers and nobody else's do.
            cited_rows = self._cited_rows(op)
            cited = [_cited_source(src) for src in cited_rows + self._speaker_lines(cited_rows)]
            findings = evidence_gate.check_op(
                self.con, name, row, current, cited, principal
            )
            if findings:
                legacy = (
                    from_packet
                    and policy is not None
                    and policy.legacy_import
                    and current is None
                    and name in LEGACY_OPS
                )
                if not legacy:
                    return (
                        f"op {label} ({name}): evidence gate: {' '.join(findings)}",
                        "rejected",
                        [],
                    )
                flags = evidence_gate.flag_op(self.con, name, row, current, cited, principal)
                marker_sentences = _marker_elements(flags)
                if len(marker_sentences) > _MARKER_MAX:
                    return f"op {label} ({name}): more than {_MARKER_MAX} unchecked sentences", "rejected", []
        if name == "upsert_fact":
            err, status, targets = self._upsert_fact(
                label, name, row, current, op, agent, change_id, decided_at, written
            )
            if not err:
                written.add((table, str(key)))
            return err, status, targets
        try:
            action = None if links_only else self._write(spec, row, current, op, agent, change_id, decided_at)
            targets: list[dict] = []
            if action:
                targets.append({"table": table, "id": key, "action": action})
                targets.extend(
                    self._marker_targets(
                        name, table, key, key_col, marker_sentences, op, f"{agent}:{change_id}", decided_at
                    )
                )
                if set_notes:
                    self._note_meta(name, key, row, set_notes, agent, decided_at)
                if name == "add_source" and action == "insert" and row.get("supersedes_id"):
                    # Every agent's superseding source carries the still-quoted claims.
                    self._carry_forward(row, f"{agent}:{change_id}", decided_at, targets, written)
                if name == "upsert_decision":
                    self._close_decision_drift(key, f"{agent}:{change_id}", decided_at, targets)
            if name in schemas.LINK_OPS:
                targets.extend(self._write_links(table, key, op, f"{agent}:{change_id}", decided_at))
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, table, row), "rejected", []
        written.add((table, str(key)))
        return "", "applied", targets

    def _cited_rows(self, op: dict) -> list[dict]:
        """0008: every source an op cites, as stored rows: its envelope source, then each of `sources` once."""
        ids = [op["source"]["source_id"], *_link_source_ids(op)]
        return [self._load_row("sources", "id", sid) for sid in dict.fromkeys(ids)]

    def _speaker_lines(self, rows: list[dict]) -> list[dict]:
        """1.9.0: the speaker-lines excerpts of the given sources (external_ref speaker-lines:<id>), not already
        among them. sources_by_external_ref (0005) serves the lookup."""
        seen = {row["id"] for row in rows}
        found: list[dict] = []
        for row in rows:
            for excerpt in self.con.execute(
                # `external_ref <> ''` lets SQLite use the partial index; without it every lookup scans all sources.
                "SELECT * FROM sources WHERE external_ref = ? AND external_ref <> '' ORDER BY id",
                (evidence_gate.SPEAKER_LINES + row["id"],),
            ):
                if excerpt["id"] not in seen:
                    seen.add(excerpt["id"])
                    found.append(dict(excerpt))
        return found

    def _speaker_lines_error(self, row: dict) -> str:
        """1.9.0: a speaker-lines excerpt names a stored source that isn't itself an excerpt, and each of its
        paragraphs is one line copied exactly from that source, starting and ending on whole words. A paragraph of
        two lines could carry the next speaker's line with it."""
        parent_id = row["external_ref"][len(evidence_gate.SPEAKER_LINES):]
        parent = self._load_row("sources", "id", parent_id)
        if parent is None or str(parent["external_ref"]).startswith(evidence_gate.SPEAKER_LINES):
            return advice.speaker_lines_parent_error(parent_id)
        for part in evidence_gate.paragraphs(row["content"]):
            if (
                has_line_break(part)
                or part not in parent["content"]
                or not evidence_gate.contains(self._content_key(parent), evidence_gate.match_key(part))
            ):
                return advice.speaker_lines_copy_error(evidence_gate.snippet(part), parent_id)
        return ""

    def _original_times(self, label, name, row, current, policy, agent, decided_at) -> str:
        """0008 part D: a legacy_import agent's insert may carry its original created_at and updated_at. Each is a
        UTC time no later than the apply time, and updated_at is not before created_at. A missing one takes the
        other's value. Any other agent, and any update, is refused."""
        sent = [field for field in schemas.ORIGINAL_TIMES if field in row]
        if name not in schemas.ORIGINAL_TIME_OPS or not sent:
            return ""
        if policy is None or not policy.legacy_import:
            return f"op {label} ({name}): " + advice.original_times_agent_error(sent[0], agent)
        if current is not None:
            return f"op {label} ({name}): " + advice.original_times_update_error(sent[0])
        for field in sent:
            if row[field] > decided_at:
                return f"op {label} ({name}): " + advice.original_times_future_error(field, row[field], decided_at)
        row.setdefault("created_at", row.get("updated_at"))
        row.setdefault("updated_at", row["created_at"])
        if row["updated_at"] < row["created_at"]:
            return f"op {label} ({name}): " + advice.original_times_order_error(row["created_at"], row["updated_at"])
        return ""

    def _write_links(self, table: str, key, op: dict, stamp: str, decided_at: str) -> list[dict]:
        """0008: one links row per item of `sources`, after the row is written. A link that already exists (same
        subject, label and source) is left alone; each new one is a receipt target under table links."""
        targets: list[dict] = []
        for item in op.get("sources") or ():
            label = item.get("label", schemas.LINK_DEFAULT_LABEL)
            cursor = self.con.execute(
                "INSERT INTO links (subject_table, subject_id, label, source_id, locator, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (subject_table, subject_id, label, source_id) DO NOTHING",
                (table, key, label, item["source_id"], item["locator"], decided_at, stamp),
            )
            if cursor.rowcount:
                targets.append({"table": "links", "id": link_target_id(table, key, label, item["source_id"]),
                                "action": "insert"})
        return targets

    def _note_meta(self, name: str, key, row: dict, fields: list[str], agent: str, decided_at: str) -> None:
        """Who wrote each note field the op set, and when. It adds no receipt target."""
        entity = schemas.NOTE_ENTITY[name]
        for field in fields:
            if row[field] != "":
                self.con.execute(
                    "INSERT INTO note_meta (entity, ref_id, field, written_by, written_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (entity, ref_id, field) DO UPDATE SET written_by = excluded.written_by, "
                    "written_at = excluded.written_at",
                    (entity, key, field, agent, decided_at),
                )
            else:
                self.con.execute(
                    "DELETE FROM note_meta WHERE entity = ? AND ref_id = ? AND field = ?", (entity, key, field)
                )

    def _marker_targets(
        self, name, table, key, key_col, marker_sentences, op, stamp, decided_at
    ) -> list[dict]:
        entity = _MARKER_ENTITY.get(table)
        if entity is None or op.get("source") is None:
            return []
        ref = f"{entity}:{key}"
        stored = self.con.execute(
            "SELECT sentences FROM unchecked_text WHERE entity = ? AND ref_id = ?", (entity, key)
        ).fetchone()
        if stored is None:
            if marker_sentences is None:
                return []
            self.con.execute(
                "INSERT INTO unchecked_text (entity, ref_id, sentences, source_id, created_at, created_by, "
                "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity,
                    key,
                    store.canonical_json(marker_sentences),
                    op["source"]["source_id"],
                    decided_at,
                    stamp,
                    decided_at,
                    stamp,
                ),
            )
            return [{"table": "unchecked_text", "id": ref, "action": "insert"}]
        row = self._load_row(table, key_col, key)
        keys = {
            _sentence_key(sentence)
            for field in evidence_gate.GATE_FIELDS[name]
            for sentence in evidence_gate.sentences(row.get(field))
        }
        elements = json.loads(stored[0])  # trusted: unchecked_text.sentences, written by the applier
        kept = [item for item in elements if isinstance(item, dict) and item.get("key") in keys]
        if not kept:
            self.con.execute(
                "DELETE FROM unchecked_text WHERE entity = ? AND ref_id = ?", (entity, key)
            )
            return [{"table": "unchecked_text", "id": ref, "action": "delete"}]
        if len(kept) != len(elements):
            self.con.execute(
                "UPDATE unchecked_text SET sentences = ?, updated_at = ?, updated_by = ? "
                "WHERE entity = ? AND ref_id = ?",
                (store.canonical_json(kept), decided_at, stamp, entity, key),
            )
            return [{"table": "unchecked_text", "id": ref, "action": "update"}]
        return []

    def _scope(self, label: str, name: str, policy, row: dict, current: dict | None) -> str:
        if policy is None:
            return ""
        spec = schemas.OPS[name]
        if spec["scope"] == "id" and policy.scope_projects is not None:
            project_id = row.get("id") if "id" in row else (current or {}).get("id")
            if project_id not in policy.scope_projects:
                return (
                    f"op {label} ({name}): project {project_id} is outside agent {policy.name}'s "
                    f"scope [{', '.join(policy.scope_projects)}]"
                )
        if spec["scope"] == "project_id" and policy.scope_projects is not None:
            new_p = row["project_id"] if "project_id" in row else (current or {}).get("project_id")
            old_p = (current or {}).get("project_id")
            if new_p is None or new_p not in policy.scope_projects:
                return (
                    f"op {label} ({name}): project {new_p} is outside agent {policy.name}'s "
                    f"scope [{', '.join(policy.scope_projects)}]"
                )
            if current is not None and old_p not in policy.scope_projects:
                return (
                    f"op {label} ({name}): project {old_p} is outside agent {policy.name}'s "
                    f"scope [{', '.join(policy.scope_projects)}]"
                )
        if spec["scope"] == "fact" and policy.scope_fact_prefixes is not None:
            key = row.get("key") or (current or {}).get("key")
            if not any(key == prefix or str(key).startswith(prefix + ".") for prefix in policy.scope_fact_prefixes):
                return (
                    f"op {label} ({name}): fact key {key} is outside agent {policy.name}'s "
                    f"fact prefixes [{', '.join(policy.scope_fact_prefixes)}]"
                )
        return ""

    def _refusal(self, label, name, exc, table, row=None) -> str:
        """A database rule that refused an op, in words that name the fix."""
        return f"op {label} ({name}): " + advice.integrity_error(self.con, exc, table, row)

    @staticmethod
    def _missing(spec: dict, missing: list[str], *, to_create: bool = True) -> str:
        """Every missing required field in one message, with the allowed values of the enum ones."""
        properties = spec["row"].get("properties") or {}
        enums = {name: properties[name]["enum"] for name in missing if "enum" in properties.get(name, {})}
        return advice.missing_fields_error(missing, enums, to_create=to_create)

    def _semantic(
        self,
        label: str,
        name: str,
        spec: dict,
        row: dict,
        current: dict | None,
        op: dict,
        agent: str,
        decided_at: str,
        principal,
    ) -> str:
        creating = current is None
        if spec["kind"] == "upsert":
            source_id = op["source"]["source_id"]
            if self._load_row("sources", "id", source_id) is None:
                return f"op {label} ({name}): " + advice.source_missing_error(source_id)
            for linked in _link_source_ids(op):
                if self._load_row("sources", "id", linked) is None:
                    return f"op {label} ({name}): " + advice.link_source_missing_error(linked)
        if name == "upsert_signal":
            # E29: only the applier's derivation opens and closes conflict signals.
            if row.get("kind") == "conflict" or (current is not None and current.get("kind") == "conflict"):
                return f"op {label} ({name}): conflict signals are opened and closed only by the applier"
            if creating and str(row.get("id", "")).startswith("sig-conflict-"):
                return f"op {label} ({name}): signal ids starting sig-conflict- are reserved for the applier"
            if creating and str(row.get("id", "")).startswith("sig-carry-"):
                return f"op {label} ({name}): signal ids starting sig-carry- are reserved for the applier"
            if creating and str(row.get("id", "")).startswith("sig-redact-"):
                return f"op {label} ({name}): signal ids starting sig-redact- are reserved for the applier"
        if creating:
            missing = [field for field in spec["required_create"] if field not in row]
            if missing:
                return f"op {label} ({name}): " + self._missing(spec, missing)
            if name == "upsert_thread_link":
                has_action = row.get("action_id") not in (None, "") if "action_id" in row else False
                has_item = (
                    row.get("checklist_item_id") not in (None, "")
                    if "checklist_item_id" in row
                    else False
                )
                if has_action == has_item:
                    return f"op {label} ({name}): link needs exactly one of action_id and checklist_item_id"
        if current is not None:
            for field in spec["identity"]:
                if field in row and not _same(row[field], current.get(field), field, spec):
                    return f"op {label} ({name}): {field} cannot change"
        decided = _parse_utc(decided_at)
        for field in spec["utc_fields"]:
            value = row.get(field) if field in row else None
            if isinstance(value, str):
                when = _parse_utc(value)
                if when - decided > _FUTURE_SLACK:
                    return f"op {label} ({name}): " + advice.future_error(field, decided_at)
        if name == "add_source":
            if current is None and row.get("kind") == "observation" and "author_person_id" not in row:
                # 1.9.0: an observation is the agent's own note, so one that leaves its author out is written by the
                # agent's person_id in brain.toml. An explicit null stays null.
                policy = self.config.agents.get(agent)
                own = policy.person_id if policy is not None else None
                if own is not None:
                    if self._load_row("people", "id", own) is None:
                        return f"op {label} ({name}): " + advice.agent_person_missing_error(agent, own)
                    row["author_person_id"] = own
            author = row.get("author_person_id")
            if (
                principal
                and author == principal.person_id
                and (
                    agent == config_mod.RESERVED_AGENT
                    or not self.config.agents[agent].author_principal
                )
            ):
                if agent != config_mod.RESERVED_AGENT:
                    return (
                        f"op {label} ({name}): agent {agent} may not add a source authored by the principal. "
                        f"Cite an existing source that {principal.display_name} ({principal.person_id}) wrote, such "
                        "as a collected email, Slack message or transcript, or add this source without "
                        "author_person_id."
                    )
            existing = current
            if existing is not None:
                missing = [field for field in spec["required_create"] if field not in row]
                if missing:
                    return f"op {label} ({name}): " + self._missing(spec, missing, to_create=False)
                proposed = self._source_fields(row, None)
                if "author_person_id" not in row:
                    # 1.9.0: a re-sent source that leaves its author out matches the author stored, whether a
                    # default, a correction or the first sender set it.
                    proposed.pop("author_person_id")
                if not _source_same(existing, proposed) and not _tombstone_same(existing, proposed):
                    # 1.8.0: the same source re-sent with the author set_source_author replaced. "Use a new id"
                    # would store it again under the old author, so name the author it has now. 1.9.0: the same for
                    # an author a default gave it.
                    if _source_same({**existing, "author_person_id": proposed.get("author_person_id")}, proposed):
                        if self.con.execute(
                            "SELECT 1 FROM source_author_changes WHERE source_id = ? AND applied = 1", (row["id"],)
                        ).fetchone():
                            return (
                                f"op {label} ({name}): "
                                + advice.author_corrected_error(row["id"], existing["author_person_id"])
                            )
                        return f"op {label} ({name}): " + advice.author_stored_error(row["id"], existing["author_person_id"])
                    return (
                        f"sources never change: {row['id']} already exists with different content; "
                        "use a new id"
                    )
            else:
                if str(row.get("external_ref", "")).startswith(evidence_gate.SPEAKER_LINES):
                    err = self._speaker_lines_error(row)
                    if err:
                        return f"op {label} ({name}): {err}"
                if session_cite.is_excerpt(row):
                    # A session excerpt is stored only when it is exactly what the session record holds.
                    err = session_cite.check(self.instance_dir, self.config, row)
                    if err:
                        return f"op {label} ({name}): {err}"
                if row.get("supersedes_id") is not None:
                    err = self._source_successor(label, name, row, agent)
                    if err:
                        return err
        if name == "record_collector_run" and current is not None:
            return f"collector_runs is append-only: {row['id']} already exists"
        if name == "upsert_meeting":
            thread_id = row["thread_id"] if "thread_id" in row else (current or {}).get("thread_id")
            thread = self._load_row("threads", "id", thread_id) if thread_id else None
            if thread is None or thread["channel"] != "calendar":
                return f"op {label} ({name}): thread {thread_id} is not a calendar thread"
        if name in {"upsert_project"} and agent != config_mod.RESERVED_AGENT:
            kind = row["kind"] if "kind" in row else (current or {}).get("kind")
            if kind == "workstream" or (current and current.get("kind") == "workstream"):
                return f"op {label} ({name}): configured agents cannot create or change workstreams"
        if name == "upsert_project" and row.get("parent_id") is not None:
            problem = self._folder_problem(row["id"], row["kind"] if "kind" in row else (current or {}).get("kind"),
                                           row["parent_id"])
            if problem:
                return f"op {label} ({name}): {problem}"
        if name == "upsert_fact_key" and agent != config_mod.RESERVED_AGENT:
            return f"op {label} ({name}): not allowed for agent {agent}"
        if name == "upsert_fact":
            key = row.get("key") or (current or {}).get("key")
            registry = self._load_row("fact_keys", "key", key)
            if registry is None:
                return f"op {label} ({name}): unknown fact key {key}"
            mapped = None
            if "value" in row:
                mapped, err = strict.map_fact_value(registry, row["value"])
                if err:
                    return f"op {label} ({name}): {err}"
                row["_mapped_value"] = mapped
            # The quote and value_raw checks.
            source = self._load_row("sources", "id", op["source"]["source_id"])
            err = strict.fact_error(label, registry, row, mapped, source)
            if err:
                return err
        if name == "upsert_fact_key" and row.get("value_words") is not None:
            value_type = row["value_type"] if "value_type" in row else (current or {}).get("value_type")
            if "allowed_values" in row:
                allowed = row["allowed_values"]
            else:
                stored = (current or {}).get("allowed_values")
                allowed = json.loads(stored) if isinstance(stored, str) else None  # trusted: fact_keys.allowed_values, written by the applier
            if config_mod.value_words_error(value_type, allowed, row["value_words"]):
                return f"op {label} ({name}): value_words is invalid"
        if name == "upsert_fact_key":
            key = row.get("key") or (current or {}).get("key")
            new_type = row["value_type"] if "value_type" in row else (current or {}).get("value_type")
            if (
                truth.key_parts(str(key))[1] == truth.VERSION_ATTRIBUTE
                and new_type != "text"
                and (current is None or current["value_type"] == "text")
            ):
                return f'op {label} ({name}): fact key {key} must have value_type = "text"'
        if name == "upsert_fact_key" and current is not None:
            fact = self._load_row("facts", "key", current["key"])
            new_type = row["value_type"] if "value_type" in row else current["value_type"]
            if fact is not None:
                if new_type != fact["value_type"]:
                    return f"op {label} ({name}): value_type cannot change while a fact uses this key"
                if "allowed_values" in row and row["allowed_values"] is not None:
                    allowed = set(row["allowed_values"])
                    if (
                        fact["value_text"] is not None
                        and fact["value_text"] not in allowed
                        and fact["value_text"] != scrub_mod.REDACTED
                    ):
                        return (
                            f"op {label} ({name}): allowed_values would drop the current fact value"
                        )
            err = self._fact_key_claims(label, name, row, current, new_type)
            if err:
                return err
        if name == "upsert_signal":
            subject_table = row["subject_table"] if "subject_table" in row else current["subject_table"]
            subject_id = row["subject_id"] if "subject_id" in row else current["subject_id"]
            kind = row["kind"] if "kind" in row else current["kind"]
            thread_id = row["thread_id"] if "thread_id" in row else (current or {}).get("thread_id")
            key_col = schemas.SUBJECT_KEY[subject_table]
            if self._load_row(subject_table, key_col, subject_id) is None:
                return f"op {label} ({name}): subject {subject_table} {subject_id} does not exist"
            if kind in {"reply", "new_message"}:
                if not thread_id:
                    return f"op {label} ({name}): reply and new_message signals need a thread"
                link_sql = (
                    "SELECT 1 FROM thread_links WHERE thread_id = ? AND status = 'active' AND "
                )
                if subject_table == "actions":
                    link = self.con.execute(
                        link_sql + "action_id = ?", (thread_id, subject_id)
                    ).fetchone()
                elif subject_table == "checklist_items":
                    link = self.con.execute(
                        link_sql + "checklist_item_id = ?", (thread_id, subject_id)
                    ).fetchone()
                else:
                    link = None
                if link is None:
                    return f"op {label} ({name}): reply and new_message signals need an active thread link"
        if name in {"upsert_action", "upsert_checklist_item"}:
            return self._resolve_people(label, name, spec, row, current)
        return ""

    # -- product truth -----------------------------------------------------------

    def _note_hold(self, reason: str) -> None:
        """Keep the first hold reason. The op goes on as if the hold were approved."""
        if not self._hold:
            self._hold = reason

    def _source_successor(self, label: str, name: str, row: dict, agent: str) -> str:
        """Who may set sources.supersedes_id on a new source."""
        superseded = row["supersedes_id"]
        target = self._load_row("sources", "id", superseded)
        if target is None:
            return f"op {label} ({name}): supersedes_id {superseded} does not exist"
        if self.con.execute(
            "SELECT 1 FROM sources WHERE supersedes_id = ?", (superseded,)
        ).fetchone():
            return f"op {label} ({name}): source {superseded} already has a successor"
        if agent == config_mod.RESERVED_AGENT:
            return ""
        # A collector's own ref rule comes first, and the author_principal shortcut is only for agents
        # that are not a collector's agent: a capture agent may supersede nothing but its own refs.
        external_ref = row.get("external_ref", "")
        collector = next((item for item in self.config.collectors.values() if item.agent == agent), None)
        if collector is not None:
            ctype = REGISTRY.get(collector.type)
            if (
                ctype is not None
                and getattr(ctype, "may_supersede", False)
                and external_ref
                and external_ref == target["external_ref"]
                and self._owns_ref(collector, ctype, row, target)
            ):
                return ""
        else:
            policy = self.config.agents.get(agent)
            if policy is not None and policy.author_principal:
                return ""
        self._note_hold(f"op {label} ({name}): supersedes_id needs a person's approval")
        return ""

    @staticmethod
    def _owns_ref(collector, ctype, row: dict, target: dict) -> bool:
        """SR13: a capture collector supersedes only what capture itself made, inside its own namespace."""
        if ctype.name != "capture":
            return True
        origin = collector.options.get("origin", "")
        return str(target["id"]).startswith("src-cap-") and str(row.get("external_ref", "")).startswith(
            f"{row.get('kind')}:{origin}/"
        )

    # -- carry-forward and drift ------------------------------------------

    def _chain(self, source_id: str) -> list[str]:
        """The superseded source and every earlier version of it, nearest first, at most
        truth.DESCENT_STEPS deep."""
        rows = self.con.execute(_CHAIN_CTE + "SELECT id FROM chain ORDER BY depth", (source_id,)).fetchall()
        return [row[0] for row in rows]

    def _carry_forward(self, row: dict, stamp: str, decided_at: str, targets: list[dict], written: set) -> None:
        """A claim whose quote is still in the new source moves onto it; one whose
        quote is gone is flagged with one drift signal; decisions are left alone. It runs no _compare and
        sets no verified time, so a carry is not a change to the fact."""
        new = self._load_row("sources", "id", row["id"])
        old_id = row["supersedes_id"]
        old = self._load_row("sources", "id", old_id)
        ref = (old["external_ref"] or old["id"])[:200]
        detail = f"source {ref} changed on {new['source_date']}; the quote is no longer in it"
        new_key = self._content_key(new)
        claim_ids: list[str] = []
        for version in self._chain(old_id):
            claim_ids.extend(
                found[0]
                for found in self.con.execute(
                    "SELECT id FROM claims WHERE source_id = ? AND status IN ('live', 'planned') AND checked = 1",
                    (version,),
                )
            )
        keys: list[str] = []
        for claim_id in sorted(claim_ids):
            claim = self._claim(claim_id)
            if claim is None:
                continue
            if self._quote_survives(claim, new, new_key):
                carried = "clm-" + store.sha256_text(f"carry\n{claim_id}\n{new['id']}")[:24]
                if self._load_row("claims", "id", carried) is not None:
                    continue
                self._insert_claim(
                    {
                        "id": carried,
                        "key": claim["key"],
                        "scope": claim["scope"],
                        "value_type": claim["value_type"],
                        "value_text": claim["value_text"],
                        "value_int": claim["value_int"],
                        "value_real": claim["value_real"],
                        "value_date": claim["value_date"],
                        "value_raw": claim["value_raw"],
                        "status": claim["status"],
                        "basis": claim["basis"],
                        "as_of": claim["as_of"],
                        "source_id": new["id"],
                        "locator": claim["locator"],
                        "quote": claim["quote"],
                        "checked": 1,
                        "supersedes": "[]",
                        "proposed_by": claim["proposed_by"],
                        "created_at": decided_at,
                        "created_by": stamp,
                        "updated_at": decided_at,
                        "updated_by": stamp,
                    }
                )
                targets.append({"table": "claims", "id": carried, "action": "insert"})
                self._set_claim(claim_id, "superseded", decided_at, stamp, targets, superseded_by=carried)
                if claim["key"] not in keys:
                    keys.append(claim["key"])
            else:
                self._open_drift("facts", claim["key"], new["id"], f"claim {claim_id}", detail, stamp, decided_at, targets)
        for key in keys:
            self._derive(key, decided_at, stamp, written, targets)
        # A decision may cite any earlier version of the chain, so a quote lost two versions on still drifts.
        new_decisions = self.con.execute(DECISIONS_BY_CHAIN_SQL, (old_id,)).fetchall()
        for decision_id, quote in new_decisions:
            if not evidence_gate.contains(new_key, evidence_gate.match_key(quote)):
                self._open_drift("decisions", decision_id, new["id"], "decision", detail, stamp, decided_at, targets)

    def _quote_survives(self, claim: dict, new: dict, new_key: str) -> bool:
        """The quote is still in the new source and still proves the claim's own stored value there, read at
        the new source's date. A date that moves with the source ("tomorrow") stops proving the
        date the claim stored, so the claim isn't carried."""
        if not evidence_gate.contains(new_key, evidence_gate.match_key(claim["quote"])):
            return False
        registry = self._load_row("fact_keys", "key", claim["key"])
        values = {column: claim[column] for column in _FACT_VALUE_COLUMNS}
        return strict.value_in_quote(registry, claim["value_raw"], values, claim["quote"], new["source_date"])

    def _open_drift(self, table, subject, source_id, locator, detail, stamp, decided_at, targets) -> None:
        # One open drift signal per fact key or decision: a later edit that still lacks the quote adds none.
        if self.con.execute(
            "SELECT 1 FROM signals WHERE kind = 'drift' AND status = 'open' AND id GLOB 'sig-carry-*' "
            "AND subject_table = ? AND subject_id = ? LIMIT 1",
            (table, subject),
        ).fetchone():
            return
        signal_id = "sig-carry-" + store.sha256_text(f"drift\n{table}\n{subject}\n{source_id}")[:24]
        if self._load_row("signals", "id", signal_id) is not None:
            return
        self.con.execute(
            "INSERT INTO signals (id, kind, subject_table, subject_id, thread_id, detail, status, basis, "
            "resolved_at, source_id, locator, version, created_at, updated_at, updated_by) "
            "VALUES (?, 'drift', ?, ?, NULL, ?, 'open', 'observed', NULL, ?, ?, 1, ?, ?, ?)",
            (signal_id, table, subject, detail, source_id, locator, decided_at, decided_at, stamp),
        )
        targets.append({"table": "signals", "id": signal_id, "action": "insert"})

    def _close_drift(self, key, decided_at, stamp, targets) -> None:
        """A fact key's open engine drift signals close when no live or planned claim on
        it still sits on a source that has a successor."""
        open_ids = [
            found[0]
            for found in self.con.execute(
                "SELECT id FROM signals WHERE kind = 'drift' AND status = 'open' AND subject_table = 'facts' "
                "AND subject_id = ? AND id GLOB 'sig-carry-*' ORDER BY id",
                (key,),
            )
        ]
        if not open_ids:
            return
        stale = self.con.execute(
            "SELECT 1 FROM claims AS c WHERE c.key = ? AND c.status IN ('live', 'planned') "
            "AND EXISTS (SELECT 1 FROM sources AS n WHERE n.supersedes_id = c.source_id) LIMIT 1",
            (key,),
        ).fetchone()
        if stale is None:
            for signal_id in open_ids:
                self._close_signal(signal_id, decided_at, stamp, targets)

    def _close_decision_drift(self, decision_id, stamp, decided_at, targets) -> None:
        """Re-citing a decision on a source with no successor ends its drift."""
        open_ids = [
            found[0]
            for found in self.con.execute(
                "SELECT id FROM signals WHERE kind = 'drift' AND status = 'open' AND subject_table = 'decisions' "
                "AND subject_id = ? AND id GLOB 'sig-carry-*' ORDER BY id",
                (decision_id,),
            )
        ]
        if not open_ids:
            return
        cited = self.con.execute("SELECT source_id FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if cited is None:
            return
        if self.con.execute("SELECT 1 FROM sources WHERE supersedes_id = ?", (cited[0],)).fetchone() is None:
            for signal_id in open_ids:
                self._close_signal(signal_id, decided_at, stamp, targets)

    def _fact_key_claims(self, label, name, row, current, new_type) -> str:
        """What a claim uses can't change under it."""
        key = current["key"]
        used = self.con.execute(
            "SELECT status, value_text FROM claims WHERE key = ?", (key,)
        ).fetchall()
        if not used:
            return ""
        if new_type != current["value_type"]:
            return f"op {label} ({name}): value_type cannot change while a claim uses this key"
        if "unit" in row and row["unit"] != current["unit"]:
            return f"op {label} ({name}): unit cannot change while a claim uses this key"
        if "allowed_values" in row and row["allowed_values"] is not None:
            allowed = set(row["allowed_values"])
            for status, value_text in used:
                if status in ("live", "planned") and value_text is not None and value_text not in allowed:
                    return f"op {label} ({name}): allowed_values would drop a value a claim uses"
        return ""

    def _quote_in(self, source: dict, quote_key: str) -> bool:
        """The quote is in the source (evidence_gate.holds), with the whole-content key cached as before."""
        if evidence_gate.is_speaker_lines(source):
            return evidence_gate.holds(source, quote_key)
        return evidence_gate.contains(self._content_key(source), quote_key)

    def _content_key(self, source: dict) -> str:
        """match_key of a source's content. Sources never change, so it is cached by content hash."""
        if scrub_mod.is_tombstone(source):
            return evidence_gate.match_key(source["content"])
        digest = source["content_sha256"]
        found = self._content_keys.get(digest)
        if found is None:
            if len(self._content_keys) > 64:
                self._content_keys.clear()
            found = self._content_keys[digest] = evidence_gate.match_key(source["content"])
        return found

    def _claim(self, claim_id: str) -> dict | None:
        found = truth.rows(self.con, truth.CLAIM_SQL + "WHERE c.id = ?", (claim_id,))
        if not found:
            return None
        claim = found[0]
        claim["canonical"] = truth.canonical(claim["key"], claim)
        return claim

    def _set_claim(self, claim_id, status, decided_at, stamp, targets, *, superseded_by=None,
                   resolution=None) -> None:
        resolution_source, resolution_locator = resolution if resolution else (None, None)
        self.con.execute(
            "UPDATE claims SET status = ?, superseded_by = ?, resolution_source_id = ?, "
            "resolution_locator = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (status, superseded_by, resolution_source, resolution_locator, decided_at, stamp, claim_id),
        )
        targets.append({"table": "claims", "id": claim_id, "action": "update"})

    def _reaches(self, start: str, target: str, key: str, writer: str) -> bool:
        """Whether following stored supersedes lists (same key, same writer, any status) from start
        reaches target, bounded at 10,000 steps."""
        return (
            self.con.execute(
                "WITH RECURSIVE reach(id) AS ("
                "SELECT ? UNION "
                "SELECT j.value FROM reach AS r JOIN claims AS c ON c.id = r.id AND c.key = ? AND c.proposed_by = ? "
                "JOIN json_each(c.supersedes) AS j LIMIT 10000"
                ") SELECT 1 FROM reach WHERE id = ? LIMIT 1",
                (start, key, writer, target),
            ).fetchone()
            is not None
        )

    def _rejected_by_sender(self, key: str, writer: str, canonical: str) -> str | None:
        """The writer's rejected unscoped claim R on the key with this value, while the settlement that
        rejected it stands. It stops only when the fact is disputed, or current
        with a value other than the one the resolution kept, so the value is recorded and a person sees
        the disagreement. While the fact is retired it stands, so the rejected value can never become
        the undisputed headline. Otherwise None."""
        fact = self._load_row("facts", "key", key)
        if fact is not None and fact["status"] == "disputed":
            return None
        for claim in truth.rows(
            self.con,
            truth.CLAIM_SQL + "WHERE c.key = ? AND c.scope = '' AND c.proposed_by = ? AND c.status = 'rejected' "
            "ORDER BY c.id",
            (key, writer),
        ):
            if truth.canonical(key, claim) != canonical:
                continue
            settled = self._settled_value(claim)
            if settled is None:
                continue
            if fact is None or fact["status"] != "current" or truth.canonical(key, fact) == settled:
                return claim["id"]
        return None

    def _settled_value(self, rejected: dict) -> str | None:
        """The canonical value the resolution that rejected a claim kept, read from that resolving
        packet itself, never from timestamps: the resolve_fact op whose reject lists the claim,
        and its keep[0] claim (step 7 makes every kept claim agree)."""
        change_id = str(rejected["updated_by"]).split(":", 1)[-1]
        for op in self._packet_ops(change_id):
            row = op.get("row") if isinstance(op, dict) else None
            if not isinstance(row, dict) or op.get("op") != "resolve_fact":
                continue
            reject, keep = row.get("reject"), row.get("keep")
            if isinstance(reject, list) and rejected["id"] in reject and isinstance(keep, list) and keep:
                kept = self._claim(str(keep[0]))
                return None if kept is None else kept["canonical"]
        return None

    def _packet_ops(self, change_id: str) -> list:
        """A decided packet's ops from write_log.packet_json, or [] when there is none or it can't be
        read. Inside an apply call each change id is read and parsed once (fix round 4)."""
        cache = self._packets
        if cache is not None and change_id in cache:
            return cache[change_id]
        found = self.con.execute("SELECT packet_json FROM write_log WHERE change_id = ?", (change_id,)).fetchone()
        ops: list = []
        if found is not None:
            try:
                parsed = json.loads(found[0]).get("ops") or []  # trusted: write_log.packet_json, the packet the applier stored
            except (ValueError, AttributeError):
                parsed = []
            ops = parsed if isinstance(parsed, list) else []
        if cache is not None:
            cache[change_id] = ops
        return ops

    def _rejected_match(self, key, scope, source_id, locator, canonical) -> str | None:
        """E39: the lowest rejected claim with this key, scope, source, locator and value."""
        for claim in truth.rows(
            self.con,
            truth.CLAIM_SQL
            + "WHERE c.key = ? AND c.scope = ? AND c.source_id = ? AND c.locator = ? "
            "AND c.status = 'rejected' ORDER BY c.id",
            (key, scope, source_id, locator),
        ):
            if truth.canonical(key, claim) == canonical:
                return claim["id"]
        return None

    def _insert_claim(self, columns: dict) -> None:
        names = list(columns)
        self.con.execute(
            f"INSERT INTO claims ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
            [columns[name] for name in names],
        )

    def _settle(self, claim_id, names, own, label, name, decided_at, stamp, targets) -> None:
        """The update-or-conflict rule for a claim N just inserted."""
        new = self._claim(claim_id)
        key, writer = new["key"], new["proposed_by"]
        # Step 0: a supersede named in advance by the same writer, whatever P's status.
        named_by = self.con.execute(
            "SELECT id FROM claims WHERE key = ? AND proposed_by = ? AND id <> ? "
            "AND EXISTS (SELECT 1 FROM json_each(claims.supersedes) AS j WHERE j.value = ?) "
            "ORDER BY id LIMIT 1",
            (key, writer, claim_id, claim_id),
        ).fetchone()
        if named_by is not None:
            self._set_claim(claim_id, "superseded", decided_at, stamp, targets, superseded_by=named_by[0])
        # Step 1: the supersedes N names (rules 2 and 3), or the shorthand's own claims. A verified
        # copy stands in for its original.
        for named in names or ():
            other = self._stand_in(self._claim(named))
            if other is None or other["status"] in ("superseded", "rejected"):
                continue
            other_id = other["id"]
            if other["proposed_by"] != writer:
                self._note_hold(
                    f"op {label} ({name}): superseding {other_id} by {other['proposed_by']} "
                    "needs a person's approval"
                )
            self._set_claim(other_id, "superseded", decided_at, stamp, targets, superseded_by=claim_id)
        for other in own or ():
            self._set_claim(other["id"], "superseded", decided_at, stamp, targets, superseded_by=claim_id)
        # Step 2: only for a live N, against the other live claims of the bucket N joins.
        if named_by is None:
            self._compare(claim_id, decided_at, stamp, targets)

    def _compare(self, claim_id, decided_at, stamp, targets) -> None:
        """A live claim N against the other live claims of its bucket, in id order.
        Rules 1 and 1b apply whatever the values; rule 4 leaves differing values to the derivation."""
        new = self._claim(claim_id)
        if new is None or new["status"] != "live":
            return
        state = truth.state(self.con, new["key"], self.config.truth)
        bucket = state.bucket_for(new["scope"])
        for other in sorted(bucket.claims, key=lambda claim: claim["id"]):
            if other["id"] == claim_id:
                continue
            if truth.descends(self.con, new["source_id"], other["source_id"]):
                # Rule 1: an update.
                self._set_claim(other["id"], "superseded", decided_at, stamp, targets, superseded_by=claim_id)
            elif truth.descends(self.con, other["source_id"], new["source_id"]):
                # Rule 1b: an older version arriving late.
                self._set_claim(claim_id, "superseded", decided_at, stamp, targets, superseded_by=other["id"])
                return
            # Rule 4: when the values differ, both stay live and the derivation raises the dispute.

    def _derive(self, key, decided_at, stamp, written, targets, verified=None, *, follow=True, settle=None) -> None:
        """Work a fact's headline and its conflict signals out from its claims.

        The only code that writes facts and conflict signals. settle names the bucket a resolve_fact
        settled, whose open conflict signals close first."""
        previous = self._deriving
        self._deriving = True
        try:
            self._derive_now(key, decided_at, stamp, written, targets, verified, follow, settle)
        finally:
            self._deriving = previous

    def _derive_now(self, key, decided_at, stamp, written, targets, verified, follow, settle) -> None:
        if settle is not None:
            self._settle_signals(key, settle, decided_at, stamp, targets)
        registry = self._load_row("fact_keys", "key", key)
        value_type = registry["value_type"]
        entity, _attribute = truth.key_parts(key)
        version_key = truth.is_version_key(key, value_type)
        before = truth.version_info(self.con, entity)[1] if version_key else None
        state = truth.state(self.con, key, self.config.truth)
        fact = self._load_row("facts", "key", key)
        headline = None
        if state.now.claims:
            best = state.now.claims[0]
            headline = {
                "value_type": value_type,
                "value_text": best["value_text"],
                "value_int": best["value_int"],
                "value_real": best["value_real"],
                "value_date": best["value_date"],
                "status": "disputed" if state.now.disputed else "current",
                "basis": best["basis"],
                "as_of": best["as_of"],
                "last_verified_at": verified or (fact["last_verified_at"] if fact else decided_at),
                "source_id": best["source_id"],
                "locator": best["locator"],
                "quote": best["quote"],
            }
        elif fact is not None:
            headline = {column: fact[column] for column in _HEADLINE}
            headline["status"] = "retired"
            if verified:
                headline["last_verified_at"] = verified
        if headline is not None:
            if fact is None:
                columns = {
                    "key": key,
                    **headline,
                    "version": 1,
                    "created_at": decided_at,
                    "updated_at": decided_at,
                    "updated_by": stamp,
                }
                self.con.execute(
                    f"INSERT INTO facts ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    list(columns.values()),
                )
                targets.append({"table": "facts", "id": key, "action": "insert"})
                written.add(("facts", key))
            elif any(fact[column] != headline[column] for column in _HEADLINE):
                columns = {**headline, "version": fact["version"] + 1, "updated_at": decided_at,
                           "updated_by": stamp}
                self.con.execute(
                    f"UPDATE facts SET {', '.join(f'{name} = ?' for name in columns)} WHERE key = ?",
                    [*columns.values(), key],
                )
                targets.append({"table": "facts", "id": key, "action": "update"})
                written.add(("facts", key))
        self._derive_signals(key, state, decided_at, stamp, targets)
        self._close_drift(key, decided_at, stamp, targets)
        if version_key and follow:
            after = truth.version_info(self.con, entity)[1]
            if after != before:
                for (other,) in self.con.execute(
                    "SELECT key FROM fact_keys WHERE key <> ? ORDER BY key", (key,)
                ).fetchall():
                    if truth.key_parts(other)[0] != entity:
                        continue
                    if after is not None:
                        # Claims that join the now bucket are compared under the bucket rules.
                        for (joining,) in self.con.execute(
                            "SELECT id FROM claims WHERE key = ? AND scope = ? AND status = 'live' ORDER BY id",
                            (other, after),
                        ).fetchall():
                            self._compare(joining, decided_at, stamp, targets)
                    self._derive(other, decided_at, stamp, written, targets, follow=False)

    def _settle_signals(self, key, settle, decided_at, stamp, targets) -> None:
        """Every open conflict signal of the settled bucket closes, including a
        legacy dispute when the bucket is now. It runs before the state is read, so a legacy dispute
        no longer holds the bucket disputed."""
        prefix = truth.signal_prefix(key, settle)
        for (signal_id,) in self.con.execute(
            "SELECT id FROM signals WHERE kind = 'conflict' AND status = 'open' AND subject_table = 'facts' "
            "AND subject_id = ? ORDER BY id",
            (key,),
        ).fetchall():
            if signal_id.startswith(prefix) or (settle == "now" and truth.is_legacy(signal_id)):
                self._close_signal(signal_id, decided_at, stamp, targets)

    def _derive_signals(self, key, state, decided_at, stamp, targets) -> None:
        open_ids = [
            row[0]
            for row in self.con.execute(
                "SELECT id FROM signals WHERE kind = 'conflict' AND status = 'open' "
                "AND subject_table = 'facts' AND subject_id = ? ORDER BY id",
                (key,),
            )
        ]
        disputed = [bucket for bucket in state.buckets() if bucket.disputed]
        prefixes = [truth.signal_prefix(key, bucket.label) for bucket in disputed]
        for bucket, prefix in zip(disputed, prefixes):
            covered = any(item.startswith(prefix) for item in open_ids) or (
                bucket.label == "now" and any(truth.is_legacy(item) for item in open_ids)
            )
            if covered:
                continue
            taken = [
                int(match.group(1))
                for (signal_id,) in self.con.execute(
                    "SELECT id FROM signals WHERE substr(id, 1, ?) = ?", (len(prefix), prefix)
                )
                if (match := re.fullmatch(re.escape(prefix) + r"([0-9]+)", signal_id))
            ]
            signal_id = f"{prefix}{max(taken, default=0) + 1}"
            latest = max(bucket.claims, key=lambda claim: (claim["created_at"], claim["id"]))
            self.con.execute(
                "INSERT INTO signals (id, kind, subject_table, subject_id, thread_id, detail, status, basis, "
                "resolved_at, source_id, locator, version, created_at, updated_at, updated_by) "
                "VALUES (?, 'conflict', 'facts', ?, NULL, ?, 'open', 'observed', NULL, ?, ?, 1, ?, ?, ?)",
                (
                    signal_id,
                    key,
                    f"values disagree ({bucket.label})",
                    latest["source_id"],
                    latest["locator"],
                    decided_at,
                    decided_at,
                    stamp,
                ),
            )
            targets.append({"table": "signals", "id": signal_id, "action": "insert"})
        for signal_id in open_ids:
            if any(signal_id.startswith(prefix) for prefix in prefixes):
                continue
            if truth.is_legacy(signal_id) and state.now.claims:
                continue
            self._close_signal(signal_id, decided_at, stamp, targets)

    def _close_signal(self, signal_id, decided_at, stamp, targets) -> None:
        self.con.execute(
            "UPDATE signals SET status = 'resolved', resolved_at = ?, version = version + 1, "
            "updated_at = ?, updated_by = ? WHERE id = ?",
            (decided_at, decided_at, stamp, signal_id),
        )
        targets.append({"table": "signals", "id": signal_id, "action": "update"})

    def _add_claim(self, label, name, op, agent, change_id, decided_at, policy, written, *, engine_id=False):
        """A claim, with the legacy fields and engine ids."""
        try:
            row = op["row"]
            spec = schemas.OPS[name]
            legacy = policy is not None and policy.legacy_import
            missing = [field for field in spec["required_create"] if field not in row and not (field == "quote" and legacy)]
            if missing:
                return f"op {label} ({name}): " + self._missing(spec, missing), "rejected", []
            if not legacy:
                for field in _LEGACY_CLAIM_FIELDS:
                    if field in row:
                        return f"op {label} ({name}): {field} is only for a legacy_import agent", "rejected", []
            err = self._scope(label, name, policy, row, None)
            if err:
                return err, "rejected", []
            key = row["key"]
            registry = self._load_row("fact_keys", "key", key)
            if registry is None:
                return f"op {label} ({name}): unknown fact key {key}", "rejected", []
            source_id = op["source"]["source_id"]
            source = self._load_row("sources", "id", source_id)
            if source is None:
                return f"op {label} ({name}): " + advice.source_missing_error(source_id), "rejected", []
            scope = row.get("scope", "")
            if truth.key_parts(key)[1] == truth.VERSION_ATTRIBUTE and scope != "":
                return (
                    f"op {label} ({name}): {key} is a current_version key and must be unscoped",
                    "rejected",
                    [],
                )
            if scope.lower() == "now":
                return f"op {label} ({name}): scope now is reserved", "rejected", []
            if truth.is_version_key(key, registry["value_type"]) and not _scope_label_ok(row["value_raw"]):
                return f"op {label} ({name}): a current_version value must be a scope label", "rejected", []
            # The legacy status and its links.
            if "status" in row and "planned" in row:
                return f"op {label} ({name}): send status or planned, not both", "rejected", []
            status = row.get("status") or ("planned" if row.get("planned", False) else "live")
            if "superseded_by" in row:
                if status != "superseded":
                    return f"op {label} ({name}): superseded_by needs status superseded", "rejected", []
                if row["superseded_by"] == row["id"]:
                    return f"op {label} ({name}): superseded_by names the claim itself", "rejected", []
            has_resolution = "resolution_source_id" in row or "resolution_locator" in row
            if has_resolution and status != "rejected":
                return (
                    f"op {label} ({name}): resolution_source_id and resolution_locator need status rejected",
                    "rejected",
                    [],
                )
            if status == "rejected":
                if "resolution_source_id" not in row or "resolution_locator" not in row:
                    return (
                        f"op {label} ({name}): a rejected claim needs resolution_source_id and resolution_locator",
                        "rejected",
                        [],
                    )
                if self._load_row("sources", "id", row["resolution_source_id"]) is None:
                    return (
                        f"op {label} ({name}): resolution source {row['resolution_source_id']} does not exist",
                        "rejected",
                        [],
                    )
            try:
                values = strict.parse_raw(registry, row["value_raw"], source["source_date"])
            except units.UnitError as exc:
                return f"op {label} ({name}): {exc.tail}", "rejected", []
            quote = row.get("quote", "")
            checked = 1 if "quote" in row else 0
            supersedes = list(row.get("supersedes", []))
            stamp = f"{agent}:{change_id}"
            claim_id = row["id"]
            content = {
                "key": key,
                "scope": scope,
                "value_type": registry["value_type"],
                **values,
                "value_raw": row["value_raw"],
                "basis": row["basis"],
                "as_of": row.get("as_of") or source["source_date"],
                "source_id": source_id,
                "locator": op["source"]["locator"],
                "quote": quote,
                "checked": checked,
                "supersedes": store.canonical_json(supersedes),
                "proposed_by": agent,
            }
            stored = self._load_row("claims", "id", claim_id)
            if stored is not None:
                if all(stored[column] == content[column] for column in _CLAIM_FIXED):
                    if legacy and not _same_import(row, status, stored):
                        return (
                            f"op {label} ({name}): claim {claim_id} is stored as {stored['status']}; "
                            "a re-import can't change it",
                            "rejected",
                            [],
                        )
                    return "", "applied", []
                return (
                    f"op {label} ({name}): claims never change: {claim_id} already exists with different "
                    "content; use a new id",
                    "rejected",
                    [],
                )
            if not engine_id and _RESERVED_CLAIM_ID.search(claim_id):
                return (
                    f"op {label} ({name}): claim ids clm-m5-* and clm-<24 hex> are reserved for the engine",
                    "rejected",
                    [],
                )
            if checked:
                quote_key = evidence_gate.match_key(quote)
                if not self._quote_in(source, quote_key):
                    return f"op {label} ({name}): quote is not in source {source_id}", "rejected", []
                if not strict.value_in_quote(registry, row["value_raw"], values, quote, source["source_date"]):
                    return f"op {label} ({name}): value {row['value_raw']} is not in the quote", "rejected", []
            if supersedes:
                if status != "live":
                    return f"op {label} ({name}): only a live claim can supersede", "rejected", []
                if claim_id in supersedes:
                    return f"op {label} ({name}): supersedes names the claim itself", "rejected", []
                for other_id in supersedes:
                    other = self._load_row("claims", "id", other_id)
                    if other is not None and other["key"] != key:
                        return (
                            f"op {label} ({name}): supersedes {other_id}, a claim on another key",
                            "rejected",
                            [],
                        )
                for other_id in supersedes:
                    if self._reaches(other_id, claim_id, key, agent):
                        return (
                            f"op {label} ({name}): supersedes would form a cycle through {other_id}",
                            "rejected",
                            [],
                        )
            canonical = truth.canonical(key, content)
            if status in ("live", "planned"):
                rejected = self._rejected_match(key, scope, source_id, content["locator"], canonical)
                if rejected is not None:
                    return _rejected_message(label, name, rejected), "rejected", []
            resolution = {"resolution_source_id": None, "resolution_locator": None}
            if status == "rejected":
                resolution = {
                    "resolution_source_id": row["resolution_source_id"],
                    "resolution_locator": row["resolution_locator"],
                }
            self._insert_claim(
                {
                    "id": claim_id,
                    **content,
                    "status": status,
                    **resolution,
                    "created_at": decided_at,
                    "created_by": stamp,
                    "updated_at": decided_at,
                    "updated_by": stamp,
                }
            )
            targets = [{"table": "claims", "id": claim_id, "action": "insert"}]
            if "superseded_by" in row:
                self._links.append((label, claim_id, row["superseded_by"], key))
            if status in ("live", "planned"):
                # A superseded or rejected claim is never in a bucket, so neither step runs.
                self._settle(claim_id, supersedes, None, label, name, decided_at, stamp, targets)
                # An unchecked legacy claim derives without verified, so an imported value doesn't
                # make a stale fact read as fresh.
                verified = self._verifies(claim_id, decided_at) if checked else None
                self._derive(key, decided_at, stamp, written, targets, verified=verified)
            return "", "applied", targets
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, "claims", op.get("row")), "rejected", []

    def _verify_claim(self, label, name, op, agent, change_id, decided_at, policy, written):
        """A checked copy of an unchecked live or planned claim, superseding it."""
        try:
            row = op["row"]
            claim_id = row["claim_id"]
            original = self._claim(claim_id)
            if original is None:
                return f"op {label} ({name}): claim {claim_id} does not exist", "rejected", []
            key = original["key"]
            err = self._scope(label, name, policy, {"key": key}, None)
            if err:
                return err, "rejected", []
            source_id = op["source"]["source_id"]
            locator = op["source"]["locator"]
            if source_id != original["source_id"]:
                return (
                    f"op {label} ({name}): claim {claim_id} cites {original['source_id']}; quote that source",
                    "rejected",
                    [],
                )
            quote = row["quote"]
            new_id = verified_id(claim_id, source_id, locator, quote)
            if self._load_row("claims", "id", new_id) is not None:
                return "", "applied", []
            if original["status"] not in ("live", "planned"):
                return (
                    f"op {label} ({name}): claim {claim_id} is {original['status']}; only a live or planned "
                    "claim can be verified",
                    "rejected",
                    [],
                )
            if original["checked"]:
                return f"op {label} ({name}): claim {claim_id} is already checked", "rejected", []
            source = self._load_row("sources", "id", source_id)
            if not self._quote_in(source, evidence_gate.match_key(quote)):
                return f"op {label} ({name}): quote is not in source {source_id}", "rejected", []
            registry = self._load_row("fact_keys", "key", key)
            values = {column: original[column] for column in _FACT_VALUE_COLUMNS}
            if not strict.value_in_quote(registry, original["value_raw"], values, quote, source["source_date"]):
                return f"op {label} ({name}): value {original['value_raw']} is not in the quote", "rejected", []
            rejected = self._rejected_match(key, original["scope"], source_id, locator, original["canonical"])
            if rejected is not None:
                return _rejected_message(label, name, rejected), "rejected", []
            stamp = f"{agent}:{change_id}"
            self._insert_claim(
                {
                    "id": new_id,
                    "key": key,
                    "scope": original["scope"],
                    "value_type": original["value_type"],
                    **values,
                    "value_raw": original["value_raw"],
                    "status": original["status"],
                    "basis": original["basis"],
                    "as_of": original["as_of"],
                    "source_id": source_id,
                    "locator": locator,
                    "quote": quote,
                    "checked": 1,
                    "supersedes": "[]",
                    "proposed_by": original["proposed_by"],
                    "created_at": decided_at,
                    "created_by": stamp,
                    "updated_at": decided_at,
                    "updated_by": stamp,
                }
            )
            targets = [{"table": "claims", "id": new_id, "action": "insert"}]
            self._set_claim(claim_id, "superseded", decided_at, stamp, targets, superseded_by=new_id)
            if original["status"] == "live":
                # A planned copy never changes the headline, so neither step runs for it. The
                # derivation takes no verified: a verification checks the quote, not that the value is
                # still current, so staleness keeps counting from the last writer's confirmation.
                self._compare(new_id, decided_at, stamp, targets)
                self._derive(key, decided_at, stamp, written, targets, verified=None)
            return "", "applied", targets
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, "claims", op.get("row")), "rejected", []

    def _stand_in(self, claim: dict | None) -> dict | None:
        """A superseded claim named in supersedes is read as its verified copy."""
        if claim is None or claim["status"] != "superseded" or not claim["superseded_by"]:
            return claim
        copy = self._claim(claim["superseded_by"])
        if copy is not None and copy["id"] == verified_id(
            claim["id"], copy["source_id"], copy["locator"], copy["quote"]
        ):
            return copy
        return claim

    def _verifies(self, claim_id, decided_at) -> str | None:
        """The decision time when claim N is live in the now bucket and has the value this op's
        derivation gives the headline, else None."""
        claim = self._claim(claim_id)
        if claim is None or claim["status"] != "live":
            return None
        state = truth.state(self.con, claim["key"], self.config.truth)
        if not any(item["id"] == claim_id for item in state.now.claims):
            return None
        if state.now.claims[0]["canonical"] != claim["canonical"]:
            return None
        return decided_at

    def _upsert_fact(self, label, name, row, current, op, agent, change_id, decided_at, written):
        """Upsert_fact as shorthand for a claim of the sender's own."""
        try:
            key = row["key"]
            stamp = f"{agent}:{change_id}"
            targets: list[dict] = []
            status = row.get("status")
            if status == "disputed":
                return (
                    f"op {label} ({name}): status is worked out from claims; a packet can't set disputed",
                    "rejected",
                    [],
                )
            registry = self._load_row("fact_keys", "key", key)
            value_type = registry["value_type"]
            own = [
                claim
                for claim in truth.claims_of(self.con, key)
                if claim["status"] == "live" and claim["scope"] == "" and claim["proposed_by"] == agent
            ]
            if status == "retired":
                for claim in own:
                    self._set_claim(claim["id"], "superseded", decided_at, stamp, targets)
                self._derive(key, decided_at, stamp, written, targets)
                return "", "applied", targets
            verified = row.get("last_verified_at")
            if "value" not in row:
                # Re-verify: nothing is created, and the derivation writes only what changes.
                self._derive(key, decided_at, stamp, written, targets, verified=verified)
                return "", "applied", targets
            mapped = row["_mapped_value"]
            canonical = truth.canonical(key, {"value_type": value_type, **mapped})
            matching = [claim for claim in own if claim["canonical"] == canonical]
            if any(claim["checked"] for claim in matching):
                best = truth.rank_order(matching, self.config.truth)[0]
                for claim in own:
                    if claim["id"] != best["id"]:
                        self._set_claim(
                            claim["id"], "superseded", decided_at, stamp, targets, superseded_by=best["id"]
                        )
                self._derive(key, decided_at, stamp, written, targets, verified=verified)
                return "", "applied", targets
            # When the writer already holds this value live only unchecked, the version-key
            # refusal and the suppression are skipped, and the checked claim below replaces it. A
            # migrated version value no scope can match can so be re-sent unchanged.
            upgrade = bool(matching)
            if not upgrade and truth.is_version_key(key, value_type) and not _scope_label_ok(row["value"]):
                return f"op {label} ({name}): a current_version value must be a scope label", "rejected", []
            settled = None if upgrade else self._rejected_by_sender(key, agent, canonical)
            if settled is not None:
                # A value a person rejected from this writer, while that settlement stands: nothing is
                # created and nothing changes, the packet still applies, and the sender is told.
                shown = units.render(value_type, units.typed(value_type, mapped))
                self._notes.append(
                    f"op {label} ({name}): value {shown} was rejected by a person ({settled}), so nothing was recorded"
                )
                return "", "applied", []
            source_id = op["source"]["source_id"]
            locator = op["source"]["locator"]
            if not locator.strip():
                # The claims CHECK trims the locator; refused here, not as a raw SQLite message.
                return f"op {label} ({name}): locator is blank", "rejected", []
            source = self._load_row("sources", "id", source_id)
            rejected = self._rejected_match(key, "", source_id, locator, canonical)
            if rejected is not None:
                return _rejected_message(label, name, rejected), "rejected", []
            claim_id = "clm-" + store.sha256_text(f"{change_id}\n{label}")[:24]
            self._insert_claim(
                {
                    "id": claim_id,
                    "key": key,
                    "scope": "",
                    "value_type": value_type,
                    **mapped,
                    "value_raw": row["value_raw"],
                    "status": "live",
                    "basis": row["basis"] if "basis" in row else current["basis"],
                    "as_of": row["as_of"] if "as_of" in row else source["source_date"],
                    "source_id": source_id,
                    "locator": locator,
                    "quote": row["quote"],
                    "checked": 1,
                    "supersedes": "[]",
                    "proposed_by": agent,
                    "created_at": decided_at,
                    "created_by": stamp,
                    "updated_at": decided_at,
                    "updated_by": stamp,
                }
            )
            targets.append({"table": "claims", "id": claim_id, "action": "insert"})
            self._settle(claim_id, None, own, label, name, decided_at, stamp, targets)
            self._derive(key, decided_at, stamp, written, targets, verified=verified)
            return "", "applied", targets
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, "facts", row), "rejected", []

    def _resolve_fact(self, label, name, op, agent, change_id, decided_at, policy, written):
        """resolve_fact: a person settles which claims stand for a fact."""
        try:
            row = op["row"]
            err = self._scope(label, name, policy, row, None)
            if err:
                return err, "rejected", []
            key = row["key"]
            registry = self._load_row("fact_keys", "key", key)
            if registry is None:
                return f"op {label} ({name}): unknown fact key {key}", "rejected", []
            source_id = op["source"]["source_id"]
            source = self._load_row("sources", "id", source_id)
            if source is None:
                return f"op {label} ({name}): " + advice.source_missing_error(source_id), "rejected", []
            keep, reject = list(row["keep"]), list(row["reject"])
            both = sorted(set(keep) & set(reject))
            if both:
                return f"op {label} ({name}): claim {both[0]} is in both keep and reject", "rejected", []
            named = set(keep) | set(reject)
            state = truth.state(self.con, key, self.config.truth)
            bucket = state.bucket_for(row["scope"])
            claims = {}
            for claim_id in sorted(named):
                claim = self._claim(claim_id)
                if claim is None:
                    return f"op {label} ({name}): claim {claim_id} does not exist", "rejected", []
                if claim["key"] != key:
                    return f"op {label} ({name}): claim {claim_id} is not a claim on {key}", "rejected", []
                # A re-read can't fix these, so they are refused rather than raced.
                if claim["status"] == "planned":
                    return f"op {label} ({name}): claim {claim_id} is planned", "rejected", []
                own = state.label_for(claim["scope"])
                if claim["status"] == "live" and own != bucket.label:
                    return (
                        f"op {label} ({name}): claim {claim_id} is in {own}, not {bucket.label}",
                        "rejected",
                        [],
                    )
                claims[claim_id] = claim
            live = {claim["id"] for claim in bucket.claims}
            if len(live) > 40:
                return (
                    f"op {label} ({name}): {bucket.label} has {len(live)} live claims, more than one "
                    "resolve_fact can name (40)",
                    "rejected",
                    [],
                )
            for claim_id in sorted(named | live):
                if claim_id in named and claim_id not in live:
                    return (
                        f"op {label} ({name}): claim {claim_id} is no longer live in {bucket.label}; "
                        "re-read with claims all and resubmit",
                        "conflict",
                        [],
                    )
                if claim_id in live and claim_id not in named:
                    return (
                        f"op {label} ({name}): claim {claim_id} is live in {bucket.label} but in neither "
                        "keep nor reject; re-read with claims all and resubmit",
                        "conflict",
                        [],
                    )
            if len({claims[claim_id]["canonical"] for claim_id in keep}) > 1:
                return f"op {label} ({name}): kept claims hold different values", "rejected", []
            quote_key = evidence_gate.match_key(row["quote"])
            if not self._quote_in(source, quote_key):
                return f"op {label} ({name}): quote is not in source {source_id}", "rejected", []
            if not any(self._names_kept(registry, claims[claim_id], row["quote"], source) for claim_id in keep):
                return f"op {label} ({name}): the quote holds no kept claim's value", "rejected", []
            author = source["author_person_id"]
            if author is None:
                return f"op {label} ({name}): source {source_id} has no named person as author", "rejected", []
            principal = self.config.principal
            if (
                principal is not None
                and author == principal.person_id
                and len(quote_key.split()) < evidence_gate.ATTRIBUTION_MIN_QUOTE_WORDS
            ):
                return (
                    f"op {label} ({name}): a quote from the principal needs at least "
                    f"{evidence_gate.ATTRIBUTION_MIN_QUOTE_WORDS} words",
                    "rejected",
                    [],
                )
            if policy is None or not policy.author_principal:
                self._note_hold(f"op {label} ({name}): needs a person's approval")
            stamp = f"{agent}:{change_id}"
            targets: list[dict] = []
            resolution = (source_id, op["source"]["locator"])
            for claim_id in reject:
                self._set_claim(claim_id, "rejected", decided_at, stamp, targets, resolution=resolution)
            self._derive(key, decided_at, stamp, written, targets, settle=bucket.label)
            return "", "applied", targets
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, "claims", op.get("row")), "rejected", []

    @staticmethod
    def _names_kept(registry, claim: dict, quote: str, source) -> bool:
        """resolve_fact's step 9 for one kept claim: the type-aware C4, read from the resolving
        source's date. For a bool or enum key only the kept value must be named and not negated,
        because a resolution names the side it rejects."""
        values = {column: claim[column] for column in _FACT_VALUE_COLUMNS}
        if registry["value_type"] in ("bool", "enum"):
            return strict.value_named(registry, values, evidence_gate.match_key(quote))
        return strict.value_in_quote(registry, claim["value_raw"], values, quote, source["source_date"])

    def _set_aliases(self, label, name, op, agent, change_id, decided_at):
        """Replace the aliases table with the packet's list."""
        try:
            items = op["row"]["aliases"]
            source_id = op["source"]["source_id"]
            if self._load_row("sources", "id", source_id) is None:
                return f"op {label} ({name}): " + advice.source_missing_error(source_id), "rejected", []
            desired: dict[tuple[str, str], str] = {}
            for item in items:
                alias = item["alias"]
                if truth.words(alias) != alias:
                    return f'op {label} ({name}): alias "{alias}" is not in normal form', "rejected", []
                pair = (item["kind"], alias)
                if pair in desired:
                    return (
                        f'op {label} ({name}): alias "{alias}" is listed twice for {item["kind"]}',
                        "rejected",
                        [],
                    )
                desired[pair] = item["target"]
            stored = {
                (kind, alias): target
                for kind, alias, target in self.con.execute("SELECT kind, alias, target FROM aliases")
            }
            targets: list[dict] = []
            for pair in sorted(stored):
                if desired.get(pair) != stored[pair]:
                    self.con.execute("DELETE FROM aliases WHERE kind = ? AND alias = ?", pair)
                    targets.append({"table": "aliases", "id": f"{pair[0]}:{pair[1]}", "action": "delete"})
            for item in items:
                pair = (item["kind"], item["alias"])
                if stored.get(pair) != item["target"]:
                    self.con.execute(
                        "INSERT INTO aliases (kind, alias, target) VALUES (?, ?, ?)",
                        (item["kind"], item["alias"], item["target"]),
                    )
                    targets.append({"table": "aliases", "id": f"{pair[0]}:{pair[1]}", "action": "insert"})
            return "", "applied", targets
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, "aliases", op.get("row")), "rejected", []

    def _set_source_author(self, label, name, op, agent, change_id, decided_at, policy):
        """1.8.0 (migration 0009): give a source a new author, or none. Only an author_principal agent may,
        the op cites the source that says why, and source_author_changes keeps the author it replaced. The
        sources trigger lets the update through only while that row is pending."""
        if policy is None or not policy.author_principal:
            return f"op {label} ({name}): " + advice.author_principal_error(), "rejected", []
        try:
            source_id, author = op["row"]["source_id"], op["row"]["author_person_id"]
            cited_id, locator = op["source"]["source_id"], op["source"]["locator"]
            target = self._load_row("sources", "id", source_id)
            if target is None:
                return f"op {label} ({name}): " + advice.not_found_error("source", source_id), "rejected", []
            if target["kind"] == "config":
                return f"op {label} ({name}): " + advice.author_target_error(source_id, "is a config source"), "rejected", []
            if scrub_mod.is_tombstone(target):
                return f"op {label} ({name}): " + advice.author_target_error(source_id, "is redacted"), "rejected", []
            if cited_id == source_id:
                return f"op {label} ({name}): " + advice.author_self_cite_error(source_id), "rejected", []
            if self._load_row("sources", "id", cited_id) is None:
                return f"op {label} ({name}): " + advice.source_missing_error(cited_id), "rejected", []
            # Giving the principal words is the gate's whole question, so this op never does it: the principal's
            # own words come in as a new source from an author_principal agent.
            principal_id = self.config.principal.person_id if self.config.principal else None
            if author is not None and author == principal_id:
                return f"op {label} ({name}): " + advice.author_principal_target_error(author), "rejected", []
            if author is not None and self._load_row("people", "id", author) is None:
                return f"op {label} ({name}): " + advice.author_person_missing_error(author), "rejected", []
            if self.con.execute(
                "SELECT 1 FROM source_author_changes WHERE change_id = ? AND source_id = ?", (change_id, source_id)
            ).fetchone():
                return f"op {label} ({name}): " + advice.author_twice_error(source_id), "rejected", []
            if target["author_person_id"] == author:
                return f"op {label} ({name}): " + advice.author_unchanged_error(source_id, author), "rejected", []
            self.con.execute(
                "INSERT INTO source_author_changes (change_id, source_id, old_author_person_id, new_author_person_id, "
                "evidence_source_id, locator, changed_at, changed_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (change_id, source_id, target["author_person_id"], author, cited_id, locator, decided_at,
                 f"{agent}:{change_id}"),
            )
            self.con.execute("UPDATE sources SET author_person_id = ? WHERE id = ?", (author, source_id))
            self.con.execute(
                "UPDATE source_author_changes SET applied = 1 WHERE change_id = ? AND source_id = ?",
                (change_id, source_id),
            )
            return "", "applied", [{"table": "sources", "id": source_id, "action": "update"}]
        except sqlite3.IntegrityError as exc:
            return self._refusal(label, name, exc, "sources", op.get("row")), "rejected", []

    def _stored_people(self, table: str, key: str, role: str) -> list[str]:
        list_table, list_key = schemas.PEOPLE_LISTS[table]
        return [
            item[0]
            for item in self.con.execute(
                f"SELECT person_id FROM {list_table} WHERE {list_key} = ? AND role = ? ORDER BY position",
                (key, role),
            )
        ]

    def _resolve_people(self, label, name, spec, row, current) -> str:
        table = spec["table"]
        plans: dict[str, list[str]] = {}
        for role, scalar, field in schemas.PEOPLE_ROLES:
            stored = self._stored_people(table, current["id"], role) if current is not None else []
            if field in row:
                new = list(row[field])
                first = new[0] if new else None
                if scalar in row and row[scalar] != first:
                    return f"op {label} ({name}): {scalar} must be the first of {field}"
                for person_id in new:
                    if self._load_row("people", "id", person_id) is None:
                        return f"op {label} ({name}): person {person_id} does not exist"
                row[scalar] = first
            elif scalar in row:
                value = row[scalar]
                if current is not None and value == current[scalar]:
                    new = stored
                else:
                    new = [] if value is None else [value]
            else:
                new = stored
            if new != stored:
                plans[role] = new
        row["_people"] = plans
        return ""

    def _write(self, spec, row, current, op, agent, change_id, decided_at) -> str | None:
        table = spec["table"]
        if table == "facts":
            raise RuntimeError("facts are written only by the derivation (Applier._derive)")
        key_col = spec["key"]
        key = row[key_col]
        stamp = f"{agent}:{change_id}"
        if spec["kind"] == "insert" and table == "sources" and current is not None:
            return None
        columns = {}
        for field, value in row.items():
            if field == "value" or field == "_mapped_value" or field == "_people":
                continue
            if field in _PEOPLE_FIELDS:
                continue
            if field not in spec["row"]["properties"]:
                continue
            columns[field] = self._store_value(spec, field, value)
        if spec["kind"] == "upsert":
            columns["source_id"] = op["source"]["source_id"]
            columns["locator"] = op["source"]["locator"]
        # 0008: a legacy import's original times (checked by _original_times), else the apply time.
        columns["updated_at"] = row.get("updated_at", decided_at) if "updated_at" in spec["row"]["properties"] else decided_at
        columns["updated_by"] = stamp
        if current is None:
            if table not in {"sources", "collector_runs"}:
                columns["version"] = 1
                columns["created_at"] = row.get("created_at", decided_at) if "created_at" in spec["row"]["properties"] else decided_at
            if table == "sources":
                columns["content_sha256"] = store.sha256_text(row["content"])
                columns["captured_at"] = decided_at
                columns["captured_by"] = stamp
                columns.pop("updated_at", None)
                columns.pop("updated_by", None)
            if table == "collector_runs":
                columns["recorded_at"] = decided_at
                columns["recorded_by"] = stamp
                columns.pop("updated_at", None)
                columns.pop("updated_by", None)
            names = list(columns)
            placeholders = ", ".join("?" for _ in names)
            self.con.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({placeholders})",
                [columns[name] for name in names],
            )
            self._write_people(table, key, row)
            return "insert"
        columns["version"] = current["version"] + 1
        assignments = ", ".join(f"{name} = ?" for name in columns)
        values = [columns[name] for name in columns]
        values.append(current[key_col])
        self.con.execute(
            f"UPDATE {table} SET {assignments} WHERE {key_col} = ?",
            values,
        )
        self._write_people(table, key, row)
        return "update"

    def _write_people(self, table: str, key, row: dict) -> None:
        plans = row.get("_people")
        if not plans:
            return
        list_table, list_key = schemas.PEOPLE_LISTS[table]
        for role, person_ids in plans.items():
            self.con.execute(
                f"DELETE FROM {list_table} WHERE {list_key} = ? AND role = ?", (key, role)
            )
            for position, person_id in enumerate(person_ids):
                self.con.execute(
                    f"INSERT INTO {list_table} ({list_key}, role, position, person_id) VALUES (?, ?, ?, ?)",
                    (key, role, position, person_id),
                )

    def _store_value(self, spec, field, value):
        if field in spec["json_fields"]:
            return None if value is None else store.canonical_json(value)
        if field in spec["bool_fields"]:
            return None if value is None else (1 if value else 0)
        return value

    def _source_fields(self, row, decided_at):
        return {
            "kind": row["kind"],
            "title": row["title"],
            "uri": row.get("uri", ""),
            "source_date": row["source_date"],
            "author_person_id": row.get("author_person_id"),
            "content": row["content"],
            "external_ref": row.get("external_ref", ""),
            "content_sha256": store.sha256_text(row["content"]),
            "supersedes_id": row.get("supersedes_id"),
        }

    def _load_row(self, table: str, key_col: str, key):
        row = self.con.execute(
            f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)
        ).fetchone()
        return dict(row) if row else None

    def _write_log(self, change_id: str):
        row = self.con.execute(
            "SELECT * FROM write_log WHERE change_id = ?", (change_id,)
        ).fetchone()
        return row

    def _finish_decision(
        self, packet, status, error, targets, decided_at, review, existing, hold_reason="", warning="", via=""
    ) -> Outcome:
        applied_seq = None
        if status == "applied":
            applied_seq = self.con.execute(
                "SELECT coalesce(max(applied_seq), 0) + 1 FROM write_log"
            ).fetchone()[0]
        if existing is None:
            self._insert_log(
                packet, status, error, targets, decided_at, applied_seq, review, hold_reason
            )
        else:
            self._update_log(
                existing,
                status=status,
                decided_at=decided_at,
                error=error,
                targets=targets,
                applied_seq=applied_seq,
                reviewed_by=review or "",
                reviewed_at=decided_at if review else None,
                approved_via=via if review else "",
            )
        if self._dry_run:
            return self._rehearsed(packet["change_id"], warning)
        self._end_txn("COMMIT")
        outcome = self._finish(packet["change_id"])
        return dataclasses.replace(outcome, warning=warning) if warning else outcome

    def _rehearsed(self, change_id: str, warning: str) -> Outcome:
        """The end of a dry run: the receipt the log row would give, read inside the transaction, then a rollback
        instead of the commit. _finish is skipped, so no integrity check runs and no PAUSED file can be written."""
        receipt = receipt_from_row(self._write_log(change_id))
        self._end_txn("ROLLBACK")
        return Outcome(receipt, _EXIT[receipt["status"]], warning=warning, dry_run=True)

    def _insert_log(
        self, packet, status, error, targets, decided_at, applied_seq, review, hold_reason=""
    ):
        payload = store.canonical_json(packet)
        stored = payload
        tombs = self._log_tombstones(packet)
        if tombs:
            types = {row[0]: row[1] for row in self.con.execute("SELECT key, value_type FROM fact_keys")}
            day = local_date(decided_at, self.config.timezone)
            scrubbed, touched = scrub_mod.scrub_packet(packet, {}, types, whole=True, date=day, cited=tombs)
            if touched:
                stored = store.canonical_json(scrubbed)
        self.con.execute(
            """INSERT INTO write_log (
                   change_id, agent, intent, status, decided_at, applied_seq,
                   reviewed_by, reviewed_at, payload_sha256, error, packet_json, targets_json,
                   hold_reason
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                packet["change_id"],
                packet["agent"],
                packet["intent"],
                status,
                decided_at,
                applied_seq,
                review or "",
                decided_at if review else None,
                store.sha256_text(payload),
                error,
                stored,
                store.canonical_json(targets),
                hold_reason[:2000],
            ),
        )

    def _update_log(
        self, existing, *, status, decided_at, error, targets, applied_seq, reviewed_by, reviewed_at, approved_via=""
    ):
        self.con.execute(
            """UPDATE write_log SET status = ?, decided_at = ?, applied_seq = ?,
                   reviewed_by = ?, reviewed_at = ?, error = ?, targets_json = ?, approved_via = ?
               WHERE change_id = ?""",
            (
                status,
                decided_at,
                applied_seq,
                reviewed_by,
                reviewed_at,
                error,
                store.canonical_json(targets),
                approved_via,
                existing["change_id"],
            ),
        )

    def _folder_problem(self, project_id: str, kind: str | None, parent_id: str) -> str:
        """Folders (0007): a parent must exist, a workstream sits at the top, and no folder is inside itself."""
        if kind == "workstream":
            return "a workstream sits at the top and has no parent"
        if parent_id == project_id:
            return f"project {project_id} can't be its own parent"
        seen = parent_id
        for depth in range(FOLDER_DEPTH_MAX):
            row = self.con.execute("SELECT parent_id FROM projects WHERE id = ?", (seen,)).fetchone()
            if row is None:
                return f"parent {parent_id} is not a project" if seen == parent_id else f"project {seen} is missing"
            if row["parent_id"] is None:
                return ""
            if row["parent_id"] == project_id:
                return f"parent {parent_id} is inside {project_id}, so this would make a loop"
            seen = row["parent_id"]
        return f"folders nest at most {FOLDER_DEPTH_MAX} deep"

    def _full_check_due(self, change_id: str, receipt: dict) -> bool:
        if any(item.get("action") == "rebuild" for item in receipt["targets"]):
            return True
        if self.batch:
            return False
        pages = self.con.execute("PRAGMA page_count").fetchone()[0]
        size = pages * self.con.execute("PRAGMA page_size").fetchone()[0]
        if size <= FULL_CHECK_MAX_BYTES:
            return True
        seq = self.con.execute(
            "SELECT rowid FROM write_log WHERE change_id = ?", (change_id,)
        ).fetchone()[0]
        return seq % FULL_CHECK_EVERY == 0

    def _finish(self, change_id: str) -> Outcome:
        row = self._write_log(change_id)
        receipt = receipt_from_row(row)
        started = time.perf_counter()
        problems: list[str] = []
        full = self._full_check_due(change_id, receipt)
        if full:
            problems.extend(store.integrity_problems(self.con))
        elif self.batch:
            self._unchecked += 1
        missing = store.missing_guards(self.con)
        if missing:
            problems.extend("missing guard " + name for name in missing)
        search_missing = store.missing_search_objects(self.con)
        if search_missing:
            problems.append("missing search objects: " + ", ".join(search_missing))
        elif self.con.execute("PRAGMA user_version").fetchone()[0] >= store.SEARCH_SINCE:
            if any(
                item.get("action") == "rebuild" or item.get("table") == "aliases"
                for item in receipt["targets"]
            ):
                problems.extend(store.search_drift(self.con))
            else:
                problems.extend(store.search_targets_drift(self.con, receipt["targets"]))
        check = "full" if full else "targeted"
        check_ms = int((time.perf_counter() - started) * 1000)
        if not problems:
            return Outcome(
                receipt,
                _EXIT[receipt["status"]],
                integrity="ok",
                check=check,
                check_ms=check_ms,
            )
        payload = {
            "paused_at": store.utc_now(),
            "change_id": change_id,
            "problems": problems,
        }
        (self.instance_dir / "PAUSED").write_text(store.canonical_json(payload) + "\n")
        return Outcome(
            receipt, 5, integrity="; ".join(problems), check=check, check_ms=check_ms
        )

    def _parse(self, raw: bytes) -> dict:
        try:
            packet = ingress.loads(raw, what="packet", max_nesting=ingress.PACKET_NESTING, max_bytes=_MAX_PACKET)
        except ingress.IngressError as exc:
            raise Unreadable(str(exc)) from exc
        if not isinstance(packet, dict):
            raise Unreadable("packet must be a JSON object")
        change_id = packet.get("change_id")
        if not isinstance(change_id, str) or _CHANGE_ID.fullmatch(change_id) is None:
            raise Unreadable("change_id is missing or invalid")
        problem = validate.envelope_error(packet)
        if problem is not None:
            raise Unreadable(problem[1])
        return packet

    @staticmethod
    def _packet_error(errors: list[dict]) -> str:
        parts = [f"{item['path']} {item['message']}" for item in errors[:10]]
        text = "packet is invalid: " + "; ".join(parts)
        extra = len(errors) - 10
        if extra > 0:
            text += f"; and {extra} more"
        return text


def _link_source_ids(op) -> list[str]:
    """The source ids of an op's `sources` list (0008), in order, skipping anything that is not one."""
    items = op.get("sources") if isinstance(op, dict) else None
    if not isinstance(items, list):
        return []
    return [item["source_id"] for item in items if isinstance(item, dict) and isinstance(item.get("source_id"), str)]


def _cited_source(row: dict) -> evidence_gate.CitedSource:
    """A stored source as the gate reads it. 1.9.0: a speaker-lines excerpt is read one paragraph at a time,
    whether it is cited itself or found through the source it was lifted from."""
    return evidence_gate.CitedSource(
        row["id"], row["content"], row["author_person_id"], by_paragraph=evidence_gate.is_speaker_lines(row)
    )


def link_target_id(table: str, key, label: str, source_id: str) -> str:
    """A link's receipt target id: subject table, subject id, label and source, the link's primary key."""
    return f"{table}:{key}:{label}:{source_id}"


def _same_import(row: dict, status: str, stored) -> bool:
    """A legacy re-import of a stored claim is a no-op only when it asks
    for the stored status and links, or for live or planned when the engine has since superseded it or
    a person has since rejected it: either settlement stands."""
    if status in ("live", "planned") and stored["status"] in ("superseded", "rejected"):
        return True
    if status != stored["status"]:
        return False
    return all(
        stored[field] == row[field]
        for field in ("superseded_by", "resolution_source_id", "resolution_locator")
        if field in row
    )


def verified_id(claim_id: str, source_id: str, locator: str, quote: str) -> str:
    """The id of claim_id's verified copy."""
    return "clm-" + store.sha256_text(f"verify\n{claim_id}\n{source_id}\n{locator}\n{quote}")[:24]


def _rejected_message(label: str, name: str, claim_id: str) -> str:
    """E39's refusal, shared by add_claim and upsert_fact."""
    return (
        f"op {label} ({name}): a claim with this key, scope, source, locator and value was rejected ({claim_id})"
    )


_scope_label_ok = truth.scope_label_ok  # one pattern, from schemas.SCOPE


def _marker_elements(flags) -> list[dict]:
    seen: dict[str, dict] = {}
    for flag in flags:
        key = _sentence_key(flag.sentence)
        if key in seen:
            continue
        seen[key] = {
            "key": key,
            "reasons": list(flag.reasons),
            "text": single_line(flag.sentence, max_chars=_MARKER_TEXT_MAX),
        }
    return list(seen.values())


def _evaluated(policy, packet: dict) -> bool:
    """An evaluated candidate: the one apply_candidate op of a packet from an extractor
    under approval = "verified" or "required". The caller checks that this is a first decision."""
    if policy is None or policy.kind != "extractor" or policy.approval not in ("required", "verified"):
        return False
    ops = packet.get("ops")
    return (
        isinstance(ops, list)
        and len(ops) == 1
        and isinstance(ops[0], dict)
        and ops[0].get("op") == "apply_candidate"
    )


def _candidate_invalid(index: int, errors) -> str:
    parts = [f"{item['path']} {item['message']}" for item in list(errors)[:10]]
    text = f"op {index} (apply_candidate): candidate is invalid: " + "; ".join(parts)
    extra = len(errors) - 10
    if extra > 0:
        text += f"; and {extra} more"
    return text


def _same(new, old, field, spec) -> bool:
    if field in spec["bool_fields"]:
        new_v = None if new is None else (1 if new else 0)
        return new_v == old
    if field in spec["json_fields"]:
        old_v = json.loads(old) if isinstance(old, str) else old  # trusted: a json column of a stored row, written by the applier
        return new == old_v
    return new == old


def _tombstone_same(existing: dict, proposed: dict) -> bool:
    """E3: a re-sent source whose stored row 2b has since turned into a tombstone is the same
    source. Deliberately narrow: the tombstone's title and content shape, the same content hash, and the
    same kind, date, reference and predecessor. Everything else is still `sources never change`."""
    return (
        existing.get("title") == TOMBSTONE_TITLE
        and str(existing.get("content", "")).startswith(TOMBSTONE_PREFIX)
        and existing.get("content_sha256") == proposed["content_sha256"]
        and all(
            existing.get(field) == proposed[field]
            for field in ("kind", "source_date", "external_ref", "supersedes_id")
        )
    )


def _source_same(existing: dict, proposed: dict) -> bool:
    for field, value in proposed.items():
        if existing.get(field) != value:
            if field in {"uri", "external_ref"} and existing.get(field) == "" and value == "":
                continue
            return False
    return True
