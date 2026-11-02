"""The `redact_source` op. The applier calls `redact_source` from `_run_ops`, inside the
packet's savepoint and the one `BEGIN IMMEDIATE` transaction, so the whole scrub commits together or not
at all."""
from __future__ import annotations

import hashlib
import json
import re

from brain import evidence_gate, scrub, store
from brain.collectors.base import local_date

CHAIN_MAX = 1000
# Above this many claim updates the redaction drops search_claims_au for the loop and refreshes each key once.
TRIGGER_SWAP_MIN = 200
# A note row "still holds the text" when it repeats this many consecutive words of a removed source, or a
# bracketed span (backticks, guillemets, brackets) of at least RESIDUAL_SPAN characters that the source contains.
RESIDUAL_WORDS = 5
RESIDUAL_SPAN = 8
RESIDUAL_LIST_MAX = 50
_BRACKETED = re.compile(r"`([^`]+)`|\u00ab([^\u00bb]+)\u00bb|\u2039([^\u203a]+)\u203a|\u300c([^\u300d]+)\u300d|\u300e([^\u300f]+)\u300f|\[([^\]]+)\]|\(([^)]+)\)|<([^>]+)>")

_CHAIN_UP = (
    "WITH RECURSIVE c(id, d) AS (SELECT ?, 0 UNION ALL SELECT s.supersedes_id, c.d + 1 FROM sources AS s "
    "JOIN c ON s.id = c.id WHERE s.supersedes_id IS NOT NULL AND c.d < ?) SELECT id FROM c"
)
_CHAIN_DOWN = (
    "WITH RECURSIVE c(id, d) AS (SELECT ?, 0 UNION ALL SELECT s.id, c.d + 1 FROM sources AS s "
    "JOIN c ON s.supersedes_id = c.id WHERE c.d < ?) SELECT id FROM c"
)
_IN_CHAIN = "(SELECT value FROM json_each(?))"
# The rows of write_log that can name a chain source: one JSON array parameter, so nothing grows with the chain.
_LOG_NAMES = (
    "EXISTS (SELECT 1 FROM json_each(?) AS j WHERE instr(packet_json, j.value) > 0 "
    "OR instr(error, j.value) > 0 OR instr(hold_reason, j.value) > 0)"
)


def chain_ids(con, source_id: str) -> list[str]:
    """The sorted ids of every version of the item `source_id` belongs to: up by `supersedes_id` and down by
    `supersedes_id = id`, each bounded at CHAIN_MAX steps. The given id is included. 1.9.0: so is each
    version's speaker-lines excerpt, which holds lines copied out of it and goes with it."""
    def versions(sid: str) -> set[str]:
        up = con.execute(_CHAIN_UP, (sid, CHAIN_MAX)).fetchall()
        down = con.execute(_CHAIN_DOWN, (sid, CHAIN_MAX)).fetchall()
        return {row[0] for row in up} | {row[0] for row in down}

    found = versions(source_id)
    for sid in sorted(found):
        # `external_ref <> ''` lets SQLite use the partial index sources_by_external_ref.
        for (excerpt,) in con.execute(
            "SELECT id FROM sources WHERE external_ref = ? AND external_ref <> ''", (evidence_gate.SPEAKER_LINES + sid,)
        ):
            found |= versions(excerpt)
    return sorted(found)


def _is_excerpt(row: dict) -> bool:
    return str(row["external_ref"]).startswith(evidence_gate.SPEAKER_LINES)


def chain_root(ids: list[str], rows: dict) -> str:
    """The first version of the chain: the one that supersedes nothing, never a speaker-lines excerpt of it."""
    return next(
        (
            sid
            for sid in ids
            if rows[sid]["supersedes_id"] is None and not _is_excerpt(rows[sid])
        ),
        ids[0],
    )


def reason_overlap(reason: str, contents) -> tuple[int, int, int] | None:
    """The first run of 16 letters and digits that the reason shares with one of `contents`, as
    (index into contents, first, last) 1-based characters of the reason, or None."""
    positions = [i for i, ch in enumerate(reason) if scrub.squeeze(ch)]
    squeezed = "".join(scrub.squeeze(reason[i]) for i in positions)
    if len(squeezed) < 16:
        return None
    windows = [squeezed[i : i + 16] for i in range(len(squeezed) - 15)]
    for index, content in enumerate(contents):
        body = scrub.squeeze(content)
        for i, window in enumerate(windows):
            if window in body:
                return index, positions[i] + 1, positions[i + 15] + 1
    return None


