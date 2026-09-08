"""Durable ARQ worker for answer generation across API restarts."""
from __future__ import annotations

import asyncio
import os
import time

from arq.connections import RedisSettings

from db_store import close_thread_connection, update_message
from jobs import GENERATION_QUEUE_NAME
from persistence import get_store


def _persist_message(message_id: str, payload: dict) -> None:
    try:
        update_message(message_id, payload)
    finally:
        close_thread_connection()


async def generate_answer_job(ctx: dict, job_id: str, payload: dict | None = None) -> dict:
    """Generate and persist one answer; PostgreSQL remains the recovery source."""
    del ctx
    store = get_store()
    job = await asyncio.to_thread(store.get_job, job_id)
    if payload is None:
        payload = await asyncio.to_thread(store.get_job_payload, job_id)
    if not isinstance(payload, dict):
        raise ValueError("generation job payload not found")
    message_id = str(payload.get("assistant_message_id", "")).strip()
    request_payload = payload.get("request")
    if not message_id or not isinstance(request_payload, dict):
        raise ValueError("assistant_message_id and request are required")
    if not job or job["cancel_requested"]:
        await asyncio.to_thread(store.update_job, job_id, status="cancelled", progress=100, finished_at=time.time())
        await asyncio.to_thread(
            _persist_message,
            message_id,
            {"content": "Генерация отменена.", "status": "error", "metadata": {"job_id": job_id}},
        )
        return {"cancelled": True, "message_id": message_id}

    await asyncio.to_thread(store.update_job, job_id, status="running", progress=10, started_at=time.time())
    try:
        # Lazy import avoids loading the ASGI application in the worker until a
        # generation job actually starts.
        from asgi_app import run_ask

        result = await asyncio.to_thread(run_ask, request_payload)
        result["message_id"] = message_id
        result["job_id"] = job_id
        result["status"] = "complete"
        await asyncio.to_thread(
            _persist_message,
            message_id,
            {
                "content": str(result.get("answer", "")),
                "content_html": str(result.get("html_answer", "")),
                "status": "complete",
                "metadata": {
                    "job_id": job_id,
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
        await asyncio.to_thread(
            store.update_job,
            job_id,
            status="completed",
            progress=100,
            result=result,
            finished_at=time.time(),
        )
        return result
    except asyncio.CancelledError:
        await asyncio.to_thread(store.update_job, job_id, status="cancelled", progress=100, finished_at=time.time())
        await asyncio.to_thread(
            _persist_message,
            message_id,
            {"content": "Генерация отменена.", "status": "error", "metadata": {"job_id": job_id}},
        )
        raise
    except Exception as exc:
        await asyncio.to_thread(
            store.update_job,
            job_id,
            status="failed",
            progress=100,
            error=str(exc)[:2000],
            finished_at=time.time(),
        )
        await asyncio.to_thread(
            _persist_message,
            message_id,
            {"content": str(exc), "status": "error", "metadata": {"job_id": job_id}},
        )
        raise


class WorkerSettings:
    functions = [generate_answer_job]
    queue_name = GENERATION_QUEUE_NAME
    redis_settings = RedisSettings.from_dsn(os.getenv("RAG_REDIS_URL", "redis://localhost:6379/0"))
    allow_abort_jobs = True
    max_jobs = int(os.getenv("RAG_GENERATION_CONCURRENCY", "4"))
    job_timeout = int(os.getenv("RAG_GENERATION_TIMEOUT_SECONDS", "1800"))
