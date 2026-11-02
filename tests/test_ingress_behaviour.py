"""Hostile input is refused the same way at every entry point.

Each row sends one hostile input through one entry point's public interface (a `brain` subprocess, an MCP line, a
hub request) and checks the documented refusal: its exit code or status, no traceback, no exit 5, no hang, and the
server still answering afterwards. Add a row when you add an entry point; the lint in test_ingress_only.py fails if
the new code parses with plain json.

Limits (5 MiB a packet, 1 MiB a candidate or a request, 32 MiB a fixture, 64 KiB a hub read body) are the documented
ones, written out here so this file also runs against older code.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from brain import ingress

from brain_hub import server as hub_server, tokens
from tests.collector_fixtures import COLLECTORS  # noqa: F401 - the notes collector the instance below configures
from tests.extract_fixtures import candidate, extract_instance

MIB = 1024 * 1024
DEEP = b"[" * 100_000 + b"]" * 100_000
UNHASHABLE_OP = json.dumps(
    {"packet_version": 1, "change_id": "plain-hostile-op", "agent": "plain", "intent": "hostile", "ops": [{"op": []}]}
).encode()
# Name -> the bytes sent. The JSON-level ones are refused before any shape check, so a bare object stands for a packet,
# a candidate, a fixture, a request and a body alike.
HOSTILE = {
    "deep": DEEP,
    "big_int": b'{"n": 9223372036854775808}',
    "big_float": b'{"n": 1e999}',
    "nan": b'{"n": NaN}',
    "surrogate": b'{"n": "\\ud800"}',
    "not_utf8": b'{"n": "\xff\xfe"}',
    "garbage": b"{not json",
}
JSON_LEVEL = list(HOSTILE)
# Refused for what they say, not for being JSON; only a surface that reads a packet has them.
UNHASHABLE = {"unhashable_op": UNHASHABLE_OP}


def _brain(*argv, stdin: bytes | None = None, env: dict | None = None, timeout: int = 90) -> subprocess.CompletedProcess:
    """`python -m brain`, so PYTHONPATH decides which engine runs. A hang is a failed row, not a stuck suite."""
    try:
        return subprocess.run([sys.executable, "-m", "brain", *map(str, argv)], input=stdin, capture_output=True,
                              timeout=timeout, env={**os.environ, **(env or {})})
    except subprocess.TimeoutExpired:
        pytest.fail(f"brain {' '.join(map(str, argv[:2]))} did not finish in {timeout} seconds (a hang)")


def _clean(done: subprocess.CompletedProcess, codes: set[int], label: str) -> None:
    stderr = done.stderr.decode("utf-8", "replace")
    assert "Traceback" not in stderr, f"{label}: traceback\n{stderr[-600:]}"
    assert done.returncode in codes, f"{label}: exit {done.returncode}, wanted one of {sorted(codes)}\n{stderr[-400:]}"
    assert done.returncode != 5, label


@pytest.fixture(scope="module")
def instance(tmp_path_factory) -> Path:
    return extract_instance(tmp_path_factory.mktemp("ingress"), "notes")


def _file(tmp_path: Path, body: bytes, name: str = "input.json") -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


# -- the command line: a file or standard input -------------------------------------------------------------------

@pytest.mark.parametrize("name", JSON_LEVEL + ["unhashable_op"])
def test_apply_refuses(instance: Path, tmp_path: Path, name: str) -> None:
    body = {**HOSTILE, **UNHASHABLE}[name]
    # A packet that is only wrong inside is rejected (exit 1); one that can't be read is unreadable (exit 2).
    done = _brain("apply", _file(tmp_path, body), "--instance", instance)
    _clean(done, {1} if name == "unhashable_op" else {2}, f"apply {name}")
    done = _brain("apply", "-", "--instance", instance, stdin=body)
    _clean(done, {1} if name == "unhashable_op" else {2}, f"apply - {name}")


@pytest.mark.parametrize("name", JSON_LEVEL + ["unhashable_op"])
def test_validate_holds_with_a_report(tmp_path: Path, name: str) -> None:
    body = {**HOSTILE, **UNHASHABLE}[name]
    for kind in ("packet", "candidate"):
        done = _brain("validate", kind, _file(tmp_path, body))
        _clean(done, {1}, f"validate {kind} {name}")
        assert json.loads(done.stdout)["status"] == "held", (kind, name, done.stdout)


@pytest.mark.parametrize("name", JSON_LEVEL)
def test_validate_against_an_instance_is_unreadable(instance: Path, tmp_path: Path, name: str) -> None:
    done = _brain("validate", "packet", _file(tmp_path, HOSTILE[name]), "--instance", instance)
    _clean(done, {2}, f"validate --instance {name}")


@pytest.mark.parametrize("name", JSON_LEVEL)
def test_extract_holds_a_bad_candidate(instance: Path, tmp_path: Path, name: str) -> None:
    done = _brain("extract", _file(tmp_path, HOSTILE[name]), "--as", "scan", "--instance", instance)
    _clean(done, {1}, f"extract {name}")
    assert json.loads(done.stdout)["status"] == "held"


# command -> (the most it reads, its argv for a source)
READERS = {
    "apply": (5 * MIB, lambda source, inst: ["apply", source, "--instance", inst]),
    "validate-packet": (5 * MIB, lambda source, inst: ["validate", "packet", source]),
    "validate-candidate": (MIB, lambda source, inst: ["validate", "candidate", source]),
    "extract": (MIB, lambda source, inst: ["extract", source, "--as", "scan", "--instance", inst]),
}


class _EndlessStream:
    """A stream that never ends, in process and finite in practice: it serves zeros for bounded reads and fails the
    test on an unbounded one. Reading /dev/zero without a cap never ends and a SIGKILL can't land mid-read, so no test
    touches a real device."""

    def __init__(self) -> None:
        self.served = 0

    def read(self, n: int = -1) -> bytes:
        assert n is not None and n >= 0, "an unbounded read of a stream that never ends"
        assert self.served + n <= 64 * MIB, "read far past any cap"
        self.served += n
        return b"\0" * n


@pytest.mark.parametrize("command", READERS)
def test_a_source_that_never_ends_is_refused_after_one_bounded_read(command: str) -> None:
    cap = READERS[command][0]
    stream = _EndlessStream()
    with pytest.raises(ingress.IngressError):
        ingress.read_bytes(stream, cap, command)
    assert stream.served == cap + 1


@pytest.mark.parametrize("command", READERS)
def test_an_oversized_input_is_refused(instance: Path, tmp_path: Path, command: str) -> None:
    cap, argv = READERS[command]
    done = _brain(*argv(_file(tmp_path, b" " * (cap + 1)), instance))
    _clean(done, {2}, f"{command} oversized")
    assert "larger than" in done.stderr.decode(), done.stderr
    _clean(_brain(*argv("-", instance), stdin=b" " * (cap + 1)), {2}, f"{command} oversized on standard input")


@pytest.mark.parametrize("name", JSON_LEVEL)
def test_read_args_are_refused(instance: Path, name: str) -> None:
    body = HOSTILE[name]
    done = _brain("read", "records_query", "--args", "-", "--instance", instance, stdin=body)
    _clean(done, {2}, f"read --args - {name}")
    # An argument can't carry bytes that aren't UTF-8, and Linux caps one argument at 128 KiB.
    if b"\xff" not in body and len(body) < 128 * 1024:
        done = _brain("read", "records_query", "--args", body.decode(), "--instance", instance)
        _clean(done, {2}, f"read --args {name}")
    done = _brain("read", "records_query", "--args", "-", "--instance", instance, stdin=b" " * (MIB + 1))
    _clean(done, {2}, "read --args - oversized")


@pytest.mark.parametrize("name", JSON_LEVEL)
def test_collect_refuses_a_fixture(instance: Path, tmp_path: Path, name: str) -> None:
    done = _brain("collect", "notes", "--fixture", _file(tmp_path, HOSTILE[name]), "--instance", instance)
    _clean(done, {2}, f"collect {name}")


def test_collect_refuses_an_oversized_fixture(instance: Path, tmp_path: Path) -> None:
    big = tmp_path / "big.json"
    with big.open("wb") as handle:
        handle.truncate(32 * MIB + 1)
    _clean(_brain("collect", "notes", "--fixture", big, "--instance", instance), {2}, "collect oversized")


# -- brain serve: one JSON-RPC request a line ---------------------------------------------------------------------

@pytest.mark.parametrize("name", JSON_LEVEL + ["oversized"])
def test_serve_answers_a_bad_line_and_carries_on(instance: Path, name: str) -> None:
    line = b" " * (MIB + 1) if name == "oversized" else HOSTILE[name]
    ping = b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    done = _brain("serve", "--instance", instance, stdin=line + b"\n" + ping)
    _clean(done, {0}, f"serve {name}")
    first, second = [json.loads(text) for text in done.stdout.decode().splitlines()]
    assert first["error"]["code"] in (-32700, -32600) and first["id"] is None, (name, first)
    assert second["id"] == 7 and "result" in second, (name, second)


# -- the hub: a request body ---------------------------------------------------------------------------------------

class _Hub:
    def __init__(self, inst: Path):
        self.app = hub_server.Hub(inst)
        self.httpd = self.app.serve("127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.writer = tokens.issue(inst, "plain", "read write", "Hostile writer")
        self.extractor = tokens.issue(inst, "scan", "read write", "Hostile extractor")

    def post(self, path: str, body: bytes, token: str | None = None) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(self.url + path, data=body, method="POST",
                                         headers={"Authorization": f"Bearer {token or self.writer}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def alive(self) -> bool:
        with urllib.request.urlopen(self.url + "/healthz", timeout=30) as response:
            return response.status == 200


@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    app = _Hub(extract_instance(tmp_path_factory.mktemp("hub"), "notes"))
    yield app
    app.httpd.shutdown()
    app.httpd.server_close()


# path, the token, the status a bad body gets, the Brain-Exit-Code header ("" when the route sets none)
HUB_ROUTES = [
    ("/mcp", "writer", 400, ""),
    ("/v1/read/records_query", "writer", 400, ""),
    ("/v1/apply", "writer", 400, "2"),
    ("/v1/validate", "writer", 422, "1"),
    ("/v1/extract", "extractor", None, None),  # a candidate that is bad JSON is held (422), one too big is 400
    ("/v1/changes/plain-x/refuse", "writer", 400, ""),
]


READ_BODIES = {"/mcp", "/v1/read/records_query", "/v1/changes/plain-x/refuse"}


@pytest.mark.parametrize("name", JSON_LEVEL)
@pytest.mark.parametrize("path, who, status, header", HUB_ROUTES, ids=[route[0] for route in HUB_ROUTES])
def test_hub_refuses_a_bad_body(hub: _Hub, path: str, who: str, status, header, name: str) -> None:
    sent = HOSTILE[name]
    if len(sent) > 64 * 1024 and path in READ_BODIES:  # a read body is capped at 64 KiB, so nest as deep as fits
        sent = b"[" * 30_000 + b"]" * 30_000
    got, headers, body = hub.post(path, sent, getattr(hub, who))
    assert got != 500 and 400 <= got < 500, f"{path} {name}: {got} {body[:200]!r}"
    if status is not None:
        assert got == status, f"{path} {name}: {got} {body[:200]!r}"
    if header:
        assert headers.get("Brain-Exit-Code") == header, (path, name, headers)
    json.loads(body)  # a JSON answer, not a stack trace
    assert hub.alive()


def test_hub_refuses_an_unhashable_op(hub: _Hub) -> None:
    got, headers, body = hub.post("/v1/apply", UNHASHABLE_OP)
    assert got == 422 and headers.get("Brain-Exit-Code") == "1", (got, body[:200])
    got, headers, body = hub.post("/v1/validate", UNHASHABLE_OP)
    assert got == 422 and json.loads(body)["status"] == "held", (got, body[:200])


@pytest.mark.parametrize("path, cap", [("/mcp", 64 * 1024), ("/v1/read/records_query", 64 * 1024),
                                         ("/v1/apply", 5 * MIB), ("/v1/validate", 5 * MIB)])
def test_hub_refuses_an_oversized_body(hub: _Hub, path: str, cap: int) -> None:
    got, _headers, body = hub.post(path, b" " * (cap + 1))
    assert got == 413, (path, got, body[:200])
    assert hub.alive()


# -- manifests and state files read from disk -----------------------------------------------------------------------

def _bundle(path: Path, manifest: bytes) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        member = tarfile.TarInfo("manifest.json")
        member.size = len(manifest)
        tar.addfile(member, io.BytesIO(manifest))
    return path


@pytest.mark.parametrize("name", JSON_LEVEL + ["oversized"])
def test_restore_refuses_a_hostile_manifest(tmp_path: Path, name: str) -> None:
    manifest = b" " * (MIB + 1) if name == "oversized" else HOSTILE[name]
    done = _brain("restore", _bundle(tmp_path / "bundle.tar.gz", manifest), "--dir", tmp_path / "target")
    _clean(done, {1, 2}, f"restore {name}")
    assert "restore refused" in done.stderr.decode(), done.stderr
    assert not (tmp_path / "target").exists() or not any((tmp_path / "target").iterdir())


@pytest.mark.parametrize("name", JSON_LEVEL)
def test_update_status_ignores_a_hostile_state_file(tmp_path: Path, name: str) -> None:
    state = tmp_path / "state" / "agent-si"
    state.mkdir(parents=True)
    (state / "update.json").write_bytes(HOSTILE[name])
    env = {"XDG_STATE_HOME": str(tmp_path / "state"), "XDG_CONFIG_HOME": str(tmp_path / "config"), "HOME": str(tmp_path)}
    done = _brain("update", "--status", env=env)
    _clean(done, {0}, f"update --status {name}")
    assert json.loads(done.stdout)["state"] == {}


@pytest.mark.parametrize("name", JSON_LEVEL)
def test_brain_fetch_readers_refuse_hostile_json(tmp_path: Path, name: str) -> None:
    from brain.redact import delete_exports
    from brain_fetch import state, transcripts
    from brain_fetch.readers import ReadError

    body = HOSTILE[name]
    inst = tmp_path / "inst"
    (inst / "fetch" / "inbox" / "slack").mkdir(parents=True)
    (inst / "fetch" / "slack.state.json").write_bytes(body)
    assert state.read_cursor(inst, "slack") == {}
    assert state.read_state(inst, "slack", "slack") == {}
    export = inst / "fetch" / "inbox" / "slack" / "20261017T000000Z-01.json"
    export.write_bytes(body)
    unchecked: list[str] = []
    assert delete_exports(inst, ["ref-1"], unchecked) == []
    assert unchecked == [str(export.relative_to(inst))]
    with pytest.raises(ReadError):
        transcripts.read_json(body.decode("utf-8", "replace"))
