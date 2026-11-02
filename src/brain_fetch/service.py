"""The run every network service shares: credential, cursor, pull, exports, collect, cursor.

The cursor moves only when every export is complete and every collect returned 0. A failed pull still writes
an export, so the run is recorded and the brief's Intake line can say the collector is failing.
"""
from __future__ import annotations

from pathlib import Path

from brain import collect, config as config_mod, marks, store
from brain.applier import Unavailable, Unreadable
from brain.text import single_line
from brain_fetch import credentials, gcal, github, gmail, net, slack, state
from brain_fetch.common import CALENDAR, CAPTURE, Ctx, capture_doc, service_of
from brain_fetch.documents import result_for

MODULES = {"gmail": gmail, "slack": slack, "github": github, "calendar": gcal}


def _failed(ctx: Ctx, message: str, error_class: str) -> tuple[str, dict]:
    message = single_line(message, max_chars=300)
    if ctx.collector.type == "calendar":
        doc = {"format": CALENDAR, "fetched_at": ctx.fetched, "complete": False, "account": ctx.options["account"],
               "outbound_visible": True, "events": []}
    else:
        doc = capture_doc(ctx, [], complete=False, incremental=False)
    doc["error"], doc["error_class"] = message, error_class
    return ctx.collector.name, doc


def _sign(instance: Path, doc: dict) -> dict:
    if doc.get("format") == CAPTURE and any(item.get("from_principal") is True for item in doc["items"]):
        doc["mark"] = marks.sign(marks.ensure_key(instance), doc)
    return doc


def run(instance, name: str, *, rescan: bool = False, allow_mass_change: bool = False) -> dict:
    instance = Path(instance)
    cfg = config_mod.load_config(instance / "brain.toml")
    try:
        with state.run_lock(instance, name):
            return _locked(instance, cfg, cfg.collectors[name], rescan, allow_mass_change)
    except state.Busy as exc:
        return result_for(name, "busy", str(exc))


def _locked(instance: Path, cfg, collector, rescan: bool, allow: bool) -> dict:
    name, service = collector.name, service_of(collector)
    module = MODULES[service]
    cursor = {} if rescan else state.read_state(instance, name, service)
    ctx = Ctx(instance, cfg, collector, "", cursor, store.utc_now())
    pulled, problem = None, ""
    try:
        try:
            secret = credentials.read_secret(cfg.name, name)
        except credentials.SecretFileError as exc:
            raise net.NetError(str(exc), "auth") from None
        if secret is None:
            raise net.NetError(f"no credential (looked in {credentials.where_it_looked(cfg.name, name)}); "
                               f"brain-fetch setup {service} says where it goes", "auth")
        ctx.secret = secret
        pulled = module.pull(ctx)
        docs = pulled.docs
    except net.NetError as exc:
        problem = f"{exc.error_class}: {exc}"
        docs = [_failed(ctx, str(exc), exc.error_class)]
    except (KeyError, IndexError, TypeError, AttributeError, ValueError) as exc:
        # An answer in a shape the module didn't expect: record a failed run (no text from the answer, which
        # could hold anything) rather than stop the scheduled job with a traceback.
        message = f"{service} answered in a shape brain-fetch didn't expect ({type(exc).__name__})"
        problem = f"other: {message}"
        docs = [_failed(ctx, message, "other")]
    paths, pruned = [], 0
    try:
        for target, doc in docs:
            inbox = state.inbox_dir(instance, target)
            path = state.export_path(inbox, ctx.fetched.replace("-", "").replace(":", ""))
            state.write_export(path, _sign(instance, doc))
            paths.append((target, path))
    except (state.InboxFull, OSError) as exc:
        return result_for(name, "error", str(exc), exports=len(paths), items=0)
    codes, last = [], None
    for target, path in paths:
        try:
            last = collect.run(instance, target, path, allow_mass_change=allow)
        except Unavailable as exc:
            return result_for(name, "unavailable", str(exc), exports=len(paths), items=0)
        except Unreadable as exc:
            return result_for(name, "error", str(exc), exports=len(paths), items=0)
        codes.append(last.exit_code)
        if last.exit_code != 0:
            problem = problem or f"collect {target} exited {last.exit_code}"
            break
    for target in dict.fromkeys(target for target, _ in paths):
        pruned += state.prune_inbox(state.inbox_dir(instance, target))
    advanced = (pulled is not None and pulled.complete and len(codes) == len(paths)
                and all(code == 0 for code in codes) and all(doc["complete"] for _, doc in docs))
    if advanced:
        state.write_state(instance, name, service, pulled.cursor)
    status = "error" if problem else (last.payload or {}).get("status", "ok") if last else "ok"
    message = problem or (pulled.note if pulled else "")
    if pulled is not None and not pulled.complete and not problem:
        message = "; ".join(filter(None, [message, "the export was incomplete; the next run tries again"]))
    return result_for(name, status, message, exports=len(paths), items=pulled.items if pulled else 0,
                      cursor="advanced" if advanced else "held", pruned=pruned)
