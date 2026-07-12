"""Persistent PostgreSQL job state with Redis/ARQ delivery."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from persistence import get_store


class JobQueue:
    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or os.getenv("RAG_REDIS_URL", "redis://localhost:6379/0")
        self._redis: ArqRedis | None = None

    async def connect(self) -> None:
        if self._redis is None:
            self._redis = await create_pool(RedisSettings.from_dsn(self.redis_url))

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def ping(self) -> bool:
        await self.connect()
        return bool(await self._redis.ping())

    async def submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        store = get_store()
        if not store.enabled:
            raise RuntimeError("RAG_DATABASE_URL is required for persistent jobs")
        job = await asyncio.to_thread(store.create_job, kind, payload)
        await self.connect()
        await self._redis.enqueue_job("ingest_document_job", job["id"], payload, _job_id=job["id"])
        return job

    async def get(self, job_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(get_store().get_job, job_id)

    async def cancel(self, job_id: str) -> bool:
        accepted = await asyncio.to_thread(get_store().request_cancel, job_id)
        if accepted:
            await self.connect()
            await self._redis.abort_job(job_id)
            await asyncio.to_thread(get_store().update_job, job_id, status="cancelled", finished_at=time.time())
        return accepted
