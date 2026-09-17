"""PostgreSQL persistence for user-scoped workspaces and RAG resources."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


MIGRATIONS_DIR = Path("migrations")
ENV_PATH = Path(".env")

_db_lock = threading.Lock()
_db_local = threading.local()
_request_subject: ContextVar[str | None] = ContextVar("rag_request_subject", default=None)
_request_admin: ContextVar[bool] = ContextVar("rag_request_admin", default=False)
_request_global_document_manager: ContextVar[bool] = ContextVar(
    "rag_request_global_document_manager",
    default=False,
)


@contextmanager
def request_scope(
    subject: str | None,
    *,
    is_admin: bool = False,
    can_manage_global_documents: bool = False,
):
    """Apply ownership filtering to all persistence calls in this task context."""
    subject_token = _request_subject.set(subject.strip() if subject else None)
    admin_token = _request_admin.set(bool(is_admin))
    global_manager_token = _request_global_document_manager.set(
        bool(can_manage_global_documents) or bool(is_admin)
    )
    try:
        yield
    finally:
        _request_global_document_manager.reset(global_manager_token)
        _request_admin.reset(admin_token)
        _request_subject.reset(subject_token)


def current_subject() -> str | None:
    return _request_subject.get()


def current_is_admin() -> bool:
    return _request_admin.get()


def current_can_manage_global_documents() -> bool:
    return _request_global_document_manager.get()


def _knowledge_scope(value: object) -> str:
    return "project_only" if str(value or "").strip() == "project_only" else "project_and_global"


def load_dotenv() -> None:
    """Load local development variables without adding a runtime dependency."""
    try:
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip("\"'"))


def database_url() -> str:
    load_dotenv()
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is required. Start PostgreSQL with docker-compose and set DATABASE_URL in .env.")
    return url


def db() -> psycopg.Connection:
    """Return one reusable connection per worker thread.

    psycopg connections are not safe for concurrent transactions.  Uvicorn's
    ``to_thread`` workers therefore must not share the single process-global
    connection that the original compatibility layer used.
    """
    with _db_lock:
        connection = getattr(_db_local, "connection", None)
        if connection is None or connection.closed:
            connection = psycopg.connect(database_url(), row_factory=dict_row, autocommit=False)
            _db_local.connection = connection
        elif connection.info.transaction_status == psycopg.pq.TransactionStatus.INERROR:
            # PostgreSQL refuses every subsequent query after a failed statement
            # until the transaction is rolled back. The HTTP handlers reuse this
            # connection, so recover it before serving the next request.
            connection.rollback()
        return connection


@contextmanager
def read_cursor():
    """Return a cursor whose read transaction cannot leak between requests.

    Psycopg starts a transaction even for ``SELECT`` when autocommit is off.
    Closing only the cursor would leave the reused backend connection ``idle in
    transaction`` and can block schema migrations indefinitely.
    """
    connection = db()
    try:
        with connection.cursor() as cursor:
            yield cursor
    finally:
        if not connection.closed and connection.info.transaction_status in {
            psycopg.pq.TransactionStatus.INTRANS,
            psycopg.pq.TransactionStatus.INERROR,
        }:
            connection.rollback()


def rollback_transaction() -> None:
    """Clear a failed transaction before returning an HTTP error response."""
    with _db_lock:
        connection = getattr(_db_local, "connection", None)
        if connection is not None and not connection.closed:
            connection.rollback()


def close_thread_connection() -> None:
    """Close the current worker thread's cached connection, if any."""
    with _db_lock:
        connection = getattr(_db_local, "connection", None)
        if connection is not None and not connection.closed:
            connection.close()
        _db_local.connection = None


def run_migrations() -> None:
    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        return
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        conn.commit()
        cur.execute("SELECT version FROM schema_migrations")
        applied = {str(row["version"]) for row in cur.fetchall()}
    for path in migration_files:
        if path.stem in applied:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        conn.commit()
    if any(path.stem == "0005_document_versions" for path in migration_files):
        _backfill_document_versions(conn)


