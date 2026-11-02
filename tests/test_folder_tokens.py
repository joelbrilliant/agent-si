"""Folder-limited hub tokens.

The canary instance holds folders demo (with a subfolder) and secret (with one), each with a row of every filed and
owned kind, plus a source, a person and a thread only secret reaches, a fact, a system, a signal, a held change and a
meeting. Everything hidden carries the word zzsecret in its id or text, and hiddenmark in its text. A token that names
demo is called through every transport and tool, with arguments aimed at the secret rows, and no response may hold
either word. The same calls with a whole-brain token do, so the test can see a leak. Invented data only."""
from __future__ import annotations

import contextlib
import io
import json
import re
import sqlite3
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brain import reads, schemas, scope, store
from brain.applier import Applier
from brain.cli import main
from brain.init import init_instance
from brain_hub import server as hub_server, tokens
from tests.conftest import make_packet, render_config, ro_rows, source_op, upsert_op
from tests.test_error_fixes import check_message

CANARY = "zzsecret"
MARK = "hiddenmark"  # in every hidden text, never sent in a request, so a leak shows even when a query echoes the canary
AGENTS = {
    "tester": {"operations": "*", "author_principal": True},
    "plain": {"operations": "*"},
    "careful": {"operations": "*", "approval": "required", "approvers": ["tester"]},
    "importer": {"operations": "*", "legacy_import": True},
}
FACT_KEYS = [
    {"key": f"{CANARY}.code", "value_type": "text"},
    {"key": f"{CANARY}.seats", "value_type": "int"},
    {"key": "demo.seats", "value_type": "int"},
]
VERIFIED = "2026-10-01T00:00:00Z"
SECRET_SENTENCE = f"The {CANARY} {MARK} launch code is violet seven."


def _apply(instance: Path, agent: str, *ops):
    writer = Applier(instance)
    try:
        outcome = writer.apply(json.dumps(make_packet(agent, *ops)).encode())
    finally:
        writer.close()
    return outcome


def _ok(instance: Path, agent: str, *ops):
    outcome = _apply(instance, agent, *ops)
    assert outcome.receipt["status"] in ("applied", "held"), outcome.receipt
    return outcome


def _project(pid: str, title: str, parent: str, source: str, base=None) -> dict:
    return upsert_op("upsert_project", {"id": pid, "kind": "project", "title": title, "parent_id": parent,
                                        "description": f"{title} work."}, source, base=base)


