"""Release-key invalidation through real Python/UI-Rust cache boundaries.

Only generation transports, configuration reads and storage are in-memory
fakes. No native Rust binary, Redis server, provider or model is exercised.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import rag_agent
import web_ui
from response_cache import ResponseCache


OLD_VERSION = 'v39-source-scope-and-math-2026-10-22'
QUESTION = 'Объясни правило проверки устройства.'
MODEL = 'synthetic-cache-model'
CORPUS_SNAPSHOT = 123456789
OLD_ANSWER = 'Ранее сохраненный синтетический ответ.'
NEW_ANSWER = 'Новый синтетический ответ.'


class MemoryRedis:
    def __init__(self):
        self.data = {}
        self.ttls = {}
        self.scan_calls = 0
        self.delete_calls = 0

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key], self.ttls[key] = value, ttl

    def scan_iter(self, **kwargs):
        self.scan_calls += 1
        raise AssertionError('A version bump must not purge cached data')

    def delete(self, *keys):
        self.delete_calls += 1
        raise AssertionError('A version bump must not delete cached data')


def evidence(file='global.md'):
    return {'file': file, 'section_path': 'Правило проверки', 'score': 1.0,
            'text': 'Проверку устройства выполняет оператор по утвержденной инструкции.'}


def agent_with_cache(cache):
    agent = object.__new__(rag_agent.RAGAgent)
    agent.response_cache = cache
    agent._generation_state = SimpleNamespace(from_cache=False)
    agent._docstore_mtime_ns = CORPUS_SNAPSHOT
    agent.llm_model = MODEL
    agent.llm = SimpleNamespace(complete=Mock(return_value=NEW_ANSWER))
    return agent


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Release cache tests must not call models/network')

    monkeypatch.setattr('requests.sessions.Session.request', blocked)
    # Request-scoped pytest restoration only; no persisted config or env writes.
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE', 'off')


def assert_no_purge(client):
    assert client.scan_calls == client.delete_calls == 0


def test_release_version_changes_v39_keys_and_ui_imports_the_same_version():
    assert rag_agent.CACHE_VERSION != OLD_VERSION
    assert web_ui.CACHE_VERSION == rag_agent.CACHE_VERSION


def test_python_actual_generation_boundary_misses_v39_then_hits_current_without_purge():
    client = MemoryRedis()
    cache = ResponseCache(client=client, ttl_seconds=120)
    agent = agent_with_cache(cache)
    agent.llm.complete.side_effect = [OLD_ANSWER, NEW_ANSWER]
    context = [evidence()]

    with patch.object(rag_agent, 'CACHE_VERSION', OLD_VERSION):
        previous = agent._generate_grounded_answer(QUESTION, context, [], cache_enabled=True)
        assert not agent.last_generation_from_cache
    old_entries = dict(client.data)
    assert previous == OLD_ANSWER and len(old_entries) == 1

    # Identical question, model, corpus snapshot, prompt and evidence. Only the
    # release version differs; changed retrieval cannot accidentally mask a bug.
    current = agent._generate_grounded_answer(QUESTION, context, [], cache_enabled=True)
    assert current == NEW_ANSWER
    assert not agent.last_generation_from_cache
    assert len(client.data) == 2
    assert all(client.data[key] == value for key, value in old_entries.items())
    assert agent._generate_grounded_answer(QUESTION, context, [], cache_enabled=True) == NEW_ANSWER
    assert agent.last_generation_from_cache
    assert agent.llm.complete.call_count == 2
    assert agent.llm.complete.call_args_list[0] == agent.llm.complete.call_args_list[1]
    assert_no_purge(client)


def test_rust_ui_cache_boundary_misses_v39_then_hits_current_without_native_runtime():
    client = MemoryRedis()
    cache = ResponseCache(client=client, ttl_seconds=120)
    context = [evidence()]
    with patch.object(web_ui, 'load_provider_config', return_value={'provider': 'ollama', 'model': MODEL}), \
         patch.object(web_ui, 'get_agent', return_value=SimpleNamespace(response_cache=cache)), \
         patch.object(web_ui, '_run_rust_json', side_effect=[{'answer': OLD_ANSWER}, {'answer': NEW_ANSWER}]) as generate:
        with patch.object(web_ui, 'CACHE_VERSION', OLD_VERSION):
            assert web_ui._rust_generate(QUESTION, context, cache_enabled=True) == (OLD_ANSWER, False)
        old_entries = dict(client.data)
        assert len(old_entries) == 1
        assert web_ui._rust_generate(QUESTION, context, cache_enabled=True) == (NEW_ANSWER, False)
        assert len(client.data) == 2
        assert all(client.data[key] == value for key, value in old_entries.items())
        assert web_ui._rust_generate(QUESTION, context, cache_enabled=True) == (NEW_ANSWER, True)
        assert generate.call_count == 2
        assert generate.call_args_list[0] == generate.call_args_list[1]
    assert_no_purge(client)


def prepare_ask_agent(context):
    client = MemoryRedis()
    cache = ResponseCache(client=client, ttl_seconds=120)
    cache.get = Mock(wraps=cache.get)
    cache.set = Mock(wraps=cache.set)
    agent = agent_with_cache(cache)
    agent._all_nodes = [object()]
    agent._refresh_docstore_if_changed = Mock()
    agent.understand_query = Mock(return_value=rag_agent.QueryUnderstanding(
        QUESTION, ('fact',), (QUESTION,)))
    agent.retrieve_context = Mock(return_value=context)
    return agent, client


@pytest.mark.parametrize('context,retrieval_query', [
    ([evidence('private.md')], None),
    ([evidence(), evidence('private.md')], None),
    ([evidence()], 'Предыдущий вопрос о другом устройстве. Уточнение: ' + QUESTION),
    ([], None),
], ids=['private', 'mixed', 'contextual-history', 'empty'])
def test_actual_ask_does_not_read_or_write_shared_answer_cache_for_nonshared_requests(context, retrieval_query):
    agent, client = prepare_ask_agent(context)
    answer, _, from_cache = agent.ask(QUESTION, 'private project prompt',
        retrieval_query=retrieval_query, shared_cache_sources={'global.md'},
        shared_system_prompt='common global prompt', pipeline_mode='off')
    assert answer == NEW_ANSWER and not from_cache
    agent.response_cache.get.assert_not_called()
    agent.response_cache.set.assert_not_called()
    assert agent.llm.complete.call_count == 1
    assert_no_purge(client)


def test_actual_ask_still_reuses_a_shared_global_answer_for_an_unchanged_question():
    agent, client = prepare_ask_agent([evidence()])
    first = agent.ask(QUESTION, 'private project prompt', shared_cache_sources={'global.md'},
        shared_system_prompt='common global prompt', pipeline_mode='off')
    second = agent.ask(QUESTION, 'different private project prompt', shared_cache_sources={'global.md'},
        shared_system_prompt='common global prompt', pipeline_mode='off')
    assert first[0] == second[0] == NEW_ANSWER
    assert not first[2] and second[2]
    assert agent.llm.complete.call_count == 1
    assert 'private project prompt' not in agent.llm.complete.call_args.args[0]
    assert agent.response_cache.get.call_count == 2
    assert agent.response_cache.set.call_count == 1
    assert_no_purge(client)


def test_existing_shared_source_catalogue_rejects_ambiguous_private_aliases():
    documents = [
        {'source_path': '/global/global.md', 'file_name': 'Общий.md', 'status': 'ready', 'owner_subject': None},
        {'source_path': '/private/private.md', 'file_name': 'Общий.md', 'status': 'ready', 'owner_subject': 'user-2'},
    ]
    with patch.object(web_ui, 'list_all_documents_for_retrieval', return_value=documents):
        shared = web_ui.shared_global_document_sources()
    assert 'global.md' in shared
    assert not {'private.md', 'общий.md'} & shared
    assert not web_ui.context_is_shared_global([evidence(), evidence('private.md')], shared)
