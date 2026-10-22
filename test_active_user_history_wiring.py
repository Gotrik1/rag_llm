"""The generator, retrieval resolver and diagnostics consume the same chat window."""
import json

import pytest

import web_ui
from test_project_history_isolation import SpyAgent, document_registry


QUESTION = "А по какой формуле?"
ANCHOR = "Как рассчитывается время работы измерительного прибора?"


def user(content, *, chat_id="chat-b", identifier="u1", role="user"):
    return {"id": identifier, "chat_id": chat_id, "content": content, "role": role}


def scope():
    return {
        "project_id": "project-1", "project_name": "Project", "memory": {"text": "Shared note"},
        "messages": [user("OTHER_PROJECT_CHAT_CONTEXT", chat_id="chat-a")],
        "active_user_messages": [user(ANCHOR)],
        "active_user_history_limit": 40, "active_user_history_truncated": False,
    }


def test_active_chat_window_survives_a_busy_project_feed():
    current = scope()
    result = web_ui.request_conversation_context(QUESTION, current, "chat-b")
    assert result.subject == ANCHOR
    assert result.source_message_ids == ("u1",)
    prompt = web_ui.project_context_prompt(current, "chat-b")
    assert ANCHOR in prompt and "Shared note" in prompt
    assert "OTHER_PROJECT_CHAT_CONTEXT" not in prompt
    assert web_ui.contextualize_retrieval_question(QUESTION, current, "chat-b") == result.retrieval_question
    restored = json.loads(json.dumps(current))
    assert web_ui.request_conversation_context(QUESTION, restored, "chat-b") == result


@pytest.mark.parametrize("active", [[], None, {}, "invalid"])
def test_present_empty_or_invalid_window_never_falls_back_to_project_messages(active):
    current = scope()
    current.update(messages=[user("DO_NOT_REPLENISH_FROM_OLD_FEED")], active_user_messages=active)
    assert web_ui.current_chat_user_messages(current, "chat-b") == []
    assert web_ui.request_conversation_context(QUESTION, current, "chat-b").mode == "unresolved"
    assert "DO_NOT_REPLENISH" not in web_ui.project_context_prompt(current, "chat-b")


def test_active_window_defensively_rejects_other_roles_chats_and_untyped_items():
    current = scope()
    current["active_user_messages"].extend([
        user("OTHER_CHAT", chat_id="chat-a"), user("MODEL_CLAIM", role="assistant"),
        {"role": "user", "content": "NO_CHAT_ID"}, None, "not a message",
    ])
    assert web_ui.current_chat_user_messages(current, "chat-b") == [user(ANCHOR)]
    assert web_ui.current_chat_user_messages(current, "") == []


def test_legacy_scope_remains_supported_without_new_database_fields():
    assert web_ui.current_chat_user_messages({"messages": [user(ANCHOR)]}, "chat-b") == [user(ANCHOR)]


def test_truncated_history_is_not_presented_as_complete_to_writer():
    current = scope()
    current["active_user_history_truncated"] = True
    prompt = web_ui.project_context_prompt(current, "chat-b")
    assert "Не считай её полной" in prompt
    assert ANCHOR in prompt


def test_writer_budget_omissions_are_visible_even_when_database_window_is_complete():
    current = scope()
    current["active_user_messages"] = [user(ANCHOR, identifier="anchor")] + [
        user(str(index) + "x" * 1700, identifier=f"m{index}") for index in range(9)]
    entries, budget = web_ui._writer_history(current, "chat-b")
    assert len(entries) < 10 and budget["omitted_user_turns"] > 0
    assert budget["eligible_user_turns"] == 10 and budget["truncated"] is True
    assert current["active_user_history_truncated"] is False
    assert "Не считай её полной" in web_ui.project_context_prompt(current, "chat-b")


def test_writer_per_turn_clipping_is_reported_without_changing_existing_budget():
    current = scope()
    current["active_user_messages"] = [user("x" * 1801)]
    entries, budget = web_ui._writer_history(current, "chat-b")
    assert entries == ["[Чат | user] " + "x" * 1800]
    assert budget["clipped_user_turns"] == 1 and budget["omitted_user_turns"] == 0
    assert budget["truncated"] is True
    assert "Не считай её полной" in web_ui.project_context_prompt(current, "chat-b")


@pytest.mark.parametrize("mode", ["python", "rust", "hybrid"])
def test_handler_uses_active_window_for_query_writer_and_trace(monkeypatch, mode):
    current = scope()
    current["active_user_history_truncated"] = True
    agent, captured = SpyAgent(), {}
    handler = object.__new__(web_ui.Handler)
    handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
    def rust_generate(question, context, prompt, **options):
        agent.prompts.append(prompt)
        return "Current answer", False
    replacements = {
        "get_agent": lambda: agent, "load_flow_mode": lambda: mode,
        "get_profile": lambda *args: {"id": "test", "prompt": "Grounded response"},
        "load_system_prompt_id": lambda: "test", "get_project_conversation": lambda chat_id: current,
        "list_all_documents_for_retrieval": document_registry,
        "validate_retrieval": lambda *args: [], "requested_response_formulas": lambda *args: [],
        "extract_formula_lines": lambda *args: [], "enrich_source_links": lambda sources: sources,
        "enrich_answer_source_citations": lambda answer, sources: answer,
        "select_user_facing_sources": lambda answer, sources: sources,
        "usage_stats": lambda *args: {}, "render_text": lambda value: value,
        "rust_context": lambda *args: [], "rust_generate": rust_generate,
        "llm_label": lambda *args: {"provider": "test", "model": "fake", "label": "Fake"},
    }
    for name, replacement in replacements.items():
        monkeypatch.setattr(web_ui, name, replacement)
    handler.handle_ask({"question": QUESTION, "chat_id": "chat-b"})
    assert captured["status"] == 200
    assert all(ANCHOR in prompt and "OTHER_PROJECT_CHAT_CONTEXT" not in prompt for prompt in agent.prompts)
    context = captured["payload"]["retrieval_trace"]["conversation_context"]
    assert context["subject"] == ANCHOR and context["source_message_ids"] == ["u1"]
    assert context["history_window"] == {
        "source": "active_chat_user_messages", "retained_user_turns": 1, "limit": 40, "truncated": True,
    }
    assert context["writer_history_window"]["included_user_turns"] == 1
    assert context["writer_history_window"]["truncated"] is False
