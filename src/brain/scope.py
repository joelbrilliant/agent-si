"""Folder-limited reads (1.12.0): one choke point for what a hub token that names folders can see.

`reader(folders)` is an opener with the same shape as `store.connect_reader`. It opens the read connection, builds
temporary tables of the visible ids from the `main` tables, then shadows every table and view the read tools can
touch with a temporary view of the same name, so `projects` in a query reads the filtered view and `main.projects`
still reads the table. The tools keep their queries; the views decide what exists.

What is visible, in this order. Anything not made visible by a step is hidden.

1. Filed rows: the named folders and every folder inside them, and the rows whose `project_id` is in that branch.
2. Owned rows: rows that belong to one visible filed row.
3. Sources a visible filed or owned row names in a column that references `sources(id)`.
4. People and threads a visible filed or owned row names, and people who wrote a visible source.
5. Hidden, always: facts in every form, systems, the write log, and every table nobody classified yet.

No visible row names a hidden one: a column that references a hidden row reads as NULL, and a `locator` reads as NULL
when the source beside it does. Which columns reference which table comes from `pragma_foreign_key_list`.

The main database stays `mode=ro`. `query_only` is off only while the temporary objects are built, inside the
transaction the read then runs in, so the ids and the rows come from one snapshot.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from brain import schemas, store

FILED = "filed"
OWNED = "owned"
POINTED = "pointed"
DERIVED = "derived"
HIDDEN = "hidden"
PASS_THROUGH = "pass-through"


@dataclass(frozen=True)
class Rule:
    kind: str
    # Filed and owned: a condition on `t`, a row of main.<table>, true when the row is visible.
    # Derived: the same, for a view built over another view of main.
    where: str = ""
    # Pass-through: why the real table has to stay readable.
    reason: str = ""


def _ids(table: str) -> str:
    """The temporary table of a table's visible ids."""
    return f"(SELECT id FROM temp.scope_{table})"


# Folders are projects. A filed row's column naming its folder ("id" for a project itself).
FILED_COLUMN = {
    "projects": "id",
    "records": "project_id",
    "decisions": "project_id",
    "actions": "project_id",
    "checklist_items": "project_id",
    "meetings": "project_id",
}
# The pointed-at tables, in the order each is worked out. A table is made visible only by the filed and owned tables
# and the pointed-at tables before it, so a person's own source or a thread's source never makes a source visible.
POINTED_ORDER = ("sources", "people", "threads")


def _by_table(table_column: str, id_column: str, tables) -> str:
    """A polymorphic reference (a table name and an id) that names a visible row of one of the tables."""
    return "(" + " OR ".join(
        f"(t.{table_column} = '{table}' AND t.{id_column} IN {_ids(table)})" for table in tables
    ) + ")"


def _by_entity(entity_column: str, id_column: str, tables) -> str:
    """The same, where the column holds the read tools' entity name."""
    by_table = {table: entity for entity, table in schemas.ENTITY_TABLE.items()}
    return "(" + " OR ".join(
        f"(t.{entity_column} = '{by_table[table]}' AND t.{id_column} IN {_ids(table)})" for table in tables
    ) + ")"


_SEARCH_TABLES = tuple(FILED_COLUMN) + POINTED_ORDER

