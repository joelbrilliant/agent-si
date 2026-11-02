from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
import re

try:
    import fcntl
except ImportError:
    fcntl = None
from dataclasses import dataclass, field
from pathlib import Path

from brain import ingress, marks, store
from brain import validate
from brain.applier import Applier, Unavailable, Unreadable, receipt_from_row
from brain.collectors import REGISTRY
from brain.collectors.base import DocumentError, RunContext, local_date

BATCH_OPS = 400
BATCH_BYTES = 4 * 1024 * 1024
_MAX_FIXTURE = ingress.MAX_DOCUMENT_BYTES
_ATTEMPTS = 3
_ATTEMPT_SQL = "SELECT id FROM sources WHERE id >= ? AND id < ? || '~'"


@dataclass
class CollectResult:
    exit_code: int
    payload: dict | None = None
    warnings: tuple[str, ...] = ()
    check_ms: tuple[int, ...] = ()


@dataclass
class _Trace:
    warnings: list[str] = field(default_factory=list)
    check_ms: list[int] = field(default_factory=list)

    def note(self, outcome) -> None:
        if outcome.warning:
            self.warnings.append(outcome.warning)
        self.check_ms.append(outcome.check_ms)

    def result(self, exit_code: int, payload: dict | None) -> CollectResult:
        return CollectResult(exit_code, payload, tuple(self.warnings), tuple(self.check_ms))


def _cut(error: str) -> str:
    if len(error) <= 2000:
        return error
    return error[:1997] + "..."


def _schema_error(errors: list[dict]) -> str:
    parts = []
    for item in errors[:5]:
        path = item["path"]
        parts.append(f"{path} {item['message']}" if path else item["message"])
    return _cut("document is invalid: " + "; ".join(parts))


def _run_key(name: str, type_name: str, agent: str, options: dict, input_sha256: str) -> str:
    payload = {
        "collector": name,
        "type": type_name,
        "agent": agent,
        "options": options,
        "input_sha256": input_sha256,
    }
    return hashlib.sha256(store.canonical_json(payload).encode()).hexdigest()[:24]


def _change_id(agent: str, material: list) -> str:
    digest = hashlib.sha256(store.canonical_json(material).encode()).hexdigest()[:24]
    return f"{agent}-{digest}"


def _read_fixture(path: Path) -> tuple[bytes, object]:
    if not path.is_file():
        raise Unreadable("fixture is unreadable: missing")
    try:
        raw = ingress.read_bytes(path, _MAX_FIXTURE, "fixture")
        return raw, ingress.loads(raw, what="fixture")
    except ingress.IngressError as exc:
        raise Unreadable(str(exc)) from exc
    except OSError as exc:
        raise Unreadable(f"fixture is unreadable: {exc}") from exc


def _snapshot(db: Path, ctype, doc: dict, ctx) -> dict:
    con = store.connect_reader(db)
    try:
        con.execute("BEGIN")
        try:
            known = ctype.known(con, doc, ctx)
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        if con.in_transaction:
            con.execute("COMMIT")
        return known
    finally:
        con.close()


def _batch_source(index, evidence, ctx, doc, input_sha256, ctype, attempt) -> dict:
    if attempt == 1:
        source_id = f"src-{ctx.collector}-{ctx.run_key}-{index}"
    else:
        source_id = f"src-{ctx.collector}-{ctx.run_key}-a{attempt}-{index}"
    return {
        "op": "add_source",
        "row": {
            "id": source_id,
            "kind": ctype.source_kind,
            "title": f"{ctx.collector} snapshot {doc['fetched_at']}, batch {index}",
            "uri": "",
            "source_date": local_date(doc["fetched_at"], ctx.timezone),
            "author_person_id": None,
            "content": store.canonical_json(
                {
                    "collector": ctx.collector,
                    "format": ctype.format,
                    "fetched_at": doc["fetched_at"],
                    "complete": doc["complete"],
                    "input_sha256": input_sha256,
                    "items": evidence,
                }
            ),
            "external_ref": f"collector:{ctx.collector}:{input_sha256}",
        },
    }


def _json_bytes(value) -> int:
    return len(store.canonical_json(value).encode())


def _escaped_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode()) - 2


def _group_cost(group) -> tuple[int, int, int, int, int | None]:
    own = 0
    blank = 0
    refs = 0
    for op in group.ops:
        size = _json_bytes(op)
        own += size
        if "source" in op:
            refs += 1
            copied = copy.deepcopy(op)
            source = dict(copied["source"])
            source["source_id"] = ""
            copied["source"] = source
            blank += _json_bytes(copied)
        else:
            blank += size
    evidence_bytes = None
    if group.evidence is not None:
        evidence_bytes = _escaped_bytes(store.canonical_json(group.evidence))
    return (len(group.ops), own, blank, refs, evidence_bytes)


