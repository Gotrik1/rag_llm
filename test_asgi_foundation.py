import unittest
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