def redact_source(app, label, op, agent, change_id, decided_at):
    """Run the whole scrub. Returns (error, status, targets) like every command handler."""
    con = app.con
    source_id, reason, operator = op["source_id"], op["reason"], op["operator"]
    root = app._load_row("sources", "id", source_id)
    # Step 0: refusals, before anything changes.
    if root is None:
        return f"op {label} (redact_source): source {source_id} does not exist", "rejected", []
    if root["kind"] == "config":
        return f"op {label} (redact_source): config sources are not redacted", "rejected", []
    ids = chain_ids(con, source_id)
    if len(ids) > CHAIN_MAX:
        return f"op {label} (redact_source): source {source_id} has more than {CHAIN_MAX} versions", "rejected", []
    if con.execute("SELECT 1 FROM redaction_gate LIMIT 1").fetchone() or con.execute(
        "SELECT 1 FROM redaction_packets LIMIT 1"
    ).fetchone():
        return f"op {label} (redact_source): redaction_gate is not empty: run brain check", "rejected", []
    rows = {sid: app._load_row("sources", "id", sid) for sid in ids}
    live = [sid for sid in ids if not scrub.is_tombstone(rows[sid])]
    # The title and the uri are replaced too, and a reason that repeats one lands in the tombstone and the log.
    texts = [(sid, rows[sid][column] or "") for sid in live for column in ("content", "title", "uri")]
    found = reason_overlap(reason, [text for _sid, text in texts])
    if found is not None:
        return (
            f"op {label} (redact_source): the reason repeats text from source {texts[found[0]][0]} "
            f"(characters {found[1]} to {found[2]} of the reason)",
            "rejected",
            [],
        )

    stamp = f"{agent}:{change_id}"
    date = local_date(decided_at, app.config.timezone)
    tombstone = scrub.tombstone_text(date, operator, reason)
    chain = json.dumps(ids)
    idset = set(ids)
    keys = {sid: scrub.source_key(rows[sid]["content"]) for sid in live}
    skeys = list(keys.values())
    targets: list[dict] = []

    def hit(table, ident, action="update"):
        targets.append({"table": table, "id": str(ident), "action": action})

    # The threads a redacted title names are read now, before step 2 overwrites the title.
    log_rows = log_rows_naming(con, chain)
    wrote = rows_linked(con, chain, rows_written(log_rows, idset))
    by_source, by_subject = _linked_threads(con, ids, rows, live, chain, wrote)

    # Step 1: open the gate, one row per chain source.
    for sid in ids:
        con.execute(
            "INSERT INTO redaction_gate (source_id, redacted_on, tombstone) VALUES (?, ?, ?)", (sid, date, tombstone)
        )

    # Step 2: sources.
    for sid in live:
        con.execute(
            "UPDATE sources SET title = ?, content = ?, uri = '', author_person_id = NULL WHERE id = ?",
            (scrub.TOMBSTONE_TITLE, tombstone, sid),
        )
        hit("sources", sid)
        _reindex_source(con, sid)

    # Step 3: claims.
    keys_touched: list[str] = []
    changes = claim_changes(con, chain)
    # Search_claims_au deletes and re-inserts one FTS row per updated claim; N updates of one key
    # inside one transaction cost O(N^2). The fact document does not read claims, so drop the trigger for the
    # loop, put back the identical SQL, and refresh each touched key once.
    trigger_sql = None
    if len(changes) > TRIGGER_SWAP_MIN:
        trigger_sql = con.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'search_claims_au'").fetchone()[0]
        con.execute("DROP TRIGGER search_claims_au")
    for cid, key, open_claim, new_quote, new_text in changes:
        if open_claim:
            con.execute(
                "UPDATE claims SET status = 'rejected', resolution_source_id = source_id, resolution_locator = ?, "
                "quote = ?, value_raw = ?, value_text = ?, updated_at = ?, updated_by = ? WHERE id = ?",
                (f"redacted on {date}", new_quote, scrub.REDACTED, new_text, decided_at, stamp, cid),
            )
        else:
            con.execute(
                "UPDATE claims SET quote = ?, value_raw = ?, value_text = ?, updated_at = ?, updated_by = ? "
                "WHERE id = ?",
                (new_quote, scrub.REDACTED, new_text, decided_at, stamp, cid),
            )
        hit("claims", cid)
        if key not in keys_touched:
            keys_touched.append(key)

    if trigger_sql is not None:
        con.execute(trigger_sql)
        for key in keys_touched:
            con.execute("INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('fact', ?)", (key,))
            con.execute("DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'fact' AND ref_id = ?)", (key,))
            con.execute("INSERT INTO search_index (rowid, entity, ref_id, status, title, body) SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id WHERE d.entity = 'fact' AND d.ref_id = ?", (key,))

    # Step 4: headlines.
    written: set = set()
    for key in keys_touched:
        app._derive(key, decided_at, stamp, written, targets)

    # Step 5: facts left on the source, and history.
    for key, version, new_text, new_quote in fact_changes(con, chain):
        con.execute(
            "UPDATE facts SET value_text = ?, quote = ?, version = ?, updated_at = ?, updated_by = ? WHERE key = ?",
            (new_text, new_quote, version + 1, decided_at, stamp, key),
        )
        hit("facts", key)
    for hid, new_text, new_quote in history_changes(con, chain):
        con.execute("UPDATE fact_history SET value_text = ?, quote = ? WHERE id = ?", (new_text, new_quote, hid))
        hit("fact_history", hid)

    # Step 6: note tables. A row cites a source only at its last edit, so the rows are found through the
    # log that wrote them as well as through source_id.
    changed_rows: set = set()
    for table, row_id, new in note_changes(con, ids, chain, skeys, wrote):
        sets = ", ".join(f"{c} = ?" for c in new)
        con.execute(
            f"UPDATE {table} SET {sets}, version = version + 1, updated_at = ?, updated_by = ? WHERE id = ?",
            [*new.values(), decided_at, stamp, row_id],
        )
        hit(table, row_id)
        changed_rows.add((table, str(row_id)))
    for hid, new_title, new_body, new_summary in history_text_changes(con, chain, skeys, wrote):
        con.execute(
            "UPDATE record_history SET title = ?, body = ?, summary = ? WHERE id = ?",
            (new_title, new_body, new_summary, hid),
        )
        hit("record_history", hid)

    # Step 7: unchecked markers keep their row, their keys and their reasons.
    for entity, ref_id, updated in unchecked_changes(con, chain, skeys):
        con.execute(
            "UPDATE unchecked_text SET sentences = ?, updated_at = ?, updated_by = ? WHERE entity = ? AND ref_id = ?",
            (updated, decided_at, stamp, entity, ref_id),
        )
        hit("unchecked_text", f"{entity}:{ref_id}")

    # Step 8: threads and meetings.
    for tid, (version, _channel, _account) in sorted({**by_source, **by_subject}.items()):
        con.execute(
            "UPDATE threads SET subject = ?, version = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (scrub.REDACTED, version + 1, decided_at, stamp, tid),
        )
        hit("threads", tid)
    for mid, version in meeting_changes(con, chain, wrote):
        con.execute(
            "UPDATE meetings SET title = ?, version = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (scrub.REDACTED, version + 1, decided_at, stamp, mid),
        )
        hit("meetings", mid)

    # Report (do not remove) the note rows that still hold the removed text without quote marks.
    residual = residual_scan(con, skeys)

    # Step 9: the log and the runs.
    types = {row[0]: row[1] for row in con.execute("SELECT key, value_type FROM fact_keys")}
    listed, actions = log_actions(con, ids, log_rows, changed_rows, keys, types, date)
    # Every packet that edited a changed row may carry its sentence in an op that cites another source, so
    # each is listed (the log triggers accept an edit of that op for these), whether or not it also names a
    # chain source.
    for change_id_ in listed:
        con.execute("INSERT OR IGNORE INTO redaction_packets (change_id) VALUES (?)", (change_id_,))
    held_rejected = 0
    for act in actions:
        if act["held"]:
            con.execute(
                "UPDATE write_log SET status = 'rejected', decided_at = ?, applied_seq = NULL, error = ?, "
                "hold_reason = '', packet_json = ? WHERE change_id = ?",
                (decided_at, act["error"], act["packet_json"], act["change_id"]),
            )
            held_rejected += 1
        else:
            con.execute(
                "UPDATE write_log SET packet_json = ?, error = ?, hold_reason = ? WHERE change_id = ?",
                (act["packet_json"], act["error"], act["hold_reason"], act["change_id"]),
            )
        hit("write_log", act["change_id"])
    for rid in run_changes(con, chain, idset):
        con.execute("UPDATE collector_runs SET error = ? WHERE id = ?", (scrub.redaction_error(date), rid))
        hit("collector_runs", rid)

    # Step 10: flag what rested on it.
    first = next((sid for sid in live if not _is_excerpt(rows[sid])), live[0]) if live else ids[0]
    root_id = chain_root(ids, rows)
    superseding = {
        key
        for (key,) in con.execute(
            "SELECT DISTINCT key FROM claims WHERE superseded_by IN "
            f"(SELECT id FROM claims WHERE source_id IN {_IN_CHAIN} AND status = 'rejected')",
            (chain,),
        )
    }
    subjects = [("facts", key) for key in keys_touched if app._load_row("facts", "key", key) is not None]
    subjects += [
        ("decisions", did)
        for (did,) in con.execute(f"SELECT id FROM decisions WHERE source_id IN {_IN_CHAIN} ORDER BY id", (chain,))
    ]
    signals_opened = 0
    for table, subject in subjects:
        sig = signal_id(table, subject, root_id)
        if app._load_row("signals", "id", sig) is not None:
            continue
        note = "; earlier claims it superseded stay superseded" if table == "facts" and subject in superseding else ""
        con.execute(
            "INSERT INTO signals (id, kind, subject_table, subject_id, thread_id, detail, status, basis, resolved_at, "
            "source_id, locator, version, created_at, updated_at, updated_by) "
            "VALUES (?, 'drift', ?, ?, NULL, ?, 'open', 'observed', NULL, ?, 'redaction', 1, ?, ?, ?)",
            (sig, table, subject, f"source redacted on {date}{note}", first, decided_at, decided_at, stamp),
        )
        hit("signals", sig, "insert")
        signals_opened += 1

    # Step 11: close the gate and verify.
    con.execute("DELETE FROM redaction_packets")
    con.execute("DELETE FROM redaction_gate")
    leftover = store.redaction_problems(con, only=idset)
    if leftover:
        return f"op {label} (redact_source): the scrub would leave text behind: {leftover[0]}", "rejected", []

    app.redaction = {
        "sources": ids,
        "refs": sorted({rows[sid]["external_ref"] for sid in ids if rows[sid]["external_ref"]}),
        "held_rejected": held_rejected,
        "signals_opened": signals_opened,
        "threads_by_subject": [
            {"id": tid, "channel": channel, "account": account}
            for tid, (_version, channel, account) in sorted(by_subject.items())
        ],
        "date": date,
        "residual": residual,
    }
    return "", "applied", targets


