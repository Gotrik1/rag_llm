-- Project-memory document registry and high-volume soft-delete query indexes.
CREATE INDEX IF NOT EXISTS idx_documents_project_active_created
    ON documents (project_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_chat_active_created
    ON documents (chat_id, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_documents_status_active
    ON documents (status, created_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_messages_chat_active_created
    ON messages (chat_id, created_at)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_evidence_sources_message_active
    ON evidence_sources (message_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_evidence_sources_document_active
    ON evidence_sources (document_id)
    WHERE deleted_at IS NULL AND document_id IS NOT NULL;
