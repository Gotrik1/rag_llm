"""Actual ordinary coverage writer: mathematical fidelity is a publication gate."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import rag_agent


SUPPORTED = 'Результат сохраняют в журнале проверки.'
REFERENCE = ' [Источник: neutral-rules.md — раздел «Расчёт»]'


class Cache:
    def __init__(self):
        self.data = {}
        self.get = Mock(side_effect=lambda key: self.data.get(key))
        self.set = Mock(side_effect=lambda key, value: self.data.__setitem__(key, deepcopy(value)))


def make_agent(answer):
    result = object.__new__(rag_agent.RAGAgent)
    result.llm_model = 'synthetic-formula-model'
    result.llm = SimpleNamespace(complete=Mock(return_value=answer),
        metadata=SimpleNamespace(context_window=32768))
    result._generation_state = SimpleNamespace(from_cache=False)
    result._docstore_mtime_ns = 98
    result.response_cache = Cache()
    return result


def evidence(formula):
    return [{'file': 'neutral-rules.md', 'section_path': 'Расчёт',
        'source_id': 'neutral-source', 'revision_id': 'r-A',
        'text': '[MATH: ' + formula + ']\n\n' + SUPPORTED, 'score': 1.0}]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', Mock(side_effect=AssertionError('No network')))
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE', 'off')


@pytest.mark.parametrize(('source', 'unsupported'), [
    ('X=A*(B *, +,- K)', r'$$X=A\times B\times K$$'),
    ('F_sub(a,2)=2*V_sub(a,2)', r'$$F_{a,2}=3\times V_{a,2}$$'),
    ('F_sub(a,2)=2*V_sub(a,2)', r'$$F_{a,2}=2\times V_{a,3}$$'),
    ('T=a+b+c', r'$$T=a+b$$'),
    ('P=С*V', r'$$P=C\times V$$'),
], ids=['operator-alternatives', 'numeric-mutation', 'index-mutation', 'dropped-term', 'cyrillic-latin'])
@pytest.mark.parametrize('cache_enabled', [False, True], ids=['fresh', 'cache-eligible'])
def test_unsupported_formula_is_not_published_or_cached(source, unsupported, cache_enabled):
    agent = make_agent(unsupported + REFERENCE + '\n\n' + SUPPORTED + REFERENCE)
    context = evidence(source)
    before = deepcopy(context)
    answer = agent._generate_grounded_answer('Объясни правило расчёта.', context, [],
        selection_mode='coverage', cache_enabled=cache_enabled)
    assert isinstance(answer, str) and answer.strip()
    assert context == before
    assert unsupported not in answer
    assert SUPPORTED in answer
    assert agent.llm.complete.call_count <= 3
    for call in agent.response_cache.set.call_args_list:
        assert unsupported not in call.args[1]['answer']


@pytest.mark.parametrize(('source', 'correct'), [
    ('X=A+B', r'$$X=A+B$$'),
    ('X=A+B', r'$$X=B+A$$'),
    ('F_sub(a,2)=2*V_sub(a,2)', r'$$F_{a,2}=2\times V_{a,2}$$'),
])
def test_exact_or_proved_equivalent_formula_remains_useful(source, correct):
    agent = make_agent(correct + REFERENCE + '\n\n' + SUPPORTED + REFERENCE)
    answer = agent._generate_grounded_answer('Объясни правило расчёта.', evidence(source), [],
        selection_mode='coverage', cache_enabled=True)
    assert correct in answer
    assert SUPPORTED in answer


def test_cached_draft_cannot_skip_current_formula_contract():
    unsafe = r'$$X=A-B$$' + REFERENCE + '\n\n' + SUPPORTED + REFERENCE
    agent = make_agent(unsafe)
    agent.response_cache.get = Mock(return_value={'answer': unsafe,
        'runtime_identity': agent._runtime_identity(None), 'producer': agent._producer(None)})
    answer = agent._generate_grounded_answer('Объясни правило расчёта.', evidence('X=A+B'), [],
        selection_mode='coverage', cache_enabled=True)
    assert r'$$X=A-B$$' not in answer
    assert SUPPORTED in answer
    assert agent.last_generation_from_cache is False
