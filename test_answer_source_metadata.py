"""Source presentation must not assume every selected section has a ranking score."""
from types import SimpleNamespace
from unittest.mock import Mock
import json

import pytest

import rag_agent
from answer_sources import build_answer_sources


def test_unscored_canonical_section_can_be_returned_after_generation(monkeypatch):
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE', 'off')
    question = 'Какие виды моделей есть в системе?'
    context = [{'file': 'manual.md', 'section_path': 'Модели',
                'text': 'Система поддерживает расчетную модель.'}]
    agent = object.__new__(rag_agent.RAGAgent)
    agent._generation_state = SimpleNamespace(from_cache=False)
    agent.llm_model = 'handoff-test'
    agent._all_nodes = [object()]
    agent.capture_runtime = Mock(return_value=None)
    agent._refresh_docstore_if_changed = Mock()
    agent.understand_query = Mock(return_value=rag_agent.QueryUnderstanding(
        question, ('fact',), (question,)))
    agent.retrieve_context = Mock(return_value=context)
    agent._generate_grounded_answer = Mock(return_value='Поддерживается расчетная модель.')

    answer, sources, cached = agent.ask(question, selection_mode='coverage')

    assert answer == 'Поддерживается расчетная модель.'
    assert sources == [{'file': 'manual.md', 'section': 'Модели',
                        'score': None, 'text_preview': context[0]['text']}]
    assert cached is False
    assert 'score' not in context[0], 'Presentation must not mutate retrieval evidence'


def test_corrective_answer_also_keeps_unknown_source_rank(monkeypatch):
    from test_corrective_integration import (
        QUESTION, FACT, agent_with_fake_dependencies, evidence,
    )

    monkeypatch.setenv('RAG_PLANNER_THINKING', 'false')
    item = evidence()
    del item['score']
    agent = agent_with_fake_dependencies(items=[item])

    answer, sources, cached = agent.ask(QUESTION, pipeline_mode='on')

    assert FACT in answer
    assert sources[0]['score'] is None
    assert cached is False
    json.dumps(sources, allow_nan=False)


@pytest.mark.parametrize('score', [None, float('nan'), float('inf'), float('-inf'), True, 'invalid'])
def test_unknown_rank_metadata_is_json_null_not_a_fabricated_zero(score):
    original = {'file': 'native.md', 'text': 'Исходный текст', 'score': score}
    sources = build_answer_sources([original])
    assert sources[0]['score'] is None
    json.dumps(sources, allow_nan=False)
    assert original['score'] is score


def test_ranked_and_unranked_sections_keep_order_and_source_text():
    context = [{'file': 'first.md', 'text': 'a' * 130, 'score': 0.8, 'section_path': '1.2'},
               {'file': 'second.md', 'text': 'Определение'}]
    assert build_answer_sources(context) == [
        {'file': 'first.md', 'section': '1.2', 'score': 0.8, 'text_preview': 'a' * 120},
        {'file': 'second.md', 'section': '', 'score': None, 'text_preview': 'Определение'},
    ]


@pytest.mark.parametrize('field', ['file', 'text'])
def test_invalid_evidence_still_fails_instead_of_inventing_a_source(field):
    item = {'file': 'source.md', 'text': 'Original'}
    del item[field]
    with pytest.raises(KeyError, match=field):
        build_answer_sources([item])
