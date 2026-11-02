"""A fake network for the service tests: recorded answers written for these tests, every request kept,
and any request without a route failing the test. Every name, address and token here is invented."""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path

from brain import store
from brain_fetch import credentials, net
from tests.collector_fixtures import COLLECTORS  # noqa: F401  (keeps the shared fixtures importable first)
from tests.phase2a import custom_instance

GOOGLE_SECRET = json.dumps({"client_id": "client-example", "client_secret": "secret-example",
                            "refresh_token": "refresh-example"})
ACCESS = "access-example-token"


class Unrouted(AssertionError):
    pass


class FakeNet:
    def __init__(self, monkeypatch):
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.calls: list[dict] = []
        self.slept: list[float] = []
        monkeypatch.setattr(net, "TRANSPORT", self)
        monkeypatch.setattr(net, "sleep", self.slept.append)

    def route(self, method: str, pattern: str, answer) -> None:
        """answer is a JSON value, a (status, JSON value) pair, a (status, JSON, headers) triple, or a function of
        the call that returns one of those. Later routes win, so a test can override one."""
        self.routes.insert(0, (method, re.compile(pattern), answer))

    def __call__(self, method, url, headers, body, timeout):
        parts = urllib.parse.urlsplit(url)
        params = {key: values[-1] for key, values in urllib.parse.parse_qs(parts.query).items()}
        form = {key: values[-1] for key, values in urllib.parse.parse_qs((body or b"").decode()).items()}
        call = {"method": method, "host": parts.hostname, "path": parts.path, "params": params, "form": form,
                "headers": dict(headers), "timeout": timeout}
        self.calls.append(call)
        for wanted, pattern, answer in self.routes:
            if wanted == method and pattern.fullmatch(f"{parts.hostname}{parts.path}"):
                result = answer(call) if callable(answer) else answer
                if isinstance(result, Exception):
                    raise result
                if not (isinstance(result, tuple) and isinstance(result[0], int)):
                    result = (200, result)
                status, payload = result[0], result[1]
                reply = {key.lower(): value for key, value in (result[2] if len(result) > 2 else {}).items()}
                data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                return status, reply, data
        raise Unrouted(f"no route for {method} {parts.hostname}{parts.path}")

    def paths(self, host: str | None = None) -> list[str]:
        return [call["path"] for call in self.calls if host is None or call["host"] == host]

    def google_token(self) -> None:
        self.route("POST", r"oauth2\.googleapis\.com/token", {"access_token": ACCESS, "expires_in": 3599})


def secret(monkeypatch, tmp_path: Path, collector: str, value: str) -> None:
    """The credential in a fake /run/secrets, and the keychain unreachable, so no test reads a real one."""
    folder = tmp_path / "secrets"
    folder.mkdir(exist_ok=True)
    path = folder / collector
    path.write_text(value + "\n")
    path.chmod(0o600)
    monkeypatch.setattr(credentials, "SECRETS_DIR", folder)
    monkeypatch.setattr(credentials, "SECURITY", str(tmp_path / "no-security"))


def no_secrets(monkeypatch, tmp_path: Path) -> None:
    folder = tmp_path / "secrets"
    folder.mkdir(exist_ok=True)
    monkeypatch.setattr(credentials, "SECRETS_DIR", folder)
    monkeypatch.setattr(credentials, "SECURITY", str(tmp_path / "no-security"))


def instance(tmp_path: Path, collectors: dict, agents: dict, timezone: str = "UTC") -> Path:
    return custom_instance(tmp_path / "inst", extra_collectors=collectors, agents=agents, timezone=timezone)


def written(instance_dir: Path) -> str:
    """Every file a run wrote under fetch/, as one text, to search for tokens and addresses."""
    return "\n".join(path.read_text(errors="replace") for path in sorted((instance_dir / "fetch").rglob("*"))
                     if path.is_file() and path.name != "mark.key")


def exports(instance_dir: Path, collector: str) -> list[dict]:
    return [json.loads(path.read_text()) for path in sorted((instance_dir / "fetch" / "inbox" / collector).glob("*.json"))]


def state_of(instance_dir: Path, collector: str) -> dict | None:
    path = instance_dir / "fetch" / f"{collector}.state.json"
    return json.loads(path.read_text()) if path.exists() else None


def at(clock, stamp: str) -> None:
    clock.at(stamp)
    assert store.utc_now() == stamp


def bf(*argv: str) -> tuple[int, dict | None, str]:
    import contextlib
    import io

    from brain_fetch.cli import main as fetch_main

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch_main(list(argv))
    text = out.getvalue().strip()
    return code, (json.loads(text) if text.startswith("{") else None), err.getvalue()


def run(instance_dir: Path, *names: str, flags: tuple = ()) -> tuple[int, dict, str]:
    code, payload, err = bf("run", "--instance", str(instance_dir), *flags, *names)
    return code, (payload["collectors"][0] if payload and payload["collectors"] else {}), err
