"""A list kept by hand must name everything it stands for.

Each test enumerates a source of truth (the ops in schemas.OPS, the live schema, the read tools, the strict
rules the applier calls) and fails naming the entry, the file and the list that does not account for it.
A list that holds one judgement per entry is checked against its source here; a list that could be computed
is computed in src/brain, and the test then holds the computation to the source.

Lists that have a guard elsewhere are not repeated here: schemas.FIELD_CLASS against every text column
(test_strict), scrub.DISPOSITION against FIELD_CLASS and every text column (test_redact_scrub,
test_redact_review), every table registered, STRICT and guarded (test_schema, test_guard), and the router
against the tools (test_read_server).
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain import migrate, schemas
from tests.test_truth import Clock, clock  # noqa: F401  (clock is a fixture)

SRC = Path(schemas.__file__).resolve().parents[1]  # the src folder of the brain under test


# -- the sources of truth ---------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live(tmp_path_factory) -> SimpleNamespace:
    """A freshly migrated store, read as the schema it holds."""
    path = tmp_path_factory.mktemp("lists") / "brain.sqlite3"
    migrate.migrate(path, create=True)
    con = sqlite3.connect(path)
    try:
        tables = dict(con.execute("SELECT name, sql FROM sqlite_schema WHERE type = 'table'"))
        return SimpleNamespace(
            tables=tables,
            columns={name: [row[1] for row in con.execute(f"PRAGMA table_info({name})")] for name in tables},
            names={row[0] for row in con.execute("SELECT name FROM sqlite_schema")},
        )
    finally:
        con.close()


def _checks(sql: str) -> list[str]:
    """The expression of each CHECK in a CREATE TABLE statement."""
    found = []
    for match in re.finditer(r"\bCHECK\s*\(", sql):
        depth, end = 1, match.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(sql[end], 0)
            end += 1
        found.append(sql[match.end() : end - 1])
    return found


def _attributes(path: str, base: str) -> set[str]:
    """Every `base.name` a source file reads."""
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    return {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == base
    }


def _all_ops() -> dict[str, dict]:
    return {**schemas.OPS, **schemas.COMMAND_OPS}


def _fail(problems: list[str]) -> None:
    assert not problems, "\n" + "\n".join(problems)


def _same(listed, truth, listed_name: str, truth_name: str) -> list[str]:
    """One problem for each entry that is in only one of two lists that must hold the same entries."""
    return [f"{entry} is in {truth_name} but not in {listed_name}" for entry in sorted(set(truth) - set(listed))] + [
        f"{entry} is in {listed_name} but not in {truth_name}" for entry in sorted(set(listed) - set(truth))
    ]


# -- ops ----------------------------------------------------------------------------------------------------------

# Strict rules the applier calls (strict.<name>) that the rollout check does not replay. Every strict rule add_claim uses, such as
# strict.parse_raw and strict.value_in_quote, must be replayed there.
NOT_REPLAYED = {
    "sets": "which note fields an op sets: the check counts every non-empty note field as set, as its comment says",
    "value_named": "resolve_fact's rule: the check does not read resolve_fact packets (a known gap)",
}


def _rollout_check():
    """tools/rollout_check.py, or None where the public release leaves it out (it checks a pre-0.7 instance)."""
    try:
        from tools import rollout_check
    except ImportError:
        return None
    return rollout_check


def test_the_rollout_check_replays_every_strict_rule_the_applier_uses() -> None:
    from brain import strict

    rollout_check = _rollout_check()
    if rollout_check is None:
        pytest.skip("tools/rollout_check.py is left out of the public tree")

    public = {name for name in dir(strict) if not name.startswith("_")}
    used = _attributes(SRC / "brain" / "applier.py", "strict") & public
    replayed = _attributes(rollout_check.__file__, "strict")
    problems = [
        f"strict.{name} is called by src/brain/applier.py but not read by tools/rollout_check.py: replay it there, "
        "or add it to NOT_REPLAYED with the reason"
        for name in sorted(used - replayed - set(NOT_REPLAYED))
    ]
    problems += [
        f"NOT_REPLAYED names strict.{name}, which {'the rollout check now reads' if name in replayed else 'the applier no longer calls'}: remove it"
        for name in sorted(NOT_REPLAYED)
        if name in replayed or name not in used
    ]
    _fail(problems)


def test_every_op_name_in_a_hand_list_is_an_op() -> None:
    from brain import applier, config, scrub, strict
    from brain.collectors import REGISTRY

    rollout_check = _rollout_check()
    named = {
        "applier._BRAIN_OPS": applier._BRAIN_OPS,
        "applier.LEGACY_OPS": applier.LEGACY_OPS,
        "config._UNGRANTABLE": config._UNGRANTABLE,
        "config._EXTRACTOR_DERIVED": config._EXTRACTOR_DERIVED,
        "schemas.COLLECTOR_OPS": schemas.COLLECTOR_OPS,
        "schemas.EXTRACTOR_OPS": schemas.EXTRACTOR_OPS,
        "scrub._OWN_RULE": scrub._OWN_RULE,
        "strict.STATE_DATES": set(strict.STATE_DATES),
        **({"rollout_check._CANDIDATE_OPS values": set(rollout_check._CANDIDATE_OPS.values())} if rollout_check else {}),
        **{f"collectors.REGISTRY[{name}].ops": set(ctype.ops) for name, ctype in REGISTRY.items()},
    }
    ops = _all_ops()
    _fail([f"{where} names {name}, which is not in schemas.OPS or schemas.COMMAND_OPS"
           for where, names in named.items() for name in sorted(names) if name not in ops])
    _fail([f"config._UNGRANTABLE names {name}, which applier._BRAIN_OPS does not let the brain agent write"
           for name in sorted(config._UNGRANTABLE - applier._BRAIN_OPS)])


def test_the_extractor_may_grant_exactly_the_ops_its_planners_derive(tmp_path: Path) -> None:
    from brain import candidates, store
    from brain import config
    from brain.config import load_config
    from brain.init import InitError
    from tests.extract_fixtures import CANDIDATE_SAMPLES, extract_instance

    try:
        instance = extract_instance(tmp_path)
    except InitError as exc:
        pytest.fail(f"config._EXTRACTOR_DERIVED (src/brain/config.py) refuses an op that SCAN in tests/extract_fixtures.py grants: {exc}")
    con = store.connect_reader(instance / "brain.sqlite3")
    con.row_factory = sqlite3.Row
    try:
        cfg = load_config(instance / "brain.toml")
        derived = {kind: {op["op"] for op in candidates.plan(con, cfg, sample, "2026-10-08T00:00:00Z").ops}
                   for kind, sample in CANDIDATE_SAMPLES.items()}
    finally:
        con.close()
    all_derived = set().union(*derived.values())
    _fail([f"{kind} derives {op} (src/brain/candidates.py), which config._EXTRACTOR_DERIVED (src/brain/config.py) "
           "does not let an extractor be granted"
           for kind, ops in derived.items() for op in sorted(ops - config._EXTRACTOR_DERIVED)])
    _fail([f"config._EXTRACTOR_DERIVED names {op}, which no candidate kind's sample derives"
           for op in sorted(config._EXTRACTOR_DERIVED - all_derived)])
    # Every kind either becomes an op the rollout check reads for notes, or its derived ops carry none.
    rollout_check = _rollout_check()
    if rollout_check is None:
        return
    _fail([f"{kind} derives {sorted(ops)} with note fields, but tools/rollout_check.py _CANDIDATE_OPS has no entry for it"
           for kind, ops in derived.items()
           if kind not in rollout_check._CANDIDATE_OPS
           and any(set(schemas.NOTE_FIELDS.get(op, ())) & set(schemas.CANDIDATE_KINDS[kind]["properties"]) for op in ops)])
    _fail(["rollout_check._CANDIDATE_OPS names " + kind + ", which is not a candidate kind"
           for kind in rollout_check._CANDIDATE_OPS if kind not in schemas.CANDIDATE_KINDS])


def test_validate_packet_checks_every_op_and_every_candidate_kind() -> None:
    from brain import validate

    def packet(op: dict) -> dict:
        return {"packet_version": 1, "change_id": "tester-1", "agent": "tester", "intent": "check", "ops": [op]}

    problems = []
    for name in _all_ops():
        errors = validate.validate_packet(packet({"op": name}))
        if any(item["path"] == "/ops/0/op" and "unknown" in item["message"] for item in errors):
            problems.append(f"validate_packet reads {name} (schemas.OPS or COMMAND_OPS) as an unknown op")
        elif name in schemas.OPS and not any(item["path"].startswith("/ops/0") for item in errors):
            problems.append(f"validate_packet checks no envelope for {name} (src/brain/validate.py)")
    for kind in schemas.CANDIDATE_KINDS:
        candidate = {"candidate_version": schemas.CANDIDATE_VERSIONS[kind], "kind": kind, "source_id": "src-a",
                     "locator": "p1", "quote": "five words are in this quote", "fields": {}}
        errors = validate.validate_packet(packet({"op": "apply_candidate", "candidate": candidate}))
        if not any(item["path"].startswith("/ops/0/candidate/fields") for item in errors):
            problems.append(f"validate_packet does not check the fields of a nested {kind} (src/brain/validate.py)")
        # A day in a candidate's schema is checked as a real date, whichever kind carries it.
        for field, prop in schemas.CANDIDATE_KINDS[kind]["properties"].items():
            if prop.get("pattern") in (schemas.PAST_DAY, schemas.FUTURE_DAY):
                errors = validate._candidate_structure(kind, {field: "2026-02-30"})
                if not any(item["path"] == f"/fields/{field}" for item in errors):
                    problems.append(f"{kind}.{field} takes a day, but validate._CANDIDATE_DAYS does not check it is a real date")
    _fail(problems)


# Row properties of an op that are not columns of its table, and what becomes of them.
NON_COLUMN_PROPERTIES = {
    ("upsert_fact", "value"): "stored in the value column its key's type names",
    ("upsert_fact", "value_raw"): "a claim's raw text, made for the claim the shorthand writes",
    ("add_claim", "planned"): "chooses the claim's status",
    ("verify_claim", "claim_id"): "names the claim to copy as checked",
    ("resolve_fact", "keep"): "claim ids to keep",
    ("resolve_fact", "reject"): "claim ids to reject",
    ("set_aliases", "aliases"): "the list the aliases rows are replaced by",
    ("set_source_author", "source_id"): "names the source whose author_person_id the applier changes",
    ("upsert_action", "owner_person_ids"): "rows of action_people",
    ("upsert_action", "waiting_on_person_ids"): "rows of action_people",
    ("upsert_checklist_item", "owner_person_ids"): "rows of checklist_item_people",
    ("upsert_checklist_item", "waiting_on_person_ids"): "rows of checklist_item_people",
}
# Columns of an op's table that its row never carries: the applier writes them.
DERIVED_COLUMNS = {
    ("add_source", "content_sha256"): "hashed from content",
    ("add_source", "captured_at"): "the decision time",
    ("add_source", "captured_by"): "the agent and change",
    ("record_collector_run", "recorded_at"): "the decision time",
    ("record_collector_run", "recorded_by"): "the agent and change",
    **{("upsert_fact", column): "from value and its key's type"
       for column in ("value_type", "value_text", "value_int", "value_real", "value_date")},
}


def test_every_op_row_property_is_a_column_and_every_column_is_writable(live) -> None:
    stamps = {*schemas._STAMPS, "version"}  # written from the envelope's source and the decision
    problems = []
    for name, spec in schemas.OPS.items():
        columns = set(live.columns[spec["table"]])
        properties = set(spec["row"]["properties"])
        for prop in sorted(properties - columns):
            if (name, prop) not in NON_COLUMN_PROPERTIES:
                problems.append(f"{name}.{prop} (schemas.OPS) is not a column of {spec['table']}: "
                                "add it to NON_COLUMN_PROPERTIES with what becomes of it")
        if spec["kind"] in ("upsert", "insert"):
            for column in sorted(columns - properties - stamps):
                if (name, column) not in DERIVED_COLUMNS:
                    problems.append(f"{spec['table']}.{column} (the live schema) is a column no {name} row can set: add it to the "
                                    f"row schema of {name} in src/brain/schemas.py, or to DERIVED_COLUMNS with who writes it")
    stale = [f"{where} is in the list but names no such property or column"
             for where, table in (("NON_COLUMN_PROPERTIES", NON_COLUMN_PROPERTIES), ("DERIVED_COLUMNS", DERIVED_COLUMNS))
             for name, prop in table
             if name not in schemas.OPS or (prop not in schemas.OPS[name]["row"]["properties"]
                                            and prop not in live.columns[schemas.OPS[name]["table"]])]
    _fail(problems + stale)


def test_an_op_row_is_never_looser_than_its_table(live) -> None:
    """An enum is the table's own CHECK list, and a text field is no longer than the table's length CHECK."""
    problems = []
    for name, spec in schemas.OPS.items():
        sql = live.tables[spec["table"]]
        for prop, schema in spec["row"]["properties"].items():
            if "enum" in schema:
                lists = [set(re.findall(r"'([^']*)'", found.group(1)))
                         for found in re.finditer(rf"(?<![\w.]){prop}\s+IN\s*\(((?:\s*'[^']*'\s*,?)+)\)", sql)]
                if set(schema["enum"]) not in lists:
                    problems.append(f"{name}.{prop} (schemas.OPS) lists {sorted(schema['enum'])}, but {spec['table']} "
                                    f"(the migrations) allows {[sorted(item) for item in lists] or 'any value'}")
            if "max_length" in schema:
                caps = [int(found.group(1) or found.group(2)) for found in re.finditer(
                    rf"length\((?:trim\()?{prop}\)?\)\s*(?:<=\s*(\d+)|BETWEEN\s+\d+\s+AND\s+(\d+))", sql)]
                if caps and schema["max_length"] > max(caps):
                    problems.append(f"{name}.{prop} (schemas.OPS) allows {schema['max_length']} characters, "
                                    f"but {spec['table']} (the migrations) stops at {max(caps)}")
    _fail(problems)


def test_a_blank_locator_is_refused_by_every_envelope_or_its_column(live) -> None:
    """add_claim's envelope refused a blank locator and candidates' did not. An op with no row of
    its own to refuse it must refuse at the envelope; an upsert's column CHECK trims, and says so in words."""
    from brain import validate

    def refuses_blank(envelope: dict) -> bool:
        locator = envelope["properties"]["source"]["properties"]["locator"]
        return bool(validate.check({"locator": " "}, {"type": "object", "properties": {"locator": locator}}))

    problems = []
    for name, spec in schemas.OPS.items():
        if "source" not in spec["envelope"]["properties"]:
            continue
        if spec["kind"] == "upsert":
            if not re.search(r"length\(trim\(locator\)\)", live.tables[spec["table"]]):
                problems.append(f"{name}: the envelope accepts a blank locator and {spec['table']} has no CHECK that trims it")
        elif not refuses_blank(spec["envelope"]):
            problems.append(f"{name} (schemas.OPS): the envelope accepts a blank locator, and this op (kind {spec['kind']}) reaches no trimming CHECK")
    candidate = schemas.CANDIDATE_ENVELOPE["properties"]["locator"]
    if not validate.check({"locator": " "}, {"type": "object", "properties": {"locator": candidate}}):
        problems.append("CANDIDATE_ENVELOPE (schemas.py) accepts a blank locator")
    _fail(problems)


