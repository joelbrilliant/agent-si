"""brain-fetch schedule: one launchd job for the instance on a Mac, systemd units
printed elsewhere. The tests replace LAUNCHCTL and Path.home(); nothing in the tests runs launchctl."""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

from brain.applier import Unreadable

LAUNCHCTL = "/bin/launchctl"
PATH_ENV = "/usr/bin:/bin:/usr/sbin:/sbin"


def label_for(instance_name: str) -> str:
    return f"ai.agent-si.fetch.{instance_name}"


def program_arguments(instance: Path) -> list[str]:
    start = os.path.abspath(sys.argv[0])
    head = [start] if os.path.basename(start) == "brain-fetch" else [sys.executable, "-m", "brain_fetch"]
    return [*head, "run", "--instance", str(Path(instance).resolve())]


def plist(instance: Path, instance_name: str, minutes: int) -> dict:
    log = str(Path(instance).resolve() / "fetch" / "run.log")
    return {
        "Label": label_for(instance_name),
        "ProgramArguments": program_arguments(instance),
        "StartInterval": minutes * 60,
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": PATH_ENV},
    }


def systemd_units(instance: Path, instance_name: str, minutes: int) -> str:
    command = " ".join(shlex.quote(part) for part in program_arguments(instance))
    unit = f"agent-si-fetch-{instance_name}"
    return (
        f"# ~/.config/systemd/user/{unit}.service\n[Unit]\nDescription=brain-fetch for {instance_name}\n\n"
        f"[Service]\nType=oneshot\nExecStart={command}\n\n"
        f"# ~/.config/systemd/user/{unit}.timer\n[Unit]\nDescription=brain-fetch every {minutes} minutes\n\n"
        f"[Timer]\nOnBootSec=5min\nOnUnitActiveSec={minutes}min\nPersistent=true\n\n"
        "[Install]\nWantedBy=timers.target\n\n"
        f"# then: systemctl --user enable --now {unit}.timer\n"
    )


def _launchctl(*args: str) -> int:
    return subprocess.run(
        [LAUNCHCTL, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    ).returncode


def _plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _installed_for(path: Path):
    """The instance path an existing job file runs, or None when there is no readable one."""
    try:
        arguments = plistlib.loads(path.read_bytes())["ProgramArguments"]
        return arguments[arguments.index("--instance") + 1]
    except Exception:  # noqa: BLE001 - any unreadable job file is simply not ours to compare with
        return None


def install(instance: Path, instance_name: str, minutes: int) -> dict:
    label = label_for(instance_name)
    path = _plist_path(label)
    where = str(Path(instance).resolve())
    other = _installed_for(path)
    if other is not None and other != where:
        raise Unreadable(
            f"a job named {label} is already installed for {other}; "
            "remove it with brain-fetch schedule --off on that instance first"
        )
    (Path(instance) / "fetch").mkdir(mode=0o700, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        plistlib.dump(plist(instance, instance_name, minutes), handle)
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{label}")
    if _launchctl("bootstrap", domain, str(path)) != 0:
        path.unlink()  # no half-installed job to retry over
        raise OSError(f"launchctl bootstrap failed for {label}")
    return {"status": "installed", "label": label, "path": str(path), "every_minutes": minutes}


def remove(instance_name: str, minutes: int) -> dict:
    label = label_for(instance_name)
    path = _plist_path(label)
    _launchctl("bootout", f"gui/{os.getuid()}/{label}")
    if path.exists():
        path.unlink()
    return {"status": "removed", "label": label, "path": str(path), "every_minutes": minutes}
