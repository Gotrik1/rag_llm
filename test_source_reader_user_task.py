"""Reader snapshots bind to exact user intent, not just a model's plan."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from source_reader import (MAX_SOURCE_INPUT_CHARS, build_source_reading_prompt,
                           read_sources, validated_writer_payload)
from user_task import UserTask


QUESTION = 'А от чего это зависит?'
PLAN = {'canonical_question': 'Модель выбрала другой предмет.',
        'requirements': ['Условия применения'], 'exclusions': []}
EVIDENCE = [{'evidence_id': 'E1', 'file': 'Проверка.md', 'section_path': 'Условия',
             'text': 'Проверку выполняют перед запуском.'}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('User-task reader tests must not use network services')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def data(prompt):
    return json.loads(prompt.split('ДАННЫЕ ЧТЕНИЯ:\n', 1)[1])


class Reader:
    def __init__(self, change=None):
        self.calls = []
        self.change = change

    def __call__(self, prompt, purpose):
        self.calls.append((prompt, purpose))
        result = {'snapshot_id': data(prompt)['snapshot_id'], 'cards': [{
            'id': 'C1', 'kind': 'rule', 'passage_ids': ['E1:P1'],
            'related_card_ids': [], 'requirement_ids': ['R1']}],
            'unresolved_requirement_ids': []}
        if self.change is not None:
            result = self.change(result)
        return json.dumps(result, ensure_ascii=False)


def task(context='Предыдущий вопрос пользователя: условия проверки датчика.'):
    return UserTask(QUESTION, context)


def test_reader_prompt_keeps_exact_user_task_separate_from_model_plan():
    raw = '  А от чего это зависит?\r\n '
    resolved = 'Тема пользователя: «Условия проверки»\r\nУточнение:  только датчик А.  '
    user_task = UserTask(raw, resolved)
    model_plan = {**PLAN, 'user_task': {'question': 'Подмена', 'resolved_user_context': 'Подмена'}}
    before = deepcopy((model_plan, EVIDENCE))

    prompt = build_source_reading_prompt(raw, model_plan, EVIDENCE, user_task=user_task)
    payload = data(prompt)

    assert payload['user_task'] == {'question': raw, 'resolved_user_context': resolved}
    assert payload['question'] == raw
    assert payload['plan']['canonical_question'] == PLAN['canonical_question']
    assert 'user_task' not in payload['plan']
    assert 'не заменяет и не изменяет эту задачу' in prompt
    assert 'Факты по-прежнему бери только из evidence' in prompt
    assert (model_plan, EVIDENCE) == before


def test_antecedent_change_invalidates_snapshot_even_if_raw_question_plan_and_evidence_are_identical():
    first = data(build_source_reading_prompt(QUESTION, PLAN, EVIDENCE, user_task=task('Условия проверки датчика.')))
    second = data(build_source_reading_prompt(QUESTION, PLAN, EVIDENCE, user_task=task('Условия поверки весов.')))
    assert first['question'] == second['question']
    assert first['plan'] == second['plan']
    assert first['evidence'] == second['evidence']
    assert first['snapshot_id'] != second['snapshot_id']


def test_snapshot_hash_includes_exact_user_payload_without_normalizing_it():
    payload = data(build_source_reading_prompt(QUESTION, PLAN, EVIDENCE,
                                             user_task=task(' Контекст\r\nс уточнением. ')))
    snapshot_id = payload.pop('snapshot_id')
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    assert snapshot_id == 'sha256:' + hashlib.sha256(serialized.encode()).hexdigest()


def test_none_preserves_legacy_prompt_payload_hash_and_rebinding_api():
    implicit = build_source_reading_prompt(QUESTION, PLAN, EVIDENCE)
    explicit = build_source_reading_prompt(QUESTION, PLAN, EVIDENCE, user_task=None)
    assert implicit == explicit
    assert 'user_task' not in data(implicit)
    reading = read_sources(QUESTION, PLAN, EVIDENCE, Reader())
    assert 'user_task' not in reading.to_dict()
    assert validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE)
    assert validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE, user_task=None)


def test_one_reader_call_resolves_only_original_quotes_and_carries_trusted_user_task():
    reader = Reader()
    user_task = task()
    reading = read_sources(QUESTION, PLAN, EVIDENCE, reader, user_task=user_task)
    audit = reading.to_dict()
    assert reading.assessed
    assert len(reader.calls) == 1 and reader.calls[0][1] == 'source-reading'
    assert audit['user_task'] == user_task.to_payload()
    assert audit['snapshot_id'] == data(reader.calls[0][0])['snapshot_id']
    assert audit['quoted_passages']['E1:P1']['quote'] == EVIDENCE[0]['text']
    assert audit['coverage_certified'] is False
    assert audit['assessment_kind'] == 'source-selection-not-entailment'
    audit['user_task']['resolved_user_context'] = 'Изменено в копии'
    assert reading.to_dict()['user_task'] == user_task.to_payload()


def test_writer_rebinding_rejects_stale_or_omitted_user_task_but_preserves_compact_guide():
    first = task('Условия проверки датчика.')
    second = task('Условия поверки весов.')
    reading = read_sources(QUESTION, PLAN, EVIDENCE, Reader(), user_task=first)
    guide = validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE, user_task=first)
    assert guide and guide['snapshot_id'] == reading.snapshot_id
    assert 'user_task' not in guide  # The main writer prompt owns exact task inputs.
    assert guide['quoted_passages']['E1:P1']['quote'] == EVIDENCE[0]['text']
    assert validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE, user_task=second) is None
    assert validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE) is None
    old = read_sources(QUESTION, PLAN, EVIDENCE, Reader())
    assert validated_writer_payload(old, QUESTION, PLAN, EVIDENCE, user_task=first) is None


@pytest.mark.parametrize('field', ['question', 'resolved_user_context'])
def test_caller_supplied_reading_cannot_forge_user_task_fields(field):
    user_task = task()
    reading = read_sources(QUESTION, PLAN, EVIDENCE, Reader(), user_task=user_task).to_dict()
    reading['user_task'][field] = 'Подменённое условие'
    assert validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE, user_task=user_task) is None


def test_model_response_cannot_replace_explicit_user_task_or_extend_its_schema():
    reader = Reader(lambda result: {**result, 'user_task': {'question': 'Подмена'}})
    reading = read_sources(QUESTION, PLAN, EVIDENCE, reader, user_task=task())
    assert not reading.assessed
    assert len(reader.calls) == 1
    assert reading.to_dict()['quoted_passages'] == {}
    assert reading.to_dict()['user_task'] == task().to_payload()


@pytest.mark.parametrize('invalid', [
    {'question': QUESTION, 'resolved_user_context': 'Не типизированная задача'},
    UserTask('Другой исходный вопрос', 'Контекст'),
])
def test_invalid_or_conflicting_user_task_fails_before_reader_call(invalid):
    reader = Reader()
    with pytest.raises(ValueError, match='invalid-user-task'):
        build_source_reading_prompt(QUESTION, PLAN, EVIDENCE, user_task=invalid)
    reading = read_sources(QUESTION, PLAN, EVIDENCE, reader, user_task=invalid)
    assert not reading.assessed
    assert reader.calls == []
    assert reading.warnings == ('invalid-user-task',)


def test_user_task_budget_is_atomic_and_does_not_truncate_context_or_add_a_call():
    user_task = task('x' * MAX_SOURCE_INPUT_CHARS)
    reader = Reader()
    frozen = deepcopy(EVIDENCE)
    reading = read_sources(QUESTION, PLAN, EVIDENCE, reader, user_task=user_task)
    assert not reading.assessed
    assert reader.calls == []
    assert reading.warnings == ('source-input-budget-exceeded',)
    assert reading.to_dict()['user_task'] == user_task.to_payload()
    assert reading.to_dict()['quoted_passages'] == {}
    assert EVIDENCE == frozen


def test_simultaneous_reader_calls_do_not_reuse_other_chats_antecedent():
    contexts = ('Условия проверки датчика А.', 'Условия поверки весов Б.')

    def run(context):
        user_task = task(context)
        reader = Reader()
        reading = read_sources(QUESTION, PLAN, EVIDENCE, reader, user_task=user_task)
        return reading, data(reader.calls[0][0]), user_task

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run, contexts))
    assert results[0][0].snapshot_id != results[1][0].snapshot_id
    for reading, payload, user_task in results:
        assert reading.to_dict()['user_task'] == payload['user_task'] == user_task.to_payload()
        assert validated_writer_payload(reading, QUESTION, PLAN, EVIDENCE, user_task=user_task)
