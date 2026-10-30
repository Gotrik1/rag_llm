"""Actual corrective request closures gate optional admission and retain authority."""
from copy import deepcopy
import json
import socket
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import candidate_admission
from corrective_rag import CorrectiveResult
from query_rank import QueryRankContext
from rag_agent import RAGAgent
from user_task import UserTask


RAW = 'А их условия действительно различаются?'
RESOLVED = 'Предыдущий USER: сравни осмотр и блокировку.\nТекущий USER: ' + RAW
FACT = 'Осмотр и блокировка имеют разные условия.'
ROW = {'evidence_id': 'E1', 'file': 'visible.md', 'text': FACT, 'section_path': 'Условия',
       'status': 'active', 'revision_id': 'r1', 'score': 1.0}


@pytest.fixture(autouse=True)
def isolated_flags_and_no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Admission wiring tests must not perform model/network I/O')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.delenv('RAG_QUERY_DIVERSE_ADMISSION', raising=False)
    for key in ('RAG_PREPACK_SOURCE_READING', 'RAG_SOURCE_READING', 'RAG_TASK_SCOPE_CHECK',
                'RAG_PLANNER_THINKING'):
        monkeypatch.setenv(key, 'off')
    monkeypatch.setenv('RAG_MAX_ADDITIONAL_SEARCH_ROUNDS', '0')
    monkeypatch.setenv('RAG_EVIDENCE_MAX_CHARS', '12000')


def agent_for():
    agent = object.__new__(RAGAgent)
    agent._all_nodes = []
    agent._generation_state = threading.local()
    agent._docstore_mtime_ns = 1
    agent.llm_model = 'scripted-no-model'
    agent.llm = SimpleNamespace(model=agent.llm_model, provider='scripted',
        metadata=SimpleNamespace(context_window=32768), complete=Mock(side_effect=AssertionError('Unexpected model call')))
    agent.retrieve_context = Mock(return_value=[deepcopy(ROW)])
    agent._section_evidence_index = SimpleNamespace(select_v2=Mock(side_effect=lambda _, pool, **kwargs: pool))
    return agent


@pytest.mark.parametrize('flag,enabled', [(None, False), ('off', False), ('false', False),
    ('0', False), ('unexpected', False), ('on', True), ('true', True), ('1', True), ('ON', True)])
@pytest.mark.parametrize('prepack', [False, True])
def test_admission_is_default_off_and_does_not_retarget_other_runtime_options(monkeypatch, flag, enabled, prepack):
    if flag is not None:
        monkeypatch.setenv('RAG_QUERY_DIVERSE_ADMISSION', flag)
    monkeypatch.setenv('RAG_PREPACK_SOURCE_READING', 'on' if prepack else 'off')
    agent = agent_for()
    aliases, excluded, versions = {'visible.md': 'Visible'}, {'hidden.md'}, {'visible.md': 'latest'}
    domains = {'visible.md': ('rre',)}
    calls = []
    def ordering(pool, *, max_chars, query_rank_context):
        calls.append((pool, max_chars, query_rank_context))
        return list(pool)
    monkeypatch.setattr(candidate_admission, 'order_by_query_coverage', ordering)
    def frontier_ordering(pool, **kwargs):
        assert prepack
        return ordering(pool, **kwargs)
    monkeypatch.setattr(candidate_admission, 'order_by_query_frontiers', frontier_ordering)
    def orchestration(question, search_question, **kwargs):
        assert (question, search_question) == (RAW, RESOLVED)
        assert ('admit' in kwargs) is enabled
        assert ('prepack_source_reading' in kwargs) is prepack
        assert kwargs['read_before_answer'] is prepack
        assert kwargs['check_answer_scope'] is False
        assert kwargs['max_additional_rounds'] == 0 and kwargs['max_context_chars'] == 12000
        pool = kwargs['retrieve']('Модельная формулировка.', ['осмотр', 'блокировка'], ['Условия'], {},
            search_focus=None, constraint_question=search_question)
        rank_context = agent.retrieve_context.call_args.kwargs['query_rank_context']
        assert isinstance(rank_context, QueryRankContext) and rank_context.question == RESOLVED
        if enabled:
            assert kwargs['admit'](UserTask(question, search_question), pool,
                                  max_chars=713, constraint_question=search_question) == pool
            assert calls[-1][1] == 713 and calls[-1][2] is rank_context
        selected = kwargs['select']('Модельная формулировка.', pool, ['Условия'],
                                   search_focus=None, constraint_question=search_question)
        assert selected.evidence
        return CorrectiveResult('Scripted result.', selected.evidence, {})
    monkeypatch.setattr('corrective_rag.run_corrective', orchestration)
    before = deepcopy((aliases, excluded, versions, domains))
    answer, sources, cached = agent._ask_corrective(RAW, RESOLVED, 'Fixed prompt',
        source_aliases=aliases, excluded_sources=excluded, source_versions=versions,
        source_domains=domains, retrieval_focus='rre')
    assert answer == 'Scripted result.' and sources and cached is False
    native = agent.retrieve_context.call_args.kwargs
    assert native['constraint_question'] == RESOLVED
    assert native['source_aliases'] is aliases and native['excluded_sources'] is excluded
    assert native['source_versions'] is versions and native['source_domains'] is domains
    assert native['retrieval_focus'] == 'rre'
    assert ('candidate_pool_only' in native) is prepack
    packed = agent._section_evidence_index.select_v2.call_args.kwargs
    assert packed['constraint_question'] == RESOLVED and packed['allowed_sources'] == {'visible.md'}
    assert packed['query_rank_context'] is native['query_rank_context']
    assert len(calls) == int(enabled)
    assert (aliases, excluded, versions, domains) == before
    agent.llm.complete.assert_not_called()


