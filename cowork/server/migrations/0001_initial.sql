-- Cowork phases 0-2 schema.
-- `messages.parent_id` is included now so phase-7 DAG branching is purely additive.

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_members (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    display_name  TEXT NOT NULL,
    joined_at     REAL NOT NULL,
    UNIQUE (project_id, display_name)
);

CREATE INDEX IF NOT EXISTS idx_members_project ON project_members(project_id);

CREATE TABLE IF NOT EXISTS member_tokens (
    token_hash   TEXT PRIMARY KEY,
    member_id    TEXT NOT NULL REFERENCES project_members(id) ON DELETE CASCADE,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_member_tokens_member ON member_tokens(member_id);

CREATE TABLE IF NOT EXISTS invite_tokens (
    token_hash         TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_by         TEXT REFERENCES project_members(id) ON DELETE SET NULL,
    created_at         REAL NOT NULL,
    expires_at         REAL,
    max_uses           INTEGER,
    used_count         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_invite_tokens_project ON invite_tokens(project_id);

CREATE TABLE IF NOT EXISTS channels (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_channels_project ON channels(project_id);

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    channel_id   TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    member_id    TEXT NOT NULL REFERENCES project_members(id) ON DELETE CASCADE,
    parent_id    TEXT REFERENCES messages(id) ON DELETE SET NULL,
    kind         TEXT NOT NULL DEFAULT 'chat',
    content      TEXT NOT NULL,
    created_at   REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_channel_ts ON messages(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);

CREATE TABLE IF NOT EXISTS message_mentions (
    message_id  TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    member_id   TEXT NOT NULL REFERENCES project_members(id) ON DELETE CASCADE,
    PRIMARY KEY (message_id, member_id)
);

CREATE INDEX IF NOT EXISTS idx_mentions_member ON message_mentions(member_id);

CREATE TABLE IF NOT EXISTS channel_reads (
    member_id              TEXT NOT NULL REFERENCES project_members(id) ON DELETE CASCADE,
    channel_id             TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    last_read_message_id   TEXT REFERENCES messages(id) ON DELETE SET NULL,
    last_read_at           REAL NOT NULL,
    PRIMARY KEY (member_id, channel_id)
);
