CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS workspace_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NULL,
    user_id UUID NULL,
    active_project_id UUID NULL,
    active_chat_id UUID NULL,
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NULL,
    user_id UUID NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,
    memory JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    tenant_id UUID NULL,
    user_id UUID NULL,
    title TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'python',
    provider TEXT NOT NULL DEFAULT 'ollama',
    model_name TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chat_id UUID NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
    content TEXT NOT NULL DEFAULT '',
    content_html TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'complete',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NULL REFERENCES projects(id) ON DELETE SET NULL,
    chat_id UUID NULL REFERENCES chats(id) ON DELETE SET NULL,
    tenant_id UUID NULL,
    user_id UUID NULL,
    file_name TEXT NOT NULL,
    source_path TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    processing_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS request_errors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NULL REFERENCES projects(id) ON DELETE SET NULL,
    chat_id UUID NULL REFERENCES chats(id) ON DELETE SET NULL,
    message_id UUID NULL REFERENCES messages(id) ON DELETE SET NULL,
    tenant_id UUID NULL,
    user_id UUID NULL,
    request_kind TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS evidence_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    document_id UUID NULL REFERENCES documents(id) ON DELETE SET NULL,
    source_name TEXT NOT NULL,
    section_path TEXT NOT NULL DEFAULT '',
    score DOUBLE PRECISION NOT NULL DEFAULT 0,
    content_preview TEXT NOT NULL DEFAULT '',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS rbac_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS rbac_permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ NULL
);

CREATE TABLE IF NOT EXISTS rbac_role_permissions (
    role_id UUID NOT NULL REFERENCES rbac_roles(id) ON DELETE CASCADE,
    permission_id UUID NOT NULL REFERENCES rbac_permissions(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (role_id, permission_id)
);

CREATE TABLE IF NOT EXISTS rbac_user_roles (
    user_id UUID NOT NULL,
    role_id UUID NOT NULL REFERENCES rbac_roles(id) ON DELETE CASCADE,
    tenant_id UUID NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, role_id)
);

CREATE INDEX IF NOT EXISTS idx_projects_tenant_deleted ON projects (tenant_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_chats_project_deleted ON chats (project_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_messages_chat_deleted_created ON messages (chat_id, deleted_at, created_at);
CREATE INDEX IF NOT EXISTS idx_documents_project_deleted ON documents (project_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_request_errors_chat_created ON request_errors (chat_id, created_at);
