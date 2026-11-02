-- 0006_redaction.sql: redaction, version 6. The gate, the packet list, the seven gated update triggers and two indexes.
-- brain.migrate supplies the transaction, the user_version bump and the write guards, so this file
-- holds no transaction or pragma statement.

-- redaction_gate: the applier fills it with one row per source it is redacting, inside the redact
-- transaction only, and empties it before COMMIT. A gated trigger below lets an update through only when
-- a row here names the source the updated row cites AND the new values are the redaction's exact form.
CREATE TABLE redaction_gate (
  source_id   TEXT PRIMARY KEY REFERENCES sources(id),
  redacted_on TEXT NOT NULL CHECK (date(redacted_on) IS redacted_on),
  tombstone   TEXT NOT NULL CHECK (length(tombstone) <= 2000
                                   AND substr(tombstone, 1, 22) = 'redacted on ' || redacted_on
                                   AND substr(tombstone, 23, 4) = ' by ')
) STRICT;

-- redaction_packets: the change ids of applied packets that wrote a row the redaction scrubbed though they
-- cite another source. Filled and emptied with the gate. The two write_log triggers accept a packet change
-- for these as well as for a packet that names a gate source.
CREATE TABLE redaction_packets (
  change_id TEXT PRIMARY KEY REFERENCES write_log(change_id)
) STRICT;

-- The carry-forward lookup, and a short list of tombstones for brain check.
CREATE INDEX decisions_by_source ON decisions(source_id);
CREATE INDEX sources_redacted ON sources(id) WHERE title = 'redacted';

-- sources: a source changes only to the tombstone.
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
BEGIN SELECT RAISE(ABORT, 'sources never change: add a new source with a new id'); END;

-- claims: quote, value_raw and a text, enum or url value change only to their redaction tokens.
DROP TRIGGER claims_content_fixed;
CREATE TRIGGER claims_content_fixed BEFORE UPDATE ON claims
WHEN NEW.id IS NOT OLD.id OR NEW.key IS NOT OLD.key OR NEW.scope IS NOT OLD.scope
  OR NEW.value_type IS NOT OLD.value_type OR NEW.value_int IS NOT OLD.value_int
  OR NEW.value_real IS NOT OLD.value_real OR NEW.value_date IS NOT OLD.value_date
  OR NEW.basis IS NOT OLD.basis OR NEW.as_of IS NOT OLD.as_of
  OR NEW.source_id IS NOT OLD.source_id OR NEW.locator IS NOT OLD.locator
  OR NEW.checked IS NOT OLD.checked OR NEW.supersedes IS NOT OLD.supersedes
  OR NEW.proposed_by IS NOT OLD.proposed_by
  OR NEW.created_at IS NOT OLD.created_at OR NEW.created_by IS NOT OLD.created_by
  OR ((NEW.quote IS NOT OLD.quote OR NEW.value_raw IS NOT OLD.value_raw OR NEW.value_text IS NOT OLD.value_text)
      AND NOT EXISTS (
        SELECT 1 FROM redaction_gate AS g
        WHERE g.source_id = OLD.source_id
          AND NEW.quote IN (OLD.quote, '[redacted]')
          AND NEW.value_raw IN (OLD.value_raw, '[redacted]')
          AND (NEW.value_text IS OLD.value_text
               OR NEW.value_text IS CASE WHEN OLD.value_type IN ('text', 'enum') THEN '[redacted]'
                                         WHEN OLD.value_type = 'url' THEN 'https://redacted.invalid/' END)))
BEGIN SELECT RAISE(ABORT, 'claims never change: add a new claim'); END;

