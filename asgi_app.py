"""ASGI entry point for the incremental RAG service migration."""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from opentelemetry.trace import format_trace_id, get_current_span
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from audit import record as audit_record
from db_store import create_document, create_message, get_document, list_documents, update_document, update_message
from document_catalog import annotate_document_versions
from jobs import JobQueue
from legacy_adapter import invoke_legacy
from persistence import get_store
from security import AuthenticationError, AuthorizationError, Principal, RAG_ACCESS, authorize, configured_identity_provider
from telemetry import AUTH_DECISIONS, JOBS, PIPELINE_SECONDS, REQUESTS, REQUEST_SECONDS, configure_logging, configure_telemetry, tracer
from runtime_paths import UPLOAD_DIR


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    chat_id: str | None = None


ALLOWED_UPLOAD_SUFFIXES = {".docx", ".pdf", ".md", ".txt"}
MAX_UPLOAD_BYTES = int(os.getenv("RAG_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
MAX_UPLOAD_FILES = int(os.getenv("RAG_MAX_UPLOAD_FILES", "20"))
GENERATION_WAIT_SECONDS = float(os.getenv("RAG_GENERATION_WAIT_SECONDS", "900"))


def safe_upload_name(filename: str) -> str:
    name = Path(filename).name.strip() or "upload.bin"
    name = re.sub(r"[^A-Za-zА-Яа-яЁё0-9._ -]+", "_", name)
    return name[:180] or "upload.bin"


def run_ask(payload: dict | str) -> dict:
    """Run the current RAG endpoint through the protected compatibility bridge."""
    if isinstance(payload, str):
        payload = {"question": payload}
    result, status = legacy_operation("POST", "/api/ask", payload)
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
    bootstrap_subject = os.getenv("BOOTSTRAP_SUPERADMIN_SUBJECT", "").strip() or "rag-superadmin"
    await asyncio.to_thread(get_store().bootstrap_superadmin, bootstrap_subject)
    yield
    await jobs.close()


app = FastAPI(title="RAG service", version="0.1.0", lifespan=lifespan)
jobs = JobQueue()
_persistent_ask_tasks: set[asyncio.Task] = set()


def _forget_persistent_ask(task: asyncio.Task) -> None:
    _persistent_ask_tasks.discard(task)
    if not task.cancelled():
        task.exception()  # Consume detached failures after a client disconnect.


async def _run_and_persist_ask(request_payload: dict, assistant_message: dict) -> dict:
    message_id = str(assistant_message["id"])
    try:
        result = await asyncio.to_thread(run_ask, request_payload)
    except Exception as exc:
        await asyncio.to_thread(update_message, message_id, {"content": str(exc), "status": "error"})
        raise
    await asyncio.to_thread(
        update_message,
        message_id,
        {
            "content": str(result.get("answer", "")),
            "content_html": str(result.get("html_answer", "")),
            "status": "complete",
            "metadata": {
                "flow_mode": result.get("flow_mode"),
                "from_cache": bool(result.get("from_cache", False)),
                "elapsed_s": result.get("elapsed_s", 0),
                "model_label": (result.get("llm") or {}).get("label"),
                "llm": result.get("llm") or {},
                "sources": result.get("sources") or [],
                "formulas": result.get("formulas") or [],
                "warnings": result.get("warnings") or [],
                "usage": result.get("usage") or {},
            },
        },
    )
    result["message_id"] = message_id
    return result
cors_origins = [origin.strip() for origin in os.getenv("RAG_CORS_ORIGINS", "http://127.0.0.1:5173").split(",") if origin.strip()]
app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"], expose_headers=["X-Request-ID", "X-Trace-ID"])


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
        request_payload = payload.model_dump(exclude_none=True)
        assistant_message: dict | None = None
        if payload.chat_id:
            assistant_message = await asyncio.to_thread(
                create_message,
                payload.chat_id,
                {"role": "assistant", "content": "", "status": "running", "metadata": {"principal": principal.subject}},
            )
        durable_generation = os.getenv("RAG_DURABLE_GENERATION", "false").lower() == "true"
        if assistant_message and durable_generation:
            try:
                job = await jobs.submit(
                    "generation",
                    {
                        "assistant_message_id": str(assistant_message["id"]),
                        "request": request_payload,
                        "owner_subject": principal.subject,
                    },
                )
                await asyncio.to_thread(
                    update_message,
                    str(assistant_message["id"]),
                    {
                        "metadata": {
                            "principal": principal.subject,
                            "job_id": str(job["id"]),
                        }
                    },
                )
            except Exception as exc:
                await asyncio.to_thread(
                    update_message,
                    str(assistant_message["id"]),
                    {"content": str(exc), "status": "error"},
                )
                raise HTTPException(503, "Generation queue is unavailable") from exc
            terminal = await jobs.wait(str(job["id"]), GENERATION_WAIT_SECONDS)
            if terminal and terminal["status"] == "completed" and isinstance(terminal.get("result"), dict):
                result = terminal["result"]
            elif terminal and terminal["status"] in {"failed", "cancelled"}:
                raise HTTPException(500, str(terminal.get("error") or "Generation did not complete"))
            else:
                result = {
                    "answer": "",
                    "message_id": str(assistant_message["id"]),
                    "job_id": str(job["id"]),
                    "status": "running",
                    "flow_mode": "queued",
                    "from_cache": False,
                    "elapsed_s": time.perf_counter() - started,
                }
        elif assistant_message:
            task = asyncio.create_task(_run_and_persist_ask(request_payload, assistant_message))
            _persistent_ask_tasks.add(task)
            task.add_done_callback(_forget_persistent_ask)
            # Client cancellation stops delivery only. The server-owned task
            # persists its terminal state for the next page load.
            result = await asyncio.shield(task)
        else:
            result = await asyncio.to_thread(run_ask, question)
        PIPELINE_SECONDS.labels("ask", result.get("flow_mode", "unknown")).observe(time.perf_counter() - started)
        return JSONResponse(
            jsonable_encoder(result),
            status_code=202 if result.get("status") == "running" else 200,
        )


def _sse(event: str, data: dict) -> str:
    encoded = jsonable_encoder(data)
    return f"event: {event}\ndata: {json.dumps(encoded, ensure_ascii=False, separators=(',', ':'))}\n\n"


@app.post("/api/ask/stream")
async def ask_stream(payload: AskRequest, request: Request, principal: Principal = Depends(current_principal)):
    """SSE contract with immediate progress, heartbeats and incremental answer chunks."""
    if payload.chat_id and os.getenv("RAG_DURABLE_GENERATION", "false").lower() == "true":
        async def durable_events():
            assistant_message = await asyncio.to_thread(
                create_message,
                payload.chat_id,
                {"role": "assistant", "content": "", "status": "running", "metadata": {"principal": principal.subject}},
            )
            try:
                job = await jobs.submit(
                    "generation",
                    {
                        "assistant_message_id": str(assistant_message["id"]),
                        "request": payload.model_dump(exclude_none=True),
                        "owner_subject": principal.subject,
                    },
                )
                await asyncio.to_thread(
                    update_message,
                    str(assistant_message["id"]),
                    {"metadata": {"principal": principal.subject, "job_id": str(job["id"])}},
                )
            except Exception as exc:
                await asyncio.to_thread(
                    update_message,
                    str(assistant_message["id"]),
                    {"content": str(exc), "status": "error"},
                )
                yield _sse("error", {"message": "Generation queue is unavailable"})
                return
            yield _sse(
                "started",
                {"flow": "queued", "job_id": str(job["id"]), "message_id": str(assistant_message["id"])},
            )
            while True:
                if await request.is_disconnected():
                    return
                state = await jobs.get(str(job["id"]))
                if state is None:
                    yield _sse("error", {"message": "Generation job disappeared"})
                    return
                if state["status"] == "completed":
                    result = dict(state.get("result") or {})
                    result.pop("html_answer", None)
                    yield _sse("completed", result)
                    return
                if state["status"] in {"failed", "cancelled"}:
                    yield _sse("error", {"message": str(state.get("error") or "Generation did not complete")})
                    return
                yield _sse("heartbeat", {"status": state["status"], "progress": state.get("progress", 0)})
                await asyncio.sleep(1)

        return StreamingResponse(
            durable_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def events():
        yield _sse("started", {"flow": "pending"})
        deltas: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        callback = lambda text: loop.call_soon_threadsafe(deltas.put_nowait, text)
        request_payload = payload.model_dump(exclude_none=True)
        task = asyncio.create_task(asyncio.to_thread(run_ask_stream, request_payload if payload.chat_id else payload.question.strip(), callback))
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
    """Persist uploads and enqueue ingestion without holding the request open."""
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"upload between 1 and {MAX_UPLOAD_FILES} files")
    normalized_project = project_id.strip() or None
    normalized_chat = chat_id.strip() or None
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    queued: list[dict] = []
    for upload in files:
        display_name = safe_upload_name(upload.filename or "upload.bin")
        if Path(display_name).suffix.lower() not in ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(415, f"unsupported document type: {display_name}")
        target = UPLOAD_DIR / f"{uuid.uuid4().hex}_{display_name}"
        size = 0
        try:
            with target.open("xb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, f"file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB: {display_name}")
                    output.write(chunk)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        document = await asyncio.to_thread(
            create_document,
            normalized_project,
            normalized_chat,
            {
                "file_name": display_name,
                "source_path": str(target),
                "mime_type": upload.content_type or "application/octet-stream",
                "status": "queued",
                "metadata": {"size_bytes": size},
            },
        )
        try:
            job = await jobs.submit(
                "ingestion",
                {
                    "path": str(target),
                    "document_id": str(document["id"]),
                    "owner_subject": principal.subject,
                },
            )
        except Exception as exc:
            await asyncio.to_thread(
                update_document,
                str(document["id"]),
                {"status": "failed", "processing_state": {"error": str(exc)[:1000]}},
            )
            raise HTTPException(503, f"ingestion queue unavailable for {display_name}") from exc
        await asyncio.to_thread(
            update_document,
            str(document["id"]),
            {"processing_state": {"job_id": job["id"], "progress": 0}},
        )
        queued.append({
            "id": str(document["id"]),
            "file": display_name,
            "size_bytes": size,
            "status": "queued",
            "job_id": job["id"],
            "url": f"/api/documents/{document['id']}/content",
        })
    return JSONResponse({"files": queued, "chunks": 0, "queued": True}, status_code=202)


@app.get("/api/projects/{project_id}/documents")
async def project_documents(project_id: str, _: Principal = Depends(current_principal)):
    items = await asyncio.to_thread(list_documents, project_id, None)
    return {"items": annotate_document_versions(items)}


@app.get("/api/documents")
async def documents(project_id: str = "", chat_id: str = "", _: Principal = Depends(current_principal)):
    items = await asyncio.to_thread(list_documents, project_id.strip() or None, chat_id.strip() or None)
    return {"items": annotate_document_versions(items)}


@app.get("/api/documents/{document_id}/content")
async def document_content(document_id: str, _: Principal = Depends(current_principal)):
    document = await asyncio.to_thread(get_document, document_id)
    if document is None:
        raise HTTPException(404, "document not found")
    path = Path(str(document.get("source_path", ""))).resolve()
    upload_root = UPLOAD_DIR.resolve()
    if upload_root not in path.parents or not path.is_file():
        raise HTTPException(404, "document content is unavailable")
    return FileResponse(path, media_type=str(document.get("mime_type") or "application/octet-stream"), filename=str(document["file_name"]))


@app.get("/api/openapi.json", include_in_schema=False)
async def api_openapi(_: Principal = Depends(current_principal)):
    """Expose the live FastAPI contract instead of the legacy static snapshot."""
    return app.openapi()


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, principal: Principal = Depends(current_principal)):
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
async def legacy_api(operation: str, request: Request, _: Principal = Depends(current_principal)):
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
    payload, status = await asyncio.to_thread(
        legacy_operation, request.method, path, body, content_type=content_type, raw_body=raw_body
    )
    return JSONResponse(jsonable_encoder(payload), status_code=status)
