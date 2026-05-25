-- Phase 3-4: agents as first-class project members.
-- An agent is a project_member row with is_agent=1, plus an `agents` row
-- holding the LLM config. Channel-scoping lets an agent live in a specific
-- channel (NULL = project-wide, available in every channel).

ALTER TABLE project_members ADD COLUMN is_agent INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS agents (
    member_id          TEXT PRIMARY KEY REFERENCES project_members(id) ON DELETE CASCADE,
    owner_member_id    TEXT NOT NULL REFERENCES project_members(id) ON DELETE CASCADE,
    channel_id         TEXT REFERENCES channels(id) ON DELETE CASCADE,
    system_prompt      TEXT NOT NULL DEFAULT '',
    trigger_mode       TEXT NOT NULL DEFAULT 'on_mention',
    model              TEXT,
    created_at         REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agents_owner ON agents(owner_member_id);
CREATE INDEX IF NOT EXISTS idx_agents_channel ON agents(channel_id);
