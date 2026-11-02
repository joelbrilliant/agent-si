"""brain update: stop writes, back up every brain, upgrade, migrate, check, and on any
failure from the upgrade on, put the old engine and the pre-update backup back in the same run.

The upgrade is always from a wheel the person downloaded from the releases page (--from-wheel). Nothing here
looks for releases or fetches a package by name, and the old engine is put back from a wheel kept before the
upgrade. The new engine is always reached through the installed command, never imported: uv replaces the files
this process was loaded from. Exits: 0 updated or current; 1 refused; 5 failed (rolled back, paused, or before
anything changed).
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path

from brain import __version__, advice, backup as backup_mod, config as config_mod, registry, store, versions
from brain.applier import UPDATING

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX only
    fcntl = None

PYTHON = "3.13"
TIMEOUT = 600
# The public name since 1.0; the working name before it, so an older install can still be read and updated.
_VERSION_LINE = re.compile(r"agent-(?:si|brain) (\S+) \(schema \d+\)")


class Refused(Exception):
    pass


class Failed(Exception):
    def __init__(self, message: str, receipt: dict):
        super().__init__(message)
        self.receipt = receipt


def package() -> str:
    import importlib.metadata

    try:
        return (importlib.metadata.packages_distributions().get("brain") or ["agent-si"])[0]
    except Exception:  # noqa: BLE001 - no metadata in a source checkout
        return "agent-si"


def _uv() -> str:
    found = shutil.which("uv")
    if found is None:
        raise Refused("uv isn't on PATH; brain update installs through uv")
    return found


def tool_bin() -> Path:
    explicit = os.environ.get("UV_TOOL_BIN_DIR")
    if explicit:
        return Path(explicit)
    done = _run([_uv(), "tool", "dir", "--bin"], timeout=60)
    if done.returncode != 0 or not done.stdout.strip():
        raise Refused("uv tool dir --bin failed; is uv working?")
    return Path(done.stdout.strip())


def _run(argv: list[str], *, timeout: int = TIMEOUT, stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"{type(exc).__name__}: {exc}")


@contextlib.contextmanager
def _lock():
    if fcntl is None:
        raise Refused("brain update needs fcntl file locks")
    folder = registry.state_dir()
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / "update.lock", "a+b")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("another brain update is running") from exc
        yield
    finally:
        handle.close()


def _instances(given) -> list[Path]:
    """Every registered instance and every --instance. A registered folder that no longer exists was deleted:
    it's skipped and the update log says so, so one removed brain can't stop updates for the rest. A folder
    that exists but isn't an instance, or a missing --instance, still refuses the run."""
    found: list[Path] = []
    explicit = [Path(item).resolve() for item in given or ()]
    for path in registry.instances():
        if not path.exists() and path not in explicit:
            registry.log(f"skipped {path}: registered, but the folder is gone")
            continue
        if path not in found:
            found.append(path)
    for path in explicit:
        if path not in found:
            found.append(path)
    if not found:
        raise Refused(f"no instances: register one in {registry.registry_path()} or pass --instance")
    for path in found:
        if not (path / "brain.toml").is_file() or not (path / "brain.sqlite3").is_file():
            raise Refused(f"{path} isn't an instance (no brain.toml or brain.sqlite3)")
    return found


def _configs(instances: list[Path]) -> list:
    try:
        return [config_mod.load_config(path / "brain.toml") for path in instances]
    except config_mod.ConfigError as exc:
        raise Refused(f"brain.toml: {exc}") from exc


def installed(brain: Path) -> str | None:
    done = _run([str(brain), "--version"], timeout=60)
    match = _VERSION_LINE.fullmatch(done.stdout.strip()) if done.returncode == 0 else None
    return match.group(1) if match else None


def _reinstall_line(name: str, version: str) -> str:
    return (f"download the {name} {version} wheel from {advice.RELEASES_URL}, then run: "
            f"uv tool install --reinstall --managed-python --python {PYTHON} PATH-TO-THE-WHEEL")


def _target(opts: dict) -> str:
    wheel = Path(opts["from_wheel"])
    target = versions.from_wheel(wheel.name)
    if target is None or not wheel.is_file():
        raise Refused(f"{wheel} isn't a wheel brain update can read")
    return target


