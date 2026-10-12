"""Contract tests for the Ollama-compatible vLLM bridge."""
import importlib.util
from pathlib import Path
import unittest
import inspect
from unittest.mock import Mock, patch

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


if __name__ == "__main__":
    unittest.main()
