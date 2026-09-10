import unittest
from unittest.mock import patch

import web_ui


class ModelDiscoveryTests(unittest.TestCase):
    def handler(self):
        handler = object.__new__(web_ui.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        return handler, captured

    def test_returns_discovered_models_when_bridge_is_available(self):
        handler, captured = self.handler()
        with patch.object(web_ui, "_agent", None), \
             patch.object(web_ui, "load_provider_config", return_value={"provider": "ollama", "model": "qwen-live"}), \
             patch.object(web_ui, "ollama_models", return_value=["qwen-live"]):
            handler.handle_models()

        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["payload"]["models"], ["qwen-live"])
        self.assertTrue(captured["payload"]["available"])
        self.assertNotIn("warning", captured["payload"])

    def test_keeps_configured_model_when_bridge_discovery_times_out(self):
        handler, captured = self.handler()
        with patch.object(web_ui, "_agent", None), \
             patch.object(web_ui, "load_provider_config", return_value={"provider": "ollama", "model": "qwen-live"}), \
             patch.object(web_ui, "ollama_models", side_effect=RuntimeError("temporary timeout")):
            handler.handle_models()

        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["payload"]["models"], ["qwen-live"])
        self.assertEqual(captured["payload"]["selected"], "qwen-live")
        self.assertFalse(captured["payload"]["available"])
        self.assertEqual(captured["payload"]["warning"], "temporary timeout")


if __name__ == "__main__":
    unittest.main()
