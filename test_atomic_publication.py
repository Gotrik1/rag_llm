"""Synthetic public-API regressions for atomic safe-partial publication."""
from copy import deepcopy

import pytest

from grounding_verifier import (
    GroundingCheck, GroundingReport, extract_answer_claims, safe_partial_answer,
    task_publication_candidate, verify_grounding,
)
from user_task import UserTask


TASK = UserTask('Опиши порядок проверки.', 'Опиши порядок проверки.')
SOURCES = [{'evidence_id': 'E1', 'file': 'inspection.md',
            'text': 'Каталог доступен. Расчет: X=A+B. Показатели проверяются независимо. $$X=A+B$$'}]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Atomic publication tests must not call a model/network')
    monkeypatch.setattr('requests.sessions.Session.request', blocked)


def checked(answer, reject=(), *, task=TASK):
    rows = []
    for claim in extract_answer_claims(answer):
        bad = any(value in claim['text'] for value in reject)
        rows.append({'claim_id': claim['claim_id'], 'context_ids': claim['context_ids'],
            'support': 'unsupported' if bad else 'supported',
            'passage_ids': [] if bad else ['E1:P1'], 'task_relevance': 'mismatch' if bad else 'matched'})
    return verify_grounding(answer, SOURCES, claims=rows, user_task=task)


@pytest.mark.parametrize('task', [TASK, None], ids=['explicit-task', 'legacy'])
def test_rejected_formula_withholds_its_colon_lead_in_but_not_independent_prose(task):
    lead = 'Расчет задается выражением [E1]:'
    answer = lead + '\n\n$$X=A+B+C$$ [E1]\n\nКаталог доступен [E1].'
    report = checked(answer, ('X=A+B+C',), task=task)
    result = safe_partial_answer(answer, report, user_task=task)
    assert 'X=A+B+C' not in result
    assert lead not in result
    assert 'Каталог доступен [E1].' in result


def test_table_row_never_survives_without_its_original_header_and_other_rows():
    answer = ('Показатель | Значение\n--- | ---\n'
              'Объем | Не подтвержден [E1].\nТариф | Подтвержден [E1].\n\nКаталог доступен [E1].')
    result = safe_partial_answer(answer, checked(answer, ('Не подтвержден',)), user_task=TASK)
    assert 'Объем |' not in result and 'Тариф |' not in result
    assert 'Каталог доступен [E1].' in result


def test_supported_table_is_rendered_as_the_whole_original_markdown_block():
    table = ('Показатель | Значение\n--- | ---\n'
             'Объем | Подтвержден [E1].\nТариф | Подтвержден [E1].')
    result = task_publication_candidate(table, checked(table), user_task=TASK)
    assert result.answer == table
    assert result.verified_claim_count == len(extract_answer_claims(table))


def test_rejected_half_of_one_paragraph_cannot_change_a_heading_to_another_subject():
    answer = ('## Объем\n\nОбъем не подтвержден [E1]. Тариф подтвержден [E1].\n\n'
              '## Каталог\n\nКаталог доступен [E1].')
    result = safe_partial_answer(answer, checked(answer, ('Объем не подтвержден',)), user_task=TASK)
    assert '## Объем' not in result and 'Тариф подтвержден' not in result
    assert '## Каталог\n\nКаталог доступен [E1].' in result


def test_independent_supported_paragraph_under_same_heading_is_not_withheld():
    answer = '## Проверка\n\nНе подтвержденный порядок [E1].\n\nКаталог доступен [E1].'
    report = checked(answer, ('Не подтвержденный',))
    snapshot = deepcopy(report.to_dict())
    result = safe_partial_answer(answer, report, user_task=TASK)
    assert '## Проверка\n\nКаталог доступен [E1].' in result
    assert 'Не подтвержденный порядок' not in result
    assert report.to_dict() == snapshot


@pytest.mark.parametrize('task', [TASK, None], ids=['explicit-task', 'legacy'])
@pytest.mark.parametrize('continuation', ['Тариф подтвержден [E1].', '  Тариф подтвержден [E1].'])
def test_list_item_condition_and_lazy_or_indented_continuation_are_atomic(task, continuation):
    answer = '- Только при проверке [E1].\n' + continuation + '\n\nКаталог доступен [E1].'
    result = safe_partial_answer(answer, checked(answer, ('Только при проверке',), task=task), user_task=task)
    assert 'Только при проверке' not in result and 'Тариф подтвержден' not in result
    assert 'Каталог доступен [E1].' in result


def test_list_siblings_remain_independent_but_nested_item_and_continuation_do_not():
    answer = ('- Условие [E1]:\n  - Тариф подтвержден [E1].\n\n'
              '  Не подтвержденный порядок [E1].\n- Каталог доступен [E1].')
    result = safe_partial_answer(answer, checked(answer, ('Не подтвержденный',)), user_task=TASK)
    assert 'Условие' not in result and 'Тариф подтвержден' not in result
    assert '- Каталог доступен [E1].' in result


def test_supported_list_keeps_original_layout_and_order():
    answer = '- Каталог доступен [E1].\n- Тариф подтвержден [E1].'
    result = task_publication_candidate(answer, checked(answer), user_task=TASK)
    assert result.answer == answer and result.verified_claim_count == 2


