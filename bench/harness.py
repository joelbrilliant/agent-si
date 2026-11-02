"""Builds the Example Co benchmark instance and measures it.

Imports `brain` and the standard library. Not packaged. Everything here is pure except build_instance,
which writes only under the directory it is given.

    python -m bench.harness measure [--no-timing]
    python -m bench.harness fingerprint
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

from brain import reads, server, store
from brain.applier import Applier
from brain.init import init_instance

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "bench" / "exampleco"
BUDGETS = ROOT / "bench" / "budgets.json"
TOOLS = ("brief", "search", "get", "fact_get", "records_query", "signals")


class BuildError(Exception):
    pass


def load_manifest(dataset: Path = DATASET) -> dict:
    return json.loads((Path(dataset) / "MANIFEST.json").read_text(encoding="utf-8"))


def build_instance(dataset: Path, parent: Path) -> Path:
    """Init the `exampleco` instance and apply each manifest packet at the manifest's applied_at."""
    dataset = Path(dataset)
    manifest = load_manifest(dataset)
    config = (dataset / "exampleco.toml").read_text(encoding="utf-8")
    saved = store.utc_now
    store.utc_now = lambda: manifest["applied_at"]
    try:
        instance = Path(init_instance("exampleco", Path(parent), config_text=config)["instance"])
        for name in manifest["packets"]:
            writer = Applier(instance)
            try:
                outcome = writer.apply((dataset / "packets" / name).read_bytes())
            finally:
                writer.close()
            receipt = outcome.receipt
            if receipt.get("status") != "applied":
                raise BuildError(f"{name}: {receipt.get('status')} {receipt.get('error')}")
    finally:
        store.utc_now = saved
    return instance


def run_path(instance: Path, question: dict, now: str) -> list[reads.ToolResult]:
    return [
        reads.run_tool(instance, call["tool"], call["arguments"], now=now) for call in question["minimal_path"]
    ]


def _walk(value, path: str):
    for part in path.split("."):
        if isinstance(value, list):
            value = value[int(part)]
        else:
            value = value[part]
    return value


def check_expect(results: list[reads.ToolResult], expect: dict) -> list[str]:
    """One message per failure: `<n>:<path>: got <x>, want <y>` or `<n>:<path>: missing`."""
    problems: list[str] = []
    for key, want in expect.items():
        index_text, _, path = key.partition(":")
        index = int(index_text)
        if index >= len(results):
            problems.append(f"{key}: missing")
            continue
        result = results[index]
        if path == "$error":
            if not result.is_error:
                problems.append(f"{key}: got no error, want {want}")
            elif result.code != want:
                problems.append(f"{key}: got {result.code}, want {want}")
            continue
        if result.is_error:
            problems.append(f"{key}: got error {result.code}, want {want}")
            continue
        try:
            got = _walk(result.structured, path)
        except (KeyError, IndexError, TypeError, ValueError):
            problems.append(f"{key}: missing")
            continue
        if got != want or isinstance(got, bool) != isinstance(want, bool):
            problems.append(f"{key}: got {got!r}, want {want!r}")
    for index, result in enumerate(results):
        if result.is_error and f"{index}:$error" not in expect:
            problems.append(f"{index}:$error: got {result.code}, want no error")
    return problems


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def result_bytes(result: reads.ToolResult) -> int:
    """The bytes the server sends for one call, text and structured content together."""
    return len(_compact(server._tool_result(result, server.LATEST_VERSION)).encode("utf-8"))


def startup(instance: Path, now: str) -> dict:
    """The router file plus the initialize, tools/list and brief results, as the startup figure counts."""
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                    "clientInfo": {"name": "bench", "version": "0"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "brief", "arguments": {}}},
    ]
    stdin = BytesIO("".join(_compact(item) + "\n" for item in requests).encode("utf-8"))
    stdout = BytesIO()
    server.serve(Path(instance), stdin, stdout, now=now)
    replies = [json.loads(line) for line in stdout.getvalue().decode("utf-8").splitlines()]
    opened, listed, brief = (reply["result"] for reply in replies)
    router = len((Path(instance) / "ROUTER.md").read_bytes())
    size = lambda obj: len(_compact(obj).encode("utf-8"))  # noqa: E731
    brief_text = brief["content"][0]["text"]
    # Since the fast path, the handshake carries the brief, so the agent makes no brief call.
    called = 0 if opened.get("instructions", "").endswith(brief_text) else size(brief)
    return {
        "router": router,
        "initialize": size(opened),
        "tools_list": size(listed),
        "brief": called,
        "S": router + size(opened) + size(listed) + called,
        "brief_text_bytes": len(brief_text.encode("utf-8")),
        "brief_lines": len(brief_text.splitlines()),
        "tools": {tool["name"]: size(tool) for tool in listed["tools"]},
    }