def _split(groups, ctx, doc, input_sha256, ctype, attempt) -> list[list]:
    costs = [_group_cost(group) for group in groups]
    batches: list[list] = []
    current: list = []
    current_costs: list = []
    measured: dict[int, tuple[int, int]] = {}

    def source_parts(index: int) -> tuple[int, int]:
        found = measured.get(index)
        if found is None:
            source = _batch_source(index, [], ctx, doc, input_sha256, ctype, attempt)
            found = (_json_bytes(source), _escaped_bytes(source["row"]["id"]))
            measured[index] = found
        return found

    def measured_size(chosen: list) -> tuple[int, int]:
        count = own = blank = refs = with_evidence = evidence_bytes = 0
        for ops, own_n, blank_n, ref_n, ev in chosen:
            count += ops
            own += own_n
            blank += blank_n
            refs += ref_n
            if ev is not None:
                with_evidence += 1
                evidence_bytes += ev
        if with_evidence == 0:
            total = count
            return total, 2 + own + max(total - 1, 0)
        total = count + 1
        source_bytes, id_bytes = source_parts(len(batches) + 1)
        return (
            total,
            2
            + source_bytes
            + evidence_bytes
            + (with_evidence - 1)
            + blank
            + refs * id_bytes
            + (total - 1),
        )

    for group, cost in zip(groups, costs, strict=True):
        if current:
            ops, nbytes = measured_size([*current_costs, cost])
            if ops > BATCH_OPS or nbytes > BATCH_BYTES:
                batches.append(current)
                current = [group]
                current_costs = [cost]
                continue
        current.append(group)
        current_costs.append(cost)
    batches.append(current)
    return batches


def _materialise(
    groups: list, index: int, ctx, doc: dict, input_sha256: str, ctype, attempt: int
) -> list[dict]:
    evidence = [group.evidence for group in groups if group.evidence is not None]
    source = None
    if evidence:
        source = _batch_source(index, evidence, ctx, doc, input_sha256, ctype, attempt)
    ops: list[dict] = []
    if source is not None:
        ops.append(source)
    for group in groups:
        for op in group.ops:
            copied = copy.deepcopy(op)
            if source is not None and "source" in copied:
                copied["source"] = dict(copied["source"])
                copied["source"]["source_id"] = source["row"]["id"]
            ops.append(copied)
    return ops


def _run_row(applier: Applier, run_id: str):
    return applier.con.execute(
        "SELECT status, items, error, recorded_by FROM collector_runs WHERE id = ?",
        (run_id,),
    ).fetchone()


def _receipt_for(applier: Applier, recorded_by: str) -> dict:
    _agent, change_id = recorded_by.split(":", 1)
    row = applier.con.execute(
        "SELECT * FROM write_log WHERE change_id = ?", (change_id,)
    ).fetchone()
    return receipt_from_row(row)


def _payload(name, run_id, status, items, packets, duplicate, receipt) -> dict:
    return {
        "collector": name,
        "run_id": run_id,
        "status": status,
        "items": items,
        "packets": packets,
        "duplicate": duplicate,
        "receipt": receipt,
    }


def _run_op(run_id, name, started, status, items, input_sha256, error) -> dict:
    return {
        "op": "record_collector_run",
        "row": {
            "id": run_id,
            "collector": name,
            "started_at": started,
            "finished_at": store.utc_now(),
            "status": status,
            "items": items,
            "input_sha256": input_sha256,
            "error": _cut(error),
        },
    }


def _record_error(applier, trace, ctx, run_id, name, agent, input_sha256, items, error, packets):
    change_id = _change_id(agent, [ctx.run_key, "error"])
    packet = {
        "packet_version": 1,
        "change_id": change_id,
        "agent": agent,
        "intent": f"collect {name}: error",
        "ops": [_run_op(run_id, name, ctx.now, "error", items, input_sha256, error)],
    }
    outcome = applier.apply(store.canonical_json(packet).encode())
    trace.note(outcome)
    row = _run_row(applier, run_id)
    status = None if row is None else row["status"]
    recorded_items = items if row is None else row["items"]
    exit_code = 1 if outcome.exit_code == 0 else outcome.exit_code
    return trace.result(
        exit_code,
        _payload(name, run_id, status, recorded_items, packets, False, outcome.receipt),
    )


def _used_attempts(applier: Applier, prefix: str) -> int:
    used = 0
    for (source_id,) in applier.con.execute(_ATTEMPT_SQL, (prefix, prefix)):
        match = re.fullmatch(r"(?:a([0-9]+)-)?[0-9]+", source_id[len(prefix):])
        if match:
            used = max(used, int(match.group(1) or 1))
    return used


class CollectorBusy(Unavailable):
    def __init__(self, name: str):
        super().__init__(f"collector {name} is already running; try again when it finishes")
        self.collector = name