def ensure_default_workspace(default_mode: str, default_provider: str, default_model: str) -> dict:
    conn = db()
    subject = current_subject()
    with conn.cursor() as cur:
        if subject:
            cur.execute(
                "SELECT * FROM workspace_settings WHERE owner_subject = %s AND deleted_at IS NULL LIMIT 1",
                (subject,),
            )
        else:
            cur.execute("SELECT * FROM workspace_settings WHERE deleted_at IS NULL ORDER BY created_at ASC LIMIT 1")
        workspace = cur.fetchone()
        if workspace is None:
            cur.execute(
                """INSERT INTO projects
                   (owner_subject, name, description, settings, memory, knowledge_scope)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (
                    subject,
                    "Мой проект" if subject else "Default project",
                    "Личная рабочая область" if subject else "Auto-created workspace project",
                    json.dumps({"llm": {}, "ui": {}}),
                    json.dumps({}),
                    "project_and_global",
                ),
            )
            project = cur.fetchone()
            cur.execute(
                """INSERT INTO chats (owner_subject, project_id, title, mode, provider, model_name, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (subject, project["id"], "Новый чат", default_mode, default_provider, default_model, json.dumps({})),
            )
            chat = cur.fetchone()
            cur.execute(
                """INSERT INTO workspace_settings (owner_subject, active_project_id, active_chat_id, settings)
                   VALUES (%s, %s, %s, %s) RETURNING *""",
                (subject, project["id"], chat["id"], json.dumps({"theme": "light"})),
            )
            workspace = cur.fetchone()
            conn.commit()
        return dict(workspace)


def list_projects() -> list[dict]:
    subject = current_subject()
    with read_cursor() as cur:
        query = """
            SELECT p.*,
                COALESCE((SELECT json_agg(c ORDER BY c.created_at ASC) FROM chats c
                          WHERE c.project_id = p.id AND c.deleted_at IS NULL
                            AND (p.owner_subject IS NULL OR c.owner_subject = p.owner_subject)), '[]'::json) AS chats
            FROM projects p
            WHERE p.deleted_at IS NULL
        """
        params: list[object] = []
        if subject:
            query += " AND p.owner_subject = %s"
            params.append(subject)
        query += " ORDER BY p.created_at ASC"
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def create_project(payload: dict) -> dict:
    conn = db()
    subject = current_subject()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO projects
               (owner_subject, name, description, settings, memory, knowledge_scope)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                subject,
                str(payload.get("name", "")).strip() or "Новый проект",
                str(payload.get("description", "")).strip(),
                json.dumps(payload.get("settings") or {}, ensure_ascii=False),
                json.dumps(payload.get("memory") or {}, ensure_ascii=False),
                _knowledge_scope(payload.get("knowledge_scope")),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row)


