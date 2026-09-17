ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS document_family TEXT NULL,
    ADD COLUMN IF NOT EXISTS version_label TEXT NULL,
    ADD COLUMN IF NOT EXISTS version_kind TEXT NULL,
    ADD COLUMN IF NOT EXISTS version_sort_key BIGINT[] NULL,
    ADD COLUMN IF NOT EXISTS version_source TEXT NULL,
    ADD COLUMN IF NOT EXISTS effective_on DATE NULL,
    ADD COLUMN IF NOT EXISTS supersedes_document_id UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'documents_version_kind_check'
          AND conrelid = 'documents'::regclass
    ) THEN
        ALTER TABLE documents
            ADD CONSTRAINT documents_version_kind_check
            CHECK (version_kind IS NULL OR version_kind IN ('semantic', 'date', 'manual'))
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'documents_version_sort_key_check'
          AND conrelid = 'documents'::regclass
    ) THEN
        ALTER TABLE documents
            ADD CONSTRAINT documents_version_sort_key_check
            CHECK (version_sort_key IS NULL OR cardinality(version_sort_key) = 4)
            NOT VALID;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'documents_supersedes_document_id_fkey'
          AND conrelid = 'documents'::regclass
    ) THEN
        ALTER TABLE documents
            ADD CONSTRAINT documents_supersedes_document_id_fkey
            FOREIGN KEY (supersedes_document_id) REFERENCES documents(id)
            ON DELETE SET NULL NOT VALID;
    END IF;
END $$;

ALTER TABLE documents VALIDATE CONSTRAINT documents_version_kind_check;
ALTER TABLE documents VALIDATE CONSTRAINT documents_version_sort_key_check;
ALTER TABLE documents VALIDATE CONSTRAINT documents_supersedes_document_id_fkey;

CREATE INDEX IF NOT EXISTS idx_documents_scope_family_version
    ON documents (
        owner_subject,
        project_id,
        chat_id,
        document_family,
        version_kind,
        version_sort_key DESC
    )
    WHERE deleted_at IS NULL AND document_family IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_documents_supersedes
    ON documents (supersedes_document_id)
    WHERE supersedes_document_id IS NOT NULL;
