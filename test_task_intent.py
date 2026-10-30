"""Plan text is an unverified search hypothesis, not a user acceptance rule."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json

import pytest

from task_intent import PLANNING_INTENT_INSTRUCTIONS, TASK_INTENT_INSTRUCTIONS, TaskIntent
from user_task import UserTask


@pytest.mark.parametrize('question,hypothesis', [
    ('Проверь предположение «сигнал заменяет разрешение» и раздели два требования.',
     'Подтвердить замену разрешения сигналом'),
    ('Сравни остановку и блокировку: это разные операции или одна заменяет другую?',
     'Доказать, что остановка заменяет блокировку'),
    ('Когда прибор не запускается без допуска?', 'Прибор всегда запускается без допуска'),
    ('Сравни 5 кВт и 5 МВт; не округляй значения.', '5 кВт равны 5 МВт'),
    ('Is "backup implies retention" actually established, or are they distinct?',
     'Confirm that backup establishes retention'),
])
def test_adversarial_plan_premises_never_become_accepted_facts_or_acceptance_criteria(question, hypothesis):
    task = UserTask(question, question)
    payload = TaskIntent(task, canonical_question=hypothesis, requirements=(hypothesis,)).to_payload()
    assert payload['user_task'] == task.to_payload()
    assert payload['acceptance_authority'] == 'user_task'
    assert payload['premise_policy'] == 'investigate_not_confirm'
    assert payload['semantic_validation'] == 'not_performed'
    assert payload['search_hypotheses']['canonical_question'] == {
        'text': hypothesis, 'role': 'retrieval_only'}
    assert payload['search_hypotheses']['requirements'] == [{
        'id': 'R1', 'text': hypothesis, 'role': 'open_evidence_question',
        'truth_assumed': False, 'acceptance_criterion': False}]


def test_the_actual_user_question_not_the_model_canonical_text_remains_authority():
    raw = 'А это не одно и то же?'
    resolved = 'Предыдущий USER: сравни Люмор и Тевра.\nТекущий USER: ' + raw
    intent = TaskIntent(UserTask(raw, resolved), canonical_question='Тевра заменяет Люмор',
                        requirements=('Подтвердить замену',))
    payload = intent.to_payload()
    assert payload['user_task']['question'] == raw
    assert payload['user_task']['resolved_user_context'] == resolved
    assert payload['search_hypotheses']['canonical_question']['text'] == 'Тевра заменяет Люмор'
    # The helper does not pretend to detect or repair arbitrary semantic drift.
    assert payload['semantic_validation'] == 'not_performed'


@pytest.mark.parametrize('question', [
    'Не исключай исключения: когда X не применяется?',
    'Объясни X, но не описывай настройку Y.',
    'Почему «X не заменяет Y»? Проверь, а не подтверждай цитату.',
    '  P = 5 МВт; p = 5 мВт; X ≠ Y; A ≤ 1,5.\r\n',
    'На 01.02.2024, только при A и не при B; сравни версии 1 и 1.0.',
    'When does A not apply, and how does B differ?',
])
def test_comparison_negation_units_conditions_and_quoted_premises_remain_exact(question):
    payload = TaskIntent(UserTask(question, question)).to_payload()
    roundtrip = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    assert roundtrip['user_task'] == {'question': question, 'resolved_user_context': question}
    assert roundtrip['search_hypotheses']['requirements'] == []
    assert 'exclusions' not in roundtrip  # Factual negatives are not inferred output constraints.


def test_empty_or_long_user_context_is_not_rejected_rewritten_or_clipped():
    context = 'USER: X ≠ Y; не при B.\n' * 3000
    task = UserTask('', context)
    requirement = 'что установить? ' * 1000
    payload = TaskIntent(task, canonical_question='  поиск\r\n', requirements=(requirement,)).to_payload()
    assert payload['user_task'] == task.to_payload()
    assert payload['search_hypotheses']['canonical_question']['text'] == '  поиск\r\n'
    assert payload['search_hypotheses']['requirements'][0]['text'] == requirement


def test_immutable_request_and_detached_payload_cannot_promote_hypotheses_by_mutation():
    task = UserTask('Сравни A и B.', 'Сравни A и B.')
    intent = TaskIntent(task, requirements=('A заменяет B',))
    original = deepcopy(intent.to_payload())
    with pytest.raises(FrozenInstanceError):
        intent.user_task = UserTask('Утверди A.', 'Утверди A.')
    with pytest.raises(FrozenInstanceError):
        intent.requirements = ('Подтверди замену',)
    altered = intent.to_payload()
    altered['user_task']['question'] = 'Подтверди замену'
    altered['search_hypotheses']['requirements'][0]['truth_assumed'] = True
    altered['search_hypotheses']['requirements'].append({'id': 'R99', 'text': 'extra'})
    assert intent.to_payload() == original


def test_requirement_ids_and_duplicate_texts_keep_the_existing_planner_order():
    intent = TaskIntent(UserTask('Сравни A и B.', 'Сравни A и B.'),
                        requirements=('B', 'A', 'B'))
    rows = intent.to_payload()['search_hypotheses']['requirements']
    assert [(row['id'], row['text']) for row in rows] == [('R1', 'B'), ('R2', 'A'), ('R3', 'B')]
    assert all(row['truth_assumed'] is False and row['acceptance_criterion'] is False for row in rows)


@pytest.mark.parametrize('task', [None, True, 'question', {}, {'question': 'q'}])
def test_task_authority_requires_an_existing_typed_user_task(task):
    with pytest.raises(TypeError, match='UserTask'):
        TaskIntent(task)


@pytest.mark.parametrize('requirements', [None, True, 'requirement', [], ('q', True), (1,), ({},)])
def test_model_containers_are_not_coerced_or_silently_repaired(requirements):
    with pytest.raises(TypeError):
        TaskIntent(UserTask('q', 'q'), requirements=requirements)


@pytest.mark.parametrize('canonical', [None, True, 3, {}, []])
def test_canonical_hypothesis_requires_an_exact_string(canonical):
    with pytest.raises(TypeError):
        TaskIntent(UserTask('q', 'q'), canonical_question=canonical)


def test_prompt_contract_explicitly_separates_search_goals_from_user_acceptance():
    assert 'user_task' in TASK_INTENT_INSTRUCTIONS and 'search_hypotheses' in TASK_INTENT_INSTRUCTIONS
    assert 'не критерии приёмки' in TASK_INTENT_INSTRUCTIONS
    assert 'не превращай предположение' in TASK_INTENT_INSTRUCTIONS
    assert 'обе стороны' in TASK_INTENT_INSTRUCTIONS
    assert 'единицы' in TASK_INTENT_INSTRUCTIONS and 'отрицания' in TASK_INTENT_INSTRUCTIONS
    assert 'фактическое отрицание' in TASK_INTENT_INSTRUCTIONS
    assert 'нейтральные вопросы' in PLANNING_INTENT_INSTRUCTIONS
    assert 'доказать' in PLANNING_INTENT_INSTRUCTIONS and 'подтвердить' in PLANNING_INTENT_INSTRUCTIONS
