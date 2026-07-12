"""ASGI entry point for the incremental RAG service migration."""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from opentelemetry.trace import format_trace_id, get_current_span
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from audit import record as audit_record
from jobs import JobQueue
from legacy_adapter import invoke_legacy
from persistence import get_store
from security import AuthenticationError, AuthorizationError, Principal, RAG_ACCESS, authorize, configured_identity_provider
from telemetry import AUTH_DECISIONS, JOBS, PIPELINE_SECONDS, REQUESTS, REQUEST_SECONDS, configure_logging, configure_telemetry, tracer


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)


def run_ask(question: str) -> dict:
    """Lazy import keeps health/auth endpoints available during dependency outages."""
    from web_ui import ask_payload
    return ask_payload(question)


def run_ask_stream(question: str, on_delta) -> dict:
    from web_ui import ask_payload
    return ask_payload(question, on_delta)


def qdrant_readiness_error() -> str | None:
    from web_ui import Handler
    return Handler.qdrant_error()


def legacy_operation(method: str, path: str, body: dict | None = None, *, content_type: str = "", raw_body: bytes = b"") -> tuple[dict, int]:
    return invoke_legacy(method, path, body, content_type=content_type, raw_body=raw_body)


def run_ingestion_job(payload: dict) -> dict:
    result, status = legacy_operation("POST", "/api/load", payload)
    if status >= 400:
        raise RuntimeError(str(result.get("error", "ingestion failed")))
    return result


def current_principal(request: Request) -> Principal:
    try:
        principal = configured_identity_provider().authenticate(request.headers.get("Authorization"))
        principal = get_store().enrich_principal(principal)
        authorize(principal, RAG_ACCESS, request.url.path, {"request": {"method": request.method}})
        request.state.principal = principal
        AUTH_DECISIONS.labels(RAG_ACCESS, "allow", principal.provider).inc()
        return principal
    except AuthenticationError as exc:
        AUTH_DECISIONS.labels(RAG_ACCESS, "unauthenticated", "unknown").inc()
        audit_record("authentication", request_id=request.headers.get("X-Request-ID"), outcome="denied")
        raise HTTPException(401, str(exc)) from exc
    except AuthorizationError as exc:
        AUTH_DECISIONS.labels(RAG_ACCESS, "deny", "unknown").inc()
        audit_record("authorization", request_id=request.headers.get("X-Request-ID"), principal=locals().get("principal"), outcome="denied", action=RAG_ACCESS)
        raise HTTPException(403, str(exc)) from exc


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    configure_telemetry()
    await asyncio.to_thread(get_store().bootstrap_superadmin, os.getenv("BOOTSTRAP_SUPERADMIN_SUBJECT", "rag-superadmin"))
    yield
    await jobs.close()


app = FastAPI(title="RAG service", version="0.1.0", lifespan=lifespan)
jobs = JobQueue()
cors_origins = [origin.strip() for origin in os.getenv("RAG_CORS_ORIGINS", "http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"], expose_headers=["X-Request-ID", "X-Trace-ID"])


@app.middleware("http")
async def request_telemetry(request: Request, call_next):
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
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz():
    # Dependency readiness remains explicit so deployments do not accidentally
    # advertise readiness before Qdrant is reachable.
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
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/me")
async def me(principal: Principal = Depends(current_principal)):
    return {"subject": principal.subject, "roles": sorted(principal.roles), "permissions": sorted(principal.permissions), "provider": principal.provider}


def require_admin(principal: Principal, path: str) -> None:
    authorize(principal, "rag.admin", path)


@app.post("/api/admin/policies")
async def save_policy(payload: dict, principal: Principal = Depends(current_principal)):
    require_admin(principal, "/api/admin/policies")
    if payload.get("effect") not in {"allow", "deny"} or not payload.get("name") or not payload.get("action"):
        raise HTTPException(400, "name, action and allow/deny effect are required")
    return await asyncio.to_thread(get_store().upsert_policy, payload)


