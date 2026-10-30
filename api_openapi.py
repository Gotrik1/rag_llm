"""Build the public contract from native routes and the protected legacy facade.

FastAPI cannot discover paths dispatched inside ``legacy_api``. Only those
explicitly supported compatibility operations are merged from ``openapi.json``;
native routes always win. This module changes documentation, never validation,
authorization, response filtering or the execution path of an HTTP request.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options", "trace"})
# The wildcard route is intentionally not itself documented as an arbitrary API.
# Keep this list aligned with Handler.do_GET/POST/PATCH/DELETE and test it.
LEGACY_OPERATIONS: dict[str, tuple[str, ...]] = {
    "/api/workspace": ("get",),
    "/api/settings": ("get", "post"),
    "/api/projects": ("get", "post"),
    "/api/projects/{project_id}": ("get", "patch", "delete"),
    "/api/projects/{project_id}/chats": ("get", "post"),
    "/api/chats": ("get", "post"),
    "/api/chats/{chat_id}": ("get", "patch", "delete"),
    "/api/chats/{chat_id}/messages": ("get", "post"),
    "/api/messages/{message_id}": ("get", "patch", "delete"),
    "/api/debug": ("post",),
    "/api/load": ("post",),
    "/api/cache/clear": ("post",),
    "/api/models": ("get",),
    "/api/models/select": ("post",),
    "/api/providers": ("get", "post"),
    "/api/providers/test": ("post",),
    "/api/system-prompts": ("get",),
    "/api/system-prompts/select": ("post",),
    "/api/flow-mode": ("get", "post"),
}
PUBLIC_API_OPERATIONS = frozenset({
    ("/api/auth/session", "get"), ("/api/auth/register", "post"),
    ("/api/auth/login", "post"), ("/api/auth/logout", "post"),
})


def _object(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Describe stable fields without forbidding additive diagnostic metadata."""
    value: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        value["required"] = list(required)
    return value


def _ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _json_response(name: str, description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _ref(name)}}}


