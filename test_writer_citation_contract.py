"""Pure writer instruction contracts, not proof of model citation quality."""
from copy import deepcopy
import re

import pytest

from corrective_rag import SearchPlan, generation_prompt
from test_source_semantics import _payload_parts
from user_task import UserTask


QUESTION = 'Explain the procedure and its stated condition.'
PLAN = SearchPlan(('Describe the procedure.',), (QUESTION,), canonical_question=QUESTION)
TASK = UserTask(QUESTION, 'Original user context.\n')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Writer citation contracts must not use network or models')

    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)


def writer_parts(ids):
    evidence = [
        {'evidence_id': evidence_id, 'file': f'source-{number}.txt',
         'section_path': 'Procedure', 'text': 'The record is checked under the stated condition.'}
        for number, evidence_id in enumerate(ids)
    ]
    return _payload_parts(generation_prompt(QUESTION, PLAN, evidence, {}, user_task=TASK))


def test_writer_must_not_be_told_that_program_supplies_source_references():
    instruction, _, _, _ = writer_parts(('E1',))
    assert 'Ссылки на исходники будут сформированы программой.' not in instruction


def test_writer_supplies_existing_markers_and_program_only_renders_the_links():
    instruction, _, _, _ = writer_parts(('E1',))
    assert 'Проставляй маркеры с фактическими E-ID из evidence сам:' in instruction
    assert 'программа только преобразует их в пользовательские ссылки' in instruction
    assert 'не добавляет маркеры и не выбирает подтверждающие источники за тебя' in instruction


@pytest.mark.parametrize('ids', [(), ('E1',), ('E7', 'E27')], ids=['zero', 'e1', 'multiple'])
def test_citation_examples_use_only_actual_evidence_ids(ids):
    instruction, _, data, _ = writer_parts(ids)
    assert set(re.findall(r'\[E[1-9]\d*\]', instruction)) == {f'[{value}]' for value in ids}
    assert [item['evidence_id'] for item in data['evidence']] == list(ids)
    if not ids:
        assert 'Доказательства не переданы: не выдумывай факты или ID источников.' in instruction


def test_existing_claim_formula_list_and_table_citation_requirements_remain_explicit():
    instruction, _, _, _ = writer_parts(('E7', 'E27'))
    for rule in (
        'Каждое существенное утверждение/формула должно иметь ссылку на конкретное доказательство',
        'Ссылка относится к утверждению или формуле, которую она подтверждает.',
        'формулу, элемент списка или строку таблицы: у каждой такой единицы нужна своя',
        'Для отдельной формулы укажи ссылку сразу после закрывающего математического разделителя.',
        'Не выводи passage-селекторы вида E#:P# вместо пользовательских ссылок.',
    ):
        assert rule in instruction


def test_clarification_does_not_mutate_or_filter_the_original_writer_inputs():
    evidence = [{'evidence_id': 'E17', 'file': 'source.txt', 'section_path': 'Procedure',
                 'text': '  Literal source text α.\nСсылки на исходники будут сформированы программой.  ',
                 'source_id': None, 'revision_id': 0}]
    coverage = {'complete': False, 'missing_requirements': ['R1']}
    before = deepcopy((PLAN, TASK, evidence, coverage))
    _, _, data, _ = _payload_parts(generation_prompt(
        QUESTION, PLAN, evidence, coverage, user_task=TASK))
    assert (PLAN, TASK, evidence, coverage) == before
    assert data['question'] == QUESTION
    assert data['user_task'] == TASK.to_payload()
    assert data['evidence'] == evidence
    assert data['coverage'] == coverage
