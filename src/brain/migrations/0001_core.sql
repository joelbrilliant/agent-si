-- 0001_core.sql: agent-brain core schema, version 1.
-- brain.migrate runs this inside one transaction, sets user_version, installs the write
-- guards and verifies the store before COMMIT. No BEGIN, COMMIT or PRAGMA in this file.

-- sources: the evidence. Immutable once written.
CREATE TABLE sources (
  id               TEXT PRIMARY KEY
                   CHECK (id GLOB 'src-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  kind             TEXT NOT NULL
                   CHECK (kind IN ('user_note', 'email', 'slack', 'meeting_transcript', 'calendar',
                                   'document', 'web', 'api', 'observation', 'config')),
  title            TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
  uri              TEXT NOT NULL DEFAULT '' CHECK (length(uri) <= 2000),
  source_date      TEXT NOT NULL CHECK (date(source_date) IS source_date),
  author_person_id TEXT REFERENCES people(id),
  content          TEXT NOT NULL CHECK (length(content) <= 2000000),
  content_sha256   TEXT NOT NULL CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
  external_ref     TEXT NOT NULL DEFAULT '' CHECK (length(external_ref) <= 500),
  captured_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', captured_at) IS captured_at),
  captured_by      TEXT NOT NULL CHECK (length(captured_by) BETWEEN 3 AND 200)
) STRICT;
CREATE INDEX sources_by_author ON sources(author_person_id) WHERE author_person_id IS NOT NULL;
CREATE INDEX sources_by_kind_date ON sources(kind, source_date);
CREATE TRIGGER sources_never_change BEFORE UPDATE ON sources
BEGIN SELECT RAISE(ABORT, 'sources never change: add a new source with a new id'); END;
CREATE TRIGGER sources_never_deleted BEFORE DELETE ON sources
BEGIN SELECT RAISE(ABORT, 'sources are never deleted'); END;

-- people: display names and aliases only. No email addresses anywhere in this table.
CREATE TABLE people (
  id           TEXT PRIMARY KEY
               CHECK (id GLOB 'per-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9-]*' AND length(id) <= 64),
  display_name TEXT NOT NULL CHECK (length(trim(display_name)) BETWEEN 1 AND 120 AND instr(display_name, '@') = 0),
  aliases      TEXT NOT NULL DEFAULT '[]'
               CHECK (json_valid(aliases) AND json_type(aliases) = 'array' AND instr(aliases, '@') = 0),
  org          TEXT NOT NULL DEFAULT '' CHECK (length(org) <= 120 AND instr(org, '@') = 0),
  role         TEXT NOT NULL DEFAULT '' CHECK (length(role) <= 120 AND instr(role, '@') = 0),
  source_id    TEXT NOT NULL REFERENCES sources(id),
  locator      TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                    AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version      INTEGER NOT NULL CHECK (version >= 1),
  created_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by   TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200)
) STRICT;

-- systems: tools and services the instance tracks.
CREATE TABLE systems (
  id              TEXT PRIMARY KEY
                  CHECK (id GLOB 'sys-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9-]*' AND length(id) <= 64),
  name            TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
  kind            TEXT NOT NULL CHECK (kind IN ('saas', 'infra', 'internal', 'device', 'other')),
  vendor          TEXT NOT NULL DEFAULT '' CHECK (length(vendor) <= 120),
  owner_person_id TEXT REFERENCES people(id),
  source_id       TEXT NOT NULL REFERENCES sources(id),
  locator         TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                       AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version         INTEGER NOT NULL CHECK (version >= 1),
  created_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at      TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by      TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200)
) STRICT;

-- projects: workstreams (from brain.toml) and projects (from agents) in one table.
CREATE TABLE projects (
  id          TEXT PRIMARY KEY
              CHECK (id GLOB '[a-z][a-z0-9-]*' AND id NOT GLOB '*[^a-z0-9-]*' AND length(id) BETWEEN 2 AND 40),
  kind        TEXT NOT NULL CHECK (kind IN ('workstream', 'project')),
  title       TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 120),
  description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 2000),
  goal        TEXT NOT NULL DEFAULT '' CHECK (length(goal) <= 500),
  goal_date   TEXT CHECK (goal_date IS NULL OR date(goal_date) IS goal_date),
  status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'parked', 'done')),
  sort_order  INTEGER NOT NULL DEFAULT 100,
  source_id   TEXT NOT NULL REFERENCES sources(id),
  locator     TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                   AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version     INTEGER NOT NULL CHECK (version >= 1),
  created_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by  TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200)
) STRICT;