# The classification: every table and view of the main schema, keyed by name. One that is missing is hidden by
# reader() and fails tests/test_every_list_is_complete.py, naming it.
RULES: dict[str, Rule] = {
    # 1. Filed rows.
    **{
        table: Rule(FILED, f"t.{column} IN {_ids('projects')}")
        for table, column in FILED_COLUMN.items()
    },
    # 2. Owned rows.
    "record_history": Rule(OWNED, f"t.project_id IN {_ids('projects')}"),
    "action_people": Rule(OWNED, f"t.action_id IN {_ids('actions')}"),
    "checklist_item_people": Rule(OWNED, f"t.item_id IN {_ids('checklist_items')}"),
    "thread_links": Rule(
        OWNED, f"(t.action_id IN {_ids('actions')} OR t.checklist_item_id IN {_ids('checklist_items')})"
    ),
    "links": Rule(OWNED, _by_table("subject_table", "subject_id", FILED_COLUMN)),
    "unchecked_text": Rule(OWNED, _by_entity("entity", "ref_id", FILED_COLUMN)),
    "note_meta": Rule(OWNED, _by_entity("entity", "ref_id", FILED_COLUMN)),
    "signals": Rule(OWNED, _by_table("subject_table", "subject_id", FILED_COLUMN)),
    # 3 and 4. Pointed at by a visible row.
    **{table: Rule(POINTED, f"t.id IN {_ids(table)}") for table in POINTED_ORDER},
    # Views over the visible rows, built from the same sets.
    "search_keys": Rule(DERIVED, _by_entity("entity", "ref_id", _SEARCH_TABLES)),
    "search_docs": Rule(DERIVED, _by_entity("entity", "ref_id", _SEARCH_TABLES)),
    "thread_link_state": Rule(
        DERIVED, f"(t.action_id IN {_ids('actions')} OR t.checklist_item_id IN {_ids('checklist_items')})"
    ),
    # 5. Hidden, always.
    **{
        table: Rule(HIDDEN)
        for table in (
            "aliases", "claims", "collector_runs", "fact_history", "fact_key_parts", "fact_keys", "facts",
            "redaction_gate", "redaction_packets", "source_author_changes", "systems", "write_log",
        )
    },
    # Pass-through, with its reason. Every other pass-through needs its reason written beside it.
    "search_index": Rule(
        PASS_THROUGH,
        reason="an FTS5 table can't be shadowed, because MATCH needs the real table, and its shadow tables hold a copy of every "
        "row's text. Search is therefore always given the branch (reads._search_filter), so only filed rows in it match.",
    ),
}
# The shadow tables of a pass-through virtual table share its name as a prefix.
PASS_THROUGH_TABLES = tuple(name for name, rule in RULES.items() if rule.kind == PASS_THROUGH)


def _is_pass_through(name: str, kind: str) -> bool:
    return any(name == base or (kind == "shadow" and name.startswith(base + "_")) for base in PASS_THROUGH_TABLES)


def objects(con: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every (name, type) in the main schema that a read could name, SQLite's own tables left out. Type is table,
    view, virtual or shadow."""
    return [
        (row[0], row[1])
        for row in con.execute(
            "SELECT name, type FROM pragma_table_list WHERE schema = 'main' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def unclassified(con: sqlite3.Connection) -> list[str]:
    """The names in the main schema that RULES doesn't classify, leaving out the shadow tables of a pass-through."""
    return [
        name for name, kind in objects(con) if name not in RULES and not _is_pass_through(name, kind)
    ]


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[0] for row in con.execute("SELECT name FROM pragma_table_info(?, 'main')", (table,))]


