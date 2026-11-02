"""The real urllib transport, without a network. The opener is replaced, so the request it
builds and the way it reads an error are checked as urllib would see them."""
from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from brain_fetch import net


class _Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json", "X-Extra": "1"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Opener:
    def __init__(self, outcome):
        self.outcome = outcome
        self.requests = []
        self.timeouts = []

    def open(self, req, timeout):
        self.requests.append(req)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _install(monkeypatch, outcome):
    opener = _Opener(outcome)
    handlers = []

    def build(*given):
        handlers.extend(given)
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build)
    return opener, handlers


def test_the_transport_sends_what_request_built(monkeypatch) -> None:
    opener, handlers = _install(monkeypatch, _Response(b'{"ok": true}'))
    status, payload = net.request("POST", "https://slack.com/api/auth.test", token="token-example",
                                  params={"a": "1 2"}, form={"x": "y"})
    assert (status, payload) == (200, {"ok": True})
    req = opener.requests[0]
    assert req.full_url == "https://slack.com/api/auth.test?a=1+2" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer token-example" and req.data == b"x=y"
    assert req.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert opener.timeouts == [30]
    assert net._NoRedirect in handlers and any(isinstance(h, urllib.request.HTTPSHandler) for h in handlers)


def test_an_http_error_is_read_as_a_status(monkeypatch) -> None:
    error = urllib.error.HTTPError("https://slack.com/api/auth.test", 404, "Not Found", {"Retry-After": "3"},
                                   io.BytesIO(b'{"error": "gone"}'))
    _install(monkeypatch, error)
    assert net.request("GET", "https://slack.com/api/auth.test") == (404, {"error": "gone"})


@pytest.mark.parametrize("failure", [urllib.error.URLError("no route"), TimeoutError("slow"), ConnectionResetError()])
def test_a_connection_failure_is_a_network_error(monkeypatch, failure) -> None:
    _install(monkeypatch, failure)
    with pytest.raises(net.NetError) as caught:
        net.request("GET", "https://api.github.com/repos/example-co/handbook", token="token-example")
    assert caught.value.error_class == "network" and "token-example" not in str(caught.value)
    assert str(caught.value).startswith("api.github.com: connection failed (")
