ALTER TABLE chats
    ALTER COLUMN project_id DROP NOT NULL;

ALTER TABLE chats
    DROP CONSTRAINT IF EXISTS chats_project_id_fkey;

ALTER TABLE chats
    ADD CONSTRAINT chats_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL;
