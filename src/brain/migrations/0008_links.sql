-- 0008_links.sql: links with sources, version 8. A record, action,
-- checklist item or decision can cite many sources. The row's own source_id and locator stay its primary
-- citation; links holds the others, each with a label. Records gain a short summary, and each version in
-- record_history keeps it.
-- brain.migrate supplies the transaction, the user_version bump and the write guards, so this file
-- holds no transaction or pragma statement.

-- links: one row per (subject, label, source). Append-only in v1: no retirement and no locator change.
CREATE TABLE links (
  subject_table TEXT NOT NULL CHECK (subject_table IN ('records', 'actions', 'checklist_items', 'decisions')),
  subject_id    TEXT NOT NULL CHECK (length(subject_id) BETWEEN 1 AND 120),
  label         TEXT NOT NULL CHECK (label IN ('cites', 'status', 'due', 'asked', 'summary')),
  source_id     TEXT NOT NULL REFERENCES sources(id),
  locator       TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                     AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  created_at    TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  created_by    TEXT NOT NULL CHECK (length(created_by) BETWEEN 3 AND 200),
  PRIMARY KEY (subject_table, subject_id, label, source_id)
) STRICT;
CREATE INDEX links_by_source ON links(source_id);
-- A link names a row that exists: subject_table picks the table, so no single foreign key can say it.
CREATE TRIGGER links_subject_exists BEFORE INSERT ON links
WHEN NOT CASE NEW.subject_table
  WHEN 'records'         THEN EXISTS (SELECT 1 FROM records WHERE id = NEW.subject_id)
  WHEN 'actions'         THEN EXISTS (SELECT 1 FROM actions WHERE id = NEW.subject_id)
  WHEN 'checklist_items' THEN EXISTS (SELECT 1 FROM checklist_items WHERE id = NEW.subject_id)
  WHEN 'decisions'       THEN EXISTS (SELECT 1 FROM decisions WHERE id = NEW.subject_id)
  ELSE 0
END
BEGIN SELECT RAISE(ABORT, 'links: the subject row does not exist'); END;
CREATE TRIGGER links_no_update BEFORE UPDATE ON links
BEGIN SELECT RAISE(ABORT, 'links are append-only: add a new link'); END;
CREATE TRIGGER links_no_delete BEFORE DELETE ON links
BEGIN SELECT RAISE(ABORT, 'links are append-only'); END;

-- records.summary, and the same column on each history version.
ALTER TABLE records ADD COLUMN summary TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 1200);
ALTER TABLE record_history ADD COLUMN summary TEXT NOT NULL DEFAULT '';

DROP TRIGGER records_history_on_insert;
CREATE TRIGGER records_history_on_insert AFTER INSERT ON records
BEGIN
  INSERT INTO record_history (record_id, version, change, kind, title, body, status, project_id,
                              source_id, locator, recorded_at, recorded_by, summary)
  VALUES (NEW.id, NEW.version, 'created', NEW.kind, NEW.title, NEW.body, NEW.status, NEW.project_id,
          NEW.source_id, NEW.locator, NEW.updated_at, NEW.updated_by, NEW.summary);
END;
DROP TRIGGER records_history_on_update;
CREATE TRIGGER records_history_on_update AFTER UPDATE ON records
BEGIN
  INSERT INTO record_history (record_id, version, change, kind, title, body, status, project_id,
                              source_id, locator, recorded_at, recorded_by, summary)
  VALUES (NEW.id, NEW.version, 'updated', NEW.kind, NEW.title, NEW.body, NEW.status, NEW.project_id,
          NEW.source_id, NEW.locator, NEW.updated_at, NEW.updated_by, NEW.summary);
END;

-- record_history under redaction (0006): the summary changes only as the title and body may, to the token.
DROP TRIGGER record_history_no_update;
CREATE TRIGGER record_history_no_update BEFORE UPDATE ON record_history
WHEN NOT EXISTS (
  SELECT 1 FROM redaction_gate AS g
  WHERE (g.source_id = OLD.source_id
         OR EXISTS (SELECT 1 FROM record_history AS h WHERE h.record_id = OLD.record_id AND h.source_id = g.source_id)
         OR EXISTS (SELECT 1 FROM links AS l WHERE l.subject_table = 'records' AND l.subject_id = OLD.record_id
                                               AND l.source_id = g.source_id))
    AND NEW.id IS OLD.id AND NEW.record_id IS OLD.record_id AND NEW.version IS OLD.version
    AND NEW.change IS OLD.change AND NEW.kind IS OLD.kind AND NEW.status IS OLD.status
    AND NEW.project_id IS OLD.project_id AND NEW.source_id IS OLD.source_id AND NEW.locator IS OLD.locator
    AND NEW.recorded_at IS OLD.recorded_at AND NEW.recorded_by IS OLD.recorded_by
    AND (NEW.title IS OLD.title
         OR (instr(NEW.title, '[redacted]') > 0
             AND length(NEW.title) <= length(OLD.title)
                 + 10 * ((length(NEW.title) - length(replace(NEW.title, '[redacted]', ''))) / 10)))
    AND (NEW.body IS OLD.body
         OR (instr(NEW.body, '[redacted]') > 0
             AND length(NEW.body) <= length(OLD.body)
                 + 10 * ((length(NEW.body) - length(replace(NEW.body, '[redacted]', ''))) / 10)))
    AND (NEW.summary IS OLD.summary
         OR (instr(NEW.summary, '[redacted]') > 0
             AND length(NEW.summary) <= length(OLD.summary)
                 + 10 * ((length(NEW.summary) - length(replace(NEW.summary, '[redacted]', ''))) / 10))))
BEGIN SELECT RAISE(ABORT, 'record_history is append-only'); END;

-- Search: a record document gains its summary after the body, appended only when non-empty, so every existing
-- document stays byte for byte the same. The record triggers read this view, so a summary edit re-indexes.
DROP VIEW search_docs;
CREATE VIEW search_docs AS
SELECT 'record' AS entity, id AS ref_id, status AS status, title AS title,
       body || CASE WHEN summary <> '' THEN char(10) || summary ELSE '' END AS body
FROM records
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
