"""RAGAgent wiring contracts using fake models/indexes; no network or database.

These complement orchestration unit tests by exercising the public ask switch,
thread-local UI evidence/trace, source-scope propagation and citation rendering.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch

import pytest

from rag_agent import RAGAgent, human_source_reference
from grounding_verifier import extract_answer_claims


def test_explicit_request_model_label_does_not_reread_other_chats_settings(monkeypatch):
    import web_ui
    monkeypatch.setattr(web_ui,'load_provider_config',lambda:{'provider':'openai','model':'replacement-model'})
    label = web_ui.llm_label({'provider':'ollama','model':'Qwen/Qwen3.8-27B'})
    assert label['model']=='Qwen/Qwen3.8-27B'
    assert label['provider']=='ollama'
    assert 'vLLM' in label['label']


QUESTION = "Какие этапы контроля проходят измерительные каналы?"
FACT = "Каждый измерительный канал проходит два этапа контроля."


def evidence(name="Проверка каналов.md", section="Основной порядок"):
    return {"file": name, "source": name, "section_path": section, "score": 0.8, "text": FACT}


class FakeModel:
    """Deterministic completions with the same prompt API as provider wrappers."""

    def __init__(self, *, answer=None, additional_search=False, assess_claims=True,
                 task_alignment="matched"):
        self.answer = answer or FACT + " [E1]"
        self.additional_search = additional_search
        self.assess_claims = assess_claims
        self.task_alignment = task_alignment
        self.local = threading.local()
        self.calls = []
        self.metadata = SimpleNamespace(context_window=32768)

    def complete(self, prompt, **kwargs):
        self.calls.append((threading.get_ident(), prompt, kwargs))
        if prompt.startswith("Планируй поиск"):
            self.local.reviews = 0
            value = {"requirements": ["Описать этапы контроля"], "queries": ["Контроль измерительных каналов"]}
        elif "Проверь доказательность ответа" in prompt:
            # Positive wiring fixtures explicitly assess their sole known fact.
            # This is test intent, not a substitute for a real semantic judge.
            payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]
            claim_inputs = {row["claim_id"]: row for row in payload["answer_claims"]}
            rows = []
            if self.assess_claims:
                for claim in extract_answer_claims(payload["answer"]):
                    if re.sub(r"\[[^\]]+\]", "", claim["text"]).strip() != FACT:
                        pytest.fail("Positive fake judge received an unexpected factual claim")
                    cited = claim_inputs[claim["claim_id"]]["cited_evidence_ids"]
                    passage_ids = self._fact_passages(payload, cited)
                    if not passage_ids:
                        pytest.fail("Positive fake judge requires the cited fixture fact")
                    rows.append({"claim_id": claim["claim_id"], "support": "supported",
                                 "passage_ids": passage_ids,
                                 "task_relevance": self.task_alignment})
            value = {"claims": rows}
        elif prompt.startswith("Оцени полноту"):
            payload = json.loads(prompt.splitlines()[-1])
            passage_ids = self._fact_passages(payload)
            count = getattr(self.local, "reviews", 0)
            self.local.reviews = count + 1
            if self.additional_search and count == 0:
                value = {"coverage": [], "missing_queries": ["Порядок дополнительной проверки канала"], "conflicts": []}
            else:
                value = {"coverage": ([{"requirement_id": "R1", "passage_ids": passage_ids,
                                        "applicable": True}] if passage_ids else []),
                         "missing_queries": [], "conflicts": []}
            value["task_alignment"] = {
                "status": self.task_alignment if passage_ids else "uncertain",
                "passage_ids": passage_ids if self.task_alignment == "matched" else []}
        else:
            return self.answer
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _fact_passages(payload, cited=None):
        return [passage["passage_id"] for item in payload["evidence"]
                if cited is None or item["evidence_id"] in cited
                for passage in item["passages"] if passage["text"] == FACT]


class FakeIndex:
    def __init__(self):
        self.calls = []

    def select_v2(self, question, pool, **kwargs):
        self.calls.append((question, list(pool), kwargs))
        allowed, excluded = kwargs.get("allowed_sources"), kwargs.get("excluded_sources") or set()
        return [item for item in pool if item["file"] not in excluded
                and (allowed is None or item["file"] in allowed)]


def agent_with_fake_dependencies(model=None, *, items=None, barrier=None):
    agent = RAGAgent.__new__(RAGAgent)
    agent.llm = model or FakeModel()
    agent.llm_model = "synthetic-local-test-model"
    agent._all_nodes = [object()]
    agent._docstore_mtime_ns = 101
    agent._section_evidence_index = FakeIndex()
    agent._refresh_docstore_if_changed = Mock()
    agent.retrieve_calls = []

    def retrieve(question, top_k, *args, **kwargs):
        agent.retrieve_calls.append((threading.get_ident(), question, top_k, args, kwargs))
        trace = kwargs.get("retrieval_trace")
        if trace is not None:
            trace.update(synthetic_question=question, visible_candidates=1)
        if barrier:
            barrier.wait(timeout=5)
        return [dict(item) for item in (items(question) if callable(items) else items or [evidence()])]

    agent.retrieve_context = retrieve
    return agent


@pytest.fixture(autouse=True)
def no_external_dependencies(monkeypatch):
    monkeypatch.setenv("RAG_CORRECTIVE_PIPELINE", "off")
    monkeypatch.setenv("RAG_PLANNER_THINKING", "false")
    monkeypatch.setenv("RAG_MAX_ADDITIONAL_SEARCH_ROUNDS", "2")

    def unexpected_network(*args, **kwargs):
        raise AssertionError("Corrective integration tests must not call network services")

    monkeypatch.setattr("requests.post", unexpected_network)
    monkeypatch.setattr("requests.get", unexpected_network)


@pytest.mark.parametrize("citation", ["[E1] [E2]", "[E1,E2]", "[E1; E2]", "[e1, e2]"])
def test_corrective_ask_renders_only_trusted_human_references(citation):
    items = [evidence("Проверка каналов.md"), evidence("Дополнительный контроль.md", "Порядок проверки")]
    agent = agent_with_fake_dependencies(FakeModel(answer=FACT + " " + citation), items=items)
    answer, sources, cached = agent.ask(QUESTION, pipeline_mode="on")
    assert FACT in answer
    assert not re.search(r"\[e\d", answer, re.I)
    for item in items:
        assert "[Источник: " + human_source_reference(item) + "]" in answer
    assert {row["file"] for row in sources} == {row["file"] for row in items}
    assert cached is False
    assert agent.last_generation_from_cache is False
    assert agent.last_retrieval_trace()["mode"] == "corrective"
    assert agent.last_retrieval_trace()["coverage_complete"] is True
    assert [row["evidence_id"] for row in agent.last_retrieved_context()] == ["E1", "E2"]


def test_all_corrective_search_rounds_and_selection_receive_identical_source_scope():
    allowed = {"Доступная методика.md": "Доступная методика.md"}
    excluded = {"Отозванный документ.md"}
    priority = {"Доступная методика.md"}
    domains = {"Доступная методика.md": ("retail",)}
    versions = {"Доступная методика.md": "2026-10-21"}
    items = [evidence("Доступная методика.md"), evidence("Чужой проект.md"), evidence("Отозванный документ.md")]
    agent = agent_with_fake_dependencies(FakeModel(additional_search=True), items=items)
    _, sources, _ = agent.ask(
        QUESTION, pipeline_mode="on", excluded_sources=excluded,
        source_aliases=allowed, priority_sources=priority, source_domains=domains,
        source_versions=versions, retrieval_focus="retail",
    )
    assert len(agent.retrieve_calls) == 2
    assert len(agent._section_evidence_index.calls) == 2
    for _, question, _, _, kwargs in agent.retrieve_calls:
        assert question == QUESTION
        assert kwargs["excluded_sources"] == excluded
        assert kwargs["source_aliases"] == allowed
        assert kwargs["priority_sources"] == priority
        assert kwargs["source_domains"] == domains
        assert kwargs["source_versions"] == versions
        assert kwargs["retrieval_focus"] == "retail"
        assert kwargs["selection_mode"] == "coverage"
        assert kwargs["evidence_facets"] == ["Описать этапы контроля"]
        assert kwargs["subject_question"] == QUESTION
        assert kwargs["constraint_question"] == QUESTION
        assert kwargs["search_focus"] is None
    for _, _, kwargs in agent._section_evidence_index.calls:
        assert kwargs["allowed_sources"] == set(allowed)
        assert kwargs["excluded_sources"] == excluded
        assert kwargs["constraint_question"] == QUESTION
        assert kwargs["search_focus"] is None
    assert [item["file"] for item in sources] == ["Доступная методика.md"]
    assert len(agent.last_retrieval_trace()["rounds"]) == 2


def test_empty_allowed_scope_is_preserved_not_broadened_on_additional_searches():
    agent = agent_with_fake_dependencies(FakeModel(additional_search=True))
    answer, sources, _ = agent.ask(QUESTION, pipeline_mode="on", source_aliases={})
    assert sources == []
    assert "Источник:" not in answer
    assert all(call[-1]["source_aliases"] == {} for call in agent.retrieve_calls)
    assert all(call[-1]["allowed_sources"] == set() for call in agent._section_evidence_index.calls)


def test_request_local_trace_and_evidence_are_isolated_between_concurrent_chats():
    barrier = threading.Barrier(2)
    agent = agent_with_fake_dependencies(
        items=lambda question: [evidence("Первый.md" if "АЛЬФА" in question else "Второй.md")], barrier=barrier,
    )

    def ask(label):
        question = QUESTION + " " + label
        answer, sources, _ = agent.ask(question, pipeline_mode="on")
        return question, answer, sources, agent.last_retrieval_trace(), agent.last_retrieved_context()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ask, ("АЛЬФА", "БЕТА")))
    for question, answer, sources, trace, context in results:
        expected = "Первый.md" if "АЛЬФА" in question else "Второй.md"
        assert [row["file"] for row in sources] == [expected]
        assert [row["file"] for row in context] == [expected]
        assert trace["rounds"][0]["retrieval"]["synthetic_question"] == question
        assert trace["plan"]["queries"][0] == question
        assert expected in answer
        assert trace["corpus_snapshot"] == 101
    # The parent thread has no completed request and cannot see either chat.
    assert agent.last_retrieval_trace() == {}
    assert agent.last_retrieved_context() is None


def test_legacy_default_retains_original_path_and_marks_trace_as_legacy():
    agent = agent_with_fake_dependencies()
    understanding = SimpleNamespace(canonical_question=QUESTION, retrieval_queries=(QUESTION,))
    agent.understand_query = Mock(return_value=understanding)
    agent._generate_grounded_answer = Mock(return_value="Прежний ответ")
    agent._ask_corrective = Mock(side_effect=AssertionError("Corrective mode must be explicitly enabled"))
    answer, sources, cached = agent.ask(QUESTION)
    assert answer == "Прежний ответ"
    assert len(sources) == 1
    assert cached is False
    agent.understand_query.assert_called_once_with(QUESTION)
    agent._generate_grounded_answer.assert_called_once()
    agent._ask_corrective.assert_not_called()
    assert agent.last_retrieval_trace() == {"mode": "legacy"}


def test_explicit_legacy_request_overrides_enabled_environment(monkeypatch):
    monkeypatch.setenv("RAG_CORRECTIVE_PIPELINE", "on")
    agent = agent_with_fake_dependencies()
    agent.understand_query = Mock(return_value=SimpleNamespace(canonical_question=QUESTION, retrieval_queries=(QUESTION,)))
    agent._generate_grounded_answer = Mock(return_value="Прежний ответ")
    agent._ask_corrective = Mock(side_effect=AssertionError("Request disabled corrective mode"))
    answer, _, _ = agent.ask(QUESTION, pipeline_mode="off")
    assert answer == "Прежний ответ"
    agent._ask_corrective.assert_not_called()


def test_environment_switch_enables_corrective_mode_without_signature_change(monkeypatch):
    monkeypatch.setenv("RAG_CORRECTIVE_PIPELINE", "on")
    agent = agent_with_fake_dependencies()
    answer, _, _ = agent.ask(QUESTION)
    assert FACT in answer
    assert agent.last_retrieval_trace()["mode"] == "corrective"


def test_unassessed_claims_are_withheld_after_one_bounded_repair():
    model = FakeModel(assess_claims=False)
    agent = agent_with_fake_dependencies(model)
    answer, _, _ = agent.ask(QUESTION, pipeline_mode="on")
    assert FACT not in answer
    assert "Не удалось надёжно сопоставить" in answer
    verification = agent.last_retrieval_trace()["verification"]
    for stage in ("initial", "after_repair"):
        assert verification[stage]["assessment_complete"] is False
        assert verification[stage]["task_relevance"]["claim_counts"]["unassessed"] == 1
    assert verification["partial_answer"] is True
    assert len(model.calls) == 6  # Plan, coverage, writer/judge, one repair writer/judge.
    assert len(agent.retrieve_calls) == 1


def test_later_legacy_request_does_not_reuse_previous_corrective_trace():
    agent = agent_with_fake_dependencies()
    agent.ask(QUESTION, pipeline_mode="on")
    assert agent.last_retrieval_trace()["mode"] == "corrective"
    agent.understand_query = Mock(return_value=SimpleNamespace(canonical_question=QUESTION, retrieval_queries=(QUESTION,)))
    agent._generate_grounded_answer = Mock(return_value="Прежний ответ")
    agent.ask(QUESTION, pipeline_mode="off")
    assert agent.last_retrieval_trace() == {"mode": "legacy"}
    assert all("evidence_id" not in row for row in agent.last_retrieved_context())


def test_structured_retrieval_context_reaches_planner_without_changing_original_question():
    enriched = "Тема: Сравни 3 и 6 ЦК\nТолько покупную мощность; сетевую не смотри"
    original = "Ответь точно и кратко"
    agent = agent_with_fake_dependencies(FakeModel(task_alignment="uncertain", assess_claims=False))
    agent.ask(original, pipeline_mode="on", retrieval_query=enriched)
    planning = [prompt for _, prompt, _ in agent.llm.calls if prompt.startswith("Планируй поиск")]
    assert len(planning) == 1
    assert enriched == json.loads(planning[0].split("ЗАПРОС И КОНТЕКСТ:\n", 1)[1])['question']
    assert agent.retrieve_calls[0][1] == enriched
    generation = [prompt for _, prompt, _ in agent.llm.calls if '"question": "Ответь точно и кратко"' in prompt]
    assert generation


def test_model_switch_in_another_chat_cannot_change_an_inflight_corrective_request():
    initial, replacement = FakeModel(), FakeModel(answer="Another chat's model must not answer")
    agent = agent_with_fake_dependencies(initial)
    agent.llm_model = "synthetic-initial-model"
    original_retrieve = agent.retrieve_context
    completion_counts_at_switch = []

    def switch_model_during_retrieval(question, top_k, *args, **kwargs):
        completion_counts_at_switch.append(len(initial.calls))
        # Simulate a settings update by another chat after planning but before
        # evidence review, answer generation and grounding verification.
        agent.llm = replacement
        agent.llm_model = "synthetic-replacement-model"
        return original_retrieve(question, top_k, *args, **kwargs)

    agent.retrieve_context = switch_model_during_retrieval
    answer, _, _ = agent.ask(QUESTION, pipeline_mode="on")
    assert completion_counts_at_switch
    assert completion_counts_at_switch[0] >= 1  # Planning precedes retrieval.
    remaining = initial.calls[completion_counts_at_switch[0]:]
    assert remaining
    assert any(prompt.startswith("Оцени полноту") for _, prompt, _ in remaining)
    assert any("Проверь доказательность ответа" in prompt for _, prompt, _ in remaining)
    assert FACT in answer
    assert replacement.calls == []
    assert agent.last_retrieval_trace()["model"] == "synthetic-initial-model"
    assert agent.llm is replacement  # No unsafe shared-settings restoration.
    assert agent.llm_model == "synthetic-replacement-model"


def test_inflight_request_uses_adapter_identity_during_partially_applied_settings_change():
    initial = FakeModel()
    initial.model = "actual-existing-adapter-model"
    agent = agent_with_fake_dependencies(initial)
    # Provider setters update this label before constructing/replacing llm.
    # Starting a request in that window must not mix the old adapter with the
    # pending new model name or label its final answer as another model's.
    agent.llm_model = "pending-new-model"
    answer, _, _ = agent.ask(QUESTION, pipeline_mode="on")
    assert FACT in answer
    assert agent.last_retrieval_trace()["model"] == "actual-existing-adapter-model"
    assert agent.llm is initial
    assert initial.calls


def _handler_request(monkeypatch, agent, mode, *, body=None):
    import web_ui

    captured = {}
    handler = object.__new__(web_ui.Handler)
    handler.send_json = lambda payload, status=200: captured.update(payload=payload, status=status)
    settings = {
        "load_flow_mode": lambda: mode,
        "get_agent": lambda: agent,
        "get_profile": lambda *args: {"prompt": "test", "id": "test"},
        "load_system_prompt_id": lambda: "test",
        "get_project_conversation": lambda chat_id: {"messages": [], "memory": {}, "project_name": "Current"},
        "is_document_catalog_question": lambda question: False,
        "retrieval_document_scope": lambda *args: (set(), {"visible.md": "visible.md"}),
        "shared_global_document_sources": lambda: {"visible.md"},
        "validate_retrieval": lambda *args: [],
        "requested_response_formulas": lambda *args: [],
        "extract_formula_lines": lambda *args: [],
        "enrich_source_links": lambda sources: sources,
        "enrich_answer_source_citations": lambda answer, sources: answer,
        "select_user_facing_sources": lambda answer, sources: sources,
        "usage_stats": lambda *args: {},
        "render_text": lambda value: value,
        "rust_context": lambda *args: [],
        "rust_generate": lambda *args, **kwargs: ("Current answer", False),
        "load_provider_config": lambda: {"provider": "ollama", "model": "current-model"},
    }
    with monkeypatch.context() as scoped:
        for name, value in settings.items():
            scoped.setattr(web_ui, name, value)
        handler.handle_ask(body if body is not None else {"question": QUESTION, "chat_id": "current-chat"})
    return captured


def _agent_with_previous_private_trace():
    agent = agent_with_fake_dependencies(items=[evidence("visible.md")])
    agent._generation_state = threading.local()
    agent._generation_state.retrieval_trace = {
        "mode": "corrective", "provider": "ollama", "model": "previous-private-model",
        "verification": {"initial": {"checks": [{"text": "DO_NOT_LEAK_PRIVATE_FACT"}]}},
        "previous_document": "another-users-private.md",
    }
    agent._generation_state.retrieved_context = [evidence("another-users-private.md")]
    agent._generate_grounded_answer = Mock(return_value="Current answer")
    return agent


def test_supported_claims_do_not_hide_incomplete_question_coverage(monkeypatch):
    agent = _agent_with_previous_private_trace()
    def current_ask(*args,**kwargs):
        agent._generation_state.retrieval_trace = {'mode':'corrective','coverage_complete':False,
            'verification':{'initial':{'assessment_complete':True}}}
        agent._generation_state.retrieved_context = [evidence('visible.md')]
        return FACT,[{'file':'visible.md'}],False
    agent.ask = current_ask
    response = _handler_request(monkeypatch,agent,'python')['payload']
    assert any('Не все части вопроса' in warning for warning in response['warnings'])


@pytest.mark.parametrize("mode", ["rust", "hybrid"])
def test_nonpython_flow_cannot_persist_previous_chats_corrective_trace(monkeypatch, mode):
    agent = _agent_with_previous_private_trace()
    reader = Mock(wraps=agent.last_retrieval_trace)
    agent.last_retrieval_trace = reader
    response = _handler_request(monkeypatch, agent, mode)["payload"]
    serialized = json.dumps(response, ensure_ascii=False)
    assert "DO_NOT_LEAK_PRIVATE_FACT" not in serialized
    assert "another-users-private" not in serialized
    assert "previous-private-model" not in serialized
    assert response["retrieval_trace"].get("mode") != "corrective"
    reader.assert_not_called()


def test_python_cache_only_adapter_does_not_reuse_old_diagnostics_or_evidence(monkeypatch):
    agent = _agent_with_previous_private_trace()
    agent.ask = Mock(return_value=("Current cached answer", [], True))
    response = _handler_request(monkeypatch, agent, "python")["payload"]
    serialized = json.dumps(response, ensure_ascii=False)
    assert response["from_cache"] is True
    assert "DO_NOT_LEAK_PRIVATE_FACT" not in serialized
    assert "another-users-private" not in serialized
    assert "previous-private-model" not in serialized
    assert [row["file"] for row in response["context"]] == ["visible.md"]


def test_empty_or_failed_request_cannot_leak_earlier_trace_in_later_hybrid_request(monkeypatch):
    agent = _agent_with_previous_private_trace()
    empty = _handler_request(monkeypatch, agent, "python", body={"question": "", "chat_id": "current-chat"})
    assert empty["status"] == 400
    agent.ask = Mock(side_effect=RuntimeError("synthetic generation failure"))
    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        _handler_request(monkeypatch, agent, "python")
    response = _handler_request(monkeypatch, agent, "hybrid")["payload"]
    assert "DO_NOT_LEAK_PRIVATE_FACT" not in json.dumps(response)
    assert "previous_document" not in response["retrieval_trace"]


class PersistedCorrectiveTraceTests(IsolatedAsyncioTestCase):
    async def test_nondurable_generation_persists_request_trace_without_database(self):
        import asgi_app

        trace = {"mode": "corrective", "model": "synthetic-model",
                 "rounds": [{"number": 0, "evidence_ids": ["E1"]}],
                 "verification": {"initial": {"assessment_complete": True}}}
        response = {"answer": FACT, "html_answer": "<p>" + FACT + "</p>",
                    "flow_mode": "python", "retrieval_trace": trace}
        request = {"question": QUESTION, "chat_id": "synthetic-chat", "_owner_subject": "local:synthetic"}
        with patch.object(asgi_app, "run_ask", return_value=response) as generate, \
                patch.object(asgi_app, "update_message") as persist:
            result = await asgi_app._run_and_persist_ask(request, {"id": "synthetic-message"})
        generate.assert_called_once_with(request)
        persist.assert_called_once()
        message_id, payload = persist.call_args.args
        assert message_id == "synthetic-message"
        assert payload["status"] == "complete"
        assert payload["metadata"]["retrieval_trace"] == trace
        assert json.loads(json.dumps(payload["metadata"]["retrieval_trace"])) == trace
        assert result["message_id"] == "synthetic-message"
