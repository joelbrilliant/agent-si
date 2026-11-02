-- 0002_search.sql: agent-brain search index, version 2.
-- Schema only. brain migrate creates these objects, then brain migrate fills the index through
-- the applier (op rebuild_search) when the store already holds rows. No BEGIN, COMMIT or PRAGMA.

-- search_index: FTS5 over one projection per searchable row. Virtual and shadow tables carry no
-- guard triggers (SQLite refuses triggers on virtual tables). Only the sync triggers below and
-- rebuild_search write it, inside the applier's transaction.
CREATE VIRTUAL TABLE search_index USING fts5(
  entity UNINDEXED,
  ref_id UNINDEXED,
  status UNINDEXED,
  title,
  body,
  tokenize = 'unicode61 remove_diacritics 2'
);

-- search_keys: maps each searchable row to its search_index rowid. A STRICT base table, so the
-- migrator gives it the usual guards and only the applier's context can write it.
CREATE TABLE search_keys (
  doc    INTEGER PRIMARY KEY,
  entity TEXT NOT NULL CHECK (entity IN ('record', 'decision', 'action', 'checklist_item', 'project',
                                         'person', 'system', 'fact', 'thread', 'source')),
  ref_id TEXT NOT NULL CHECK (length(ref_id) BETWEEN 1 AND 120),
  UNIQUE (entity, ref_id)
) STRICT;

-- search_docs: the one definition of what each entity contributes to the index. The sync
-- triggers, rebuild_search, the drift check and search's hit verification all read this view.
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
SELECT 'fact', key, '', key, description || char(10) || unit FROM fact_keys
UNION ALL
SELECT 'thread', id, '', subject, channel || char(10) || account FROM threads
UNION ALL
SELECT 'source', id, kind, title, content FROM sources WHERE kind <> 'config';

-- Sync triggers. Insert and update run the same three steps, so a row the index has never
-- seen (after a migration, before the rebuild) gets its key on first write.

-- records
CREATE TRIGGER search_records_ai AFTER INSERT ON records BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('record', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'record' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'record' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_records_au AFTER UPDATE ON records BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('record', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'record' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'record' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_records_ad AFTER DELETE ON records BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'record' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'record' AND ref_id = OLD.id;
END;

-- decisions
CREATE TRIGGER search_decisions_ai AFTER INSERT ON decisions BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('decision', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'decision' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'decision' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_decisions_au AFTER UPDATE ON decisions BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('decision', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'decision' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'decision' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_decisions_ad AFTER DELETE ON decisions BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'decision' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'decision' AND ref_id = OLD.id;
END;

-- actions
CREATE TRIGGER search_actions_ai AFTER INSERT ON actions BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('action', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'action' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'action' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_actions_au AFTER UPDATE ON actions BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('action', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'action' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'action' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_actions_ad AFTER DELETE ON actions BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'action' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'action' AND ref_id = OLD.id;
END;

-- checklist_items
CREATE TRIGGER search_checklist_items_ai AFTER INSERT ON checklist_items BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('checklist_item', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'checklist_item' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'checklist_item' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_checklist_items_au AFTER UPDATE ON checklist_items BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('checklist_item', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'checklist_item' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'checklist_item' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_checklist_items_ad AFTER DELETE ON checklist_items BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'checklist_item' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'checklist_item' AND ref_id = OLD.id;
END;

-- projects
CREATE TRIGGER search_projects_ai AFTER INSERT ON projects BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('project', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'project' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'project' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_projects_au AFTER UPDATE ON projects BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('project', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'project' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'project' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_projects_ad AFTER DELETE ON projects BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'project' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'project' AND ref_id = OLD.id;
END;

-- people
CREATE TRIGGER search_people_ai AFTER INSERT ON people BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('person', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'person' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'person' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_people_au AFTER UPDATE ON people BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('person', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'person' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'person' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_people_ad AFTER DELETE ON people BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'person' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'person' AND ref_id = OLD.id;
END;

-- systems
CREATE TRIGGER search_systems_ai AFTER INSERT ON systems BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('system', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'system' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'system' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_systems_au AFTER UPDATE ON systems BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('system', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'system' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'system' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_systems_ad AFTER DELETE ON systems BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'system' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'system' AND ref_id = OLD.id;
END;

-- fact_keys
CREATE TRIGGER search_fact_keys_ai AFTER INSERT ON fact_keys BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('fact', NEW.key);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'fact' AND ref_id = NEW.key);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'fact' AND d.ref_id = NEW.key;
END;
CREATE TRIGGER search_fact_keys_au AFTER UPDATE ON fact_keys BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('fact', NEW.key);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'fact' AND ref_id = NEW.key);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'fact' AND d.ref_id = NEW.key;
END;
CREATE TRIGGER search_fact_keys_ad AFTER DELETE ON fact_keys BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'fact' AND ref_id = OLD.key);
  DELETE FROM search_keys WHERE entity = 'fact' AND ref_id = OLD.key;
END;

-- threads
CREATE TRIGGER search_threads_ai AFTER INSERT ON threads BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('thread', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'thread' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'thread' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_threads_au AFTER UPDATE ON threads BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('thread', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'thread' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'thread' AND d.ref_id = NEW.id;
END;
CREATE TRIGGER search_threads_ad AFTER DELETE ON threads BEGIN
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'thread' AND ref_id = OLD.id);
  DELETE FROM search_keys WHERE entity = 'thread' AND ref_id = OLD.id;
END;

-- sources: insert only (sources_never_change and sources_never_deleted abort the rest).
-- Config snapshots stay out of the index.
CREATE TRIGGER search_sources_ai AFTER INSERT ON sources WHEN NEW.kind <> 'config' BEGIN
  INSERT OR IGNORE INTO search_keys (entity, ref_id) VALUES ('source', NEW.id);
  DELETE FROM search_index WHERE rowid = (SELECT doc FROM search_keys WHERE entity = 'source' AND ref_id = NEW.id);
  INSERT INTO search_index (rowid, entity, ref_id, status, title, body)
  SELECT k.doc, d.entity, d.ref_id, d.status, d.title, d.body
  FROM search_docs AS d JOIN search_keys AS k ON k.entity = d.entity AND k.ref_id = d.ref_id
  WHERE d.entity = 'source' AND d.ref_id = NEW.id;
END;
