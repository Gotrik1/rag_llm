"""The ordinary Python writer can opt into coverage without corrective RAG."""
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import rag_agent
from evidence_selection import SectionEvidenceIndex
from response_cache import ResponseCache


QUESTION = 'Объясни правило проверки устройства.'
TEXT = 'Проверку устройства выполняет оператор по утвержденной инструкции.'


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value

    def scan_iter(self, *args, **kwargs):
        raise AssertionError('Selector changes must not scan or clear shared cache')

    def delete(self, *args, **kwargs):
        raise AssertionError('Selector changes must not delete shared cache')


def evidence(file='global.md'):
    return {'file': file, 'section_path': 'Правило проверки', 'score': 1.0, 'text': TEXT}


def ask_agent(context=None):
    agent = object.__new__(rag_agent.RAGAgent)
    client = MemoryRedis()
    agent.response_cache = ResponseCache(client=client, ttl_seconds=120)
    agent.response_cache.get = Mock(wraps=agent.response_cache.get)
    agent.response_cache.set = Mock(wraps=agent.response_cache.set)
    agent._generation_state = SimpleNamespace(from_cache=False)
    agent._docstore_mtime_ns = 123456789
    agent.llm_model = 'synthetic-cache-model'
    agent.llm = SimpleNamespace(complete=Mock(return_value=TEXT +
        ' [Источник: global.md — раздел «Правило проверки»]'))
    agent._all_nodes = [object()]
    agent._refresh_docstore_if_changed = Mock()
    agent.understand_query = Mock(return_value=rag_agent.QueryUnderstanding(
        QUESTION, ('fact',), (QUESTION,)))
    agent.retrieve_context = Mock(return_value=[evidence()] if context is None else context)
    return agent, client


def retrieval_agent():
    node = SimpleNamespace(text=TEXT, metadata={'file_name': 'global.md',
        'section_path': 'Правило проверки', 'status': 'active'})
    agent = object.__new__(rag_agent.RAGAgent)
    agent._all_nodes = [node]
    agent._refresh_docstore_if_changed = Mock()
    agent._build_query_engine = Mock(return_value=SimpleNamespace(
        retriever=SimpleNamespace(retrieve=Mock(return_value=[]))))
    index = SectionEvidenceIndex(agent._all_nodes)
    agent._section_evidence_index = index
    agent._section_evidence_nodes = agent._all_nodes
    index.select = Mock(wraps=index.select)
    index.select_v2 = Mock(wraps=index.select_v2)
    return agent, index


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv('RAG_EVIDENCE_SELECTION_MODE', raising=False)
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE', 'off')
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')
    monkeypatch.setattr('requests.sessions.Session.request', Mock(
        side_effect=AssertionError('No network/model calls in selector contracts')))


@pytest.mark.parametrize(('env', 'explicit', 'expected', 'source'), [
    (None, None, 'legacy', 'default'),
    ('coverage', None, 'coverage', 'environment'),
    ('legacy', 'coverage', 'coverage', 'explicit'),
    ('coverage', 'legacy', 'legacy', 'explicit'),
    ('invalid-env', 'legacy', 'legacy', 'explicit'),
])
def test_ordinary_ask_resolves_mode_once_and_passes_it_to_retrieval_and_writer(
        monkeypatch, env, explicit, expected, source):
    if env is not None:
        monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', env)
    agent, _ = ask_agent()
    writer = Mock(return_value='Ответ.')
    agent._generate_grounded_answer = writer
    options = {'selection_mode': explicit} if explicit is not None else {}
    agent.ask(QUESTION, **options)
    assert agent.retrieve_context.call_args.kwargs['selection_mode'] == expected
    assert writer.call_args.kwargs['selection_mode'] == expected
    assert agent.last_retrieval_trace()['selection_mode_source'] == source
    # A mocked retrieval never called a selector: do not claim it did.
    assert agent.last_retrieval_trace()['selection_applied'] is False


@pytest.mark.parametrize('value', ['on', 'true', 'v2', '', 'unexpected'])
def test_invalid_env_is_rejected_before_understanding_or_generation(monkeypatch, value):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', value)
    agent, _ = ask_agent()
    with pytest.raises(ValueError, match='RAG_EVIDENCE_SELECTION_MODE'):
        agent.ask(QUESTION)
    agent.understand_query.assert_not_called()
    agent.llm.complete.assert_not_called()


@pytest.mark.parametrize('value', [False, 2, 'on'])
def test_invalid_explicit_mode_does_not_coerce_or_silently_fall_back(value):
    agent, _ = ask_agent()
    with pytest.raises(ValueError, match='selection_mode'):
        agent.ask(QUESTION, selection_mode=value)


