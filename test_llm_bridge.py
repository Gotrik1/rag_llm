"""Contract tests for the Ollama-compatible vLLM bridge."""
import importlib.util
from pathlib import Path
import unittest
import inspect
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException


BRIDGE_PATH = Path(__file__).parent / "deploy" / "llm-bridge" / "app.py"
SPEC = importlib.util.spec_from_file_location("rag_llm_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class LlmBridgeTests(unittest.TestCase):
    def test_cpu_embeddings_are_not_event_loop_handlers(self):
        self.assertFalse(inspect.iscoroutinefunction(bridge.embed))
        self.assertFalse(inspect.iscoroutinefunction(bridge.embeddings))

    def test_embedding_batch_memory_is_bounded(self):
        vector = Mock()
        vector.tolist.return_value = [0.1, 0.2]
        model = Mock()
        model.embed.return_value = iter([vector])
        with patch.object(bridge, '_embedder', return_value=model):
            self.assertEqual(bridge.embed({'input': ['text']})['embeddings'], [[0.1, 0.2]])
        model.embed.assert_called_once_with(['text'], batch_size=bridge.EMBED_BATCH_SIZE)

    def test_unlimited_ollama_output_does_not_become_invalid_vllm_limit(self):
        for options in ({}, {"num_predict": -1}, {"num_predict": 0}):
            self.assertNotIn("max_tokens", bridge._openai_payload({"prompt": "test", "options": options}, False))

    def test_explicit_operator_output_limit_is_preserved(self):
        self.assertEqual(bridge._openai_payload({"options": {"num_predict": 9000}}, False)["max_tokens"], 9000)

    def test_qwen38_disables_thinking_even_when_client_omits_flag(self):
        payload = bridge._openai_payload({"prompt": "test", "options": {}}, False)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})

    def test_source_reading_thinking_requires_operator_opt_in(self):
        stage = {"purpose": "source-reading", "think": True,
                 "options": {"num_predict": 4096, "reasoning_effort": "low"}}
        with patch.object(bridge, "LLM_MODEL", "Qwen/Qwen3.8-27B"):
            with patch.dict(bridge.os.environ, {"RAG_ALLOW_PLANNER_THINKING": "false"}):
                payload = bridge._openai_payload(stage, False)
                self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
            with patch.dict(bridge.os.environ, {"RAG_ALLOW_PLANNER_THINKING": "true"}):
                payload = bridge._openai_payload(stage, False)
                self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": True})
                self.assertEqual(payload["reasoning_effort"], "low")
                self.assertEqual(payload["max_tokens"], 4096)
                answer = bridge._openai_payload({"purpose": "answer", "think": True}, False)
                self.assertEqual(answer["chat_template_kwargs"], {"enable_thinking": False})
                self.assertNotIn("max_tokens", answer)

    def test_rerank_rejects_runtime_model_switch(self):
        with self.assertRaises(HTTPException) as raised:
            bridge.rerank({
                "query": "test",
                "documents": ["document"],
                "model": "unconfigured-model",
            })
        self.assertEqual(raised.exception.status_code, 400)

    def test_rerank_returns_one_score_per_document(self):
        model = Mock()
        model.rerank.return_value = iter([0.25, 0.75])
        with patch.object(bridge, "_reranker", return_value=model):
            result = bridge.rerank({
                "query": "test",
                "documents": ["first", "second"],
                "model": bridge.RERANKER_MODEL,
            })
        self.assertEqual(result["scores"], [0.25, 0.75])


class LlmBridgeInternalStageRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _route_result(self, handler, payload, content, finish_reason):
        response = httpx.Response(200, json={"choices": [{
            "message": {"content": content}, "finish_reason": finish_reason,
        }]})
        client = Mock(aclose=AsyncMock())
        response.extensions["bridge_client"] = client
        with patch.object(bridge, "_upstream", new=AsyncMock(return_value=response)) as upstream:
            result = await handler(payload)
        upstream.assert_awaited_once_with(payload, stream=False)
        client.aclose.assert_awaited_once_with()
        return result

    async def test_source_reading_rejects_parseable_json_when_openai_output_is_truncated(self):
        content = '{"readings": []}'
        for handler, field in ((bridge.generate, "response"), (bridge.chat, "message")):
            with self.subTest(route=handler.__name__):
                result = await self._route_result(handler, {"purpose": "source-reading"}, content, "length")
                actual = result[field] if field == "response" else result[field]["content"]
                self.assertEqual(actual, "")

    async def test_source_reading_accepts_complete_openai_output(self):
        content = '{"readings": []}'
        for handler, field in ((bridge.generate, "response"), (bridge.chat, "message")):
            with self.subTest(route=handler.__name__):
                result = await self._route_result(handler, {"purpose": "source-reading"}, content, "stop")
                actual = result[field] if field == "response" else result[field]["content"]
                self.assertEqual(actual, content)

    async def test_final_answer_remains_outside_internal_stage_truncation_policy(self):
        content = "The final answer is retained."
        for handler, field in ((bridge.generate, "response"), (bridge.chat, "message")):
            with self.subTest(route=handler.__name__):
                payload = {"purpose": "answer", "options": {}}
                self.assertNotIn("max_tokens", bridge._openai_payload(payload, False))
                result = await self._route_result(handler, payload, content, "length")
                actual = result[field] if field == "response" else result[field]["content"]
                self.assertEqual(actual, content)


if __name__ == "__main__":
    unittest.main()