def _references(con: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    """(column, referenced table) for each foreign key of a table. A key over several columns fails closed."""
    found = list(con.execute('SELECT "from", "table", "to", seq FROM pragma_foreign_key_list(?, \'main\')', (table,)))
    if any(row[3] for row in found):
        raise RuntimeError(f"{table} has a foreign key over several columns; scope.py reads one column at a time")
    for column, parent, target, _seq in found:
        if target not in (None, "id"):
            raise RuntimeError(f"{table}.{column} references {parent}({target}); scope.py expects {parent}(id)")
    return [(row[0], row[1]) for row in found]


def _scoped_view(con: sqlite3.Connection, table: str, rule: Rule) -> str:
    """CREATE TEMP VIEW for a table of rows made visible by rule.where, every column that references a hidden row
    reading as NULL."""
    references = dict(_references(con, table))
    columns = _columns(con, table)
    source_kept = None
    expressions = []
    for column in columns:
        parent = references.get(column)
        if parent is None:
            expressions.append(f't."{column}" AS "{column}"')
            continue
        parent_rule = RULES.get(parent)
        if parent_rule is not None and parent_rule.kind in (FILED, POINTED):
            keep = f't."{column}" IN {_ids(parent)}'
            expressions.append(f'CASE WHEN {keep} THEN t."{column}" END AS "{column}"')
            if column == "source_id":
                source_kept = keep
        else:
            expressions.append(f'NULL AS "{column}"')
    if source_kept is None and "source_id" in references:
        source_kept = "0"
    if "locator" in columns and source_kept is not None:
        # A locator points into a source, so it reads as empty when the source does.
        expressions[columns.index("locator")] = f'CASE WHEN {source_kept} THEN t."locator" END AS "locator"'
    where = rule.where or "0"
    return f'CREATE TEMP VIEW "{table}" AS SELECT {", ".join(expressions)} FROM main."{table}" AS t WHERE {where}'


def _build(con: sqlite3.Connection, folders: tuple[str, ...]) -> None:
    """Build the visible id tables and the shadowing views. The caller turns query_only off around this."""
    con.execute("PRAGMA temp_store = MEMORY")
    present = objects(con)
    names = {name for name, _kind in present}

    # 1. The branch: each named folder and every folder inside it, worked out now, on this snapshot.
    con.execute("CREATE TEMP TABLE scope_projects (id TEXT PRIMARY KEY)")
    marks = ", ".join("?" for _ in folders)
    con.execute(
        "WITH RECURSIVE branch(id) AS (SELECT id FROM main.projects WHERE id IN (" + marks + ") "
        "UNION SELECT p.id FROM main.projects AS p JOIN branch AS b ON p.parent_id = b.id) "
        "INSERT OR IGNORE INTO scope_projects SELECT id FROM branch",
        tuple(folders),
    )
    for table, column in FILED_COLUMN.items():
        if table == "projects" or table not in names:
            continue
        con.execute(f"CREATE TEMP TABLE scope_{table} (id TEXT PRIMARY KEY)")
        con.execute(
            f"INSERT OR IGNORE INTO scope_{table} SELECT t.id FROM main.\"{table}\" AS t WHERE {RULES[table].where}"
        )

    # 3 and 4. What the visible rows point at. Each table is made visible by the filed and owned tables and the
    # pointed-at tables worked out before it.
    origins = [name for name, rule in RULES.items() if rule.kind in (FILED, OWNED) and name in names]
    for position, target in enumerate(POINTED_ORDER):
        con.execute(f"CREATE TEMP TABLE scope_{target} (id TEXT PRIMARY KEY)")
        for origin in origins + list(POINTED_ORDER[:position]):
            for column, parent in _references(con, origin):
                if parent != target:
                    continue
                con.execute(
                    f"INSERT OR IGNORE INTO scope_{target} SELECT t.\"{column}\" FROM main.\"{origin}\" AS t "
                    f"WHERE t.\"{column}\" IS NOT NULL AND ({RULES[origin].where})"
                )

    # The views. A name that nobody classified, and every shadow or virtual table that isn't a pass-through, is hidden.
    for name, kind in present:
        if kind == "virtual" or kind == "shadow":
            if _is_pass_through(name, kind):
                continue
        rule = RULES.get(name)
        if rule is None or rule.kind == HIDDEN:
            con.execute(f'CREATE TEMP VIEW "{name}" AS SELECT * FROM main."{name}" WHERE 0')
        elif rule.kind == DERIVED:
            con.execute(f'CREATE TEMP VIEW "{name}" AS SELECT t.* FROM main."{name}" AS t WHERE {rule.where}')
        elif rule.kind in (FILED, OWNED, POINTED):
            con.execute(_scoped_view(con, name, rule))
        elif rule.kind == PASS_THROUGH:
            continue
        else:
            raise RuntimeError(f"{name}: no scope for kind {rule.kind}")


def reader(folders) -> Callable[[Path], sqlite3.Connection]:
    """An opener like store.connect_reader whose connection reads only the named folders and what they hold. The
    connection comes back inside an open read transaction, with query_only on again."""
    named = tuple(folders)
    if not named or not all(isinstance(folder, str) and folder for folder in named):
        raise ValueError("folders must be a non-empty tuple of folder ids")

    def open_scoped(db: Path) -> sqlite3.Connection:
        con = store.connect_reader(db)
        try:
            con.execute("BEGIN")
            con.execute("PRAGMA query_only = OFF")
            try:
                _build(con, named)
            finally:
                con.execute("PRAGMA query_only = ON")
        except BaseException:
            con.close()
            raise
        return con

    return open_scoped
