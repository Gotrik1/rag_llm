"""Typed search topics improve recall without becoming source/scope authority."""
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex, _focused_excerpt
from search_focus import SearchFocus, SearchOperation, SearchSubject


def node(path, text, file='printing.md', **metadata):
    return SimpleNamespace(text=text, metadata={
        'file_name': file, 'section_path': path,
        'section_title': path.rsplit(' > ', 1)[-1], 'status': 'active', **metadata,
    })


def hit(item):
    return {**item.metadata, 'file': item.metadata['file_name'], 'text': item.text, 'score': 1}


def focus(*subjects, operation=SearchOperation.LOOKUP):
    return SearchFocus(operation, tuple(SearchSubject(subject) for subject in subjects))


def test_answer_format_does_not_become_a_required_retrieval_subject():
    original = node('Печать > Дуплекс', 'Дуплекс печатает текст на обеих сторонах листа.')
    index = SectionEvidenceIndex([original])
    concise = 'Что такое дуплекс?'
    verbose = ('Пожалуйста, объясни дуплекс, сохранив точные названия и различия '
               'между ними, оформи таблицей с краткими выводами.')
    selected = [index.select_v2(question, [], search_focus=focus('дуплекс'),
                                constraint_question=question) for question in (concise, verbose)]
    assert [row['text'] for row in selected[0]] == [row['text'] for row in selected[1]]
    assert selected[1][0]['text'] == original.text


def test_independent_comparison_subjects_are_or_not_a_cross_document_and():
    laser = node('Лазерная печать', 'Лазерная печать использует сухой тонер.')
    inkjet = node('Струйная печать', 'Струйная печать наносит жидкие чернила.', 'inkjet.md')
    index = SectionEvidenceIndex([laser, inkjet])
    question = 'Сравни лазерную печать со струйной и укажи различия между ними.'
    search_focus = focus('лазерная печать', 'струйная печать', operation=SearchOperation.COMPARE)
    seeds = index.body_seeds(question, mode='coverage', search_focus=search_focus)
    selected = index.select_v2(question, [], search_focus=search_focus, top_k=2)
    assert {row['file'] for row in seeds} == {'printing.md', 'inkjet.md'}
    assert {row['file'] for row in selected} == {'printing.md', 'inkjet.md'}
    assert set().union(*(set(row['coverage_facets']) for row in selected)) >= {'subject:0', 'subject:1'}


def test_alias_is_a_soft_alternative_not_an_additional_required_word():
    item = node('Печать > Двусторонний режим', 'Двусторонний режим расходует меньше бумаги.')
    search_focus = SearchFocus(SearchOperation.LOOKUP, (
        SearchSubject('дуплекс', alternatives=('двусторонний режим',)),
    ))
    selected = SectionEvidenceIndex([item]).select_v2('Что такое дуплекс?', [], search_focus=search_focus)
    assert selected and selected[0]['text'] == item.text


def test_enumeration_does_not_require_interrogative_or_kind_words():
    item = node('Параметры печати', '[TABLE]\nРежим | Допустимые значения: Черновой; Стандартный; Фото\n'
                'Условие | Фото доступно только на фотобумаге\n[/TABLE]\n\n'
                'Изменение режима применяется к следующему заданию.')
    search_focus = focus('режим печати', operation=SearchOperation.ENUMERATE)
    selected = SectionEvidenceIndex([item]).select_v2(
        'Нужен полный каталог режимов печати с точными названиями.', [], search_focus=search_focus,
    )
    assert len(selected) == 1
    assert selected[0]['text'] == item.text
    assert selected[0]['selection_signals']['inventory_enumeration_reserved']


