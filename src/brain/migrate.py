from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from brain import store

_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    pass


def _package_migrations() -> list[tuple[int, str, str]]:
    from importlib.resources import files

    root = files("brain").joinpath("migrations")
    found: list[tuple[int, str, str]] = []
    for item in root.iterdir():
        match = _MIGRATION_NAME.fullmatch(item.name)
        if not match:
            continue
        found.append((int(match.group(1)), match.group(2), item.read_text(encoding="utf-8")))
    return sorted(found)


def available_migrations(migrations_dir=None) -> list[tuple[int, str, str]]:
    if migrations_dir is None:
        return _package_migrations()
    root = Path(migrations_dir)
    found: list[tuple[int, str, str]] = []
    for path in sorted(root.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if not match:
            continue
        found.append((int(match.group(1)), match.group(2), path.read_text(encoding="utf-8")))
    return sorted(found)


def latest_version(migrations_dir=None) -> int:
    migrations = available_migrations(migrations_dir)
    if not migrations:
        return 0
    return migrations[-1][0]


def migrate(
    db: Path, *, create: bool = False, migrations_dir: Path | None = None, upto: int | None = None
) -> tuple[int, int]:
    db = Path(db)
    migrations = available_migrations(migrations_dir)
    if not migrations:
        raise MigrationError("no migrations available")
    latest = migrations[-1][0]
    if not db.exists():
        if not create:
            raise MigrationError(f"no store at {db}")
        db.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(db), isolation_level=None, timeout=10.0)
    try:
        con.execute("PRAGMA foreign_keys = ON")
        current = con.execute("PRAGMA user_version").fetchone()[0]
        if current > latest:
            raise MigrationError(
                f"store is at version {current}, newer than this engine ({latest})"
            )
        pending = [
            item for item in migrations if item[0] > current and (upto is None or item[0] <= upto)
        ]
        for number, slug, sql in pending:
            try:
                con.executescript(
                    "BEGIN IMMEDIATE;\n" + sql + f"\nPRAGMA user_version = {number};\n"
                )
                store.install_guards(con)
                version = con.execute("PRAGMA user_version").fetchone()[0]
                problems = store.integrity_problems(con)
                missing = store.missing_guards(con)
                search_missing = store.missing_search_objects(con)
                # Skip the search-object check only for a caller-supplied migration
                # set. Those fixtures are not the packaged schema and may not
                # install search. The packaged set always checks, whatever a
                # migration file is named.
                if migrations_dir is not None:
                    search_missing = []
                if problems or missing or search_missing or version != number:
                    detail = []
                    if version != number:
                        detail.append(f"user_version={version}")
                    if problems:
                        detail.append("integrity: " + "; ".join(problems))
                    if missing:
                        detail.append("missing guards: " + ", ".join(missing))
                    if search_missing:
                        detail.append("missing search objects: " + ", ".join(search_missing))
                    raise MigrationError(
                        f"migration {number:04d}_{slug} failed verification: "
                        + "; ".join(detail)
                    )
                con.execute("COMMIT")
            except Exception as exc:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                if isinstance(exc, MigrationError):
                    raise
                raise MigrationError(f"migration {number:04d}_{slug} failed: {exc}") from exc
        final = con.execute("PRAGMA user_version").fetchone()[0]
        return current, final
    finally:
        con.close()


# -- the schema comparison ------------------------------------------------

# SQLite writes these five shadow tables of search_index itself, so only their names are compared.
_FTS_SHADOWS = frozenset(
    f"search_index_{suffix}" for suffix in ("data", "idx", "content", "docsize", "config")
)
_SCHEMA_ROWS_SQL = (
    "SELECT type, name, tbl_name, sql FROM sqlite_schema "
    "WHERE substr(name, 1, 17) <> 'sqlite_autoindex_' "
    "AND (substr(name, 1, 7) <> 'sqlite_' OR substr(name, 1, 11) = 'sqlite_stat') "
    "ORDER BY type, name"
)
_expected: dict[int, list[tuple[str, str, str, str]]] = {}


def schema_rows(con: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """(type, name, tbl_name, sql) of every schema object, sorted by type and name. The sql is exact;
    the FTS5 shadow tables are read with an empty sql."""
    rows = []
    for kind, name, tbl_name, sql in con.execute(_SCHEMA_ROWS_SQL):
        if name in _FTS_SHADOWS:
            sql = ""
        rows.append((kind, name, tbl_name, sql if sql is not None else ""))
    return rows


def expected_schema(version: int) -> list[tuple[str, str, str, str]]:
    """The schema rows of a fresh store migrated to version, cached for the process."""
    found = _expected.get(version)
    if found is None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="brain-schema-") as tmp:
            db = Path(tmp) / "brain.sqlite3"
            migrate(db, create=True, upto=version)
            con = sqlite3.connect(str(db))
            try:
                found = schema_rows(con)
            finally:
                con.close()
        _expected[version] = found
    return found


def schema_differences(con: sqlite3.Connection, version: int, *, ignore_stats: bool = False) -> list[str]:
    """One message per object that is extra, missing or different from the engine's schema. `ignore_stats`
    leaves out the planner statistics SQLite writes (`sqlite_stat1` and the like) so that a store someone ran
    ANALYZE on still passes `brain check`; a bundle being restored is always compared strictly."""
    want = {(row[0], row[1]): row for row in expected_schema(version)}
    have = {(row[0], row[1]): row for row in schema_rows(con) if not (ignore_stats and row[1].startswith("sqlite_stat"))}
    extra = [f"{kind} {name} is not in the engine's schema" for kind, name in sorted(have) if (kind, name) not in want]
    missing = [f"{kind} {name} is missing" for kind, name in sorted(want) if (kind, name) not in have]
    different = [
        f"{kind} {name} differs from the engine's schema"
        for kind, name in sorted(want)
        if (kind, name) in have and have[(kind, name)] != want[(kind, name)]
    ]
    return extra + missing + different
