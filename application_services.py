"""Application use cases shared by HTTP adapters and background orchestration.

The services own message/job and document state transitions. They deliberately
do not import FastAPI: adapters translate ``ServiceError`` and event tuples to
their transport. Persistence and queue functions are injected at request time,
so request-local ownership and existing compatibility adapters remain intact.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable


class ServiceError(Exception):
    """A use-case failure with a stable status code for transport adapters."""

    def __init__(self, status_code: int, detail: str):
        """Retain the failure status and detail for protocol adapters."""
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class GenerationService:
    """Manage generation independently of the lifetime of an HTTP connection."""

    get_chat: Callable
    create_message: Callable
    update_message: Callable
    run_ask: Callable
    queue: Any
    tracked_tasks: set[asyncio.Task]
    forget_task: Callable
    durable: bool
    wait_seconds: float

    async def prepare(self, payload: dict, owner: str, admin: bool) -> dict:
        """Bind trusted ownership and validate a chat before publishing a task."""
        if not str(payload.get("question", "")).strip():
            raise ServiceError(400, "empty question")
        request = {**payload, "_owner_subject": owner, "_owner_admin": admin}
        if (
            request.get("chat_id")
            and await asyncio.to_thread(self.get_chat, request["chat_id"]) is None
        ):
            raise ServiceError(404, "chat not found")
        return request

    async def new_message(self, chat_id: str, owner: str) -> dict:
        """Create the durable assistant placeholder visible after page reload."""
        return await asyncio.to_thread(
            self.create_message,
            chat_id,
            {
                "role": "assistant",
                "content": "",
                "status": "running",
                "metadata": {"principal": owner},
            },
        )

    async def enqueue(self, request: dict, message: dict, owner: str) -> dict:
        """Submit one worker job and attach its identity to the placeholder."""
        try:
            job = await self.queue.submit(
                "generation",
                {
                    "assistant_message_id": str(message["id"]),
                    "request": request,
                    "owner_subject": owner,
                },
            )
            await asyncio.to_thread(
                self.update_message,
                str(message["id"]),
                {
                    "metadata": {"principal": owner, "job_id": str(job["id"])},
                },
            )
            return job
        except Exception as exc:
            await asyncio.to_thread(
                self.update_message, str(message["id"]), {"content": str(exc), "status": "error"}
            )
            raise ServiceError(503, "Generation queue is unavailable") from exc

    async def persist(self, request: dict, message: dict) -> dict:
        """Publish exactly the result metadata needed to restore a conversation."""
        message_id = str(message["id"])
        try:
            result = await asyncio.to_thread(self.run_ask, request)
        except Exception as exc:
            await asyncio.to_thread(
                self.update_message, message_id, {"content": str(exc), "status": "error"}
            )
            raise
        await asyncio.to_thread(
            self.update_message,
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
                    "retrieval_trace": result.get("retrieval_trace") or {},
                },
            },
        )
        result["message_id"] = message_id
        return result

    async def execute(self, payload: dict, owner: str, admin: bool) -> dict:
        """Return a completed answer or a pollable job without cancelling work."""
        started = time.perf_counter()
        request = await self.prepare(payload, owner, admin)
        message = (
            await self.new_message(request["chat_id"], owner) if request.get("chat_id") else None
        )
        if message and self.durable:
            job = await self.enqueue(request, message, owner)
            terminal = await self.queue.wait(str(job["id"]), self.wait_seconds)
            if (
                terminal
                and terminal["status"] == "completed"
                and isinstance(terminal.get("result"), dict)
            ):
                return terminal["result"]
            if terminal and terminal["status"] in {"failed", "cancelled"}:
                raise ServiceError(500, str(terminal.get("error") or "Generation did not complete"))
            return {
                "answer": "",
                "message_id": str(message["id"]),
                "job_id": str(job["id"]),
                "status": "running",
                "flow_mode": "queued",
                "from_cache": False,
                "elapsed_s": time.perf_counter() - started,
            }
        if message:
            task = asyncio.create_task(self.persist(request, message))
            self.tracked_tasks.add(task)
            task.add_done_callback(self.forget_task)
            # Disconnect cancels only delivery. The owned task must persist its result.
            return await asyncio.shield(task)
        return await asyncio.to_thread(self.run_ask, request)

    async def stream_events(
        self, request: dict, owner: str, disconnected: Callable, run_stream: Callable
    ) -> AsyncIterator[tuple[str, dict]]:
        """Yield transport-neutral progress events; durable work survives disconnect."""
        if request.get("chat_id") and self.durable:
            message = await self.new_message(request["chat_id"], owner)
            try:
                job = await self.enqueue(request, message, owner)
            except ServiceError as exc:
                yield "error", {"message": exc.detail}
                return
            yield "started", {
                "flow": "queued",
                "job_id": str(job["id"]),
                "message_id": str(message["id"]),
            }
            while not await disconnected():
                state = await self.queue.get(str(job["id"]))
                if state is None:
                    yield "error", {"message": "Generation job disappeared"}
                    return
                if state["status"] == "completed":
                    result = dict(state.get("result") or {})
                    result.pop("html_answer", None)
                    yield "completed", result
                    return
                if state["status"] in {"failed", "cancelled"}:
                    yield "error", {
                        "message": str(state.get("error") or "Generation did not complete")
                    }
                    return
                yield "heartbeat", {"status": state["status"], "progress": state.get("progress", 0)}
                await asyncio.sleep(1)
            return
        yield "started", {"flow": "pending"}
        deltas: asyncio.Queue[str] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        callback = lambda text: loop.call_soon_threadsafe(deltas.put_nowait, text)
        task = asyncio.create_task(asyncio.to_thread(run_stream, request, callback))
        while not task.done() or not deltas.empty():
            if await disconnected():
                task.cancel()
                return
            try:
                yield "delta", {"text": await asyncio.wait_for(deltas.get(), timeout=10)}
            except TimeoutError:
                yield "heartbeat", {"status": "running"}
        try:
            result = task.result()
            result.pop("html_answer", None)
            yield "completed", result
        except Exception:
            yield "error", {"message": "RAG generation failed"}


@dataclass
class DocumentService:
    """Own upload registration, processing submission and deletion tombstones."""

    create_document: Callable
    update_document: Callable
    get_document: Callable
    tombstone_document: Callable
    infer_domains: Callable
    safe_name: Callable
    queue: Any
    upload_dir: Path
    allowed_suffixes: set[str]
    max_bytes: int
    max_files: int

    async def upload(
        self, files: list, project_id: str, chat_id: str, owner: str, manage_global: bool
    ) -> dict:
        """Register authorized files and enqueue ingestion with explicit scope."""
        if not files or len(files) > self.max_files:
            raise ServiceError(400, f"upload between 1 and {self.max_files} files")
        project = project_id.strip() or None
        chat = chat_id.strip() or None
        if not project and not chat and not manage_global:
            raise ServiceError(403, "global document upload is not permitted for this account")
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        queued = []
        for upload in files:
            name = self.safe_name(upload.filename or "upload.bin")
            if Path(name).suffix.lower() not in self.allowed_suffixes:
                raise ServiceError(415, f"unsupported document type: {name}")
            target = self.upload_dir / f"{uuid.uuid4().hex}_{name}"
            size = 0
            try:
                with target.open("xb") as output:
                    while chunk := await upload.read(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ServiceError(
                                413,
                                f"file is larger than {self.max_bytes // (1024 * 1024)} MiB: {name}",
                            )
                        output.write(chunk)
                document = await asyncio.to_thread(
                    self.create_document,
                    project,
                    chat,
                    {
                        "file_name": name,
                        "source_path": str(target),
                        "mime_type": upload.content_type or "application/octet-stream",
                        "status": "queued",
                        "metadata": {
                            "size_bytes": size,
                            "knowledge_domains": list(self.infer_domains(name)),
                        },
                    },
                )
            except Exception:
                # An unregistered upload has no owner in the registry; avoid orphaned bytes.
                target.unlink(missing_ok=True)
                raise
            try:
                job = await self.queue.submit(
                    "ingestion",
                    {
                        "path": str(target),
                        "document_id": str(document["id"]),
                        "owner_subject": owner,
                    },
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self.update_document,
                    str(document["id"]),
                    {
                        "status": "failed",
                        "processing_state": {"error": str(exc)[:1000]},
                    },
                )
                raise ServiceError(503, f"ingestion queue unavailable for {name}") from exc
            await asyncio.to_thread(
                self.update_document,
                str(document["id"]),
                {
                    "processing_state": {"job_id": job["id"], "progress": 0},
                },
            )
            queued.append(
                {
                    "id": str(document["id"]),
                    "file": name,
                    "size_bytes": size,
                    "status": "queued",
                    "job_id": job["id"],
                    "url": f"/api/documents/{document['id']}/content",
                }
            )
        return {"files": queued, "chunks": 0, "queued": True}

    async def content(self, document_id: str) -> tuple[Path, str, str]:
        """Resolve only existing original files strictly below the upload root."""
        document = await asyncio.to_thread(self.get_document, document_id)
        if document is None:
            raise ServiceError(404, "document not found")
        path = Path(str(document.get("source_path", ""))).resolve()
        if self.upload_dir.resolve() not in path.parents or not path.is_file():
            raise ServiceError(404, "document content is unavailable")
        return (
            path,
            str(document.get("mime_type") or "application/octet-stream"),
            str(document["file_name"]),
        )

    async def delete(self, document_id: str, owner: str, manage_global: bool) -> dict:
        """Hide before enqueueing cleanup; queue failure must not restore evidence."""
        document = await asyncio.to_thread(self.get_document, document_id)
        if document is None:
            raise ServiceError(404, "document not found")
        if not document.get("owner_subject") and not manage_global:
            raise ServiceError(403, "shared document deletion is not permitted for this account")
        try:
            tombstone = await asyncio.to_thread(self.tombstone_document, document_id)
        except PermissionError as exc:
            raise ServiceError(403, str(exc)) from exc
        if tombstone is None:
            raise ServiceError(409, "document is already being deleted")
        try:
            job = await self.queue.submit(
                "deletion",
                {
                    "path": str(document.get("source_path") or ""),
                    "document_id": document_id,
                    "owner_subject": owner,
                },
            )
        except Exception as exc:
            # The tombstone is intentional: stale vectors remain excluded while cleanup retries.
            raise ServiceError(503, "document is hidden, but cleanup queue is unavailable") from exc
        return {"id": document_id, "status": "deleting", "job_id": str(job["id"])}