def build(tmp_path: Path) -> Path:
    """An instance with the demo and secret folders. See the module text for what each holds."""
    instance = Path(init_instance("demo", tmp_path, config_text=render_config(agents=AGENTS, fact_keys=FACT_KEYS))["instance"])
    sentences = {
        "src-demo-notes": "The demo kettle is descaled monthly. Order the demo filters on Friday.",
        "src-shared": "A shared note about the office. The shared planning call is on Tuesday.",
        f"src-{CANARY}-notes": f"{SECRET_SENTENCE} The {CANARY} {MARK} vault is under the stairs.",
        f"src-{CANARY}-extra": f"A second {CANARY} {MARK} note. The {CANARY} {MARK} seats are 7.",
        f"src-{CANARY}-other": f"A rival {CANARY} {MARK} note. The {CANARY} {MARK} seats are 9.",
        f"src-{CANARY}-meeting": f"Transcript of the {CANARY} {MARK} board meeting.",
        f"src-{CANARY}-thread": f"Capture of the {CANARY} {MARK} email thread.",
        "src-demo-thread": "Capture of the demo email thread.",
        "src-demo-meeting": "Transcript of the demo planning call.",
    }
    _ok(instance, "tester",
        *[source_op(sid, text, date="2026-10-01") for sid, text in sentences.items()],
        source_op("src-jade", "We will keep the demo kettle routine as it is.", author="per-jade", date="2026-10-02"),
        source_op(f"src-{CANARY}-jade", f"We will lock the {CANARY} {MARK} vault on Fridays.", author="per-jade", date="2026-10-02"),
        upsert_op("upsert_person", {"id": "per-demo-dana", "display_name": "Dana Demo", "org": "Demo Co"}, "src-demo-notes"),
        upsert_op("upsert_person", {"id": "per-shared-lee", "display_name": "Lee Shared"}, "src-shared"),
        upsert_op("upsert_person", {"id": "per-outside-kim", "display_name": "Kim Outside"}, f"src-{CANARY}-extra"),
        upsert_op("upsert_person", {"id": f"per-{CANARY}-sam", "display_name": f"Sam {CANARY.title()}", "org": f"{CANARY} {MARK} Ltd"},
                  f"src-{CANARY}-notes"))
    _ok(instance, "tester",
        source_op("src-demo-dana", "We will keep the demo kettle routine as it is.", author="per-demo-dana", date="2026-10-02"),
        source_op(f"src-{CANARY}-sam", f"The {CANARY} {MARK} owner is Sam.", author=f"per-{CANARY}-sam", date="2026-10-02"))
    _ok(instance, "tester",
        _project("demo", "Demo folder", "general", "src-demo-notes"),
        _project("demo-sub", "Demo subfolder", "demo", "src-demo-notes"),
        _project(CANARY, f"{CANARY.title()} {MARK} folder", "ops", f"src-{CANARY}-notes"),
        _project(f"{CANARY}-sub", f"{CANARY.title()} {MARK} subfolder", CANARY, f"src-{CANARY}-notes"))
    # Threads: one per folder, plus a calendar thread for each meeting.
    _ok(instance, "tester",
        upsert_op("upsert_thread", {"id": "thr-demo", "channel": "email", "account": "work", "external_id": "t-demo",
                                    "subject": "Demo filters", "last_inbound_at": "2026-10-04T09:00:00Z",
                                    "last_outbound_at": "2026-10-02T09:00:00Z", "outbound_seen": True}, "src-demo-thread"),
        upsert_op("upsert_thread", {"id": f"thr-{CANARY}", "channel": "email", "account": "work", "external_id": "t-s",
                                    "subject": f"The {CANARY} {MARK} vault", "last_inbound_at": "2026-10-04T09:00:00Z",
                                    "last_outbound_at": "2026-10-02T09:00:00Z", "outbound_seen": True}, f"src-{CANARY}-thread"),
        upsert_op("upsert_thread", {"id": "thr-cal-demo", "channel": "calendar", "account": "work-cal", "external_id": "c-d"},
                  "src-demo-meeting"),
        upsert_op("upsert_thread", {"id": f"thr-cal-{CANARY}", "channel": "calendar", "account": "work-cal", "external_id": "c-s"},
                  f"src-{CANARY}-meeting"))
    secret_action = f"act-{CANARY}-1"
    _ok(instance, "tester",
        # Secret rows first, so the demo rows can point at them across folders.
        upsert_op("upsert_record", {"id": f"rec-{CANARY}-1", "kind": "note", "title": f"{CANARY.title()} {MARK} note",
                                    "body": SECRET_SENTENCE, "project_id": CANARY}, f"src-{CANARY}-notes"),
        upsert_op("upsert_record", {"id": f"rec-{CANARY}-sub", "kind": "note", "title": f"{CANARY.title()} {MARK} sub note",
                                    "body": f"The {CANARY} {MARK} vault is under the stairs.", "project_id": f"{CANARY}-sub"},
                  f"src-{CANARY}-notes"),
        # A record that starts in secret and moves to demo: its old version stays in secret's history.
        upsert_op("upsert_record", {"id": "rec-moved", "kind": "note", "title": "Moved note",
                                    "body": SECRET_SENTENCE, "project_id": CANARY}, f"src-{CANARY}-notes"),
        upsert_op("upsert_decision", {"id": f"dec-{CANARY}-1", "title": f"Lock the {CANARY} {MARK} vault",
                                      "quote": f"We will lock the {CANARY} {MARK} vault on Fridays.", "decided_on": "2026-10-02",
                                      "decided_by_person_id": "per-jade", "status": "in_effect", "project_id": CANARY},
                  f"src-{CANARY}-jade"),
        upsert_op("upsert_action", {"id": secret_action, "title": f"Move the {CANARY} {MARK} vault", "status": "waiting",
                                    "status_basis": "observed", "project_id": CANARY, "due_date": "2026-10-01",
                                    "due_kind": "deadline", "owner_person_ids": [f"per-{CANARY}-sam"],
                                    "waiting_on_person_ids": ["per-shared-lee"], "last_verified_at": VERIFIED,
                                    "record_id": f"rec-{CANARY}-1"}, f"src-{CANARY}-notes"),
        upsert_op("upsert_checklist_item", {"id": f"chk-{CANARY}-1", "project_id": CANARY, "title": f"Count the {CANARY} {MARK} keys",
                                            "state": "waiting", "state_basis": "observed", "last_verified_at": VERIFIED,
                                            "owner_person_ids": [f"per-{CANARY}-sam"], "action_id": secret_action},
                  f"src-{CANARY}-notes"),
        upsert_op("upsert_meeting", {"id": f"mtg-{CANARY}", "thread_id": f"thr-cal-{CANARY}", "title": f"{CANARY.title()} {MARK} board",
                                     "status": "confirmed", "starts_at": "2026-10-08T01:00:00Z", "ends_at": "2026-10-08T02:00:00Z",
                                     "project_id": CANARY, "transcript_source_id": f"src-{CANARY}-meeting"},
                  f"src-{CANARY}-meeting"),
        upsert_op("upsert_thread_link", {"id": f"lnk-{CANARY}", "action_id": secret_action, "thread_id": f"thr-{CANARY}",
                                         "role": "awaiting_reply", "since": "2026-10-02T09:00:00Z"}, f"src-{CANARY}-notes"),
        upsert_op("upsert_signal", {"id": f"sig-{CANARY}-reply", "kind": "reply", "subject_table": "actions",
                                    "subject_id": secret_action, "thread_id": f"thr-{CANARY}", "basis": "observed"},
                  f"src-{CANARY}-notes"),
        upsert_op("upsert_signal", {"id": f"sig-{CANARY}-note", "kind": "other", "subject_table": "records",
                                    "subject_id": f"rec-{CANARY}-1", "detail": f"A {CANARY} {MARK} follow-up.", "basis": "inferred"},
                  f"src-{CANARY}-notes"),
        upsert_op("upsert_system", {"id": f"sys-{CANARY}", "name": f"{CANARY} {MARK} vault", "kind": "saas",
                                    "vendor": f"{CANARY} {MARK} Inc", "owner_person_id": f"per-{CANARY}-sam"}, f"src-{CANARY}-notes"),
        upsert_op("upsert_fact", {"key": f"{CANARY}.code", "value": "violet seven", "quote": "launch code is violet seven",
                                  "value_raw": "violet seven", "basis": "observed", "as_of": "2026-10-01",
                                  "last_verified_at": VERIFIED}, f"src-{CANARY}-notes"),
        {"op": "add_claim", "row": {"id": f"clm-{CANARY}-a", "key": f"{CANARY}.seats", "value_raw": "7", "basis": "reported",
                                    "quote": f"The {CANARY} {MARK} seats are 7"},
         "source": {"source_id": f"src-{CANARY}-extra", "locator": "para 1"}},
        {"op": "add_claim", "row": {"id": f"clm-{CANARY}-b", "key": f"{CANARY}.seats", "value_raw": "9", "basis": "reported",
                                    "quote": f"The {CANARY} {MARK} seats are 9"},
         "source": {"source_id": f"src-{CANARY}-other", "locator": "para 1"}})
    _ok(instance, "tester",
        upsert_op("upsert_record", {"id": "rec-moved", "project_id": "demo", "body": "The demo kettle is descaled monthly."},
                  "src-demo-notes", base=1))
    # An unchecked record in each folder, from an importer.
    _ok(instance, "importer",
        upsert_op("upsert_record", {"id": f"rec-{CANARY}-unchecked", "kind": "note", "title": f"{CANARY.title()} {MARK} loose note",
                                    "body": f'Notes say "the {CANARY} {MARK} safe is open".', "project_id": CANARY},
                  f"src-{CANARY}-notes"),
        upsert_op("upsert_record", {"id": "rec-demo-unchecked", "kind": "note", "title": "Demo loose note",
                                    "body": 'Notes say "the demo kettle is broken".', "project_id": "demo"}, "src-demo-notes"))
    demo_action = "act-demo-1"
    _ok(instance, "tester",
        upsert_op("upsert_record", {"id": "rec-demo-1", "kind": "note", "title": "Demo kettle note",
                                    "body": "The demo kettle is descaled monthly.", "project_id": "demo"}, "src-demo-notes"),
        upsert_op("upsert_record", {"id": "rec-demo-sub", "kind": "note", "title": "Demo filters note",
                                    "body": "Order the demo filters on Friday.", "project_id": "demo-sub"}, "src-demo-notes"),
        upsert_op("upsert_decision", {"id": "dec-demo-1", "title": "Keep the demo kettle routine",
                                      "quote": "We will keep the demo kettle routine as it is.", "decided_on": "2026-10-02",
                                      "decided_by_person_id": "per-jade", "status": "in_effect", "project_id": "demo"},
                  "src-jade"),
        upsert_op("upsert_action", {"id": demo_action, "title": "Order the demo filters", "status": "waiting",
                                    "status_basis": "observed", "project_id": "demo", "due_date": "2026-10-01",
                                    "due_kind": "deadline", "owner_person_ids": ["per-demo-dana"],
                                    "waiting_on_person_ids": ["per-shared-lee", "per-outside-kim"], "last_verified_at": VERIFIED,
                                    "blocked_by": secret_action, "record_id": f"rec-{CANARY}-1"}, "src-demo-notes"),
        upsert_op("upsert_checklist_item", {"id": "chk-demo-1", "project_id": "demo-sub", "title": "Count the demo filters",
                                            "state": "waiting", "state_basis": "observed", "last_verified_at": VERIFIED,
                                            "owner_person_ids": ["per-demo-dana"], "action_id": secret_action},
                  "src-demo-notes"),
        upsert_op("upsert_meeting", {"id": "mtg-demo", "thread_id": "thr-cal-demo", "title": "Demo planning",
                                     "status": "confirmed", "starts_at": "2026-10-08T03:00:00Z", "ends_at": "2026-10-08T04:00:00Z",
                                     "project_id": "demo", "transcript_source_id": "src-demo-meeting"}, "src-demo-meeting"),
        upsert_op("upsert_thread_link", {"id": "lnk-demo", "action_id": demo_action, "thread_id": "thr-demo",
                                         "role": "awaiting_reply", "since": "2026-10-02T09:00:00Z"}, "src-demo-notes"),
        upsert_op("upsert_signal", {"id": "sig-demo-reply", "kind": "reply", "subject_table": "actions",
                                    "subject_id": demo_action, "thread_id": "thr-demo", "basis": "observed"}, "src-demo-notes"),
        upsert_op("upsert_signal", {"id": "sig-demo-note", "kind": "other", "subject_table": "records", "subject_id": "rec-demo-1",
                                    "detail": "Needs a second reader.", "basis": "inferred"}, "src-demo-notes"))
    # Links: the demo and secret records each cite a second source; the shared source is cited from both folders.
    _ok(instance, "tester",
        {**upsert_op("upsert_record", {"id": "rec-demo-1", "title": "Demo kettle note"}, "src-demo-notes", base=1),
         "sources": [{"source_id": "src-shared", "locator": "p1"}]},
        {**upsert_op("upsert_record", {"id": f"rec-{CANARY}-1", "title": f"{CANARY.title()} {MARK} note"}, f"src-{CANARY}-notes", base=1),
         "sources": [{"source_id": "src-shared", "locator": "p1"}, {"source_id": f"src-{CANARY}-extra", "locator": "p1"}]})
    # A change held for approval, whose packet carries the canary.
    held = _apply(instance, "careful", source_op(f"src-{CANARY}-held", f"A held {CANARY} {MARK} note.", date="2026-10-03"))
    assert held.receipt["status"] == "held", held.receipt
    return instance