def signal_id(table: str, subject: str, root_id: str) -> str:
    """The id of a redaction's drift signal: one per subject, whichever version of the item was named."""
    return "sig-redact-" + hashlib.sha256(f"redact\n{table}\n{subject}\n{root_id}".encode()).hexdigest()[:24]


def _contained(skeys, quote) -> bool:
    qkey = evidence_gate.match_key(quote)
    return bool(qkey) and any(evidence_gate.contains(key, qkey) for key in skeys)


def _subject_key(text: str) -> str:
    words = evidence_gate.match_key(text).split()
    while words and words[0] in ("re", "fwd", "fw"):
        words.pop(0)
    return " ".join(words)


def _linked_threads(con, ids, rows, live, chain, wrote=None):
    """(by source_id, by subject): thread id -> (version, channel, account)."""
    by_source: dict[str, tuple] = {}
    by_subject: dict[str, tuple] = {}
    for tid, version, channel, account in con.execute(
        f"SELECT id, version, channel, account FROM threads WHERE (source_id IN {_IN_CHAIN} OR id IN {_IN_CHAIN}) "
        "AND subject <> '' AND subject <> ?",
        (chain, json.dumps(sorted((wrote or {}).get("threads", ()))), scrub.REDACTED),
    ):
        by_source[tid] = (version, channel, account)
    wanted = set()
    for sid in live:
        if rows[sid]["kind"] in ("email", "slack", "meeting_transcript"):
            key = _subject_key(rows[sid]["title"])
            if key and (len(key.split()) >= 4 or len(key) >= 20):
                wanted.add(key)
    if wanted:
        for tid, subject, version, channel, account in con.execute(
            "SELECT id, subject, version, channel, account FROM threads WHERE subject <> '' AND subject <> ?",
            (scrub.REDACTED,),
        ):
            if tid not in by_source and _subject_key(subject) in wanted:
                by_subject[tid] = (version, channel, account)
    return by_source, by_subject


