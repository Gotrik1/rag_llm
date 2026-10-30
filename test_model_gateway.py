from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse


ROOT = Path(__file__).resolve().parent
GATEWAY_PATH = ROOT / "deploy" / "model-gateway" / "app.py"


class ModelGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.token = "mdl_test_unit_test_token"
        secret = Path(cls.temp_dir.name) / "hashes"
        digest = hashlib.sha256(cls.token.encode("utf-8")).hexdigest()
        secret.write_text(f"developer-test:{digest}\n", encoding="utf-8")

        cls.previous_secret = os.environ.get("MODEL_API_TOKEN_HASHES_FILE")
        os.environ["MODEL_API_TOKEN_HASHES_FILE"] = str(secret)
        spec = importlib.util.spec_from_file_location("test_direct_model_gateway", GATEWAY_PATH)
        cls.gateway = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.gateway)
        cls.client = TestClient(cls.gateway.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.temp_dir.cleanup()
        if cls.previous_secret is None:
            os.environ.pop("MODEL_API_TOKEN_HASHES_FILE", None)
        else:
            os.environ["MODEL_API_TOKEN_HASHES_FILE"] = cls.previous_secret

    def test_health_reports_configured_token_count(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok", "configured_tokens": 1})

    def test_missing_or_invalid_token_is_rejected(self):
        missing = self.client.get("/v1/models")
        invalid = self.client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(missing.headers["www-authenticate"], "Bearer")

    def test_valid_token_reaches_only_the_selected_upstream_route(self):
        proxy = AsyncMock(return_value=JSONResponse({"ok": True}))
        with patch.object(self.gateway, "_proxy", proxy):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"model": "Qwen/Qwen3.8-27B", "messages": [{"role": "user", "content": "test"}]},
            )
        self.assertEqual(response.status_code, 200)
        proxy.assert_awaited_once()
        self.assertEqual(proxy.await_args.args[1:], ("/v1/chat/completions", "developer-test"))

    def test_unlisted_upstream_route_is_not_exposed(self):
        response = self.client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {self.token}"},
            json={"input": "test"},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
