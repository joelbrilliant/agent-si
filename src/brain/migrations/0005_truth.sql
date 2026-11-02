-- 0005_truth.sql: product truth, version 5. Claims under facts, aliases, the conflict signal
-- kind, and every other column. brain.migrate supplies the transaction, the
-- user_version bump and the write guards, so this file holds no transaction or pragma statement.

-- claims: one sourced value for one fact key in one scope. Its content never changes. Only the
-- applier moves status, superseded_by, the resolution columns and updated_at/updated_by.
CREATE TABLE claims (
  id                   TEXT PRIMARY KEY
                       CHECK (id GLOB 'clm-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  key                  TEXT NOT NULL REFERENCES fact_keys(key),
  scope                TEXT NOT NULL DEFAULT ''
                       CHECK (scope = '' OR (length(scope) <= 40 AND scope GLOB '[A-Za-z0-9]*'
                                             AND scope NOT GLOB '*[^A-Za-z0-9._-]*' AND lower(scope) <> 'now')),
  value_type           TEXT NOT NULL CHECK (value_type IN ('text', 'int', 'number', 'date', 'bool', 'url', 'enum')),
  value_text           TEXT CHECK (value_text IS NULL OR length(value_text) <= 2000),
  value_int            INTEGER,
  value_real           REAL,
  value_date           TEXT,
  value_raw            TEXT NOT NULL CHECK (length(value_raw) BETWEEN 1 AND 2000),
  status               TEXT NOT NULL CHECK (status IN ('live', 'planned', 'superseded', 'rejected')),
  basis                TEXT NOT NULL CHECK (basis IN ('observed', 'reported', 'inferred')),
  as_of                TEXT NOT NULL CHECK (date(as_of) IS as_of),
  source_id            TEXT NOT NULL REFERENCES sources(id),
  locator              TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                            AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  quote                TEXT NOT NULL DEFAULT '' CHECK (length(quote) <= 500),
  checked              INTEGER NOT NULL CHECK (checked IN (0, 1)),
  supersedes           TEXT NOT NULL DEFAULT '[]'
                       CHECK (json_valid(supersedes) AND json_type(supersedes) = 'array'
                              AND json_array_length(supersedes) <= 20),
  proposed_by          TEXT NOT NULL CHECK (length(proposed_by) BETWEEN 1 AND 200),
  superseded_by        TEXT REFERENCES claims(id) CHECK (superseded_by IS NULL OR superseded_by <> id),
  resolution_source_id TEXT REFERENCES sources(id),
  resolution_locator   TEXT CHECK (resolution_locator IS NULL OR (length(trim(resolution_locator)) BETWEEN 1 AND 200
                                   AND instr(resolution_locator, char(10)) = 0
                                   AND instr(resolution_locator, char(13)) = 0)),
  created_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  created_by           TEXT NOT NULL CHECK (length(created_by) BETWEEN 3 AND 200),
  updated_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by           TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  CHECK ((value_text IS NOT NULL) + (value_int IS NOT NULL) + (value_real IS NOT NULL) + (value_date IS NOT NULL) = 1),
  CHECK (CASE value_type
           WHEN 'text'   THEN value_text IS NOT NULL AND length(trim(value_text)) > 0
           WHEN 'enum'   THEN value_text IS NOT NULL AND length(value_text) > 0
           WHEN 'url'    THEN value_text IS NOT NULL AND (value_text GLOB 'https://?*' OR value_text GLOB 'http://?*')
           WHEN 'int'    THEN value_int IS NOT NULL
           WHEN 'bool'   THEN value_int IS NOT NULL AND value_int IN (0, 1)
           WHEN 'number' THEN value_real IS NOT NULL
           WHEN 'date'   THEN value_date IS NOT NULL AND date(value_date) IS value_date
           ELSE 0
         END),
  CHECK (checked = 0 OR length(trim(quote)) > 0),
  CHECK (superseded_by IS NULL OR status = 'superseded'),
  CHECK ((status = 'rejected') = (resolution_source_id IS NOT NULL)),
  CHECK ((resolution_source_id IS NULL) = (resolution_locator IS NULL))
) STRICT;
CREATE INDEX claims_by_key ON claims(key, status, scope);
CREATE INDEX claims_by_source ON claims(source_id);
CREATE TRIGGER claims_content_fixed BEFORE UPDATE ON claims
WHEN NEW.id IS NOT OLD.id OR NEW.key IS NOT OLD.key OR NEW.scope IS NOT OLD.scope
  OR NEW.value_type IS NOT OLD.value_type OR NEW.value_text IS NOT OLD.value_text
  OR NEW.value_int IS NOT OLD.value_int OR NEW.value_real IS NOT OLD.value_real
  OR NEW.value_date IS NOT OLD.value_date OR NEW.value_raw IS NOT OLD.value_raw
  OR NEW.basis IS NOT OLD.basis OR NEW.as_of IS NOT OLD.as_of
  OR NEW.source_id IS NOT OLD.source_id OR NEW.locator IS NOT OLD.locator
  OR NEW.quote IS NOT OLD.quote OR NEW.checked IS NOT OLD.checked
  OR NEW.supersedes IS NOT OLD.supersedes OR NEW.proposed_by IS NOT OLD.proposed_by
  OR NEW.created_at IS NOT OLD.created_at OR NEW.created_by IS NOT OLD.created_by
BEGIN SELECT RAISE(ABORT, 'claims never change: add a new claim'); END;
CREATE TRIGGER claims_never_deleted BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are never deleted'); END;

-- aliases: words that resolve to an entity or an attribute, loaded from brain.toml [truth] by
-- brain apply-config. alias holds the normal form of the alias.
CREATE TABLE aliases (
  kind   TEXT NOT NULL CHECK (kind IN ('entity', 'attribute')),
  alias  TEXT NOT NULL CHECK (length(alias) BETWEEN 1 AND 80),
  target TEXT NOT NULL CHECK (length(target) BETWEEN 1 AND 119 AND target NOT GLOB '*[^a-z0-9_.]*'),
  PRIMARY KEY (kind, alias)
) STRICT;
CREATE INDEX aliases_by_target ON aliases(kind, target);

-- note_meta: who last wrote each note field, and when. Created here, written by the strict writes.
CREATE TABLE note_meta (
  entity     TEXT NOT NULL CHECK (entity IN ('action', 'checklist_item', 'project', 'decision', 'record')),
  ref_id     TEXT NOT NULL CHECK (length(ref_id) BETWEEN 1 AND 120),
  field      TEXT NOT NULL CHECK (CASE entity
                                    WHEN 'action'         THEN field IN ('detail', 'next_step', 'close_reason')
                                    WHEN 'checklist_item' THEN field = 'detail'
                                    WHEN 'project'        THEN field IN ('description', 'goal')
                                    WHEN 'decision'       THEN field = 'detail'
                                    WHEN 'record'         THEN field = 'body'
                                    ELSE 0
                                  END),
  written_by TEXT NOT NULL CHECK (length(written_by) BETWEEN 2 AND 40),
  written_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', written_at) IS written_at),
  PRIMARY KEY (entity, ref_id, field)
) STRICT;

-- New columns. facts.quote and fact_history.quote are null only for rows written before 0005.
-- write_log.hold_reason names why a packet is held.
ALTER TABLE facts ADD COLUMN quote TEXT CHECK (quote IS NULL OR length(quote) <= 500);
ALTER TABLE fact_history ADD COLUMN quote TEXT;
ALTER TABLE fact_keys ADD COLUMN value_words TEXT
  CHECK (value_words IS NULL OR (json_valid(value_words) AND json_type(value_words) = 'object'));
ALTER TABLE write_log ADD COLUMN approved_via TEXT NOT NULL DEFAULT ''
  CHECK (length(approved_via) <= 40 AND approved_via NOT GLOB '*[^a-z-]*');
ALTER TABLE write_log ADD COLUMN hold_reason TEXT NOT NULL DEFAULT '' CHECK (length(hold_reason) <= 2000);
ALTER TABLE sources ADD COLUMN supersedes_id TEXT REFERENCES sources(id)
  CHECK (supersedes_id IS NULL OR supersedes_id <> id);
CREATE UNIQUE INDEX sources_one_successor ON sources(supersedes_id) WHERE supersedes_id IS NOT NULL;
CREATE INDEX sources_by_external_ref ON sources(external_ref) WHERE external_ref <> '';

-- The fact history triggers carry the quote.
DROP TRIGGER facts_history_on_insert;
DROP TRIGGER facts_history_on_update;
CREATE TRIGGER facts_history_on_insert AFTER INSERT ON facts
BEGIN
  INSERT INTO fact_history (key, version, change, value_type, value_text, value_int, value_real, value_date,
                            status, basis, as_of, last_verified_at, source_id, locator, recorded_at, recorded_by,
                            quote)
  VALUES (NEW.key, NEW.version, 'created', NEW.value_type, NEW.value_text, NEW.value_int, NEW.value_real,
          NEW.value_date, NEW.status, NEW.basis, NEW.as_of, NEW.last_verified_at, NEW.source_id, NEW.locator,
          NEW.updated_at, NEW.updated_by, NEW.quote);
END;
CREATE TRIGGER facts_history_on_update AFTER UPDATE ON facts
BEGIN
  INSERT INTO fact_history (key, version, change, value_type, value_text, value_int, value_real, value_date,
                            status, basis, as_of, last_verified_at, source_id, locator, recorded_at, recorded_by,
                            quote)
  VALUES (NEW.key, NEW.version, 'updated', NEW.value_type, NEW.value_text, NEW.value_int, NEW.value_real,
          NEW.value_date, NEW.status, NEW.basis, NEW.as_of, NEW.last_verified_at, NEW.source_id, NEW.locator,
          NEW.updated_at, NEW.updated_by, NEW.quote);
END;

-- signals: rebuilt for the conflict kind (SQLite can't alter a CHECK in place). A conflict
-- signal always names a fact.
CREATE TABLE signals_new (
  id            TEXT PRIMARY KEY
                CHECK (id GLOB 'sig-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  kind          TEXT NOT NULL CHECK (kind IN ('reply', 'new_message', 'overdue', 'stale', 'drift', 'conflict', 'other')),
  subject_table TEXT NOT NULL CHECK (subject_table IN ('actions', 'checklist_items', 'decisions', 'facts',
                                                       'projects', 'records')),
  subject_id    TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 120),
  thread_id     TEXT REFERENCES threads(id),
  detail        TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 500),
  status        TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'dismissed', 'resolved')),
  basis         TEXT NOT NULL CHECK (basis IN ('observed', 'inferred')),
  resolved_at   TEXT CHECK (resolved_at IS NULL OR strftime('%Y-%m-%dT%H:%M:%SZ', resolved_at) IS resolved_at),
  source_id     TEXT NOT NULL REFERENCES sources(id),
  locator       TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                     AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version       INTEGER NOT NULL CHECK (version >= 1),
  created_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by    TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  CHECK (kind NOT IN ('reply', 'new_message')
         OR (thread_id IS NOT NULL AND subject_table IN ('actions', 'checklist_items'))),
  CHECK (kind <> 'conflict' OR (subject_table = 'facts' AND thread_id IS NULL)),
  CHECK ((status = 'open') = (resolved_at IS NULL))
) STRICT;
INSERT INTO signals_new (id, kind, subject_table, subject_id, thread_id, detail, status, basis, resolved_at,
                         source_id, locator, version, created_at, updated_at, updated_by)