def _reindex_source(con, sid) -> None:
    """The three statements of the search_sources_ai trigger: sources has no update sync trigger, so this is
    the only thing that removes the text from the search index."""
    con.execute("INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('source', ?)", (sid,))
    con.execute(
        "DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'source' AND ref_id = ?)",
        (sid,),
    )
    con.execute(
        "INSERT INTO search_index (rowid, entity, ref_id, status, title, body) "
        "SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body FROM search_docs AS d JOIN search_keys AS k "
        "ON k.entity = d.entity AND k.ref_id = d.ref_id WHERE d.entity = 'source' AND d.ref_id = ?",
        (sid,),
    )


# -- what each step would change -----------------------------------------------------------------
# The selectors read and decide; the op writes. `survey` (the plan) reads the same selectors, so the plan
# counts what the op will change.

def claim_changes(con, chain):
    """(id, key, open, new quote, new text) for each chain claim the redaction changes."""
    out = []
    for cid, key, status, value_type, value_text, value_raw, quote in con.execute(
        f"SELECT id, key, status, value_type, value_text, value_raw, quote FROM claims "
        f"WHERE source_id IN {_IN_CHAIN} ORDER BY id",
        (chain,),
    ).fetchall():
        token = scrub.value_token(value_type)
        new_text = value_text if token is None else token
        new_quote = quote if quote in ("", None) else scrub.REDACTED
        open_claim = status in ("live", "planned")
        if open_claim or (new_quote, scrub.REDACTED, new_text) != (quote, value_raw, value_text):
            out.append((cid, key, open_claim, new_quote, new_text))
    return out


