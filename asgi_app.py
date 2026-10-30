"""HTTP adapters for authentication, request scopes, SSE and application use cases.

Generation/document state transitions live in application_services; synchronous
RAG selection lives in question_service behind the protected compatibility bridge.
Keep domain rules out of route handlers and bind dependency overrides per request.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from opentelemetry.trace import format_trace_id, get_current_span
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from application_services import DocumentService, GenerationService, ServiceError
from api_openapi import install_openapi
from audit import record as audit_record
from db_store import (
    create_document,
    create_message,
    get_chat,
    get_document,
    list_documents,
    request_scope,
    tombstone_document,
    update_document,
    update_message,
)
from document_catalog import annotate_document_versions
from domain_routing import infer_document_domains
from jobs import JobQueue
from legacy_adapter import invoke_legacy
from local_auth import (
    SESSION_COOKIE_NAME,
    InvalidCredentials,
    RegistrationError,
    authenticate_credentials,
    authenticate_session,
    cookie_max_age,
    cookie_secure,
    create_session,
    csrf_token,
    public_user,
    register_user,
    revoke_session,
    verify_csrf,
)
from persistence import get_store
from security import AuthenticationError, AuthorizationError, Principal, RAG_ACCESS, authorize, configured_identity_provider
from telemetry import AUTH_DECISIONS, JOBS, PIPELINE_SECONDS, REQUESTS, REQUEST_SECONDS, configure_logging, configure_telemetry, tracer
from runtime_paths import UPLOAD_DIR


class AskRequest(BaseModel):
    """Validated question input; ownership is bound by authentication, never by client fields."""
    question: str = Field(min_length=1, max_length=20_000)
    chat_id: str | None = None
    retrieval_focus: str = Field(default="auto", pattern="^(auto|rre|orem|both)$")


class LoginRequest(BaseModel):
    """Credentials accepted by the local login endpoint; extra request fields are rejected."""
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class RegisterRequest(LoginRequest):
    """Local registration input extending credentials with the public display name."""
    display_name: str = Field(default="", max_length=80)


DOCUMENT_UPLOAD_SUFFIXES = {".docx", ".pdf", ".md", ".txt"}
IMAGE_UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
ALLOWED_UPLOAD_SUFFIXES = DOCUMENT_UPLOAD_SUFFIXES | IMAGE_UPLOAD_SUFFIXES
MAX_UPLOAD_BYTES = int(os.getenv("RAG_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_UPLOAD_FILES = int(os.getenv("RAG_MAX_UPLOAD_FILES", "20"))
GENERATION_WAIT_SECONDS = float(os.getenv("RAG_GENERATION_WAIT_SECONDS", "900"))
GLOBAL_DOCUMENT_MANAGEMENT_PERMISSION = "rag.global_documents.manage"


def safe_upload_name(filename: str) -> str:
    """Normalize an untrusted display filename without preserving client directory components."""
    name = Path(filename).name.strip() or "upload.bin"
    name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._ -]+", "_", name)
    return name[:180] or "upload.bin"


def run_ask(payload: dict | str) -> dict:
    """Run the current RAG endpoint through the protected compatibility bridge."""
    if isinstance(payload, str):
        payload = {"question": payload}
    request_payload = dict(payload)
    owner_subject = str(request_payload.pop("_owner_subject", "")).strip() or None
    owner_admin = bool(request_payload.pop("_owner_admin", False))
    with request_scope(owner_subject, is_admin=owner_admin):
        result, status = legacy_operation("POST", "/api/ask", request_payload)
    if status >= 400:
        raise RuntimeError(str(result.get("error", "RAG request failed")))
    return result


def run_ask_stream(payload: dict | str, on_delta) -> dict:
    """Emit a safe final delta until the active generator exposes tokens."""
    result = run_ask(payload)
    answer = result.get("answer")
    if isinstance(answer, str) and answer:
        on_delta(answer)
    return result


def qdrant_readiness_error() -> str | None:
    """Probe vector storage through the compatibility adapter without running a question."""
    from web_ui import Handler
    return Handler.qdrant_error()


def legacy_operation(method: str, path: str, body: dict | None = None, *, content_type: str = "", raw_body: bytes = b"") -> tuple[dict, int]:
    """Call the synchronous compatibility adapter with its existing payload/status contract."""
    return invoke_legacy(method, path, body, content_type=content_type, raw_body=raw_body)


def run_ingestion_job(payload: dict) -> dict:
    """Execute legacy ingestion and surface a failed result to the queue worker."""
    result, status = legacy_operation("POST", "/api/load", payload)
    if status >= 400:
        raise RuntimeError(str(result.get("error", "ingestion failed")))
    return result


def _local_auth_enabled() -> bool:
    """Select server-side cookie sessions only in the configured local authentication mode."""
    return os.getenv("RAG_AUTH_MODE", "development").strip().lower() == "local"


def _all_users_manage_global_documents() -> bool:
    """Read the explicit testing/deployment override for shared-library management."""
    return os.getenv("RAG_ALL_USERS_MANAGE_GLOBAL_DOCUMENTS", "false").strip().lower() == "true"


def can_manage_global_documents(principal: Principal) -> bool:
    """Evaluate shared-library management separately from ordinary RAG access."""
    return (
        _all_users_manage_global_documents()
        or "superadmin" in principal.roles
        or "*" in principal.permissions
        or GLOBAL_DOCUMENT_MANAGEMENT_PERMISSION in principal.permissions
    )


def session_user(user: dict) -> dict:
    """Project public account data plus the effective shared-library UI capability."""
    result = public_user(user)
    result["can_manage_global_documents"] = bool(result.get("is_admin")) or _all_users_manage_global_documents()
    return result


async def current_principal(request: Request) -> AsyncIterator[Principal]:
    """Authenticate, authorize RAG access and bind tenant context for the entire request."""
    try:
        if _local_auth_enabled():
            user = await asyncio.to_thread(authenticate_session, request.cookies.get(SESSION_COOKIE_NAME, ""))
            is_admin = bool(user.get("is_admin"))
            principal = Principal(
                subject=f"local:{user['id']}",
                roles=frozenset({"superadmin"}) if is_admin else frozenset({"user"}),
                permissions=frozenset({"*"}) if is_admin else frozenset({RAG_ACCESS}),
                attributes={
                    "user_id": str(user["id"]),
                    "email": str(user["email"]),
                    "display_name": str(user["display_name"]),
                    "is_admin": "true" if is_admin else "false",
                },
                provider="local",
            )
        else:
            principal = await asyncio.to_thread(
                configured_identity_provider().authenticate,
                request.headers.get("Authorization"),
            )
        principal = await asyncio.to_thread(get_store().enrich_principal, principal)
        authorize(principal, RAG_ACCESS, request.url.path, {"request": {"method": request.method}})
        request.state.principal = principal
        AUTH_DECISIONS.labels(RAG_ACCESS, "allow", principal.provider).inc()
        with request_scope(
            principal.subject,
            is_admin="superadmin" in principal.roles,
            can_manage_global_documents=can_manage_global_documents(principal),
        ):
            yield principal
    except (AuthenticationError, InvalidCredentials) as exc:
        AUTH_DECISIONS.labels(RAG_ACCESS, "unauthenticated", "unknown").inc()
        audit_record("authentication", request_id=request.headers.get("X-Request-ID"), outcome="denied")
        raise HTTPException(401, str(exc)) from exc
    except AuthorizationError as exc:
        AUTH_DECISIONS.labels(RAG_ACCESS, "deny", "unknown").inc()
        audit_record("authorization", request_id=request.headers.get("X-Request-ID"), principal=locals().get("principal"), outcome="denied", action=RAG_ACCESS)
        raise HTTPException(403, str(exc)) from exc


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Initialize telemetry and bootstrap authorization before serving requests; close the queue on exit."""
    configure_logging()
    configure_telemetry()
    bootstrap_subject = os.getenv("BOOTSTRAP_SUPERADMIN_SUBJECT", "").strip() or "rag-superadmin"
    await asyncio.to_thread(get_store().bootstrap_superadmin, bootstrap_subject)
    yield
    await jobs.close()