-- fact_history: a row that cites a redacted source changes its text, enum or url value and its quote only.
DROP TRIGGER fact_history_no_update;
CREATE TRIGGER fact_history_no_update BEFORE UPDATE ON fact_history
WHEN NOT EXISTS (
  SELECT 1 FROM redaction_gate AS g
  WHERE g.source_id = OLD.source_id
    AND NEW.id IS OLD.id AND NEW.key IS OLD.key AND NEW.version IS OLD.version AND NEW.change IS OLD.change
    AND NEW.value_type IS OLD.value_type AND NEW.value_int IS OLD.value_int AND NEW.value_real IS OLD.value_real
    AND NEW.value_date IS OLD.value_date AND NEW.status IS OLD.status AND NEW.basis IS OLD.basis
    AND NEW.as_of IS OLD.as_of AND NEW.last_verified_at IS OLD.last_verified_at
    AND NEW.source_id IS OLD.source_id AND NEW.locator IS OLD.locator
    AND NEW.recorded_at IS OLD.recorded_at AND NEW.recorded_by IS OLD.recorded_by
    AND (NEW.value_text IS OLD.value_text
         OR NEW.value_text IS CASE WHEN OLD.value_type IN ('text', 'enum') THEN '[redacted]'
                                   WHEN OLD.value_type = 'url' THEN 'https://redacted.invalid/' END)
    AND (NEW.quote IS OLD.quote OR NEW.quote = '[redacted]'))
BEGIN SELECT RAISE(ABORT, 'fact_history is append-only'); END;

-- record_history: a row that cites a redacted source, or belongs to a record that has such a version, changes
-- its title and body only. A changed one holds the token and is no longer than the old text plus 10
-- characters for each token.
DROP TRIGGER record_history_no_update;
CREATE TRIGGER record_history_no_update BEFORE UPDATE ON record_history
WHEN NOT EXISTS (
  SELECT 1 FROM redaction_gate AS g
  WHERE (g.source_id = OLD.source_id
         OR EXISTS (SELECT 1 FROM record_history AS h WHERE h.record_id = OLD.record_id AND h.source_id = g.source_id))
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
                 + 10 * ((length(NEW.body) - length(replace(NEW.body, '[redacted]', ''))) / 10))))
BEGIN SELECT RAISE(ABORT, 'record_history is append-only'); END;

-- collector_runs: an error that names a redacted source becomes the redaction's error and nothing else changes.
DROP TRIGGER collector_runs_no_update;
CREATE TRIGGER collector_runs_no_update BEFORE UPDATE ON collector_runs
WHEN NOT EXISTS (
  SELECT 1 FROM redaction_gate AS g
  WHERE instr(OLD.error, g.source_id) > 0 AND NEW.error = 'redacted on ' || g.redacted_on
    AND NEW.id IS OLD.id AND NEW.collector IS OLD.collector AND NEW.started_at IS OLD.started_at
    AND NEW.finished_at IS OLD.finished_at AND NEW.status IS OLD.status AND NEW.items IS OLD.items
    AND NEW.input_sha256 IS OLD.input_sha256 AND NEW.recorded_at IS OLD.recorded_at
    AND NEW.recorded_by IS OLD.recorded_by)
BEGIN SELECT RAISE(ABORT, 'collector_runs is append-only'); END;