SELECT id, kind, subject_table, subject_id, thread_id, detail, status, basis, resolved_at,
       source_id, locator, version, created_at, updated_at, updated_by
FROM signals;
-- A fact marked disputed by hand stays disputed: its open conflict signal is the derivation's
-- input, and only resolve_fact closes it.
INSERT INTO signals_new (id, kind, subject_table, subject_id, thread_id, detail, status, basis, resolved_at,
                         source_id, locator, version, created_at, updated_at, updated_by)
SELECT 'sig-conflict-m5-' || f.rowid, 'conflict', 'facts', f.key, NULL, 'marked disputed before 0.7.0',
       'open', 'observed', NULL, f.source_id, f.locator, 1, f.updated_at, f.updated_at, 'brain:migrate-0005'
FROM facts AS f WHERE f.status = 'disputed';
DROP TABLE signals;
ALTER TABLE signals_new RENAME TO signals;
CREATE INDEX signals_by_status_kind ON signals(status, kind);
CREATE INDEX signals_by_subject ON signals(subject_table, subject_id);

-- Each existing fact becomes one unchecked, unscoped claim. Headlines are unchanged.
INSERT INTO claims (id, key, scope, value_type, value_text, value_int, value_real, value_date, value_raw,
                    status, basis, as_of, source_id, locator, quote, checked, supersedes, proposed_by,
                    superseded_by, resolution_source_id, resolution_locator,
                    created_at, created_by, updated_at, updated_by)