-- fact_keys: the registry. Every fact uses an exact registered key.
CREATE TABLE fact_keys (
  key              TEXT PRIMARY KEY
                   CHECK (length(key) BETWEEN 3 AND 120 AND key NOT GLOB '*[^a-z0-9_.]*'
                          AND key GLOB '[a-z]*.*' AND key NOT GLOB '*..*' AND key NOT GLOB '*.'),
  value_type       TEXT NOT NULL CHECK (value_type IN ('text', 'int', 'number', 'date', 'bool', 'url', 'enum')),
  unit             TEXT NOT NULL DEFAULT '' CHECK (length(unit) <= 40),
  allowed_values   TEXT CHECK (allowed_values IS NULL OR (json_valid(allowed_values)
                               AND json_type(allowed_values) = 'array'
                               AND json_array_length(allowed_values) BETWEEN 1 AND 50)),
  stale_after_days INTEGER CHECK (stale_after_days IS NULL OR stale_after_days BETWEEN 1 AND 3650),
  description      TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 500),
  source_id        TEXT NOT NULL REFERENCES sources(id),
  locator          TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                        AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version          INTEGER NOT NULL CHECK (version >= 1),
  created_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by       TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  CHECK ((value_type = 'enum') = (allowed_values IS NOT NULL))
) STRICT;

-- facts: one current typed value per key. Exactly one value column is set and it matches value_type.
CREATE TABLE facts (
  key              TEXT PRIMARY KEY REFERENCES fact_keys(key),
  value_type       TEXT NOT NULL CHECK (value_type IN ('text', 'int', 'number', 'date', 'bool', 'url', 'enum')),
  value_text       TEXT CHECK (value_text IS NULL OR length(value_text) <= 2000),
  value_int        INTEGER,
  value_real       REAL,
  value_date       TEXT,
  status           TEXT NOT NULL DEFAULT 'current' CHECK (status IN ('current', 'disputed', 'retired')),
  basis            TEXT NOT NULL CHECK (basis IN ('observed', 'reported', 'inferred')),
  as_of            TEXT NOT NULL CHECK (date(as_of) IS as_of),
  last_verified_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_verified_at) IS last_verified_at),
  source_id        TEXT NOT NULL REFERENCES sources(id),
  locator          TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                        AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version          INTEGER NOT NULL CHECK (version >= 1),
  created_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by       TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
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
         END)
) STRICT;
CREATE INDEX facts_by_status ON facts(status);

-- fact_history: one row per fact version, written only by the triggers below. Append-only.
CREATE TABLE fact_history (
  id               INTEGER PRIMARY KEY,
  key              TEXT NOT NULL REFERENCES facts(key),
  version          INTEGER NOT NULL,
  change           TEXT NOT NULL CHECK (change IN ('created', 'updated')),
  value_type       TEXT NOT NULL,
  value_text       TEXT,
  value_int        INTEGER,
  value_real       REAL,
  value_date       TEXT,
  status           TEXT NOT NULL,
  basis            TEXT NOT NULL,
  as_of            TEXT NOT NULL,
  last_verified_at TEXT NOT NULL,
  source_id        TEXT NOT NULL REFERENCES sources(id),
  locator          TEXT NOT NULL,
  recorded_at      TEXT NOT NULL,
  recorded_by      TEXT NOT NULL,
  UNIQUE (key, version)
) STRICT;
CREATE TRIGGER facts_history_on_insert AFTER INSERT ON facts
BEGIN
  INSERT INTO fact_history (key, version, change, value_type, value_text, value_int, value_real, value_date,
                            status, basis, as_of, last_verified_at, source_id, locator, recorded_at, recorded_by)
  VALUES (NEW.key, NEW.version, 'created', NEW.value_type, NEW.value_text, NEW.value_int, NEW.value_real,
          NEW.value_date, NEW.status, NEW.basis, NEW.as_of, NEW.last_verified_at, NEW.source_id, NEW.locator,
          NEW.updated_at, NEW.updated_by);