# -- The hub ------------------------------------------------------------------------------------------------------


class Door:
    """A hub on loopback over the canary instance, with a token of each kind."""

    def __init__(self, inst: Path):
        self.inst = inst
        self.app = hub_server.Hub(inst)
        self.httpd = self.app.serve("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.demo = tokens.issue(inst, "plain", "read", "Cloud agent, demo only", folders=["demo"])
        self.whole = tokens.issue(inst, "plain", "read", "Whole-brain reader")
        self.writer = tokens.issue(inst, "tester", "read write", "Whole-brain writer")

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method: str, path: str, body=None, token: str | None = None, headers: dict | None = None):
        data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
        if method == "POST" and data is None:
            data = b""
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers or {})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()


@pytest.fixture(scope="module")
def door(tmp_path_factory):
    inst = build(tmp_path_factory.mktemp("canary"))
    # The canary sweep makes several hundred reads: lift the per-minute read limit for it.
    saved = hub_server.LIMITS["read"]
    hub_server.LIMITS["read"] = (1_000_000, 60)
    opened = Door(inst)
    try:
        yield opened
    finally:
        opened.stop()
        hub_server.LIMITS["read"] = saved


def _all_ids(inst: Path) -> dict[str, list[str]]:
    """Every row id of every entity the read tools name, whichever folder it is in."""
    tables = {**schemas.ENTITY_TABLE, "fact": "fact_keys", "claim": "claims"}
    con = sqlite3.connect(inst / "brain.sqlite3")
    try:
        return {
            entity: [row[0] for row in con.execute(f"SELECT {'key' if entity == 'fact' else 'id'} FROM {table} ORDER BY 1")]
            for entity, table in tables.items()
        }
    finally:
        con.close()


