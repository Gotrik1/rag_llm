"""Synthetic numeric-ordinal scope regressions; no corpus fixtures or models."""
from types import SimpleNamespace

import pytest

from evidence_selection import (
    SectionEvidenceIndex,
    _content_blocks,
    _focused_excerpt,
    _formula_dependencies,
    _query_groups,
    category_ids,
    term_keys,
)


NUMBERS = [('1', 'первой'), ('2', 'второй'), ('3', 'третьей'),
           ('4', 'четвертой'), ('5', 'пятой'), ('6', 'шестой')]
ENDINGS = [('й', 'ценовой категории'), ('я', 'ценовая категория'),
           ('ю', 'ценовую категорию'), ('ой', 'ценовой категории'),
           ('ая', 'ценовая категория'), ('ую', 'ценовую категорию')]
SEPARATORS = ['-', '\u2011', ' - ', '\u2011 ', ' ']


@pytest.mark.parametrize('number,written', NUMBERS)
@pytest.mark.parametrize('ending,noun', ENDINGS)
@pytest.mark.parametrize('separator', SEPARATORS)
def test_numeric_ordinal_and_written_scope_share_one_lexical_view(number, written, ending, noun, separator):
    numeric = f'{number}{separator}{ending} {noun}'
    canonical = f'{written} ценовой категории'
    assert category_ids(numeric) == frozenset({number})
    assert term_keys('Расчет в ' + numeric) == term_keys('Расчет в ' + canonical)
    assert _query_groups('Расчет в ' + numeric) == _query_groups('Расчет в ' + canonical)
    assert 'й' not in term_keys(numeric)


@pytest.mark.parametrize('number,written', NUMBERS)
def test_numeric_ordinal_abbreviation_and_ordinary_numeric_form_agree(number, written):
    forms = [f'{number}-й ЦК', f'{number}\u2011ю ЦК', f'{number} ЦК', f'{written} ЦК']
    assert all(category_ids(form) == frozenset({number}) for form in forms)
    assert all(_query_groups(form) == _query_groups(f'{written} ценовой категории') for form in forms)


@pytest.mark.parametrize('text', [
    '6-й пункт', '6\u2011й раздел', '6 кВ', '6кВ', '6-й год', '2026 год',
    '2026-й ценовой категории', '16-й ценовой категории', '66-я ценовая категория',
    '6-й категории', '6-й ценовой пункт', '6 ценовых категорий', '6 - 4 ЦК',
])
def test_unrelated_ordinals_units_years_and_counts_do_not_invent_category_scope(text):
    if text == '6 - 4 ЦК':
        # Existing explicit category ranges retain their existing semantics.
        assert category_ids(text) == category_ids('6-4 ЦК')
    else:
        assert category_ids(text) == frozenset()


def test_two_explicit_numeric_ordinals_preserve_both_categories():
    assert category_ids('Сравни 2-ю ценовую категорию и 6-ю ценовую категорию') == frozenset({'2', '6'})


def _competing_quantity_section():
    legend = '\n'.join([
        '[TABLE]', 'Обозначение | Описание | Ед. изм.',
        '[MATH: P] | Цена материала | условные единицы',
        '[MATH: R] | Цена материала для 4 и 6 ЦК | условные единицы',
        '[MATH: A] | Базовый параметр материала | условные единицы',
        '[MATH: B] | Поправка материала | условные единицы',
    ])
    return '\n\n'.join([
        legend,
        *(['Справочные сведения о хранении образцов. ' * 5] * 8),
        'Для целевого материала:',
        'Цена как сумма базового параметра и поправки:',
        '[MATH: P=A+B]',
        *(['Сведения о таре и перевозке. ' * 5] * 8),
        'Для смежного материала:',
        '[MATH: R=A-B]',
    ])


@pytest.mark.parametrize('scope', ['6-й ценовой категории', '6\u2011ой ценовой категории',
                                  '6 ЦК', 'шестой ценовой категории'])
def test_quantity_dependency_does_not_reward_category_boilerplate_as_the_subject(scope):
    text = _competing_quantity_section()
    question = f'Как определяется цена целевого материала в {scope}?'
    groups = _query_groups(question)
    packages, _ = _formula_dependencies(_content_blocks(text), question, groups)
    assert packages and packages[0]['symbol'] == 'p'
    assert all(package['symbol'] != 'r' for package in packages)
    signals = {}
    excerpt, _ = _focused_excerpt(text, 750, groups, {}, question=question, diagnostics=signals)
    assert '[MATH: P=A+B]' in excerpt
    assert signals['formula_dependency_blocks']
    assert len(excerpt) <= 750


def _node(scope, source='public.md'):
    title = f'Метод > {scope}'
    return SimpleNamespace(text=f'# {scope}\n\nЦена целевого материала определяется явно.',
                           metadata={'file_name': source, 'section_path': title,
                                     'section_title': scope, 'status': 'active'})


def _hit(node):
    return {**node.metadata, 'file': node.metadata['file_name'], 'text': node.text, 'score': 1}


def test_numeric_ordinal_scope_never_widens_source_visibility_or_admits_other_category():
    wrong, right, private = _node('3 ЦК'), _node('6 ЦК'), _node('6 ЦК', 'private.md')
    index = SectionEvidenceIndex([wrong, right, private])
    query = 'Цена целевого материала в 6-й ценовой категории'
    selected = index.select_v2(query, [_hit(wrong), _hit(private), _hit(right)],
                               allowed_sources={'public.md'}, max_chars=2000)
    assert selected
    assert all(item['file'] == 'public.md' and item['scope_categories'] == ['6'] for item in selected)
    assert index.select_v2(query, [_hit(right), _hit(private)], allowed_sources={'public.md'},
                           excluded_sources={'public.md'}) == []