-- write_log: a decided row that names a redacted source may change its packet, its error and its hold
-- reason, and a held row may also move to rejected (the existing rule). The packet keeps its identity,
-- its payload_sha256 and its op count, and gains redacted_ops.
DROP TRIGGER write_log_decided_is_final;
CREATE TRIGGER write_log_decided_is_final BEFORE UPDATE ON write_log
WHEN OLD.status <> 'held' AND NOT EXISTS (
  SELECT 1 FROM redaction_gate AS g
  WHERE (instr(OLD.packet_json, g.source_id) > 0 OR instr(OLD.error, g.source_id) > 0
         OR instr(OLD.hold_reason, g.source_id) > 0
         OR EXISTS (SELECT 1 FROM redaction_packets AS p WHERE p.change_id = OLD.change_id))
    AND NEW.change_id IS OLD.change_id AND NEW.agent IS OLD.agent AND NEW.intent IS OLD.intent
    AND NEW.status IS OLD.status AND NEW.decided_at IS OLD.decided_at AND NEW.applied_seq IS OLD.applied_seq
    AND NEW.reviewed_by IS OLD.reviewed_by AND NEW.reviewed_at IS OLD.reviewed_at
    AND NEW.payload_sha256 IS OLD.payload_sha256 AND NEW.targets_json IS OLD.targets_json
    AND NEW.approved_via IS OLD.approved_via
    AND (NEW.error IS OLD.error OR NEW.error = 'redacted on ' || g.redacted_on)
    AND (NEW.hold_reason IS OLD.hold_reason OR NEW.hold_reason = '')
    AND (NEW.packet_json IS OLD.packet_json
         OR (json_remove(NEW.packet_json, '$.ops', '$.redacted_ops') IS json_remove(OLD.packet_json, '$.ops', '$.redacted_ops')
             AND json_array_length(NEW.packet_json, '$.ops') = json_array_length(OLD.packet_json, '$.ops')
             AND json_type(NEW.packet_json, '$.redacted_ops') = 'array'
             AND NOT EXISTS (
               SELECT 1 FROM json_each(NEW.packet_json, '$.ops') AS n JOIN json_each(OLD.packet_json, '$.ops') AS o ON o.key = n.key
               WHERE n.value IS NOT o.value
                 AND (n.key NOT IN (SELECT value FROM json_each(NEW.packet_json, '$.redacted_ops'))
                      OR json_extract(n.value, '$.op') IS NOT json_extract(o.value, '$.op')
                      OR json_extract(n.value, '$.row.id') IS NOT json_extract(o.value, '$.row.id')
                      OR json_extract(n.value, '$.source') IS NOT json_extract(o.value, '$.source')
                      OR json_extract(n.value, '$.candidate.source_id') IS NOT json_extract(o.value, '$.candidate.source_id')
                      OR NOT (EXISTS (SELECT 1 FROM redaction_gate AS g2 WHERE instr(o.value, g2.source_id) > 0)
                              OR EXISTS (SELECT 1 FROM redaction_packets AS p2 WHERE p2.change_id = OLD.change_id)))))))
BEGIN SELECT RAISE(ABORT, 'write_log: a decided change is final'); END;

DROP TRIGGER write_log_identity_is_fixed;
CREATE TRIGGER write_log_identity_is_fixed BEFORE UPDATE ON write_log
WHEN NEW.change_id IS NOT OLD.change_id OR NEW.agent IS NOT OLD.agent OR NEW.intent IS NOT OLD.intent
  OR NEW.payload_sha256 IS NOT OLD.payload_sha256
  OR (NEW.packet_json IS NOT OLD.packet_json AND NOT EXISTS (
        SELECT 1 FROM redaction_gate AS g
        WHERE (instr(OLD.packet_json, g.source_id) > 0
               OR EXISTS (SELECT 1 FROM redaction_packets AS p WHERE p.change_id = OLD.change_id))
          AND json_remove(NEW.packet_json, '$.ops', '$.redacted_ops') IS json_remove(OLD.packet_json, '$.ops', '$.redacted_ops')
          AND json_array_length(NEW.packet_json, '$.ops') = json_array_length(OLD.packet_json, '$.ops')
          AND json_type(NEW.packet_json, '$.redacted_ops') = 'array'
          AND NOT EXISTS (
            SELECT 1 FROM json_each(NEW.packet_json, '$.ops') AS n JOIN json_each(OLD.packet_json, '$.ops') AS o ON o.key = n.key
            WHERE n.value IS NOT o.value
              AND (n.key NOT IN (SELECT value FROM json_each(NEW.packet_json, '$.redacted_ops'))
                   OR json_extract(n.value, '$.op') IS NOT json_extract(o.value, '$.op')
                   OR json_extract(n.value, '$.row.id') IS NOT json_extract(o.value, '$.row.id')
                   OR json_extract(n.value, '$.source') IS NOT json_extract(o.value, '$.source')
                   OR json_extract(n.value, '$.candidate.source_id') IS NOT json_extract(o.value, '$.candidate.source_id')
                   OR NOT (EXISTS (SELECT 1 FROM redaction_gate AS g2 WHERE instr(o.value, g2.source_id) > 0)
                           OR EXISTS (SELECT 1 FROM redaction_packets AS p2 WHERE p2.change_id = OLD.change_id))))))
BEGIN SELECT RAISE(ABORT, 'write_log: identity columns never change'); END;