def _tool_enum(tool: str, name: str) -> list[str]:
    return list(reads._TOOLS[tool]["inputSchema"]["properties"][name]["enum"])


def _catalog(inst: Path) -> list[tuple[str, dict]]:
    """Every read tool with no arguments, and with arguments aimed at the secret rows."""
    calls: list[tuple[str, dict]] = [(tool, {}) for tool in reads.TOOL_NAMES]
    ids = _all_ids(inst)
    words = [CANARY, MARK, "violet", "vault safe keys", f"Sam {CANARY}", f"{CANARY}*", "launch code"]
    entities = _tool_enum_items("search", "entities")
    for word in words:
        calls.append(("search", {"query": word}))
        calls.append(("search", {"query": word, "include_closed": True, "limit": 25}))
        calls += [("search", {"query": word, "entities": [entity]}) for entity in entities]
        calls += [("search", {"query": word, "under": folder}) for folder in (CANARY, f"{CANARY}-sub", "demo")]
    for entity in _tool_enum("get", "entity"):
        for row_id in ids[entity]:
            calls.append(("get", {"entity": entity, "id": row_id}))
            if entity in ("record", "fact"):
                calls.append(("get", {"entity": entity, "id": row_id, "include_history": True}))
            if entity in ("record", "source"):
                calls.append(("get", {"entity": entity, "id": row_id, "field": "body" if entity == "record" else "content"}))
    links = set(schemas.LINK_TABLES)
    for entity in _tool_enum("records_query", "entity"):
        calls.append(("records_query", {"entity": entity, "limit": 100}))
        calls.append(("records_query", {"entity": entity, "limit": 100, "format": "rows"}))
        calls.append(("records_query", {"entity": entity, "filters": {"id": {"contains": CANARY}}}))
        calls.append(("records_query", {"entity": entity, "filters": {"project_id": {"under": CANARY}}}))
        calls.append(("records_query", {"entity": entity, "filters": {"unchecked": True}}))
        names = [field.name for field in reads._ENTITIES[entity]["fields"]]
        calls.append(("records_query", {"entity": entity, "fields": names[:40], "limit": 100}))
        if schemas.ENTITY_TABLE.get(entity) in links:
            calls.append(("records_query", {"entity": entity, "filters": {"cites": f"src-{CANARY}-notes"}}))
            calls.append(("records_query", {"entity": entity, "filters": {"cites": "src-shared"}}))
    calls.append(("records_query", {"entity": "project", "filters": {"id": {"under": CANARY}}}))
    calls.append(("records_query", {"entity": "project", "filters": {"id": {"under": "general"}}}))
    calls.append(("signals", {"limit": 100}))
    for kind in _tool_enum_items("signals", "kinds"):
        calls.append(("signals", {"kinds": [kind], "limit": 100}))
    calls.append(("signals", {"project_id": CANARY}))
    for entity in _tool_enum_subject():
        for row_id in ids[entity]:
            calls.append(("signals", {"subject": {"entity": entity, "id": row_id}}))
    for folder in ("demo", "demo-sub", CANARY, f"{CANARY}-sub", "general", "ops"):
        calls.append(("brief", {"under": folder}))
    for key in (f"{CANARY}.code", f"{CANARY}.seats", "demo.seats", f"{CANARY}.*", f"{CANARY} code", "demo.*"):
        calls.append(("fact_get", {"key": key}))
        calls.append(("fact_get", {"key": key, "claims": "all", "include_retired": True}))
    return calls


def _tool_enum_items(tool: str, name: str) -> list[str]:
    return list(reads._TOOLS[tool]["inputSchema"]["properties"][name]["items"]["enum"])


