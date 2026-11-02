-- meetings: one row per calendar event, keyed by its calendar thread.
CREATE TABLE meetings (
  id                   TEXT PRIMARY KEY
                       CHECK (id GLOB 'mtg-[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9._-]*' AND length(id) <= 120),
  thread_id            TEXT NOT NULL UNIQUE REFERENCES threads(id),
  title                TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 300),
  starts_at            TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', starts_at) IS starts_at),
  ends_at              TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', ends_at) IS ends_at),
  all_day              INTEGER NOT NULL DEFAULT 0 CHECK (all_day IN (0, 1)),
  status               TEXT NOT NULL CHECK (status IN ('confirmed', 'tentative', 'cancelled')),
  attendee_count       INTEGER NOT NULL DEFAULT 0 CHECK (attendee_count BETWEEN 0 AND 10000),
  project_id           TEXT REFERENCES projects(id),
  transcript_source_id TEXT REFERENCES sources(id),
  source_id            TEXT NOT NULL REFERENCES sources(id),
  locator              TEXT NOT NULL CHECK (length(trim(locator)) BETWEEN 1 AND 200
                                            AND instr(locator, char(10)) = 0 AND instr(locator, char(13)) = 0),
  version              INTEGER NOT NULL CHECK (version >= 1),
  created_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  updated_at           TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by           TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  CHECK (ends_at >= starts_at)
) STRICT;
CREATE INDEX meetings_by_start ON meetings(starts_at);
CREATE INDEX meetings_by_project ON meetings(project_id, starts_at) WHERE project_id IS NOT NULL;