def _fact_values(value_type, value_text, quote):
    token = scrub.value_token(value_type)
    new_text = value_text if token is None else token
    new_quote = scrub.REDACTED if quote not in (None, "") else quote
    return new_text, new_quote


def fact_changes(con, chain):
    """(key, version, new text, new quote) for each fact still resting on a chain source."""
    out = []
    for key, value_type, value_text, quote, version in con.execute(
        f"SELECT key, value_type, value_text, quote, version FROM facts WHERE source_id IN {_IN_CHAIN} ORDER BY key",
        (chain,),
    ).fetchall():
        new_text, new_quote = _fact_values(value_type, value_text, quote)
        if (new_text, new_quote) != (value_text, quote):
            out.append((key, version, new_text, new_quote))
    return out


def history_changes(con, chain):
    out = []
    for hid, value_type, value_text, quote in con.execute(
        f"SELECT id, value_type, value_text, quote FROM fact_history WHERE source_id IN {_IN_CHAIN} ORDER BY id",
        (chain,),
    ).fetchall():
        new_text, new_quote = _fact_values(value_type, value_text, quote)
        if (new_text, new_quote) != (value_text, quote):
            out.append((hid, new_text, new_quote))
    return out


def log_rows_naming(con, chain):
    """The write_log rows that can name a chain source: one JSON parameter, so nothing grows with the chain."""
    return con.execute(
        f"SELECT change_id, status, packet_json, error, hold_reason, targets_json FROM write_log WHERE {_LOG_NAMES} "
        "ORDER BY change_id",
        (chain,),
    ).fetchall()


def rows_written(log_rows, idset) -> dict[str, set]:
    """table -> ids of the rows an applied packet that names a chain source wrote."""
    wrote: dict[str, set] = {}
    for _cid, status, packet_json, _error, _hold, targets_json in log_rows:
        if status != "applied" or not (scrub.named_sources(packet_json) & idset):
            continue
        for item in json.loads(targets_json):  # trusted: write_log.targets_json, written by the applier
            wrote.setdefault(item["table"], set()).add(str(item["id"]))
    return wrote