def _current_schemas() -> dict[str, Any]:
    """Stable response fields; optional fields reflect different configured flows."""
    string = {"type": "string"}
    nullable_string = {"type": ["string", "null"]}
    json_object = {"type": "object", "additionalProperties": True}
    return {
        "DetailError": _object({"detail": {}}, ("detail",)),
        "ApiFailure": {"anyOf": [_ref("DetailError"), _ref("Error")]},
        "SessionUser": _object({
            "id": string, "email": string, "display_name": string,
            "is_admin": {"type": "boolean"}, "can_manage_global_documents": {"type": "boolean"},
        }, ("id", "email", "display_name", "is_admin", "can_manage_global_documents")),
        "SessionResponse": _object({
            "authenticated": {"type": "boolean"},
            "user": {"anyOf": [_ref("SessionUser"), {"type": "null"}]},
            "csrf_token": nullable_string,
        }, ("authenticated", "user", "csrf_token")),
        "LogoutResponse": _object({"authenticated": {"type": "boolean", "const": False}}, ("authenticated",)),
        "PrincipalResponse": _object({
            "subject": string, "roles": {"type": "array", "items": string},
            "permissions": {"type": "array", "items": string}, "provider": string,
            "user": {"anyOf": [_ref("SessionUser"), {"type": "null"}]},
        }, ("subject", "roles", "permissions", "provider", "user")),
        "AskResponse": _object({
            "answer": string, "html_answer": string, "message_id": string,
            "sources": {"type": "array", "items": _ref("Source")},
            "context": {"type": "array", "items": json_object},
            "formulas": {"type": "array", "items": _ref("Formula")},
            "warnings": {"type": "array", "items": string}, "from_cache": {"type": "boolean"},
            "flow_mode": {"type": "string", "description": "Actual answer path, including workspace/catalogue flows."},
            "llm": json_object, "usage": json_object, "retrieval_trace": json_object,
            "documents": {"type": "array", "items": json_object}, "elapsed_s": {"type": "number"},
        }, ("answer",)),
        "GenerationPending": _object({
            "answer": {"type": "string", "const": ""}, "status": {"type": "string", "const": "running"},
            "job_id": string, "message_id": string, "flow_mode": {"type": "string", "const": "queued"},
            "from_cache": {"type": "boolean"}, "elapsed_s": {"type": "number"},
        }, ("answer", "status", "job_id", "message_id")),
        "Job": _object({
            "id": string, "kind": {"type": "string", "enum": ["generation", "ingestion", "deletion"]},
            "status": {"type": "string", "enum": ["queued", "running", "completed", "failed", "cancelled"]},
            "progress": {"type": "integer"}, "cancel_requested": {"type": "boolean"},
            "created_at": {"type": "number"}, "started_at": {"type": ["number", "null"]},
            "finished_at": {"type": ["number", "null"]}, "result": {"anyOf": [json_object, {"type": "null"}]},
            "error": nullable_string,
        }, ("id", "kind", "status", "progress", "created_at")),
        "Document": _object({
            "id": string, "file_name": string, "status": string, "scope": string,
            "project_id": nullable_string, "chat_id": nullable_string, "metadata": json_object,
            "processing_state": json_object, "version_label": nullable_string,
            "uploaded_at": {"type": ["string", "null"], "format": "date-time"},
        }, ("id", "file_name", "status")),
        "DocumentList": _object({"items": {"type": "array", "items": _ref("Document")}}, ("items",)),
        "QueuedUpload": _object({
            "id": string, "file": string, "size_bytes": {"type": "integer"},
            "status": {"type": "string", "const": "queued"}, "job_id": string, "url": string,
        }, ("id", "file", "size_bytes", "status", "job_id", "url")),
        "UploadResponse": _object({
            "files": {"type": "array", "items": _ref("QueuedUpload")},
            "chunks": {"type": "integer", "const": 0}, "queued": {"type": "boolean", "const": True},
        }, ("files", "chunks", "queued")),
        "DocumentDeleting": _object({"id": string, "status": {"type": "string", "const": "deleting"}, "job_id": string}, ("id", "status", "job_id")),
        "JobCancelled": _object({"id": string, "status": {"type": "string", "const": "cancelled"}}, ("id", "status")),
        "StatusResponse": _object({"status": string}, ("status",)),
        "PolicyRequest": _object({
            "name": string, "action": string, "effect": {"type": "string", "enum": ["allow", "deny"]},
            "resource": string, "conditions": json_object,
        }, ("name", "action", "effect")),
        "RoleGrantRequest": _object({"role": string, "permissions": {"type": "array", "items": string}}, ("role",)),
        "RoleGrantResponse": _object({"subject": string, "role": string, "permissions": {"type": "array", "items": string}}, ("subject", "role", "permissions")),
    }


def _decorate_operations(spec: dict[str, Any], session_cookie_name: str) -> None:
    """Document conditional cookie/CSRF authentication and both error envelopes."""
    components = spec.setdefault("components", {})
    components.setdefault("securitySchemes", {}).update({
        "SessionCookie": {"type": "apiKey", "in": "cookie", "name": session_cookie_name,
                          "description": "Local mode: sign in through /api/auth/login; the browser stores the HttpOnly cookie."},
        "BearerIdentity": {"type": "http", "scheme": "bearer",
                           "description": "Only when the configured identity provider accepts bearer credentials. Local mode uses SessionCookie instead."},
    })
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in HTTP_METHODS:
                continue
            if path.startswith("/api/"):
                public = (path, method) in PUBLIC_API_OPERATIONS
                operation["security"] = [] if public else [{"SessionCookie": []}, {"BearerIdentity": []}]
                responses = operation.setdefault("responses", {})
                if path in LEGACY_OPERATIONS and method in {"post", "put", "patch", "delete"}:
                    responses.setdefault("400", _json_response("ApiFailure", "The compatibility facade could not parse the JSON request."))
                # A facade operation can fail before dispatch (HTTPException /
                # invalid JSON) or inside Handler (the legacy error envelope).
                for status, response in responses.items():
                    if status.isdigit() and int(status) >= 400 and status != "422":
                        if "$ref" in response:
                            responses[status] = _json_response("ApiFailure", "Request could not be completed.")
                        elif "application/json" in response.get("content", {}):
                            response["content"]["application/json"]["schema"] = _ref("ApiFailure")
                if not public:
                    responses.setdefault("401", _json_response("ApiFailure", "Authentication required."))
                    responses.setdefault("403", _json_response("ApiFailure", "Permission denied or local session CSRF check failed."))
                if method in {"post", "put", "patch", "delete"} and path not in {"/api/auth/login", "/api/auth/register"}:
                    operation.setdefault("parameters", []).append({
                        "in": "header", "name": "X-CSRF-Token", "required": False,
                        "schema": {"type": "string"},
                        "description": "Required for mutations when a local session cookie is present. Obtain the token from /api/auth/session. Not required for a bearer identity provider.",
                    })
                    responses.setdefault("403", _json_response("ApiFailure", "Permission denied or local session CSRF check failed."))
            else:
                operation["security"] = []