SELECT 'clm-m5-' || f.rowid, f.key, '', f.value_type, f.value_text, f.value_int, f.value_real, f.value_date,
       CASE f.value_type
         WHEN 'bool'   THEN CASE f.value_int WHEN 1 THEN 'true' ELSE 'false' END
         WHEN 'int'    THEN CAST(f.value_int AS TEXT)
         WHEN 'number' THEN printf('%.15g', f.value_real)
         WHEN 'date'   THEN f.value_date
         ELSE f.value_text
       END,
       CASE f.status WHEN 'retired' THEN 'superseded' ELSE 'live' END,
       f.basis, f.as_of, f.source_id, f.locator, '', 0, '[]',
       CASE WHEN instr(f.updated_by, ':') > 1 THEN substr(f.updated_by, 1, instr(f.updated_by, ':') - 1)
            ELSE f.updated_by END,
       NULL, NULL, NULL, f.updated_at, f.updated_by, f.updated_at, f.updated_by
FROM facts AS f;

-- Held claim candidates in format 1 can't be applied by any later engine, so they are refused.
-- The guard is dropped for this one statement; brain.migrate reinstalls it before the commit.
DROP TRIGGER guard_write_log_update;
UPDATE write_log
SET status = 'rejected',
    decided_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
    error = 'candidate format changed in 0.7.0'