def rows_linked(con, chain, wrote: dict[str, set]) -> dict[str, set]:
    """`wrote` plus, by table, the ids of the rows that cite a chain source through links (0008). A link is a
    citation like a row's own source_id, so its row's quoted spans are scrubbed the same way. The links rows
    themselves hold no words of the source and are kept."""
    out = {table: set(found) for table, found in wrote.items()}
    for table, subject_id in con.execute(
        f"SELECT DISTINCT subject_table, subject_id FROM links WHERE source_id IN {_IN_CHAIN}", (chain,)
    ):
        out.setdefault(table, set()).add(str(subject_id))
    return out


def note_changes(con, ids, chain, skeys, wrote):
    """(table, id, {column: new value}) for each note row that cites a chain source or that a chain packet wrote."""
    idset = set(ids)
    out = []
    for table, columns in scrub.SPAN_COLUMNS.items():
        names = list(columns)
        extra = ["quote", "source_id"] if table == "decisions" else []
        listed = json.dumps(sorted(wrote.get(table, ())))
        for row in con.execute(
            f"SELECT id, {', '.join(names + extra)} FROM {table} "
            f"WHERE source_id IN {_IN_CHAIN} OR id IN {_IN_CHAIN} ORDER BY id",
            (chain, listed),
        ).fetchall():
            old = dict(zip(["id", *names, *extra], row))
            new = {c: scrub.scrub_spans(old[c], skeys, columns[c]) for c in names}
            if extra:
                quote = old["quote"]
                new["quote"] = quote
                if quote != scrub.REDACTED and (old["source_id"] in idset or _contained(skeys, quote)):
                    new["quote"] = scrub.REDACTED
            if any(new[c] != old[c] for c in new):
                out.append((table, old["id"], new))
    return out


def history_text_changes(con, chain, skeys, wrote):
    """(id, title, body, summary) for each record_history row that cites a chain source or belongs to a record
    that has such a version, that a chain packet wrote or that links to a chain source."""
    edited = {
        row[0]
        for row in con.execute(f"SELECT DISTINCT record_id FROM record_history WHERE source_id IN {_IN_CHAIN}", (chain,))
    } | wrote.get("records", set())
    out = []
    for hid, title, body, summary in con.execute(
        f"SELECT id, title, body, summary FROM record_history WHERE source_id IN {_IN_CHAIN} OR record_id IN {_IN_CHAIN} "
        "ORDER BY id",
        (chain, json.dumps(sorted(edited))),
    ).fetchall():
        new = tuple(scrub.scrub_spans(text, skeys) for text in (title, body, summary))
        if new != (title, body, summary):
            out.append((hid, *new))
    return out


def unchecked_changes(con, chain, skeys):
    out = []
    for entity, ref_id, sentences in con.execute(
        f"SELECT entity, ref_id, sentences FROM unchecked_text WHERE source_id IN {_IN_CHAIN} ORDER BY entity, ref_id",
        (chain,),
    ).fetchall():
        items = json.loads(sentences)  # trusted: unchecked_text.sentences, written by the applier
        for item in items:
            item["text"] = scrub.scrub_spans(item["text"], skeys, 300)
        updated = store.canonical_json(items)
        if updated != sentences:
            out.append((entity, ref_id, updated))
    return out


def meeting_changes(con, chain, wrote=None):
    return con.execute(
        f"SELECT id, version FROM meetings WHERE (source_id IN {_IN_CHAIN} OR id IN {_IN_CHAIN}) AND title <> ? ORDER BY id",
        (chain, json.dumps(sorted((wrote or {}).get("meetings", ()))), scrub.REDACTED),
    ).fetchall()


