"""Contract regression checks without a database, model or running service."""
import ast
from copy import deepcopy
import inspect
import json
import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

import asgi_app
from api_openapi import HTTP_METHODS, LEGACY_OPERATIONS, install_openapi
from security import Principal


ROOT = Path(__file__).resolve().parent


class OpenAPIContractTests(unittest.TestCase):
    def setUp(self):
        install_openapi(asgi_app.app, legacy_spec_path=ROOT / "openapi.json")
        self.spec = asgi_app.app.openapi()
        self.saved_overrides = dict(asgi_app.app.dependency_overrides)
        self.environment = patch.dict(os.environ, {"RAG_AUTH_MODE": "development", "RAG_DURABLE_GENERATION": "false"})
        self.environment.start()

        async def principal():
            yield Principal(subject="contract-test", roles=frozenset({"superadmin"}), permissions=frozenset({"*"}), attributes={}, provider="test")

        asgi_app.app.dependency_overrides[asgi_app.current_principal] = principal
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        self.client.close()
        self.environment.stop()
        asgi_app.app.dependency_overrides.clear()
        asgi_app.app.dependency_overrides.update(self.saved_overrides)
        asgi_app.app.openapi_schema = None

    def validate_response(self, path, method, status, value):
        schema = self.spec["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"]
        Draft202012Validator({**schema, "components": self.spec["components"]}).validate(value)

    def test_every_registered_native_api_operation_is_documented(self):
        expected = set()
        for route in asgi_app.app.routes:
            if isinstance(route, APIRoute) and route.include_in_schema:
                expected.update((route.path, method.lower()) for method in route.methods if method.lower() in HTTP_METHODS)
        actual = {(path, method) for path, item in self.spec["paths"].items() for method in item if method in HTTP_METHODS}
        self.assertTrue(expected <= actual, expected - actual)
        self.assertNotIn("/api/{operation}", self.spec["paths"])
        self.assertNotIn("/api/{operation:path}", self.spec["paths"])
        for path, methods in LEGACY_OPERATIONS.items():
            self.assertEqual(set(methods), set(self.spec["paths"][path]) & HTTP_METHODS)

    def test_literal_compatibility_routes_cannot_disappear_from_the_contract(self):
        # A newly added literal dispatch branch must acquire a documented path.
        from web_ui import Handler
        supported = set(self.spec["paths"]) | {"/api/openapi.json"}
        for name in ("do_GET", "do_POST", "do_PATCH", "do_DELETE"):
            source = inspect.getsource(getattr(Handler, name))
            tree = ast.parse(textwrap.dedent(source))
            literals = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("/api/") and not node.value.endswith("/")}
            self.assertFalse(literals - supported, (name, literals - supported))

    def test_both_schema_urls_serve_the_same_complete_contract(self):
        first = self.client.get("/openapi.json")
        second = self.client.get("/api/openapi.json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        for path in ("/api/projects", "/api/chats/{chat_id}/messages", "/api/providers", "/api/jobs/{job_id}"):
            self.assertIn(path, first.json()["paths"])

    def test_schema_generation_does_not_mutate_the_legacy_file(self):
        before = (ROOT / "openapi.json").read_bytes()
        result = deepcopy(self.spec)
        asgi_app.app.openapi_schema = None
        self.assertEqual(result, asgi_app.app.openapi())
        self.assertEqual(before, (ROOT / "openapi.json").read_bytes())

    def test_local_refs_resolve_and_operation_ids_are_unique(self):
        operation_ids = []
        def visit(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertTrue(value["$ref"].startswith("#/"))
                    target = self.spec
                    for key in value["$ref"][2:].split("/"):
                        target = target[key.replace("~1", "/").replace("~0", "~")]
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(self.spec)
        for item in self.spec["paths"].values():
            for method, operation in item.items():
                if method in HTTP_METHODS:
                    operation_ids.append(operation["operationId"])
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_component_schemas_are_valid_json_schema_and_swagger_uses_live_url(self):
        json.dumps(self.spec, ensure_ascii=False)
        for name, schema in self.spec["components"]["schemas"].items():
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/openapi.json", response.text)

    def test_authentication_and_conditional_csrf_are_explicit(self):
        schemes = self.spec["components"]["securitySchemes"]
        self.assertEqual(schemes["SessionCookie"]["in"], "cookie")
        self.assertEqual(schemes["SessionCookie"]["name"], "rag_session")
        ask = self.spec["paths"]["/api/ask"]["post"]
        self.assertEqual(ask["security"], [{"SessionCookie": []}, {"BearerIdentity": []}])
        self.assertIn("401", ask["responses"])
        self.assertIn("403", ask["responses"])
        self.assertEqual(next(p for p in ask["parameters"] if p["name"] == "X-CSRF-Token")["required"], False)
        self.assertEqual(self.spec["paths"]["/api/auth/login"]["post"]["security"], [])

    def test_native_ask_optionality_and_queue_response_match_runtime(self):
        request = self.spec["components"]["schemas"]["AskRequest"]
        self.assertIn("question", request["required"])
        self.assertNotIn("chat_id", request["required"])
        result = {"answer": "Ответ", "flow_mode": "workspace", "sources": [{"file": "Методика.docx", "score": None}]}
        with patch("asgi_app.run_ask", return_value=result):
            response = self.client.post("/api/ask", json={"question": "Какие документы?"})
        self.assertEqual(response.status_code, 200)
        self.validate_response("/api/ask", "post", 200, response.json())
        pending = {"answer": "", "status": "running", "job_id": "job-1", "message_id": "message-1", "flow_mode": "queued", "from_cache": False, "elapsed_s": 1.0}
        with patch("asgi_app.run_ask", return_value=pending):
            response = self.client.post("/api/ask", json={"question": "Долгий вопрос"})
        self.assertEqual(response.status_code, 202)
        self.validate_response("/api/ask", "post", 202, response.json())

    def test_stream_is_documented_as_sse_including_error_events(self):
        response_contract = self.spec["paths"]["/api/ask/stream"]["post"]["responses"]["200"]
        self.assertEqual(set(response_contract["content"]), {"text/event-stream"})
        self.assertIn("error", self.spec["paths"]["/api/ask/stream"]["post"]["x-sse-events"])
        with patch("asgi_app.run_ask_stream", side_effect=RuntimeError("model unavailable")):
            response = self.client.post("/api/ask/stream", json={"question": "Вопрос"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: error", response.text)

    def test_upload_and_background_ingestion_document_actual_202(self):
        for path in ("/api/upload", "/api/jobs/ingestion"):
            self.assertIn("202", self.spec["paths"][path]["post"]["responses"])
            self.assertNotIn("200", self.spec["paths"][path]["post"]["responses"])
        with tempfile.TemporaryDirectory() as folder, \
             patch("asgi_app.UPLOAD_DIR", Path(folder)), \
             patch("asgi_app.create_document", return_value={"id": "document-1"}), \
             patch("asgi_app.update_document", return_value={}), \
             patch.object(asgi_app.jobs, "submit", new=AsyncMock(return_value={"id": "job-1"})):
            response = self.client.post("/api/upload", files={"files": ("тест.txt", b"test", "text/plain")})
        self.assertEqual(response.status_code, 202)
        self.validate_response("/api/upload", "post", 202, response.json())

    def test_original_download_is_binary_and_free_chat_is_supported(self):
        content = self.spec["paths"]["/api/documents/{document_id}/content"]["get"]["responses"]["200"]["content"]
        self.assertNotIn("application/json", content)
        schema = {"$ref": "#/components/schemas/ChatCreateWithProject", "components": self.spec["components"]}
        Draft202012Validator(schema).validate({"title": "Свободный чат"})
        self.assertIn("knowledge_scope", self.spec["components"]["schemas"]["ProjectCreate"]["properties"])

    def test_unscored_canonical_context_is_valid_without_an_invented_zero(self):
        schema = {"$ref": "#/components/schemas/ContextItem", "components": self.spec["components"]}
        context = {"file": "Методика.docx", "text": "Основное правило", "html_text": "<p>Основное правило</p>"}
        Draft202012Validator(schema).validate(context)
        Draft202012Validator(schema).validate({**context, "score": None})

    def test_legacy_invalid_json_uses_documented_native_error_envelope(self):
        response = self.client.post("/api/projects", content="{broken", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("detail", response.json())
        self.validate_response("/api/projects", "post", 400, response.json())


if __name__ == "__main__":
    unittest.main()
