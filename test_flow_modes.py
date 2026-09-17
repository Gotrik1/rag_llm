"""Contract tests for Python, Rust and hybrid flow routing."""
import unittest
from unittest.mock import patch

import web_ui


class FakeAgent:
    def __init__(self):
        self.calls = []
        self._last_retrieved_context = None

    def ask(
        self,
        question,
        prompt,
        prompt_id,
        retrieval_query=None,
        excluded_sources=None,
        shared_cache_sources=None,
        shared_system_prompt=None,
        source_aliases=None,
        priority_sources=None,
    ):
        self.calls.append(("ask", question))
        if retrieval_query is not None:
            self.calls.append(("retrieval_query", retrieval_query))
        if excluded_sources:
            self.calls.append(("excluded_sources", excluded_sources))
        self.calls.append(("source_scope", source_aliases, priority_sources))
        self.calls.append(("shared_cache", shared_cache_sources, shared_system_prompt))
        self._last_retrieved_context = [context_item("python")]
        return "python answer", [{"file": "python.docx", "section": "1", "score": 1, "text_preview": "evidence"}], False

    def last_retrieved_context(self):
        if self._last_retrieved_context is None:
            return None
        return [dict(item) for item in self._last_retrieved_context]

    def retrieve_context(
        self,
        question,
        top_k,
        spec=None,
        excluded_sources=None,
        source_aliases=None,
        priority_sources=None,
    ):
        self.calls.append(("retrieve", question))
        return [context_item("python")]

    def _generate_grounded_answer(self, question, context, calculations, spec, prompt, cache_enabled=False):
        self.calls.append(("generate", context, cache_enabled, prompt))
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
             patch.object(web_ui, "retrieval_document_scope", return_value=(set(), {})), \
             patch.object(web_ui, "shared_global_document_sources", return_value={"python.docx", "rust.docx"}), \
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
        self.assertFalse(any(call[0] == "retrieve" for call in agent.calls))
        self.assertEqual(result["payload"]["context"][0]["file"], "python.docx")

    def test_missing_chat_id_uses_active_workspace_chat(self):
        result, agent = self.invoke("python", include_chat=False)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["payload"]["answer"], "python answer")
        self.assertIn(("ask", "Что сказано?"), agent.calls)

    def test_document_inventory_question_uses_registry_without_retrieval(self):
        handler = object.__new__(web_ui.Handler)
        captured = {}
        handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
        documents = [{
            "id": "doc-1",
            "file_name": "Регламент v2.0.docx",
            "status": "ready",
            "created_at": "2026-09-09T08:15:00Z",
        }]
        with patch.object(
            web_ui,
            "get_project_conversation",
            return_value={"project_id": "project-1", "project_name": "ТЭС", "memory": {}, "messages": []},
        ), patch.object(web_ui, "list_documents", return_value=documents) as list_documents, patch.object(
            web_ui, "get_agent"
        ) as get_agent:
            handler.handle_ask({"question": "Какие регламенты у нас есть?", "chat_id": "chat-1"})

        self.assertEqual(captured["status"], 200)
        self.assertIn("Регламент v2.0.docx", captured["payload"]["answer"])
        self.assertIn("09.09.2026 08:15 UTC", captured["payload"]["answer"])
        self.assertEqual(captured["payload"]["flow_mode"], "workspace")
        list_documents.assert_called_once_with("project-1", None)
        get_agent.assert_not_called()

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
        self.assertTrue(agent.calls[1][2])
        self.assertEqual(agent.calls[1][3], "test")

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

    def test_short_question_with_explicit_price_category_is_not_a_follow_up(self):
        question = "Как рассчитывается 1 ЦК?"
        scope = {"messages": [
            {"chat_id": "chat-1", "role": "user", "content": "Что такое ИВ0?"},
        ]}
        self.assertEqual(web_ui.contextualize_retrieval_question(question, scope, "chat-1"), question)

    def test_shared_sources_exclude_private_and_ambiguous_names(self):
        documents = [
            {"source_path": "C:/uploads/global.pdf", "file_name": "Регламент.pdf", "owner_subject": None, "status": "ready"},
            {"source_path": "C:/uploads/private.pdf", "file_name": "Личный.pdf", "owner_subject": "user-1", "status": "ready"},
            {"source_path": "C:/global/shared.pdf", "file_name": "Одинаковый.pdf", "owner_subject": None, "status": "ready"},
            {"source_path": "C:/private/other.pdf", "file_name": "Одинаковый.pdf", "owner_subject": "user-2", "status": "ready"},
            {"source_path": "C:/uploads/deleted.pdf", "owner_subject": None, "status": "deleting", "deleted_at": "2026-09-10"},
        ]
        with patch.object(web_ui, "list_all_documents_for_retrieval", return_value=documents):
            sources = web_ui.shared_global_document_sources()
        self.assertIn("global.pdf", sources)
        self.assertIn("регламент.pdf", sources)
        self.assertNotIn("private.pdf", sources)
        self.assertNotIn("личный.pdf", sources)
        self.assertNotIn("одинаковый.pdf", sources)
        self.assertNotIn("deleted.pdf", sources)

    def test_context_is_shared_only_when_every_chunk_is_global(self):
        self.assertTrue(web_ui.context_is_shared_global(
            [{"file": "C:/uploads/global.pdf"}], {"global.pdf"}
        ))
        self.assertFalse(web_ui.context_is_shared_global(
            [{"file": "global.pdf"}, {"file": "private.pdf"}], {"global.pdf"}
        ))

    def test_project_scope_excludes_documents_from_other_projects_and_chats(self):
        documents = [
            {"source_path": "C:/uploads/global.pdf", "project_id": None, "chat_id": None, "owner_subject": None, "status": "ready"},
            {"source_path": "C:/uploads/current.pdf", "project_id": "project-1", "chat_id": "chat-1", "owner_subject": "user-1", "status": "ready"},
            {"source_path": "C:/uploads/same-project.pdf", "project_id": "project-1", "chat_id": "chat-2", "owner_subject": "user-1", "status": "ready"},
            {"source_path": "C:/uploads/private-chat.pdf", "project_id": None, "chat_id": "chat-1", "owner_subject": "user-1", "status": "ready"},
            {"source_path": "C:/uploads/other-project.pdf", "project_id": "project-2", "chat_id": "chat-9", "owner_subject": "user-2", "status": "ready"},
            {"source_path": "C:/uploads/other-chat.pdf", "project_id": None, "chat_id": "chat-9", "owner_subject": "user-2", "status": "ready"},
            {"source_path": "C:/uploads/deleted.pdf", "project_id": "project-1", "chat_id": None, "owner_subject": "user-1", "status": "deleting", "deleted_at": "2026-09-10"},
            {"source_path": "C:/uploads/failed.pdf", "project_id": "project-1", "chat_id": None, "owner_subject": "user-1", "status": "failed"},
        ]
        with patch.object(web_ui, "list_all_documents_for_retrieval", return_value=documents):
            excluded = web_ui.excluded_document_sources(
                {"project_id": "project-1", "owner_subject": "user-1", "knowledge_scope": "project_and_global"},
                "chat-1",
            )
        self.assertEqual(excluded, {
            "same-project.pdf", "other-project.pdf", "other-chat.pdf", "deleted.pdf", "failed.pdf",
        })

        with patch.object(web_ui, "list_all_documents_for_retrieval", return_value=documents):
            project_only = web_ui.excluded_document_sources(
                {"project_id": "project-1", "owner_subject": "user-1", "knowledge_scope": "project_only"},
                "chat-1",
            )
        self.assertIn("global.pdf", project_only)

    def test_scope_uses_unique_upload_source_when_display_names_collide(self):
        documents = [
            {
                "source_path": "C:/uploads/visible-id_ПП 442.md",
                "file_name": "ПП 442.md",
                "project_id": "project-1",
                "chat_id": None,
                "owner_subject": "user-1",
                "status": "ready",
            },
            {
                "source_path": "C:/uploads/hidden-id_ПП 442.md",
                "file_name": "ПП 442.md",
                "project_id": "project-2",
                "chat_id": None,
                "owner_subject": "user-2",
                "status": "ready",
            },
        ]
        with patch.object(web_ui, "list_all_documents_for_retrieval", return_value=documents):
            excluded, aliases = web_ui.retrieval_document_scope(
                {"project_id": "project-1", "owner_subject": "user-1", "knowledge_scope": "project_only"},
                "chat-1",
            )

        self.assertEqual(excluded, {"hidden-id_пп 442.md"})
        self.assertEqual(aliases, {"visible-id_пп 442.md": "ПП 442.md"})

    def test_visible_source_wins_over_stale_deleted_duplicate_of_same_upload(self):
        source_path = "C:/uploads/source-id_Постановление 1179.docx"
        documents = [
            {
                "source_path": source_path,
                "file_name": "Постановление 1179.docx",
                "project_id": None,
                "chat_id": None,
                "owner_subject": None,
                "status": "ready",
                "deleted_at": None,
            },
            {
                "source_path": source_path,
                "file_name": "Постановление 1179.docx",
                "project_id": None,
                "chat_id": None,
                "owner_subject": None,
                "status": "deleting",
                "deleted_at": "2026-09-10",
            },
        ]
        with patch.object(web_ui, "list_all_documents_for_retrieval", return_value=documents):
            excluded, aliases = web_ui.retrieval_document_scope(
                {"project_id": None, "owner_subject": "user-1", "knowledge_scope": "project_and_global"},
                "chat-1",
            )

        source_id = "source-id_постановление 1179.docx"
        self.assertNotIn(source_id, excluded)
        self.assertEqual(aliases[source_id], "Постановление 1179.docx")

    def test_scope_exposes_visible_document_version_status(self):
        documents = [
            {
                "source_path": "C:/uploads/old_Методика v1.docx",
                "file_name": "Методика v1.docx",
                "project_id": None,
                "chat_id": None,
                "owner_subject": None,
                "status": "ready",
            },
            {
                "source_path": "C:/uploads/new_Методика v2.docx",
                "file_name": "Методика v2.docx",
                "project_id": None,
                "chat_id": None,
                "owner_subject": None,
                "status": "ready",
            },
        ]
        with patch.object(web_ui, "list_all_documents_for_retrieval", return_value=documents):
            scope = web_ui.retrieval_document_scope(
                {"project_id": None, "owner_subject": "user-1", "knowledge_scope": "project_and_global"},
                "chat-1",
            )

        self.assertEqual(scope.source_versions["old_методика v1.docx"], "superseded")
        self.assertEqual(scope.source_versions["new_методика v2.docx"], "latest")

    def test_scope_registry_failure_is_fail_closed(self):
        with patch.object(
            web_ui,
            "list_all_documents_for_retrieval",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "scope is unavailable"):
                web_ui.retrieval_document_scope(
                    {"project_id": "project-1", "owner_subject": "user-1"},
                    "chat-1",
                )

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