def log_actions(con, ids, log_rows, changed_rows, keys, types, date):
    """(change ids to list in redaction_packets, one action per write_log row the redaction changes)."""
    idset = set(ids)
    named = {cid: (status, packet_json, error, hold) for cid, status, packet_json, error, hold, _t in log_rows}
    later = []
    if changed_rows:
        later = [
            tuple(row)
            for row in con.execute(
                "SELECT change_id, status, packet_json, error, hold_reason FROM write_log AS w "
                "WHERE w.status = 'applied' AND EXISTS (SELECT 1 FROM json_each(w.targets_json) AS t "
                "WHERE json_extract(t.value, '$.table') || ':' || json_extract(t.value, '$.id') "
                "IN (SELECT value FROM json_each(?))) ORDER BY change_id",
                (json.dumps(sorted(f"{t}:{i}" for t, i in changed_rows)),),
            )
        ]
    work = [(cid, *named[cid]) for cid in sorted(named)] + [row for row in later if row[0] not in named]
    actions = []
    for cid, status, packet_json, error, hold in work:
        packet = json.loads(packet_json)  # trusted: write_log.packet_json, the packet the applier stored
        if packet.get("agent") == "brain" and any(
            isinstance(o, dict) and o.get("op") == "redact_source" for o in packet.get("ops") or ()
        ):
            continue
        new_packet, touched = scrub.scrub_packet(
            packet, keys, types, whole=status != "applied", date=date, cited=ids, rows=changed_rows
        )
        names_error = bool(scrub.named_sources(error) & idset)
        names_hold = bool(scrub.named_sources(hold) & idset)
        if not touched and not names_error and not names_hold:
            continue
        new_json = store.canonical_json(new_packet) if touched else packet_json
        held = status == "held" and bool(touched or names_error)
        if held:
            new_error, new_hold = scrub.redaction_error(date), ""
        else:
            new_error = scrub.redaction_error(date) if (names_error or (touched and error != "")) else error
            new_hold = "" if (touched or names_hold) else hold
        actions.append(
            {"change_id": cid, "held": held, "packet_json": new_json, "error": new_error, "hold_reason": new_hold}
        )
    return [row[0] for row in later], actions


def run_changes(con, chain, idset):
    return [
        rid
        for rid, error in con.execute(
            "SELECT id, error FROM collector_runs WHERE EXISTS (SELECT 1 FROM json_each(?) AS j "
            "WHERE instr(error, j.value) > 0) ORDER BY id",
            (chain,),
        ).fetchall()
        if scrub.named_sources(error) & idset
    ]


def survey(con, source_id: str) -> dict:
    """What a redaction of source_id would change, read without writing (the plan). It uses the
    selectors the op uses; the facts count is the facts that rest on a chain source, and a dispute that ends is
    not counted."""
    ids = chain_ids(con, source_id)
    chain = json.dumps(ids)
    idset = set(ids)
    rows = {}
    for sid in ids:
        row = con.execute(
            "SELECT id, kind, title, content, content_sha256, external_ref, supersedes_id FROM sources WHERE id = ?",
            (sid,),
        ).fetchone()
        rows[sid] = dict(zip(("id", "kind", "title", "content", "content_sha256", "external_ref", "supersedes_id"), row))
    live = [sid for sid in ids if not scrub.is_tombstone(rows[sid])]
    keys = {sid: scrub.source_key(rows[sid]["content"]) for sid in live}
    skeys = list(keys.values())
    counts: dict[str, int] = {}
    counts["sources"] = len(live)
    claims = claim_changes(con, chain)
    counts["claims"] = len(claims)
    touched = {key for _cid, key, _open, _q, _t in claims}
    facts = {c[0] for c in fact_changes(con, chain)}
    for (key,) in con.execute(f"SELECT key FROM facts WHERE source_id IN {_IN_CHAIN}", (chain,)):
        if key in touched:
            facts.add(key)
    counts["facts"] = len(facts)
    counts["fact_history"] = len(history_changes(con, chain))
    log_rows = log_rows_naming(con, chain)
    wrote = rows_linked(con, chain, rows_written(log_rows, idset))
    changed = note_changes(con, ids, chain, skeys, wrote)
    for table, _id_, _new in changed:
        counts[table] = counts.get(table, 0) + 1
    counts["record_history"] = len(history_text_changes(con, chain, skeys, wrote))
    counts["unchecked_text"] = len(unchecked_changes(con, chain, skeys))
    by_source, by_subject = _linked_threads(con, ids, rows, live, chain, wrote)
    counts["threads"] = len(by_source) + len(by_subject)
    counts["meetings"] = len(meeting_changes(con, chain, wrote))
    types = {row[0]: row[1] for row in con.execute("SELECT key, value_type FROM fact_keys")}
    changed_rows = {(table, str(row_id)) for table, row_id, _new in changed}
    _listed, actions = log_actions(con, ids, log_rows, changed_rows, keys, types, "0000-00-00")
    counts["write_log"] = len(actions)
    counts["collector_runs"] = len(run_changes(con, chain, idset))
    overrides = {(table, str(row_id)): new for table, row_id, new in changed}
    overrides.update({("threads", tid): {"subject": scrub.REDACTED} for tid in {**by_source, **by_subject}})
    overrides.update({("meetings", str(mid)): {"title": scrub.REDACTED} for mid, _version in meeting_changes(con, chain, wrote)})
    residual = residual_scan(con, skeys, overrides)
    # the drift signals the redaction would open, and the signals it rewrites
    root_id = chain_root(ids, rows)
    subjects = [("facts", key) for key in sorted(touched) if con.execute("SELECT 1 FROM facts WHERE key = ?", (key,)).fetchone()]
    subjects += [("decisions", did) for (did,) in con.execute(f"SELECT id FROM decisions WHERE source_id IN {_IN_CHAIN} ORDER BY id", (chain,))]
    for table, subject in subjects:
        ident = signal_id(table, subject, root_id)
        if con.execute("SELECT 1 FROM signals WHERE id = ?", (ident,)).fetchone() is None:
            counts["signals"] = counts.get("signals", 0) + 1
    return {
        "sources": ids,
        "refs": sorted({rows[sid]["external_ref"] for sid in ids if rows[sid]["external_ref"]}),
        "rows": {table: n for table, n in counts.items() if n},
        "write_log_held": sum(1 for act in actions if act["held"]),
        "threads_by_subject": [
            {"id": tid, "channel": channel, "account": account} for tid, (_v, channel, account) in sorted(by_subject.items())
        ],
        "residual": residual,
        "people": con.execute(f"SELECT count(*) FROM people WHERE source_id IN {_IN_CHAIN}", (chain,)).fetchone()[0],
        "systems": con.execute(f"SELECT count(*) FROM systems WHERE source_id IN {_IN_CHAIN}", (chain,)).fetchone()[0],
    }