def get_project(project_id: str) -> dict | None:
    subject = current_subject()
    with read_cursor() as cur:
        if subject:
            cur.execute(
                "SELECT * FROM projects WHERE id = %s AND owner_subject = %s AND deleted_at IS NULL",
                (project_id, subject),
            )
        else:
            cur.execute("SELECT * FROM projects WHERE id = %s AND deleted_at IS NULL", (project_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_project(project_id: str, payload: dict) -> dict | None:
    fields, values = _update_values(payload, ("name", "description"), ("settings", "memory"))
    if "knowledge_scope" in payload:
        fields.append("knowledge_scope = %s")
        values.append(_knowledge_scope(payload.get("knowledge_scope")))
    if not fields:
        return get_project(project_id)
    if get_project(project_id) is None:
        return None
    return _update_entity("projects", project_id, fields, values)


def delete_project(project_id: str) -> bool:
    if get_project(project_id) is None:
        return False
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE chats SET project_id = NULL, updated_at = now() WHERE project_id = %s AND deleted_at IS NULL",
            (project_id,),
        )
        cur.execute(
            "UPDATE projects SET deleted_at = now(), updated_at = now() WHERE id = %s AND deleted_at IS NULL RETURNING id",
            (project_id,),
        )
        deleted = cur.fetchone() is not None
    conn.commit()
    return deleted


def list_chats(project_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM chats WHERE deleted_at IS NULL"
    params: list[object] = []
    subject = current_subject()
    if subject:
        query += " AND owner_subject = %s"
        params.append(subject)
    if project_id:
        query += " AND project_id = %s"
        params.append(project_id)
    query += " ORDER BY created_at ASC"
    with read_cursor() as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def create_chat(project_id: str | None, payload: dict, default_mode: str, default_provider: str, default_model: str) -> dict:
    normalized_project_id = str(project_id or "").strip() or None
    if normalized_project_id is not None and get_project(normalized_project_id) is None:
        raise ValueError("project not found")
    conn = db()
    subject = current_subject()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO chats (owner_subject, project_id, title, mode, provider, model_name, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                subject,
                normalized_project_id,
                str(payload.get("title", "")).strip() or "Новый чат",
                str(payload.get("mode", default_mode)).strip() or default_mode,
                str(payload.get("provider", default_provider)).strip() or default_provider,
                str(payload.get("model_name", default_model)).strip() or default_model,
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row)


def get_chat(chat_id: str) -> dict | None:
    subject = current_subject()
    with read_cursor() as cur:
        if subject:
            cur.execute(
                "SELECT * FROM chats WHERE id = %s AND owner_subject = %s AND deleted_at IS NULL",
                (chat_id, subject),
            )
        else:
            cur.execute("SELECT * FROM chats WHERE id = %s AND deleted_at IS NULL", (chat_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_chat(chat_id: str, payload: dict) -> dict | None:
    if get_chat(chat_id) is None:
        return None
    fields, values = _update_values(payload, ("title", "mode", "provider", "model_name"), ("metadata",))
    if "project_id" in payload:
        project_id = str(payload.get("project_id", "")).strip()
        if not project_id or get_project(project_id) is None:
            return None
        fields.append("project_id = %s")
        values.append(project_id)
    if not fields:
        return get_chat(chat_id)
    return _update_entity("chats", chat_id, fields, values)


def delete_chat(chat_id: str) -> bool:
    if get_chat(chat_id) is None:
        return False
    return _soft_delete("chats", chat_id)


def list_messages(chat_id: str) -> list[dict]:
    if get_chat(chat_id) is None:
        return []
    with read_cursor() as cur:
        cur.execute("SELECT * FROM messages WHERE chat_id = %s AND deleted_at IS NULL ORDER BY created_at ASC", (chat_id,))
        return [dict(row) for row in cur.fetchall()]


def get_project_conversation(chat_id: str, limit: int = 40) -> dict | None:
    """Return project history, or only the active chat history for a free chat."""
    subject = current_subject()
    with read_cursor() as cur:
        ownership = " AND c.owner_subject = %s" if subject else ""
        scope_params: tuple[object, ...] = (chat_id, subject) if subject else (chat_id,)
        cur.execute(
            f"""
            SELECT c.project_id, c.owner_subject, p.name AS project_name, p.memory, p.knowledge_scope
            FROM chats c
            LEFT JOIN projects p ON p.id = c.project_id AND p.deleted_at IS NULL
            WHERE c.id = %s AND c.deleted_at IS NULL
            {ownership}
            """,
            scope_params,
        )
        scope = cur.fetchone()
        if scope is None:
            return None
        if scope["project_id"] is None:
            chat_owner_filter = " AND c.owner_subject = %s" if subject else ""
            message_params: tuple[object, ...] = (
                (chat_id, subject, max(1, min(limit, 100)))
                if subject else (chat_id, max(1, min(limit, 100)))
            )
            cur.execute(
                f"""
                SELECT c.id AS chat_id, c.title AS chat_title, m.role, m.content, m.created_at
                FROM messages m
                JOIN chats c ON c.id = m.chat_id
                WHERE c.id = %s AND c.deleted_at IS NULL
                  {chat_owner_filter}
                  AND m.deleted_at IS NULL AND m.status = 'complete'
                ORDER BY m.created_at DESC LIMIT %s
                """,
                message_params,
            )
        else:
            project_owner_filter = " AND c.owner_subject = %s" if subject else ""
            message_params = (
                (scope["project_id"], subject, max(1, min(limit, 100)))
                if subject else (scope["project_id"], max(1, min(limit, 100)))
            )
            cur.execute(
                f"""
            SELECT c.id AS chat_id, c.title AS chat_title, m.role, m.content, m.created_at
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE c.project_id = %s
              AND c.deleted_at IS NULL
              {project_owner_filter}
              AND m.deleted_at IS NULL
              AND m.status = 'complete'
            ORDER BY m.created_at DESC
            LIMIT %s
                """,
                message_params,
            )
        messages = [dict(row) for row in reversed(cur.fetchall())]
    return {**dict(scope), "messages": messages}


def create_message(chat_id: str, payload: dict) -> dict:
    if get_chat(chat_id) is None:
        raise ValueError("chat not found")
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO messages (chat_id, role, content, content_html, status, metadata)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                chat_id,
                str(payload.get("role", "user")),
                str(payload.get("content", "")),
                str(payload.get("content_html", "")),
                str(payload.get("status", "complete")),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row)


def get_message(message_id: str) -> dict | None:
    subject = current_subject()
    with read_cursor() as cur:
        if subject:
            cur.execute(
                """SELECT m.* FROM messages m JOIN chats c ON c.id = m.chat_id
                   WHERE m.id = %s AND m.deleted_at IS NULL
                     AND c.deleted_at IS NULL AND c.owner_subject = %s""",
                (message_id, subject),
            )
        else:
            cur.execute("SELECT * FROM messages WHERE id = %s AND deleted_at IS NULL", (message_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_message(message_id: str, payload: dict) -> dict | None:
    if get_message(message_id) is None:
        return None
    fields, values = _update_values(payload, ("content", "content_html", "status"), ("metadata",), strip_text=False)
    if not fields:
        return get_message(message_id)
    return _update_entity("messages", message_id, fields, values)


def delete_message(message_id: str) -> bool:
    if get_message(message_id) is None:
        return False
    return _soft_delete("messages", message_id)


def list_documents(project_id: str | None = None, chat_id: str | None = None) -> list[dict]:
    subject = current_subject()
    query = "SELECT * FROM documents WHERE deleted_at IS NULL"
    params: list[object] = []

    if subject:
        if project_id:
            project = get_project(project_id)
            if project is None:
                return []
            if _knowledge_scope(project.get("knowledge_scope")) == "project_only":
                query += " AND project_id = %s AND owner_subject = %s"
                params.extend((project_id, subject))
            else:
                query += " AND ((project_id = %s AND owner_subject = %s) OR owner_subject IS NULL)"
                params.extend((project_id, subject))
        elif chat_id:
            if get_chat(chat_id) is None:
                return []
            query += " AND ((chat_id = %s AND owner_subject = %s) OR owner_subject IS NULL)"
            params.extend((chat_id, subject))
        else:
            query += " AND owner_subject IS NULL"
    else:
        if project_id:
            query += " AND project_id = %s"
            params.append(project_id)
        if chat_id:
            query += " AND chat_id = %s"
            params.append(chat_id)

    query += " ORDER BY created_at DESC"
    with read_cursor() as cur:
        cur.execute(query, params)
        return [_document_record(row) for row in cur.fetchall()]


def list_all_documents_for_retrieval() -> list[dict]:
    """Return active records and tombstones for retrieval access filtering."""
    with read_cursor() as cur:
        cur.execute("SELECT * FROM documents ORDER BY created_at DESC")
        return [_document_record(row) for row in cur.fetchall()]


def _document_record(row: object) -> dict:
    record = dict(row)
    record["scope"] = "global" if not record.get("owner_subject") else "project"
    return record


def _document_version_fields(file_name: str, metadata: dict) -> dict:
    from document_catalog import document_version_fields

    return document_version_fields({"file_name": file_name, "metadata": metadata})


def _version_group(record: dict) -> tuple[object, object, object, str, str] | None:
    family = str(record.get("document_family") or "").strip()
    kind = str(record.get("version_kind") or "").strip()
    if not family or not kind or not record.get("version_sort_key"):
        return None
    return (
        record.get("owner_subject"),
        record.get("project_id"),
        record.get("chat_id"),
        family,
        kind,
    )


def _rebuild_document_version_chain(cur: object, group: tuple[object, object, object, str, str]) -> None:
    """Re-link one version family after out-of-order upload, edit, or deletion."""
    owner_subject, project_id, chat_id, family, kind = group
    cur.execute(
        """SELECT id, version_sort_key FROM documents
           WHERE deleted_at IS NULL
             AND owner_subject IS NOT DISTINCT FROM %s
             AND project_id IS NOT DISTINCT FROM %s
             AND chat_id IS NOT DISTINCT FROM %s
             AND document_family = %s AND version_kind = %s
             AND version_sort_key IS NOT NULL
           ORDER BY version_sort_key ASC, created_at ASC, id ASC""",
        (owner_subject, project_id, chat_id, family, kind),
    )
    predecessor_id = None
    predecessor_key: tuple | None = None
    previous_version_id = None
    for version_row in cur.fetchall():
        current_key = tuple(version_row.get("version_sort_key") or ())
        if predecessor_key is not None and current_key > predecessor_key:
            predecessor_id = previous_version_id
        cur.execute(
            "UPDATE documents SET supersedes_document_id = %s WHERE id = %s",
            (predecessor_id, version_row["id"]),
        )
        if predecessor_key is None or current_key > predecessor_key:
            predecessor_key = current_key
            previous_version_id = version_row["id"]


def _backfill_document_versions(conn: object) -> None:
    """Populate persisted version metadata and a same-scope supersession chain."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT * FROM documents
               WHERE deleted_at IS NULL AND document_family IS NULL"""
        )
        missing = [dict(row) for row in cur.fetchall()]
    if missing:
        with conn.cursor() as cur:
            for document in missing:
                fields = _document_version_fields(
                    str(document.get("file_name") or ""),
                    dict(document.get("metadata") or {}),
                )
                cur.execute(
                    """UPDATE documents
                       SET document_family = %s, version_label = %s, version_kind = %s,
                           version_sort_key = %s, version_source = %s, effective_on = %s,
                           updated_at = now()
                       WHERE id = %s""",
                    (
                        fields["document_family"],
                        fields["version_label"],
                        fields["version_kind"],
                        fields["version_sort_key"],
                        fields["version_source"],
                        fields["effective_on"],
                        document["id"],
                    ),
                )
        conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id, owner_subject, project_id, chat_id, document_family,
                      version_kind, version_sort_key, created_at
               FROM documents
               WHERE deleted_at IS NULL AND document_family IS NOT NULL
                 AND version_kind IS NOT NULL AND version_sort_key IS NOT NULL"""
        )
        versioned = [dict(row) for row in cur.fetchall()]
    groups: dict[tuple, list[dict]] = {}
    for document in versioned:
        group_key = (
            document.get("owner_subject"),
            document.get("project_id"),
            document.get("chat_id"),
            document.get("document_family"),
            document.get("version_kind"),
        )
        groups.setdefault(group_key, []).append(document)
    with conn.cursor() as cur:
        for group in groups.values():
            ordered = sorted(
                group,
                key=lambda item: (
                    tuple(item.get("version_sort_key") or ()),
                    item.get("created_at"),
                    str(item.get("id")),
                ),
            )
            predecessor_id = None
            predecessor_key: tuple | None = None
            for document in ordered:
                current_key = tuple(document.get("version_sort_key") or ())
                if predecessor_key is not None and current_key > predecessor_key:
                    predecessor_id = previous_id
                cur.execute(
                    "UPDATE documents SET supersedes_document_id = %s WHERE id = %s",
                    (predecessor_id, document["id"]),
                )
                if predecessor_key is None or current_key > predecessor_key:
                    predecessor_key = current_key
                    previous_id = document["id"]
    conn.commit()


def create_document(project_id: str | None, chat_id: str | None, payload: dict) -> dict:
    subject = current_subject()
    project_id = project_id or None
    chat_id = chat_id or None
    if project_id and get_project(project_id) is None:
        raise ValueError("project not found")
    if chat_id and get_chat(chat_id) is None:
        raise ValueError("chat not found")
    if subject and not project_id and not chat_id and not current_can_manage_global_documents():
        raise PermissionError("global document upload is not permitted for this account")

    document_owner = subject if project_id or chat_id else None
    metadata = dict(payload.get("metadata") or {})
    metadata["scope"] = "global" if not document_owner else "project"
    file_name = str(payload.get("file_name", "")).strip()
    version = _document_version_fields(file_name, metadata)
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO documents
               (owner_subject, project_id, chat_id, file_name, source_path, mime_type,
                status, processing_state, metadata, document_family, version_label,
                version_kind, version_sort_key, version_source, effective_on,
                supersedes_document_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING *""",
            (
                document_owner,
                project_id,
                chat_id,
                file_name,
                str(payload.get("source_path", "")).strip(),
                str(payload.get("mime_type", "")).strip(),
                str(payload.get("status", "queued")).strip() or "queued",
                json.dumps(payload.get("processing_state") or {}, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                version["document_family"],
                version["version_label"],
                version["version_kind"],
                version["version_sort_key"],
                version["version_source"],
                version["effective_on"],
                None,
            ),
        )
        row = cur.fetchone()
        version_group = _version_group(dict(row))
        if version_group:
            _rebuild_document_version_chain(cur, version_group)
            cur.execute("SELECT * FROM documents WHERE id = %s", (row["id"],))
            row = cur.fetchone()
    conn.commit()
    return _document_record(row)


def get_document(document_id: str) -> dict | None:
    subject = current_subject()
    with read_cursor() as cur:
        if subject and not current_is_admin():
            cur.execute(
                """SELECT * FROM documents
                   WHERE id = %s AND deleted_at IS NULL
                     AND (owner_subject = %s OR owner_subject IS NULL)""",
                (document_id, subject),
            )
        else:
            cur.execute("SELECT * FROM documents WHERE id = %s AND deleted_at IS NULL", (document_id,))
        row = cur.fetchone()
        return _document_record(row) if row else None


def update_document(document_id: str, payload: dict) -> dict | None:
    document = get_document(document_id)
    if document is None:
        return None
    if current_subject() and not current_can_manage_global_documents() and not document.get("owner_subject"):
        raise PermissionError("shared document update is not permitted for this account")
    effective_payload = dict(payload)
    if "metadata" in effective_payload:
        merged_metadata = dict(document.get("metadata") or {})
        merged_metadata.update(dict(effective_payload.get("metadata") or {}))
        effective_payload["metadata"] = merged_metadata
    fields, values = _update_values(
        effective_payload,
        ("file_name", "source_path", "mime_type", "status"),
        ("processing_state", "metadata"),
    )
    if "file_name" in effective_payload or "metadata" in effective_payload:
        file_name = str(effective_payload.get("file_name", document.get("file_name", ""))).strip()
        metadata = dict(effective_payload.get("metadata") or document.get("metadata") or {})
        version = _document_version_fields(file_name, metadata)
        for key in (
            "document_family",
            "version_label",
            "version_kind",
            "version_sort_key",
            "version_source",
            "effective_on",
        ):
            fields.append(f"{key} = %s")
            values.append(version[key])
        fields.append("supersedes_document_id = %s")
        values.append(None)
    if not fields:
        return document
    old_group = _version_group(document)
    updated = _update_entity("documents", document_id, fields, values)
    if not updated:
        return None
    new_group = _version_group(updated)
    groups = {group for group in (old_group, new_group) if group}
    if groups:
        conn = db()
        with conn.cursor() as cur:
            for group in groups:
                _rebuild_document_version_chain(cur, group)
            cur.execute("SELECT * FROM documents WHERE id = %s", (document_id,))
            updated = dict(cur.fetchone())
        conn.commit()
    return _document_record(updated)


def tombstone_document(document_id: str) -> dict | None:
    """Immediately hide an owned document while queued index cleanup runs."""
    document = get_document(document_id)
    if document is None:
        return None
    if current_subject() and not current_can_manage_global_documents() and not document.get("owner_subject"):
        raise PermissionError("shared document deletion is not permitted for this account")
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE documents
               SET status = 'deleting', deleted_at = now(), updated_at = now()
               WHERE id = %s AND deleted_at IS NULL
               RETURNING *""",
            (document_id,),
        )
        row = cur.fetchone()
        version_group = _version_group(document)
        if version_group:
            _rebuild_document_version_chain(cur, version_group)
    conn.commit()
    return _document_record(row) if row else None


def document_sources(source_names: list[str]) -> dict[str, dict]:
    """Resolve retrieval metadata names to active uploaded document records."""
    names = {str(name).strip() for name in source_names if str(name).strip()}
    if not names:
        return {}
    subject = current_subject()
    with read_cursor() as cur:
        if subject and not current_is_admin():
            cur.execute(
                """SELECT * FROM documents WHERE deleted_at IS NULL AND status = 'ready'
                   AND (owner_subject = %s OR owner_subject IS NULL)""",
                (subject,),
            )
        else:
            cur.execute("SELECT * FROM documents WHERE deleted_at IS NULL AND status = 'ready'")
        rows = [_document_record(row) for row in cur.fetchall()]
    resolved: dict[str, dict] = {}
    for row in rows:
        stored_name = Path(str(row.get("source_path", ""))).name
        display_name = str(row.get("file_name", ""))
        for candidate in names:
            if candidate == stored_name or candidate == display_name:
                resolved[candidate] = row
    return resolved


def get_workspace(default_mode: str, default_provider: str, default_model: str) -> dict:
    return ensure_default_workspace(default_mode, default_provider, default_model)


def update_workspace(payload: dict, default_mode: str, default_provider: str, default_model: str) -> dict:
    fields, values = _update_values(payload, (), ("settings",))
    if "active_project_id" in payload:
        project_id = str(payload.get("active_project_id") or "").strip() or None
        if project_id and get_project(project_id) is None:
            raise ValueError("project not found")
        fields.append("active_project_id = %s")
        values.append(project_id)
    if "active_chat_id" in payload:
        chat_id = str(payload.get("active_chat_id") or "").strip() or None
        if chat_id and get_chat(chat_id) is None:
            raise ValueError("chat not found")
        fields.append("active_chat_id = %s")
        values.append(chat_id)
    workspace = get_workspace(default_mode, default_provider, default_model)
    if not fields:
        return workspace
    return _update_entity("workspace_settings", str(workspace["id"]), fields, values) or workspace


def _update_values(
    payload: dict,
    text_fields: tuple[str, ...],
    json_fields: tuple[str, ...],
    *,
    strip_text: bool = True,
) -> tuple[list[str], list[object]]:
    fields: list[str] = []
    values: list[object] = []
    for key in text_fields:
        if key in payload:
            fields.append(f"{key} = %s")
            value = str(payload.get(key, ""))
            values.append(value.strip() if strip_text else value)
    for key in json_fields:
        if key in payload:
            fields.append(f"{key} = %s")
            values.append(json.dumps(payload.get(key) or {}, ensure_ascii=False))
    return fields, values


def _update_entity(table: str, entity_id: str, fields: list[str], values: list[object]) -> dict | None:
    conn = db()
    values = [*values, entity_id]
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET {', '.join(fields)}, updated_at = now() WHERE id = %s AND deleted_at IS NULL RETURNING *",
            values,
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row) if row else None


def _soft_delete(table: str, entity_id: str) -> bool:
    conn = db()
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {table} SET deleted_at = now(), updated_at = now() WHERE id = %s AND deleted_at IS NULL", (entity_id,))
        deleted = cur.rowcount > 0
    conn.commit()
    return deleted
