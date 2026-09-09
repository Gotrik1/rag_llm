import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import asgi_app
from local_auth import RegistrationError, csrf_token, normalize_email, validate_password, verify_csrf


class LocalAuthUnitTests(unittest.TestCase):
    def test_email_is_normalized_and_password_policy_is_enforced(self):
        self.assertEqual(normalize_email("  User@Example.COM "), "user@example.com")
        with self.assertRaises(RegistrationError):
            validate_password("short", "user@example.com")
        with self.assertRaises(RegistrationError):
            validate_password("user-very-long-password", "user@example.com")

    def test_csrf_token_is_bound_to_session_and_application_secret(self):
        with patch.dict(os.environ, {"RAG_SECRET_KEY": "s" * 40}):
            token = csrf_token("session-one")
            self.assertTrue(verify_csrf("session-one", token))
            self.assertFalse(verify_csrf("session-two", token))


class LocalAuthApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(asgi_app.app)
        self.user = {
            "id": "78d80de5-0d21-45ad-bec7-d43c28681387",
            "email": "user@example.com",
            "display_name": "Пользователь",
            "is_admin": False,
            "created_at": None,
        }

    def test_missing_local_session_is_unauthorized(self):
        with patch.dict(os.environ, {"RAG_AUTH_MODE": "local", "RAG_SECRET_KEY": "s" * 40}):
            response = self.client.get("/api/me")
        self.assertEqual(response.status_code, 401)

    def test_session_endpoint_restores_user_and_issues_csrf_value(self):
        self.client.cookies.set("rag_session", "opaque-session")
        with patch.dict(os.environ, {"RAG_AUTH_MODE": "local", "RAG_SECRET_KEY": "s" * 40}), \
             patch("asgi_app.authenticate_session", return_value=self.user):
            response = self.client.get("/api/auth/session")
            csrf_is_valid = verify_csrf("opaque-session", response.json()["csrf_token"])

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])
        self.assertEqual(response.json()["user"]["email"], "user@example.com")
        self.assertTrue(csrf_is_valid)
        self.assertIn("HttpOnly", response.headers["set-cookie"])

    def test_mutation_rejects_missing_csrf_before_business_handler(self):
        self.client.cookies.set("rag_session", "opaque-session")
        with patch.dict(os.environ, {"RAG_AUTH_MODE": "local", "RAG_SECRET_KEY": "s" * 40}):
            response = self.client.post("/api/jobs/ingestion", json={"path": "private.docx"})
        self.assertEqual(response.status_code, 403)

    def test_valid_csrf_and_session_allow_mutation(self):
        self.client.cookies.set("rag_session", "opaque-session")
        identity_store = Mock()
        identity_store.enrich_principal.side_effect = lambda principal: principal
        queued = {"id": "job-1", "kind": "ingestion", "status": "queued", "progress": 0}
        with patch.dict(os.environ, {"RAG_AUTH_MODE": "local", "RAG_SECRET_KEY": "s" * 40}), \
             patch("asgi_app.authenticate_session", return_value=self.user), \
             patch("asgi_app.get_store", return_value=identity_store), \
             patch.object(asgi_app.jobs, "submit", AsyncMock(return_value=queued)):
            response = self.client.post(
                "/api/jobs/ingestion",
                json={"path": "private.docx"},
                headers={"X-CSRF-Token": csrf_token("opaque-session")},
            )
        self.assertEqual(response.status_code, 202)


if __name__ == "__main__":
    unittest.main()