def _tool_enum_subject() -> list[str]:
    return list(reads._TOOLS["signals"]["inputSchema"]["properties"]["subject"]["properties"]["entity"]["enum"])


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _payloads(path: str, status: int, headers: dict, body: bytes, sent: dict) -> list[str]:
    """The text a response shows, ready to scan. A refusal echoes the id or key the caller sent, and a search echoes its
    own query, which says nothing about a hidden row; those echoes are taken out, and nothing else is."""
    texts: list[tuple[str, bool]] = []
    if path == "/mcp":
        message = json.loads(body)
        if "error" in message:
            texts.append((json.dumps(message["error"]), True))
        result = message.get("result", {})
        for item in result.get("content", []):
            texts.append((item["text"], bool(result.get("isError"))))
        if "structuredContent" in result:
            texts.append((json.dumps(result["structuredContent"]), bool(result.get("isError"))))
        for key in ("instructions", "serverInfo", "tools", "protocolVersion", "capabilities"):
            if key in result:
                texts.append((json.dumps(result[key]), False))
    else:
        texts.append((body.decode(), headers.get("Brain-Exit-Code") == "1" or status >= 400))
    shown = []
    for text, is_error in texts:
        if is_error:
            for value in _strings(sent):
                text = text.replace(value, "<sent>").replace(json.dumps(value)[1:-1], "<sent>")
        else:
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and "hits" in parsed:
                parsed.pop("query", None)
                parsed.pop("match", None)
                text = json.dumps(parsed)
        shown.append(text)
    return shown