def test_env_change_during_request_cannot_change_captured_selector_or_cache(monkeypatch):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'coverage')
    agent, _ = ask_agent()
    understanding = agent.understand_query.return_value
    def understand(*args, **kwargs):
        monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'legacy')
        return understanding
    agent.understand_query.side_effect = understand
    agent._generate_grounded_answer = Mock(return_value='Ответ.')
    agent.ask(QUESTION)
    assert agent.retrieve_context.call_args.kwargs['selection_mode'] == 'coverage'
    assert agent._generate_grounded_answer.call_args.kwargs['selection_mode'] == 'coverage'


def test_corrective_branch_ignores_selector_env_but_rejects_explicit_selector(monkeypatch):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'invalid-even-here')
    agent, _ = ask_agent()
    agent._ask_corrective = Mock(return_value=('corrective', [], False))
    assert agent.ask(QUESTION, pipeline_mode='on')[0] == 'corrective'
    with pytest.raises(ValueError, match='corrective'):
        agent.ask(QUESTION, pipeline_mode='on', selection_mode='legacy')
    assert agent._ask_corrective.call_count == 1


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_actual_selector_and_applied_trace_agree(mode):
    agent, index = retrieval_agent()
    trace = {}
    context = agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,),
        selection_mode=mode, retrieval_trace=trace)
    assert context
    assert index.select_v2.call_count == int(mode == 'coverage')
    assert index.select.call_count == int(mode == 'legacy')
    assert trace['selection_mode'] == mode
    assert trace['selection_applied'] is True


def test_direct_retrieval_none_preserves_legacy_even_when_env_requests_coverage(monkeypatch):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'coverage')
    agent, index = retrieval_agent()
    trace = {}
    agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,), retrieval_trace=trace)
    index.select.assert_called_once()
    index.select_v2.assert_not_called()
    assert trace['selection_mode'] == 'legacy'


def test_failed_selector_does_not_report_applied():
    agent, index = retrieval_agent()
    index.select_v2.side_effect = RuntimeError('synthetic selector failure')
    trace = {}
    with pytest.raises(RuntimeError, match='synthetic'):
        agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,),
            selection_mode='coverage', retrieval_trace=trace)
    assert trace['selection_applied'] is False
    assert trace['selection_mode'] == 'not_applied'


@pytest.mark.parametrize(('question', 'options', 'reason'), [
    (QUESTION, {'candidate_pool_only': True}, 'candidate_pool_only'),
    ('В каких документах упоминается проверка устройства?', {}, 'document_occurrence'),
])
def test_bypassed_selector_is_not_reported_applied(question, options, reason):
    agent, index = retrieval_agent()
    trace = {}
    agent.retrieve_context(question, retrieval_queries=(question,),
        selection_mode='coverage', retrieval_trace=trace, **options)
    index.select.assert_not_called()
    index.select_v2.assert_not_called()
    assert trace['selection_applied'] is False
    assert trace['selection_bypass_reason'] == reason


def test_identical_prompt_different_selector_misses_without_deleting_old_cache():
    agent, client = ask_agent()
    shared = {'shared_cache_sources': {'global.md'}, 'shared_system_prompt': 'Общий промпт.'}
    assert agent.ask(QUESTION, selection_mode='legacy', **shared)[2] is False
    old_keys = set(client.data)
    assert agent.ask(QUESTION, selection_mode='coverage', **shared)[2] is False
    assert old_keys < set(client.data)
    assert agent.ask(QUESTION, selection_mode='coverage', **shared)[2] is True
    assert agent.ask(QUESTION, selection_mode='legacy', **shared)[2] is True
    assert agent.llm.complete.call_count == 2


