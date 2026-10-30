"""Coverage JSON examples must teach shape, never pre-certify a user task.

All responses below are controlled strings, not native model calls.
"""
from copy import deepcopy
import json
import socket

import pytest

from corrective_rag import SearchPlan, assess_coverage
from evidence_anchors import build_evidence_anchors
from user_task import UserTask


QUESTION = 'Как проводят осмотр контрольного датчика?'
PLAN = SearchPlan(('Порядок осмотра',), (QUESTION,), canonical_question=QUESTION)
EVIDENCE = [
    {'evidence_id': 'E37', 'file': 'control.md', 'section_path': 'Основной порядок',
     'text': 'Осмотр контрольного датчика начинают после получения допуска.'},
    {'evidence_id': 'E91', 'file': 'archive.md', 'section_path': 'Архив',
     'text': 'Результат осмотра контрольного датчика фиксируют в журнале.'},
]
TASK = UserTask(QUESTION, QUESTION)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('Coverage schema tests must not access the network')
    monkeypatch.setattr(socket.socket, 'connect', fail)
    monkeypatch.setattr(socket, 'create_connection', fail)


def response(**extra):
    return {'coverage': [{'requirement_id': 'R1', 'passage_ids': ['E37:P1'],
                          'applicable': True}],
            'missing_queries': [], 'conflicts': [], **extra}


def invoke(data=None, *, user_task=TASK, evidence=None):
    prompts = []
    def complete(prompt, purpose):
        assert purpose == 'evidence-review'
        prompts.append(prompt)
        return json.dumps(data if data is not None else response(), ensure_ascii=False)
    values = deepcopy(EVIDENCE if evidence is None else evidence)
    before = deepcopy(values)
    result = assess_coverage(PLAN, values, complete, user_task=user_task)
    assert len(prompts) == 1 and values == before
    return result, prompts[0]


def syntax_example(prompt):
    prefix = 'Верни JSON:'
    start = prompt.index(prefix) + len(prefix)
    return json.JSONDecoder().raw_decode(prompt[start:].lstrip())[0]


def test_explicit_user_task_has_a_concrete_alignment_object_in_json_syntax_example():
    _, prompt = invoke()
    example = syntax_example(prompt)
    assert example['task_alignment'] == {'status': 'uncertain', 'passage_ids': []}
    assert 'Это только пример структуры' in prompt
    assert 'не предзаданный вердикт' in prompt
    assert 'matched/mismatch/uncertain' in prompt
    assert 'user_task' in prompt


def test_syntax_example_references_only_real_requirement_and_passage_ids():
    _, prompt = invoke()
    example = syntax_example(prompt)
    valid_passages = set(build_evidence_anchors(EVIDENCE))
    for row in example['coverage']:
        assert row['requirement_id'] in {'R1'}
        assert row['passage_ids'] and set(row['passage_ids']) <= valid_passages
    assert example['coverage'][0]['passage_ids'] == ['E37:P1']
    assert 'E1:P1' not in json.dumps(example)


def test_example_does_not_fabricate_a_passage_when_evidence_is_empty():
    _, prompt = invoke(evidence=[])
    example = syntax_example(prompt)
    assert example['coverage'] == []
    assert example['task_alignment'] == {'status': 'uncertain', 'passage_ids': []}


def test_legacy_no_user_task_keeps_its_old_optional_alignment_contract():
    result, prompt = invoke(user_task=None)
    assert result['complete'] is True
    assert 'task_alignment' not in result
    assert 'task_alignment' not in syntax_example(prompt)
    assert 'task_alignment' not in prompt


@pytest.mark.parametrize('malformed', [
    None, 'matched', [],
    [{'status': 'matched', 'passage_ids': ['E37:P1']}],
    {'rows': [{'status': 'matched', 'passage_ids': ['E37:P1']}]},
    {'status': {'value': 'matched'}, 'passage_ids': ['E37:P1']},
    {'status': 'matched', 'passage_ids': {'rows': ['E37:P1']}},
    {'status': 'matched', 'passage_ids': [{'passage_id': 'E37:P1'}]},
])
def test_wrong_alignment_shapes_are_unassessed_not_coerced(malformed):
    result, _ = invoke(response(task_alignment=malformed))
    assert result['task_alignment'] == {'status': 'unassessed', 'passage_ids': []}
    assert result['complete'] is False
    # The factual row and the missing intent judgment are separate outcomes.
    assert result['covered_requirements'] == ['R1']


@pytest.mark.parametrize('passages', [[], ['E999:P1'], ['E37:P999'], ['E37'],
                                     ['E37:P1', 'E37:P1'], ['E37:P1', 'E999:P1']])
def test_matched_alignment_still_requires_valid_existing_passage_ids(passages):
    result, _ = invoke(response(task_alignment={'status': 'matched', 'passage_ids': passages}))
    assert result['task_alignment']['status'] == 'unassessed'
    assert result['complete'] is False


def test_valid_matched_alignment_and_actual_coverage_remain_accepted():
    result, _ = invoke(response(task_alignment={'status': 'matched', 'passage_ids': ['E37:P1']}))
    assert result['task_alignment'] == {'status': 'matched', 'passage_ids': ['E37:P1']}
    assert result['complete'] is True


@pytest.mark.parametrize('status', ['mismatch', 'uncertain'])
def test_legitimate_negative_or_uncertain_alignment_may_have_no_passages(status):
    result, _ = invoke(response(task_alignment={'status': status, 'passage_ids': []}))
    assert result['task_alignment'] == {'status': status, 'passage_ids': []}
    assert result['complete'] is False