def test_the_gate_reads_every_note_field_of_every_op() -> None:
    """GATE_FIELDS is each op's gate_fields in schemas.OPS; each is a field of the row, and each note field is gated."""
    from brain import evidence_gate

    assert evidence_gate.GATE_FIELDS == {name: spec["gate_fields"] for name, spec in schemas.OPS.items() if spec["gate_fields"]}
    problems = []
    for name, spec in schemas.OPS.items():
        properties = spec["row"]["properties"]
        problems += [f"{name}.{field} is in gate_fields (schemas.OPS) but not in the row" for field in spec["gate_fields"] if field not in properties]
        problems += [f"{name}.{field} is a note field (schemas.FIELD_CLASS) but not in its gate_fields (schemas.OPS)"
                     for field in properties
                     if schemas.FIELD_CLASS.get(f"{spec['table']}.{field}") == "note" and field not in spec["gate_fields"]]
    _fail(problems)


def test_every_op_with_note_fields_has_a_note_entity_and_table() -> None:
    from brain import store

    _fail(_same(schemas.NOTE_ENTITY, schemas.NOTE_FIELDS, "schemas.NOTE_ENTITY", "schemas.NOTE_FIELDS"))
    _fail(_same(store.NOTE_TABLES, schemas.NOTE_ENTITY.values(), "store.NOTE_TABLES (src/brain/store.py)", "schemas.NOTE_ENTITY"))
    _fail([f"{name}: its table {schemas.OPS[name]['table']} is in no entity of schemas.ENTITY_TABLE"
           for name in schemas.NOTE_FIELDS if schemas.OPS[name]["table"] not in schemas.ENTITY_TABLE.values()])


