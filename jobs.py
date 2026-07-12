"""In-process background jobs for ingestion during the migration period."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Callable


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return asdict(self)


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def submit(self, kind: str, operation: Callable[[], dict[str, Any]]) -> Job:
        job = Job(id=uuid.uuid4().hex, kind=kind, created_at=time.time())
        self._jobs[job.id] = job

        async def run() -> None:
            job.status, job.started_at = "running", time.time()
            try:
                job.result = await asyncio.to_thread(operation)
                job.status = "completed"
            except Exception as exc:
                job.status, job.error = "failed", str(exc)
            finally:
                job.finished_at = time.time()

        asyncio.create_task(run(), name=f"rag-{kind}-{job.id}")
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)
