import asyncio
import threading
import unittest
import tempfile
import uuid
from datetime import datetime, timezone
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import asgi_app
from asgi_app import app


class AsgiFoundationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_health_metrics_and_request_identifiers(self):
        response = self.client.get("/healthz", headers={"X-Request-ID": "test-request"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["X-Request-ID"], "test-request")
        self.assertIn("X-Trace-ID", response.headers)

        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("rag_http_requests_total", metrics.text)

    def test_live_openapi_schema_includes_production_routes(self):
        response = self.client.get("/api/openapi.json")
        self.assertEqual(response.status_code, 200)
        paths = response.json()["paths"]
        self.assertIn("/api/ask", paths)
        self.assertIn("/api/upload", paths)
        self.assertIn("/api/projects/{project_id}/documents", paths)

    def test_development_bootstrap_is_superadmin(self):
        response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["subject"], "rag-superadmin")
        self.assertEqual(response.json()["roles"], ["superadmin"])

    def test_rbac_denies_development_user_without_rag_access(self):
        response = self.client.get("/api/me", headers={"Authorization": "Bearer ordinary-user:"})
        self.assertEqual(response.status_code, 403)

    def test_ask_keeps_existing_payload_and_runs_off_event_loop(self):
        payload = {"answer": "ok", "flow_mode": "python"}
        with patch("asgi_app.run_ask", return_value=payload) as ask:
            response = self.client.post("/api/ask", json={"question": "Test question"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)
        ask.assert_called_once_with("Test question")

    def test_ask_serializes_registry_uuid_and_timestamp(self):
        document_id = uuid.uuid4()
        uploaded_at = datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc)
        payload = {
            "answer": "registry",
            "flow_mode": "workspace",
            "documents": [{"id": document_id, "created_at": uploaded_at}],
        }
        with patch("asgi_app.run_ask", return_value=payload):
            response = self.client.post("/api/ask", json={"question": "Какие документы загружены?"})

        self.assertEqual(response.status_code, 200)
        document = response.json()["documents"][0]
        self.assertEqual(document["id"], str(document_id))
        self.assertEqual(document["created_at"], "2026-09-09T09:00:00+00:00")

    def test_ask_persists_server_owned_assistant_message(self):
        payload = {"answer": "persistent answer", "html_answer": "<p>persistent answer</p>", "flow_mode": "python"}
        created = {"id": "message-1"}
        with patch("asgi_app.create_message", return_value=created) as create_message, \
             patch("asgi_app.update_message", return_value={}) as update_message, \
             patch("asgi_app.run_ask", return_value=payload):
            response = self.client.post("/api/ask", json={"question": "Test question", "chat_id": "chat-1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message_id"], "message-1")
        create_message.assert_called_once()
        self.assertEqual(update_message.call_args.args[1]["status"], "complete")

    def test_durable_ask_uses_generation_queue_and_returns_persisted_result(self):
        created = {"id": "message-1"}
        completed = {
            "status": "completed",
            "result": {
                "answer": "durable answer",
                "message_id": "message-1",
                "job_id": "job-1",
                "status": "complete",
                "flow_mode": "python",
            },
        }
        with patch.dict("os.environ", {"RAG_DURABLE_GENERATION": "true"}), \
             patch("asgi_app.create_message", return_value=created), \
             patch("asgi_app.update_message", return_value={}), \
             patch.object(asgi_app.jobs, "submit", AsyncMock(return_value={"id": "job-1"})) as submit, \
             patch.object(asgi_app.jobs, "wait", AsyncMock(return_value=completed)):
            response = self.client.post("/api/ask", json={"question": "Test question", "chat_id": "chat-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "durable answer")
        submit.assert_awaited_once_with(
            "generation",
            {
                "assistant_message_id": "message-1",
                "request": {"question": "Test question", "chat_id": "chat-1"},
                "owner_subject": "rag-superadmin",
            },
        )

    def test_durable_ask_returns_accepted_when_job_outlives_http_wait(self):
        with patch.dict("os.environ", {"RAG_DURABLE_GENERATION": "true"}), \
             patch("asgi_app.create_message", return_value={"id": "message-1"}), \
             patch("asgi_app.update_message", return_value={}), \
             patch.object(asgi_app.jobs, "submit", AsyncMock(return_value={"id": "job-1"})), \
             patch.object(asgi_app.jobs, "wait", AsyncMock(return_value={"id": "job-1", "status": "running"})):
            response = self.client.post("/api/ask", json={"question": "Test question", "chat_id": "chat-1"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "running")
        self.assertEqual(response.json()["job_id"], "job-1")

    def test_server_owned_generation_survives_client_waiter_cancellation(self):
        started = threading.Event()
        release = threading.Event()

        def slow_ask(_payload):
            started.set()
            self.assertTrue(release.wait(2))
            return {"answer": "done", "html_answer": "<p>done</p>", "flow_mode": "python"}

        async def scenario():
            generation = asyncio.create_task(
                asgi_app._run_and_persist_ask({"question": "test", "chat_id": "chat-1"}, {"id": "message-1"})
            )

            async def client_waiter():
                return await asyncio.shield(generation)

            waiter = asyncio.create_task(client_waiter())
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
            release.set()
            return await generation

        with patch("asgi_app.run_ask", side_effect=slow_ask), \
             patch("asgi_app.update_message", return_value={}) as update_message:
            result = asyncio.run(scenario())
        self.assertEqual(result["message_id"], "message-1")
        self.assertEqual(update_message.call_args.args[1]["status"], "complete")

    def test_two_chat_generations_run_concurrently(self):
        barrier = threading.Barrier(2)
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0

        def concurrent_ask(payload):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                barrier.wait(timeout=2)
                return {
                    "answer": f"answer for {payload['chat_id']}",
                    "html_answer": "<p>answer</p>",
                    "flow_mode": "python",
                }
            finally:
                with state_lock:
                    active -= 1

        async def scenario():
            return await asyncio.gather(
                asgi_app._run_and_persist_ask({"question": "one", "chat_id": "chat-1"}, {"id": "message-1"}),
                asgi_app._run_and_persist_ask({"question": "two", "chat_id": "chat-2"}, {"id": "message-2"}),
            )

        with patch("asgi_app.run_ask", side_effect=concurrent_ask), \
             patch("asgi_app.update_message", return_value={}):
            results = asyncio.run(scenario())

        self.assertEqual(maximum_active, 2)
        self.assertEqual({item["message_id"] for item in results}, {"message-1", "message-2"})

    def test_sse_stream_emits_answer_deltas_and_completion(self):
        payload = {"answer": "streamed answer", "html_answer": "ignored", "flow_mode": "python"}
        def streamed(_question, callback):
            callback("streamed answer")
            return payload
        with patch("asgi_app.run_ask_stream", side_effect=streamed):
            response = self.client.post("/api/ask/stream", json={"question": "Test"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: delta", response.text)
        self.assertIn("streamed answer", response.text)
        self.assertIn("event: completed", response.text)

    def test_durable_sse_stream_uses_queue_and_persisted_result(self):
        completed = {
            "id": "job-1",
            "status": "completed",
            "result": {"answer": "durable stream", "flow_mode": "python", "status": "complete"},
        }
        with patch.dict("os.environ", {"RAG_DURABLE_GENERATION": "true"}), \
             patch("asgi_app.create_message", return_value={"id": "message-1"}), \
             patch("asgi_app.update_message", return_value={}), \
             patch.object(asgi_app.jobs, "submit", AsyncMock(return_value={"id": "job-1"})), \
             patch.object(asgi_app.jobs, "get", AsyncMock(return_value=completed)):
            response = self.client.post("/api/ask/stream", json={"question": "Test", "chat_id": "chat-1"})

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: started", response.text)
        self.assertIn("event: completed", response.text)
        self.assertIn("durable stream", response.text)

    def test_legacy_routes_are_protected_and_keep_response_status(self):
        with patch("asgi_app.legacy_operation", return_value=({"mode": "python"}, 200)) as operation:
            response = self.client.get("/api/flow-mode")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"mode": "python"})
        operation.assert_called_once()

    def test_ingestion_job_is_accepted_and_exposed(self):
        queued = {"id": "job-1", "kind": "ingestion", "status": "queued", "progress": 0}
        with patch.object(__import__("asgi_app").jobs, "submit", AsyncMock(return_value=queued)), \
             patch.object(__import__("asgi_app").jobs, "get", AsyncMock(return_value=queued)):
            created = self.client.post("/api/jobs/ingestion", json={"path": "test.docx"})
            self.assertEqual(created.status_code, 202)
            status = self.client.get(f"/api/jobs/{created.json()['id']}")
        self.assertEqual(status.json()["status"], "queued")

    def test_upload_is_persisted_and_queued_for_project(self):
        queued = {"id": "job-1", "kind": "ingestion", "status": "queued", "progress": 0}
        document = {"id": "document-1"}
        with tempfile.TemporaryDirectory() as directory, \
             patch("asgi_app.UPLOAD_DIR", Path(directory)), \
             patch("asgi_app.create_document", return_value=document) as create_document, \
             patch("asgi_app.update_document", return_value=document), \
             patch.object(__import__("asgi_app").jobs, "submit", AsyncMock(return_value=queued)) as submit:
            response = self.client.post(
                "/api/upload",
                data={"project_id": "project-1", "chat_id": "chat-1"},
                files={"files": ("manual.md", b"# Manual\nUseful content", "text/markdown")},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["files"][0]["job_id"], "job-1")
        self.assertEqual(create_document.call_args.args[0], "project-1")
        self.assertEqual(create_document.call_args.args[1], "chat-1")
        self.assertEqual(submit.await_args.args[1]["document_id"], "document-1")

    def test_project_document_registry_is_exposed(self):
        items = [{"id": "document-1", "file_name": "manual.pdf", "status": "ready"}]
        with patch("asgi_app.list_documents", return_value=items) as list_documents:
            response = self.client.get("/api/projects/project-1/documents")
        self.assertEqual(response.status_code, 200)
        document = response.json()["items"][0]
        self.assertEqual(document["file_name"], "manual.pdf")
        self.assertEqual(document["version_status"], "unknown")
        self.assertIsNone(document["version_label"])
        list_documents.assert_called_once_with("project-1", None)


if __name__ == "__main__":
    unittest.main()