_NOT_RECORDED = {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"}


def snapshot_wheel(name: str, folder: Path) -> Path | None:
    """Pack the installed copy of `name` back into a wheel in `folder`, so a failed update can put it back
    without fetching anything. None when the install isn't a plain one (an editable or source install)."""
    import importlib.metadata

    try:
        dist = importlib.metadata.distribution(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    entries: list[tuple[str, Path]] = []
    for item in dist.files or ():
        if item.suffix == ".pth":
            return None
        if item.parts[0] == ".." or "__pycache__" in item.parts or item.suffix == ".pyc":
            continue
        if item.parts[0].endswith(".dist-info") and item.name in _NOT_RECORDED:
            continue
        source = Path(dist.locate_file(item))
        if source.is_file():
            entries.append((item.as_posix(), source))
    if "brain/__init__.py" not in {arc for arc, _ in entries}:
        return None
    tag = "py3-none-any"
    for arc, source in entries:
        if arc.endswith(".dist-info/WHEEL"):
            tag = next((line.split(":", 1)[1].strip() for line in source.read_text(encoding="utf-8").splitlines()
                        if line.startswith("Tag:")), tag)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{name.replace('-', '_')}-{dist.version}-{tag}.whl"
    record = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as wheel:
        for arc, source in sorted(entries):
            data = source.read_bytes()
            wheel.writestr(arc, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{arc},sha256={digest},{len(data)}")
        info = next(arc.split("/", 1)[0] for arc, _ in entries if ".dist-info/" in arc)
        record.append(f"{info}/RECORD,,")
        wheel.writestr(f"{info}/RECORD", "\n".join(record) + "\n")
    return target


def _rollback_wheel(name: str, current: str, opts: dict) -> Path:
    """The wheel of the version running now, kept before anything changes so a failed update can go back to it."""
    if opts.get("rollback_from"):
        given = Path(opts["rollback_from"])
        found = versions.from_wheel(given.name)
        if found != current or not given.is_file():
            raise Refused(advice.rollback_version_error(str(given), found, current))
        return given.resolve()
    kept = snapshot_wheel(name, registry.state_dir() / "previous")
    if kept is None:
        raise Refused(advice.rollback_wheel_error(name, current))
    return kept


def _drain(db: Path) -> None:
    con = sqlite3.connect(str(db), isolation_level=None, timeout=30.0)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("ROLLBACK")
    finally:
        con.close()


def _applied_seq(db: Path) -> int:
    con = store.connect_readonly(db)
    try:
        return backup_mod.log_digest(con)[0]
    finally:
        con.close()


def _smoke(brain: Path, instance: Path) -> str:
    """initialize, tools/list and a brief over the installed serve: the empty string when all three answer."""
    lines = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
             {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "brief", "arguments": {}}}]
    done = _run([str(brain), "serve", "--instance", str(instance)], timeout=120,
                stdin="".join(json.dumps(line) + "\n" for line in lines))
    try:
        answers = {item.get("id"): item for item in map(json.loads, done.stdout.splitlines()) if isinstance(item, dict)}  # trusted: the output of the brain serve this function just ran
    except ValueError:
        return "serve answered with something that isn't JSON"
    tools = [tool.get("name") for tool in (answers.get(2) or {}).get("result", {}).get("tools", [])]
    brief = (answers.get(3) or {}).get("result") or {}
    if "result" not in (answers.get(1) or {}) or "brief" not in tools or brief.get("isError") or not brief.get("content"):
        return "the MCP smoke test failed (initialize, tools/list or brief)"
    return ""


def _check(brain: Path, instance: Path) -> str:
    done = _run([str(brain), "check", "--instance", str(instance)], timeout=TIMEOUT)
    return "" if done.returncode == 0 else f"brain check failed: {(done.stderr or done.stdout).strip()[:200]}"


def _install(wheel: Path | str) -> str:
    argv = [_uv(), "tool", "install", "--reinstall", "--managed-python", "--python", PYTHON, str(wheel)]
    done = _run(argv)
    return "" if done.returncode == 0 else f"uv tool install failed: {(done.stderr or done.stdout).strip()[-300:]}"


def _write_marker(path: Path, payload: dict) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(store.canonical_json(payload) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _remove(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(given) -> dict:
    """The last result, and each instance's markers. An UPDATING whose process has gone is named stale: an
    update stopped part way (a power cut, a killed process), and the README says what to do."""
    found = []
    for path in [*registry.instances(), *(Path(item).resolve() for item in given or ())]:
        entry = {"instance": str(path), "updating": (path / UPDATING).exists(), "paused": (path / "PAUSED").exists(),
                 "missing": not path.exists()}
        if entry["updating"]:
            try:
                marker = json.loads((path / UPDATING).read_text(encoding="utf-8"))  # trusted: the UPDATING marker, written by brain update
            except (OSError, ValueError):
                marker = {}
            entry["updating_stale"] = not _alive(marker.get("pid") if isinstance(marker, dict) else None)
        found.append(entry)
    return {"state": registry.read_state(), "instances": found}


def check_releases(given) -> tuple[int, dict]:
    """Says what is installed and where releases are. It makes no network call: there is no release source to ask."""
    try:
        current = installed(tool_bin() / "brain") or __version__
    except Refused:
        current = __version__
    return 0, {"result": "not_checked", "current": current, "message": advice.updates_elsewhere_error("brain update --check")}


def update(given, opts: dict) -> tuple[int, dict]:
    """The whole run, under the machine lock. Returns (exit code, receipt)."""
    if opts.get("to") or opts.get("auto") or not opts.get("from_wheel"):
        raise Refused(advice.updates_elsewhere_error(
            "brain update --to" if opts.get("to") else "brain update --auto" if opts.get("auto") else "brain update"))
    with _lock():
        try:
            return _update(given, opts)
        except Failed as exc:
            return 5, exc.receipt


def _update(given, opts: dict) -> tuple[int, dict]:
    instances = _instances(given)
    _configs(instances)
    name = package()
    brain = tool_bin() / "brain"
    if not brain.is_file():
        raise Refused(f"brain isn't installed as a uv tool (no {brain}); install it with: "
                      f"{_reinstall_line(name, __version__).replace(' --reinstall', '')}")
    current = installed(brain)
    if current is None:
        raise Refused(f"the installed brain doesn't start; reinstall it with: {_reinstall_line(name, __version__)}")
    for path in instances:
        for marker, words in (("PAUSED", "is paused: investigate PAUSED first"),
                              (UPDATING, "has UPDATING: an update is running or stopped part way; see brain update --status")):
            if (path / marker).exists():
                raise Refused(f"{path} {words}")
        problem = _check(brain, path)
        if problem:
            raise Refused(f"{path}: {problem}")
        size = (path / "brain.sqlite3").stat().st_size
        if shutil.disk_usage(path).free < 2 * size:
            raise Refused(f"{path}: free disk is under twice the database ({size} bytes)")
    target = _target(opts)
    if target == current:
        return 0, {"result": "current", "from": current, "to": current, "instances": [], "message": ""}
    if not versions.newer(target, current):
        raise Refused(f"{target} is older than {current}; brain update doesn't downgrade")
    previous = _rollback_wheel(name, current, opts)  # before anything is stopped or changed
    receipt = {"result": "", "from": current, "to": target, "instances": [], "steps": [], "message": ""}
    rows = {str(path): {"instance": str(path), "result": "", "backup": "", "applied_seq": None} for path in instances}
    receipt["instances"] = list(rows.values())
    marked: list[Path] = []
    try:
        for path in instances:  # 2. stop writes, then drain any writer already past its preconditions
            _write_marker(path / UPDATING, {"from": current, "to": target, "at": store.utc_now(), "pid": os.getpid()})
            marked.append(path)
        for path in instances:
            _drain(path / "brain.sqlite3")
        receipt["steps"].append("writes stopped")
        for path in instances:  # 3. back up, and prove no write slipped through
            made = backup_mod.backup(path, path / "backups", label="pre-update")
            rows[str(path)].update(backup=made["bundle"], applied_seq=made["applied_seq"])
            if _applied_seq(path / "brain.sqlite3") != made["applied_seq"]:
                raise RuntimeError(f"{path}: a write landed after the backup")
        receipt["steps"].append("backed up")
    except Exception as exc:  # noqa: BLE001 - nothing has changed yet: lift the stop and say why
        for path in marked:
            _remove(path / UPDATING)
        receipt.update(result="failed_before_upgrade", message=f"{type(exc).__name__}: {exc}"[:300])
        _record(receipt, target)
        raise Failed(receipt["message"], receipt) from None
    migrated: list[Path] = []
    problem = _install(Path(opts["from_wheel"]).resolve())  # 4. upgrade
    if not problem:
        receipt["steps"].append("installed")
        for path in instances:  # 5. migrate with the new engine
            migrated.append(path)
            done = _run([str(brain), "migrate", "--instance", str(path)])
            if done.returncode != 0:
                problem = f"{path}: brain migrate failed: {(done.stderr or done.stdout).strip()[:200]}"
                break
    if not problem:
        receipt["steps"].append("migrated")
        now_version = installed(brain)  # 6. check the new engine end to end
        if now_version != target:
            problem = f"the installed brain reports {now_version or 'nothing'}, not {target}"
        for path in instances:
            if problem:
                break
            problem = _check(brain, path)
            if not problem and _run([str(brain), "router", "--instance", str(path)]).returncode != 0:
                problem = f"{path}: brain router failed"
            problem = problem or _smoke(brain, path)
    if problem:
        return _rollback(receipt, rows, instances, migrated, brain, previous, current, target, problem)
    for path in instances:  # 7. finish
        _remove(path / UPDATING)
        rows[str(path)]["result"] = "updated"
    receipt.update(result="updated")
    receipt["steps"].append("checked")
    _record(receipt, target)
    return 0, receipt


def _rollback(receipt, rows, instances, migrated, brain, previous, current, target, problem):
    stamp = store.utc_now().replace("-", "").replace(":", "")
    reinstalled = not _install(previous) and installed(brain) == current
    paused = False
    for path in instances:
        row = rows[str(path)]
        db = path / "brain.sqlite3"
        if not reinstalled:
            reason = f"brain update to {target} failed ({problem}) and the reinstall of {current} failed"
            _pause(path, reason, row["backup"], [])
            row["result"], paused = "paused", True
            continue
        moved: list[str] = []
        if path in migrated:
            # E8: the database and any hot journal move aside together, or the journal would replay onto the
            # restored file.
            for suffix in ("", "-journal"):
                source = path / f"brain.sqlite3{suffix}"
                if source.exists():
                    aside = path / f"broken-{stamp}.sqlite3{suffix}"
                    os.replace(source, aside)
                    moved.append(str(aside))
            staging = path / f".update-restore-{stamp}"
            try:
                backup_mod.restore(row["backup"], staging)
                os.replace(staging / "brain.sqlite3", db)
                if (staging / "ROUTER.md").is_file():  # the new engine's router may already have been written
                    os.replace(staging / "ROUTER.md", path / "ROUTER.md")
            except Exception as exc:  # noqa: BLE001 - a restore that fails is a pause, with what was tried
                _pause(path, f"restore of {row['backup']} failed after a failed update ({exc})", row["backup"], moved)
                row["result"], paused = "paused", True
                continue
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        trouble = _check(brain, path)
        if trouble:
            _pause(path, f"after rolling back to {current}: {trouble}", row["backup"], moved)
            row["result"], paused = "paused", True
            continue
        _remove(path / UPDATING)
        row["result"] = "rolled_back"
    receipt.update(result="paused" if paused else "rolled_back", message=problem[:300])
    _record(receipt, target)
    raise Failed(problem, receipt)


def _pause(path: Path, reason: str, bundle: str, moved: list[str]) -> None:
    _write_marker(path / "PAUSED", {"reason": reason, "pre_update_backup": bundle, "moved_aside": moved,
                                    "at": store.utc_now(), "by": "brain update"})
    _remove(path / UPDATING)


def _record(receipt: dict, target: str) -> None:
    changes = {"last_result": receipt["result"], "last_target": target, "last_run_at": store.utc_now()}
    if receipt["result"] == "updated":
        changes["current"] = target
    registry.write_state(changes)
    registry.log(f"manual: {receipt['result']} {receipt['from']} -> {target}"
                 + (f" ({receipt['message']})" if receipt.get("message") else ""))
