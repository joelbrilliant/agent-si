-- people lists and unchecked text. The migrator supplies the transaction, the
-- user_version bump and the write guards, so this file holds none of BEGIN, COMMIT or PRAGMA.

-- action_people and checklist_item_people: ordered lists of the people who own an item or wait
-- on someone. The scalar columns on actions and checklist_items stay and equal position 0.
CREATE TABLE action_people (
  action_id TEXT NOT NULL REFERENCES actions(id),
  role      TEXT NOT NULL CHECK (role IN ('owner', 'waiting_on')),
  position  INTEGER NOT NULL CHECK (position BETWEEN 0 AND 19),
  person_id TEXT NOT NULL REFERENCES people(id),
  PRIMARY KEY (action_id, role, position),
  UNIQUE (action_id, role, person_id)
) STRICT;
CREATE INDEX action_people_by_person ON action_people(person_id, role);

CREATE TABLE checklist_item_people (
  item_id   TEXT NOT NULL REFERENCES checklist_items(id),
  role      TEXT NOT NULL CHECK (role IN ('owner', 'waiting_on')),
  position  INTEGER NOT NULL CHECK (position BETWEEN 0 AND 19),
  person_id TEXT NOT NULL REFERENCES people(id),
  PRIMARY KEY (item_id, role, position),
  UNIQUE (item_id, role, person_id)
) STRICT;
CREATE INDEX checklist_item_people_by_person ON checklist_item_people(person_id, role);

INSERT INTO action_people (action_id, role, position, person_id)
  SELECT id, 'owner', 0, owner_person_id FROM actions WHERE owner_person_id IS NOT NULL;
INSERT INTO action_people (action_id, role, position, person_id)
  SELECT id, 'waiting_on', 0, waiting_on_person_id FROM actions WHERE waiting_on_person_id IS NOT NULL;
INSERT INTO checklist_item_people (item_id, role, position, person_id)
  SELECT id, 'owner', 0, owner_person_id FROM checklist_items WHERE owner_person_id IS NOT NULL;
INSERT INTO checklist_item_people (item_id, role, position, person_id)
  SELECT id, 'waiting_on', 0, waiting_on_person_id FROM checklist_items WHERE waiting_on_person_id IS NOT NULL;

-- unchecked_text: one marker per row that a legacy import agent inserted past the evidence gate.
-- It lists the sentences the gate would have refused. The marker only shrinks.
CREATE TABLE unchecked_text (
  entity     TEXT NOT NULL CHECK (entity IN ('project', 'record', 'action', 'checklist_item')),
  ref_id     TEXT NOT NULL CHECK (length(ref_id) BETWEEN 1 AND 120),
  sentences  TEXT NOT NULL CHECK (json_valid(sentences) AND json_type(sentences) = 'array'
                                  AND json_array_length(sentences) BETWEEN 1 AND 500),
  source_id  TEXT NOT NULL REFERENCES sources(id),
  created_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', created_at) IS created_at),
  created_by TEXT NOT NULL CHECK (length(created_by) BETWEEN 3 AND 200),
  updated_at TEXT NOT NULL CHECK (strftime('%Y-%m-%dT%H:%M:%SZ', updated_at) IS updated_at),
  updated_by TEXT NOT NULL CHECK (length(updated_by) BETWEEN 3 AND 200),
  PRIMARY KEY (entity, ref_id)
) STRICT;
