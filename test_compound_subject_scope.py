"""Original-question subjects constrain their own evidence, not unrelated requested facts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex


THEMES = (
    ('North Star', 'North Crossing', 'ALPHA', 'BETA',
     'Какой код использует проект North Star? Сколько часов занимает доставка ALPHA '
     'и сколько дней хранится пакет BETA?',
     'Проект North Star использует код ZX-17.',
     'Доставка ALPHA занимает ровно 43 часа.', 'Пакет BETA хранится ровно 68 дней.'),
    ('Silver Lake', 'Silver Coast', 'DELTA', 'SIGMA',
     'Какой номер присвоен программе Silver Lake? Каков срок подготовки карточки DELTA '
     'и срок рассмотрения формы SIGMA?',
     'Программе Silver Lake присвоен номер K-29.',
     'Подготовка карточки DELTA занимает ровно 43 часа.', 'Рассмотрение формы SIGMA занимает ровно 68 дней.'),
)


def fixture(theme, *, image=False, distractors=True):
    name, wrong, first, second, question, primary, fact_a, fact_b = theme
    rows = [
        {'file': 'primary.md', 'section_path': f'Паспорт {name}', 'text': primary, 'score': 1.37},
        {'file': 'first.png' if image else 'first.md', 'section_path': '', 'text': fact_a, 'score': .89},
        {'file': 'second.jpg' if image else 'second.md', 'section_path': '', 'text': fact_b, 'score': .89},
    ]
    if distractors:
        rows += [{'file': 'foreign.md', 'section_path': f'Паспорт {wrong}',
                  'text': primary.replace(name, wrong).replace('ZX-17', 'WRONG-91').replace('K-29', 'WRONG-91'),
                  'score': .95}]
    rows = [{**row, 'source_id': row['file'], 'revision_id': 'revision-A', 'content_hash': 'revision-A',
             'status': 'active'} for row in rows]
    nodes = [SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows]
    return SectionEvidenceIndex(nodes), rows


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('image', (False, True))
@pytest.mark.parametrize('lowercase', (False, True))
def test_independent_requested_facts_survive_primary_name_guard(theme, image, lowercase):
    index, rows = fixture(theme, image=image)
    original = deepcopy(rows)
    question = theme[4].lower() if lowercase else theme[4]
    result = index.select_v2(question, rows, top_k=10, max_chars=36000, resolve_references=False)
    assert {row['file'] for row in result} == {row['file'] for row in rows[:3]}
    assert {row['text'] for row in result} == {theme[5], theme[6], theme[7]}
    assert rows == original, 'Subject selection must not rewrite native evidence or caller metadata'


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('lowercase', (False, True))
def test_single_named_target_keeps_precision_and_multiword_identity(theme, lowercase):
    index, rows = fixture(theme)
    question = theme[4].split('?', 1)[0] + '?'
    question = question.lower() if lowercase else question
    result = index.select_v2(question, rows, resolve_references=False)
    assert [row['file'] for row in result] == ['primary.md']
    assert result[0]['text'] == theme[5]


@pytest.mark.parametrize('theme', THEMES)
def test_independent_clause_without_named_identifier_requires_its_body_support(theme):
    index, rows = fixture(theme, distractors=False)
    rows = [rows[0], {**rows[1], 'text': 'Подготовка заявки занимает ровно 12 часов.'},
            {**rows[2], 'text': 'Доставка обычной посылки занимает ровно 23 часа.'}]
    index = SectionEvidenceIndex([SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows])
    question = theme[4].split('?', 1)[0] + '? Сколько часов занимает подготовка заявки?'
    result = index.select_v2(question, rows, resolve_references=False)
    assert {row['file'] for row in result} == {'primary.md', 'first.md'}
    assert any(row['text'] == rows[1]['text'] for row in result)
    assert all(row['file'] != 'second.md' for row in result), 'Shared quantity/action nouns are not independent topic proof'


@pytest.mark.parametrize('theme', THEMES)
def test_clause_subject_reservations_preserve_acl(theme):
    index, rows = fixture(theme)
    result = index.select_v2(theme[4], rows, allowed_sources={'primary.md', 'first.md'},
                             resolve_references=False)
    assert {row['file'] for row in result} == {'primary.md', 'first.md'}
    assert all(row['file'] != 'second.md' for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_original_question_subjects_not_generated_facet_names(theme):
    index, rows = fixture(theme)
    question = theme[4].split('?', 1)[0] + '?'
    result = index.select_v2(question, rows, facets=[theme[2], theme[3], theme[1]], resolve_references=False)
    assert [row['file'] for row in result] == ['primary.md']


@pytest.mark.parametrize('theme', THEMES)
def test_question_coordination_preserves_distinct_requested_subjects(theme):
    index, rows = fixture(theme)
    question = theme[4].replace('? ', ', а также ', 1)
    result = index.select_v2(question, rows, resolve_references=False)
    assert {row['file'] for row in result} == {'primary.md', 'first.md', 'second.md'}


@pytest.mark.parametrize('theme', THEMES)
def test_original_clause_uses_existing_yo_and_morphological_matching(theme):
    _, rows = fixture(theme, distractors=False)
    rows = [rows[0], {**rows[1], 'text': 'Прием отправлений занимает ровно 12 часов.'}]
    index = SectionEvidenceIndex([SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows])
    question = theme[4].split('?', 1)[0] + '? Сколько часов занимает приём отправления?'
    result = index.select_v2(question, rows, resolve_references=False)
    assert {row['file'] for row in result} == {'primary.md', 'first.md'}
    assert result[-1]['text'] == rows[1]['text']


@pytest.mark.parametrize('theme', THEMES)
def test_subject_support_does_not_borrow_a_stale_revision(theme):
    _, rows = fixture(theme, distractors=False)
    rows[1]['section_path'] = 'Порядок обработки'
    current = {**rows[1], 'revision_id': 'revision-B', 'content_hash': 'revision-B',
               'text': rows[1]['text'].replace('43', '44')}
    native = [rows[0], current, rows[2]]
    index = SectionEvidenceIndex([SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in native])
    result = index.select_v2(theme[4], [rows[0], rows[1], current, rows[2]], resolve_references=False)
    matched = [row for row in result if row['file'] == 'first.md']
    assert matched and all(row['revision_id'] == 'revision-B' and row['text'] == current['text'] for row in matched)
    assert all(row['text'] != rows[1]['text'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_citation_instruction_does_not_become_an_independent_subject(theme):
    _, rows = fixture(theme, distractors=False)
    rows = [rows[0], {**rows[1], 'text': 'Источник каждого факта указан отдельно для справочной заметки.'}]
    index = SectionEvidenceIndex([SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows])
    question = theme[4].split('?', 1)[0] + '? Укажи источник каждого факта.'
    result = index.select_v2(question, rows, resolve_references=False)
    assert [row['file'] for row in result] == ['primary.md']


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('directive,presentation_fact', (
    ('Ответь в формате JSON.', 'Формат JSON используется для справочной выгрузки.'),
    ('Ответь в формате «короткий список».', 'Короткий список используется в справочной заметке.'),
))
def test_response_presentation_is_not_a_requested_evidence_subject(theme, directive, presentation_fact):
    _, rows = fixture(theme, distractors=False)
    rows = [rows[0], {**rows[1], 'file': 'format-only.md', 'text': presentation_fact}]
    index = SectionEvidenceIndex([SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows])
    question = theme[4].split('?', 1)[0] + '? ' + directive
    result = index.select_v2(question, rows, resolve_references=False)
    assert [row['file'] for row in result] == ['primary.md']


def test_genuine_question_about_json_format_still_keeps_its_evidence():
    theme = THEMES[0]
    _, rows = fixture(theme, distractors=False)
    rows = [rows[0], {**rows[1], 'file': 'format-rules.md',
                     'text': 'Формат JSON используется для справочной выгрузки.'}]
    index = SectionEvidenceIndex([SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows])
    question = theme[4].split('?', 1)[0] + '? Для какой выгрузки используется формат JSON?'
    result = index.select_v2(question, rows, resolve_references=False)
    assert {row['file'] for row in result} == {'primary.md', 'format-rules.md'}
    assert any(row['text'] == rows[1]['text'] for row in result)