def _sweep(door: Door, token: str, calls) -> list[tuple[str, str, dict, list[str]]]:
    """Run every call through /mcp and /v1/read: (transport, tool, arguments, texts shown)."""
    seen = []
    for number, (tool, arguments) in enumerate(calls):
        status, headers, body = door.request(
            "POST", "/mcp", {"jsonrpc": "2.0", "id": number, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            token=token, headers={"MCP-Protocol-Version": "2025-11-25"})
        assert status == 200, (tool, arguments, status, body[:200])
        seen.append(("/mcp", tool, arguments, _payloads("/mcp", status, headers, body, arguments)))
        status, headers, body = door.request("POST", f"/v1/read/{tool}", arguments, token=token)
        assert status == 200, (tool, arguments, status, body[:200])
        seen.append((f"/v1/read/{tool}", tool, arguments, _payloads("read", status, headers, body, arguments)))
    for number, method in enumerate(("initialize", "tools/list", "ping")):
        params = {"protocolVersion": "2025-11-25"} if method == "initialize" else {}
        status, headers, body = door.request("POST", "/mcp", {"jsonrpc": "2.0", "id": number, "method": method, "params": params},
                                             token=token, headers={"MCP-Protocol-Version": "2025-11-25"})
        assert status == 200, (method, status)
        seen.append(("/mcp", method, params, _payloads("/mcp", status, headers, body, params)))
    return seen


def _leaks(seen) -> list[tuple[str, str, dict]]:
    return [
        (transport, tool, arguments)
        for transport, tool, arguments, texts in seen
        if any(CANARY in text.lower() or MARK in text.lower() for text in texts)
    ]


@pytest.fixture(scope="module")
def demo_sweep(door):
    return _sweep(door, door.demo, _catalog(door.inst))


@pytest.fixture(scope="module")
def whole_sweep(door):
    return _sweep(door, door.whole, _catalog(door.inst))


# -- 1. The canary ----------------------------------------------------------------------------------------------


def test_no_response_to_the_demo_token_holds_the_canary(demo_sweep) -> None:
    assert len(demo_sweep) > 500, "the sweep is too small to prove anything"
    leaks = _leaks(demo_sweep)
    assert not leaks, f"{len(leaks)} calls showed hidden text; the first are {leaks[:5]}"


def test_a_whole_brain_token_does_see_the_canary_so_the_sweep_can_see_a_leak(whole_sweep) -> None:
    leaking = {tool for _transport, tool, _arguments in _leaks(whole_sweep)}
    assert {"brief", "search", "get", "records_query", "signals", "fact_get"} <= leaking
    for transport in ("/mcp", "/v1/read/search"):
        assert any(CANARY in " ".join(texts).lower() for t, tool, _a, texts in whole_sweep if t == transport and tool == "search")


def test_every_hub_route_that_shows_records_is_closed_to_the_demo_token(door) -> None:
    for token, expect in ((door.demo, 403), (door.whole, 200)):
        for path in ("/v1/held?limit=50", f"/v1/receipts/{door.held_change_id()}"):
            status, _headers, body = door.request("GET", path, token=token)
            assert status == expect, (path, status)
            if expect == 403:
                assert CANARY not in body.decode() and MARK not in body.decode()
                check_message(path, json.loads(body)["error"], ["whole-brain token", "--folder"])
            else:
                assert door.held_change_id() in body.decode()


def _held_change_id(self) -> str:
    return ro_rows(self.inst, "SELECT change_id FROM write_log WHERE status = 'held'")[0][0]


Door.held_change_id = _held_change_id


def test_a_hidden_id_gets_the_not_found_of_an_id_that_does_not_exist(door) -> None:
    hidden = {"record": f"rec-{CANARY}-1", "action": f"act-{CANARY}-1", "source": f"src-{CANARY}-notes",
              "person": f"per-{CANARY}-sam", "thread": f"thr-{CANARY}", "project": CANARY, "decision": f"dec-{CANARY}-1",
              "checklist_item": f"chk-{CANARY}-1", "meeting": f"mtg-{CANARY}", "signal": f"sig-{CANARY}-reply",
              "thread_link": f"lnk-{CANARY}"}
    for entity, row_id in hidden.items():
        answers = []
        for asked in (row_id, "nothing-here"):
            status, _h, body = door.request("POST", "/v1/read/get", {"entity": entity, "id": asked}, token=door.demo)
            assert status == 200
            answers.append(body.decode().replace(asked, "<id>"))
        assert answers[0] == answers[1], entity
        assert "no " + entity in answers[0]
    for tool, arguments in (("brief", {"under": CANARY}), ("search", {"query": "kettle", "under": CANARY})):
        answers = [
            door.request("POST", f"/v1/read/{tool}", {**arguments, "under": asked}, token=door.demo)[2].decode().replace(asked, "<id>")
            for asked in (CANARY, "nothing-here")
        ]
        assert answers[0] == answers[1] and "no project" in answers[0], tool


# -- 2. The folder still answers ---------------------------------------------------------------------------------


def _read(door: Door, tool: str, token: str | None = None, **arguments) -> dict | str:
    status, headers, body = door.request("POST", f"/v1/read/{tool}", arguments, token=token or door.demo)
    assert status == 200, body
    text = body.decode()
    return text if tool == "brief" else json.loads(text)


def test_the_demo_token_reads_its_folder_and_the_folder_inside_it(door) -> None:
    found = _read(door, "search", query="kettle filters")
    assert {hit["id"] for hit in found["hits"]} >= {"rec-demo-1", "rec-demo-sub", "act-demo-1", "chk-demo-1"}
    projects = _read(door, "records_query", entity="project")
    assert [row["id"] for row in projects["rows"]] == ["demo", "demo-sub"]
    records = _read(door, "records_query", entity="record", limit=100)
    assert {row["id"] for row in records["rows"]} == {"rec-demo-1", "rec-demo-sub", "rec-moved", "rec-demo-unchecked"}
    action = _read(door, "get", entity="action", id="act-demo-1")
    assert action["row"]["owner_person_ids"] == ["per-demo-dana"]
    assert [item["id"] for item in action["related"]["sources"]["items"]] == ["src-demo-notes"]
    people = {row["id"]: row["display_name"] for row in _read(door, "records_query", entity="person")["rows"]}
    assert people["per-demo-dana"] == "Dana Demo" and people["per-shared-lee"] == "Lee Shared"
    assert f"per-{CANARY}-sam" not in people
    record = _read(door, "get", entity="record", id="rec-demo-1")
    assert [item["id"] for item in record["related"]["sources"]["items"]] == ["src-demo-notes", "src-shared"]
    meeting = _read(door, "get", entity="meeting", id="mtg-demo")
    assert meeting["related"]["transcript"]["id"] == "src-demo-meeting" and meeting["related"]["thread"]["id"] == "thr-cal-demo"
    sources = {row["id"] for row in _read(door, "records_query", entity="source", limit=100)["rows"]}
    assert "src-demo-notes" in sources and "src-shared" in sources and f"src-{CANARY}-notes" not in sources
    signals = _read(door, "signals")
    assert {item["project_id"] for item in signals["signals"]} == {"demo"} and signals["counts"]["reply"] == 1


def test_a_record_moved_into_the_folder_leaves_its_old_versions_behind(door) -> None:
    moved = _read(door, "get", entity="record", id="rec-moved", include_history=True)
    assert moved["row"]["project_id"] == "demo"
    assert [item["version"] for item in moved["history"]["items"]] == [2]
    whole = _read(door, "get", entity="record", id="rec-moved", token=door.whole, include_history=True)
    assert [item["version"] for item in whole["history"]["items"]] == [2, 1]


def test_a_column_naming_a_hidden_row_reads_as_empty(door) -> None:
    action = _read(door, "get", entity="action", id="act-demo-1")["row"]
    assert action["blocked_by"] is None and action["record_id"] is None
    assert _read(door, "get", entity="action", id="act-demo-1", token=door.whole)["row"]["blocked_by"] == f"act-{CANARY}-1"
    assert _read(door, "get", entity="checklist_item", id="chk-demo-1")["row"]["action_id"] is None
    top = _read(door, "get", entity="project", id="demo")
    assert top["row"]["parent_id"] is None and top["related"]["folder"]["path"] == []
    sub = _read(door, "get", entity="project", id="demo-sub")
    assert sub["row"]["parent_id"] == "demo" and sub["related"]["folder"]["path"] == ["demo"]
    shared = _read(door, "get", entity="person", id="per-shared-lee")
    assert shared["related"]["counts"]["actions_waiting_on"] == 1
    whole = _read(door, "get", entity="person", id="per-shared-lee", token=door.whole)
    assert whole["related"]["counts"]["actions_waiting_on"] == 2
    assert _read(door, "get", entity="source", id="src-shared")["related"]["cited_by"]["total"] == 1
    assert _read(door, "get", entity="source", id="src-shared", token=door.whole)["related"]["cited_by"]["total"] == 2


def test_a_person_whose_own_source_is_hidden_reads_without_a_source_or_locator(door) -> None:
    kim = _read(door, "get", entity="person", id="per-outside-kim")
    assert kim["row"]["display_name"] == "Kim Outside"
    assert kim["source"] is None and kim["row"]["source_id"] is None and kim["row"]["locator"] is None
    whole = _read(door, "get", entity="person", id="per-outside-kim", token=door.whole)
    assert whole["row"]["source_id"] == f"src-{CANARY}-extra" and whole["source"]["id"] == f"src-{CANARY}-extra"
    rows = _read(door, "records_query", entity="person", fields=["id", "source_id", "locator"])["rows"]
    assert {row["id"]: row["source_id"] for row in rows}["per-outside-kim"] is None


# -- The scoped brief and the handshake ---------------------------------------------------------------------------


def test_the_scoped_brief_keeps_its_folder_lines_and_drops_the_brain_wide_ones(door) -> None:
    text = _read(door, "brief")
    lines = text.splitlines()
    assert lines[1] == "Folders: demo (Demo folder), with 1 folder inside. This token reads only these folders; facts aren't shown."
    for kept in ("Actions:", "Checklist:", "Signals:", "Due by", "Waiting:", "Active projects:", "Unchecked text: 1 row"):
        assert any(line.startswith(kept) for line in lines), kept
    for dropped in ("Facts:", "Disputed", "Search index", "Held", "Intake", "stale", "Last change", "Update"):
        assert not any(dropped in line for line in lines), dropped
    whole = _read(door, "brief", token=door.whole)
    assert "Facts:" in whole and "Last change:" in whole and "Folders:" not in whole
    under = _read(door, "brief", under="demo-sub")
    assert under.splitlines()[2].startswith("Folder demo-sub:") and "facts are brain-wide" not in under


def test_the_handshake_names_the_folders_and_not_fact_get(door) -> None:
    status, _headers, body = door.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                           "params": {"protocolVersion": "2025-11-25"}}, token=door.demo)
    instructions = json.loads(body)["result"]["instructions"]
    assert instructions.startswith("Read-only tools for the demo brain, limited to the folders demo.")
    assert "fact_get" not in instructions and "search and records_query" in instructions
    assert "Folders: demo (Demo folder)" in instructions
    status, _headers, body = door.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                           "params": {"protocolVersion": "2025-11-25"}}, token=door.whole)
    assert "Use fact_get for facts" in json.loads(body)["result"]["instructions"]