@pytest.mark.parametrize('formula', ['$X=A+B+C$', r'\(X=A+B+C\)', '`X=A+B+C`', 'X=A+B+C'])
@pytest.mark.parametrize('task', [TASK, None], ids=['explicit-task', 'legacy'])
def test_colon_lead_depends_on_detected_formula_not_only_display_delimiters(formula, task):
    lead = 'Расчет задается выражением [E1]:'
    answer = lead + '\n\n' + formula + ' [E1]\n\nКаталог доступен [E1].'
    result = safe_partial_answer(answer, checked(answer, ('X=A+B+C',), task=task), user_task=task)
    assert lead not in result and 'X=A+B+C' not in result
    assert 'Каталог доступен [E1].' in result


@pytest.mark.parametrize('task', [TASK, None], ids=['explicit-task', 'legacy'])
def test_formula_rejection_does_not_remove_independent_noncontinuing_prose(task):
    answer = 'Каталог доступен [E1].\n\n$$X=A+B+C$$ [E1]'
    result = safe_partial_answer(answer, checked(answer, ('X=A+B+C',), task=task), user_task=task)
    assert 'Каталог доступен [E1].' in result and 'X=A+B+C' not in result


def test_missing_inline_citation_is_not_borrowed_from_another_sentence_in_unit():
    answer = 'Каталог доступен. Тариф подтвержден [E1].\n\nПоказатели проверяются независимо [E1].'
    result = task_publication_candidate(answer, checked(answer), user_task=TASK)
    assert 'Каталог доступен' not in result.answer and 'Тариф подтвержден' not in result.answer
    assert 'Показатели проверяются независимо [E1].' in result.answer
    assert result.verified_claim_count == 1


def test_invalid_citation_withholds_whole_original_paragraph_not_neighbouring_block():
    answer = 'Каталог доступен [E999]. Тариф подтвержден [E1].\n\nПоказатели проверяются независимо [E1].'
    result = task_publication_candidate(answer, checked(answer), user_task=TASK)
    assert 'Каталог доступен' not in result.answer and 'Тариф подтвержден' not in result.answer
    assert result.verified_claim_count == 1


def test_claim_cap_does_not_upgrade_unassessed_sentence_in_same_atomic_paragraph():
    answer = ' '.join('Каталог доступен [E1].' for _ in range(100)) + ' Неоцененный хвост [E1].'
    assert len(extract_answer_claims(answer)) == 100
    result = task_publication_candidate(answer, checked(answer), user_task=TASK)
    assert result.verified_claim_count == 0
    assert 'Неоцененный хвост' not in result.answer and 'Каталог доступен' not in result.answer


def test_unassessed_second_sentence_cannot_survive_via_whole_paragraph_reconstruction():
    answer = 'Каталог доступен [E1]. Тариф подтвержден [E1].\n\nПоказатели проверяются независимо [E1].'
    report = checked(answer)
    report.checks = [item for item in report.checks if item.details.get('claim_id') != 'C2']
    result = task_publication_candidate(answer, report, user_task=TASK)
    assert 'Каталог доступен' not in result.answer and 'Тариф подтвержден' not in result.answer
    assert result.verified_claim_count == 1


@pytest.mark.parametrize('fence', ['```', '~~~'])
def test_fenced_code_is_one_unit_and_cannot_leak_tail_or_make_heading_context(fence):
    answer = fence + 'text\n# Только при проверке\n\nНе подтвержденный порядок [E1].\n' + fence + '\n\nКаталог доступен [E1].'
    result = safe_partial_answer(answer, checked(answer, ('Не подтвержденный',)), user_task=TASK)
    assert fence not in result and '# Только при проверке' not in result
    assert 'Каталог доступен [E1].' in result


@pytest.mark.parametrize('bad_span', [(-1, 4), (0, 10_000), (5, 3)])
def test_invalid_legacy_actionable_bounds_cannot_authorize_atomic_expansion(bad_span):
    answer = 'Каталог доступен [E1].'
    report = GroundingReport(checks=[GroundingCheck('claim', 'invalid span', 'unsupported', start=bad_span[0], end=bad_span[1])])
    assert safe_partial_answer(answer, report) == answer


@pytest.mark.parametrize('task', [TASK, None], ids=['explicit-task', 'legacy'])
def test_heading_with_no_surviving_body_is_not_left_dangling(task):
    answer = '## Проверка\n\nНе подтвержденный порядок [E1].\n\n## Каталог\n\nКаталог доступен [E1].'
    result = safe_partial_answer(answer, checked(answer, ('Не подтвержденный',), task=task), user_task=task)
    assert '## Проверка' not in result and 'Не подтвержденный' not in result
    assert '## Каталог\n\nКаталог доступен [E1].' in result


def test_legacy_unassessed_cap_tail_keeps_its_original_condition():
    answer = '## Только при проверке\n\n' + '\n\n'.join('Не подтвержденный порядок [E1].' for _ in range(100))
    answer += '\n\nНеоцененный хвост [E1].'
    report = checked(answer, ('Не подтвержденный',), task=None)
    result = safe_partial_answer(answer, report)
    assert 'Не подтвержденный' not in result
    assert '## Только при проверке\n\nНеоцененный хвост [E1].' in result
