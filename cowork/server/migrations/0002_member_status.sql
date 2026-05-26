-- Per-member presence/status for the right-hand sidebar.
-- 'online' is the default; clients can change it via the update_status WS frame
-- to one of the fixed presets the protocol enforces.

ALTER TABLE project_members ADD COLUMN status TEXT NOT NULL DEFAULT 'online';