app = FastAPI(title="RAG service", version="0.1.0", lifespan=lifespan)
jobs = JobQueue()
_persistent_ask_tasks: set[asyncio.Task] = set()


def _forget_persistent_ask(task: asyncio.Task) -> None:
    """Release a completed owned task and consume errors after delivery disconnects."""
    _persistent_ask_tasks.discard(task)
    if not task.cancelled():
        task.exception()  # Consume detached failures after a client disconnect.


def _generation_service() -> GenerationService:
    """Bind request-time dependencies so ownership context and overrides stay local."""
    return GenerationService(
        get_chat=get_chat, create_message=create_message, update_message=update_message,
        run_ask=run_ask, queue=jobs, tracked_tasks=_persistent_ask_tasks,
        forget_task=_forget_persistent_ask,
        durable=os.getenv("RAG_DURABLE_GENERATION", "false").lower() == "true",
        wait_seconds=GENERATION_WAIT_SECONDS,
    )


def _document_service() -> DocumentService:
    """Assemble the document use case without capturing mutable globals at import."""
    return DocumentService(
        create_document=create_document, update_document=update_document,
        get_document=get_document, tombstone_document=tombstone_document,
        infer_domains=infer_document_domains, safe_name=safe_upload_name,
        queue=jobs, upload_dir=UPLOAD_DIR, allowed_suffixes=ALLOWED_UPLOAD_SUFFIXES,
        max_bytes=MAX_UPLOAD_BYTES, max_files=MAX_UPLOAD_FILES,
    )


