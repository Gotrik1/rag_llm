"""PostgreSQL persistence for workspace, projects, chats, and messages.

The module deliberately knows nothing about HTTP or RAG. Authentication is not
enabled yet; tenant_id and user_id columns remain available for its future use.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


MIGRATIONS_DIR = Path("migrations")
ENV_PATH = Path(".env")

_db_lock = threading.Lock()
_db_conn: psycopg.Connection | None = None


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
    global _db_conn
    with _db_lock:
        if _db_conn is None or _db_conn.closed:
            _db_conn = psycopg.connect(database_url(), row_factory=dict_row, autocommit=False)
        return _db_conn


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


def ensure_default_workspace(default_mode: str, default_provider: str, default_model: str) -> dict:
    conn = db()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM workspace_settings WHERE deleted_at IS NULL ORDER BY created_at ASC LIMIT 1")
        workspace = cur.fetchone()
        if workspace is None:
            cur.execute(
                "INSERT INTO projects (name, description, settings, memory) VALUES (%s, %s, %s, %s) RETURNING *",
                ("Default project", "Auto-created workspace project", json.dumps({"llm": {}, "ui": {}}), json.dumps({})),
            )
            project = cur.fetchone()
            cur.execute(
                """INSERT INTO chats (project_id, title, mode, provider, model_name, metadata)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                (project["id"], "Новый чат", default_mode, default_provider, default_model, json.dumps({})),
            )
            chat = cur.fetchone()
            cur.execute(
                """INSERT INTO workspace_settings (active_project_id, active_chat_id, settings)
                   VALUES (%s, %s, %s) RETURNING *""",
                (project["id"], chat["id"], json.dumps({"theme": "light"})),
            )
            workspace = cur.fetchone()
            conn.commit()
        return dict(workspace)


def list_projects() -> list[dict]:
    with db().cursor() as cur:
        cur.execute(
            """
            SELECT p.*,
                COALESCE((SELECT json_agg(c ORDER BY c.created_at ASC) FROM chats c
                          WHERE c.project_id = p.id AND c.deleted_at IS NULL), '[]'::json) AS chats
            FROM projects p
            WHERE p.deleted_at IS NULL
            ORDER BY p.created_at ASC
            """
        )
        return [dict(row) for row in cur.fetchall()]


def create_project(payload: dict) -> dict:
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (name, description, settings, memory) VALUES (%s, %s, %s, %s) RETURNING *",
            (
                str(payload.get("name", "")).strip() or "Новый проект",
                str(payload.get("description", "")).strip(),
                json.dumps(payload.get("settings") or {}, ensure_ascii=False),
                json.dumps(payload.get("memory") or {}, ensure_ascii=False),
            ),
        )
        row = cur.fetchone()
    conn.commit()
    return dict(row)


def get_project(project_id: str) -> dict | None:
    with db().cursor() as cur:
        cur.execute("SELECT * FROM projects WHERE id = %s AND deleted_at IS NULL", (project_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_project(project_id: str, payload: dict) -> dict | None:
    fields, values = _update_values(payload, ("name", "description"), ("settings", "memory"))
    if not fields:
        return get_project(project_id)
    return _update_entity("projects", project_id, fields, values)


def delete_project(project_id: str) -> bool:
    return _soft_delete("projects", project_id)


def list_chats(project_id: str | None = None) -> list[dict]:
    query = "SELECT * FROM chats WHERE deleted_at IS NULL"
    params: list[object] = []
    if project_id:
        query += " AND project_id = %s"
        params.append(project_id)
    query += " ORDER BY created_at ASC"
    with db().cursor() as cur:
        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def create_chat(project_id: str, payload: dict, default_mode: str, default_provider: str, default_model: str) -> dict:
    conn = db()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO chats (project_id, title, mode, provider, model_name, metadata)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
            (
                project_id,
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
    with db().cursor() as cur:
        cur.execute("SELECT * FROM chats WHERE id = %s AND deleted_at IS NULL", (chat_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_chat(chat_id: str, payload: dict) -> dict | None:
    fields, values = _update_values(payload, ("title", "mode", "provider", "model_name"), ("metadata",))
    if not fields:
        return get_chat(chat_id)
    return _update_entity("chats", chat_id, fields, values)


def delete_chat(chat_id: str) -> bool:
    return _soft_delete("chats", chat_id)


def list_messages(chat_id: str) -> list[dict]:
    with db().cursor() as cur:
        cur.execute("SELECT * FROM messages WHERE chat_id = %s AND deleted_at IS NULL ORDER BY created_at ASC", (chat_id,))
        return [dict(row) for row in cur.fetchall()]


def create_message(chat_id: str, payload: dict) -> dict:
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
    with db().cursor() as cur:
        cur.execute("SELECT * FROM messages WHERE id = %s AND deleted_at IS NULL", (message_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def update_message(message_id: str, payload: dict) -> dict | None:
    fields, values = _update_values(payload, ("content", "content_html", "status"), ("metadata",), strip_text=False)
    if not fields:
        return get_message(message_id)
    return _update_entity("messages", message_id, fields, values)


def delete_message(message_id: str) -> bool:
    return _soft_delete("messages", message_id)


def get_workspace(default_mode: str, default_provider: str, default_model: str) -> dict:
    return ensure_default_workspace(default_mode, default_provider, default_model)


def update_workspace(payload: dict, default_mode: str, default_provider: str, default_model: str) -> dict:
    fields, values = _update_values(payload, (), ("settings",))
    for key in ("active_project_id", "active_chat_id"):
        if key in payload:
            fields.append(f"{key} = %s")
            values.append(payload[key])
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
