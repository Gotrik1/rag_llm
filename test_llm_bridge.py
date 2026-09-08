"""Contract tests for the Ollama-compatible vLLM bridge."""
import importlib.util
from pathlib import Path
import unittest


BRIDGE_PATH = Path(__file__).parent / "deploy" / "llm-bridge" / "app.py"
SPEC = importlib.util.spec_from_file_location("rag_llm_bridge", BRIDGE_PATH)
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class LlmBridgeTests(unittest.TestCase):
    def test_qwen38_disables_thinking_even_when_client_omits_flag(self):
        payload = bridge._openai_payload({"prompt": "test", "options": {}}, False)
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})


if __name__ == "__main__":
    unittest.main()