def test_enumeration_cannot_reserve_an_unrelated_nearby_table():
    unrelated = node('Печать', '[TABLE]\nЦвет корпуса | Выбор из списка: Белый; Черный\n[/TABLE]')
    selected = SectionEvidenceIndex([unrelated]).select_v2(
        'Каталог режимов печати', [hit(unrelated)],
        search_focus=focus('режим печати', operation=SearchOperation.ENUMERATE),
    )
    assert all(not row['selection_signals'].get('inventory_enumeration_reserved') for row in selected)


@pytest.mark.parametrize('search_focus', [focus('передача'), focus('стоимость для 3 ЦК')])
def test_user_category_cannot_be_changed_or_removed_by_model_focus(search_focus):
    wrong = node('3 ЦК', 'Для третьей ценовой категории стоимость зависит от тарифа.')
    right = node('6 ЦК', 'Для шестой ценовой категории стоимость зависит от плана.')
    index = SectionEvidenceIndex([wrong, right])
    selected = index.select_v2('Стоимость для 3 ЦК', [hit(wrong), hit(right)],
                               search_focus=search_focus, constraint_question='Как рассчитывается 6 ЦК?')
    assert selected and {row['section_title'] for row in selected} == {'6 ЦК'}
    seeds = index.body_seeds('Стоимость для 3 ЦК', mode='coverage', search_focus=search_focus,
                             constraint_question='Как рассчитывается 6 ЦК?')
    assert all(row['section_title'] != '3 ЦК' for row in seeds)


def test_model_canonical_source_kind_cannot_override_original_request():
    instruction = node('Дуплекс', 'Дуплекс поддерживает двустороннюю печать.', 'Руководство.md')
    regulation = node('Дуплекс', 'Дуплекс в нормативном документе.', 'Постановление.md')
    selected = SectionEvidenceIndex([instruction, regulation]).select_v2(
        'Ответь только по постановлениям о дуплексе.', [hit(instruction), hit(regulation)],
        search_focus=focus('дуплекс'), constraint_question='Объясни дуплекс.',
        allowed_sources={'руководство.md'},
    )
    assert selected and {row['file'] for row in selected} == {'Руководство.md'}


def test_original_negative_topic_survives_a_bad_focus_and_model_canonical():
    desired = node('Договор > Покупная мощность', 'Покупная мощность определяется по часам пика.')
    excluded = node('Договор > Сетевая мощность', 'Сетевая мощность определяется по нагрузке.')
    selected = SectionEvidenceIndex([desired, excluded]).select_v2(
        'Покупная и сетевая мощность', [hit(excluded), hit(desired)],
        search_focus=focus('сетевая мощность', 'покупная мощность'),
        constraint_question='Покупная мощность; не учитывай сетевую мощность.',
    )
    assert selected and {row['section_title'] for row in selected} == {'Покупная мощность'}


def test_formatting_overlap_cannot_admit_an_excluded_child_heading():
    desired = node('Покупная мощность', 'Покупная мощность определяется по часам пика.')
    excluded = node('Сетевая мощность', 'Сетевая мощность: точные названия оформлены в таблице.')
    selected = SectionEvidenceIndex([desired, excluded]).select_v2(
        'Покупная и сетевая мощность', [hit(excluded), hit(desired)],
        search_focus=focus('покупная мощность', 'сетевая мощность'),
        constraint_question=('Покупная мощность; сохрани точные названия и оформи таблицей. '
                             'Не учитывай сетевую мощность.'),
    )
    assert selected and {row['section_title'] for row in selected} == {'Покупная мощность'}


def test_focus_never_widens_acl_deleted_or_excluded_source_scope():
    public = node('Дуплекс', 'Дуплекс печатает с двух сторон.', 'public.md')
    private = node('Дуплекс', 'Приватный дуплекс другой модели.', 'private.md')
    deleted = node('Дуплекс', 'Удаленный дуплекс.', 'deleted.md', status='deleted')
    selected = SectionEvidenceIndex([public, private, deleted]).select_v2(
        'Расскажи про дуплекс', [hit(private), hit(deleted)], search_focus=focus('дуплекс'),
        allowed_sources={'public.md', 'deleted.md'}, excluded_sources={'deleted.md'},
    )
    assert selected and {row['file'] for row in selected} == {'public.md'}


