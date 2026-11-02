"""The brain-fetch command line. Exits: 0 done, 1 a collector failed, 2 usage or
unreadable config, 5 unavailable or busy. Every message on stderr is `brain-fetch: <text>`; stdout is one
JSON object with sorted keys."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from pathlib import Path

from brain import __version__
from brain import config as config_mod
from brain.applier import Unavailable, Unreadable
from brain_fetch import check as check_mod, documents, guides, schedule, service
from brain_fetch.common import service_of

_NAME = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_EVERY = re.compile(r"([0-9]+)([mh])")
class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Unreadable(message)


def _print(payload) -> None:
    print(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def _instance(ns) -> Path:
    raw = ns.instance or os.environ.get("BRAIN_INSTANCE")
    if not raw:
        raise Unreadable("no instance given (pass --instance or set BRAIN_INSTANCE)")
    return Path(raw)


def _served(collector) -> bool:
    """A collector brain-fetch runs: a documents folder, a network service, or a calendar with an id."""
    kind = service_of(collector)
    if kind == "documents":
        return bool(collector.options.get("live_folder"))
    if kind == "calendar":
        return bool(collector.options.get("live_calendar_id"))
    return kind in service.MODULES


def _names(cfg, given: list[str]) -> list[str]:
    for name in given:
        collector = cfg.collectors.get(name)
        if collector is None:
            raise Unreadable(f"no collector {name}")
        if collector.type not in ("capture", "calendar"):
            raise Unreadable(
                f"collector {name} is type {collector.type}; brain-fetch handles capture and calendar collectors"
            )
        if collector.type == "calendar" and not collector.options.get("live_calendar_id"):
            raise Unreadable(f"collector {name} has no live_calendar_id")
    if given:
        return given
    return [n for n, item in cfg.collectors.items() if _served(item)]


def _setup(ns) -> int:
    instance = _instance(ns)
    cfg = config_mod.load_config(instance / "brain.toml")
    if ns.collector in guides.DEFAULT_NAMES:
        kind, name = ns.collector, guides.DEFAULT_NAMES[ns.collector]
    else:
        name = ns.collector
        configured = cfg.collectors.get(name)
        kind = service_of(configured) if configured is not None and service_of(configured) else "documents"
    if not _NAME.fullmatch(name):
        raise Unreadable(f"{ns.collector} is not a valid collector name")
    sys.stdout.write(guides.guide(kind, cfg.name, str(instance.resolve()), name))
    return 0


def _check(ns) -> int:
    instance = _instance(ns)
    cfg = config_mod.load_config(instance / "brain.toml")
    payload = check_mod.check(instance, cfg, _names(cfg, ns.collectors))
    _print(payload)
    return 0 if payload["ok"] else 1


def _run(ns) -> int:
    instance = _instance(ns)
    cfg = config_mod.load_config(instance / "brain.toml")
    results = [
        (documents if service_of(cfg.collectors[name]) == "documents" else service).run(
            instance, name, rescan=ns.rescan, allow_mass_change=ns.allow_mass_change)
        for name in _names(cfg, ns.collectors)
    ]
    _print({"collectors": results})
    for item in results:
        if item["status"] in ("busy", "unavailable") and item["message"]:
            print(f"brain-fetch: {item['message']}", file=sys.stderr)
        elif item["status"] == "error" and item["message"]:
            print(f"brain-fetch: {item['collector']}: {item['message']}", file=sys.stderr)
    statuses = {item["status"] for item in results}
    if "error" in statuses:
        return 1
    return 5 if statuses & {"busy", "unavailable"} else 0


def _schedule(ns) -> int:
    instance = _instance(ns)
    cfg = config_mod.load_config(instance / "brain.toml")
    minutes = cfg.fetch.every_minutes
    if ns.every is not None:
        match = _EVERY.fullmatch(ns.every)
        asked = int(match.group(1)) * (60 if match and match.group(2) == "h" else 1) if match else None
        if asked != minutes:
            raise Unreadable(
                f"--every {ns.every} disagrees with [fetch] every_minutes = {minutes}; change brain.toml first"
            )
    if ns.off:
        _print(schedule.remove(cfg.name, minutes))
    elif sys.platform == "darwin":
        _print(schedule.install(instance, cfg.name, minutes))
    else:
        text = schedule.systemd_units(instance, cfg.name, minutes)
        _print({"status": "printed", "label": schedule.label_for(cfg.name), "path": "", "every_minutes": minutes,
                "text": text})
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="brain-fetch")
    parser.add_argument("--version", action="store_true")
    subs = parser.add_subparsers(dest="command", parser_class=_Parser)
    for name, func in (("setup", _setup), ("check", _check), ("run", _run), ("schedule", _schedule)):
        sub = subs.add_parser(name)
        sub.add_argument("--instance")
        sub.set_defaults(func=func)
        if name == "setup":
            sub.add_argument("collector")
        elif name in ("check", "run"):
            sub.add_argument("collectors", nargs="*", metavar="COLLECTOR")
        if name == "run":
            sub.add_argument("--rescan", action="store_true")
            sub.add_argument("--allow-mass-change", action="store_true")
        if name == "schedule":
            sub.add_argument("--every")
            sub.add_argument("--off", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["--version"]:
            print(f"agent-si {__version__}")
            return 0
        ns = _parser().parse_args(argv)
        if not ns.command:
            raise Unreadable("usage: brain-fetch setup|check|run|schedule --instance DIR")
        return ns.func(ns)
    except SystemExit as exc:  # argparse --help
        return int(exc.code or 0)
    except Unreadable as exc:
        message, code = str(exc), 2
    except config_mod.ConfigError as exc:
        message, code = f"brain.toml: {exc}", 2
    except Unavailable as exc:
        message, code = str(exc), 5
    except OSError as exc:
        message, code = str(exc), 1
    except Exception:  # noqa: BLE001 - last resort: a traceback and exit 5, as brain does
        traceback.print_exc()
        return 5
    print(f"brain-fetch: {message}", file=sys.stderr)
    return code