def test_pre_selector_cache_namespace_is_preserved_but_never_reused():
    agent, client = ask_agent()
    system_prompt = 'Общий промпт.'
    # Obtain the unchanged writer prompt, then seed the pre-switch cache key.
    agent._generate_grounded_answer(QUESTION, [evidence()], [],
        system_prompt=system_prompt, evidence_question=QUESTION)
    prompt = agent.llm.complete.call_args.args[0]
    old_key = hashlib.sha256((f'{rag_agent.CACHE_VERSION}\ngeneration-runtime-v1\n'
        f'{agent._docstore_mtime_ns}\n{agent._runtime_identity(None)}\n{prompt}').encode()).hexdigest()
    agent.response_cache.set(old_key, {'answer': 'Устаревший ответ.',
        'runtime_identity': agent._runtime_identity(None), 'producer': agent._producer(None)})
    old_keys = set(client.data)
    agent.llm.complete.reset_mock()
    result = agent.ask(QUESTION, selection_mode='legacy',
        shared_cache_sources={'global.md'}, shared_system_prompt=system_prompt)
    assert result[2] is False
    assert 'Устаревший' not in result[0]
    agent.llm.complete.assert_called_once()
    assert old_keys < set(client.data)


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
@pytest.mark.parametrize(('context', 'retrieval_query', 'shared_prompt'), [
    ([evidence('private.md')], None, 'Общий промпт.'),
    ([evidence(), evidence('private.md')], None, 'Общий промпт.'),
    ([evidence()], 'Контекст предыдущего вопроса.', 'Общий промпт.'),
    ([], None, 'Общий промпт.'),
    ([evidence()], None, None),
])
def test_selector_never_weakens_shared_cache_privacy(mode, context, retrieval_query, shared_prompt):
    agent, client = ask_agent(context)
    agent.ask(QUESTION, selection_mode=mode, retrieval_query=retrieval_query,
        shared_cache_sources={'global.md'}, shared_system_prompt=shared_prompt)
    agent.response_cache.get.assert_not_called()
    agent.response_cache.set.assert_not_called()
    assert client.data == {}


def test_cli_debug_uses_same_env_mode_and_records_actual_trace(monkeypatch, capsys):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'coverage')
    agent, index = retrieval_agent()
    agent.understand_query = Mock(return_value=rag_agent.QueryUnderstanding(QUESTION, ('fact',), (QUESTION,)))
    agent.debug_retrieval(QUESTION)
    index.select_v2.assert_called_once()
    assert agent.last_retrieval_trace()['selection_mode'] == 'coverage'
    assert agent.last_retrieval_trace()['selection_applied'] is True
    assert 'coverage' in capsys.readouterr().out


def debug_handler(monkeypatch, mode='python', adapter=None):
    import web_ui
    agent, index = retrieval_agent()
    agent.understand_query = Mock(return_value=rag_agent.QueryUnderstanding(QUESTION, ('fact',), (QUESTION,)))
    if adapter is not None:
        agent.retrieve_context = adapter
    monkeypatch.setattr(web_ui, 'get_agent', lambda: agent)
    monkeypatch.setattr(web_ui, 'load_provider_config', lambda: {})
    monkeypatch.setattr(web_ui, 'load_flow_mode', lambda: mode)
    monkeypatch.setattr(web_ui, 'get_project_conversation', lambda *args: {})
    monkeypatch.setattr(web_ui, 'retrieval_document_scope', lambda *args: (set(), {'global.md': 'global.md'}))
    monkeypatch.setattr(web_ui, 'shared_global_document_sources', lambda: {'global.md'})
    monkeypatch.setattr(web_ui, 'contextualize_retrieval_question', lambda *args: QUESTION)
    monkeypatch.setattr(web_ui, 'rust_context', lambda *args: [])
    handler = object.__new__(web_ui.Handler)
    handler.send_json = Mock()
    return handler, index


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_python_http_debug_reports_actual_selector_independently_of_answer(monkeypatch, mode):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', mode)
    handler, index = debug_handler(monkeypatch)
    handler.handle_debug({'question': QUESTION, 'chat_id': 'synthetic-chat'})
    payload = handler.send_json.call_args.args[0]
    assert payload['context']
    assert payload['retrieval_trace']['selection_mode'] == mode
    assert payload['retrieval_trace']['selection_applied'] is True
    assert index.select_v2.call_count == int(mode == 'coverage')
    assert index.select.call_count == int(mode == 'legacy')


def test_python_http_debug_rejects_unsupported_coverage_without_silent_fallback(monkeypatch):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'coverage')
    def adapter(question, top_k, excluded_sources=None, source_aliases=None, priority_sources=None):
        raise AssertionError('Legacy-only adapter must not run for coverage request')
    handler, _ = debug_handler(monkeypatch, adapter=adapter)
    with pytest.raises(RuntimeError, match='coverage'):
        handler.handle_debug({'question': QUESTION, 'chat_id': 'synthetic-chat'})


@pytest.mark.parametrize('mode', ['hybrid', 'rust'])
def test_http_debug_other_flows_do_not_inherit_python_selector_env(monkeypatch, mode):
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE', 'invalid-for-python')
    handler, index = debug_handler(monkeypatch, mode=mode)
    handler.handle_debug({'question': QUESTION, 'chat_id': 'synthetic-chat'})
    index.select.assert_called_once()
    index.select_v2.assert_not_called()
