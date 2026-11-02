"""brain schedule off: remove the nightly job an earlier version installed. Nothing here
installs a job any more, because this build never looks for releases by itself: `brain schedule on` refuses,
and new releases are on the releases page (brain.advice.updates_elsewhere_error). The tests replace LAUNCHCTL
and Path.home(); nothing in the tests runs launchctl."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

LAUNCHCTL = "/bin/launchctl"
LABEL = "ai.agent-si.update"


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _launchctl(*args: str) -> int:
    return subprocess.run([LAUNCHCTL, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, check=False).returncode


def off() -> dict:
    path = _plist_path()
    if sys.platform == "darwin":
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if path.exists():
        path.unlink()
    return {"status": "removed", "label": LABEL, "path": str(path)}
