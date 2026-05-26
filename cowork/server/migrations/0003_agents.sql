-- Agents are project members too — same id space, same display-name uniqueness,
-- same membership lookup. They're distinguished by `kind='agent'` and carry
-- their system prompt + model choice in a JSON `agent_config` blob.
-- Humans keep `kind='human'` (the default) and `agent_config=NULL`.

ALTER TABLE project_members ADD COLUMN kind TEXT NOT NULL DEFAULT 'human';
ALTER TABLE project_members ADD COLUMN agent_config TEXT;