def test_enabled_admission_uses_the_fresh_rank_context_of_each_retrieval_round(monkeypatch):
    monkeypatch.setenv('RAG_QUERY_DIVERSE_ADMISSION', 'on')
    agent, seen, contexts = agent_for(), [], []
    def native_retrieve(*args, **kwargs):
        context = kwargs['query_rank_context']
        own = {**ROW, 'text': FACT + f' Native round {len(contexts)}.'}
        context.record(RESOLVED, [own])
        contexts.append((context, own))
        return [own]
    agent.retrieve_context.side_effect = native_retrieve
    def ordering(pool, *, max_chars, query_rank_context):
        seen.append(query_rank_context)
        assert query_rank_context is contexts[-1][0]
        assert query_rank_context.question == RESOLVED and query_rank_context.origins(pool[0])
        if len(contexts) > 1:
            assert not query_rank_context.origins(contexts[0][1])
        return list(pool)
    monkeypatch.setattr(candidate_admission, 'order_by_query_coverage', ordering)
    def orchestration(question, search_question, *, retrieve, admit, **kwargs):
        for probe in ('Исходная гипотеза поиска.', search_question):
            pool = retrieve(probe, [probe], [probe], {}, search_focus=None,
                            constraint_question=search_question)
            admit(UserTask(question, search_question), pool, max_chars=24000,
                  constraint_question=search_question)
        return CorrectiveResult('Scripted result.', [], {})
    monkeypatch.setattr('corrective_rag.run_corrective', orchestration)
    agent._ask_corrective(RAW, RESOLVED, None)
    assert len(seen) == 2 and seen[0] is not seen[1]
    assert not any(isinstance(value, QueryRankContext) for value in vars(agent).values())


@pytest.mark.parametrize('enabled', [False, True])
def test_actual_run_corrective_passes_typed_raw_task_to_admission_without_an_extra_model_call(monkeypatch, enabled):
    monkeypatch.setenv('RAG_QUERY_DIVERSE_ADMISSION', 'on' if enabled else 'off')
    agent = agent_for()
    complete_calls, boundary_calls, rank_contexts = [], [], []
    def complete(prompt):
        if prompt.startswith('Планируй поиск'):
            stage = 'planning'
            output = {'canonical_question': 'Условия осмотра и блокировки',
                      'requirements': ['Какие различия условий?'], 'queries': ['осмотр блокировка']}
        elif prompt.startswith('Оцени полноту'):
            stage = 'coverage'
            output = {'coverage': [{'requirement_id': 'R1', 'passage_ids': ['E1:P1'], 'applicable': True}],
                      'missing_queries': [], 'conflicts': [],
                      'task_alignment': {'status': 'matched', 'passage_ids': ['E1:P1']}}
        elif prompt.startswith('Проверь доказательность'):
            stage = 'grounding'
            data = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]
            output = {'claims': [{'claim_id': row['claim_id'], 'support': 'supported',
                'context_ids': row['context_ids'], 'passage_ids': ['E1:P1'], 'task_relevance': 'matched'}
                for row in data['answer_claims']]}
        else:
            stage, output = 'answer', None
        complete_calls.append(stage)
        return FACT + ' [E1]' if output is None else json.dumps(output)
    agent.llm.complete = complete
    def native_retrieve(*args, **kwargs):
        rank_contexts.append(kwargs['query_rank_context'])
        kwargs['query_rank_context'].record(RESOLVED, [ROW])
        return [deepcopy(ROW)]
    agent.retrieve_context.side_effect = native_retrieve
    actual_apply = candidate_admission.apply_candidate_ordering
    def observed(user_task, items, *, max_chars, constraint_question, admit):
        boundary_calls.append((user_task, constraint_question, admit))
        assert user_task == UserTask(RAW, RESOLVED) and constraint_question == RESOLVED
        return actual_apply(user_task, items, max_chars=max_chars,
                            constraint_question=constraint_question, admit=admit)
    monkeypatch.setattr(candidate_admission, 'apply_candidate_ordering', observed)
    original_ordering = candidate_admission.order_by_query_coverage
    def observed_ordering(items, *, max_chars, query_rank_context):
        assert query_rank_context is rank_contexts[-1] and query_rank_context.question == RESOLVED
        return original_ordering(items, max_chars=max_chars, query_rank_context=query_rank_context)
    ordering = Mock(side_effect=observed_ordering)
    monkeypatch.setattr(candidate_admission, 'order_by_query_coverage', ordering)
    answer, _, cached = agent._ask_corrective(RAW, RESOLVED, None, source_aliases={'visible.md': 'Visible'})
    assert FACT in answer and cached is False
    assert complete_calls == ['planning', 'coverage', 'answer', 'grounding']
    assert len(boundary_calls) == 1 and (boundary_calls[0][2] is not None) is enabled
    assert ordering.call_count == int(enabled)
    state = agent.last_retrieval_trace()['rounds'][0]['retrieval']['candidate_ordering']
    assert state['status'] == ('applied' if enabled else 'disabled')
