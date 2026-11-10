from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

from brain import __version__
from brain import ingress
from brain import store
from brain import reads
from brain import validate as validate_mod
from brain.applier import (
    Applier,
    NotAllowed,
    Outcome,
    Unavailable,
    UnknownChange,
    Unreadable,
    receipt_from_row,
)
from brain.config import ConfigError, load_config
from brain.init import InitError, apply_config, init_instance, search_rebuild
from brain.migrate import MigrationError, latest_version, migrate
from brain.schemas import UPSERT_TABLES
from brain.sessions import asking_session as sessions_asking



def _stdin():
    """Standard input as bytes when it has a byte buffer, or the stream itself when a caller swapped in a text one
    (an in-process caller or a test), so a bounded read never trips on a missing .buffer."""
    return getattr(sys.stdin, "buffer", sys.stdin)

def _print_json(payload) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _error(message: str) -> int:
    print(f"brain: {message}", file=sys.stderr)
    return 1


def _instance_dir(ns) -> Path:
    raw = getattr(ns, "instance", None) or os.environ.get("BRAIN_INSTANCE")
    if not raw:
        raise Unreadable("no instance given (pass --instance or set BRAIN_INSTANCE)")
    return Path(raw)


_READ_LIMIT = {"packet": ingress.MAX_PACKET_BYTES, "candidate": ingress.MAX_CANDIDATE_BYTES}


def _read_bytes(source: str, what: str = "packet") -> bytes:
    """A packet or candidate from a file or standard input, never more than its limit: a refusal (exit 2) rather
    than a wait on /dev/zero."""
    try:
        return ingress.read_bytes(_stdin() if source == "-" else source, _READ_LIMIT[what], what)
    except ingress.IngressError as exc:
        raise Unreadable(str(exc)) from None
    except OSError as exc:
        raise Unreadable(f"{what} is unreadable: {exc}") from exc


def _print_outcome(outcome: Outcome) -> int:
    _print_json(outcome.receipt)
    if outcome.warning:
        print(f"brain: {outcome.warning}", file=sys.stderr)
    from brain import applier as applier_mod

    if outcome.check_ms > applier_mod.CHECK_BUDGET_MS:
        print(
            f"brain: post-commit check took {outcome.check_ms} ms (budget 1000 ms); "
            "run brain check when the store is idle",
            file=sys.stderr,
        )
    return outcome.exit_code


def _runtime_block() -> str | None:
    old = store.sqlite_too_old_message()
    if old:
        return old
    return store.fts5_missing_message()


def _cmd_version(_ns) -> int:
    print(f"agent-si {__version__} (schema {latest_version()})")
    return 0


def _cmd_init(ns) -> int:
    from brain import registry

    if ns.name is None:
        return _guided_init(register=not ns.no_register)
    result = init_instance(
        ns.name,
        Path(ns.dir) if ns.dir else Path.cwd(),
        principal=ns.principal,
        timezone=ns.timezone,
    )
    notice = result.pop("_notice", None)
    if not ns.no_register:
        registry.add(result["instance"])  # Brain update migrates every registered instance
    _print_json(result)
    if notice:
        print(f"brain: {notice}", file=sys.stderr)
    return 0


def _guided_init(*, register: bool = True) -> int:
    """No name: ask, but only in a terminal. Prompts read /dev/tty, because under curl | sh stdin is the script."""
    from brain import guided

    try:
        reader, writer = open("/dev/tty", encoding="utf-8"), open("/dev/tty", "w", encoding="utf-8")
    except OSError:
        raise Unreadable('brain init needs a terminal to ask, or a name: brain init work --principal "Your Name" '
                         f"--timezone {guided.local_zone()}") from None
    with reader, writer:
        result = guided.run(reader, writer, register=register)
    _print_json({key: value for key, value in result.items() if key != "connected"})
    return 0


def _cmd_setup(ns) -> int:
    """One command: a brain, registered, with every detected tool connected. One question at most; nothing scheduled."""
    from brain import connect, setup

    options = setup.Options(yes=ns.yes, principal=ns.principal, name=ns.name, dir=ns.dir, timezone=ns.timezone,
                            no_connect=ns.no_connect, no_sessions=ns.no_sessions)
    try:
        result = setup.run(options)
    except connect.ConnectError as exc:
        raise Unavailable(str(exc)) from None
    return 0 if result["ok"] else 1