@contextlib.contextmanager
def collector_lock(instance_dir: Path, name: str):
    if fcntl is None:
        raise Unavailable("brain collect needs fcntl file locks, which this platform doesn't have")
    handle = open(Path(instance_dir) / f".collect-{name}.lock", "a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CollectorBusy(name) from exc
        yield
    finally:
        handle.close()


def run(
    instance_dir: Path, name: str, fixture: Path | None, *, allow_mass_change: bool = False
) -> CollectResult:
    instance_dir = Path(instance_dir)
    old = store.sqlite_too_old_message()
    if old:
        raise Unavailable(old)
    missing_fts = store.fts5_missing_message()
    if missing_fts:
        raise Unavailable(missing_fts)
    applier = Applier(instance_dir, collect=True)
    trace = _Trace()
    try:
        applier.preconditions()
        started = store.utc_now()
        if fixture is None:
            raise Unreadable("brain collect needs --input FILE; the engine reads exported files only")
        collector = applier.config.collectors.get(name)
        if collector is None:
            raise Unreadable(f"no collector {name}")
        with collector_lock(instance_dir, name):
            raw, doc = _read_fixture(Path(fixture))
            ctype = REGISTRY[collector.type]
            input_sha256 = hashlib.sha256(raw).hexdigest()
            options = dict(collector.options)
            key_options = {**options, "allow_mass_change": True} if allow_mass_change else options
            run_key = _run_key(name, collector.type, collector.agent, key_options, input_sha256)
            run_id = f"run-{name}-{run_key}"
            principal = applier.config.principal
            ctx = RunContext(
                name,
                options,
                applier.config.timezone,
                run_key,
                started,
                collector.agent,
                instance_dir=str(instance_dir),
                allow_mass_change=allow_mass_change,
                principal_id="" if principal is None else principal.person_id,
                author_principal=applier.config.agents[collector.agent].author_principal,
            )
            existing = _run_row(applier, run_id)
            if existing is not None:
                status = existing["status"]
                return trace.result(
                    0 if status in {"ok", "partial"} else 1,
                    _payload(
                        name,
                        run_id,
                        status,
                        existing["items"],
                        0,
                        True,
                        _receipt_for(applier, existing["recorded_by"]),
                    ),
                )
            if not isinstance(doc, dict):
                return _record_error(
                    applier, trace, ctx, run_id, name, collector.agent, input_sha256,
                    0, _schema_error(validate.check(doc, ctype.document_schema)), 1,
                )
            errors = validate.check(doc, ctype.document_schema)
            if errors or doc.get("format") != ctype.format:
                text = _schema_error(errors or validate.check(doc, ctype.document_schema))
                return _record_error(
                    applier, trace, ctx, run_id, name, collector.agent, input_sha256, 0, text, 1,
                )
            if collector.type == "capture":
                ctx = dataclasses.replace(ctx, marked=marks.verify(instance_dir, doc))
            agent = collector.agent
            last_conflict = None
            submitted = 0
            plan = None
            used = _used_attempts(applier, f"src-{name}-{run_key}-")
            for attempt in range(used + 1, used + _ATTEMPTS + 1):
                try:
                    known = _snapshot(applier.db, ctype, doc, ctx)
                    plan = ctype.plan(doc, known, ctx)
                except DocumentError as exc:
                    return _record_error(
                        applier, trace, ctx, run_id, name, agent, input_sha256, 0, str(exc), 1,
                    )
                batches = _split(plan.groups, ctx, doc, input_sha256, ctype, attempt)
                total = len(batches)
                submitted = 0
                conflict = False
                last = None
                for index, groups in enumerate(batches, start=1):
                    ops = _materialise(groups, index, ctx, doc, input_sha256, ctype, attempt)
                    change_id = _change_id(agent, [run_key, index == total, ops])
                    if index == total:
                        status, error = ("partial", plan.note) if plan.note else ("ok", "")
                        ops = [
                            *ops,
                            _run_op(run_id, name, started, status, plan.items, input_sha256, error),
                        ]
                    packet = {
                        "packet_version": 1,
                        "change_id": change_id,
                        "agent": agent,
                        "intent": f"collect {name}: batch {index} of {total}, {plan.items} items",
                        "ops": ops,
                    }
                    outcome = applier.apply(store.canonical_json(packet).encode())
                    trace.note(outcome)
                    submitted += 1
                    last = outcome
                    if outcome.exit_code == 0:
                        continue
                    if outcome.exit_code == 3:
                        last_conflict = outcome
                        conflict = True
                        break
                    if outcome.exit_code == 5:
                        row = _run_row(applier, run_id)
                        return trace.result(
                            5,
                            _payload(
                                name,
                                run_id,
                                None if row is None else row["status"],
                                plan.items if row is None else row["items"],
                                submitted,
                                False,
                                outcome.receipt,
                            ),
                        )
                    return _record_error(
                        applier,
                        trace,
                        ctx,
                        run_id,
                        name,
                        agent,
                        input_sha256,
                        plan.items,
                        f"packet {change_id} was rejected: {outcome.receipt['error']}",
                        submitted + 1,
                    )
                if not conflict:
                    row = _run_row(applier, run_id)
                    return trace.result(
                        0,
                        _payload(
                            name,
                            run_id,
                            row["status"],
                            row["items"],
                            total,
                            False,
                            last.receipt,
                        ),
                    )
            row = _run_row(applier, run_id)
            return trace.result(
                3,
                _payload(
                    name,
                    run_id,
                    None if row is None else row["status"],
                    plan.items if row is None else row["items"],
                    submitted,
                    False,
                    None if last_conflict is None else last_conflict.receipt,
                ),
            )
    finally:
        applier.close()