def test_tools_list_is_the_same_for_every_token(door) -> None:
    lists = [door.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, token=token)[2]
             for token in (door.demo, door.whole)]
    assert lists[0] == lists[1]


# -- Refusals ---------------------------------------------------------------------------------------------------


REFUSALS = [
    ("fact_get", "fact_get", {"key": "demo.seats"}),
    ("get on a fact", "get", {"entity": "fact", "id": "demo.seats"}),
    ("get on a system", "get", {"entity": "system", "id": f"sys-{CANARY}"}),
    ("get on a claim", "get", {"entity": "claim", "id": f"clm-{CANARY}-a"}),
    ("records_query on facts", "records_query", {"entity": "fact"}),
    ("records_query on systems", "records_query", {"entity": "system"}),
]


@pytest.mark.parametrize(("name", "tool", "arguments"), REFUSALS, ids=[row[0] for row in REFUSALS])
def test_a_folder_token_refusal_names_its_fix(door, name, tool, arguments) -> None:
    status, headers, body = door.request("POST", f"/v1/read/{tool}", arguments, token=door.demo)
    assert (status, headers["Brain-Exit-Code"]) == (200, "1")
    error = json.loads(body)["error"]
    assert error["code"] == "folder_token"
    check_message(name, error["message"], ["only the folders demo", "search", "records_query", "entity record"])
    assert CANARY not in body.decode()


def test_the_refusal_is_the_same_through_the_mcp_server(door) -> None:
    status, _headers, body = door.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                           "params": {"name": "fact_get", "arguments": {"key": "demo.seats"}}},
                                          token=door.demo)
    result = json.loads(body)["result"]
    assert result["isError"] and "only the folders demo" in result["content"][0]["text"]


# -- Tokens and the hub -------------------------------------------------------------------------------------------


def test_whoami_and_the_token_list_show_the_folders(door) -> None:
    who = json.loads(door.request("GET", "/v1/whoami", token=door.demo)[2])
    assert who["folders"] == ["demo"]
    assert json.loads(door.request("GET", "/v1/whoami", token=door.whole)[2])["folders"] is None
    listed = {row["label"]: row["folders"] for row in tokens.listing(door.inst)}
    assert listed["Cloud agent, demo only"] == ["demo"] and listed["Whole-brain reader"] is None


def test_a_folder_token_cannot_write(door) -> None:
    packet = make_packet("plain", source_op("src-blocked", "A note."))
    assert door.request("POST", "/v1/apply", packet, token=door.demo)[0] == 403


def test_issue_refuses_write_scope_and_a_folder_that_does_not_exist(tmp_path) -> None:
    inst = build(tmp_path)
    with pytest.raises(tokens.TokenError) as caught:
        tokens.issue(inst, "tester", "read write", "Writer", folders=["demo"])
    check_message("--folder with write scope", str(caught.value), ["--scope read"])
    with pytest.raises(tokens.TokenError) as caught:
        tokens.issue(inst, "plain", "read", "Reader", folders=["nowhere"])
    check_message("a folder that doesn't exist", str(caught.value), ["nowhere", "records_query on project"])
    assert tokens.listing(inst) == []