def test_every_source_kind_is_ranked_and_every_collector_writes_one() -> None:
    from brain import truth
    from brain.collectors import REGISTRY, capture
    from brain_fetch import scan

    _fail(_same(truth.DEFAULT_RANK_KINDS, schemas.SOURCE_KINDS, "truth.DEFAULT_RANK_KINDS (src/brain/truth.py)", "schemas.SOURCE_KINDS"))
    _fail(_same(truth.BASIS_ORDER, schemas.BASIS, "truth.BASIS_ORDER (src/brain/truth.py)", "schemas.BASIS"))
    assert len(set(truth.DEFAULT_RANK_KINDS)) == len(truth.DEFAULT_RANK_KINDS), "truth.DEFAULT_RANK_KINDS ranks a kind twice"
    _fail([f"collectors.REGISTRY[{name}].source_kind is {ctype.source_kind}, which is not in schemas.SOURCE_KINDS"
           for name, ctype in REGISTRY.items() if ctype.source_kind not in schemas.SOURCE_KINDS])
    _fail([f"capture.KINDS (src/brain/collectors/capture.py) names {kind}, which is not in schemas.SOURCE_KINDS"
           for kind in capture.KINDS if kind not in schemas.SOURCE_KINDS])
    _fail([f"scan.FILE_TYPES (src/brain_fetch/scan.py) reads {suffix} as {kind}, which capture.KINDS does not take"
           for suffix, kind, _tag in scan.FILE_TYPES if kind not in capture.KINDS])


