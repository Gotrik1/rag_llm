import unittest
from unittest.mock import AsyncMock, Mock, patch

from jobs import JobQueue
from generation_worker import generate_answer_job
from ingestion_worker import ingest_document_job


class JobQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_access_is_limited_to_owner_or_superadmin(self) -> None:
        store = Mock()
        store.get_job_payload.return_value = {"owner_subject": "alice"}
        queue = JobQueue("redis://example.invalid:6379/0")
        with patch("jobs.get_store", return_value=store):
            self.assertTrue(await queue.can_access("job-1", "alice"))
            self.assertFalse(await queue.can_access("job-1", "bob"))
            self.assertTrue(await queue.can_access("job-1", "bob", privileged=True))

    async def test_submit_routes_generation_to_dedicated_worker(self) -> None:
        store = Mock(enabled=True)
        store.create_job.return_value = {"id": "job-generation"}
        queue = JobQueue("redis://example.invalid:6379/0")
        queue._redis = Mock()
        queue._redis.enqueue_job = AsyncMock()

        with patch("jobs.get_store", return_value=store):
            job = await queue.submit("generation", {"request": {"question": "test"}})

        self.assertEqual(job, {"id": "job-generation"})
        queue._redis.enqueue_job.assert_awaited_once_with(
            "generate_answer_job",
            "job-generation",
            _job_id="job-generation",
        )

    async def test_submit_rejects_unknown_job_kind_before_persisting(self) -> None:
        store = Mock(enabled=True)
        queue = JobQueue("redis://example.invalid:6379/0")
        with patch("jobs.get_store", return_value=store):
            with self.assertRaisesRegex(ValueError, "unsupported job kind"):
                await queue.submit("other", {})
        store.create_job.assert_not_called()

    async def test_generation_worker_persists_terminal_answer_and_job(self) -> None:
        store = Mock()
        store.get_job.return_value = {"cancel_requested": False}
        answer = {
            "answer": "grounded",
            "html_answer": "<p>grounded</p>",
            "flow_mode": "python",
            "sources": [{"file": "manual.pdf"}],
        }
        with patch("generation_worker.get_store", return_value=store), \
             patch("generation_worker.update_message", return_value={}) as update_message, \
             patch("asgi_app.run_ask", return_value=answer):
            result = await generate_answer_job(
                {},
                "job-1",
                {"assistant_message_id": "message-1", "request": {"question": "test", "chat_id": "chat-1"}},
            )

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["message_id"], "message-1")
        self.assertEqual(update_message.call_args.args[1]["status"], "complete")
        self.assertEqual(store.update_job.call_args.kwargs["status"], "completed")

    async def test_generation_worker_loads_private_payload_from_postgresql(self) -> None:
        store = Mock()
        store.get_job.return_value = {"cancel_requested": False}
        store.get_job_payload.return_value = {
            "assistant_message_id": "message-1",
            "request": {"question": "test", "chat_id": "chat-1"},
        }
        with patch("generation_worker.get_store", return_value=store), \
             patch("generation_worker.update_message", return_value={}), \
             patch("asgi_app.run_ask", return_value={"answer": "ok", "flow_mode": "python"}):
            result = await generate_answer_job({}, "job-1")

        self.assertEqual(result["answer"], "ok")
        store.get_job_payload.assert_called_once_with("job-1")

    async def test_generation_worker_recovers_already_cancelled_job(self) -> None:
        store = Mock()
        store.get_job.return_value = {"cancel_requested": True}
        with patch("generation_worker.get_store", return_value=store), \
             patch("generation_worker.update_message", return_value={}) as update_message:
            result = await generate_answer_job(
                {},
                "job-1",
                {"assistant_message_id": "message-1", "request": {"question": "test"}},
            )

        self.assertTrue(result["cancelled"])
        self.assertEqual(update_message.call_args.args[1]["status"], "error")
        self.assertEqual(store.update_job.call_args.kwargs["status"], "cancelled")

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

    async def test_cancel_closes_registry_for_job_aborted_before_start(self) -> None:
        store = Mock()
        store.get_job_payload.return_value = {"assistant_message_id": "message-1"}
        store.request_cancel.return_value = True
        queue = JobQueue("redis://example.invalid:6379/0")
        queue._redis = Mock()
        arq_job = Mock()
        arq_job.abort = AsyncMock()

        with patch("jobs.get_store", return_value=store), \
             patch("jobs.Job", return_value=arq_job), \
             patch("jobs._mark_payload_cancelled") as mark_cancelled:
            self.assertTrue(await queue.cancel("job-1"))

        mark_cancelled.assert_called_once_with("job-1", {"assistant_message_id": "message-1"})

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
