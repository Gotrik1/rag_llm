"""Unknown negative scope must survive ordinary retrieval and publication."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import rag_agent
from evidence_selection import SectionEvidenceIndex
from response_cache import ResponseCache


QUESTION = 'Проверка устройства; не рассматривай архивные заявки.'
PRIMARY = 'Проверка устройства выполняется оператором по утвержденной инструкции.'
EXCEPTION = 'Архивные заявки являются исключением из данного порядка.'
NOTICE = ('Связанные условия источника не удалось полностью отделить от исключённой темы; '
          'эту часть ответа нужно проверить.')


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value

    def scan_iter(self, *args, **kwargs):
        raise AssertionError('No shared-cache scan')

    def delete(self, *args, **kwargs):
        raise AssertionError('No shared-cache deletion')


def row(**extra):
    return {'file': 'neutral-procedure.md', 'section_path': 'Порядок проверки',
            'score': 1.0, 'text': PRIMARY, **extra}


def agent_and_index():
    node = SimpleNamespace(text=PRIMARY + '\n\n' + EXCEPTION,
        metadata={'file_name': 'neutral-procedure.md', 'section_path': 'Порядок проверки',
                  'section_title': 'Проверка устройства', 'source_id': 'neutral-source',
                  'revision_id': 'revision-A', 'content_hash': 'revision-A', 'status': 'active'})
    agent = object.__new__(rag_agent.RAGAgent)
    agent._all_nodes = [node]
    agent._refresh_docstore_if_changed = Mock()
    agent._build_query_engine = Mock(return_value=SimpleNamespace(
        retriever=SimpleNamespace(retrieve=Mock(return_value=[]))))
    index = SectionEvidenceIndex(agent._all_nodes)
    agent._section_evidence_index = index
    agent._section_evidence_nodes = agent._all_nodes
    agent._generation_state = SimpleNamespace(from_cache=False)
    agent._docstore_mtime_ns = 123
    agent.llm_model = 'synthetic-no-network-model'
    agent.llm = SimpleNamespace(complete=Mock(return_value=PRIMARY +
        ' [Источник: neutral-procedure.md — раздел «Порядок проверки»]'),
        metadata=SimpleNamespace(context_window=32768))
    client = MemoryRedis()
    agent.response_cache = ResponseCache(client=client, ttl_seconds=120)
    agent.understand_query = Mock(return_value=rag_agent.QueryUnderstanding(
        QUESTION, ('fact',), (QUESTION,)))
    return agent, index, client


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('RAG_EVIDENCE_SELECTION_MODE', raising=False)
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE', 'off')
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')
    monkeypatch.setattr('requests.sessions.Session.request', Mock(
        side_effect=AssertionError('No network/model calls in negative-scope contracts')))


def test_actual_coverage_unknown_is_summarized_without_native_coordinates():
    agent, _, _ = agent_and_index()
    trace = {}
    context = agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,),
        selection_mode='coverage', retrieval_trace=trace)
    assert any(item.get('selection_signals', {}).get('negative_scope_unresolved') for item in context)
    assert trace['negative_scope_unresolved'] is True
    assert 'negative_scope_excluded_block_indices' not in trace
    assert 'negative_scope_unresolved_block_indices' not in trace


def test_omitted_atomic_unknown_reaches_trace_even_without_a_signal_carrier():
    agent, index, _ = agent_and_index()
    def select(*args, **kwargs):
        metadata = kwargs.get('selection_meta')
        assert isinstance(metadata, dict), 'Coverage must receive fresh selection metadata'
        metadata.update(negative_scope_unresolved=True,
            negative_scope_atomic_conflict_incomplete=True, secret_source_title='private-name')
        return [row()]
    index.select_v2 = Mock(side_effect=select)
    trace = {}
    agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,),
        selection_mode='coverage', retrieval_trace=trace)
    assert trace['negative_scope_unresolved'] is True
    assert trace['negative_scope_atomic_conflict_incomplete'] is True
    assert 'secret_source_title' not in trace and 'private-name' not in str(trace)


def test_reused_diagnostic_trace_does_not_inherit_previous_unknown():
    agent, _, _ = agent_and_index()
    trace = {'negative_scope_unresolved': True, 'negative_scope_atomic_conflict_incomplete': True}
    agent.retrieve_context('Объясни порядок проверки устройства.',
        retrieval_queries=('проверка устройства',), selection_mode='coverage', retrieval_trace=trace)
    assert 'negative_scope_unresolved' not in trace
    assert 'negative_scope_atomic_conflict_incomplete' not in trace


def test_ordinary_ask_passes_captured_omission_metadata_to_writer():
    agent, _, _ = agent_and_index()
    def retrieve(*args, **kwargs):
        kwargs['retrieval_trace'].update(negative_scope_unresolved=True,
            negative_scope_atomic_conflict_incomplete=True)
        return [row()]
    agent.retrieve_context = Mock(side_effect=retrieve)
    agent._generate_grounded_answer = Mock(return_value='Ответ.')
    agent.ask(QUESTION, selection_mode='coverage')
    assert agent._generate_grounded_answer.call_args.kwargs['negative_scope'] == {
        'negative_scope_unresolved': True, 'negative_scope_atomic_conflict_incomplete': True}


@pytest.mark.parametrize('origin', ('context', 'metadata'))
def test_writer_receives_unknown_instruction_and_publishes_caution(origin):
    agent, _, _ = agent_and_index()
    context = [row(selection_signals={'negative_scope_unresolved': True})] if origin == 'context' else [row()]
    options = {'negative_scope': {'negative_scope_unresolved': True}} if origin == 'metadata' else {}
    answer = agent._generate_grounded_answer(QUESTION, context, [],
        selection_mode='coverage', **options)
    prompt = agent.llm.complete.call_args_list[0].args[0]
    assert 'ПОЛНОТА ИСКЛЮЧЕНИЯ ТЕМ НЕ ПОДТВЕРЖДЕНА' in prompt
    assert 'Не объявляй полное соблюдение исключения' in prompt
    assert answer.count(NOTICE) == 1


def test_unknown_partitions_same_prompt_cache_and_notice_is_not_duplicated():
    agent, _, client = agent_and_index()
    common = {'selection_mode': 'coverage', 'cache_enabled': True}
    clean = agent._generate_grounded_answer(QUESTION, [row()], [], **common)
    old_keys = set(client.data)
    assert NOTICE not in clean
    options = {**common, 'negative_scope': {'negative_scope_unresolved': True}}
    first = agent._generate_grounded_answer(QUESTION, [row()], [], **options)
    assert agent._generation_state.from_cache is False
    assert old_keys < set(client.data)
    second = agent._generate_grounded_answer(QUESTION, [row()], [], **options)
    assert agent._generation_state.from_cache is True
    assert first == second and second.count(NOTICE) == 1
    assert agent.llm.complete.call_count == 2


@pytest.mark.parametrize('signal', (False, 'false', None))
def test_writer_does_not_invent_unknown_for_absent_or_malformed_flags(signal):
    agent, _, _ = agent_and_index()
    answer = agent._generate_grounded_answer(QUESTION,
        [row(selection_signals={'negative_scope_unresolved': signal})], [], selection_mode='coverage')
    assert NOTICE not in answer
    assert 'ПОЛНОТА ИСКЛЮЧЕНИЯ ТЕМ НЕ ПОДТВЕРЖДЕНА' not in agent.llm.complete.call_args.args[0]


def test_extractive_return_keeps_unknown_notice_without_calling_model(monkeypatch):
    agent, _, _ = agent_and_index()
    monkeypatch.setattr(rag_agent, 'generate_extractive_value_answer', lambda *args: 'Точное значение.')
    answer = agent._generate_grounded_answer(QUESTION,
        [row(selection_signals={'negative_scope_unresolved': True})], [], selection_mode='coverage')
    agent.llm.complete.assert_not_called()
    assert answer == 'Точное значение.\n\n' + NOTICE