def test_every_status_word_in_a_query_is_one_the_schema_allows(live) -> None:
    """A query that compares status, state, kind, role or the like to a word the table never holds matches nothing."""
    allowed: dict[str, set[str]] = {}
    for sql in live.tables.values():
        for found in re.finditer(r"(?<![\w.])(\w+)\s+(?:NOT\s+)?IN\s*\(((?:\s*'[^']*'\s*,?)+)\)", sql or ""):
            allowed.setdefault(found.group(1), set()).update(re.findall(r"'([^']*)'", found.group(2)))
    columns = "|".join(sorted(allowed))
    equals = re.compile(rf"(?<![\w.'])(?:\w+\.)?({columns})\s*(?:=|<>|!=)\s*'([^']*)'")
    listed = re.compile(rf"(?<![\w.'])(?:\w+\.)?({columns})\s+(?:NOT\s+)?IN\s*\(((?:\s*'[^']*'\s*,?)+)\)")
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.search(r"\b(SELECT|UPDATE|DELETE|INSERT|WHERE)\b", node.value)):
                continue
            words = [(m.group(1), m.group(2)) for m in equals.finditer(node.value)]
            words += [(m.group(1), word) for m in listed.finditer(node.value) for word in re.findall(r"'([^']*)'", m.group(2))]
            problems += [f"{path.relative_to(SRC.parent)}:{node.lineno} compares {column} to '{word}', which no table's CHECK allows for {column}"
                         for column, word in words if word not in allowed[column]]
    _fail(problems)


