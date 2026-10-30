"""A broad calculation question retains native operations before their legend."""
import pytest

from evidence_selection import _content_blocks, _query_groups, _scoped_cohorts


def chapter(category=6, *, condition='Учет по часам; применяется совместно с планом.'):
    return ('# Методика\n\n'
        f'Применение {category} ЦК: {condition}\n\n'
        f'Результат: [MATH: Y_sub({category}цк)=A_sub({category}цк)×B_sub({category}цк)]\n\n'
        'Где:\n\nОбозначения и источники данных находятся в следующей таблице.')


@pytest.mark.parametrize('category', range(1, 7))
@pytest.mark.parametrize('question', ['Как рассчитывается {n} ЦК?', 'Что такое {n}ЦК и как она рассчитывается?'])
def test_single_native_operation_with_its_whole_applicability_is_a_calculation_core(category, question):
    query = question.format(n=category)
    blocks = _content_blocks(chapter(category))
    cohorts = _scoped_cohorts(blocks, query, _query_groups(query))
    assert len(cohorts) == 1
    cohort = cohorts[0]
    assert cohort['bounded']
    assert cohort['scope_categories'] == [str(category)]
    assert [blocks[n] for n in cohort['core']] == blocks[1:3]


@pytest.mark.parametrize('quantity', ['цена', 'стоимость', 'сетевая мощность', 'покупная мощность'])
def test_an_explicit_quantity_does_not_become_an_unrestricted_scope_calculation(quantity):
    query = f'Как рассчитывается {quantity} по 6ЦК?'
    assert not _scoped_cohorts(_content_blocks(chapter()), query, _query_groups(query))


def test_another_category_cannot_supply_a_calculation_core():
    query = 'Как рассчитывается 6 ЦК?'
    assert not _scoped_cohorts(_content_blocks(chapter(5)), query, _query_groups(query))


def test_no_new_operation_is_inferred_without_an_applicability_clause():
    query = 'Как рассчитывается 6ЦК?'
    text = '# Справочник\n\n[MATH: Y_sub(6цк)=A_sub(6цк)×B_sub(6цк)]'
    assert not _scoped_cohorts(_content_blocks(text), query, _query_groups(query))


def test_nearest_native_heading_can_supply_scope_for_a_literal_unnumbered_application():
    query = 'Как рассчитывается 1 ЦК?'
    text = ('# Расчет по 1 ЦК\n\nПрименение: только при совместном учете условий.\n\n'
            'Формула расчёта стоимости:\n\n[MATH: Y=A×B]\n\nГде:\n\nОпределения.')
    blocks = _content_blocks(text)
    core = _scoped_cohorts(blocks, query, _query_groups(query))
    assert len(core) == 1
    assert [blocks[n] for n in core[0]['core']] == blocks[1:4]
    assert core[0]['scope_categories'] == ['1']


@pytest.mark.parametrize('boundary', ['## Параметры другого раздела', 'Где:'])
def test_scope_is_not_inherited_through_a_new_heading_or_a_legend(boundary):
    query = 'Как рассчитывается 1 ЦК?'
    text = '# Расчет по 1 ЦК\n\n' + boundary + '\n\nПрименение: все условия.\n\n[MATH: A=B+C]'
    assert not _scoped_cohorts(_content_blocks(text), query, _query_groups(query))


def test_a_literal_legend_constant_is_not_a_new_calculation_core():
    query = 'Как рассчитывается 6ЦК?'
    text = '# Расчет по 6 ЦК\n\nПрименение 6 ЦК: учет.\n\n[MATH: N=720]'
    assert not _scoped_cohorts(_content_blocks(text), query, _query_groups(query))


@pytest.mark.parametrize('fence', ['```', '~~~~'])
@pytest.mark.parametrize('gap', ['', '\n\n'])
def test_a_fenced_example_heading_never_grants_a_foreign_category_scope(fence, gap):
    query = 'Как рассчитывается 6ЦК?'
    text = ('# Расчет по 5 ЦК\n\n' + fence + 'text\n' + gap +
            '# Пример кода по 6 ЦК\n' + gap + fence + '\n\n'
            'Применение: совместный учет условий.\n\n[MATH: Y=A×B]')
    assert not _scoped_cohorts(_content_blocks(text), query, _query_groups(query))


@pytest.mark.parametrize('indent', ['', '   '])
def test_a_numbered_application_inside_an_unclosed_fence_is_literal_data(indent):
    query = 'Как рассчитывается 6ЦК?'
    text = '# Расчет по 6 ЦК\n\n' + indent + '```text\n\nПрименение 6ЦК: учет.\n\n[MATH: Y=A×B]'
    assert not _scoped_cohorts(_content_blocks(text), query, _query_groups(query))


def test_four_space_indented_code_does_not_supply_a_heading_scope():
    query = 'Как рассчитывается 6ЦК?'
    text = '# Расчет по 5 ЦК\n\n    # Пример по 6 ЦК\n\nПрименение: учет.\n\n[MATH: Y=A×B]'
    assert not _scoped_cohorts(_content_blocks(text), query, _query_groups(query))
