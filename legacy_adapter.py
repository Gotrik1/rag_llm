"""Compatibility bridge for the legacy HTTP handler during the ASGI migration."""
from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from typing import Any


def invoke_legacy(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    content_type: str = "",
    raw_body: bytes = b"",
) -> tuple[dict, int]:
    """Execute an existing API handler in-process without a second HTTP server.

    The adapter deliberately delegates routing to ``web_ui.Handler`` rather
    than duplicating the workspace/chat API map. It keeps the current UI
    contract intact while FastAPI owns authentication, authorization,
    observability and request lifecycle concerns.
    """
    from web_ui import Handler

    handler_method = getattr(Handler, f"do_{method.upper()}", None)
    if handler_method is None:
        return {"error": "method not allowed"}, 405

    if not raw_body and body is not None:
        raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    captured: dict[str, Any] = {}
    handler = object.__new__(Handler)
    handler.path = path
    handler.command = method.upper()
    # Keep the email-style header interface expected by BaseHTTPRequestHandler
    # and the compatibility multipart parser.
    headers = Message()
    headers["Content-Type"] = content_type or "application/json"
    headers["Content-Length"] = str(len(raw_body))
    handler.headers = headers
    handler.rfile = BytesIO(raw_body)
    handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)

    try:
        handler_method(handler)
    finally:
        # The adapter bypasses BaseHTTPRequestHandler.finish(), so it owns the
        # per-worker psycopg connection lifecycle explicitly.
        from db_store import close_thread_connection
        close_thread_connection()
    return captured.get("payload", {"error": "empty legacy response"}), captured.get("status", 500)