WHERE status = 'held'
  AND json_array_length(packet_json, '$.ops') = 1
  AND json_extract(packet_json, '$.ops[0].op') = 'apply_candidate'
  AND json_extract(packet_json, '$.ops[0].candidate.kind') = 'claim_candidate';

-- Search: a fact document gains its claims' quotes and its aliases, each part appended only when
-- non-empty, so every existing document stays byte for byte the same.
CREATE VIEW fact_key_parts AS
SELECT key,
       substr(key, 1, length(rtrim(key, replace(key, '.', ''))) - 1) AS entity,
       substr(key, length(rtrim(key, replace(key, '.', ''))) + 1) AS attribute
FROM fact_keys;
DROP VIEW search_docs;
CREATE VIEW search_docs AS
SELECT 'record' AS entity, id AS ref_id, status AS status, title AS title, body AS body FROM records
UNION ALL
SELECT 'decision', id, status, title, detail || char(10) || quote FROM decisions
UNION ALL
SELECT 'action', id, status, title, detail || char(10) || next_step || char(10) || close_reason FROM actions
UNION ALL
SELECT 'checklist_item', id, state, title, section || char(10) || detail FROM checklist_items
UNION ALL
SELECT 'project', id, status, title, description || char(10) || goal FROM projects
UNION ALL
SELECT 'person', id, '', display_name, org || char(10) || role || char(10) || aliases FROM people
UNION ALL
SELECT 'system', id, '', name, kind || char(10) || vendor FROM systems
UNION ALL
SELECT 'fact', k.key, '', k.key,
       k.description || char(10) || k.unit
       || coalesce((SELECT char(10) || group_concat(q, char(10))
                    FROM (SELECT DISTINCT c.quote AS q FROM claims AS c
                          WHERE c.key = k.key AND c.quote <> '' ORDER BY c.quote)), '')
       || coalesce((SELECT char(10) || group_concat(w, char(10))
                    FROM (SELECT a.alias AS w FROM aliases AS a JOIN fact_key_parts AS p ON p.key = k.key
                          WHERE (a.kind = 'entity' AND a.target = p.entity)
                             OR (a.kind = 'attribute' AND a.target = p.attribute)
                          ORDER BY a.kind, a.alias)), '')
FROM fact_keys AS k
UNION ALL
SELECT 'thread', id, '', subject, channel || char(10) || account FROM threads
UNION ALL
SELECT 'source', id, kind, title, content FROM sources WHERE kind <> 'config';

-- claims: a new claim, or a status change, re-indexes its fact document.
CREATE TRIGGER search_claims_ai AFTER INSERT ON claims BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('fact', NEW.key);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'fact' AND ref_id = NEW.key);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'fact' AND d.ref_id = NEW.key;
END;
CREATE TRIGGER search_claims_au AFTER UPDATE ON claims BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('fact', NEW.key);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'fact' AND ref_id = NEW.key);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'fact' AND d.ref_id = NEW.key;
END;

-- aliases: an added or removed alias re-indexes every fact document of its entity or attribute.
CREATE TRIGGER search_aliases_ai AFTER INSERT ON aliases BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id)
  SELECT 'fact', p.key FROM fact_key_parts AS p
  WHERE (NEW.kind = 'entity' AND p.entity = NEW.target) OR (NEW.kind = 'attribute' AND p.attribute = NEW.target);
  DELETE FROM search_index WHERE rowid IN (
    SELECT k.doc FROM search_keys AS k JOIN fact_key_parts AS p ON k.entity = 'fact' AND k.ref_id = p.key
    WHERE (NEW.kind = 'entity' AND p.entity = NEW.target) OR (NEW.kind = 'attribute' AND p.attribute = NEW.target));
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  JOIN fact_key_parts AS p ON p.key = d.ref_id
  WHERE d.entity = 'fact'
    AND ((NEW.kind = 'entity' AND p.entity = NEW.target) OR (NEW.kind = 'attribute' AND p.attribute = NEW.target));
END;
CREATE TRIGGER search_aliases_ad AFTER DELETE ON aliases BEGIN
  DELETE FROM search_index WHERE rowid IN (
    SELECT k.doc FROM search_keys AS k JOIN fact_key_parts AS p ON k.entity = 'fact' AND k.ref_id = p.key
    WHERE (OLD.kind = 'entity' AND p.entity = OLD.target) OR (OLD.kind = 'attribute' AND p.attribute = OLD.target));
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  JOIN fact_key_parts AS p ON p.key = d.ref_id
  WHERE d.entity = 'fact'
    AND ((OLD.kind = 'entity' AND p.entity = OLD.target) OR (OLD.kind = 'attribute' AND p.attribute = OLD.target));
END;