# -- Unquoted copies of the removed text ---------------------------------------------------------

_RESIDUAL_COLUMNS = {**{table: tuple(columns) for table, columns in scrub.SPAN_COLUMNS.items()},
                     "threads": ("subject",), "meetings": ("title",)}


def residual_scan(con, skeys, overrides=None) -> dict[str, list[str]]:
    """table -> ids of the note rows (SPAN_COLUMNS tables, thread subjects and meeting titles) that still hold the
    text of a removed source though no quote marks mark it: a run of RESIDUAL_WORDS consecutive words, or a bracketed
    span the source contains. `overrides` maps (table, id) to the values the redaction is about to write. It
    reports ids only and changes nothing. With no source text left (a rerun) there is nothing to compare with."""
    overrides = overrides or {}
    keys = [key for key in skeys if key]
    if not keys:
        return {}
    shingles: set[str] = set()
    for key in keys:
        words = key.split()
        if len(words) >= RESIDUAL_WORDS:
            shingles.update(" ".join(words[i : i + RESIDUAL_WORDS]) for i in range(len(words) - RESIDUAL_WORDS + 1))

    def holds(text) -> bool:
        if not isinstance(text, str) or not text or text == scrub.REDACTED:
            return False
        words = evidence_gate.match_key(text).split()
        if any(" ".join(words[i : i + RESIDUAL_WORDS]) in shingles for i in range(len(words) - RESIDUAL_WORDS + 1)):
            return True
        for match in _BRACKETED.finditer(text):
            span = next(group for group in match.groups() if group is not None)
            inner = evidence_gate.match_key(span)
            if len(span.strip()) >= RESIDUAL_SPAN and inner and any(evidence_gate.contains(key, inner) for key in keys):
                return True
        return False

    found: dict[str, list[str]] = {}
    for table, columns in _RESIDUAL_COLUMNS.items():
        for row in con.execute(f"SELECT id, {', '.join(columns)} FROM {table} ORDER BY id"):
            changed = overrides.get((table, str(row[0])), {})
            values = [changed.get(column, value) for column, value in zip(columns, row[1:])]
            if any(holds(value) for value in values):
                found.setdefault(table, []).append(str(row[0]))
    return found
