"""The remote CLI: brain apply, extract, held, approve, refuse, receipt and read against a hub,
with --remote URL or BRAIN_INSTANCE=https://..., and the token from BRAIN_TOKEN. Output and exit codes are the same
as local. http:// is refused except to exact loopback, so a token never crosses a network in the clear."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from brain import ingress

LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class RemoteError(Exception):
    pass


def target(remote: str | None) -> str | None:
    """The hub URL from --remote or an https BRAIN_INSTANCE, or None for a local instance."""
    url = remote or (os.environ.get("BRAIN_INSTANCE") or "")
    if not url.startswith(("https://", "http://")):
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise RemoteError("a hub is reached over https; http is only for this machine's loopback address")
    if parts.username or parts.password or parts.query:
        raise RemoteError("the hub address can't carry a user, a password or a query; the token goes in BRAIN_TOKEN")
    return url.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def quote(text: str) -> str:
    """One path segment, percent-encoded, so the engine package needs no urllib of its own."""
    return urllib.parse.quote(text, safe="")


def call(url: str, method: str, path: str, body: bytes | None = None, query: dict | None = None) -> tuple[int, int, bytes]:
    """(HTTP status, Brain-Exit-Code, body)."""
    token = os.environ.get("BRAIN_TOKEN", "")
    if not token:
        raise RemoteError("set BRAIN_TOKEN to your hub token")
    address = url + path + ("?" + urllib.parse.urlencode(query) if query else "")
    request = urllib.request.Request(address, data=body if body is not None else (b"" if method == "POST" else None),
                                     method=method, headers={"Authorization": f"Bearer {token}",
                                                             "Content-Type": "application/json"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=60) as response:
            status, headers = response.status, response.headers
            data = ingress.read_bytes(response, ingress.MAX_DOCUMENT_BYTES, "the hub's answer")
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, exc.headers
        try:
            data = ingress.read_bytes(exc, ingress.MAX_DOCUMENT_BYTES, "the hub's answer")
        except ingress.IngressError as big:
            raise RemoteError(str(big)) from None
    except ingress.IngressError as exc:
        raise RemoteError(str(exc)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise RemoteError(f"couldn't reach the hub ({type(exc).__name__})") from None
    code = headers.get("Brain-Exit-Code") if headers else None
    exit_code = int(code) if code and code.isdigit() else {200: 0, 202: 4, 401: 2, 403: 1, 404: 2, 409: 3, 413: 2,
                                                            422: 1, 429: 5, 503: 5}.get(status, 5)
    return status, exit_code, data


def error_text(data: bytes) -> str:
    try:
        return str(ingress.loads(data, what="the hub's answer").get("error") or "")
    except (ingress.IngressError, AttributeError):
        return ""


def smoke(url: str, token: str) -> str:
    """initialize, tools/list and brief against the hub, with the token: pass or fail, never the token."""
    def post(message: dict) -> dict | None:
        request = urllib.request.Request(url, data=json.dumps(message).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                                  "Accept": "application/json, text/event-stream"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return ingress.loads(ingress.read_bytes(response, ingress.MAX_DOCUMENT_BYTES, "the hub's answer"),
                                     what="the hub's answer")
        except (urllib.error.URLError, OSError, ingress.IngressError):
            return None

    opened = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
    listed = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    brief = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "brief", "arguments": {}}})
    ok = bool(opened and "result" in opened and listed and any(t.get("name") == "brief" for t in listed.get("result", {}).get("tools", []))
              and brief and not brief.get("result", {}).get("isError") and brief.get("result", {}).get("content"))
    return "pass" if ok else "fail"