def _cmd_connect(ns) -> int:
    from brain import connect

    def ask(question: str) -> bool:
        if not sys.stdin.isatty():
            return False
        return input(f"{question}? [y/N] ").strip().lower() in ("y", "yes")

    try:
        if ns.url:
            results = connect.remote_connect(ns.harnesses, ns.url, ns.token_env, ns.name or "brain-hub",
                                             print_only=ns.print_only, remove=ns.remove, yes=ns.yes, ask=ask)
        else:
            results = connect.connect(_instance_dir(ns), ns.harnesses, print_only=ns.print_only, remove=ns.remove,
                                      yes=ns.yes, ask=ask)
    except connect.ConnectError as exc:
        raise Unavailable(str(exc)) from None
    payload = {"results": results}
    if not ns.url and not ns.print_only and not ns.remove:
        # Connecting is when a person first meets the brain, so the record of past sessions is built now, not on
        # the first search.
        from brain import sessions as sessions_mod

        instance = _instance_dir(ns)
        payload["sessions"] = sessions_mod.summary(instance, load_config(instance / "brain.toml"))
    _print_json(payload)
    return 1 if any(item["status"] == "failed" or item["smoke"] == "fail" for item in results) else 0


def _cmd_migrate(ns) -> int:
    instance = _instance_dir(ns)
    if (instance / "PAUSED").exists():
        raise Unavailable("instance is paused: see PAUSED; investigate, then delete it")
    old, new = migrate(instance / "brain.sqlite3")
    if old == new or old < 1:
        _print_json({"from": old, "to": new})
        return 0
    if 1 <= old < store.SEARCH_SINCE <= new:
        try:
            outcome = search_rebuild(instance, f"migrate {old} to {new}")
        except Unavailable as exc:
            _print_json({"from": old, "to": new, "search_rebuild": None})
            print(f"brain: {exc}", file=sys.stderr)
            return 5
        _print_json({"from": old, "to": new, "search_rebuild": outcome.receipt})
        return outcome.exit_code
    _print_json({"from": old, "to": new, "search_rebuild": None})
    return 0


def _cmd_search_rebuild(ns) -> int:
    instance = _instance_dir(ns)
    if (instance / "PAUSED").exists():
        raise Unavailable("instance is paused: see PAUSED; investigate, then delete it")
    if (instance / "UPDATING").exists():
        raise Unavailable("brain is updating; writes resume shortly")
    outcome = search_rebuild(instance, ns.reason)
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        indexed = con.execute("SELECT count(*) FROM search_keys").fetchone()[0]
    finally:
        con.close()
    _print_json(
        {"status": outcome.receipt["status"], "receipt": outcome.receipt, "indexed": indexed}
    )
    return outcome.exit_code


