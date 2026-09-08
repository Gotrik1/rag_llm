import unittest
from unittest.mock import AsyncMock, Mock, patch

from jobs import JobQueue
from ingestion_worker import ingest_document_job


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_aborts_the_arq_job_and_persists_state(self) -> None:
        store = Mock()
        store.request_cancel.return_value = True
        queue = JobQueue("redis://example.invalid:6379/0")
        queue._redis = Mock()
        arq_job = Mock()
        arq_job.abort = AsyncMock()

        with patch("jobs.get_store", return_value=store), patch("jobs.Job", return_value=arq_job):
            self.assertTrue(await queue.cancel("job-1"))

        arq_job.abort.assert_awaited_once_with()
        store.update_job.assert_called_once()

    async def test_worker_cancellation_updates_document_registry(self) -> None:
        store = Mock()
        store.get_job.return_value = {"cancel_requested": True}

        with patch("ingestion_worker.get_store", return_value=store), \
             patch("ingestion_worker.update_document", return_value={}) as update_document:
            result = await ingest_document_job(
                {},
                "job-1",
                {"document_id": "document-1", "path": "manual.pdf"},
            )

        self.assertEqual(result, {"cancelled": True})
        store.update_job.assert_called_once_with("job-1", status="cancelled", finished_at=unittest.mock.ANY)
        self.assertEqual(update_document.call_args.args[0], "document-1")
        self.assertEqual(update_document.call_args.args[1]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