END;
CREATE TRIGGER facts_history_on_update AFTER UPDATE ON facts
BEGIN
  INSERT INTO fact_history (key, version, change, value_type, value_text, value_int, value_real, value_date,
                            status, basis, as_of, last_verified_at, source_id, locator, recorded_at, recorded_by)
  VALUES (NEW.key, NEW.version, 'updated', NEW.value_type, NEW.value_text, NEW.value_int, NEW.value_real,
          NEW.value_date, NEW.status, NEW.basis, NEW.as_of, NEW.last_verified_at, NEW.source_id, NEW.locator,
          NEW.updated_at, NEW.updated_by);
END;
CREATE TRIGGER fact_history_no_update BEFORE UPDATE ON fact_history
BEGIN SELECT RAISE(ABORT, 'fact_history is append-only'); END;
CREATE TRIGGER fact_history_no_delete BEFORE DELETE ON fact_history
BEGIN SELECT RAISE(ABORT, 'fact_history is append-only'); END;

-- records: sourced prose (notes, references, processes, learnings, gotchas, questions).
CREATE TABLE records (
  id         TEXT PRIMARY KEY
             CHECK (id GLOB 'rec-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  kind       TEXT NOT NULL CHECK (kind IN ('note', 'reference', 'process', 'learning', 'gotcha', 'question')),
  title      TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
  body       TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 100000),
  status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'superseded', 'archived')),
  project_id TEXT REFERENCES projects(id),
  source_id  TEXT NOT NULL REFERENCES sources(id),
  locator    TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                  AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version    INTEGER NOT NULL CHECK (version >= 1),
  created_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200)
) STRICT;
CREATE INDEX records_by_kind_status ON records(kind, status);
CREATE INDEX records_by_project ON records(project_id) WHERE project_id IS NOT NULL;

-- record_history: one row per record version, written only by the triggers below. Append-only.
CREATE TABLE record_history (
  id          INTEGER PRIMARY KEY,
  record_id   TEXT NOT NULL REFERENCES records(id),
  version     INTEGER NOT NULL,
  change      TEXT NOT NULL CHECK (change IN ('created', 'updated')),
  kind        TEXT NOT NULL,
  title       TEXT NOT NULL,
  body        TEXT NOT NULL,
  status      TEXT NOT NULL,
  project_id  TEXT,
  source_id   TEXT NOT NULL REFERENCES sources(id),
  locator     TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  recorded_by TEXT NOT NULL,
  UNIQUE (record_id, version)
) STRICT;
CREATE TRIGGER records_history_on_insert AFTER INSERT ON records
BEGIN
  INSERT INTO record_history (record_id, version, change, kind, title, body, status, project_id,
                              source_id, locator, recorded_at, recorded_by)
  VALUES (NEW.id, NEW.version, 'created', NEW.kind, NEW.title, NEW.body, NEW.status, NEW.project_id,
          NEW.source_id, NEW.locator, NEW.updated_at, NEW.updated_by);
END;
CREATE TRIGGER records_history_on_update AFTER UPDATE ON records
BEGIN
  INSERT INTO record_history (record_id, version, change, kind, title, body, status, project_id,
                              source_id, locator, recorded_at, recorded_by)
  VALUES (NEW.id, NEW.version, 'updated', NEW.kind, NEW.title, NEW.body, NEW.status, NEW.project_id,
          NEW.source_id, NEW.locator, NEW.updated_at, NEW.updated_by);
END;
CREATE TRIGGER record_history_no_update BEFORE UPDATE ON record_history
BEGIN SELECT RAISE(ABORT, 'record_history is append-only'); END;
CREATE TRIGGER record_history_no_delete BEFORE DELETE ON record_history
BEGIN SELECT RAISE(ABORT, 'record_history is append-only'); END;

-- decisions: who decided what, when, with the quote that shows it.
CREATE TABLE decisions (
  id                   TEXT PRIMARY KEY
                       CHECK (id GLOB 'dec-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  title                TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 90),
  detail               TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 2000),
  quote                TEXT NOT NULL CHECK (length(trim(quote)) BETWEEN 1 AND 2000),
  decided_on           TEXT NOT NULL CHECK (date(decided_on) IS decided_on),
  decided_by_person_id TEXT NOT NULL REFERENCES people(id),
  status               TEXT NOT NULL CHECK (status IN ('proposed', 'in_effect', 'reversed')),
  governs_from         TEXT CHECK (governs_from IS NULL OR date(governs_from) IS governs_from),
  project_id           TEXT NOT NULL REFERENCES projects(id),
  supersedes_id        TEXT REFERENCES decisions(id) CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  source_id            TEXT NOT NULL REFERENCES sources(id),
  locator              TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                            AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version              INTEGER NOT NULL CHECK (version >= 1),
  created_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by           TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200)
) STRICT;
CREATE INDEX decisions_by_project ON decisions(project_id, decided_on);

