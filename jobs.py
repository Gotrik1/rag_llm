"""Persistent PostgreSQL job state with Redis/ARQ delivery."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from arq.jobs import Job

from persistence import get_store


INGESTION_QUEUE_NAME = os.getenv("RAG_INGESTION_QUEUE_NAME", "arq:queue:ingestion")
GENERATION_QUEUE_NAME = os.getenv("RAG_GENERATION_QUEUE_NAME", "arq:queue:generation")


def _mark_payload_cancelled(job_id: str, payload: dict[str, Any]) -> None:
    """Close registry state even when ARQ aborts a job before it starts."""
    from db_store import close_thread_connection, update_document, update_message

    try:
        message_id = str(payload.get("assistant_message_id", "")).strip()
        document_id = str(payload.get("document_id", "")).strip()
        if message_id:
            update_message(
                message_id,
                {"content": "Генерация отменена.", "status": "error", "metadata": {"job_id": job_id}},
            )
        if document_id:
            update_document(
                document_id,
                {"status": "cancelled", "processing_state": {"job_id": job_id, "progress": 100}},
            )
    finally:
        close_thread_connection()


class JobQueue:
    """Coordinate persistent job records and ARQ delivery; Redis receives job IDs rather than private payloads."""
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or os.getenv("RAG_REDIS_URL", "redis://localhost:6379/0")
        self._redis: ArqRedis | None = None

    async def connect(self) -> None:
        """Lazily open this queue facade's Redis connection pool."""
        if self._redis is None:
            self._redis = await create_pool(RedisSettings.from_dsn(self.redis_url))

    async def close(self) -> None:
        """Release the owned Redis pool and permit a later reconnect."""
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def ping(self) -> bool:
        """Check Redis delivery availability, establishing the connection when necessary."""
        await self.connect()
        return bool(await self._redis.ping())

    async def submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist a supported job before enqueueing its ID; mark the record failed if delivery raises."""
        store = get_store()
        if not store.enabled:
            raise RuntimeError("RAG_DATABASE_URL is required for persistent jobs")
        route = {
            "ingestion": ("ingest_document_job", INGESTION_QUEUE_NAME),
            "deletion": ("delete_document_job", INGESTION_QUEUE_NAME),
            "generation": ("generate_answer_job", GENERATION_QUEUE_NAME),
        }.get(kind)
        if route is None:
            raise ValueError(f"unsupported job kind: {kind}")
        function_name, queue_name = route
        job = await asyncio.to_thread(store.create_job, kind, payload)
        try:
            await self.connect()
            # The potentially sensitive payload stays in PostgreSQL. Redis
            # carries only the opaque job id used for delivery and retries.
            await self._redis.enqueue_job(
                function_name,
                job["id"],
                _job_id=job["id"],
                _queue_name=queue_name,
            )
        except Exception as exc:
            await asyncio.to_thread(
                store.update_job,
                job["id"],
                status="failed",
                error=str(exc),
                finished_at=time.time(),
            )
            raise
        return job

    async def wait(self, job_id: str, timeout: float, poll_interval: float = 0.25) -> dict[str, Any] | None:
        """Wait for persistent terminal state without owning or cancelling the ARQ job."""
        deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
        while True:
            job = await self.get(job_id)
            if job is None or job["status"] in {"completed", "failed", "cancelled"}:
                return job
            if asyncio.get_running_loop().time() >= deadline:
                return job
            await asyncio.sleep(max(0.05, poll_interval))

    async def get(self, job_id: str) -> dict[str, Any] | None:
        """Fetch the public persisted job status without exposing the worker-only payload."""
        return await asyncio.to_thread(get_store().get_job, job_id)

    async def can_access(self, job_id: str, subject: str, privileged: bool = False) -> bool:
        """Check payload ownership unless the caller has already established privileged access."""
        if privileged:
            return True
        payload = await asyncio.to_thread(get_store().get_job_payload, job_id)
        return isinstance(payload, dict) and payload.get("owner_subject") == subject

    async def cancel(self, job_id: str) -> bool:
        """Request cancellation, abort ARQ delivery and close associated message/document state when accepted."""
        store = get_store()
        payload = await asyncio.to_thread(store.get_job_payload, job_id)
        accepted = await asyncio.to_thread(store.request_cancel, job_id)
        if accepted:
            await self.connect()
            await Job(job_id, self._redis).abort()
            await asyncio.to_thread(store.update_job, job_id, status="cancelled", progress=100, finished_at=time.time())
            if isinstance(payload, dict):
                await asyncio.to_thread(_mark_payload_cancelled, job_id, payload)
        return accepted
