"""A named document topic can govern supported native descendants, not foreign facts."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex, _inherited_original_subject, _original_subject_scopes


THEMES = (
    ('North Harbor', 'North Crossing', 'обычной заявки', 'Обычная заявка',
     'Стоимость обычной заявки рассчитывается по формуле S = V * T. '
     'V — количество единиц, T — рублей за единицу. Надбавка отсутствует.'),
    ('Север', 'Восток', 'обычного отправления', 'Обычное отправление',
     'Стоимость обычного отправления рассчитывается по формуле S = M * P. '
     'M — масса в килограммах, P — рублей за килограмм. Надбавка отсутствует.'),
)


def corpus(theme, *, ancestor=False):
    name, foreign, subject, _, fact = theme
    parent = f'Руководство > Проект {name}' if ancestor else ''
    path = f'{parent} > 1. Правила расчета' if parent else '1. Правила расчета'
    common = {'file': 'opaque.md', 'file_name': 'opaque.md', 'file_path': '/owned/opaque.md',
              'source_id': 'source-A', 'revision_id': 'revision-A', 'status': 'active'}
    rows = [
        {**common, 'section_path': parent, 'section_title': parent.rsplit(' > ', 1)[-1],
         'text': f'Расчет стоимости {subject}: проект {name}\n\nКонтрольный материал для проверки.', 'score': 1.0},
        {**common, 'section_path': path, 'section_title': '1. Правила расчета', 'text': fact, 'score': 1.3},
        {**common, 'section_path': '2. Данные проекта', 'section_title': '2. Данные проекта',
         'text': f'Код проекта {name} — CHECK-47. Срок обработки — 19 минут.', 'score': 1.5},
    ]
    return rows, f'Как рассчитывается стоимость {subject} в проекте {name}? Укажи формулу и условия.'


def select(rows, question, **kwargs):
    nodes = [SimpleNamespace(text=row['text'], metadata=row) for row in rows]
    return SectionEvidenceIndex(nodes).select_v2(question, rows, **kwargs)


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('ancestor', (False, True))
@pytest.mark.parametrize('references', (False, True))
def test_named_root_or_verified_ancestor_governs_supported_rule(theme, ancestor, references):
    rows, question = corpus(theme, ancestor=ancestor)
    original = deepcopy(rows)
    result = select(rows, question, resolve_references=references)
    assert any(row['section_path'] == rows[1]['section_path'] and row['text'] == rows[1]['text'] for row in result)
    assert rows == original


@pytest.mark.parametrize('theme', THEMES)
def test_same_filename_without_physical_binding_cannot_supply_subject(theme):
    rows, question = corpus(theme)
    for row in rows:
        for key in ('file_path', 'source_id'):
            row.pop(key)
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('change', ('file_path', 'source_id', 'revision_id'))
def test_another_physical_source_or_revision_cannot_supply_subject(theme, change):
    rows, question = corpus(theme)
    rows[0][change] = 'different-binding-or-revision'
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_incidental_root_body_mention_is_not_governing_title(theme):
    rows, question = corpus(theme)
    rows[0]['text'] = f'Справочник иных операций\n\nДля сравнения упоминается проект {theme[0]}.'
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('lowercase', (False, True))
def test_explicit_foreign_named_descendant_does_not_inherit_document_subject(theme, lowercase):
    rows, question = corpus(theme)
    name = theme[1].lower() if lowercase else theme[1]
    rows[1]['section_title'] = f'Проект {name}'
    rows[1]['section_path'] = f'1. Проект {name}'
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_leaf_needs_its_own_positive_subject_support(theme):
    rows, question = corpus(theme)
    rows[1]['text'] = 'Стоимость настройки экрана задается пользователем. На экране показывается результат.'
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_hidden_root_does_not_supply_subject_to_another_source(theme):
    rows, question = corpus(theme)
    rows[0].update(file='hidden.md', file_name='hidden.md')
    result = select(rows, question, allowed_sources={'opaque.md'}, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_unrelated_named_sibling_does_not_govern_leaf_in_another_branch(theme):
    rows, question = corpus(theme, ancestor=True)
    rows[1]['section_path'] = 'Руководство > Чужая область > 1. Правила расчета'
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('predicate,fact', (('лимит', 'Лимит составляет 25 единиц.'),
                                         ('цена', 'Цена составляет 37 рублей за единицу.')))
@pytest.mark.parametrize('opening', ('Каков ', ''))
def test_short_precise_scalar_predicate_can_inherit_named_root(predicate, fact, opening):
    rows, _ = corpus(THEMES[1])
    rows[0]['text'] = 'Паспорт проекта Лира\n\nКонтрольный материал.'
    rows[1]['text'] = fact
    rows[2]['text'] = 'Код проекта Лира — CHECK-47.'
    result = select(rows, f'{opening}{predicate} проекта Лира?', resolve_references=False)
    assert any(row['text'] == fact for row in result)


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('opening', ('Для проекта', 'Проект'))
@pytest.mark.parametrize('lowercase', (False, True))
def test_explicit_foreign_entity_in_leaf_assertion_cannot_inherit(theme, opening, lowercase):
    rows, question = corpus(theme)
    name = theme[1].lower() if lowercase else theme[1]
    rows[1]['text'] = f'{opening} {name}: ' + rows[1]['text']
    result = select(rows, question, resolve_references=False)
    assert all(row['section_path'] != rows[1]['section_path'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_same_explicit_entity_value_is_case_invariant(theme):
    rows, question = corpus(theme)
    rows[1]['text'] = f'Для проекта {theme[0].lower()}: ' + rows[1]['text']
    result = select(rows, question, resolve_references=False)
    assert any(row['section_path'] == rows[1]['section_path'] and row['text'] == rows[1]['text'] for row in result)


@pytest.mark.parametrize('theme', THEMES)
def test_same_explicit_entity_heading_is_case_invariant(theme):
    rows, question = corpus(theme)
    rows[1]['section_path'] = rows[1]['section_title'] = f'Проект {theme[0].lower()}'
    result = select(rows, question, resolve_references=False)
    assert any(row['section_path'] == rows[1]['section_path'] and row['text'] == rows[1]['text'] for row in result)


@pytest.mark.parametrize('name,foreign', (('North Harbor', 'North Crossing'), ('Северный Порт', 'Северный Город')))
@pytest.mark.parametrize('location', ('root', 'ancestor', 'body', 'heading', 'path'))
@pytest.mark.parametrize('other', (False, True))
@pytest.mark.parametrize('lowercase', (False, True))
@pytest.mark.parametrize('binding', ('same', 'other-path', 'other-revision'))
def test_explicit_entity_slot_matrix_requires_full_identity_and_native_binding(name, foreign, location, other, lowercase, binding):
    """Exercise inheritance itself; an explicit own leaf need not inherit in select()."""
    rows, _ = corpus(THEMES[0])
    question = f'Как рассчитывается стоимость обычной заявки в проекте {name}?'
    declared = foreign if other else name
    declared = declared.lower() if lowercase else declared
    rows[0]['text'] = f'Паспорт проекта {name}\n\nКонтрольный материал.'
    rows[2]['text'] = f'Код проекта {name} — CHECK-47.'
    if location == 'root':
        rows[0]['text'] = f'Паспорт проекта {declared}\n\nКонтрольный материал.'
    elif location == 'ancestor':
        rows[0]['section_path'] = rows[0]['section_title'] = f'Руководство > Проект {declared}'
        rows[1]['section_path'] = rows[0]['section_path'] + ' > 1. Правила расчета'
    elif location == 'body':
        rows[1]['text'] = f'Для проекта {declared}: ' + rows[1]['text']
    elif location == 'heading':
        rows[1]['section_title'] = f'Проект {declared}'
    elif location == 'path':
        rows[1]['section_path'] = f'1. Проект {declared}'
    if binding == 'other-path':
        rows[0]['file_path'] = '/different/source.md'
    elif binding == 'other-revision':
        rows[0]['revision_id'] = 'revision-B'
    scope = _original_subject_scopes(question)[0]
    actual = _inherited_original_subject(rows[1], scope, rows)
    assert actual is (not other and binding == 'same')