def test_the_cli_issues_a_folder_token_and_audits_the_folders(tmp_path) -> None:
    inst = build(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["hub", "token", "--instance", str(inst), "issue", "--agent", "plain", "--scope", "read",
                     "--folder", "demo", "--folder", "demo-sub", "--label", "Two folders"])
    assert code == 0 and out.getvalue().startswith("abt_")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
        assert main(["hub", "token", "--instance", str(inst), "issue", "--agent", "tester", "--scope", "write",
                     "--folder", "demo", "--label", "Nope"]) == 2
    assert "folder tokens read only in v1; use --scope read" in err.getvalue()
    listing = io.StringIO()
    with contextlib.redirect_stdout(listing):
        assert main(["hub", "token", "--instance", str(inst), "list"]) == 0
    assert json.loads(listing.getvalue())["tokens"][0]["folders"] == ["demo", "demo-sub"]
    issued = [row for row in tokens.audit_rows(inst, "2000-01-01") if row["action"] == "token_issued"]
    assert issued[-1]["detail"] == "scope read; folders demo, demo-sub"


def test_a_hub_made_before_folders_gets_the_column_and_its_tokens_stay_whole_brain(tmp_path) -> None:
    inst = build(tmp_path)
    old = sqlite3.connect(inst / "hub.sqlite3")
    old.executescript(
        "CREATE TABLE tokens (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL,"
        " scope TEXT NOT NULL CHECK (scope IN ('read', 'read write')), label TEXT NOT NULL, issued_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL, revoked_at TEXT) STRICT;")
    old.close()
    con = tokens.connect(inst)
    try:
        assert "folders" in {row[1] for row in con.execute("PRAGMA table_info(tokens)")}
        con.execute("INSERT INTO tokens (id, sha256, agent, kind, scope, label, issued_at, expires_at) VALUES "
                    "('aaaaaaaaaaaa', 'x', 'plain', 'agent', 'read', 'Old token', '2026-01-01T00:00:00Z', '2099-01-01T00:00:00Z')")
    finally:
        con.close()
    assert tokens.listing(inst)[0]["folders"] is None
    again = tokens.connect(inst)  # a second connect finds the column and leaves it alone
    again.close()
    fresh = tokens.issue(inst, "plain", "read", "New reader", folders=["demo"])
    assert tokens.check(inst, f"Bearer {fresh}", __import__("brain").config.load_config(inst / "brain.toml")).folders == ("demo",)


def test_a_token_with_two_folders_reads_both_branches(door) -> None:
    both = tokens.issue(door.inst, "plain", "read", "Sub folders", folders=["demo-sub", f"{CANARY}-sub"])
    rows = _read(door, "records_query", token=both, entity="record", limit=100)["rows"]
    assert {row["id"] for row in rows} == {"rec-demo-sub", f"rec-{CANARY}-sub"}
    assert _read(door, "brief", token=both).splitlines()[1].startswith("Folders: demo-sub (Demo subfolder), zzsecret-sub (")


# -- The choke point ----------------------------------------------------------------------------------------------


def test_the_scoped_connection_can_read_the_views_and_cannot_write(tmp_path) -> None:
    inst = build(tmp_path)
    con = scope.reader(("demo",))(inst / "brain.sqlite3")
    try:
        assert con.execute("PRAGMA query_only").fetchone()[0] == 1 and con.in_transaction
        assert con.execute("SELECT count(*) FROM projects").fetchone()[0] == 2
        assert con.execute("SELECT count(*) FROM main.projects").fetchone()[0] > 2
        assert con.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        for statement in ("CREATE TEMP VIEW leak AS SELECT * FROM main.facts", "DELETE FROM main.records",
                          "UPDATE main.projects SET title = 'x'"):
            with pytest.raises(sqlite3.Error):
                con.execute(statement)
    finally:
        con.close()


def test_a_folder_that_gets_a_new_folder_inside_it_includes_it_on_the_next_request(door, applier=None) -> None:
    writer = Applier(door.inst)
    try:
        packet = make_packet("tester", source_op("src-late", "Late folder notes."),
                             upsert_op("upsert_project", {"id": "demo-late", "kind": "project", "title": "Late folder",
                                                          "parent_id": "demo"}, "src-late"))
        assert writer.apply(json.dumps(packet).encode()).receipt["status"] == "applied"
    finally:
        writer.close()
    assert [row["id"] for row in _read(door, "records_query", entity="project")["rows"]] == ["demo", "demo-late", "demo-sub"]


def test_the_brief_for_a_token_without_folders_is_unchanged(door) -> None:
    assert _read(door, "brief", token=door.whole) == reads.run_tool(door.inst, "brief", {}).text + "\n"


# -- The failed-sign-in limit --------------------------------------------------------------------------


def test_a_valid_token_is_never_refused_by_the_failed_sign_in_limit(tmp_path) -> None:
    inst = build(tmp_path)
    opened = Door(inst)
    try:
        before = len([row for row in tokens.audit_rows(inst, "2000-01-01") if row["action"] == "auth_failed"])
        statuses = [opened.request("GET", "/v1/whoami", token=f"abt_{'0' * 12}_{'x' * 43}")[0] for _ in range(25)]
        assert statuses[:20] == [401] * 20 and statuses[20:] == [429] * 5
        assert opened.request("GET", "/v1/whoami", token=opened.demo)[0] == 200
        assert opened.request("POST", "/v1/read/brief", {}, token=opened.whole)[0] == 200
        assert opened.request("GET", "/v1/whoami", token=f"abt_{'0' * 12}_{'x' * 43}")[0] == 429
        after = len([row for row in tokens.audit_rows(inst, "2000-01-01") if row["action"] == "auth_failed"])
        assert after - before == 20
    finally:
        opened.stop()
