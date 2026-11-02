#!/usr/bin/env python3
"""Build the one-file installer: agent-si and agent-memory in one self-extracting sh file.

    python3 tools/bundle.py --memory PATH-TO-AGENT-MEMORY-CHECKOUT --out dist/

It builds a wheel of this checkout and of the agent-memory checkout (each from a clean copy, so a stale build folder
can't leak into it), writes SHA256SUMS for both, and appends a gzipped tar of the three files to the install script
(tools/bundle-install.sh) after a marker line. The script's first lines hold the header's length in bytes, so it
finds the payload without scanning binary. The file is written twice with the same bytes: as
install-agent-si-<version>.sh, and as install-agent-si.sh, so that releases/latest/download/install-agent-si.sh is a
stable address. A SHA256SUMS file beside them lists the stable name, and the file's own SHA-256 is printed so it can
be checked after it is copied. Nothing here talks to a package index by name: `uv build` fetches only the build
backend a package asks for, and nothing is published.

The same functions build the bundles in tests/test_bundle.py, including the damaged ones.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "tools" / "bundle-install.sh"
STABLE = "install-agent-si.sh"
MARKER = "__AGENT_SI_BUNDLE_PAYLOAD__"
PLACEHOLDER = "@HEADER_BYTES@"
_IGNORED = (".git", ".scratch", ".venv", "venv", "build", "dist", "*.egg-info", "__pycache__", ".pytest_cache", ".mypy_cache")
_WHEEL = re.compile(r"^(?P<dist>[A-Za-z0-9_]+)-(?P<version>[0-9][^-]*)-py3-none-any\.whl$")


class BundleError(Exception):
    pass


def build_wheel(source: Path, dest: Path) -> Path:
    """The wheel of a source tree, built from a clean copy of it with `uv build --wheel`."""
    source = Path(source).resolve()
    if not (source / "pyproject.toml").is_file():
        raise BundleError(f"{source} has no pyproject.toml; pass the folder of the package's checkout")
    uv = shutil.which("uv")
    if uv is None:
        raise BundleError("uv isn't on PATH; install uv (https://docs.astral.sh/uv/) to build the bundle")
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bundle-src-") as scratch:
        copy = Path(scratch) / source.name
        shutil.copytree(source, copy, ignore=shutil.ignore_patterns(*_IGNORED), symlinks=True)
        done = subprocess.run([uv, "build", "--wheel", "--out-dir", str(dest), str(copy)], capture_output=True, text=True,
                              check=False, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise BundleError(f"uv build failed for {source}:\n{(done.stderr or done.stdout).strip()[-800:]}")
    wheels = sorted(dest.glob("*.whl"), key=lambda path: path.stat().st_mtime)
    if not wheels:
        raise BundleError(f"uv build made no wheel for {source}")
    return wheels[-1]


def sums(wheels: dict[str, bytes]) -> str:
    """SHA256SUMS text in sha256sum's form, one line per wheel in name order."""
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(wheels.items()))


def payload(files: dict[str, bytes]) -> bytes:
    """A gzipped tar of flat files, the same bytes for the same files (no times, owners or gzip name)."""
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name in sorted(files):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime, info.uid, info.gid = len(files[name]), 0o644, 0, 0, 0
                tar.addfile(info, io.BytesIO(files[name]))
    return raw.getvalue()


def header(template: str) -> bytes:
    """The script with its own length filled in. The length counts the marker line and its newline, and the number
    is written without padding, so the loop finds the digits count that makes the file consistent."""
    if template.count(PLACEHOLDER) != 1 or not template.endswith(MARKER + "\n"):
        raise BundleError(f"the script must hold {PLACEHOLDER} once and end with the {MARKER} line")
    base = len(template.replace(PLACEHOLDER, "").encode("utf-8"))
    for digits in range(1, 10):
        total = base + digits
        if len(str(total)) == digits:
            return template.replace(PLACEHOLDER, str(total)).encode("utf-8")
    raise BundleError("the script is too long to number")


def assemble(template: str, wheels: dict[str, bytes]) -> bytes:
    """The whole bundle for wheels given by file name: script, marker line, tar of the wheels and their SHA256SUMS."""
    files = {**wheels, "SHA256SUMS": sums(wheels).encode("ascii")}
    return header(template) + payload(files)


def build(memory: Path, out: Path, engine: Path = ROOT) -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bundle-wheels-") as scratch:
        si = build_wheel(engine, Path(scratch) / "si")
        mem = build_wheel(memory, Path(scratch) / "memory")
        names = {}
        for wheel, dist in ((si, "agent_si"), (mem, "agent_memory")):
            found = _WHEEL.match(wheel.name)
            if not found or found["dist"] != dist:
                raise BundleError(f"expected a {dist} wheel, got {wheel.name}")
            names[wheel.name] = wheel.read_bytes()
        version = _WHEEL.match(si.name)["version"]
    data = assemble(TEMPLATE.read_text(encoding="utf-8"), names)
    target = out / f"install-agent-si-{version}.sh"
    stable = out / STABLE
    for path in (target, stable):
        path.write_bytes(data)
        path.chmod(0o755)
    (out / "SHA256SUMS").write_bytes(sums({STABLE: data}).encode("ascii"))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the one-file agent-si and agent-memory installer.")
    parser.add_argument("--memory", required=True, help="the agent-memory checkout (a folder with a pyproject.toml)")
    parser.add_argument("--out", required=True, help="the folder to write install-agent-si.sh (and a versioned copy) into")
    ns = parser.parse_args(argv)
    try:
        target = build(Path(ns.memory), Path(ns.out))
    except BundleError as exc:
        print(f"bundle.py: {exc}", file=sys.stderr)
        return 1
    data = target.read_bytes()
    print(f"bundle: {target.resolve()}")
    print(f"stable name: {(target.parent / STABLE).resolve()} (the same bytes)")
    print(f"checksums: {(target.parent / 'SHA256SUMS').resolve()}")
    print(f"size: {len(data)} bytes")
    print(f"sha256: {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
