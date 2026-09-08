"""Contract tests for Python, Rust and hybrid flow routing."""
import unittest
from unittest.mock import patch

import web_ui


class FakeAgent:
    def __init__(self):
        self.calls = []

    def ask(self, question, prompt, prompt_id, retrieval_query=None, excluded_sources=None):
        self.calls.append(("ask", question))
        if retrieval_query is not None:
            self.calls.append(("retrieval_query", retrieval_query))
        if excluded_sources:
            self.calls.append(("excluded_sources", excluded_sources))
        return "python answer", [{"file": "python.docx", "section": "1", "score": 1, "text_preview": "evidence"}], False

    def retrieve_context(self, question, top_k, spec=None, excluded_sources=None):
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
    def invoke(self, mode, include_chat=True):
        handler = object.__new__(web_ui.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        agent = FakeAgent()
        with patch.object(web_ui, "load_flow_mode", return_value=mode), \
             patch.object(web_ui, "get_agent", return_value=agent), \
             patch.object(web_ui, "get_profile", return_value={"prompt": "test", "id": "test"}), \
             patch.object(web_ui, "get_workspace", return_value={"active_chat_id": "active-chat"}), \
             patch.object(web_ui, "get_project_conversation", return_value={"project_name": "test", "memory": {}, "messages": []}), \
             patch.object(web_ui, "validate_retrieval", return_value=[]), \
             patch.object(web_ui, "extract_formula_lines", return_value=[]), \
            patch.object(web_ui, "llm_label", return_value={"provider": "ollama", "model": "test", "label": "Ollama · test"}):
            if mode == "rust":
                with patch.object(web_ui, "rust_context", return_value=[context_item("rust")]), \
                     patch.object(web_ui, "rust_generate", return_value="rust answer") as rust:
                    handler.handle_ask({"question": "Что сказано?", "chat_id": "test-chat"})
                    self.assertEqual(rust.call_count, 1)
            elif mode == "hybrid":
                with patch.object(web_ui, "rust_context", return_value=[context_item("rust")]):
                    handler.handle_ask({"question": "Что сказано?", "chat_id": "test-chat"})
            else:
                body = {"question": "Что сказано?"}
                if include_chat:
                    body["chat_id"] = "test-chat"
                handler.handle_ask(body)
        return captured, agent

    def test_python_mode_uses_only_python_flow(self):
        result, agent = self.invoke("python")
        self.assertEqual(result["payload"]["answer"], "python answer")
        self.assertEqual(result["payload"]["flow_mode"], "python")
        self.assertIn(("ask", "Что сказано?"), agent.calls)

    def test_missing_chat_id_uses_active_workspace_chat(self):
        result, agent = self.invoke("python", include_chat=False)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["payload"]["answer"], "python answer")
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

    def test_qwen38_compatibility_provider_is_labeled_as_vllm(self):
        with patch.object(web_ui, "load_provider_config", return_value={
            "provider": "ollama",
            "model": "Qwen/Qwen3.8-27B",
        }):
            self.assertEqual(web_ui.llm_label()["label"], "vLLM · Qwen/Qwen3.8-27B")

    def test_follow_up_retrieval_uses_previous_user_turn_from_same_chat(self):
        scope = {"messages": [
            {"chat_id": "other", "role": "user", "content": "Что такое другой показатель?"},
            {"chat_id": "chat-1", "role": "user", "content": "Что такое ИВ0? Как рассчитывается?"},
            {"chat_id": "chat-1", "role": "assistant", "content": "Ответ"},
            {"chat_id": "chat-1", "role": "user", "content": "А по какой формуле?"},
        ]}
        query = web_ui.contextualize_retrieval_question("А по какой формуле?", scope, "chat-1")
        self.assertIn("Что такое ИВ0?", query)
        self.assertNotIn("другой показатель", query)

    def test_standalone_retrieval_question_is_not_expanded(self):
        question = "Объясни порядок расчета ИВ0 по шагам"
        self.assertEqual(web_ui.contextualize_retrieval_question(question, {"messages": []}, "chat-1"), question)

    def test_project_scope_excludes_documents_from_other_projects_and_chats(self):
        documents = [
            {"source_path": "C:/uploads/global.pdf", "project_id": None, "chat_id": None},
            {"source_path": "C:/uploads/current.pdf", "project_id": "project-1", "chat_id": "chat-1"},
            {"source_path": "C:/uploads/same-project.pdf", "project_id": "project-1", "chat_id": "chat-2"},
            {"source_path": "C:/uploads/private-chat.pdf", "project_id": None, "chat_id": "chat-1"},
            {"source_path": "C:/uploads/other-project.pdf", "project_id": "project-2", "chat_id": "chat-9"},
            {"source_path": "C:/uploads/other-chat.pdf", "project_id": None, "chat_id": "chat-9"},
        ]
        with patch.object(web_ui, "list_documents", return_value=documents):
            excluded = web_ui.excluded_document_sources({"project_id": "project-1"}, "chat-1")
        self.assertEqual(excluded, {"other-project.pdf", "other-chat.pdf"})

    def test_scoped_context_filter_uses_source_basename(self):
        items = [
            {"file": "C:/uploads/current.pdf"},
            {"file": "C:/uploads/other.pdf"},
        ]
        self.assertEqual(
            web_ui.filter_scoped_context(items, {"other.pdf"}),
            [{"file": "C:/uploads/current.pdf"}],
        )


if __name__ == "__main__":
    unittest.main()
