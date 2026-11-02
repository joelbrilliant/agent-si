"""The team hub: one instance for one company, served over HTTP behind Caddy, which
handles TLS. Standard library only. Every request but the health checks needs a token, which decides the agent.

Responses for writes are byte-identical to `brain apply` output, with the exit code in Brain-Exit-Code and mirrored
in the HTTP status. Logs carry the route template, status, token id and duration only: never bodies, arguments,
quotes or change ids.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from brain import advice, config as config_mod, ingress, reads, server as mcp, store, usage
from brain.applier import Applier, NotAllowed, Unavailable, UnknownChange, Unreadable
from brain_hub import tokens

HTTP_FOR_EXIT = {0: 200, 1: 422, 2: 400, 3: 409, 4: 202, 5: 503}
READ_BODY = 64 * 1024
APPLY_BODY = 5 * 1024 * 1024
HANDLERS = 16
DRAIN = 16 * 1024 * 1024  # an oversized body is read and dropped up to this, then the connection closes
# Rate limits, per minute unless named: failed sign-ins per client address, reads and writes per
# token, and rejected writes per token per hour.
LIMITS = {"auth_failed": (20, 300), "read": (600, 60), "write": (120, 60), "rejected": (60, 3600)}
DEFAULT_MCP = "2025-03-26"


class _Limiter:
    def __init__(self):
        self.events: dict[tuple[str, str], deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def over(self, kind: str, key: str, add: bool = True) -> bool:
        count, window = LIMITS[kind]
        now = time.monotonic()
        with self.lock:
            queue = self.events[(kind, key)]
            while queue and now - queue[0] > window:
                queue.popleft()
            if len(queue) >= count:
                return True
            if add:
                queue.append(now)
            return False

    def note(self, kind: str, key: str) -> None:
        self.over(kind, key, add=True)


class Hub:
    def __init__(self, instance, *, origins: tuple[str, ...] = ()):
        self.instance = Path(instance).resolve()
        self.cfg = config_mod.load_config(self.instance / "brain.toml")
        self.origins = origins
        self.write_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(HANDLERS)
        self.limits = _Limiter()
        self.ready = not store.check_instance(self.instance)
        # The stream the hub started with: a later redirect of sys.stdout elsewhere in the process (a test, an
        # embedding tool) never mixes its log lines into someone else's output.
        self.log_stream = sys.stdout

    def serve(self, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
        hub = self

        class Handler(_Handler):
            pass

        Handler.hub = hub
        httpd = ThreadingHTTPServer((host, port), Handler)
        httpd.daemon_threads = True
        return httpd


def health_only(host: str, port: int) -> ThreadingHTTPServer:
    """No brain yet: /healthz answers so the container is alive, and everything else is 503 until init or restore."""

    class Handler(BaseHTTPRequestHandler):
        server_version = "agent-si-hub"
        sys_version = ""

        def log_message(self, format, *args):  # noqa: A002
            pass

        def do_GET(self):  # noqa: N802
            ok = self.path == "/healthz"
            body = json.dumps({"status": "ok"} if ok else {"error": "no brain: run brain init or brain restore"}).encode()
            self.send_response(200 if ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET  # noqa: N815

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


ROUTES = [
    ("POST", re.compile(r"/mcp"), "/mcp", "read", "_mcp"),
    ("POST", re.compile(r"/v1/apply"), "/v1/apply", "write", "_apply"),
    ("POST", re.compile(r"/v1/extract"), "/v1/extract", "write", "_extract"),
    ("POST", re.compile(r"/v1/validate"), "/v1/validate", "write", "_validate"),
    ("POST", re.compile(r"/v1/changes/([^/]{1,200})/(approve|refuse)"), "/v1/changes/{id}/{decision}", "write", "_review"),
    ("GET", re.compile(r"/v1/receipts/([^/]{1,200})"), "/v1/receipts/{id}", "read", "_receipt"),
    ("GET", re.compile(r"/v1/held"), "/v1/held", "read", "_held"),
    ("POST", re.compile(r"/v1/read/([a-z_]{1,40})"), "/v1/read/{tool}", "read", "_read"),
    ("GET", re.compile(r"/v1/whoami"), "/v1/whoami", "any", "_whoami"),
    ("GET", re.compile(r"/healthz"), "/healthz", "none", "_healthz"),
    ("GET", re.compile(r"/readyz"), "/readyz", "none", "_readyz"),
]


class _Handler(BaseHTTPRequestHandler):
    hub: Hub
    body_read = False
    server_version = "agent-si-hub"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30

    # -- plumbing ------------------------------------------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler's name
        pass  # every line is written by _log, which holds no content

    def _log(self, template: str, status: int, token_id: str, started: float) -> None:
        stream = self.hub.log_stream
        stream.write(f"hub {self.command} {template} {status} {token_id or '-'} {round((time.perf_counter() - started) * 1000)}ms\n")
        stream.flush()

    def _send(self, status: int, body: bytes, content_type: str = "application/json", headers: dict | None = None) -> None:
        sent = self.headers.get("Content-Length", "0") not in ("", "0") or self.headers.get("Transfer-Encoding")
        if sent and not self.body_read and not self.close_connection:
            # A refusal before the body is read leaves it in the stream, where it would be read as the next request on
            # this connection: behind a proxy that reuses connections (Tailscale Funnel, Caddy), that request got a 501.
            self.close_connection = True
            headers = {**(headers or {}), "Connection": "close"}
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload, headers: dict | None = None) -> None:
        self._send(status, (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode(), headers=headers)

    def _body(self, limit: int) -> bytes | None:
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            self._json(411, {"error": "Content-Length is required"})
            return None
        if int(length) > limit:
            # Read what was sent, up to a bound, before answering, so the client sees the 413 rather than a reset;
            # past the bound the connection just closes.
            remaining = min(int(length), DRAIN)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 16))
                if not chunk:
                    break
                remaining -= len(chunk)
            self.close_connection = True
            self._json(413, {"error": f"the body is over {limit} bytes"}, {"Connection": "close"})
            return None
        self.body_read = True
        return self.rfile.read(int(length))

    def do_GET(self):  # noqa: N802 - http.server's names
        self._route()

    def do_POST(self):  # noqa: N802
        self._route()

    def do_DELETE(self):  # noqa: N802
        self._route()

    def do_PUT(self):  # noqa: N802
        self._route()

    def _route(self) -> None:
        started = time.perf_counter()
        self.body_read = False  # one handler serves every request on a kept-alive connection
        hub = self.hub
        if not hub.slots.acquire(blocking=False):
            self._json(503, {"error": "busy; try again"}, {"Retry-After": "1"})
            self._log("-", 503, "", started)
            return
        try:
            self._dispatch(started)
        finally:
            hub.slots.release()

    def _dispatch(self, started: float) -> None:
        hub = self.hub
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        if {"token", "access_token", "auth"} & set(query):
            self._json(400, {"error": "a token goes in the Authorization header, never the address"})
            self._log("-", 400, "", started)
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in hub.origins:
            self._json(403, {"error": "this origin isn't allowed"})
            self._log("-", 403, "", started)
            return
        for method, pattern, template, need, name in ROUTES:
            match = pattern.fullmatch(parts.path)
            if match is None:
                continue
            if method != self.command:
                self._json(405, {"error": f"use {method}"}, {"Allow": method})
                self._log(template, 405, "", started)
                return
            token = None
            if need != "none":
                ip = self.client_address[0]
                # The token is checked first, so a flood of bad ones from one address (every outside client shares
                # 127.0.0.1 behind a tunnel) never locks out a good token. Only a bad token meets the failed-sign-in
                # limit, and past it nothing is audited, so a flood can't fill the audit table.
                token = tokens.check(hub.instance, self.headers.get("Authorization"), hub.cfg)
                if token is None:
                    if hub.limits.over("auth_failed", ip, add=False):
                        self._json(429, {"error": "too many failed sign-ins; wait and try again"}, {"Retry-After": "60"})
                        self._log(template, 429, "", started)
                        return
                    hub.limits.note("auth_failed", ip)
                    tokens.audit(hub.instance, "auth_failed", http_status=401, client_ip=ip)
                    self._json(401, {"error": "a valid token is required"}, {"WWW-Authenticate": "Bearer"})
                    self._log(template, 401, "", started)
                    return
                if need == "write" and not token.writes:
                    self._json(403, {"error": "this token can only read"})
                    self._log(template, 403, token.id, started)
                    return
                kind = "write" if need == "write" else "read"
                if hub.limits.over(kind, token.id):
                    self._json(429, {"error": "too many requests for this token"}, {"Retry-After": "5"})
                    self._log(template, 429, token.id, started)
                    return
            status = getattr(self, name)(token, *match.groups(), query=query)
            self._log(template, status, token.id if token else "", started)
            return
        self._json(404, {"error": "no such route"})
        self._log("-", 404, "", started)

    def _outcome(self, outcome) -> int:
        status = HTTP_FOR_EXIT.get(outcome.exit_code, 500)
        headers = {"Brain-Exit-Code": str(outcome.exit_code)}
        if outcome.duplicate:
            headers["Brain-Duplicate"] = "true"
        self._json(status, outcome.receipt, headers)
        return status

    def _failure(self, exc: Exception) -> int:
        status, code = {Unreadable: (400, 2), NotAllowed: (403, 1), UnknownChange: (404, 2), Unavailable: (503, 5)}.get(
            type(exc), (500, 5))
        self._json(status, {"error": str(exc)[:300]}, {"Brain-Exit-Code": str(code)})
        return status

    # -- routes ----------------------------------------------------------------------------------------------
    def _mcp(self, token, query) -> int:
        body = self._body(READ_BODY)
        if body is None:
            return 413
        header = self.headers.get("MCP-Protocol-Version")
        if header is not None and header not in mcp.SUPPORTED_VERSIONS:
            self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Unsupported protocol version"}})
            return 400
        try:
            message = ingress.loads(body, what="request")
        except ingress.IngressError as exc:
            self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": mcp.parse_error(exc)}})
            return 400
        state = SimpleNamespace(record=self.hub.cfg.usage.record, client=token.label, session=token.id, calls=1,
                                token=token.label, changed=lambda: None, folders=token.folders)
        response, _ = mcp._dispatch(self.hub.instance, message, header or DEFAULT_MCP, None, state)
        if response is None:
            self._send(202, b"")
            return 202
        status = 400 if "error" in response and response["error"]["code"] in (-32600, -32700) else 200
        self._json(status, response)
        return status

    def _edge(self, token, raw: bytes) -> str | None:
        """Refuse before the applier, writing nothing to write_log, a packet that isn't the token's agent's."""
        try:
            packet = ingress.loads(raw, what="packet", max_nesting=ingress.PACKET_NESTING)
        except ingress.IngressError:
            return None  # the applier says why it can't read it
        agent = packet.get("agent") if isinstance(packet, dict) else None
        change_id = packet.get("change_id") if isinstance(packet, dict) else None
        if agent != token.agent or agent == "brain" or not (isinstance(change_id, str) and change_id.startswith(f"{token.agent}-")):
            return f"this token writes as {token.agent}"
        return None

    def _apply(self, token, query) -> int:
        raw = self._body(APPLY_BODY)
        if raw is None:
            return 413
        sha = hashlib.sha256(raw).hexdigest()
        refusal = self._edge(token, raw)
        if refusal:
            tokens.audit(self.hub.instance, "refused_at_edge", token_id=token.id, agent=token.agent, body_sha256=sha,
                         http_status=403, exit_code=1, client_ip=self.client_address[0])
            self._json(403, {"error": refusal}, {"Brain-Exit-Code": "1"})
            return 403
        with self.hub.write_lock:
            applier = Applier(self.hub.instance)
            try:
                outcome = applier.apply(raw, as_agent=token.agent)
            except (Unreadable, NotAllowed, UnknownChange, Unavailable) as exc:
                return self._failure(exc)
            finally:
                applier.close()
        if outcome.exit_code == 1:
            self.hub.limits.note("rejected", token.id)
        # The audit row is written before the answer, so a client never sees a reply whose row isn't there yet.
        tokens.audit(self.hub.instance, "apply", token_id=token.id, agent=token.agent, change_id=outcome.receipt.get("change_id"),
                     body_sha256=sha, http_status=HTTP_FOR_EXIT.get(outcome.exit_code, 500), exit_code=outcome.exit_code,
                     client_ip=self.client_address[0])
        return self._outcome(outcome)

    def _extract(self, token, query) -> int:
        from brain.extract import run

        if token.kind != "extractor":
            self._json(403, {"error": "only an extractor's token may extract"})
            return 403
        raw = self._body(APPLY_BODY)
        if raw is None:
            return 413
        with self.hub.write_lock:
            try:
                result = run(self.hub.instance, token.agent, raw)
            except (Unreadable, NotAllowed, UnknownChange, Unavailable) as exc:
                return self._failure(exc)
        status = HTTP_FOR_EXIT.get(result.exit_code, 500)
        tokens.audit(self.hub.instance, "extract", token_id=token.id, agent=token.agent, body_sha256=hashlib.sha256(raw).hexdigest(),
                     http_status=status, exit_code=result.exit_code, client_ip=self.client_address[0])
        self._json(status, result.payload, {"Brain-Exit-Code": str(result.exit_code)})
        return status

    def _validate(self, token, query) -> int:
        from brain import validate

        raw = self._body(APPLY_BODY)
        if raw is None:
            return 413
        kind = (query.get("kind") or ["packet"])[0]
        if kind not in ("packet", "candidate"):
            self._json(400, {"error": "kind must be packet or candidate"})
            return 400
        code, payload = validate.validate_command(kind, raw)
        self._json(HTTP_FOR_EXIT.get(code, 500), payload, {"Brain-Exit-Code": str(code)})
        return HTTP_FOR_EXIT.get(code, 500)

    def _review(self, token, change_id, decision, query) -> int:
        raw = self._body(READ_BODY)
        if raw is None:
            return 413
        note = ""
        if decision == "refuse":
            try:
                note = ingress.loads(raw or b"{}", what="note").get("note", "")
            except (ingress.IngressError, AttributeError):
                note = ""
            if not isinstance(note, str) or not note.strip():
                self._json(400, {"error": "a refusal needs a note: {\"note\": \"...\"}"})
                return 400
        with self.hub.write_lock:
            applier = Applier(self.hub.instance)
            try:
                if decision == "approve":
                    outcome = applier.approve(change_id, token.agent, via="remote")
                else:
                    outcome = applier.refuse(change_id, token.agent, note, via="remote")
            except (Unreadable, NotAllowed, UnknownChange, Unavailable) as exc:
                return self._failure(exc)
            finally:
                applier.close()
        tokens.audit(self.hub.instance, decision, token_id=token.id, agent=token.agent, change_id=change_id,
                     http_status=HTTP_FOR_EXIT.get(outcome.exit_code, 500), exit_code=outcome.exit_code,
                     client_ip=self.client_address[0])
        return self._outcome(outcome)

    def _whole_brain_only(self, token, route: str) -> bool:
        """A folder token can't read a route that shows brain-wide write records: answer 403 and say so."""
        if token.folders is None:
            return False
        self._json(403, {"error": advice.folder_route_error(route)})
        return True

    def _receipt(self, token, change_id, query) -> int:
        from brain.applier import receipt_from_row

        if self._whole_brain_only(token, "/v1/receipts"):
            return 403

        con = store.connect_readonly(self.hub.instance / "brain.sqlite3")
        try:
            con.row_factory = __import__("sqlite3").Row
            row = con.execute("SELECT * FROM write_log WHERE change_id = ?", (change_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            self._json(404, {"error": "no such change"}, {"Brain-Exit-Code": "2"})
            return 404
        receipt = receipt_from_row(row)
        code = {"applied": 0, "rejected": 1, "conflict": 3, "held": 4}[receipt["status"]]
        self._json(200, receipt, {"Brain-Exit-Code": str(code)})
        return 200

    def _held(self, token, query) -> int:
        from brain.extract import held

        if self._whole_brain_only(token, "/v1/held"):
            return 403

        try:
            limit = int((query.get("limit") or ["50"])[0])
        except ValueError:
            limit = 0
        if not 1 <= limit <= 500:
            self._json(400, {"error": "limit must be between 1 and 500"})
            return 400
        self._json(200, held(self.hub.instance, limit))
        return 200

    def _read(self, token, tool, query) -> int:
        raw = self._body(READ_BODY)
        if raw is None:
            return 413
        problem = "the body must be a JSON object of arguments"
        try:
            arguments = ingress.loads(raw or b"{}", what="the body")
        except ingress.IngressError as exc:
            arguments = None
            if not exc.malformed:
                problem = str(exc)
        if not isinstance(arguments, dict):
            self._json(400, {"error": problem})
            return 400
        began = time.perf_counter()
        try:
            result = reads.run_tool(self.hub.instance, tool, arguments, folders=token.folders)
        except reads.UnknownTool:
            self._json(404, {"error": "no such tool"})
            return 404
        if self.hub.cfg.usage.record:
            kind, rows = usage.outcome(tool, result)
            try:
                usage.record(self.hub.instance, tool=tool, client="http", token=token.label, result_bytes=len(result.text.encode()),
                             rows=rows, result=kind, ms=(time.perf_counter() - began) * 1000, session=token.id, first=False,
                             relaxed=usage.relaxed(tool, result))
            except OSError:
                pass
        self._send(200, (result.text + "\n").encode(), "text/plain; charset=utf-8",
                   {"Brain-Exit-Code": "1" if result.is_error else "0"})
        return 200

    def _whoami(self, token, query) -> int:
        self._json(200, {"agent": token.agent, "scope": token.scope, "token_id": token.id, "label": token.label,
                         "expires_at": token.expires_at, "folders": None if token.folders is None else list(token.folders)})
        return 200

    def _healthz(self, token, query) -> int:
        self._json(200, {"status": "ok"})
        return 200

    def _readyz(self, token, query) -> int:
        readable = (self.hub.instance / "brain.sqlite3").is_file()
        ok = self.hub.ready and readable
        self._json(200 if ok else 503, {"status": "ready" if ok else "not ready"})
        return 200 if ok else 503