def test_the_open_statuses_are_action_statuses() -> None:
    from brain import reads

    _fail([f"reads.OPEN_ACTION_STATUSES (src/brain/reads.py) names {status}, which is not in schemas.ACTION_STATUSES"
           for status in reads.OPEN_ACTION_STATUSES if status not in schemas.ACTION_STATUSES])


# -- tables, entities and tools ---------------------------------------------------------------------------------


def test_every_search_object_in_the_schema_is_required_and_every_search_table_is_an_entity(live) -> None:
    from brain import store

    required = {name for _kind, name in store.required_search_objects(migrate.latest_version())}
    present = {name for name in live.names if name.startswith("search_") and not name.startswith("search_index_")}
    _fail([f"{name} is in the live schema (the migrations) but not in store.required_search_objects (src/brain/store.py)"
           for name in sorted(present - required)])
    by_table = {table: entity for entity, table in schemas.ENTITY_TABLE.items()}
    _fail([f"store.SEARCH_TABLES[{table}] says entity {entity}, but schemas.ENTITY_TABLE says {by_table.get(table, 'fact' if table == 'fact_keys' else None)}"
           for table, (entity, _key, _events) in store.SEARCH_TABLES.items()
           if entity != by_table.get(table, "fact" if table == "fact_keys" else None)])
    _fail(_same(migrate._FTS_SHADOWS, {name for name in live.names if name.startswith("search_index_")},
                "migrate._FTS_SHADOWS (src/brain/migrate.py)", "the search_index_* tables SQLite makes"))


def test_the_markers_follow_the_migration_and_the_gate(live) -> None:
    from brain import store

    check = next(item for item in _checks(live.tables["unchecked_text"]) if item.startswith("entity IN"))
    _fail(_same({entity for entity, _table in store.UNCHECKED_PARENTS}, re.findall(r"'([^']*)'", check),
                "store.UNCHECKED_PARENTS (src/brain/store.py)", "the entity CHECK of unchecked_text (the migrations)"))
    _fail([f"{table} holds a marker (store.UNCHECKED_PARENTS) but no op of it has gate_fields (schemas.OPS)"
           for _entity, table in store.UNCHECKED_PARENTS
           if not any(spec["table"] == table and spec["gate_fields"] for spec in schemas.OPS.values())])
    _fail([f"{entity} is in store.UNCHECKED_PARENTS but schemas.ENTITY_TABLE says its table is {schemas.ENTITY_TABLE.get(entity)}, not {table}"
           for entity, table in store.UNCHECKED_PARENTS if schemas.ENTITY_TABLE.get(entity) != table])


# What each table column that a read tool does not list as a field is read as instead.
NOT_READ_FIELDS = {
    "records.body": "get reads it a page at a time",
    "sources.content": "get reads it a page at a time",
    "sources.supersedes_id": "read only by its effect, fact_get's source_current",
    "facts.quote": "only each claim's quote is read, by fact_get and get on a claim",
    **{f"facts.{column}": "read as the one field value"
       for column in ("value_text", "value_int", "value_real", "value_date")},
}


