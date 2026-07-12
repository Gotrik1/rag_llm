"""Compatibility bridge while HTTP handlers move from BaseHTTPRequestHandler."""
from __future__ import annotations

from io import BytesIO
from typing import Any

GET_OPERATIONS = {
    "/api/models": "handle_models", "/api/providers": "handle_provider_get",
    "/api/system-prompts": "handle_system_prompts", "/api/flow-mode": None,
}
POST_OPERATIONS = {
    "/api/debug": "handle_debug", "/api/models/select": "handle_model_select",
    "/api/providers": "handle_provider_save", "/api/providers/test": "handle_provider_test",
    "/api/system-prompts/select": "handle_system_prompt_select", "/api/flow-mode": "handle_flow_mode",
    "/api/load": "handle_load", "/api/cache/clear": "handle_cache_clear", "/api/upload": "handle_upload",
}


def invoke_legacy(method: str, path: str, body: dict[str, Any] | None = None, *, content_type: str = "", raw_body: bytes = b"") -> tuple[dict, int]:
    """Call an existing handler without opening a second HTTP server."""
    from web_ui import Handler, load_flow_mode
    operations = GET_OPERATIONS if method == "GET" else POST_OPERATIONS
    if path not in operations:
        return {"error": "not found"}, 404
    if path == "/api/flow-mode" and method == "GET":
        return {"mode": load_flow_mode()}, 200
    captured: dict[str, Any] = {}
    handler = object.__new__(Handler)
    handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
    method_name = operations[path]
    if path == "/api/upload":
        handler.headers = {"Content-Type": content_type, "Content-Length": str(len(raw_body))}
        handler.rfile = BytesIO(raw_body)
        handler.handle_upload()
    elif method_name:
        operation = getattr(handler, method_name)
        if method_name in {"handle_models", "handle_provider_get", "handle_system_prompts", "handle_cache_clear"}:
            operation()
        else:
            operation(body or {})
    return captured.get("payload", {"error": "empty legacy response"}), captured.get("status", 500)