-- actions: things to do or chase. Thread links live in thread_links, never in person columns.
CREATE TABLE actions (
  id                   TEXT PRIMARY KEY
                       CHECK (id GLOB 'act-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  title                TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 90),
  detail               TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 2000),
  next_step            TEXT NOT NULL DEFAULT '' CHECK (length(next_step) <= 500),
  status               TEXT NOT NULL CHECK (status IN ('proposed', 'open', 'prepared', 'scheduled', 'sent',
                                                       'waiting', 'blocked', 'done', 'parked', 'dropped')),
  status_basis         TEXT NOT NULL CHECK (status_basis IN ('observed', 'reported', 'inferred')),
  project_id           TEXT NOT NULL REFERENCES projects(id),
  owner_person_id      TEXT REFERENCES people(id),
  waiting_on_person_id TEXT REFERENCES people(id),
  due_date             TEXT CHECK (due_date IS NULL OR date(due_date) IS due_date),
  due_kind             TEXT CHECK (due_kind IS NULL OR due_kind IN ('deadline', 'meeting')),
  asked_on             TEXT CHECK (asked_on IS NULL OR date(asked_on) IS asked_on),
  blocked_by           TEXT REFERENCES actions(id) CHECK (blocked_by IS NULL OR blocked_by <> id),
  record_id            TEXT REFERENCES records(id),
  last_verified_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_verified_at) IS last_verified_at),
  closed_at            TEXT CHECK (closed_at IS NULL OR strftime('%Y-%m-%dT%H:%M:%SZ', closed_at) IS closed_at),
  close_reason         TEXT NOT NULL DEFAULT '' CHECK (length(close_reason) <= 500),
  source_id            TEXT NOT NULL REFERENCES sources(id),
  locator              TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                            AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version              INTEGER NOT NULL CHECK (version >= 1),
  created_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by           TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  CHECK ((due_date IS NULL) = (due_kind IS NULL)),
  CHECK ((status IN ('done', 'dropped')) = (closed_at IS NOT NULL))
) STRICT;
CREATE INDEX actions_by_status ON actions(status);
CREATE INDEX actions_by_project_status ON actions(project_id, status);
CREATE INDEX actions_by_due ON actions(due_date) WHERE due_date IS NOT NULL;

-- checklist_items: the steps of a project or workstream.
CREATE TABLE checklist_items (
  id                   TEXT PRIMARY KEY
                       CHECK (id GLOB 'chk-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  project_id           TEXT NOT NULL REFERENCES projects(id),
  section              TEXT NOT NULL DEFAULT '' CHECK (length(section) <= 80),
  title                TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 140),
  detail               TEXT NOT NULL DEFAULT '' CHECK (length(detail) <= 2000),
  state                TEXT NOT NULL CHECK (state IN ('not_started', 'in_progress', 'waiting', 'blocked',
                                                     'done', 'not_applicable')),
  state_basis          TEXT NOT NULL CHECK (state_basis IN ('observed', 'reported', 'inferred')),
  owner_person_id      TEXT REFERENCES people(id),
  waiting_on_person_id TEXT REFERENCES people(id),
  land_by              TEXT CHECK (land_by IS NULL OR date(land_by) IS land_by),
  action_id            TEXT REFERENCES actions(id),
  external_ref         TEXT NOT NULL DEFAULT '' CHECK (length(external_ref) <= 200),
  sort_order           INTEGER NOT NULL DEFAULT 100,
  last_verified_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', last_verified_at) IS last_verified_at),
  source_id            TEXT NOT NULL REFERENCES sources(id),
  locator              TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                            AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version              INTEGER NOT NULL CHECK (version >= 1),
  created_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by           TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200)
) STRICT;
CREATE INDEX checklist_items_by_project_state ON checklist_items(project_id, state, sort_order);