def build_openapi(app: FastAPI, *, legacy_spec_path: Path, session_cookie_name: str = "rag_session") -> dict[str, Any]:
    """Return one cached contract for Swagger and both OpenAPI URLs.

    The compatibility file is required input. Failing explicitly on a missing
    snapshot is safer than silently publishing an incomplete integration API.
    """
    if app.openapi_schema is not None:
        return app.openapi_schema
    legacy = json.loads(legacy_spec_path.read_text(encoding="utf-8"))
    native = get_openapi(title=app.title, version=app.version, routes=app.routes, openapi_version=app.openapi_version)
    components = deepcopy(legacy.get("components", {}))
    for category, values in native.get("components", {}).items():
        components.setdefault(category, {}).update(values)
    native["components"] = components
    for path, methods in LEGACY_OPERATIONS.items():
        if path in native["paths"]:
            continue
        documented = legacy.get("paths", {}).get(path)
        if not isinstance(documented, dict) or any(method not in documented for method in methods):
            raise RuntimeError(f"Compatibility OpenAPI contract is missing {path}")
        native["paths"][path] = {key: deepcopy(value) for key, value in documented.items() if key in methods or key == "parameters"}
    schemas = components.setdefault("schemas", {})
    schemas.update(_current_schemas())
    # A section synthesized by retrieval need not have a numeric ranking score.
    schemas["Source"]["required"] = ["file"]
    schemas["Source"]["properties"]["score"] = {"type": ["number", "null"], "description": "Optional retriever score; null means no numeric score was supplied."}
    schemas["ContextItem"]["required"] = [field for field in schemas["ContextItem"].get("required", []) if field != "score"]
    schemas["ContextItem"]["properties"]["score"] = deepcopy(schemas["Source"]["properties"]["score"])
    schemas["QuestionRequest"]["required"] = ["question"]
    schemas["QuestionRequest"]["properties"]["chat_id"]["description"] = "Optional chat scope; the legacy debug operation otherwise uses the active workspace chat."
    schemas["MessageCreate"].pop("required", None)
    schemas["MessageCreate"]["properties"]["role"]["default"] = "user"
    schemas["MessageCreate"]["properties"]["content"]["default"] = ""
    schemas["MessageCreate"]["properties"]["status"]["description"] = "Message state, not a job state. Omit for a completed user message or use complete; completed is a job status and is not the conversation-history state."
    schemas["ProjectCreate"]["properties"]["knowledge_scope"] = {"type": "string", "enum": ["project_only", "project_and_global"], "default": "project_and_global"}
    schemas["ProjectUpdate"]["properties"]["knowledge_scope"] = deepcopy(schemas["ProjectCreate"]["properties"]["knowledge_scope"])
    schemas["Project"]["properties"]["knowledge_scope"] = deepcopy(schemas["ProjectCreate"]["properties"]["knowledge_scope"])
    # Free chats are supported; project_id is not mandatory for POST /api/chats.
    schemas["ChatCreateWithProject"] = {"allOf": [_ref("ChatCreate"), _object({"project_id": {"type": ["string", "null"], "format": "uuid"}})]}
    native["info"]["description"] = (
        "Live RAG API. Native FastAPI routes and explicitly documented compatibility operations share authentication and request scope. "
        "In local mode use login/session cookies plus X-CSRF-Token for mutations; an alternate configured identity provider may accept bearer credentials. "
        "A documented authentication mode is not an instruction to enable it. Projects, chats and messages use PATCH and soft DELETE. "
        "Answer generation can complete with HTTP 200 or remain queued with HTTP 202."
    )
    native["tags"] = deepcopy(legacy.get("tags", []))
    paths = native["paths"]
    completed = _json_response("AskResponse", "Completed answer. Optional diagnostics depend on the selected flow.")
    paths["/api/ask"]["post"]["responses"]["200"] = completed
    paths["/api/ask"]["post"]["responses"]["202"] = _json_response("GenerationPending", "Generation continues on the server. Poll /api/jobs/{job_id} or reload persisted chat messages.")
    paths["/api/ask"]["post"]["responses"]["500"] = {"description": "Generation failed.", "content": {"application/json": {"schema": _ref("ApiFailure")}, "text/plain": {"schema": {"type": "string"}}}}
    stream = paths["/api/ask/stream"]["post"]
    stream["responses"]["200"] = {"description": "Server-sent events: started, heartbeat, delta (when available), completed, error. An error event after headers still has HTTP 200. Durable jobs persist after disconnect; the non-durable stream is not resumable.", "content": {"text/event-stream": {"schema": {"type": "string"}, "example": 'event: started\ndata: {"flow":"pending"}\n\nevent: completed\ndata: {"answer":"Ответ"}\n\n'}}}
    stream["x-sse-events"] = ["started", "heartbeat", "delta", "completed", "error"]
    contracts = {
        ("/api/auth/session", "get"): ("200", "SessionResponse"),
        ("/api/auth/register", "post"): ("201", "SessionResponse"),
        ("/api/auth/login", "post"): ("200", "SessionResponse"),
        ("/api/auth/logout", "post"): ("200", "LogoutResponse"),
        ("/api/me", "get"): ("200", "PrincipalResponse"),
        ("/api/upload", "post"): ("202", "UploadResponse"),
        ("/api/jobs/ingestion", "post"): ("202", "Job"),
        ("/api/jobs/{job_id}", "get"): ("200", "Job"),
        ("/api/jobs/{job_id}", "delete"): ("200", "JobCancelled"),
        ("/api/documents", "get"): ("200", "DocumentList"),
        ("/api/projects/{project_id}/documents", "get"): ("200", "DocumentList"),
        ("/api/documents/{document_id}", "delete"): ("202", "DocumentDeleting"),
    }
    for (path, method), (status, schema_name) in contracts.items():
        responses = paths[path][method]["responses"]
        if status == "202":
            responses.pop("200", None)
        responses[status] = _json_response(schema_name, "Accepted for background processing." if status == "202" else "Successful response.")
    paths["/api/documents/{document_id}/content"]["get"]["responses"]["200"] = {
        "description": "Original file, served with its recorded MIME type and Content-Disposition filename.",
        "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in ("application/octet-stream", "*/*")},
    }
    for path, schema_name in (("/api/admin/policies", "PolicyRequest"), ("/api/admin/principals/{subject}/roles", "RoleGrantRequest")):
        operation = paths[path]["post"]
        operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": _ref(schema_name)}}}
        operation["description"] = "Requires the rag.admin permission; declaring a role in the request never grants caller privileges."
    paths["/api/admin/principals/{subject}/roles"]["post"]["responses"]["200"] = _json_response("RoleGrantResponse", "Role grant recorded.")
    extra_errors = {
        ("/api/auth/register", "post"): [400], ("/api/auth/login", "post"): [401],
        ("/api/ask", "post"): [400, 404, 503], ("/api/ask/stream", "post"): [400, 404],
        ("/api/upload", "post"): [400, 413, 415, 503],
        ("/api/jobs/ingestion", "post"): [503], ("/api/jobs/{job_id}", "get"): [404],
        ("/api/jobs/{job_id}", "delete"): [404, 409],
        ("/api/documents/{document_id}/content", "get"): [404],
        ("/api/documents/{document_id}", "delete"): [404, 409, 503],
        ("/api/debug", "post"): [404, 409],
        ("/api/admin/policies", "post"): [400],
        ("/api/admin/principals/{subject}/roles", "post"): [400],
    }
    for (path, method), codes in extra_errors.items():
        for code in codes:
            paths[path][method]["responses"].setdefault(str(code), _json_response("ApiFailure", "Request could not be completed."))
    _decorate_operations(native, session_cookie_name)
    app.openapi_schema = native
    return native


def install_openapi(app: FastAPI, *, legacy_spec_path: Path, session_cookie_name: str = "rag_session") -> None:
    """Install after route registration so schema construction sees every route."""
    app.openapi_schema = None
    app.openapi = lambda: build_openapi(app, legacy_spec_path=legacy_spec_path, session_cookie_name=session_cookie_name)