def test_every_column_is_a_read_field_or_read_another_way(live) -> None:
    """parent_id (added with folders) was a column and a row property before records_query could list it."""
    from brain import reads

    problems = []
    for entity, spec in reads._ENTITIES.items():
        table = spec["from"].split(" AS ")[0]
        fields = {field.name for field in spec["fields"]}
        problems += [f"{table}.{column} (the live schema) is not a field of reads._ENTITIES[{entity}] (src/brain/reads.py): "
                     "add the field, or add the column to NOT_READ_FIELDS with how it is read"
                     for column in live.columns[table] if column not in fields and f"{table}.{column}" not in NOT_READ_FIELDS]
    problems += [f"NOT_READ_FIELDS names {key}, which is no column" for key in NOT_READ_FIELDS
                 if key.split(".")[1] not in live.columns[key.split(".")[0]]]
    _fail(problems)


def test_the_entity_to_table_map_agrees_with_the_read_entities() -> None:
    from brain import reads

    tables = {entity: spec["from"].split(" AS ")[0] for entity, spec in reads._ENTITIES.items() if entity != "fact"}
    _fail([f"{entity} reads from {table} (reads._ENTITIES) but schemas.ENTITY_TABLE says {schemas.ENTITY_TABLE.get(entity)}"
           for entity, table in tables.items() if schemas.ENTITY_TABLE.get(entity) != table]
          + _same(schemas.ENTITY_TABLE, tables, "schemas.ENTITY_TABLE", "reads._ENTITIES"))


def test_the_read_tools_list_the_entities_the_engine_has() -> None:
    from brain import reads, store, usage

    tools = {tool["name"]: tool["inputSchema"]["properties"] for tool in reads.TOOLS}
    entities = set(reads._ENTITIES)
    expected = {
        # A past session is read from the session record beside the brain, so it is in no brain table: search and
        # get take it, records_query doesn't.
        "get.entity": ({*entities, "claim", reads.SESSION_ENTITY}, set(tools["get"]["entity"]["enum"])),
        "records_query.entity": (entities, set(tools["records_query"]["entity"]["enum"])),
        "search.entities": ({entity for entity, _key, _events in store.SEARCH_TABLES.values()} | {reads.SESSION_ENTITY},
                            set(tools["search"]["entities"]["items"]["enum"])),
    }
    _fail([f"{where} (src/brain/reads.py TOOLS) lists {sorted(listed)}, but the engine has {sorted(engine)}"
           for where, (engine, listed) in expected.items() if engine != listed])
    _fail([f"usage._ROW_KEYS (src/brain/usage.py) names {tool}, which is not a read tool" for tool in usage._ROW_KEYS
           if tool not in reads.TOOL_NAMES])
    from brain import advice

    takes = set().union(*reads._FIELD_OPS.values()) | {"under"}
    _fail(_same(advice.OPERATORS, takes, "advice.OPERATORS (src/brain/advice.py)", "the operators reads._FIELD_OPS takes, and under"))
    kinds = set(tools["signals"]["kinds"]["items"]["enum"])
    _fail([f"the signals tool (src/brain/reads.py TOOLS) lists the kind {kind}, which is in neither schemas.SIGNAL_KINDS nor schemas.LINK_ROLES"
           for kind in sorted(kinds - set(schemas.SIGNAL_KINDS) - set(schemas.LINK_ROLES))])
    for where, keys in (("schemas.SUBJECT_KEY", schemas.SUBJECT_KEY), ("reads._SUBJECT_ENTITY (src/brain/reads.py)", reads._SUBJECT_ENTITY),
                        ("reads._SUBJECT_TITLE (src/brain/reads.py)", reads._SUBJECT_TITLE)):
        _fail(_same(keys, schemas.SIGNAL_SUBJECTS, where, "schemas.SIGNAL_SUBJECTS"))


# What a table's rows hold that a redaction rewrites by another route than the unquoted-copy scan.
RESIDUAL_EXEMPT = {
    "sources": "the removed text itself, rewritten whole",
    "claims": "rewritten through the chain's source ids",
    "facts": "rewritten through the chain's source ids",
    "fact_history": "rewritten through the chain's source ids",
    "record_history": "rewritten with its record (history_text_changes)",
    "unchecked_text": "rewritten with the chain's markers (unchecked_changes)",
    "write_log": "rewritten through the log rows that name the chain",
    "collector_runs": "rewritten through the runs that name the chain",
}


def _redactable_tables() -> set[str]:
    from brain import scrub

    return {key.split(".")[0] for key, kind in scrub.DISPOSITION.items() if kind in ("token", "spans")}


