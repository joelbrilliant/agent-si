"""The one module that reaches the network.

HTTPS only, to a fixed list of hosts, with no redirects followed, so a credential is never sent anywhere else. A
credential, a token or an Authorization header never appears in a message this module raises. Tests replace
TRANSPORT and sleep, and nothing else.
"""
from __future__ import annotations

import http.client
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from brain import __version__, ingress

HOSTS = frozenset({"gmail.googleapis.com", "www.googleapis.com", "oauth2.googleapis.com", "slack.com",
                   "api.github.com"})
TIMEOUT = 30
MAX_BYTES = 32 * 1024 * 1024
MAX_WAIT = 60
RETRIES = 3


class NetError(Exception):
    """A pull that can't go on. error_class is one of the capture export's classes."""

    def __init__(self, message: str, error_class: str = "network", status: int = 0):
        super().__init__(message)
        self.error_class = error_class
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002 - urllib's signature
        return None  # urllib then raises the 3xx as an HTTPError, which request() refuses


def _urllib(method: str, url: str, headers: dict, body: bytes | None, timeout: int) -> tuple[int, dict, bytes]:
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as response:
            return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        data = exc.read(MAX_BYTES + 1) if exc.fp is not None else b""
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, data


TRANSPORT = _urllib
sleep = time.sleep


def _retry_after(headers: dict) -> float:
    try:
        wait = float(headers.get("retry-after", "5"))
    except ValueError:
        wait = 5.0
    return min(max(wait, 1.0), MAX_WAIT)


def _decode(data: bytes):
    try:
        return ingress.loads(data, what="response")
    except ingress.IngressError:
        return None


def request(method: str, url: str, *, token: str | None = None, params: dict | None = None,
            form: dict | None = None, accept: str = "application/json", expect: tuple[int, ...] = ()):
    """(status, decoded JSON). 404 and 410, and any status in expect, come back to the caller; every other
    failure raises NetError with its class."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or "?"
    if parts.scheme != "https" or host not in HOSTS or parts.username or parts.port not in (None, 443):
        raise NetError(f"refused to contact {host}: brain-fetch talks only to its services over HTTPS", "config")
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    headers = {"Accept": accept, "User-Agent": f"agent-si/{__version__} brain-fetch"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    for attempt in range(RETRIES + 1):
        try:
            status, reply, data = TRANSPORT(method, url, headers, body, TIMEOUT)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise NetError(f"{host}: connection failed ({type(exc).__name__})") from None
        if status != 429 or attempt == RETRIES:
            break
        sleep(_retry_after(reply))
    if len(data) > MAX_BYTES:
        raise NetError(f"{host}: the answer is over {MAX_BYTES // (1024 * 1024)} MiB", "limit", status)
    payload = _decode(data)
    if status in (404, 410) or status in expect:
        return status, payload
    if status == 429:
        raise NetError(f"{host}: still rate limited after {RETRIES} waits", "network", status)
    if status in (401, 403):
        raise NetError(f"{host} refused the credential ({status})", "auth", status)
    if 300 <= status < 400:
        raise NetError(f"{host} answered with a redirect ({status}); brain-fetch never follows one", "network", status)
    if status >= 500:
        raise NetError(f"{host} had an error ({status}); the next run retries", "network", status)
    if status >= 400:
        raise NetError(f"{host} refused the request ({status})", "other", status)
    if payload is None:
        raise NetError(f"{host} answered with something that isn't JSON", "network", status)
    return status, payload


def quote(text: str, safe: str = "/") -> str:
    """Percent-encoding for a path segment, so the service modules need no urllib of their own."""
    return urllib.parse.quote(text, safe=safe)


def field(payload, *path, default=None):
    """payload[path[0]][path[1]]..., or default when any step is missing or the wrong shape."""
    for key in path:
        if isinstance(payload, dict) and key in payload:
            payload = payload[key]
        elif isinstance(payload, list) and isinstance(key, int) and -len(payload) <= key < len(payload):
            payload = payload[key]
        else:
            return default
    return payload
