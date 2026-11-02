-- 1.8.0: a source's author can be corrected, and nothing else about it.
-- A bulk import gave the principal every email, Slack and meeting transcript source,
-- whoever wrote them, so the evidence gate took other people's words as the principal's. set_source_author
-- logs each correction here, with the author it replaced, and the trigger below lets a source change only
-- where a pending row of this table names that exact change.
CREATE TABLE source_author_changes (
  change_id            TEXT NOT NULL CHECK (length(change_id) BETWEEN 1 AND 200),
  source_id            TEXT NOT NULL REFERENCES sources(id),
  old_author_person_id TEXT REFERENCES people(id),
  new_author_person_id TEXT REFERENCES people(id),
  evidence_source_id   TEXT NOT NULL REFERENCES sources(id),
  locator              TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200),
  changed_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', changed_at) IS changed_at),
  changed_by           TEXT NOT NULL CHECK (length(changed_by) BETWEEN 3 AND 300),
  applied              INTEGER NOT NULL DEFAULT 0 CHECK (applied IN (0, 1)),
  PRIMARY KEY (change_id, source_id),
  CHECK (old_author_person_id IS NOT new_author_person_id)
) STRICT;
CREATE INDEX source_author_changes_by_source ON source_author_changes(source_id);

-- sources: a source changes only to the redaction tombstone (0006), or to a new author that a pending
-- source_author_changes row names, with every other column as it was.
DROP TRIGGER sources_never_change;
CREATE TRIGGER sources_never_change BEFORE UPDATE ON sources
WHEN NOT EXISTS (
  SELECT 1 FROM redaction_gate AS g
  WHERE g.source_id = OLD.id
    AND NEW.title = 'redacted' AND NEW.content = g.tombstone AND NEW.uri = '' AND NEW.author_person_id IS NULL
    AND NEW.id IS OLD.id AND NEW.kind IS OLD.kind AND NEW.source_date IS OLD.source_date
    AND NEW.content_sha256 IS OLD.content_sha256 AND NEW.external_ref IS OLD.external_ref
    AND NEW.captured_at IS OLD.captured_at AND NEW.captured_by IS OLD.captured_by
    AND NEW.supersedes_id IS OLD.supersedes_id)
AND NOT EXISTS (
  SELECT 1 FROM source_author_changes AS a
  WHERE a.source_id = OLD.id AND a.applied = 0
    AND a.old_author_person_id IS OLD.author_person_id AND a.new_author_person_id IS NEW.author_person_id
    AND NEW.id IS OLD.id AND NEW.kind IS OLD.kind AND NEW.title IS OLD.title AND NEW.uri IS OLD.uri
    AND NEW.source_date IS OLD.source_date AND NEW.content IS OLD.content
    AND NEW.content_sha256 IS OLD.content_sha256 AND NEW.external_ref IS OLD.external_ref
    AND NEW.captured_at IS OLD.captured_at AND NEW.captured_by IS OLD.captured_by
    AND NEW.supersedes_id IS OLD.supersedes_id)
BEGIN SELECT RAISE(ABORT, 'sources never change: add a new source with a new id'); END;

-- source_author_changes: a row is written pending, marked applied once, and never changes again.
CREATE TRIGGER source_author_changes_applied_once BEFORE UPDATE ON source_author_changes
WHEN NOT (OLD.applied = 0 AND NEW.applied = 1
  AND NEW.change_id IS OLD.change_id AND NEW.source_id IS OLD.source_id
  AND NEW.old_author_person_id IS OLD.old_author_person_id AND NEW.new_author_person_id IS OLD.new_author_person_id
  AND NEW.evidence_source_id IS OLD.evidence_source_id AND NEW.locator IS OLD.locator
  AND NEW.changed_at IS OLD.changed_at AND NEW.changed_by IS OLD.changed_by)
BEGIN SELECT RAISE(ABORT, 'source_author_changes never change once applied'); END;
CREATE TRIGGER source_author_changes_never_deleted BEFORE DELETE ON source_author_changes
BEGIN SELECT RAISE(ABORT, 'source_author_changes are never deleted'); END;