def test_the_redaction_updates_and_counts_every_table_it_rewrites() -> None:
    from brain import redact, scrub

    redactable = _redactable_tables()
    updated = set(re.findall(r"UPDATE (\w+) SET", (SRC / "brain" / "redact_ops.py").read_text(encoding="utf-8")))
    _fail([f"{table} has a column scrub.DISPOSITION rewrites (src/brain/scrub.py) but redact_ops never updates it"
           for table in sorted(redactable - updated - set(scrub.SPAN_COLUMNS))]
          + _same(redact._ROW_TABLES, redactable, "redact._ROW_TABLES (src/brain/redact.py)", "the tables scrub.DISPOSITION rewrites"))


def test_the_redaction_finds_the_rows_an_upsert_edited_through_the_log() -> None:
    """Threads and meetings are found through the log, not by source_id alone. A row's text can be edited by an op
    that cites another source, so a table an upsert op writes is found through the log that wrote it."""
    from brain import scrub

    tree = ast.parse((SRC / "brain" / "redact_ops.py").read_text(encoding="utf-8"))
    through_log = set(scrub.SPAN_COLUMNS) | {
        node.args[0].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
        and "wrote" in ast.dump(node.func.value) and node.args and isinstance(node.args[0], ast.Constant)
    }
    # facts are restamped by every upsert_fact, so their source_id is always the latest.
    editable = {spec["table"] for spec in schemas.OPS.values() if spec["kind"] == "upsert"} & _redactable_tables() - {"facts"}
    _fail([f"{table} is edited by upsert ops and rewritten by redaction, but redact_ops (src/brain/redact_ops.py) finds its rows "
           "by source_id only: look it up in rows_written's wrote" for table in sorted(editable - through_log)])


def test_the_residual_scan_reads_every_table_that_is_not_rewritten_another_way() -> None:
    from brain import redact_ops, scrub

    scanned = set(redact_ops._RESIDUAL_COLUMNS)
    redactable = _redactable_tables()
    _fail([f"{table} has a column scrub.DISPOSITION rewrites but redact_ops._RESIDUAL_COLUMNS (src/brain/redact_ops.py) does not scan it: "
           "add it, or add it to RESIDUAL_EXEMPT with the route that rewrites it"
           for table in sorted(redactable - scanned - set(RESIDUAL_EXEMPT))]
          + [f"{table}.{column} is scanned by redact_ops._RESIDUAL_COLUMNS but scrub.DISPOSITION does not rewrite it"
             for table, columns in redact_ops._RESIDUAL_COLUMNS.items() for column in columns
             if scrub.DISPOSITION.get(f"{table}.{column}") not in ("token", "spans")]
          + [f"RESIDUAL_EXEMPT names {table}, which is scanned or not redactable" for table in RESIDUAL_EXEMPT
             if table in scanned or table not in redactable])


# -- entry points ---------------------------------------------------------------------------------------------------


def test_every_guide_grants_the_operations_its_collector_needs() -> None:
    """The printed setup guides hold the operations list brain.toml must match (config.py refuses any other)."""
    from brain.collectors import REGISTRY
    from brain_fetch import guides

    problems = []
    for service in guides._TEXT:
        text = guides.guide(service, "demo", "/instances/demo", "work-" + service)
        blocks = re.findall(r"^\[[a-z].*?(?=\n\n|\n[0-9]\.|\Z)", text, flags=re.S | re.M)
        table = tomllib.loads("\n\n".join(blocks))
        for name, block in table.get("collectors", {}).items():
            ops = tuple(table["agents"][block["agent"]]["operations"])
            expected = REGISTRY[block["type"]].ops
            if set(ops) != set(expected):
                problems.append(f"the {service} guide (src/brain_fetch/guides.py) grants {name} {list(ops)}, "
                                f"but collector type {block['type']} needs {list(expected)}")
    _fail(problems)


def test_every_harness_has_its_commands() -> None:
    from brain import connect
    from tools import accept_connect

    harnesses = set(connect.HARNESSES)
    for where, keys in (("accept_connect.CHECKS", accept_connect.CHECKS), ("accept_connect.VERSION", accept_connect.VERSION)):
        _fail(_same(keys, harnesses, f"{where} (tools/accept_connect.py)", "connect.HARNESSES (src/brain/connect.py)"))
    _fail([f"connect.INSTRUCTIONS names {name}, which is not in connect.HARNESSES" for name in connect.INSTRUCTIONS if name not in harnesses])
    tree = ast.parse((SRC / "brain" / "connect.py").read_text(encoding="utf-8"))
    func = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "_cli")
    plans = next(node.value for node in ast.walk(func) if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "plans" for target in node.targets))
    _fail(_same({key.value for key in plans.keys}, harnesses - {"cursor"}, "the plans in connect._cli (src/brain/connect.py)",
                "connect.HARNESSES but cursor"))