@app.post("/api/admin/principals/{subject}/roles")
async def grant_principal_role(subject: str, payload: dict, principal: Principal = Depends(current_principal)):
    require_admin(principal, "/api/admin/principals/*/roles")
    role = str(payload.get("role", "")).strip()
    permissions = [str(item) for item in payload.get("permissions", [])]
    if not role: raise HTTPException(400, "role is required")
    await asyncio.to_thread(get_store().grant_role, subject, role, permissions)
    return {"subject": subject, "role": role, "permissions": permissions}


@app.post("/api/ask")
async def ask(payload: AskRequest, principal: Principal = Depends(current_principal)):
    question = payload.question.strip()
    if not question:
        raise HTTPException(400, "empty question")
    # Current RAG/Qdrant/LLM SDKs are synchronous. Isolating them in a worker
    # thread keeps the event loop responsive without falsely marking blocking IO as async.
    with tracer().start_as_current_span("rag.ask") as span:
        span.set_attribute("rag.principal_provider", principal.provider)
        started = time.perf_counter()
        result = await asyncio.to_thread(run_ask, question)
        PIPELINE_SECONDS.labels("ask", result["flow_mode"]).observe(time.perf_counter() - started)
        return JSONResponse(result)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.post("/api/ask/stream")
async def ask_stream(payload: AskRequest, request: Request, principal: Principal = Depends(current_principal)):
    """SSE contract with immediate progress, heartbeats and incremental answer chunks."""
    async def events():
        yield _sse("started", {"flow": "pending"})
        loop = asyncio.get_running_loop()
        deltas: asyncio.Queue[str] = asyncio.Queue()
        callback = lambda text: loop.call_soon_threadsafe(deltas.put_nowait, text)
        task = asyncio.create_task(asyncio.to_thread(run_ask_stream, payload.question.strip(), callback))
        while not task.done() or not deltas.empty():
            if await request.is_disconnected():
                task.cancel()
                return
            try:
                delta = await asyncio.wait_for(deltas.get(), timeout=10)
                yield _sse("delta", {"text": delta})
            except TimeoutError:
                yield _sse("heartbeat", {"status": "running"})
        try:
            result = task.result()
            result.pop("html_answer", None)
            yield _sse("completed", result)
        except Exception:
            yield _sse("error", {"message": "RAG generation failed"})
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/jobs/ingestion")
async def submit_ingestion(payload: dict, _: Principal = Depends(current_principal)):
    """Start long ingestion without holding an HTTP connection open."""
    try:
        job = await jobs.submit("ingestion", payload)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    JOBS.labels("ingestion", "queued").inc()
    return JSONResponse(job, status_code=202)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, _: Principal = Depends(current_principal)):
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job["status"] in {"completed", "failed", "cancelled"}:
        JOBS.labels(job["kind"], job["status"]).inc()
    return job


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str, _: Principal = Depends(current_principal)):
    if not await jobs.cancel(job_id):
        raise HTTPException(409, "job cannot be cancelled")
    return {"id": job_id, "status": "cancelled"}


@app.api_route("/api/{operation:path}", methods=["GET", "POST"])
async def legacy_api(operation: str, request: Request, _: Principal = Depends(current_principal)):
    """Protected async facade for remaining legacy API endpoints."""
    path = f"/api/{operation}"
    raw_body = await request.body()
    body: dict | None = None
    content_type = request.headers.get("content-type", "")
    if request.method == "POST" and "multipart/form-data" not in content_type:
        try:
            body = await request.json() if raw_body else {}
        except ValueError as exc:
            raise HTTPException(400, "invalid JSON") from exc
    payload, status = await asyncio.to_thread(
        legacy_operation, request.method, path, body, content_type=content_type, raw_body=raw_body
    )
    return JSONResponse(payload, status_code=status)
