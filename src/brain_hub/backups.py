"""The hub's scheduled backups: `brain backup` every 15 minutes when write_log or
the audit moved, and once a day regardless, into the backups folder. The hub's own database (tokens and audit) is
copied beside each bundle with SQLite's online backup API, so the bundle format and its checks don't change.

Pruning, in the same job: every bundle for 24 hours, then the newest of each day for 35 days, and every pre-update
bundle for 35 days.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from brain import backup as backup_mod, config as config_mod, store
from brain_hub import tokens

EVERY = timedelta(minutes=15)
DAILY = timedelta(hours=24)
KEEP_ALL = timedelta(hours=24)
KEEP_DAYS = timedelta(days=35)


def _parse(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _bundle_time(path: Path) -> datetime | None:
    """The UTC stamp in <name>-<utc>-seq<n>[-label].tar.gz."""
    for part in path.name.split("-"):
        if len(part) == 16 and part.endswith("Z") and "T" in part:
            try:
                return datetime.strptime(part, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                return None
    return None


class Backups:
    def __init__(self, instance, out):
        self.instance = Path(instance)
        self.out = Path(out)
        self.last_seq: int | None = None
        self.last_audit: int | None = None
        self.last_at: datetime | None = None

    def _moved(self) -> tuple[int, int]:
        con = store.connect_readonly(self.instance / "brain.sqlite3")
        try:
            seq = backup_mod.log_digest(con)[0]
        finally:
            con.close()
        hub = tokens.connect(self.instance)
        try:
            audit = hub.execute("SELECT coalesce(max(id), 0) FROM audit").fetchone()[0]
        finally:
            hub.close()
        return seq, audit

    def tick(self) -> dict | None:
        """One scheduled run: a backup when anything moved since the last one, or a day has passed. None otherwise."""
        now = _parse(store.utc_now())
        seq, audit = self._moved()
        due = (self.last_at is None or seq != self.last_seq or audit != self.last_audit or now - self.last_at >= DAILY)
        if not due:
            return None
        self.out.mkdir(parents=True, exist_ok=True)
        made = backup_mod.backup(self.instance, self.out)
        self._copy_hub(Path(made["bundle"]))
        tokens.audit(self.instance, "backup", token_id="shell", detail=f"applied_seq {made['applied_seq']}")
        # The audit row above moved the audit: count it in, so the next tick doesn't back up for its own row.
        self.last_seq, self.last_audit, self.last_at = seq, self._moved()[1], now
        made["pruned"] = self.prune(now)
        return made

    def _copy_hub(self, bundle: Path) -> None:
        source = sqlite3.connect(str(self.instance / "hub.sqlite3"))
        target_path = bundle.with_name(bundle.name.replace(".tar.gz", ".hub.sqlite3"))
        target = sqlite3.connect(str(target_path))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        target_path.chmod(0o600)

    def prune(self, now: datetime) -> list[str]:
        name = config_mod.load_config(self.instance / "brain.toml").name
        bundles = sorted((path for path in backup_mod.list_bundles(self.out, name) if _bundle_time(path)),
                         key=lambda path: _bundle_time(path))
        keep: set[Path] = set()
        newest_of_day: dict[str, Path] = {}
        for path in bundles:
            at = _bundle_time(path)
            age = now - at
            if age <= KEEP_ALL:
                keep.add(path)
            elif age <= KEEP_DAYS:
                if path.name.endswith("-pre-update.tar.gz"):
                    keep.add(path)
                newest_of_day[at.strftime("%Y-%m-%d")] = path  # sorted oldest first, so the last one wins
        keep |= set(newest_of_day.values())
        if bundles:
            keep.add(bundles[-1])  # never prune the only good copy
        removed = []
        for path in bundles:
            if path not in keep:
                for doomed in (path, path.with_name(path.name.replace(".tar.gz", ".hub.sqlite3"))):
                    if doomed.exists():
                        doomed.unlink()
                removed.append(path.name)
        return removed


def run_forever(instance, out, stop: threading.Event, every: timedelta = EVERY) -> None:
    job = Backups(instance, out)
    while not stop.is_set():
        try:
            job.tick()
        except Exception as exc:  # noqa: BLE001 - a failed backup is logged and retried next tick, never fatal
            print(f"hub backup failed ({type(exc).__name__}); trying again in {int(every.total_seconds() // 60)} minutes")
        stop.wait(every.total_seconds())
