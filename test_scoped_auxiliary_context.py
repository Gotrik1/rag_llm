"""Auxiliary Rust evidence must obey the same visible-source scope as Python."""
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import web_ui
from test_flow_modes import FakeAgent, context_item


class AuxiliarySourceScopeTests(unittest.TestCase):
    def test_allowlist_applies_without_any_exclusions(self):
        visible = {"file": "/uploads/visible-id_manual.docx"}
        hidden = {"file": "/uploads/hidden-id_manual.docx"}
        self.assertEqual(
            web_ui.filter_scoped_context([visible, hidden], set(), {"visible-id_manual.docx"}),
            [visible],
        )

    def test_explicit_empty_allowlist_permits_no_sources(self):
        self.assertEqual(web_ui.filter_scoped_context([{"file": "manual.docx"}], set(), set()), [])

    def test_absent_allowlist_preserves_legacy_unrestricted_contract(self):
        items = [{"file": "manual.docx"}]
        self.assertIs(web_ui.filter_scoped_context(items, set(), None), items)
        self.assertEqual(web_ui.filter_scoped_context(items, {"other.docx"}, None), items)

    def test_exclusion_wins_even_when_source_is_allowlisted(self):
        self.assertEqual(
            web_ui.filter_scoped_context([{"file": "manual.docx"}], {"manual.docx"}, {"manual.docx"}),
            [],
        )

    def test_revision_and_upload_ids_do_not_collapse_to_display_filename(self):
        visible = {"file": "C:/uploads/visible-id_Manual_v2.docx"}
        other_upload = {"file": "C:/uploads/hidden-id_Manual_v2.docx"}
        other_revision = {"file": "C:/uploads/visible-id_Manual_v1.docx"}
        self.assertEqual(
            web_ui.filter_scoped_context(
                [visible, other_upload, other_revision], set(), {"visible-id_manual_v2.docx"},
            ),
            [visible],
        )

    def test_rust_and_hybrid_generation_drop_unknown_auxiliary_sources(self):
        for mode in ("rust", "hybrid"):
            with self.subTest(mode=mode), ExitStack() as stack:
                handler = object.__new__(web_ui.Handler)
                captured = {}
                handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
                agent = FakeAgent()
                visible = {**context_item("rust"), "file": "visible-id_manual.docx"}
                hidden = {**context_item("rust"), "file": "hidden-id_manual.docx"}
                aliases = {"python.docx": "python.docx", "visible-id_manual.docx": "manual.docx"}
                values = {
                    "load_flow_mode": mode,
                    "load_provider_config": {"provider": "ollama", "model": "test"},
                    "load_system_prompt_id": "test",
                    "get_agent": agent,
                    "get_profile": {"prompt": "test", "id": "test"},
                    "get_project_conversation": {"project_name": "test", "memory": {}, "messages": []},
                    "retrieval_document_scope": (set(), aliases),
                    "shared_global_document_sources": set(aliases),
                    "rust_context": [visible, hidden],
                    "rust_generate": ("rust answer", False),
                    "validate_retrieval": [],
                    "requested_response_formulas": [],
                    "extract_formula_lines": [],
                    "llm_label": {"provider": "ollama", "model": "test", "label": "test"},
                }
                for name, value in values.items():
                    stack.enter_context(patch.object(web_ui, name, return_value=value))
                stack.enter_context(patch.object(web_ui, "filter_rust_context", side_effect=lambda _q, items: items))
                stack.enter_context(patch.object(web_ui, "enrich_source_links", side_effect=lambda items: items))
                stack.enter_context(patch.object(web_ui, "enrich_answer_source_citations", side_effect=lambda answer, _s: answer))
                handler.handle_ask({"question": "Что сказано?", "chat_id": "chat-1"})
                self.assertEqual(captured["status"], 200)
                sources = {item["file"] for item in captured["payload"]["context"]}
                self.assertIn("visible-id_manual.docx", sources)
                self.assertNotIn("hidden-id_manual.docx", sources)


if __name__ == "__main__":
    unittest.main()
