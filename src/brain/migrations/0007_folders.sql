-- 0007_folders.sql: folders, version 7. A project can sit inside a
-- workstream or another project, like a folder inside a folder. The applier refuses a missing parent, a loop
-- and a workstream with a parent; reads can take a folder and everything under it.
-- brain.migrate supplies the transaction, the user_version bump and the write guards, so this file
-- holds no transaction or pragma statement.

ALTER TABLE projects ADD COLUMN parent_id TEXT REFERENCES projects(id);

CREATE INDEX projects_by_parent ON projects(parent_id);