-- threads: one row per email thread, Slack thread or calendar event, identified by channel,
-- account and external id. Collectors record only message times and direction.
CREATE TABLE threads (
  id               TEXT PRIMARY KEY
                   CHECK (id GLOB 'thr-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  channel          TEXT NOT NULL CHECK (channel IN ('email', 'slack', 'calendar')),
  account          TEXT NOT NULL CHECK (account GLOB '[a-z][a-z0-9-]*' AND account NOT GLOB '*[^a-z0-9-]*'
                                        AND length(account) BETWEEN 2 AND 40),
  external_id      TEXT NOT NULL CHECK (length(external_id) BETWEEN 1 AND 500 AND instr(external_id, ' ') = 0
                                        AND (channel <> 'slack' OR external_id GLOB '[A-Z0-9]*:[0-9]*.[0-9]*')),
  subject          TEXT NOT NULL DEFAULT '' CHECK (length(subject) <= 300),
  last_inbound_at  TEXT CHECK (last_inbound_at IS NULL
                               OR strftime('%Y-%m-%dT%H:%M:%SZ', last_inbound_at) IS last_inbound_at),
  last_outbound_at TEXT CHECK (last_outbound_at IS NULL
                               OR strftime('%Y-%m-%dT%H:%M:%SZ', last_outbound_at) IS last_outbound_at),
  outbound_seen    INTEGER NOT NULL DEFAULT 0 CHECK (outbound_seen IN (0, 1)),
  source_id        TEXT NOT NULL REFERENCES sources(id),
  locator          TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                        AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version          INTEGER NOT NULL CHECK (version >= 1),
  created_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at       TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by       TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  UNIQUE (channel, account, external_id)
) STRICT;

-- thread_links: an action or a checklist item depends on a thread.
CREATE TABLE thread_links (
  id                TEXT PRIMARY KEY
                    CHECK (id GLOB 'lnk-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  action_id         TEXT REFERENCES actions(id),
  checklist_item_id TEXT REFERENCES checklist_items(id),
  thread_id         TEXT NOT NULL REFERENCES threads(id),
  role              TEXT NOT NULL CHECK (role IN ('awaiting_reply', 'owes_reply', 'event')),
  since             TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', since) IS since),
  seen_through      TEXT CHECK (seen_through IS NULL OR strftime('%Y-%m-%dT%H:%M:%SZ', seen_through) IS seen_through),
  status            TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
  source_id         TEXT NOT NULL REFERENCES sources(id),
  locator           TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                         AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version           INTEGER NOT NULL CHECK (version >= 1),
  created_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at        TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by        TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  CHECK ((action_id IS NULL) <> (checklist_item_id IS NULL)),
  UNIQUE (action_id, thread_id),
  UNIQUE (checklist_item_id, thread_id)
) STRICT;
CREATE INDEX thread_links_by_thread ON thread_links(thread_id);

-- thread_link_state: replied, waiting and new message, worked out from thread ids and times only.
CREATE VIEW thread_link_state AS
SELECT
  l.id                AS link_id,
  l.action_id         AS action_id,
  l.checklist_item_id AS checklist_item_id,
  l.thread_id         AS thread_id,
  t.channel           AS channel,
  t.account           AS account,
  t.external_id       AS external_id,
  l.role              AS role,
  CASE
    WHEN l.role = 'awaiting_reply' THEN
      CASE WHEN t.last_inbound_at > l.since THEN 'replied' ELSE 'waiting' END
    WHEN l.role = 'owes_reply' THEN
      CASE
        WHEN t.outbound_seen = 0 THEN 'unknown'
        WHEN t.last_outbound_at > max(l.since, coalesce(t.last_inbound_at, l.since)) THEN 'replied'
        ELSE 'waiting'
      END
  END AS reply_state,
  CASE WHEN t.last_inbound_at > max(l.since, coalesce(l.seen_through, l.since)) THEN 1 ELSE 0 END AS new_message
FROM thread_links AS l
JOIN threads AS t ON t.id = l.thread_id
WHERE l.status = 'active';

-- signals: things that need attention. Reply and new-message signals must name a linked thread.
CREATE TABLE signals (
  id            TEXT PRIMARY KEY
                CHECK (id GLOB 'sig-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  kind          TEXT NOT NULL CHECK (kind IN ('reply', 'new_message', 'overdue', 'stale', 'drift', 'other')),
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
  CHECK ((status = 'open') = (resolved_at IS NULL))
) STRICT;
CREATE INDEX signals_by_status_kind ON signals(status, kind);
CREATE INDEX signals_by_subject ON signals(subject_table, subject_id);

-- collector_runs: one row per collector run. Append-only.
CREATE TABLE collector_runs (
  id           TEXT PRIMARY KEY
               CHECK (id GLOB 'run-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  collector    TEXT NOT NULL CHECK (collector GLOB '[a-z][a-z0-9-]*' AND collector NOT GLOB '*[^a-z0-9-]*'
                                    AND length(collector) BETWEEN 2 AND 40),
  started_at   TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', started_at) IS started_at),
  finished_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', finished_at) IS finished_at),
  status       TEXT NOT NULL CHECK (status IN ('ok', 'partial', 'error')),
  items        INTEGER NOT NULL DEFAULT 0 CHECK (items >= 0),
  input_sha256 TEXT NOT NULL DEFAULT '' CHECK (input_sha256 = ''
                                               OR (length(input_sha256) = 64 AND input_sha256 NOT GLOB '*[^0-9a-f]*')),
  error        TEXT NOT NULL DEFAULT '' CHECK (length(error) <= 2000),
  recorded_at  TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', recorded_at) IS recorded_at),
  recorded_by  TEXT NOT NULL CHECK (length(recorded_by) BETWEEN 3 AND 200),
  CHECK (finished_at >= started_at),
  CHECK ((status = 'ok') = (error = ''))
) STRICT;
CREATE INDEX collector_runs_by_collector ON collector_runs(collector, started_at);
CREATE TRIGGER collector_runs_no_update BEFORE UPDATE ON collector_runs
BEGIN SELECT RAISE(ABORT, 'collector_runs is append-only'); END;
CREATE TRIGGER collector_runs_no_delete BEFORE DELETE ON collector_runs
BEGIN SELECT RAISE(ABORT, 'collector_runs is append-only'); END;

-- write_log: one row per decided packet. A decided row is final. Only a held row may move once.
CREATE TABLE write_log (
  change_id      TEXT PRIMARY KEY
                 CHECK (length(change_id) BETWEEN 4 AND 152 AND change_id GLOB '[a-z]*-[a-z0-9]*'
                        AND change_id NOT GLOB '*[^a-z0-9._-]*'),
  agent          TEXT NOT NULL CHECK (agent GLOB '[a-z]*' AND agent NOT GLOB '*[^a-z0-9]*'
                                      AND length(agent) BETWEEN 2 AND 40),
  intent         TEXT NOT NULL CHECK (length(intent) <= 500),
  status         TEXT NOT NULL CHECK (status IN ('applied', 'rejected', 'conflict', 'held')),
  decided_at     TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', decided_at) IS decided_at),
  applied_seq    INTEGER UNIQUE CHECK (applied_seq IS NULL OR applied_seq >= 1),
  reviewed_by    TEXT NOT NULL DEFAULT '',
  reviewed_at    TEXT CHECK (reviewed_at IS NULL OR strftime('%Y-%m-%dT%H:%M:%SZ', reviewed_at) IS reviewed_at),
  payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64 AND payload_sha256 NOT GLOB '*[^0-9a-f]*'),
  error          TEXT NOT NULL DEFAULT '',
  packet_json    TEXT NOT NULL CHECK (json_valid(packet_json)),
  targets_json   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(targets_json) AND json_type(targets_json) = 'array'),
  CHECK ((status = 'applied') = (applied_seq IS NOT NULL)),
  CHECK ((reviewed_by = '') = (reviewed_at IS NULL)),
  CHECK ((status IN ('rejected', 'conflict')) = (error <> ''))
) STRICT;
CREATE INDEX write_log_by_status ON write_log(status, decided_at);
CREATE INDEX write_log_by_agent ON write_log(agent, decided_at);
CREATE TRIGGER write_log_decided_is_final BEFORE UPDATE ON write_log
WHEN OLD.status <> 'held'
BEGIN SELECT RAISE(ABORT, 'write_log: a decided change is final'); END;
CREATE TRIGGER write_log_identity_is_fixed BEFORE UPDATE ON write_log
WHEN NEW.change_id IS NOT OLD.change_id OR NEW.agent IS NOT OLD.agent OR NEW.intent IS NOT OLD.intent
  OR NEW.payload_sha256 IS NOT OLD.payload_sha256 OR NEW.packet_json IS NOT OLD.packet_json
BEGIN SELECT RAISE(ABORT, 'write_log: identity columns never change'); END;
CREATE TRIGGER write_log_no_delete BEFORE DELETE ON write_log
BEGIN SELECT RAISE(ABORT, 'write_log is append-only'); END;
