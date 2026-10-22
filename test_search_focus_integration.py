"""Search hypotheses are soft; resolved user scope remains authoritative.

These wiring tests use deterministic provider/retriever fakes. They verify
transport and scope boundaries, not semantic quality of a model-generated plan.
"""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from rag_agent import RAGAgent, refine_prism_query
from search_focus import SearchFocus, SearchOperation, SearchSubject
from test_corrective_integration import FakeModel, agent_with_fake_dependencies, evidence


class PlannedModel(FakeModel):
    def __init__(self, plan):
        super().__init__()
        self.plan = plan

    def complete(self, prompt, **kwargs):
        if prompt.startswith("Планируй поиск"):
            self.calls.append((0, prompt, kwargs))
            self.local.reviews = 0
            return json.dumps(self.plan, ensure_ascii=False)
        return super().complete(prompt, **kwargs)


def plan(canonical, focus=True):
    result = {"canonical_question": canonical,
              "requirements": ["Этапы контроля"], "queries": ["контроль каналов"]}
    if focus:
        result["search_focus"] = {"operation": "enumerate", "subjects": [
            {"text": "этапы контроля", "alternatives": ["стадии проверки"]}]}
    return result


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setenv("RAG_PLANNER_THINKING", "false")
    monkeypatch.setenv("RAG_SOURCE_READING", "off")
    monkeypatch.setenv("RAG_TASK_SCOPE_CHECK", "off")

    def unexpected(*args, **kwargs):
        raise AssertionError("Search-focus wiring tests must not use the network")

    monkeypatch.setattr("requests.sessions.Session.request", unexpected)


@pytest.mark.parametrize("focus", [False, True])
def test_corrective_model_document_name_cannot_replace_users_explicit_source(focus):
    original = "Из файла control.md перечисли этапы контроля."
    canonical = "Из файла reserve.md перечисли этапы контроля."
    agent = agent_with_fake_dependencies(PlannedModel(plan(canonical, focus)),
        items=[evidence("control.md"), evidence("reserve.md")])
    aliases = {"control.md": "control.md", "reserve.md": "reserve.md"}

    _, sources, _ = agent.ask(original, pipeline_mode="on", source_aliases=aliases)

    assert [item["file"] for item in sources] == ["control.md"]
    assert agent.retrieve_calls[0][1] == canonical
    retrieve_kwargs = agent.retrieve_calls[0][-1]
    select_kwargs = agent._section_evidence_index.calls[0][-1]
    assert retrieve_kwargs["constraint_question"] == original
    assert select_kwargs["constraint_question"] == original
    assert select_kwargs["allowed_sources"] == {"control.md"}
    assert select_kwargs["search_focus"] is retrieve_kwargs["search_focus"]
    assert (retrieve_kwargs["search_focus"] is not None) is focus


@pytest.mark.parametrize("focus", [False, True])
def test_model_document_name_does_not_create_a_new_single_source_filter(focus):
    original = "Перечисли этапы контроля каналов."
    agent = agent_with_fake_dependencies(PlannedModel(plan("По файлу control.md перечисли этапы контроля.", focus)),
        items=[evidence("control.md"), evidence("reserve.md")])
    aliases = {"control.md": "control.md", "reserve.md": "reserve.md"}

    _, sources, _ = agent.ask(original, pipeline_mode="on", source_aliases=aliases)

    assert {item["file"] for item in sources} == set(aliases)
    assert agent._section_evidence_index.calls[0][-1]["allowed_sources"] == set(aliases)


def test_constraints_use_resolved_user_context_not_short_followup_or_model_rewrite():
    resolved = "Тема: Сравни режимы датчика 17 и 28\nНе рассматривай аварийное отключение."
    canonical = "Сравнение режимов датчика"
    agent = agent_with_fake_dependencies(PlannedModel(plan(canonical)))

    agent.ask("А между ними какая разница?", retrieval_query=resolved, pipeline_mode="on")

    assert agent.retrieve_calls[0][-1]["constraint_question"] == resolved
    assert agent._section_evidence_index.calls[0][-1]["constraint_question"] == resolved
    # Coverage and the writer still receive the full task, not only short noun phrases.
    assert agent.last_retrieval_trace()["plan"]["requirements"] == ("Этапы контроля",)


def test_simultaneous_corrective_requests_cannot_share_focus_or_user_constraints():
    class PerRequestPlan(FakeModel):
        def complete(self, prompt, **kwargs):
            if not prompt.startswith("Планируй поиск"):
                return super().complete(prompt, **kwargs)
            question = json.loads(prompt.split("ЗАПРОС И КОНТЕКСТ:\n", 1)[1])["question"]
            label = "АЛЬФА" if "АЛЬФА" in question else "БЕТА"
            payload = plan("Контроль каналов " + label)
            payload["search_focus"]["subjects"][0]["text"] = "контроль " + label
            self.local.reviews = 0
            return json.dumps(payload, ensure_ascii=False)

    barrier = threading.Barrier(2)
    agent = agent_with_fake_dependencies(PerRequestPlan(), barrier=barrier,
        items=lambda question: [evidence("alpha.md" if "АЛЬФА" in question else "beta.md")])

    def ask(label):
        question = "Перечисли этапы контроля " + label
        _, sources, _ = agent.ask(question, pipeline_mode="on")
        return question, sources, agent.last_retrieval_trace()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(ask, ("АЛЬФА", "БЕТА")))

    for question, sources, trace in results:
        label = "АЛЬФА" if "АЛЬФА" in question else "БЕТА"
        assert [item["file"] for item in sources] == ["alpha.md" if label == "АЛЬФА" else "beta.md"]
        assert trace["plan"]["search_focus"]["subjects"][0]["text"] == "контроль " + label
    for _, task, _, _, kwargs in agent.retrieve_calls:
        label = "АЛЬФА" if "АЛЬФА" in task else "БЕТА"
        assert kwargs["constraint_question"] == "Перечисли этапы контроля " + label
        assert kwargs["search_focus"].subjects[0].text == "контроль " + label
    for task, _, kwargs in agent._section_evidence_index.calls:
        label = "АЛЬФА" if "АЛЬФА" in task else "БЕТА"
        assert kwargs["constraint_question"] == "Перечисли этапы контроля " + label
        assert kwargs["search_focus"].subjects[0].text == "контроль " + label


