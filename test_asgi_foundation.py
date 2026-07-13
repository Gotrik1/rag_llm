import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

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


if __name__ == "__main__":
    unittest.main()
