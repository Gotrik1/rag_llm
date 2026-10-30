"""A short formula request must receive the same coverage publication gates."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
import rag_agent
from test_ordinary_formula_fidelity import make_agent, evidence, REFERENCE, SUPPORTED


SPEC = rag_agent.PrismQuery(intent='formula', entities=('X',), target_sections=(),
    required_sections=(), excluded_chunk_types=(), answer_mode='formulas_only')


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', Mock(side_effect=AssertionError('No network')))
    monkeypatch.setattr(rag_agent, 'generate_extractive_value_answer', lambda *a, **kw: '')
    monkeypatch.setattr(rag_agent, 'generate_image_transcription_answer', lambda *a, **kw: '')


@pytest.mark.parametrize('native', ['X=A+B', 'X=A*(B *, +,- K)'])
def test_coverage_formula_request_reaches_the_writer_without_the_legacy_template(monkeypatch, native):
    legacy = Mock(return_value='Unverified legacy shortcut.')
    monkeypatch.setattr(rag_agent, 'generate_formula_only_answer', legacy)
    context = evidence(native); original = deepcopy(context)
    agent = make_agent('[[FORMULA:F1]]' + REFERENCE + '\n\n' + SUPPORTED + REFERENCE)
    answer = agent._generate_grounded_answer('Формула X какая?', context, [], spec=SPEC,
        selection_mode='coverage', cache_enabled=False)
    assert agent.llm.complete.call_count >= 1
    assert legacy.call_count == 0
    assert context == original
    assert 'Unverified legacy shortcut.' not in answer
    assert 'ИСХОДНЫЕ СВЕДЕНИЯ' in agent.llm.complete.call_args_list[0].args[0]
    assert agent._generation_state.retrieval_trace['publication_verification']['status'] != 'source_extractive'


def test_legacy_formula_route_remains_byte_identical_without_new_model_calls(monkeypatch):
    legacy = Mock(return_value='Existing deterministic response.')
    monkeypatch.setattr(rag_agent, 'generate_formula_only_answer', legacy)
    agent = make_agent('Must not be called')
    result = agent._generate_grounded_answer('Формула X какая?', evidence('X=A+B'), [],
        spec=SPEC, selection_mode='legacy')
    assert result == 'Existing deterministic response.'
    assert legacy.call_count == 1 and agent.llm.complete.call_count == 0


def test_formula_route_keeps_rejecting_a_changed_equation_and_never_caches_it():
    agent = make_agent(r'$$X=A-B$$' + REFERENCE)
    answer = agent._generate_grounded_answer('Формула X какая?', evidence('X=A+B'), [],
        spec=SPEC, selection_mode='coverage', cache_enabled=True)
    assert r'$$X=A-B$$' not in answer
    assert not agent.response_cache.set.called
    assert agent._generation_state.retrieval_trace['publication_verification']['status'] == 'partial'


def test_formula_request_does_not_bypass_whole_input_budget():
    agent = make_agent('Must not be called'); agent.llm.metadata.context_window = 2048
    agent._generate_grounded_answer('Формула X какая?', evidence('X=A+B'), [], spec=SPEC,
        system_prompt='Все существенные условия сохраняются. ' * 1800,
        selection_mode='coverage', cache_enabled=True)
    assert agent.llm.complete.call_count == 0 and not agent.response_cache.set.called
    assert agent._generation_state.retrieval_trace['publication_verification']['reason_codes'] == ['input_budget_exceeded']
