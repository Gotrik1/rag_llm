"""Actual planning/coverage/writer prompts keep user authority without new calls."""
from copy import deepcopy
import json
import socket

import pytest

from corrective_rag import SearchPlan, assess_coverage, generation_prompt, make_plan, run_corrective
from task_intent import PLANNING_INTENT_INSTRUCTIONS, TASK_INTENT_INSTRUCTIONS
from user_task import UserTask


QUESTION = 'Проверь гипотезу «A заменяет B» и сравни условия, не смешивая 5 кВт и 5 МВт.'
RESOLVED = 'Предыдущий USER: разграничь A и B.\nТекущий USER: ' + QUESTION
REQUIREMENTS = ('Какие условия относятся к A?', 'Какие условия относятся к B?')
PLAN = SearchPlan(REQUIREMENTS, ('условия A', 'условия B'), canonical_question='Различия условий A и B')
FACT = 'A и B имеют разные условия.'
EVIDENCE = [{'evidence_id': 'E1', 'file': 'rules.md', 'section_path': 'Условия',
             'text': FACT, 'revision_id': 'r1', 'status': 'active'}]


@pytest.fixture(autouse=True)
def no_runtime_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Task-intent integration must not use models or network services')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def payload(prompt, prefix):
    return json.JSONDecoder().raw_decode(prompt[prompt.index(prefix):])[0]


def coverage_response(alignment='matched'):
    result = {'coverage': [{'requirement_id': f'R{number}', 'passage_ids': ['E1:P1'],
                            'applicable': True} for number in (1, 2)],
              'missing_queries': [], 'conflicts': []}
    if alignment is not None:
        result['task_alignment'] = {'status': alignment,
                                    'passage_ids': ['E1:P1'] if alignment == 'matched' else []}
    return result


def assert_contract(prompt, data):
    assert TASK_INTENT_INSTRUCTIONS in prompt
    assert prompt.count('"user_task":') == 1
    assert data['user_task'] == {'question': QUESTION, 'resolved_user_context': RESOLVED}
    assert data['task_intent_contract'] == {
        'acceptance_authority': 'user_task', 'premise_policy': 'investigate_not_confirm',
        'semantic_validation': 'not_performed'}
    assert 'search_hypotheses' not in data['task_intent_contract']
    assert 'user_task' not in data['task_intent_contract']
    assert 'requirements' not in data['task_intent_contract']


@pytest.mark.parametrize('question', [QUESTION, 'Когда A не применяется без B?',
                                    'Сравни 5 кВт и 5 МВт; не округляй.'])
def test_actual_make_plan_keeps_schema_and_exact_task_with_one_scripted_planning_call(question):
    calls = []
    def complete(prompt, purpose):
        calls.append((prompt, purpose))
        return json.dumps({'canonical_question': question, 'requirements': [question],
                           'queries': [question]}, ensure_ascii=False)
    plan = make_plan(question, complete, corpus_hints=[{'title': 'Native rules'}])
    assert len(calls) == 1 and calls[0][1] == 'planning'
    prompt = calls[0][0]
    assert PLANNING_INTENT_INSTRUCTIONS in prompt
    assert '"requirements":["что надо установить по источникам?"]' in prompt
    data = json.loads(prompt.split('ЗАПРОС И КОНТЕКСТ:\n', 1)[1])
    assert data == {'question': question, 'available_documents': [{'title': 'Native rules'}]}
    assert not plan.fallback and plan.requirements == (question,)
    assert plan.queries[0] == question


@pytest.mark.parametrize('alignment', [None, 'mismatch', 'uncertain', 'matched'])
def test_coverage_contract_preserves_top_fields_and_never_certifies_semantic_alignment(alignment):
    seen = []
    before = deepcopy(EVIDENCE)
    def complete(prompt, purpose):
        seen.append((prompt, purpose))
        return json.dumps(coverage_response(alignment))
    result = assess_coverage(PLAN, EVIDENCE, complete, user_task=UserTask(QUESTION, RESOLVED))
    assert len(seen) == 1 and seen[0][1] == 'evidence-review'
    data = payload(seen[0][0], '{"requirements":')
    assert_contract(seen[0][0], data)
    assert data['requirements'] == [{'id': 'R1', 'text': REQUIREMENTS[0]},
                                    {'id': 'R2', 'text': REQUIREMENTS[1]}]
    assert data['exclusions'] == [] and data['evidence'][0]['evidence_id'] == 'E1'
    assert result['covered_requirements'] == ['R1', 'R2']
    assert result['complete'] is (alignment == 'matched')
    assert EVIDENCE == before


def test_writer_contract_keeps_original_question_and_plan_text_once_without_inference():
    prompt = generation_prompt(QUESTION, PLAN, EVIDENCE, {'complete': False},
                               user_task=UserTask(QUESTION, RESOLVED))
    data = payload(prompt, '{"question":')
    assert_contract(prompt, data)
    assert data['question'] == QUESTION and data['canonical_question'] == PLAN.canonical_question
    assert data['requirements'] == list(REQUIREMENTS)
    assert data['exclusions'] == [] and data['coverage'] == {'complete': False}
    assert data['evidence'] == EVIDENCE


def test_none_user_task_preserves_legacy_prompt_shape_without_new_response_obligations():
    observed = []
    def complete(prompt, purpose):
        observed.append(prompt)
        return json.dumps(coverage_response(None))
    result = assess_coverage(PLAN, EVIDENCE, complete)
    writer = generation_prompt(QUESTION, PLAN, EVIDENCE, {'complete': False})
    for prompt, prefix in ((observed[0], '{"requirements":'), (writer, '{"question":')):
        data = payload(prompt, prefix)
        assert 'user_task' not in data and 'task_intent_contract' not in data
        assert TASK_INTENT_INSTRUCTIONS not in prompt
    assert result['complete'] and 'task_alignment' not in result


def test_actual_orchestration_adds_no_new_model_stage_or_semantic_fidelity_pass():
    calls = []
    def complete(prompt, purpose):
        calls.append((purpose, prompt))
        if purpose == 'planning':
            assert PLANNING_INTENT_INSTRUCTIONS in prompt
            return json.dumps({'canonical_question': PLAN.canonical_question,
                               'requirements': list(REQUIREMENTS), 'queries': ['условия A', 'условия B']})
        if purpose == 'answer':
            assert_contract(prompt, payload(prompt, '{"question":'))
            return FACT + ' [E1]'
        if prompt.startswith('Оцени полноту'):
            assert_contract(prompt, payload(prompt, '{"requirements":'))
            return json.dumps(coverage_response())
        assert prompt.startswith('Проверь доказательность ответа')
        data = payload(prompt, '{"question":')
        return json.dumps({'claims': [{'claim_id': row['claim_id'], 'support': 'supported',
            'passage_ids': ['E1:P1'], 'context_ids': row['context_ids'], 'task_relevance': 'matched'}
            for row in data['answer_claims']]})
    result = run_corrective(QUESTION, RESOLVED, retrieve=lambda *args, **kwargs: EVIDENCE,
                            complete=complete, max_additional_rounds=0)
    assert result.answer == FACT + ' [E1]'
    assert [purpose for purpose, _ in calls] == ['planning', 'evidence-review', 'answer', 'evidence-review']
    assert result.trace['coverage_complete']
    # Scripted model verdicts, not this payload metadata, supplied the judgments.
    assert result.trace['verification']['initial']['assessment_complete']
