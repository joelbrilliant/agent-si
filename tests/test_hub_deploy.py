"""The hub's scheduled backups and their pruning, the no-brain mode, and the deploy files' shape. The
Docker acceptance itself (a clean VM, a reboot, a test bucket) is run by hand; these tests hold the files to it."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain import store
from brain.applier import Applier
from brain_hub import backups, server as hub_server, tokens
from tests.conftest import make_packet
from tests.phase2a import custom_instance
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "hub"


def _write(inst: Path, sid: str) -> None:
    writer = Applier(inst)
    try:
        packet = make_packet("tester", {"op": "add_source", "row": {"id": sid, "kind": "user_note", "title": "Note",
                                                                     "content": "Pilot starts Monday.", "source_date": "2026-10-09"}})
        assert writer.apply(json.dumps(packet).encode()).exit_code == 0
    finally:
        writer.close()


def test_backups_run_when_something_moved_and_once_a_day(tmp_path, clock: Clock) -> None:
    inst = custom_instance(tmp_path / "inst")
    tokens.connect(inst).close()
    job = backups.Backups(inst, tmp_path / "backups")
    first = job.tick()
    assert first is not None and Path(first["bundle"]).is_file()
    assert Path(first["bundle"].replace(".tar.gz", ".hub.sqlite3")).is_file()
    clock.at("2026-10-09T00:15:00Z")
    assert job.tick() is None  # nothing moved, and its own audit row doesn't count
    _write(inst, "src-bk-1")
    clock.at("2026-10-09T00:30:00Z")
    assert job.tick()["applied_seq"] == first["applied_seq"] + 1
    clock.at("2026-10-10T00:31:00Z")
    assert job.tick() is not None  # a day later, regardless
    rows = [row["action"] for row in tokens.audit_rows(inst, "2000-01-01")]
    assert rows.count("backup") == 3


def _bundle(folder: Path, at: datetime, seq: int, label: str = "") -> Path:
    path = folder / f"demo-{at.strftime('%Y%m%dT%H%M%SZ')}-seq{seq}{'-' + label if label else ''}.tar.gz"
    path.write_bytes(b"x")
    path.with_name(path.name.replace(".tar.gz", ".hub.sqlite3")).write_bytes(b"x")
    return path


def test_pruning_keeps_a_day_of_everything_then_one_a_day(tmp_path) -> None:
    inst = custom_instance(tmp_path / "inst")
    out = tmp_path / "backups"
    out.mkdir()
    now = datetime(2026, 10, 12, 6, 0, tzinfo=timezone.utc)
    recent = [_bundle(out, now - timedelta(minutes=15 * n), 100 - n) for n in range(4)]
    day3 = [_bundle(out, now - timedelta(days=3, hours=h), 50 - h) for h in range(3)]
    pre = _bundle(out, now - timedelta(days=10, hours=5), 30, "pre-update")
    other_day10 = _bundle(out, now - timedelta(days=10, hours=1), 31)
    old = _bundle(out, now - timedelta(days=40), 1)
    removed = backups.Backups(inst, out).prune(now)
    left = {path.name for path in out.glob("*.tar.gz")}
    assert all(path.name in left for path in recent)
    assert day3[0].name in left and day3[1].name not in left and day3[2].name not in left  # the newest of the day
    assert pre.name in left and other_day10.name in left and old.name not in left
    assert sorted(removed) == sorted([day3[1].name, day3[2].name, old.name])
    assert not old.with_name(old.name.replace(".tar.gz", ".hub.sqlite3")).exists()  # its hub copy goes with it


def test_the_only_bundle_is_never_pruned(tmp_path) -> None:
    inst = custom_instance(tmp_path / "inst")
    out = tmp_path / "backups"
    out.mkdir()
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    only = _bundle(out, now - timedelta(days=90), 1)
    assert backups.Backups(inst, out).prune(now) == [] and only.exists()


def test_with_no_brain_the_hub_answers_health_only(tmp_path) -> None:
    httpd = hub_server.health_only("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert urllib.request.urlopen(url + "/healthz", timeout=10).status == 200
        for path in ("/readyz", "/v1/whoami"):
            try:
                urllib.request.urlopen(url + path, timeout=10)
                raise AssertionError(path)
            except urllib.error.HTTPError as exc:
                assert exc.code == 503 and "no brain: run brain init or brain restore" in exc.read().decode()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_dockerfile_matches_the_spec() -> None:
    text = "\n".join(line for line in (DEPLOY / "Dockerfile").read_text().splitlines() if not line.startswith("#"))
    assert re.search(r"^FROM python:3\.13-slim@sha256:[0-9a-f]{64}$", text, re.M)
    assert not re.search(r"^ENTRYPOINT", text, re.M) and "USER brain" in text and "brain --version" in text
    assert 'CMD ["brain", "hub", "serve", "--instance", "/data/brain", "--listen", "0.0.0.0:8080", "--backups", "/backups"]' in text
    assert "HEALTHCHECK --interval=30s --timeout=5s --start-period=30s" in text and "urllib.request.urlopen" in text
    assert "curl" not in text.split("HEALTHCHECK", 1)[1] and "wget" not in text


def test_the_compose_file_matches_the_spec() -> None:
    text = (DEPLOY / "docker-compose.yml").read_text()
    services = re.findall(r"^  ([a-z]+):\n", text.split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0], re.M)
    assert services == ["brain", "fetch", "caddy", "offsite"]
    assert text.count("restart: unless-stopped") == 3 and "<<: *brain" in text  # brain and fetch share the anchor's
    brain_block = text.split("  brain:\n", 1)[1].split("\n  fetch:\n", 1)[0]
    assert "ports" not in brain_block and 'expose: ["8080"]' in brain_block
    assert '"80:80", "443:443"' in text and "caddy-data:/data" in text and "caddy-config:/config" in text
    assert 'profiles: ["offsite"]' in text and "rclone copy --immutable --s3-no-check-bucket" in text
    for image in re.findall(r"image: (\S+)", text):
        assert image.startswith("${BRAIN_IMAGE") or re.search(r"@sha256:[0-9a-f]{64}$", image), image
    assert "read_only: true" in text and "XDG_STATE_HOME: /data/state" in text


def test_the_caddyfile_and_the_host_script() -> None:
    caddy = (DEPLOY / "Caddyfile").read_text().splitlines()
    assert caddy[0] == "{$BRAIN_HOST} {" and "        max_size 6MB" in caddy and "    reverse_proxy brain:8080" in caddy
    assert '    header Strict-Transport-Security "max-age=31536000"' in caddy and "    header -Server" in caddy
    script = DEPLOY / "hub"
    assert subprocess.run(["sh", "-n", str(script)]).returncode == 0 and script.stat().st_mode & 0o111
    text = script.read_text()
    for word in ("update)", "status)", "prune-offsite)", "--image", "pre-update", ".env.previous", "/readyz"):
        assert word in text, word
    done = subprocess.run(["sh", str(script)], capture_output=True, text=True)
    assert done.returncode == 2 and "usage: ./hub update | status | prune-offsite" in done.stderr
    env = (DEPLOY / "env.example").read_text()  # not .env.example: the repo never tracks a .env file
    for key in ("BRAIN_HOST=", "BRAIN_IMAGE=", "BRAIN_CHANNEL=", "RCLONE_CONFIG="):
        assert key in env


def test_hub_md_holds_the_runbook() -> None:
    text = (ROOT / "docs" / "HUB.md").read_text()
    for needle in ("sha256sum -c SHA256SUMS", "brain hub token", "./hub update", "brain restore", "brain redact",
                   "prune-offsite", "--immutable", "versioning", "Recovery point"):
        assert needle in text, needle