class RecordingIndex:
    def __init__(self):
        self.seed_calls = []
        self.select_calls = []

    def body_seeds(self, question, **kwargs):
        self.seed_calls.append((question, kwargs))
        return []

    def select_v2(self, question, pool, **kwargs):
        self.select_calls.append((question, list(pool), kwargs))
        return list(pool)


def retrieval_agent(items):
    """Use the real retrieve_context while replacing only its I/O endpoints."""
    agent = RAGAgent.__new__(RAGAgent)
    agent._all_nodes = []
    agent._section_evidence_nodes = agent._all_nodes
    agent._section_evidence_index = RecordingIndex()
    agent._refresh_docstore_if_changed = Mock()
    agent.llm = SimpleNamespace(metadata=SimpleNamespace(context_window=32768))
    agent.reranker = None
    calls = []

    def retrieve(bundle):
        calls.append(bundle.query_str)
        return [SimpleNamespace(node=SimpleNamespace(text=item["text"], metadata={
            "file_name": item["file"], "section_path": item.get("section_path", "Основной порядок"),
        }), score=1.0) for item in items]

    agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=SimpleNamespace(retrieve=retrieve)))
    for name in ("_procedural_seed_results", "_exact_section_seed_results", "_lexical_section_seed_results",
                 "_section_seed_results", "_exact_entity_seed_results", "_formula_clause_seed_results",
                 "_regulation_concept_seed_results", "_document_occurrence_seed_results",
                 "_price_category_seed_results", "_scoped_lexical_seed_results", "_domain_lexical_seed_results"):
        setattr(agent, name, Mock(return_value=[]))
    return agent, calls


def test_real_retrieval_reserves_raw_user_probe_and_each_primary_focus_before_alternatives():
    original = "Сравни режимы датчика 17 и 28 при 20 °C; сохрани точные названия."
    focus = SearchFocus(SearchOperation.COMPARE, tuple(
        SearchSubject(f"режим датчика {number}", tuple(f"вариант {number} {suffix}" for suffix in "ABC"))
        for number in range(6)))
    agent, probes = retrieval_agent([])
    trace = {}

    agent.retrieve_context("краткий модельный запрос", retrieval_queries=["переписанный запрос"] * 12,
        selection_mode="coverage", search_focus=focus, constraint_question=original,
        retrieval_trace=trace)

    assert probes[0] == original
    assert probes[1:7] == [subject.text for subject in focus.subjects]
    assert len(probes) <= 9
    assert len({probe.casefold() for probe in probes}) == len(probes)
    assert trace["constraint_question"] == original
    for _, kwargs in agent._section_evidence_index.seed_calls:
        assert kwargs["search_focus"] is focus
        assert kwargs["constraint_question"] == original
    assert agent._section_evidence_index.select_calls[0][-1]["search_focus"] is focus


@pytest.mark.parametrize("focus", [None, SearchFocus(SearchOperation.LOOKUP, (SearchSubject("контроль каналов"),))])
def test_real_retrieval_uses_user_source_even_with_conflicting_model_task(focus):
    original = "Расскажи про контроль каналов из control.md."
    agent, _ = retrieval_agent([evidence("control.md"), evidence("reserve.md")])

    selected = agent.retrieve_context("Расскажи по reserve.md.",
        retrieval_queries=["контроль каналов"], selection_mode="coverage",
        source_aliases={"control.md": "control.md", "reserve.md": "reserve.md"},
        search_focus=focus, constraint_question=original)

    assert [item["file"] for item in selected] == ["control.md"]
    assert agent._section_evidence_index.select_calls[0][-1]["allowed_sources"] == {"control.md"}


@pytest.mark.parametrize("user_section,model_section,expected", [
    (" по пункту 8.1", " по пункту 99.1", {"8.1 Основной порядок"}),
    ("", " по пункту 99.1", {"8.1 Основной порядок", "99.1 Резервирование"}),
])
def test_real_retrieval_prism_point_constraint_is_from_user_not_model_or_supplied_spec(user_section, model_section, expected):
    original = "Расскажи о контроле каналов" + user_section
    model_question = "Расскажи о контроле каналов" + model_section
    second = evidence("control.md", "99.1 Резервирование")
    second["text"] += " Резервирование выполняется отдельно."
    agent, _ = retrieval_agent([evidence("control.md", "8.1 Основной порядок"), second])

    selected = agent.retrieve_context(model_question,
        retrieval_queries=["контроль каналов"], selection_mode="coverage",
        constraint_question=original, spec=refine_prism_query(model_question))

    assert {item["section_path"] for item in selected} == expected
    assert agent._section_evidence_index.select_calls[0][-1]["constraint_question"] == original


def test_default_retrieval_does_not_pass_new_keywords_to_legacy_selector_contract():
    agent, _ = retrieval_agent([])
    index = agent._section_evidence_index
    index.select = Mock(return_value=[])

    agent.retrieve_context("Контроль каналов", retrieval_queries=["контроль каналов"])

    assert all("search_focus" not in kwargs and "constraint_question" not in kwargs
               for _, kwargs in index.seed_calls)
    assert "search_focus" not in index.select.call_args.kwargs
    assert "constraint_question" not in index.select.call_args.kwargs