def test_semantic_hit_is_not_vetoed_by_inaccurate_generated_subject():
    item = node('Экономия бумаги', 'Встроенный переворот листа сокращает расход бумаги.')
    selected = SectionEvidenceIndex([item]).select_v2(
        'Что такое дуплекс?', [hit(item)], search_focus=focus('двусторонняя печать'),
    )
    assert selected and selected[0]['text'] == item.text


def test_excerpt_can_reach_either_independent_subject():
    text = 'Преамбула не о печати.\n\n' * 30 + 'Струйная печать наносит жидкие чернила.'
    from evidence_selection import _query_groups
    profiles = tuple(_query_groups(value) for value in ('лазерная печать', 'струйная печать'))
    excerpt, _ = _focused_excerpt(text, 250, profiles[0], {}, question='Сравни способы печати',
                                   subject_profiles=profiles)
    assert 'жидкие чернила' in excerpt


def test_formula_focus_preserves_price_versus_quantity():
    from evidence_selection import _query_groups
    text = ('Количество картриджей.\n\n$$N=A/B$$\n\n'
            'Цена картриджа.\n\n$$P=C/D$$\n\n'
            '[TABLE]\n$N$ | Количество картриджей\n$P$ | Цена картриджа\n[/TABLE]')
    profiles = (_query_groups('цена картриджа', subject_phrase=True),)
    diagnostics = {}
    excerpt, _ = _focused_excerpt(text, 90, profiles[0], {}, question='Цена картриджа, оформи точно.',
                                   subject_profiles=profiles, formula_subjects=('цена картриджа',),
                                   diagnostics=diagnostics)
    assert 'P=C/D' in excerpt
    assert diagnostics['formula_dependency_blocks'] == [3]


def test_independent_subject_is_reserved_before_complete_sibling_family():
    parent = node('Печать', 'Печать включает настройки нескольких режимов.')
    children = [node(f'Печать > Режим {number}', f'Правило печати {number}. ' + 'Содержательная строка. ' * 10)
                for number in range(3)]
    binding = node('Переплет', 'Переплет соединяет готовые страницы.', 'binding.md')
    index = SectionEvidenceIndex([parent, *children, binding])
    selected = index.select_v2('Какие правила печати и переплета?', [], top_k=2,
                               max_chars=sum(len(item.text) for item in children),
                               search_focus=focus('печать', 'переплет', operation=SearchOperation.COMPARE))
    assert any(row['file'] == 'binding.md' for row in selected)
    assert any(row['file'] == 'printing.md' for row in selected)
    assert sum(len(row['text']) for row in selected) <= sum(len(item.text) for item in children)


def test_original_named_entity_outranks_model_generic_subject():
    wrong = node('Резервуар Рубин', 'Резервуар Рубин вмещает 180 литров.', 'ruby.md')
    right = node('Резервуар Лазурит', 'Резервуар Лазурит вмещает 74 литра.', 'lazurite.md')
    selected = SectionEvidenceIndex([wrong, right]).select_v2(
        'Вместимость резервуара', [hit(wrong), hit(right)], search_focus=focus('резервуар'),
        constraint_question='Какова вместимость резервуара Лазурит?', top_k=1,
    )
    assert selected and selected[0]['file'] == 'lazurite.md'


def test_no_focus_retains_legacy_selection_snapshot():
    items = [node('Дуплекс', 'Дуплекс печатает с двух сторон.'),
             node('Печать > Особые случаи', 'Только для теста дуплекс недоступен.')]
    index = SectionEvidenceIndex(items)
    implicit = index.select_v2('Объясни дуплекс.', [hit(items[1])])
    explicit = index.select_v2('Объясни дуплекс.', [hit(items[1])], search_focus=None)
    assert implicit == explicit