def p95_ms(instance: Path, question: dict, now: str, repeats: int = 20) -> float:
    run_path(instance, question, now)
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        run_path(instance, question, now)
        times.append((time.perf_counter() - started) * 1000)
    times.sort()
    return times[math.ceil(0.95 * repeats) - 1]


def _entities(instance: Path, now: str) -> list[str]:
    found = reads.run_tool(
        instance, "records_query",
        {"entity": "fact", "fields": ["key"], "order_by": "key", "limit": 100}, now=now)
    keys = [row["key"] for row in found.structured["rows"]]
    return sorted({key.rpartition(".")[0] for key in keys})


def disputed_keys(instance: Path, now: str) -> list[str]:
    found = reads.run_tool(
        instance, "records_query",
        {"entity": "signal", "filters": {"kind": "conflict", "status": "open"}, "fields": ["subject_id"],
         "order_by": "subject_id", "limit": 100}, now=now)
    return [row["subject_id"] for row in found.structured["rows"]]


def figures(instance: Path, questions: list[dict], now: str, timing: bool = True) -> dict:
    start = startup(instance, now)
    a5 = {}
    for item in questions:
        results = run_path(instance, item, now)
        a5[item["id"]] = {"calls": len(results), "bytes": sum(result_bytes(r) for r in results)}
    disputed = max(
        result_bytes(reads.run_tool(instance, "fact_get", {"key": key}, now=now))
        for key in disputed_keys(instance, now)
    )
    items, pages = [], []
    for entity in _entities(instance, now):
        result = reads.run_tool(instance, "fact_get", {"key": f"{entity}.*"}, now=now)
        size = result_bytes(result)
        items.append(math.ceil(size / len(result.structured["facts"])))
        pages.append(size)
    out = {
        "a1": {"exampleco": {k: start[k] for k in ("S", "router", "initialize", "tools_list", "brief")}},
        "a2": {"router": start["router"], "brief_text_bytes": start["brief_text_bytes"],
               "brief_lines": start["brief_lines"], "disputed_key_max": disputed,
               "prefix_item_bytes": max(items), "prefix_page_bytes": max(pages)},
        "a3": {"tools_list": start["tools_list"], "tools": start["tools"]},
        "a5": a5,
    }
    if timing:
        worst = ("brief", 0.0)
        paths = [("brief", {"minimal_path": [{"tool": "brief", "arguments": {}}]})]
        paths += [(item["id"], item) for item in questions]
        for name, item in paths:
            value = p95_ms(instance, item, now)
            if value > worst[1]:
                worst = (name, value)
        out["a7"] = {"question": worst[0], "p95_ms": round(worst[1], 3)}
    return out


def fingerprint(budgets: dict) -> str:
    body = {key: value for key, value in budgets.items() if key != "changelog"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench.harness")
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("measure")
    measure.add_argument("--no-timing", action="store_true")
    sub.add_parser("fingerprint")
    ns = parser.parse_args(argv)
    if ns.command == "fingerprint":
        if not BUDGETS.is_file():
            print("bench: bench/budgets.json is missing", file=sys.stderr)
            return 2
        print(fingerprint(json.loads(BUDGETS.read_text(encoding="utf-8"))))
        return 0
    manifest = load_manifest()
    questions = json.loads((DATASET / "questions.json").read_text(encoding="utf-8"))["questions"]
    with tempfile.TemporaryDirectory(prefix="bench-") as parent:
        instance = build_instance(DATASET, Path(parent))
        report = figures(instance, questions, manifest["now"], timing=not ns.no_timing)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
