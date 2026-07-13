"""ARQ worker process for long-running document ingestion."""
from __future__ import annotations

import asyncio
import os
import json
import sys
import time

from arq import cron
from arq.connections import RedisSettings

from persistence import get_store


async def ingest_document_job(ctx: dict, job_id: str, payload: dict) -> dict:
    del ctx
    store = get_store()
    job = await asyncio.to_thread(store.get_job, job_id)
    if not job or job["cancel_requested"]:
        await asyncio.to_thread(store.update_job, job_id, status="cancelled", finished_at=time.time())
        return {"cancelled": True}
    await asyncio.to_thread(store.update_job, job_id, status="running", progress=10, started_at=time.time())
    process = None
    try:
        path = str(payload.get("path", "")).strip()
        if not path: raise ValueError("ingestion path is required")
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "ingestion_task", path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        progress = 20
        while process.returncode is None:
            try: await asyncio.wait_for(process.wait(), timeout=1)
            except TimeoutError: pass
            latest = await asyncio.to_thread(store.get_job, job_id)
            if latest and latest["cancel_requested"]:
                process.terminate(); await process.wait()
                await asyncio.to_thread(store.update_job, job_id, status="cancelled", progress=100, finished_at=time.time())
                return {"cancelled": True}
            progress = min(90, progress + 2)
            await asyncio.to_thread(store.update_job, job_id, progress=progress)
        stdout, stderr = await process.communicate()
        lines = stdout.decode("utf-8", errors="replace").splitlines()
        result = json.loads(lines[-1]) if lines else {"error": stderr.decode("utf-8", errors="replace")[-1000:]}
        if process.returncode != 0: raise RuntimeError(str(result.get("error", "ingestion failed")))
        await asyncio.to_thread(store.update_job, job_id, status="completed", progress=100, result=result, finished_at=time.time())
        return result
    except asyncio.CancelledError:
        if process and process.returncode is None:
            process.terminate(); await process.wait()
        await asyncio.to_thread(store.update_job, job_id, status="cancelled", progress=100, finished_at=time.time())
        raise
    except Exception as exc:
        await asyncio.to_thread(store.update_job, job_id, status="failed", error=str(exc), finished_at=time.time())
        raise


async def purge_expired_audit_events(ctx: dict) -> int:
    del ctx
    return await asyncio.to_thread(get_store().purge_audit, int(os.getenv("RAG_AUDIT_RETENTION_DAYS", "90")))


class WorkerSettings:
    functions = [ingest_document_job]
    cron_jobs = [cron(purge_expired_audit_events, hour=3, minute=15)]
    redis_settings = RedisSettings.from_dsn(os.getenv("RAG_REDIS_URL", "redis://localhost:6379/0"))
    allow_abort_jobs = True
    max_jobs = int(os.getenv("RAG_WORKER_CONCURRENCY", "2"))
    job_timeout = int(os.getenv("RAG_INGESTION_TIMEOUT_SECONDS", "1800"))
