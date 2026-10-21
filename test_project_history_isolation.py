"""Chat-private history must not bypass the retrieval source boundary."""
import json

import pytest

import web_ui


PRIVATE_FACT = "SYNTHETIC_CHAT_A_PRIVATE_DOCUMENT_FACT"
PRIOR_MODEL_CLAIM = "SYNTHETIC_UNTRUSTED_PRIOR_MODEL_CLAIM"
CURRENT_USER_INTENT = "Как проверить калибровку датчика?"
PROJECT_NOTE = "SYNTHETIC_EXPLICITLY_SHARED_PROJECT_NOTE"


def project_scope():
    return {
        "project_id": "project-1", "owner_subject": "user-1",
        "project_name": "Current project", "knowledge_scope": "project_and_global",
        "memory": {"text": PROJECT_NOTE},
        "messages": [
            {"chat_id": "chat-a", "chat_title": "Private A", "role": "user", "content": PRIVATE_FACT},
            {"chat_id": "chat-a", "chat_title": "Private A", "role": "assistant", "content": PRIVATE_FACT},
            {"chat_id": "chat-b", "chat_title": "Current B", "role": "user", "content": CURRENT_USER_INTENT},
            {"chat_id": "chat-b", "chat_title": "Current B", "role": "assistant", "content": PRIOR_MODEL_CLAIM},
            {"chat_id": "chat-b", "role": "system", "content": "SYNTHETIC_PERSISTED_SYSTEM_ASSERTION"},
            {"role": "user", "content": "SYNTHETIC_MISSING_CHAT_ID"},
        ],
    }


def document_registry():
    return [
        {"source_path": "uuid-global.md", "file_name": "Global.md", "owner_subject": None,
         "project_id": None, "chat_id": None, "status": "ready"},
        {"source_path": "uuid-project.md", "file_name": "Project.md", "owner_subject": "user-1",
         "project_id": "project-1", "chat_id": None, "status": "ready"},
        {"source_path": "uuid-chat-b.md", "file_name": "Current chat.md", "owner_subject": "user-1",
         "project_id": "project-1", "chat_id": "chat-b", "status": "ready"},
        {"source_path": "uuid-chat-a.md", "file_name": "Private.md", "owner_subject": "user-1",
         "project_id": "project-1", "chat_id": "chat-a", "status": "ready"},
        {"source_path": "uuid-foreign.md", "file_name": "Foreign.md", "owner_subject": "user-2",
         "project_id": "project-1", "chat_id": None, "status": "ready"},
    ]


def test_project_notes_and_current_user_intent_remain_without_peer_chat_or_model_assertions():
    scope = project_scope()
    prompt = web_ui.project_context_prompt(scope, "chat-b")
    assert PROJECT_NOTE in prompt
    assert CURRENT_USER_INTENT in prompt
    assert PRIVATE_FACT not in prompt
    assert PRIOR_MODEL_CLAIM not in prompt
    assert "SYNTHETIC_PERSISTED_SYSTEM_ASSERTION" not in prompt
    assert "SYNTHETIC_MISSING_CHAT_ID" not in prompt
    assert "намерения пользователя, а не подтверждённые факты" in prompt
    # The shared source scope is not rewritten or destructively filtered in place.
    assert len(scope["messages"]) == 6


@pytest.mark.parametrize("chat_id", ["", None, "unknown-chat"])
def test_missing_or_unknown_chat_identity_fails_closed_for_history_but_keeps_notes(chat_id):
    prompt = web_ui.project_context_prompt(project_scope(), chat_id)
    assert PROJECT_NOTE in prompt
    assert CURRENT_USER_INTENT not in prompt
    assert PRIVATE_FACT not in prompt
    assert PRIOR_MODEL_CLAIM not in prompt


def test_refresh_reconstructs_the_same_history_boundary():
    scope = project_scope()
    restored = json.loads(json.dumps(scope))
    assert web_ui.project_context_prompt(scope, "chat-b") == web_ui.project_context_prompt(restored, "chat-b")


class SpyAgent:
    def __init__(self):
        self.prompts = []
        self.source_options = []
        self.last_generation_from_cache = False

    @staticmethod
    def context():
        return [{"file": "uuid-project.md", "score": 1.0, "text": "Доступный раздел проекта.",
                 "section_path": "Калибровка", "status": "active", "chunk_type": "unknown"}]

    def ask(self, question, prompt, prompt_id, **options):
        self.prompts.append(prompt)
        self.source_options.append(options)
        return "Current answer", [{"file": "uuid-project.md", "section": "Калибровка", "score": 1.0}], False

    def last_retrieved_context(self):
        return self.context()

    def retrieve_context(self, question, top_k, **options):
        self.source_options.append(options)
        return self.context()

    def _generate_grounded_answer(self, question, context, calculations, spec, prompt, **options):
        self.prompts.append(prompt)
        return "Current answer"


@pytest.mark.parametrize("mode", ["python", "rust", "hybrid"])
def test_every_handler_flow_keeps_chat_private_history_out_of_generation(monkeypatch, mode):
    agent = SpyAgent()
    captured = {}
    handler = object.__new__(web_ui.Handler)
    handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)

    def rust_generate(question, context, prompt, **options):
        agent.prompts.append(prompt)
        return "Current answer", False

    replacements = {
        "get_agent": lambda: agent,
        "load_flow_mode": lambda: mode,
        "get_profile": lambda *args: {"id": "test", "prompt": "Ground answers in authorized documents."},
        "load_system_prompt_id": lambda: "test",
        "get_project_conversation": lambda chat_id: project_scope(),
        "list_all_documents_for_retrieval": document_registry,
        "validate_retrieval": lambda *args: [],
        "requested_response_formulas": lambda *args: [],
        "extract_formula_lines": lambda *args: [],
        "enrich_source_links": lambda sources: sources,
        "enrich_answer_source_citations": lambda answer, sources: answer,
        "select_user_facing_sources": lambda answer, sources: sources,
        "usage_stats": lambda *args: {},
        "render_text": lambda value: value,
        "rust_context": lambda *args: [],
        "rust_generate": rust_generate,
        "llm_label": lambda *args: {"provider": "test", "model": "fake", "label": "Fake model"},
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(web_ui, name, replacement)

    handler.handle_ask({"question": "Как проверяется точность датчика?", "chat_id": "chat-b"})
    assert captured["status"] == 200
    assert agent.prompts
    for prompt in agent.prompts:
        assert PROJECT_NOTE in prompt
        assert CURRENT_USER_INTENT in prompt
        assert PRIVATE_FACT not in prompt
        assert PRIOR_MODEL_CLAIM not in prompt
    for options in agent.source_options:
        assert set(options["source_aliases"]) == {"uuid-global.md", "uuid-project.md", "uuid-chat-b.md"}
        assert {"uuid-chat-a.md", "uuid-foreign.md"} <= set(options["excluded_sources"])
    assert PRIVATE_FACT not in json.dumps(captured["payload"])
    assert PRIOR_MODEL_CLAIM not in json.dumps(captured["payload"])