async def _run_and_persist_ask(request_payload: dict, assistant_message: dict) -> dict:
    """Compatibility seam for callers that persist a server-owned generation."""
    return await _generation_service().persist(request_payload, assistant_message)
cors_origins = [origin.strip() for origin in os.getenv("RAG_CORS_ORIGINS", "http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"], expose_headers=["X-Request-ID", "X-Trace-ID"])


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    """Require the session-bound CSRF token on authenticated cookie-based API mutations."""
    if (
        _local_auth_enabled()
        and request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and request.url.path.startswith("/api/")
        and request.url.path not in {"/api/auth/login", "/api/auth/register"}
    ):
        session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
        if session_token and not verify_csrf(session_token, request.headers.get("X-CSRF-Token", "")):
            return JSONResponse({"detail": "Проверка безопасности сессии не пройдена. Обновите страницу."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def request_telemetry(request: Request, call_next):
    """Record transport metadata and correlation IDs without logging document or answer content."""
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
    with tracer().start_as_current_span(f"{request.method} {request.url.path}"):
        response = await call_next(request)
        span = get_current_span()
        span_context = span.get_span_context()
        response.headers["X-Trace-ID"] = (
            format_trace_id(span_context.trace_id) if span_context.is_valid else uuid.uuid4().hex
        )
    route = request.scope.get("route")
    route_name = getattr(route, "path", request.url.path)
    REQUEST_SECONDS.labels(route_name, request.method).observe(time.perf_counter() - started)
    REQUESTS.labels(route_name, request.method, str(response.status_code)).inc()
    response.headers["X-Request-ID"] = request_id
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        audit_record(
            "api_mutation", request_id=request_id, principal=getattr(request.state, "principal", None),
            outcome="success" if response.status_code < 400 else "failed", path=route_name, status=response.status_code,
        )
    return response


@app.get("/healthz", include_in_schema=False)
async def healthz():
    """Return process liveness without requiring external dependencies."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    # Dependency readiness remains explicit so deployments do not accidentally
    # advertise readiness before Qdrant is reachable.
    """Require configured vector, relational and queue dependencies before reporting readiness."""
    problem = await asyncio.to_thread(qdrant_readiness_error)
    if problem:
        raise HTTPException(503, problem)
    try:
        await asyncio.to_thread(get_store().ping)
    except Exception as exc:
        raise HTTPException(503, "PostgreSQL is unavailable") from exc
    if os.getenv("RAG_REDIS_REQUIRED", "false").lower() == "true":
        try:
            await jobs.ping()
        except Exception as exc:
            raise HTTPException(503, "Redis is unavailable") from exc
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Expose the Prometheus registry for internal observability."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _session_response(user: dict, session_token: str, *, status_code: int = 200) -> JSONResponse:
    """Set the HTTP-only session cookie and return public account/CSRF information."""
    response = JSONResponse(
        jsonable_encoder({"authenticated": True, "user": session_user(user), "csrf_token": csrf_token(session_token)}),
        status_code=status_code,
    )
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_token,
        max_age=cookie_max_age(),
        httponly=True,
        secure=cookie_secure(),
        samesite="lax",
        path="/",
    )
    return response


@app.get("/api/auth/session")
async def auth_session(request: Request):
    """Restore a valid local session or expose development bootstrap identity."""
    if not _local_auth_enabled():
        return {
            "authenticated": True,
            "user": {
                "id": "development",
                "email": "",
                "display_name": "Development",
                "is_admin": True,
                "can_manage_global_documents": True,
            },
            "csrf_token": None,
        }
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    try:
        user = await asyncio.to_thread(authenticate_session, session_token)
    except InvalidCredentials:
        response = JSONResponse({"authenticated": False, "user": None, "csrf_token": None})
        if session_token:
            response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=cookie_secure(), samesite="lax")
        return response
    return _session_response(user, session_token)


@app.post("/api/auth/register", status_code=201)
async def auth_register(payload: RegisterRequest, request: Request):
    """Register an account through local_auth and issue its initial server session."""
    try:
        user = await asyncio.to_thread(register_user, payload.email, payload.display_name, payload.password)
    except RegistrationError as exc:
        raise HTTPException(400, str(exc)) from exc
    session_token = await asyncio.to_thread(
        create_session,
        str(user["id"]),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else None,
    )
    audit_record("registration", outcome="success", subject=f"local:{user['id']}")
    return _session_response(user, session_token, status_code=201)


@app.post("/api/auth/login")
async def auth_login(payload: LoginRequest, request: Request):
    """Verify local credentials and issue a new server session."""
    try:
        user = await asyncio.to_thread(authenticate_credentials, payload.email, payload.password)
    except InvalidCredentials as exc:
        audit_record("authentication", outcome="denied")
        raise HTTPException(401, str(exc)) from exc
    session_token = await asyncio.to_thread(
        create_session,
        str(user["id"]),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else None,
    )
    audit_record("authentication", outcome="success", subject=f"local:{user['id']}")
    return _session_response(user, session_token)


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    """Revoke the current server session and expire its browser cookie."""
    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    await asyncio.to_thread(revoke_session, session_token)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", secure=cookie_secure(), samesite="lax")
    return response


@app.get("/api/me")
async def me(principal: Principal = Depends(current_principal)):
    """Return the effective authenticated identity and capabilities, never credential material."""
    return {
        "subject": principal.subject,
        "roles": sorted(principal.roles),
        "permissions": sorted(principal.permissions),
        "provider": principal.provider,
        "user": {
            "id": principal.attributes.get("user_id"),
            "email": principal.attributes.get("email"),
            "display_name": principal.attributes.get("display_name"),
            "is_admin": principal.attributes.get("is_admin") == "true",
            "can_manage_global_documents": can_manage_global_documents(principal),
        } if principal.provider == "local" else None,
    }


def require_admin(principal: Principal, path: str) -> None:
    """Authorize an administrative action using the effective principal permissions."""
    authorize(principal, "rag.admin", path)


@app.post("/api/admin/policies")
async def save_policy(payload: dict, principal: Principal = Depends(current_principal)):
    """Validate the policy envelope before delegating persistence to the authorization store."""
    require_admin(principal, "/api/admin/policies")
    if payload.get("effect") not in {"allow", "deny"} or not payload.get("name") or not payload.get("action"):
        raise HTTPException(400, "name, action and allow/deny effect are required")
    return await asyncio.to_thread(get_store().upsert_policy, payload)


@app.post("/api/admin/principals/{subject}/roles")
async def grant_principal_role(subject: str, payload: dict, principal: Principal = Depends(current_principal)):
    """Authorize role administration before recording the requested principal grant."""
    require_admin(principal, "/api/admin/principals/*/roles")
    role = str(payload.get("role", "")).strip()
    permissions = [str(item) for item in payload.get("permissions", [])]
    if not role: raise HTTPException(400, "role is required")
    await asyncio.to_thread(get_store().grant_role, subject, role, permissions)
    return {"subject": subject, "role": role, "permissions": permissions}


@app.post("/api/ask")
async def ask(payload: AskRequest, principal: Principal = Depends(current_principal)):
    """Deliver an answer or a pollable job; use cases own publication and retries."""
    with tracer().start_as_current_span("rag.ask") as span:
        span.set_attribute("rag.principal_provider", principal.provider)
        started = time.perf_counter()
        try:
            result = await _generation_service().execute(
                payload.model_dump(exclude_none=True, exclude_defaults=True),
                principal.subject, "superadmin" in principal.roles,
            )
        except ServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        PIPELINE_SECONDS.labels("ask", result.get("flow_mode", "unknown")).observe(time.perf_counter() - started)
        return JSONResponse(jsonable_encoder(result), status_code=202 if result.get("status") == "running" else 200)


def _sse(event: str, data: dict) -> str:
    """Encode one SSE event; payload encoding matches ordinary JSON responses."""
    encoded = jsonable_encoder(data)
    return f"event: {event}\ndata: {json.dumps(encoded, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.post("/api/ask/stream")
async def ask_stream(payload: AskRequest, request: Request, principal: Principal = Depends(current_principal)):
    """Encode generation progress as SSE without duplicating lifecycle rules."""
    service = _generation_service()
    try:
        prepared = await service.prepare(
            payload.model_dump(exclude_none=True, exclude_defaults=True),
            principal.subject, "superadmin" in principal.roles,
        )
    except ServiceError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    async def events():
        """Encode transport-neutral generation events as SSE frames."""
        async for event, data in service.stream_events(prepared, principal.subject, request.is_disconnected, run_ask_stream):
            yield _sse(event, data)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/ingestion")
async def submit_ingestion(payload: dict, principal: Principal = Depends(current_principal)):
    """Start long ingestion without holding an HTTP connection open."""
    try:
        job = await jobs.submit("ingestion", {**payload, "owner_subject": principal.subject})
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    JOBS.labels("ingestion", "queued").inc()
    return JSONResponse(job, status_code=202)


@app.post("/api/upload")
async def upload_documents(
    files: list[UploadFile] = File(...),
    project_id: str = Form(""),
    chat_id: str = Form(""),
    principal: Principal = Depends(current_principal),
):
    """Adapt multipart uploads to the document registration/ingestion use case."""
    try:
        result = await _document_service().upload(
            files, project_id, chat_id, principal.subject, can_manage_global_documents(principal),
        )
    except ServiceError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return JSONResponse(result, status_code=202)


@app.get("/api/projects/{project_id}/documents")
async def project_documents(project_id: str, _: Principal = Depends(current_principal)):
    """List authorized project documents and annotate their known version relationships."""
    items = await asyncio.to_thread(list_documents, project_id, None)
    return {"items": annotate_document_versions(items)}


@app.get("/api/documents")
async def documents(project_id: str = "", chat_id: str = "", _: Principal = Depends(current_principal)):
    """List the requested visible document scope with version annotations."""
    items = await asyncio.to_thread(list_documents, project_id.strip() or None, chat_id.strip() or None)
    return {"items": annotate_document_versions(items)}


@app.get("/api/documents/{document_id}/content")
async def document_content(document_id: str, _: Principal = Depends(current_principal)):
    """Send the authorized original after service-side path containment checks."""
    try:
        path, media_type, filename = await _document_service().content(document_id)
    except ServiceError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    return FileResponse(path, media_type=media_type, filename=filename)


@app.delete("/api/documents/{document_id}")
async def delete_document(document_id: str, principal: Principal = Depends(current_principal)):
    """Accept deletion after the service hides the source and queues cleanup."""
    try:
        result = await _document_service().delete(
            document_id, principal.subject, can_manage_global_documents(principal),
        )
    except ServiceError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    JOBS.labels("deletion", "queued").inc()
    return JSONResponse(result, status_code=202)


@app.get("/api/openapi.json", include_in_schema=False)
async def api_openapi(_: Principal = Depends(current_principal)):
    """Expose the live FastAPI contract instead of the legacy static snapshot."""
    return app.openapi()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, principal: Principal = Depends(current_principal)):
    """Return a job only when its owner or an administrator may access it."""
    if not await jobs.can_access(job_id, principal.subject, "superadmin" in principal.roles):
        raise HTTPException(404, "job not found")
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] in {"completed", "failed", "cancelled"}:
        JOBS.labels(job["kind"], job["status"]).inc()
    return job


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str, principal: Principal = Depends(current_principal)):
    """Request cancellation only for an accessible, cancellable background job."""
    if not await jobs.can_access(job_id, principal.subject, "superadmin" in principal.roles):
        raise HTTPException(404, "job not found")
    if not await jobs.cancel(job_id):
        raise HTTPException(409, "job cannot be cancelled")
    return {"id": job_id, "status": "cancelled"}


@app.api_route(
    "/api/{operation:path}",
    methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    include_in_schema=False,
)
async def legacy_api(operation: str, request: Request, principal: Principal = Depends(current_principal)):
    """Protected async facade for remaining legacy API endpoints."""
    path = f"/api/{operation}"
    raw_body = await request.body()
    body: dict | None = None
    content_type = request.headers.get("content-type", "")
    if request.method in {"POST", "PATCH", "PUT", "DELETE"} and "multipart/form-data" not in content_type:
        try:
            body = await request.json() if raw_body else {}
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON") from exc
    def invoke_scoped_legacy_operation() -> tuple[dict, int]:
        """Restore trusted ownership when the compatibility call enters a worker thread."""
        with request_scope(principal.subject, is_admin="superadmin" in principal.roles):
            return legacy_operation(
                request.method,
                path,
                body,
                content_type=content_type,
                raw_body=raw_body,
            )

    payload, status = await asyncio.to_thread(invoke_scoped_legacy_operation)
    return JSONResponse(jsonable_encoder(payload), status_code=status)


# Register the documentation only after all native and compatibility routes exist.
install_openapi(app, legacy_spec_path=Path(__file__).with_name("openapi.json"), session_cookie_name=SESSION_COOKIE_NAME)