def test_the_hub_audits_only_known_actions_and_answers_every_exit_code() -> None:
    from brain import applier
    from brain_hub import server, tokens

    used = set()
    for path in (SRC / "brain_hub").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "audit"
                    and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)):
                used.add((path.name, node.args[1].value))
    _fail([f"{name} audits {action}, which tokens.ACTIONS (src/brain_hub/tokens.py) does not list"
           for name, action in sorted(used) if action not in tokens.ACTIONS])
    _fail([f"applier._EXIT has {code} (src/brain/applier.py), which server.HTTP_FOR_EXIT (src/brain_hub/server.py) has no status for"
           for code in sorted(set(applier._EXIT.values()) - set(server.HTTP_FOR_EXIT))])


def test_every_read_tool_hides_the_value_of_a_fact_retired_after_a_dispute(tmp_path: Path, clock: Clock) -> None:
    """A fact retired after a dispute reads as disputed. fact_get, get and records_query hid its value and
    search did not (8076128 to 49dac3a), so each read tool has a probe here, and a new tool needs one."""
    from brain import reads
    from tests.test_truth import CTX, SIDES, _brief, _read, _retired_after_dispute

    def shown(tool: str, text: str) -> list[str]:
        return [f"{tool} shows {side}, a side of a dispute (src/brain/reads.py)" for side in SIDES if side in text]

    def search(inst: Path, history: bool) -> list[str]:
        problems = []
        for query in ("context window", "128K", "131"):
            hits = _read(inst, "search", query=query, entities=["fact"]).structured["hits"]
            problems += shown("search", json.dumps(hits, ensure_ascii=False))
            problems += [f"search on {query} lists {CTX} without flagging it disputed (src/brain/reads.py)"
                         for hit in hits if hit["id"] == CTX and not (hit.get("disputed") is True and hit["snippet"] == "disputed")]
        return problems

    def get(inst: Path, history: bool) -> list[str]:
        # On the retire path the first history row was current before the dispute began, so it shows its value.
        got = _read(inst, "get", entity="fact", id=CTX, include_history=history)
        return shown("get", got.text) + ([] if got.structured["row"]["value"] == "disputed" else ["get shows a value for it"])

    def fact_get(inst: Path, history: bool) -> list[str]:
        text = _read(inst, "fact_get", key=CTX).text + _read(inst, "fact_get", key="excloud.*", include_retired=True).text
        return shown("fact_get", text)

    def records_query(inst: Path, history: bool) -> list[str]:
        rows = _read(inst, "records_query", entity="fact", fields=["key", "value", "status"])
        found = {row["key"]: row["value"] for row in rows.structured["rows"]}
        return shown("records_query", rows.text) + ([] if found[CTX] == "disputed" else [f"records_query shows {found[CTX]} for {CTX}"])

    probes = {
        "brief": lambda inst, history: shown("brief", "\n".join(line for line in _brief(inst) if not line.startswith("Disputed "))),
        "search": search,
        "get": get,
        "fact_get": fact_get,
        "records_query": records_query,
        "signals": lambda inst, history: shown("signals", _read(inst, "signals").text),
    }
    _fail(_same(probes, reads.TOOL_NAMES, "the probes (tests/test_every_list_is_complete.py)", "reads.TOOL_NAMES (src/brain/reads.py)"))
    for path in ("version-move", "retire"):
        inst = _retired_after_dispute(tmp_path / path, path)
        _fail([f"{path}: {problem}" for probe in probes.values() for problem in probe(inst, path == "version-move")])


def test_the_folder_scope_classifies_every_table_and_view(tmp_path: Path) -> None:
    """A table nobody classified is hidden from a folder token (scope.reader), and this fails naming it, so adding a table
    forces a decision about who may see it."""
    from brain import scope

    path = tmp_path / "brain.sqlite3"
    migrate.migrate(path, create=True)
    con = sqlite3.connect(path)
    try:
        missing = scope.unclassified(con)
        _fail([f"{name}: add it to RULES in src/brain/scope.py (filed, owned, pointed, derived or hidden)" for name in missing])
        present = {name for name, _kind in scope.objects(con)}
        _fail([
            f"scope.RULES names {name}, which the schema doesn't have: remove it from src/brain/scope.py"
            for name in scope.RULES if name not in present
        ])
        for name, rule in scope.RULES.items():
            if rule.kind == scope.PASS_THROUGH:
                assert rule.reason, f"{name} is a pass-through in src/brain/scope.py and needs its reason written beside it"
            if rule.kind in (scope.FILED, scope.OWNED, scope.POINTED):
                for column, parent in scope._references(con, name):
                    assert parent in scope.RULES, f"{name}.{column} references {parent}, which scope.RULES doesn't classify"
    finally:
        con.close()
