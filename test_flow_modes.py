"""Contract tests for Python, Rust and hybrid flow routing."""
import unittest
from unittest.mock import patch

import web_ui


class FakeAgent:
    def __init__(self):
        self.calls = []

    def ask(self, question, prompt, prompt_id):
        self.calls.append(("ask", question))
        return "python answer", [{"file": "python.docx", "section": "1", "score": 1, "text_preview": "evidence"}], False

    def retrieve_context(self, question, top_k):
        self.calls.append(("retrieve", question))
        return [context_item("python")]

    def _generate_grounded_answer(self, question, context, calculations, spec, prompt):
        self.calls.append(("generate", context))
        return "hybrid answer"

    def clear_cache(self):
        self.calls.append(("clear_cache",))


def context_item(prefix):
    return {"file": f"{prefix}.docx", "score": 1.0, "text": "Для создания параметра выберите группу", "section_path": "Создание параметра", "status": "active", "chunk_type": "unknown"}


class FlowModeTests(unittest.TestCase):
    def invoke(self, mode):
        handler = object.__new__(web_ui.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        agent = FakeAgent()
        with patch.object(web_ui, "load_flow_mode", return_value=mode), \
             patch.object(web_ui, "get_agent", return_value=agent), \
             patch.object(web_ui, "get_profile", return_value={"prompt": "test", "id": "test"}), \
             patch.object(web_ui, "validate_retrieval", return_value=[]), \
             patch.object(web_ui, "extract_formula_lines", return_value=[]), \
            patch.object(web_ui, "llm_label", return_value={"provider": "ollama", "model": "test", "label": "Ollama · test"}):
            if mode == "rust":
                with patch.object(web_ui, "rust_context", return_value=[context_item("rust")]), \
                     patch.object(web_ui, "rust_generate", return_value="rust answer") as rust:
                    handler.handle_ask({"question": "Что сказано?"})
                    self.assertEqual(rust.call_count, 1)
            elif mode == "hybrid":
                with patch.object(web_ui, "rust_context", return_value=[context_item("rust")]):
                    handler.handle_ask({"question": "Что сказано?"})
            else:
                handler.handle_ask({"question": "Что сказано?"})
        return captured, agent

    def test_python_mode_uses_only_python_flow(self):
        result, agent = self.invoke("python")
        self.assertEqual(result["payload"]["answer"], "python answer")
        self.assertEqual(result["payload"]["flow_mode"], "python")
        self.assertIn(("ask", "Что сказано?"), agent.calls)

    def test_rust_mode_uses_rust_answer(self):
        result, agent = self.invoke("rust")
        self.assertEqual(result["payload"]["answer"], "rust answer")
        self.assertEqual(result["payload"]["flow_mode"], "rust")
        self.assertEqual(agent.calls[0][0], "retrieve")

    def test_hybrid_uses_rust_context_and_python_generation(self):
        result, agent = self.invoke("hybrid")
        self.assertEqual(result["payload"]["answer"], "hybrid answer")
        self.assertEqual(result["payload"]["flow_mode"], "hybrid")
        self.assertEqual([call[0] for call in agent.calls], ["retrieve", "generate"])
        generated_context = agent.calls[1][1]
        self.assertEqual(generated_context[0]["file"], "python.docx")
        self.assertTrue(any(item["file"] == "rust.docx" for item in generated_context))

    def test_fusion_prefers_current_python_index(self):
        python_items = [context_item(f"python-{index}") for index in range(10)]
        rust_items = [context_item(f"rust-{index}") for index in range(10)]
        fused = web_ui.fuse_contexts(python_items, rust_items, 10)
        self.assertEqual(sum(item["flow_origin"] == "python" for item in fused), 8)
        self.assertEqual(sum(item["flow_origin"] == "rust" for item in fused), 2)

    def test_irrelevant_rust_regulation_is_rejected_for_parameter_creation(self):
        items = [{"section_path": "Регламент", "text": "Расчет стоимостного параметра внешней инициативы"}]
        self.assertEqual(web_ui.filter_rust_context("как добавить параметр?", items), [])

    def test_switch_clears_shared_answer_cache(self):
        handler = object.__new__(web_ui.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        agent = FakeAgent()
        with patch.object(web_ui, "load_flow_mode", return_value="python"), \
             patch.object(web_ui, "get_agent", return_value=agent), \
             patch.object(web_ui, "save_flow_mode") as save:
            handler.handle_flow_mode({"mode": "hybrid"})
        self.assertIn(("clear_cache",), agent.calls)
        self.assertTrue(captured["payload"]["cache_cleared"])
        save.assert_called_once_with("hybrid")


if __name__ == "__main__":
    unittest.main()
