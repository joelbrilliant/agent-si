"""The team hub over loopback HTTP: tokens, the as_agent check at the edge and in the applier, MCP, writes
byte-identical to brain apply, remote approvals, audit, limits, and logs with no content. Invented data only."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import re
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brain import reads, store
from brain.applier import Applier, NotAllowed
from brain_hub import server as hub_server, tokens
from tests.conftest import make_packet, ro_rows
from tests.phase2a import custom_instance


def _source(sid: str, text: str = "Pilot starts Monday.") -> dict:
    return {"op": "add_source", "row": {"id": sid, "kind": "user_note", "title": "Note", "content": text,
                                         "source_date": "2026-10-09"}}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    inst = custom_instance(tmp_path / "inst")
    instance = Hub(inst)
    yield instance
    instance.stop()


class Hub:
    def __init__(self, inst: Path):
        self.inst = inst
        self.app = hub_server.Hub(inst)
        self.httpd = self.app.serve("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.write = tokens.issue(inst, "tester", "read write", "Alex Example laptop, Claude Code")
        self.plain = tokens.issue(inst, "plain", "read write", "Sam Example laptop, Codex")
        self.careful = tokens.issue(inst, "careful", "read write", "Careful bot")
        self.read = tokens.issue(inst, "plain", "read", "Read-only dashboard")

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def request(self, method: str, path: str, body=None, token: str | None = "", headers: dict | None = None):
        token = self.write if token == "" else token
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


def _log_count(inst: Path) -> int:
    return ro_rows(inst, "SELECT count(*) FROM write_log")[0][0]


def test_health_needs_no_token_and_everything_else_does(hub) -> None:
    assert hub.request("GET", "/healthz", token=None)[0] == 200
    assert hub.request("GET", "/readyz", token=None)[0] == 200
    for token in (None, "abt_000000000000_" + "x" * 43, "not-a-token"):
        status, headers, body = hub.request("GET", "/v1/whoami", token=token)
        assert status == 401 and headers["WWW-Authenticate"] == "Bearer"
    assert hub.request("GET", f"/v1/whoami?token={hub.write}", token=None)[0] == 400
    status, _, body = hub.request("GET", "/v1/whoami")
    who = json.loads(body)
    assert (status, who["agent"], who["scope"], who["label"]) == (200, "tester", "read write", "Alex Example laptop, Claude Code")
    actions = [row["action"] for row in tokens.audit_rows(hub.inst, "2000-01-01")]
    assert actions.count("auth_failed") == 3 and actions.count("token_issued") == 4


def test_mcp_over_http_gives_the_same_text_as_stdio(hub) -> None:
    status, _, body = hub.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                   "params": {"protocolVersion": "2025-11-25"}})
    assert status == 200 and json.loads(body)["result"]["protocolVersion"] == "2025-11-25"
    status, _, body = hub.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                  headers={"MCP-Protocol-Version": "2025-11-25"})
    assert sorted(tool["name"] for tool in json.loads(body)["result"]["tools"]) == sorted(reads._TOOLS)
    status, _, body = hub.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                   "params": {"name": "brief", "arguments": {}}},
                                  token=hub.read, headers={"MCP-Protocol-Version": "2025-11-25"})
    assert json.loads(body)["result"]["content"][0]["text"] == reads.run_tool(hub.inst, "brief", {}).text
    status, _, body = hub.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                                  headers={"MCP-Protocol-Version": "2026-07-28"})
    assert status == 400 and json.loads(body)["error"]["code"] == -32600
    assert hub.request("GET", "/mcp")[0] == 405 and hub.request("DELETE", "/mcp")[0] == 405
    assert hub.request("POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202
    usage_lines = [json.loads(line) for path in (hub.inst / "usage").glob("*.jsonl") for line in path.read_text().splitlines()]
    assert usage_lines and usage_lines[-1]["token"] == "Read-only dashboard" and usage_lines[-1]["tool"] == "brief"


def test_writes_are_byte_identical_to_brain_apply_and_resends_are_duplicates(hub, tmp_path) -> None:
    packet = make_packet("tester", _source("src-hub-1"))
    status, headers, body = hub.request("POST", "/v1/apply", packet)
    assert (status, headers["Brain-Exit-Code"]) == (200, "0")
    receipt = json.loads(body)
    assert body == (json.dumps(receipt, sort_keys=True, ensure_ascii=False) + "\n").encode()
    status, headers, again = hub.request("POST", "/v1/apply", packet)
    assert (status, headers["Brain-Duplicate"], again) == (200, "true", body)
    status, headers, body = hub.request("GET", f"/v1/receipts/{packet['change_id']}", token=hub.read)
    assert status == 200 and json.loads(body)["status"] == "applied"
    rejected = make_packet("tester", {"op": "add_claim", "row": {"id": "clm-x", "key": "demo.seats", "value_raw": "5",
                                                                  "basis": "reported", "quote": "not in it"},
                                       "source": {"source_id": "src-hub-1", "locator": "para 1"}})
    status, headers, body = hub.request("POST", "/v1/apply", rejected)
    assert (status, headers["Brain-Exit-Code"]) == (422, "1")


def test_a_token_writes_only_as_its_own_agent(hub) -> None:
    before = _log_count(hub.inst)
    for packet in (make_packet("plain", _source("src-hub-2")), make_packet("tester", _source("src-hub-3"), change_id="plain-1"),
                   make_packet("brain", _source("src-hub-4"))):
        status, headers, body = hub.request("POST", "/v1/apply", packet)
        assert status == 403 and json.loads(body)["error"] == "this token writes as tester"
    assert _log_count(hub.inst) == before  # refused at the edge: nothing logged, no change_id used up
    edge = [row for row in tokens.audit_rows(hub.inst, "2000-01-01") if row["action"] == "refused_at_edge"]
    assert len(edge) == 3 and all(row["change_id"] is None for row in edge)
    assert hub.request("POST", "/v1/apply", make_packet("plain", _source("src-hub-5")), token=hub.read)[0] == 403
    assert hub.request("POST", "/v1/apply", make_packet("plain", _source("src-hub-5")), token=hub.plain)[0] == 200
    # In the applier too, for any caller that skips the edge.
    writer = Applier(hub.inst)
    try:
        with pytest.raises(NotAllowed):
            writer.apply(json.dumps(make_packet("plain", _source("src-hub-6"))).encode(), as_agent="tester")
    finally:
        writer.close()


def test_held_changes_are_approved_remotely_by_an_approver_only(hub, monkeypatch) -> None:
    packet = make_packet("careful", _source("src-hub-7"))
    status, headers, body = hub.request("POST", "/v1/apply", packet, token=hub.careful)
    assert (status, headers["Brain-Exit-Code"]) == (202, "4")
    status, _, body = hub.request("GET", "/v1/held?limit=5", token=hub.read)
    assert status == 200 and packet["change_id"] in body.decode()
    status, _, body = hub.request("POST", f"/v1/changes/{packet['change_id']}/approve", {}, token=hub.plain)
    assert status == 403 and "plain may not review" in json.loads(body)["error"]
    from brain.cli import main

    monkeypatch.setenv("BRAIN_INSTANCE", hub.url)
    monkeypatch.setenv("BRAIN_TOKEN", hub.write)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["approve", packet["change_id"]]) == 0
    # The hub runs in this process too, so its log line shares stdout here: take the receipt line.
    receipt = next(json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{"))
    assert receipt["status"] == "applied"
    row = ro_rows(hub.inst, "SELECT reviewed_by, approved_via FROM write_log WHERE change_id = ?", (packet["change_id"],))
    assert row == [("tester", "remote")]
    with contextlib.redirect_stderr(io.StringIO()) as err:
        assert main(["approve", packet["change_id"], "--as", "plain"]) == 2
    assert "doesn't match this token's agent" in err.getvalue()


def test_hostile_packets_never_get_a_500(hub) -> None:
    bodies = [b"", b"{", b"[]", b"null", b'"x"', b"{}", json.dumps({"agent": "tester", "change_id": "tester-x", "ops": "no"}).encode(),
              json.dumps({"packet_version": 1, "agent": "tester", "change_id": "tester-y", "intent": "x",
                          "ops": [{"op": "drop_table"}]}).encode(), b"\xff\xfe", json.dumps({"a": [[[[[[[[[[[[[[[]]]]]]]]]]]]]]]}).encode(),
              json.dumps(make_packet("tester", {"op": "add_source", "row": {"id": "bad id!"}})).encode()]
    for body in bodies:
        status, _, _ = hub.request("POST", "/v1/apply", body)
        assert status in (400, 403, 422), (body[:40], status)


def test_limits_on_size_length_origin_and_slots(hub) -> None:
    assert hub.request("POST", "/v1/apply", b"x" * (hub_server.APPLY_BODY + 1))[0] == 413
    assert hub.request("POST", "/mcp", b"x" * (hub_server.READ_BODY + 1))[0] == 413
    conn = http.client.HTTPConnection("127.0.0.1", hub.httpd.server_address[1], timeout=10)
    conn.putrequest("POST", "/v1/apply")
    conn.putheader("Authorization", f"Bearer {hub.write}")
    conn.endheaders()
    assert conn.getresponse().status == 411
    conn.close()
    assert hub.request("GET", "/v1/whoami", headers={"Origin": "https://evil.example"})[0] == 403
    for _ in range(hub_server.HANDLERS):
        hub.app.slots.acquire()
    try:
        status, headers, _ = hub.request("GET", "/healthz", token=None)
        assert (status, headers["Retry-After"]) == (503, "1")
    finally:
        for _ in range(hub_server.HANDLERS):
            hub.app.slots.release()


def test_a_refusal_that_leaves_the_body_unread_closes_the_connection(hub) -> None:
    # Behind a proxy that reuses connections (Tailscale Funnel, Caddy), a refused request's unread body was read as the
    # start of the next request on the same connection, which then got a 501 (found through the Funnel).
    port = hub.httpd.server_address[1]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}, separators=(",", ":")).encode()

    def post(token: str) -> bytes:
        return (f"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body

    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(post("abt_000000000000_" + "A" * 43) + post(hub.read))
        sock.shutdown(socket.SHUT_WR)
        replies = b""
        while chunk := sock.recv(65536):
            replies += chunk
    statuses = re.findall(rb"^HTTP/1\.[01] (\d{3})", replies, re.M)
    assert statuses[0] == b"401"
    assert set(statuses[1:]) <= {b"200"}, statuses  # never a 501 from the body read as a request line
    assert b"connection: close" in replies.split(b"\r\n\r\n")[0].lower()
    # A request whose body was read keeps its connection.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    for ident in (1, 2):
        conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": ident, "method": "ping"}),
                     {"Authorization": f"Bearer {hub.read}", "Content-Type": "application/json"})
        response = conn.getresponse()
        assert response.status == 200 and response.getheader("Connection") != "close"
        response.read()
    conn.close()


def test_failed_sign_ins_are_limited_per_address(hub) -> None:
    for _ in range(hub_server.LIMITS["auth_failed"][0]):
        assert hub.request("GET", "/v1/whoami", token="bad")[0] == 401
    assert hub.request("GET", "/v1/whoami", token="bad")[0] == 429
    assert hub.request("GET", "/v1/whoami")[0] == 200  # a valid token is never refused by the failed-sign-in limit


def test_tokens_revoke_expire_and_follow_the_config(hub, clock=None) -> None:
    public = hub.plain.split("_")[1]
    assert tokens.revoke(hub.inst, public) and hub.request("GET", "/v1/whoami", token=hub.plain)[0] == 401
    with pytest.raises(tokens.TokenError, match="the reserved agent brain never gets a token"):
        tokens.issue(hub.inst, "brain", "read write", "nope")
    with pytest.raises(tokens.TokenError, match="no agent ghost"):
        tokens.issue(hub.inst, "ghost", "read", "nope")
    con = tokens.connect(hub.inst)
    try:
        con.execute("UPDATE tokens SET expires_at = '2000-01-01T00:00:00Z' WHERE agent = 'careful'")
    finally:
        con.close()
    assert hub.request("GET", "/v1/whoami", token=hub.careful)[0] == 401
    stored = (hub.inst / "hub.sqlite3").read_bytes()
    assert hub.write.encode() not in stored and hub.write[len("abt_") + 13:].encode() not in stored  # a hash only


def test_logs_carry_no_content(hub) -> None:
    hub.app.log_stream = stream = io.StringIO()
    packet = make_packet("tester", _source("src-hub-9", "SecretQuote about Example Pro"))
    hub.request("POST", "/v1/apply", packet)
    hub.request("POST", f"/v1/changes/{packet['change_id']}/approve", {})
    hub.request("POST", "/v1/read/search", {"query": "SecretQuery"})
    out = stream.getvalue()
    assert "hub POST /v1/apply 200" in out and "/v1/changes/{id}/{decision}" in out
    for secret in ("SecretQuote", "SecretQuery", packet["change_id"], hub.write, hub.write[len("abt_") + 13:]):
        assert secret not in out


def test_remote_cli_reads_and_writes_and_refuses_plain_http(hub, monkeypatch, tmp_path) -> None:
    from brain.cli import main

    monkeypatch.setenv("BRAIN_TOKEN", hub.write)
    packet = tmp_path / "packet.json"
    packet.write_text(json.dumps(make_packet("tester", _source("src-hub-10"))))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert main(["apply", str(packet), "--remote", hub.url]) == 0
        assert main(["read", "brief", "--remote", hub.url]) == 0
        assert main(["held", "--remote", hub.url]) == 0
    text = out.getvalue()
    assert '"status": "applied"' in text and "demo brain" in text
    with contextlib.redirect_stderr(io.StringIO()) as err:
        assert main(["read", "brief", "--remote", "http://brain.example.co"]) == 2
    assert "reached over https" in err.getvalue()


def test_the_extract_route_is_for_extractors_only(hub) -> None:
    status, _, body = hub.request("POST", "/v1/extract", {"candidates": []})
    assert status == 403 and "only an extractor" in json.loads(body)["error"]


def test_the_hub_router_and_config_never_hold_a_token() -> None:
    from brain_hub.cli import ROUTER

    assert len(ROUTER.encode()) <= 2048 and "Call the brain's `brief` tool first" in ROUTER
    root = Path(__file__).resolve().parents[1]
    for path in [root / "src" / "brain" / "templates" / "brain.toml.template", *root.glob("bench/**/*.toml")]:
        assert "abt_" not in path.read_text(), path


def test_connect_writes_each_tools_hub_form_and_smokes_it(hub, tmp_path, monkeypatch) -> None:
    from brain import connect
    from tests import test_connect as tc

    world, home, inst = tc._world(tmp_path / "w", monkeypatch, harnesses=("claude", "codex", "grok", "gemini"))
    monkeypatch.setenv("BRAIN_TOKEN", hub.read)
    url = hub.url + "/mcp"
    results = {item["harness"]: item for item in connect.remote_connect(["claude", "codex", "grok", "gemini", "cursor"],
                                                                         url, "BRAIN_TOKEN", "brain-work", yes=True)}
    adds = {call[0]: call[1:] for call in tc._calls(home) if call[1:3] == ["mcp", "add"]}
    assert adds["claude"] == ["mcp", "add", "--transport", "http", "--scope", "user", "brain-work", url, "--header",
                              "Authorization: Bearer ${BRAIN_TOKEN}"]
    assert adds["codex"] == ["mcp", "add", "brain-work", "--url", url, "--bearer-token-env-var", "BRAIN_TOKEN"]
    assert adds["grok"][-2:] == ["-H", "Authorization: Bearer ${BRAIN_TOKEN}"]
    assert results["gemini"]["status"] == "printed" and "paste" in results["gemini"]["message"]
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["brain-work"]
    assert cursor == {"type": "http", "url": url, "headers": {"Authorization": "Bearer ${env:BRAIN_TOKEN}"}}
    assert {item["smoke"] for item in results.values()} == {"pass"}
    every = json.dumps(tc._calls(home)) + (home / ".cursor" / "mcp.json").read_text()
    assert hub.read not in every  # the token itself is never written
    with pytest.raises(connect.ConnectError):
        connect.remote_connect(["claude"], "http://brain.example.co/mcp", "BRAIN_TOKEN", "brain-work")


def test_extract_runs_against_the_hub_with_an_extractor_token(tmp_path, monkeypatch) -> None:
    """Acceptance 4: the verification runs on the hub, as the token's extractor, through brain extract --remote."""
    from brain.cli import main
    from tests import extract_fixtures as ef

    inst = ef.extract_instance(tmp_path / "x")
    app = hub_server.Hub(inst)
    httpd = app.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        monkeypatch.setenv("BRAIN_TOKEN", tokens.issue(inst, "scan", "read write", "Scan extractor"))
        path = tmp_path / "candidate.json"
        path.write_text(store.canonical_json(ef.candidate("decision_candidate")))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["extract", str(path), "--as", "scan", "--remote", url])
        payload = next(json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{"))
        # As locally: this candidate waits for an approver (exit 4), with the extractor as the writer.
        assert (code, payload["kind"], payload["receipt"]["status"], payload["receipt"]["agent"]) == (
            4, "decision_candidate", "held", "scan")
        assert [row["action"] for row in tokens.audit_rows(inst, "2000-01-01")][-1] == "extract"
    finally:
        httpd.shutdown()
        httpd.server_close()