def _cmd_read(ns) -> int:
    try:
        raw = ingress.read_bytes(_stdin(), ingress.MAX_REQUEST_BYTES, "--args") if ns.args == "-" else ns.args
        arguments = ingress.loads(raw, what="--args")
    except ingress.IngressError as exc:
        print("brain: --args must be a JSON object" if exc.malformed else f"brain: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("brain: --args must be a JSON object", file=sys.stderr)
        return 2
    url = _remote(ns)
    if url:
        return _over_hub(url, "POST", f"/v1/read/{ns.tool}", json.dumps(arguments).encode())
    try:
        result = reads.run_tool(_instance_dir(ns), ns.tool, arguments, local=True,
                                asking=sessions_asking(os.environ))
    except reads.UnknownTool as exc:
        print(f"brain: unknown tool {exc.name}", file=sys.stderr)
        return 2
    sys.stdout.write(result.text if result.text.endswith("\n") else result.text + "\n")
    if result.code in {"unavailable", "internal"}:
        return 5
    if result.is_error:
        return 1
    return 0


def _cmd_serve(ns) -> int:
    from brain import server
    from brain.config import load_config

    instance = _instance_dir(ns)
    db = instance / "brain.sqlite3"
    if not db.is_file():
        print(f"brain: no store at {db}", file=sys.stderr)
        return 5
    con = store.connect_readonly(db)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    expected = latest_version()
    if version != expected:
        print(
            f"brain: store is at version {version}, engine expects {expected}; run brain migrate",
            file=sys.stderr,
        )
        return 5
    try:
        load_config(instance / "brain.toml")
    except ConfigError as exc:
        print(f"brain: brain.toml: {exc}", file=sys.stderr)
        return 5
    return server.serve(instance)


def _cmd_apply_config(ns) -> int:
    instance = _instance_dir(ns)
    if (instance / "PAUSED").exists():
        raise Unavailable("instance is paused: see PAUSED; investigate, then delete it")
    outcome = apply_config(instance)
    warnings = list(getattr(apply_config, "last_warnings", []))
    if outcome is None:
        _print_json({"status": "nothing to apply", "warnings": warnings})
        return 0
    payload = {"status": outcome.receipt["status"], "receipt": outcome.receipt, "warnings": warnings}
    batches = list(getattr(apply_config, "last_batches", []))
    if len(batches) > 1:
        payload["batches"] = batches
    _print_json(payload)
    return outcome.exit_code


def _cmd_collect(ns) -> int:
    from brain import applier as applier_mod
    from brain.collect import CollectorBusy, run

    fixture = None if ns.fixture is None else Path(ns.fixture)
    try:
        result = run(_instance_dir(ns), ns.name, fixture, allow_mass_change=ns.allow_mass_change)
    except CollectorBusy as exc:
        print(store.canonical_json({"collector": exc.collector, "status": "busy"}))
        raise
    if result.payload is not None:
        print(json.dumps(result.payload, ensure_ascii=False, separators=(",", ":")))
    for warning in result.warnings:
        print(f"brain: {warning}", file=sys.stderr)
    for elapsed in result.check_ms:
        if elapsed > applier_mod.CHECK_BUDGET_MS:
            print(
                f"brain: post-commit check took {elapsed} ms (budget 1000 ms); "
                "run brain check when the store is idle",
                file=sys.stderr,
            )
    return result.exit_code


def _cmd_backup(ns) -> int:
    from brain import backup

    if not ns.out:
        raise Unreadable("brain backup needs --out DIR")
    _print_json(backup.backup(_instance_dir(ns), Path(ns.out), ns.label or ""))
    return 0


def _cmd_redact(ns) -> int:
    from brain import redact

    if (_instance_dir(ns) / "UPDATING").exists():
        raise Unavailable("brain is updating; writes resume shortly")
    result = redact.command(
        _instance_dir(ns), ns.source_id, ns.reason, yes=ns.yes, operator=ns.operator, backups=ns.backups or []
    )
    _print_json(result)
    return 0


def _cmd_restore(ns) -> int:
    from brain import backup

    _print_json(backup.restore(Path(ns.bundle), Path(ns.dir)))
    return 0


def _cmd_extract(ns) -> int:
    from brain.extract import run

    url = _remote(ns)
    if url:
        return _over_hub(url, "POST", "/v1/extract", _read_bytes(ns.source, "candidate"))
    result = run(_instance_dir(ns), ns.as_agent, _read_bytes(ns.source, "candidate"))
    _print_json(result.payload)
    for warning in result.warnings:
        print(f"brain: {warning}", file=sys.stderr)
    return result.exit_code


def _cmd_held(ns) -> int:
    from brain.extract import held

    if ns.limit < 1 or ns.limit > 500:
        raise Unreadable("--limit must be between 1 and 500")
    url = _remote(ns)
    if url:
        return _over_hub(url, "GET", "/v1/held", query={"limit": ns.limit})
    instance = _instance_dir(ns)
    db = instance / "brain.sqlite3"
    if not db.is_file():
        print(f"brain: no store at {db}", file=sys.stderr)
        return 5
    con = store.connect_readonly(db)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
    finally:
        con.close()
    expected = latest_version()
    if version != expected:
        print(
            f"brain: store is at version {version}, engine expects {expected}; run brain migrate",
            file=sys.stderr,
        )
        return 5
    try:
        load_config(instance / "brain.toml")
    except ConfigError as exc:
        print(f"brain: brain.toml: {exc}", file=sys.stderr)
        return 5
    _print_json(held(instance, ns.limit))
    return 0


def _remote(ns):
    """The hub URL for --remote or an https BRAIN_INSTANCE, or None for a local instance."""
    from brain_hub import remote

    try:
        return remote.target(getattr(ns, "remote", None))
    except remote.RemoteError as exc:
        raise Unreadable(str(exc)) from None


def _over_hub(url: str, method: str, path: str, body: bytes | None = None, query: dict | None = None) -> int:
    """Run one remote command: the hub's body on stdout, as a local command would print it, and its exit code."""
    from brain_hub import remote

    try:
        status, code, data = remote.call(url, method, path, body, query)
    except remote.RemoteError as exc:
        raise Unavailable(str(exc)) from None
    text = data.decode("utf-8", errors="replace")
    problem = remote.error_text(data) if status >= 400 else ""
    if problem and not text.lstrip().startswith('{"agent"'):
        print(f"brain: {problem}", file=sys.stderr)
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return code


def _cmd_apply(ns) -> int:
    url = _remote(ns)
    if url:
        return _over_hub(url, "POST", "/v1/apply", _read_bytes(ns.packet))
    instance = _instance_dir(ns)
    applier = Applier(instance)
    try:
        return _print_outcome(applier.apply(_read_bytes(ns.packet)))
    finally:
        applier.close()


def _approval_channel(relay: bool) -> str:
    """How an approval reached the brain: agent-relay when --relay is given or
    standard input isn't an interactive terminal, else terminal."""
    if relay:
        return "agent-relay"
    try:
        return "terminal" if sys.stdin.isatty() else "agent-relay"
    except Exception:
        return "agent-relay"


def _remote_review(ns, url: str, decision: str) -> int:
    """The hub takes the approver from the token; --as, if given, must be that agent."""
    from brain_hub import remote

    if ns.as_agent:
        try:
            status, code, data = remote.call(url, "GET", "/v1/whoami")
        except remote.RemoteError as exc:
            raise Unavailable(str(exc)) from None
        try:
            agent = ingress.loads(data, what="the hub's answer").get("agent") if status == 200 else None
        except (ingress.IngressError, AttributeError):
            raise Unavailable("the hub's answer to /v1/whoami isn't a JSON object") from None
        if agent != ns.as_agent:
            raise Unreadable(f"--as {ns.as_agent} doesn't match this token's agent")
    body = json.dumps({"note": ns.note}).encode() if decision == "refuse" else b"{}"
    return _over_hub(url, "POST", f"/v1/changes/{remote.quote(ns.change_id)}/{decision}", body)


def _cmd_approve(ns) -> int:
    url = _remote(ns)
    if url:
        return _remote_review(ns, url, "approve")
    if not ns.as_agent:
        raise Unreadable("brain approve needs --as AGENT")
    instance = _instance_dir(ns)
    applier = Applier(instance)
    try:
        return _print_outcome(applier.approve(ns.change_id, ns.as_agent, via=_approval_channel(ns.relay)))
    finally:
        applier.close()


def _cmd_refuse(ns) -> int:
    url = _remote(ns)
    if url:
        return _remote_review(ns, url, "refuse")
    if not ns.as_agent:
        raise Unreadable("brain refuse needs --as AGENT")
    instance = _instance_dir(ns)
    applier = Applier(instance)
    try:
        return _print_outcome(
            applier.refuse(ns.change_id, ns.as_agent, ns.note, via=_approval_channel(ns.relay))
        )
    finally:
        applier.close()


def _cmd_receipt(ns) -> int:
    url = _remote(ns)
    if url:
        from brain_hub import remote

        return _over_hub(url, "GET", f"/v1/receipts/{remote.quote(ns.change_id)}")
    instance = _instance_dir(ns)
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        con.row_factory = __import__("sqlite3").Row
        row = con.execute(
            "SELECT * FROM write_log WHERE change_id = ?", (ns.change_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        raise Unreadable(f"no change {ns.change_id}")
    receipt = receipt_from_row(row)
    _print_json(receipt)
    return {"applied": 0, "rejected": 1, "conflict": 3, "held": 4}[receipt["status"]]


def _cmd_base(ns) -> int:
    instance = _instance_dir(ns)
    if ns.table not in UPSERT_TABLES:
        raise Unreadable(f"unknown table {ns.table}")
    key = UPSERT_TABLES[ns.table]
    con = store.connect_readonly(instance / "brain.sqlite3")
    try:
        row = con.execute(
            f"SELECT version FROM {ns.table} WHERE {key} = ?", (ns.id,)
        ).fetchone()
    finally:
        con.close()
    _print_json({"table": ns.table, "id": ns.id, "base": None if row is None else row[0]})
    return 0


def _cmd_check(ns) -> int:
    instance = _instance_dir(ns)
    try:
        cfg = load_config(instance / "brain.toml")
    except (ConfigError, OSError):
        cfg = None
    if cfg is not None:
        for name in sorted(agent.name for agent in cfg.agents.values() if agent.manual_sources):
            print(
                f"brain: agent {name} adds sources by hand (manual_sources = true)",
                file=sys.stderr,
            )
    db = instance / "brain.sqlite3"
    if db.is_file():
        # A store with a hot journal cannot be read read-only; the check below names the journal, so the
        # two read errors are not repeated.
        hot = (db.parent / (db.name + "-journal")).exists()
        con = store.connect_readonly(db)
        try:
            con.execute("PRAGMA query_only = ON")
            nontext = store.nontext_version_keys(con)
            unmatchable = store.unmatchable_version_values(con)
        except sqlite3.Error as exc:
            if not hot:
                print(f"brain: could not read fact keys: {exc}", file=sys.stderr)
            nontext, unmatchable = [], []
        try:
            note_lines = store.note_meta_warnings(con)
            if cfg is not None:
                note_lines += store.agent_person_warnings(con, cfg.agents.values())
            noted_rows = store.rows_with_notes(con) if cfg is not None and not cfg.writes.notes else 0
        except sqlite3.Error as exc:
            if not hot:
                print(f"brain: could not read notes: {exc}", file=sys.stderr)
            note_lines, noted_rows = [], 0
        finally:
            con.close()
        for key in nontext:
            print(f"brain: fact key {key} is not text, so it sets no version", file=sys.stderr)
        for key, value in unmatchable:
            print(f'brain: fact key {key} holds "{value}", which no claim scope can match', file=sys.stderr)
        for line in note_lines:
            print(f"brain: {line}", file=sys.stderr)
        if cfg is not None and not cfg.writes.notes:
            print(f"brain: notes are off, and {noted_rows} rows hold note text", file=sys.stderr)
    problems = store.check_instance(instance)
    _print_json({"ok": not problems, "problems": problems})
    return 0 if not problems else 1


def _cmd_update(ns) -> int:
    from brain import update

    try:
        if ns.status:
            _print_json(update.status(ns.instance))
            return 0
        if ns.check:
            code, receipt = update.check_releases(ns.instance)
        else:
            code, receipt = update.update(ns.instance, {"auto": ns.auto, "to": ns.to, "from_wheel": ns.from_wheel,
                                                       "rollback_from": ns.rollback_from})
    except update.Refused as exc:
        _print_json({"result": "refused", "message": str(exc)})
        print(f"brain: {exc}", file=sys.stderr)
        return 1
    _print_json(receipt)
    if code == 5:
        print(f"brain: update {receipt.get('result')}: {receipt.get('message')}", file=sys.stderr)
    return code


def _cmd_usage(ns) -> int:
    """brain usage report: a Markdown and a JSON file in <instance>/usage/, counts and enums only. Local only."""
    from brain import usage

    instance = _instance_dir(ns)
    data = usage.report(instance, ns.period)
    usage.prune(instance)
    folder = instance / "usage"
    folder.mkdir(mode=0o700, exist_ok=True)
    stem = folder / f"report-{ns.period}-{data['to'][:10]}"
    stem.with_suffix(".json").write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    stem.with_suffix(".md").write_text(usage.markdown(data), encoding="utf-8")
    _print_json({"json": str(stem.with_suffix(".json")), "markdown": str(stem.with_suffix(".md")), "calls": data["calls"]})
    return 0


def _cmd_sessions(ns) -> int:
    """brain sessions: the record of past agent sessions beside the brain. status and sync print counts only."""
    from brain import advice
    from brain import sessions as sessions_mod
    from brain.config import load_config
    from brain.sessions import record

    instance = _instance_dir(ns)
    cfg = load_config(instance / "brain.toml")
    db = record.path_for(instance)
    if ns.action == "status":
        _print_json({"record": cfg.sessions.record, "file": str(db), **record.status(db)})
        return 0
    if ns.action == "sync":
        if not cfg.sessions.record:
            message = advice.sessions_off_error()
            _print_json({"status": "off", "message": message})
            print(f"brain: {message}", file=sys.stderr)
            return 1
        result = sessions_mod.refresh(instance, cfg)
        not_installed = sorted({e["harness"] for e in result.errors if e["error"] == "FileNotFoundError"})
        _print_json(
            {
                "status": "busy" if result.busy else "synced",
                "sessions": result.sessions,
                "new_sessions": result.new_sessions,
                "new_messages": result.new_messages,
                "skipped_secret": result.skipped_secret,
                "seconds": round(result.seconds, 2),
                "no_store_found": not_installed,
                "errors": [e for e in result.errors if e["error"] != "FileNotFoundError"],
            }
        )
        return 0
    # search shows a session with a message number on the end (claude:0f3a...#12). These commands act on the
    # session, so the number is dropped.
    place = record.parse_place(ns.session)
    if place is None:
        raise Unreadable(advice.session_ref_error(ns.session))
    ref = place[:2]
    session = record.session_ref(*ref)
    if ns.action == "excerpt":
        from brain.sessions import cite

        try:
            _print_json(cite.excerpt_op(instance, cfg, session, ns.first, ns.last, ns.id))
        except cite.CiteError as exc:
            raise Unreadable(str(exc)) from None
        return 0
    # forget
    removed = record.forget(db, *ref)
    _print_json({"status": "forgotten", "session": session, "messages_removed": removed})
    return 0


def _cmd_schedule(ns) -> int:
    from brain import advice, update_schedule

    if ns.state == "on":
        message = advice.updates_elsewhere_error("brain schedule on")
        _print_json({"result": "refused", "message": message})
        print(f"brain: {message}", file=sys.stderr)
        return 1
    _print_json(update_schedule.off())
    return 0


def _cmd_router(ns) -> int:
    from brain.config import load_config
    from brain.init import render_router

    instance = _instance_dir(ns)
    cfg = load_config(instance / "brain.toml")
    data = render_router(cfg.name, cfg.writes.notes).encode("utf-8")
    router = instance / "ROUTER.md"
    if router.is_file() and router.read_bytes() == data:
        _print_json({"status": "unchanged", "router_bytes": len(data)})
        return 0
    tmp = router.with_name(f".{router.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, router)
    finally:
        if tmp.exists():
            tmp.unlink()
    _print_json({"status": "updated", "router_bytes": router.stat().st_size})
    return 0


def _cmd_validate(ns) -> int:
    raw = _read_bytes(ns.source, ns.kind)
    if ns.instance:
        if ns.kind != "packet":
            raise Unreadable("--instance applies to brain validate packet only")
        return _validate_against(Path(ns.instance), raw)
    code, payload = validate_mod.validate_command(ns.kind, raw)
    if ns.kind == "packet":
        payload["checked"] = VALIDATE_SHAPE_ONLY
    _print_json(payload)
    return code


VALIDATE_SHAPE_ONLY = (
    "shape only: the packet's fields were checked, not the store, the evidence gate or the agent's policy; "
    "pass --instance DIR for the verdict brain apply would give"
)
VALIDATE_DRY_RUN = "the verdict brain apply would give, from a dry run on this instance; nothing was written"


def _validate_against(instance: Path, raw: bytes) -> int:
    """The real verdict: the applier's own decision, rolled back. The exit code is apply's."""
    applier = Applier(instance)
    try:
        outcome = applier.apply(raw, dry_run=True)
    except Unreadable as exc:
        message = str(exc)
        _print_json(
            {
                "status": "unreadable", "kind": "packet", "errors": [{"path": "", "message": message}],
                "error": message, "targets": [], "dry_run": True, "checked": VALIDATE_DRY_RUN,
            }
        )
        return 2
    finally:
        applier.close()
    receipt = outcome.receipt
    payload = {
        "status": receipt["status"], "kind": "packet",
        "errors": [{"path": "", "message": receipt["error"]}] if receipt["error"] else [],
        "error": receipt["error"], "targets": receipt["targets"], "change_id": receipt["change_id"],
        "dry_run": True, "checked": VALIDATE_DRY_RUN,
    }
    if outcome.duplicate:
        payload["duplicate"] = True
    if outcome.warning:
        payload["warning"] = outcome.warning
    _print_json(payload)
    return outcome.exit_code


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain")
    parser.add_argument("--version", action="store_true")
    subs = parser.add_subparsers(dest="command")

    init = subs.add_parser("init")
    init.add_argument("name", nargs="?", default=None)
    init.add_argument("--dir")
    init.add_argument("--principal")
    init.add_argument("--timezone", default="UTC")
    # A scripted or throwaway brain (a test, an hourly rebuild) stays out of the registry, so brain update leaves it.
    init.add_argument("--no-register", action="store_true")
    init.set_defaults(func=_cmd_init)

    setup_p = subs.add_parser("setup")
    setup_p.add_argument("--yes", action="store_true", help="ask nothing; take every default")
    setup_p.add_argument("--principal", default=None, help='your name; "" for none')
    setup_p.add_argument("--name", default="work")
    setup_p.add_argument("--dir", default="~/Brains")
    setup_p.add_argument("--timezone", default=None)
    setup_p.add_argument("--no-connect", action="store_true")
    setup_p.add_argument("--no-sessions", action="store_true", help="start with past-session recording off")
    setup_p.set_defaults(func=_cmd_setup)

    migrate_p = subs.add_parser("migrate")
    migrate_p.add_argument("--instance")
    migrate_p.set_defaults(func=_cmd_migrate)

    rebuild = subs.add_parser("search-rebuild")
    rebuild.add_argument("--instance")
    rebuild.add_argument("--reason", default="manual")
    rebuild.set_defaults(func=_cmd_search_rebuild)

    read_p = subs.add_parser("read")
    read_p.add_argument("tool")
    read_p.add_argument("--instance")
    read_p.add_argument("--remote", default=None)
    read_p.add_argument("--args", default="{}")
    read_p.set_defaults(func=_cmd_read)

    serve_p = subs.add_parser("serve")
    serve_p.add_argument("--instance")
    serve_p.set_defaults(func=_cmd_serve)

    apply_cfg = subs.add_parser("apply-config")
    apply_cfg.add_argument("--instance")
    apply_cfg.set_defaults(func=_cmd_apply_config)

    apply_p = subs.add_parser("apply")
    apply_p.add_argument("packet")
    apply_p.add_argument("--instance")
    apply_p.add_argument("--remote", default=None)
    apply_p.set_defaults(func=_cmd_apply)

    extract_p = subs.add_parser("extract")
    extract_p.add_argument("source")
    extract_p.add_argument("--instance")
    extract_p.add_argument("--remote", default=None)
    extract_p.add_argument("--as", dest="as_agent", required=True)
    extract_p.set_defaults(func=_cmd_extract)

    held_p = subs.add_parser("held")
    held_p.add_argument("--instance")
    held_p.add_argument("--remote", default=None)
    held_p.add_argument("--limit", type=int, default=50)
    held_p.set_defaults(func=_cmd_held)

    collect_p = subs.add_parser("collect")
    collect_p.add_argument("name")
    collect_p.add_argument("--instance")
    collect_p.add_argument("--input", "--fixture", dest="fixture", default=None)
    collect_p.add_argument("--allow-mass-change", action="store_true")
    collect_p.set_defaults(func=_cmd_collect)

    backup_p = subs.add_parser("backup")
    backup_p.add_argument("--instance")
    backup_p.add_argument("--out")
    backup_p.add_argument("--label", default=None)
    backup_p.set_defaults(func=_cmd_backup)

    redact_p = subs.add_parser("redact")
    redact_p.add_argument("source_id")
    redact_p.add_argument("--reason", required=True)
    redact_p.add_argument("--yes", action="store_true")
    redact_p.add_argument("--operator", default=None)
    redact_p.add_argument("--backups", action="append", default=None)
    redact_p.add_argument("--instance")
    redact_p.set_defaults(func=_cmd_redact)

    restore_p = subs.add_parser("restore")
    restore_p.add_argument("bundle")
    restore_p.add_argument("--dir", required=True)
    restore_p.set_defaults(func=_cmd_restore)

    approve = subs.add_parser("approve")
    approve.add_argument("change_id")
    approve.add_argument("--as", dest="as_agent", default=None)
    approve.add_argument("--instance")
    approve.add_argument("--remote", default=None)
    approve.add_argument("--relay", action="store_true")
    approve.set_defaults(func=_cmd_approve)

    refuse = subs.add_parser("refuse")
    refuse.add_argument("change_id")
    refuse.add_argument("--as", dest="as_agent", default=None)
    refuse.add_argument("--note", required=True)
    refuse.add_argument("--instance")
    refuse.add_argument("--remote", default=None)
    refuse.add_argument("--relay", action="store_true")
    refuse.set_defaults(func=_cmd_refuse)

    receipt = subs.add_parser("receipt")
    receipt.add_argument("change_id")
    receipt.add_argument("--instance")
    receipt.add_argument("--remote", default=None)
    receipt.set_defaults(func=_cmd_receipt)

    base = subs.add_parser("base")
    base.add_argument("table")
    base.add_argument("id")
    base.add_argument("--instance")
    base.set_defaults(func=_cmd_base)

    check = subs.add_parser("check")
    check.add_argument("--instance")
    check.set_defaults(func=_cmd_check)

    usage_p = subs.add_parser("usage")
    usage_p.add_argument("action", choices=("report",))
    usage_p.add_argument("--period", choices=("week", "month"), default="week")
    usage_p.add_argument("--instance")
    usage_p.set_defaults(func=_cmd_usage)

    sessions_p = subs.add_parser("sessions")
    sessions_subs = sessions_p.add_subparsers(dest="action", required=True)
    for action in ("status", "sync"):
        sub = sessions_subs.add_parser(action)
        sub.add_argument("--instance")
    forget_p = sessions_subs.add_parser("forget")
    forget_p.add_argument("session", help="a session id as search shows it, such as claude:0f3a...")
    forget_p.add_argument("--instance")
    excerpt_p = sessions_subs.add_parser("excerpt")
    excerpt_p.add_argument("session", help="a session id as search shows it")
    excerpt_p.add_argument("--from", dest="first", type=int, required=True, help="the first message's number")
    excerpt_p.add_argument("--to", dest="last", type=int, required=True, help="the last message's number")
    excerpt_p.add_argument("--id", default=None, help="the source id to use, starting src-")
    excerpt_p.add_argument("--instance")
    sessions_p.set_defaults(func=_cmd_sessions)

    connect_p = subs.add_parser("connect")
    connect_p.add_argument("harnesses", nargs="+", metavar="HARNESS")
    connect_p.add_argument("--instance")
    connect_p.add_argument("--print", dest="print_only", action="store_true")
    connect_p.add_argument("--remove", action="store_true")
    connect_p.add_argument("--yes", action="store_true")
    connect_p.add_argument("--url", default=None, help="a hub's https address, ending /mcp")
    connect_p.add_argument("--token-env", default="BRAIN_TOKEN")
    connect_p.add_argument("--name", default=None)
    connect_p.set_defaults(func=_cmd_connect)

    from brain_hub import cli as hub_cli

    hub_cli.add_parser(subs)

    update_p = subs.add_parser("update")
    update_p.add_argument("--instance", action="append", default=[])
    update_p.add_argument("--check", action="store_true")
    update_p.add_argument("--auto", action="store_true")
    update_p.add_argument("--status", action="store_true")
    update_p.add_argument("--to", default=None)  # refused: there is no release source to pick a version from
    update_p.add_argument("--from-wheel", "--from", dest="from_wheel", default=None, metavar="WHEEL",
                          help="install this agent-si wheel from the releases page, with a backup and a rollback")
    update_p.add_argument("--rollback-from", dest="rollback_from", default=None, metavar="WHEEL",
                          help="the wheel of the version you run now, when brain update can't keep one itself")
    update_p.set_defaults(func=_cmd_update)

    schedule_p = subs.add_parser("schedule")
    schedule_p.add_argument("state", choices=("on", "off"))
    schedule_p.add_argument("--check-only", action="store_true")
    schedule_p.set_defaults(func=_cmd_schedule)

    router = subs.add_parser("router")
    router.add_argument("--instance")
    router.set_defaults(func=_cmd_router)

    val = subs.add_parser("validate")
    val.add_argument("kind", choices=("packet", "candidate"))
    val.add_argument("source")
    val.add_argument("--instance")
    val.set_defaults(func=_cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--version"] or (argv and argv[0] == "--version"):
        blocked = _runtime_block()
        if blocked:
            print(f"brain: {blocked}", file=sys.stderr)
            return 5
        return _cmd_version(None)
    parser = _build_parser()
    try:
        ns = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    if not getattr(ns, "command", None):
        parser.print_usage(sys.stderr)
        return 2
    blocked = _runtime_block()
    if blocked:
        print(f"brain: {blocked}", file=sys.stderr)
        return 5
    try:
        return ns.func(ns)
    except Unreadable as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 2
    except UnknownChange as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 2
    except NotAllowed as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 1
    except Unavailable as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 5
    except ConfigError as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 5
    except MigrationError as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return 5
    except InitError as exc:
        print(f"brain: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception:
        traceback.print_exc()
        return 5
