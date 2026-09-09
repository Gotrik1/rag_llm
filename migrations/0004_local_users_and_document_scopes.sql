CREATE TABLE IF NOT EXISTS app_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL,
    email_normalized TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    is_admin BOOLEAN NOT NULL DEFAULT FALSE,
    failed_login_count INTEGER NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
    locked_until TIMESTAMPTZ NULL,
    last_login_at TIMESTAMPTZ NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
    user_id UUID NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ NULL,
    user_agent_hash TEXT NOT NULL DEFAULT '',
    ip_address INET NULL
);

ALTER TABLE workspace_settings ADD COLUMN IF NOT EXISTS owner_subject TEXT NULL;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner_subject TEXT NULL;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS knowledge_scope TEXT NOT NULL DEFAULT 'project_and_global';
ALTER TABLE chats ADD COLUMN IF NOT EXISTS owner_subject TEXT NULL;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS owner_subject TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'projects_knowledge_scope_check'
          AND conrelid = 'projects'::regclass
    ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_knowledge_scope_check
            CHECK (knowledge_scope IN ('project_only', 'project_and_global'));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_owner_active
    ON workspace_settings (owner_subject)
    WHERE deleted_at IS NULL AND owner_subject IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_projects_owner_active_created
    ON projects (owner_subject, created_at)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_chats_owner_project_active_created
    ON chats (owner_subject, project_id, created_at)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_owner_project_active_created
    ON documents (owner_subject, project_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active
    ON auth_sessions (user_id, expires_at DESC)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry
    ON auth_sessions (expires_at)
    WHERE revoked_at IS NULL;

-- The corpus that predates per-user accounts is the shared library requested
-- by the product owner. This migration changes only registry ownership; the
-- original files, vector payloads and source links remain intact.
UPDATE documents
SET project_id = NULL,
    chat_id = NULL,
    user_id = NULL,
    owner_subject = NULL,
    metadata = COALESCE(metadata, '{}'::jsonb) || '{"scope":"global"}'::jsonb,
    updated_at = now()
WHERE deleted_at IS NULL;
