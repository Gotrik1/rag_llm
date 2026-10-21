"""Document-independent coverage, applicability and rollback regressions."""
from types import SimpleNamespace

import pytest

from evidence_selection import (SectionEvidenceIndex, category_ids, overview_sections, _focused_excerpt,
                                _query_groups, _positive_query, _content_blocks, _formula_dependencies,
                                _math_expressions)


def node(path, text, file='novel-manual.md', **metadata):
    return SimpleNamespace(text=text, metadata={
        'file_name': file, 'section_path': path,
        'section_title': path.rsplit(' > ', 1)[-1], 'status': 'active', **metadata,
    })


def hit(item, score=1):
    return {**item.metadata, 'file': item.metadata['file_name'], 'text': item.text, 'score': score}


def test_v2_keeps_substantive_ranked_evidence_when_another_heading_matches():
    superficial = node('Порядок возврата оборудования', 'Перечень используемых экранов.', 'screens.md')
    main = node('Условия договора > Пункт 8',
                'Для возврата оборудования оформляют акт приема-передачи. Срок возврата — пять дней.', 'contract.md')
    selected = SectionEvidenceIndex([superficial, main]).select_v2(
        'Каков порядок возврата оборудования?', [hit(main)], top_k=2,
    )
    assert any(item['file'] == 'contract.md' and 'пять дней' in item['text'] for item in selected)


@pytest.mark.parametrize('question', ['Что такое КРФ/КР?', 'Объясни подробно КРФ (КР), пожалуйста.', 'КРФ или КР', 'что такое крф/кр?'])
def test_explicit_alias_spellings_are_or_groups_not_and_requirements(question):
    index = SectionEvidenceIndex([node('', 'КР — коэффициент резервирования фабрики.', 'alias-only.txt')])
    seeds = index.body_seeds(question, mode='coverage')
    assert seeds and seeds[0]['lexical_coverage'] == 1
    selected = index.select_v2(question, [])
    assert selected and selected[0]['file'] == 'alias-only.txt'


def test_novel_acronym_lookup_retains_original_acronym_without_alias_dictionary():
    index = SectionEvidenceIndex([node('Телеметрия > НЩЛ', 'НЩЛ — новый параметр счетчика.')])
    assert index.select_v2('Объясни НЩЛ простыми словами, пожалуйста.', [])[0]['section_title'] == 'НЩЛ'


def test_overview_reserves_each_immediate_sibling_not_just_different_documents():
    children = ['Прогнозные', 'Фактические', 'Корректировочные', 'Округление']
    items = [node('Договорные условия', 'Договор задает виды начислений и настройки.')]
    items += [node('Договорные условия > ' + label, f'Самостоятельный подраздел {label}.') for label in children]
    # Many apparently strong hits from one branch must not crowd out siblings.
    items += [node(f'Договорные условия > Прогнозные > Пункт {n}', 'Детали прогноза.') for n in range(8)]
    selected = SectionEvidenceIndex(items).select_v2('Что ведется в договорных условиях?', [hit(item) for item in items[5:]], top_k=5)
    assert set(children) <= {item['section_title'] for item in selected}
    assert len(overview_sections('Что ведется в договорных условиях?', selected)) == 4


def test_main_rule_precedes_high_score_rare_exception():
    main = node('Калибровка датчика', 'Калибровка датчика выполняется каждые 30 дней.')
    exception = node('Калибровка датчика > Особые случаи', 'Только для испытательного стенда калибровка датчика выполняется ежечасно.')
    selected = SectionEvidenceIndex([main, exception]).select_v2('Как выполняется калибровка датчика?', [hit(exception, 999)], top_k=1)
    assert selected[0]['section_title'] == 'Калибровка датчика'
    assert selected[0]['evidence_role'] != 'exception'


def test_explicit_exception_question_can_select_exception():
    exception = node('Калибровка датчика > Особые случаи', 'В особых случаях калибровка датчика выполняется ежечасно.')
    selected = SectionEvidenceIndex([exception]).select_v2('Особые случаи калибровки датчика?', [hit(exception)])
    assert selected and selected[0]['evidence_role'] == 'exception'


def test_comparison_reserves_both_categories_in_one_document():
    three = node('3 ЦК', 'Стоимость для третьей ценовой категории включает объем и тариф.')
    six = node('6 ЦК', 'Стоимость для шестой ценовой категории включает план и отклонения.')
    more_three = [node(f'3 ЦК > Правило {n}', 'Для третьей ценовой категории дополнительное условие.') for n in range(6)]
    selected = SectionEvidenceIndex([three, six, *more_three]).select_v2(
        'Сравни 3 и 6 ЦК', [hit(item) for item in [three, *more_three, six]], top_k=2,
    )
    assert {'3 ЦК', '6 ЦК'} <= set().union(*(set(item['coverage_facets']) for item in selected))
    assert {item['section_title'] for item in selected} >= {'3 ЦК', '6 ЦК'}


def test_specific_category_cannot_be_replaced_by_incidental_later_mention():
    wrong = node('3 ЦК', 'Стоимость определяется по фактическому объему.\n\nДля сравнения: 6 ЦК требует планирования.')
    right = node('6 ЦК', 'Шестая ценовая категория требует почасового плана.')
    selected = SectionEvidenceIndex([wrong, right]).select_v2('Как рассчитывается 6 ЦК?', [hit(wrong, 100), hit(right)], top_k=2)
    assert selected and all(item['section_title'] != '3 ЦК' for item in selected)


def test_numbered_normative_opening_preserves_category_applicability():
    wrong = node('Пункт 4', '4. Цена для первой ценовой категории.', 'ПП1179.md')
    right = node('Пункт 5', '5. Цена для второй ценовой категории определяется с учетом зон суток.', 'ПП1179.md')
    selected = SectionEvidenceIndex([wrong, right]).select_v2('Как рассчитывается 2 ЦК по постановлениям?', [hit(wrong)])
    assert selected and {item['section_title'] for item in selected} == {'Пункт 5'}


def test_explicit_facets_reserve_distinct_answer_requirements():
    definition = node('Транспорт > Назначение', 'Буферные цистерны служат для временного хранения суспензии.')
    definition2 = node('Склад > Назначение', 'Буферные цистерны являются временным резервуаром.')
    maintenance = node('Транспорт > Обслуживание', 'Буферные цистерны промывают каждые 48 часов.')
    selected = SectionEvidenceIndex([definition, definition2, maintenance]).select_v2(
        'Расскажи про буферные цистерны.', [hit(definition), hit(definition2), hit(maintenance)],
        facets=['назначение буферных цистерн', 'обслуживание буферных цистерн'], top_k=2,
    )
    assert any('48 часов' in item['text'] for item in selected)
    assert {'назначение буферных цистерн', 'обслуживание буферных цистерн'} <= set().union(*(set(item['coverage_facets']) for item in selected))


def test_negative_topic_is_not_required_and_network_only_hit_is_filtered():
    desired = node('6 ЦК > Покупная мощность', 'Покупная мощность определяется по часам пикового потребления.')
    excluded = node('6 ЦК > Сетевая мощность', 'Сетевая мощность определяется по договорной нагрузке.')
    selected = SectionEvidenceIndex([desired, excluded]).select_v2(
        'Смотри только покупную мощность для 6 ЦК; сетевую не смотри.', [hit(excluded, 100), hit(desired)], top_k=2,
    )
    assert selected and {item['section_title'] for item in selected} == {'Покупная мощность'}


def test_mixed_section_with_desired_fact_and_excluded_comparator_is_preserved():
    mixed = node('6 ЦК > Мощность', 'Покупная мощность оплачивается отдельно. Сетевая мощность — другая составляющая.')
    selected = SectionEvidenceIndex([mixed]).select_v2(
        'Покупная мощность для 6 ЦК; не учитывай сетевую мощность.', [hit(mixed)],
    )
    assert selected and 'Покупная мощность' in selected[0]['text']
    assert selected[0]['excluded_topics'] == ['сетевую мощность']


def test_acl_excluded_and_deleted_filters_apply_to_all_candidate_origins():
    public = node('Возврат насосов', 'Насосы возвращают в течение пяти дней.', 'public.md')
    secret = node('Возврат насосов', 'Насосы возвращают через десять дней.', 'private.md')
    deleted = node('Возврат насосов', 'Старая инструкция.', 'deleted.md', status='deleted')
    index = SectionEvidenceIndex([public, secret, deleted])
    selected = index.select_v2('Возврат насосов', [hit(secret), hit(deleted)], allowed_sources={'public.md'}, excluded_sources={'deleted.md'})
    assert selected and {item['file'] for item in selected} == {'public.md'}
    assert index.select_v2('Возврат насосов', [hit(public)], allowed_sources=set()) == []
    assert index.select_v2('Возврат насосов', [hit(public)], excluded_sources={'public.md', 'private.md'}) == []


def test_revision_mismatch_cannot_reassemble_stale_candidate_or_neighbor():
    current = node('Возврат насосов', 'Текущая редакция: пять дней.', content_hash='new')
    stale = node('Возврат насосов', 'Старая редакция: сто дней.', content_hash='old')
    selected = SectionEvidenceIndex([current]).select_v2('Возврат насосов', [hit(stale)])
    assert selected and all('сто дней' not in item['text'] for item in selected)
    assert all(item.get('content_hash') == 'new' for item in selected)


def test_colliding_revision_nodes_are_not_concatenated_in_coverage_mode():
    stale = node('Возврат насосов', 'Старая редакция: сто дней.', content_hash='old')
    current = node('Возврат насосов', 'Текущая редакция: пять дней.', content_hash='new')
    index = SectionEvidenceIndex([stale, current])
    selected = index.select_v2('Возврат насосов', [hit(current)])
    assert selected and all('сто дней' not in item['text'] for item in selected)
    assert all(item.get('content_hash') == 'new' for item in selected)


def test_comma_category_list_finds_each_category_even_without_semantic_hits():
    items = [node(f'{number} ЦК', f'Правило для категории {number}.') for number in [1, 3, 6]]
    selected = SectionEvidenceIndex(items).select_v2('Сравни 1, 3 и 6 ЦК', [], top_k=3)
    assert {item['section_title'] for item in selected} == {'1 ЦК', '3 ЦК', '6 ЦК'}


def test_headingless_short_fact_and_russian_inflection_remain_general():
    good = node('', 'Резервуар Лазурит вмещает 74 литра.', 'capacity.txt')
    wrong = node('Объем рынка', 'Вместимость резервуара в данном документе не описывается.', 'market.md')
    selected = SectionEvidenceIndex([good, wrong]).select_v2('Какова вместимость резервуара Лазурит?', [hit(wrong, 100)])
    assert selected and selected[0]['file'] == 'capacity.txt'


def test_whole_block_budget_never_slices_formula_and_reports_truncation():
    item = node('Цена', 'Цена выражается в рублях.\n\n' + '$$' + 'x' * 1000 + '$$')
    selected = SectionEvidenceIndex([item]).select_v2('Что такое цена?', [], max_chars=500)
    assert selected and selected[0]['text'] == 'Цена выражается в рублях.'
    assert selected[0]['evidence_truncated']
    assert sum(len(item['text']) for item in selected) <= 500


def test_truncated_tail_does_not_claim_unseen_facet_coverage():
    item = node('Хранение', 'Введение о хранении.\n\n' + 'Перечень: ' + 'x' * 500 + '\n\nУтилизация выполняется по акту.')
    selected = SectionEvidenceIndex([item]).select_v2('Хранение', [hit(item)], facets=['утилизация'], max_chars=100)
    assert selected
    assert ('утилизация' in selected[0]['coverage_facets']) == ('Утилизация выполняется по акту.' in selected[0]['text'])


def test_legacy_default_is_unchanged_and_coverage_is_opt_in():
    index = SectionEvidenceIndex([node('', 'КР — коэффициент резервирования.')])
    assert index.body_seeds('Что такое КРФ/КР?') == []
    assert index.body_seeds('Что такое КРФ/КР?', mode='coverage')
    assert index.select_v2('КР', [], top_k=0) == []
    assert index.select_v2('КР', [], max_chars=0) == []


def test_semantic_synonym_without_lexical_overlap_is_not_unconditionally_dropped():
    semantic = node('Порядок ремонта', 'Поврежденный агрегат доставляется на сервисную площадку.')
    selected = SectionEvidenceIndex([semantic]).select_v2('Как починить сломанный насос?', [hit(semantic)])
    assert selected and selected[0]['text'] == semantic.text


@pytest.mark.parametrize('ordinal,number', [('первую','1'), ('вторая','2'), ('третьей','3'),
                                         ('четвёртую','4'), ('пятой','5'), ('шестая','6')])
def test_written_ordinal_abbreviation_has_same_scope_as_numeric_category(ordinal, number):
    assert category_ids(ordinal + ' ЦК') == frozenset({number})
    wrong = node('7 > 1 ЦК', 'Применяется объем за весь период.')
    right = node(f'{number} ЦК', 'Применяется подтвержденное условие этой категории.')
    selected = SectionEvidenceIndex([wrong, right]).select_v2(f'Как считается {ordinal} ЦК?', [hit(wrong, 999), hit(right)])
    assert selected and all(number in item['scope_categories'] for item in selected)


def test_written_ordinal_comparison_keeps_both_categories_without_admitting_other_one():
    items = [node(f'{number} ЦК', f'Условие категории {number}.') for number in [2, 4, 5]]
    selected = SectionEvidenceIndex(items).select_v2('Сравни четвёртую и пятую ЦК', [hit(item) for item in items], top_k=2)
    assert {item['section_title'] for item in selected} == {'4 ЦК', '5 ЦК'}


def test_exact_acronym_fact_does_not_discard_semantic_full_term_definition():
    abbreviated = node('Краткие обозначения', 'РКС применяется в режиме диагностики.', 'short.md')
    definition = node('Понятия', 'Резерв контрольной системы — дополнительный объем ресурсов для восстановления.', 'full.md')
    selected = SectionEvidenceIndex([abbreviated, definition]).select_v2('Что такое РКС?',
        [hit(abbreviated, 9000), hit(definition, -999)], top_k=2)
    assert {item['file'] for item in selected} == {'short.md', 'full.md'}


def test_collapsed_large_markdown_cannot_crowd_out_governing_section_and_facets():
    background = ('Предисловие о практике работы, организации производства и истории предприятия. ' * 20)
    incidental = 'В справочнике упоминаются режимы очистки и условия применения, а также классификация установок.'
    giant = node('Справочник > Предисловие', '\n\n'.join([background] * 34 + [incidental]), 'background.md')
    governing_text = ('Режимы очистки классифицируются по температуре и виду реагента. '
                      'Основное правило: холодный режим применяется для растворов первого класса.\n\n'
                      + 'Описание технологических ограничений и состава реагентов. ' * 26)
    governing = node('Классификация режимов очистки', governing_text, 'operations.md')
    conditions = node('Режимы очистки > Условия применения',
                      'Условия применения режимов очистки: контроль температуры и состава реагента.', 'safety.md')
    index = SectionEvidenceIndex([giant, governing, conditions])
    selected = index.select_v2('Что такое режимы очистки?', [hit(giant, 1e12), hit(governing, -100), hit(conditions, -1000)],
        facets=['Классификация режимов очистки', 'Условия применения режимов очистки'], max_chars=36000)
    assert len(giant.text) > 50000
    assert selected[0]['file'] == 'operations.md'
    assert any('Основное правило:' in item['text'] for item in selected)
    assert any(item['file'] == 'safety.md' for item in selected)
    assert sum(len(item['text']) for item in selected) <= 36000
    assert max(len(item['text']) for item in selected) < 18000
    assert selected[0]['selection_signals']['heading_coverage'] == 1.0
    assert selected[0]['selection_signals']['heading_specificity'] > 0


def test_long_section_excerpt_reaches_relevant_late_paragraph_not_only_preface():
    preface = 'Нейтральный вводный текст о работе предприятия. ' * 12
    text = '\n\n'.join([preface] * 90 + [
        'Для калибровки фотоприемника применяют эталонный луч.',
        '$$D=A-B$$',
        'Где D — величина смещения; A и B — показания эталона.',
    ])
    selected = SectionEvidenceIndex([node('Общие сведения', text)]).select_v2(
        'Калибровка фотоприемника', [], max_chars=1800,
    )
    assert selected and 'эталонный луч' in selected[0]['text']
    assert '$$D=A-B$$' in selected[0]['text']
    assert 'Где D' in selected[0]['text']
    assert selected[0]['evidence_excerpt']
    assert len(selected[0]['text']) <= 1800


def test_focused_excerpt_preserves_whole_condition_and_formula_neighbor_paragraphs():
    prefix = 'Вводные организационные сведения. ' * 20
    condition = 'Только при отключенном питании и температуре ниже 20 градусов допускается калибровка фотоприемника; для включенного прибора этот порядок запрещен.'
    formula = '$$D=A-B$$'
    notation = 'Где D — смещение, A — сигнал эталона, B — сигнал прибора. Изменять знак вычитания нельзя.'
    text = '\n\n'.join([prefix] * 40 + [condition, formula, notation] + [prefix] * 20)
    selected = SectionEvidenceIndex([node('Порядок', text)]).select_v2('Калибровка фотоприемника', [], max_chars=1500)
    assert selected
    assert condition in selected[0]['text']
    assert formula in selected[0]['text']
    assert notation in selected[0]['text']


def test_heading_only_leftovers_do_not_count_as_useful_evidence():
    heading = node('Настройка датчика', '## Настройка датчика')
    main = node('Руководство > Настройка датчика', 'Настройка датчика выполняется по коду прибора.')
    selected = SectionEvidenceIndex([heading, main]).select_v2('Настройка датчика', [hit(heading, 100), hit(main)], max_chars=500)
    assert selected and all(item['text'] != heading.text for item in selected)
    assert any('по коду прибора' in item['text'] for item in selected)


def test_display_equation_with_blank_lines_remains_atomic_during_excerpting():
    equation = '$$\nA=B+\n\nC+D\n$$'
    text = 'Калибровка фотоприемника.\n\n' + equation + '\n\nОбозначения A, B, C, D.'
    selected = SectionEvidenceIndex([node('Формула', text)]).select_v2('Калибровка фотоприемника', [], max_chars=46)
    assert selected
    for item in selected:
        assert item['text'].count('$$') % 2 == 0
        assert equation in item['text'] or '$$' not in item['text']


def test_explicit_local_section_reference_resolves_equation_in_new_domain():
    quote = 'Калибровка темнового канала выполняется по коэффициенту, определенному в разделе 7.2.'
    origin = node('Калибровка темнового канала', quote)
    target = node('7.2 Коэффициент темнового канала', 'Коэффициент темнового канала задается формулой:\n\n$$K=A/B$$\n\nЗдесь B не может равняться нулю.')
    selected = SectionEvidenceIndex([origin, target]).select_v2('Калибровка темнового канала', [hit(origin)], resolve_references=True)
    linked = [item for item in selected if item.get('reference_from')]
    assert linked and '$$K=A/B$$' in linked[0]['text']
    assert linked[0]['applicability'] == 'referenced'
    assert linked[0]['reference_target'] == {'kind': 'section', 'locator': '7.2'}
    assert linked[0]['reference_from'][0]['quote'] == quote
    assert linked[0]['reference_from'][0]['section_path'] == origin.metadata['section_path']


def test_category_analogy_imports_only_explicitly_referenced_rule_with_original_scope():
    quote = 'Коэффициент погрешности для 2 ЦК определяется так же как для 4 или 5 ЦК.'
    origin = node('2 ЦК', quote)
    target = node('4 и 5 ЦК', 'Коэффициент погрешности определяется как:\n\n$$E=X-Y$$\n\nДля формулы требуется положительное X.')
    unrelated = node('6 ЦК', 'Коэффициент погрешности вычисляется совершенно иначе.')
    selected = SectionEvidenceIndex([origin, target, unrelated]).select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)], resolve_references=True)
    linked = [item for item in selected if item.get('reference_from')]
    assert linked and linked[0]['scope_categories'] == ['4', '5']
    assert linked[0]['reference_target']['categories'] == ['4', '5']
    assert linked[0]['applicability'] == 'referenced'
    assert '$$E=X-Y$$' in linked[0]['text']
    assert all(item['section_title'] != '6 ЦК' for item in selected)


def test_comparison_mention_without_analogy_is_not_permission_to_import_other_scope():
    origin = node('2 ЦК', 'Коэффициент погрешности отличается от варианта для 4 ЦК.')
    target = node('4 ЦК', 'Коэффициент погрешности: отдельная формула.')
    selected = SectionEvidenceIndex([origin, target]).select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)], resolve_references=True)
    assert all(not item.get('reference_from') for item in selected)
    assert all(item['scope_categories'] == ['2'] for item in selected)


def test_reference_does_not_import_different_subject_from_same_category():
    origin = node('2 ЦК', 'Коэффициент погрешности вычисляется как для 4 ЦК.')
    target = node('4 ЦК', 'Потребление топлива вычисляется по расходомеру.')
    selected = SectionEvidenceIndex([origin, target]).select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)], resolve_references=True)
    assert all(not item.get('reference_from') for item in selected)


def test_reference_expansion_is_one_hop_and_no_more_than_three_targets():
    quote = 'Калибровка канала: см. раздел 7.2, раздел 7.3, раздел 7.4 и раздел 7.5.'
    origin = node('Калибровка канала', quote)
    targets = [node(f'7.{number} Коэффициент канала', f'Калибровка канала: коэффициент {number}. См. раздел 9.1.') for number in range(2, 6)]
    second_hop = node('9.1 Независимый справочник', 'Данные для вторичного восстановления.')
    selected = SectionEvidenceIndex([origin, *targets, second_hop]).select_v2('Калибровка канала', [hit(origin)], resolve_references=True)
    linked = [item for item in selected if item.get('reference_from')]
    assert len(linked) == 3
    assert all(item['reference_target']['locator'].startswith('7.') for item in linked)
    assert all(item['section_title'] != second_hop.metadata['section_title'] for item in selected)
    assert all(item['reference_from'][0]['quote'] == quote for item in linked)


def test_exact_symbolic_reference_is_one_hop_not_a_recursive_walk():
    origin = node('Калибровка темнового канала', 'Калибровка темнового канала: см. раздел 7.2.')
    target = node('7.2 Операция', '$$K=A/B$$\n\nПри B=0 расчет запрещен. См. раздел 9.1.')
    second_hop = node('9.1 Другие операции', '$$L=C/D$$')
    selected = SectionEvidenceIndex([origin, target, second_hop]).select_v2('Калибровка темнового канала', [hit(origin)], resolve_references=True)
    linked = [item for item in selected if item.get('reference_from')]
    assert len(linked) == 1
    assert linked[0]['reference_target']['locator'] == '7.2'
    assert '$$K=A/B$$' in linked[0]['text']
    assert all('L=C/D' not in item['text'] for item in selected)


def test_reference_cannot_cross_physical_source_acl_or_revision_boundary():
    origin = node('2 ЦК', 'Коэффициент погрешности определяется как для 4 ЦК.', content_hash='new')
    old = node('4 ЦК', 'Коэффициент погрешности: старая формула.', content_hash='old')
    other = node('4 ЦК', 'Коэффициент погрешности: чужая формула.', file='foreign.md', content_hash='new')
    deleted = node('4 ЦК', 'Коэффициент погрешности: удаленная формула.', status='deleted', content_hash='new')
    selected = SectionEvidenceIndex([origin, old, other, deleted]).select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)],
        allowed_sources={'novel-manual.md', 'foreign.md'}, resolve_references=True)
    assert selected and all(not item.get('reference_from') for item in selected)
    assert all(item['file'] == 'novel-manual.md' and item['scope_categories'] == ['2'] for item in selected)


def test_reference_expansion_can_be_disabled_independently(monkeypatch):
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')
    origin = node('2 ЦК', 'Коэффициент погрешности определяется как для 4 ЦК.')
    target = node('4 ЦК', 'Коэффициент погрешности: эталонная формула.')
    index = SectionEvidenceIndex([origin, target])
    assert all(not item.get('reference_from') for item in index.select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)]))
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'on')
    assert any(item.get('reference_from') for item in index.select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)]))
    assert all(not item.get('reference_from') for item in index.select_v2('Коэффициент погрешности для 2 ЦК', [hit(origin)], resolve_references=False))


@pytest.mark.parametrize('crumb', ['Где:', '1.', '[...]', '.', '=', 'Формула:', '39\n\n[ПРОПУСК АБЗАЦЕВ]\n\n134'])
def test_non_substantive_crumb_is_not_returned_as_a_packed_fact(crumb):
    long_fact = 'Порядок хранения требует соблюдения заданных условий. ' * 40
    item = node('Порядок хранения', long_fact + '\n\n' + crumb)
    index = SectionEvidenceIndex([item])
    assert index.select_v2('Порядок хранения', [hit(item)], max_chars=40) == []
    assert SectionEvidenceIndex([node('Порядок хранения', crumb)]).select_v2('Порядок хранения', [], max_chars=100) == []


def test_amendment_is_softly_deprioritized_only_when_comparable_operative_rule_exists():
    operative = node('Калибровка датчика', 'Калибровка датчика выполняется раз в тридцать дней.', 'manual.md')
    amendment = node('ИЗМЕНЕНИЯ, КОТОРЫЕ ВНОСЯТСЯ В ПОРЯДОК > Калибровка датчика',
                     'Калибровка датчика выполнялась каждый день.', 'changes.md')
    selected = SectionEvidenceIndex([operative, amendment]).select_v2('Калибровка датчика', [hit(amendment, 9999), hit(operative)])
    assert selected[0]['file'] == 'manual.md'
    other = next(item for item in selected if item['file'] == 'changes.md')
    assert other['section_role'] == 'amendment'
    assert other['section_role_penalty'] == -1.2
    assert other['section_role_basis'] == 'heading-pattern-only'


def test_only_amendment_remains_available_without_an_operative_alternative():
    amendment = node('Изменения, которые вносятся в порядок > Калибровка датчика', 'Калибровка датчика выполняется раз в день.')
    selected = SectionEvidenceIndex([amendment]).select_v2('Калибровка датчика', [hit(amendment)])
    assert selected and selected[0]['section_role'] == 'amendment'
    assert not selected[0].get('section_role_penalty')


def test_historical_question_does_not_deprioritize_change_log():
    current = node('Калибровка датчика', 'Калибровка датчика — действующий порядок.')
    history = node('История изменений > Калибровка датчика', 'Калибровка датчика — прежний порядок.')
    selected = SectionEvidenceIndex([current, history]).select_v2('История изменений калибровки датчика', [hit(history), hit(current)])
    historic = next(item for item in selected if item['section_title'] == history.metadata['section_title'] and item['text'] == history.text)
    assert historic['section_role'] == 'change_log'
    assert not historic.get('section_role_penalty')


@pytest.mark.parametrize('style', ['native', 'latex'])
def test_excerpt_reserves_defining_formula_not_earlier_use_or_large_dictionary(style):
    def math(expression):
        return f'[MATH: {expression}]' if style == 'native' else f'$${expression}$$'

    early_use = math('M=C*V')
    defining = math('C=K+Z')
    condition = 'При отрицательном значении Z эта формула неприменима; требуется отдельная калибровка.'
    definitions = '\n'.join([
        '[TABLE]', 'Обозначение | Описание | Ед. изм.',
        math('M') + ' | Масса активного реагента | мг',
        math('C') + ' | Концентрация активного реагента | мг/л',
        math('K') + ' | Эталонная концентрация | мг/л',
        math('Z') + ' | Поправка на разбавление | мг/л',
    ])
    dictionary = 'Концентрация активного реагента указана в терминологическом указателе. ' * 6
    text = '\n\n'.join([
        'Масса активного реагента определяется через концентрацию:', early_use, definitions,
        *([dictionary] * 35),
        'Для активного реагента применяются две формулы.',
        'Как сумма эталонной величины и поправки:', defining, condition,
        *(['Сведения о транспортировке и упаковке. ' * 20] * 4),
    ])
    question = 'Как определяется концентрация активного реагента?'
    selected = SectionEvidenceIndex([node('Метод анализа > Параметры', text)]).select_v2(
        question, [], max_chars=1500,
    )
    assert selected and defining in selected[0]['text']
    assert condition in selected[0]['text']
    assert math('C') + ' | Концентрация активного реагента' in selected[0]['text']
    assert selected[0]['selection_signals']['formula_dependency_blocks']
    assert sum(len(item['text']) for item in selected) <= 1500


def test_symbolic_dependency_is_one_hop_and_does_not_merge_descriptive_subscripts():
    blocks = [
        '[MATH: R_sub(cold,t)] | Сопротивление холодного канала | Ом',
        '[MATH: R_sub(hot,t)] | Сопротивление горячего канала | Ом',
        'Сопротивление холодного канала:', '[MATH: R_sub(cold,t)=K+A]',
        'Показатель K:', '[MATH: K=B+D]',
        'Показатель B:', '[MATH: B=E+F]',
        'Сопротивление горячего канала:', '[MATH: R_sub(hot,t)=G+H]',
        *(['Обычное техническое описание узла. ' * 30] * 20),
    ]
    question = 'Как определяется сопротивление холодного канала?'
    signals = {}
    excerpt, _ = _focused_excerpt('\n\n'.join(blocks), 1100, _query_groups(question), {},
                                  question=question, diagnostics=signals)
    assert '[MATH: R_sub(cold,t)=K+A]' in excerpt
    assert '[MATH: K=B+D]' in excerpt
    assert signals['formula_dependency_max_hops'] == 1
    assert set(signals['formula_dependency_blocks']) == {3, 5}
    assert 7 not in signals['formula_dependency_blocks']
    assert 9 not in signals['formula_dependency_blocks']


def test_formula_dependencies_never_reconstruct_another_section_or_source():
    active = node('Процесс > Реагент', 'Концентрация реагента:\n\n[MATH: C=K+Z]\n\n' + 'Данные по процессу. ' * 200)
    secret = node('Секретный процесс', 'Концентрация реагента:\n\n[MATH: K=SECRET]', 'other.md')
    selected = SectionEvidenceIndex([active, secret]).select_v2('Концентрация реагента', [hit(active)],
        allowed_sources={'novel-manual.md'}, max_chars=800)
    assert selected and all('SECRET' not in item['text'] for item in selected)
    assert all(item['file'] == 'novel-manual.md' for item in selected)


def test_formula_definition_can_omit_period_indices_but_not_descriptive_qualifier():
    definition = '[MATH: C_sub(active)] | Концентрация активного реагента | мг/л'
    correction = '[MATH: Z_sub(active)] | Поправка активного реагента | мг/л'
    equation = '[MATH: C_sub(active,g,t)=K_sub(active,g,t)+Z_sub(active,g,t)]'
    text = '\n\n'.join([
        definition, correction,
        *(['Концентрация активного реагента — термин классификатора. ' * 5] * 30),
        'Сумма эталонной величины и поправки:', equation,
    ])
    question = 'Концентрация активного реагента'
    excerpt, _ = _focused_excerpt(text, 1200, _query_groups(question), {}, question=question)
    assert equation in excerpt and definition in excerpt and correction in excerpt


def test_equation_is_not_packed_without_oversized_explicit_condition():
    condition = 'Только при соблюдении всех условий допускается расчет: ' + 'требование обязательной проверки; ' * 60
    equation = '[MATH: C=K+Z]'
    text = '\n\n'.join(['Концентрация реагента.', condition, equation, 'Концентрация реагента измеряется прибором.'])
    question = 'Концентрация реагента'
    excerpt, _ = _focused_excerpt(text, 200, _query_groups(question), {}, question=question)
    assert equation not in excerpt
    assert condition not in excerpt


def test_dynamic_budget_reserves_payload_for_tenth_formula_after_four_long_hits():
    question = 'Калибровка датчика'
    long_body = '\n\n'.join(['Калибровка датчика выполняется в соответствии с процедурой испытаний. ' * 8] * 22)
    items = [node(f'Приложение {number}', long_body, f'background-{number}.md') for number in range(4)]
    items += [node(f'Порядок {number}', 'Калибровка датчика требует проверки герметичности.',
                   f'conditions-{number}.md') for number in range(5)]
    equation = '[MATH: K_sub(sensor,t)=A_sub(sensor,t)-B_sub(sensor,t)]'
    guard = 'При нулевом эталонном сигнале этот расчет запрещен; датчик требует отдельной настройки.'
    target = node('Измерение > Поправка', '\n\n'.join([
        'Калибровка датчика: вычисляют поправку по измерениям эталона.', equation, guard,
        'Обозначения A и B — показания двух каналов; K — поправка.',
    ]), 'primary-procedure.md')
    items.append(target)
    selected = SectionEvidenceIndex(items).select_v2(question, [hit(item) for item in items],
                                                   top_k=10, max_chars=12000)
    primary = next(item for item in selected if item['file'] == target.metadata['file_name'])
    assert len(selected) == 10
    assert equation in primary['text'] and guard in primary['text']
    assert sum(len(item['text']) for item in selected) <= 12000
    assert all(item['selection_signals']['fair_budget_chars'] >= 1200 for item in selected)
    assert selected[0]['selection_signals']['reserved_payload_slots'] == 10


@pytest.mark.parametrize('count', [8, 9])
def test_compact_sibling_family_preserves_every_method_and_locator(count):
    parent = node('Обслуживание > Регенерация фильтра', 'Регенерация фильтра допускает несколько самостоятельных методов.')
    children = [node(parent.metadata['section_path'] + f' > Метод {number}',
                     f'Метод {number}: промыть элемент раствором заданной температуры; выдержать {number + 2} минут.')
                for number in range(count)]
    noise = [node(f'Справочник {number}', 'Регенерация фильтра описана в руководстве. ' * 30, f'other-{number}.md')
             for number in range(8)]
    index = SectionEvidenceIndex([parent, *children, *noise])
    selected = index.select_v2('Как выполняется регенерация фильтра?',
                               [*[hit(item, 1000) for item in noise], hit(parent), hit(children[0]), hit(children[1])],
                               top_k=10, max_chars=4000)
    family = next(item['evidence_family'] for item in selected if item.get('evidence_family'))
    expected = {child.metadata['section_path'] for child in children}
    assert family['complete'] and set(family['selected_children']) == expected
    assert not family['missing_children'] and not family['excerpted_children']
    assert len([item for item in selected if item.get('evidence_family_id')]) == count
    assert expected <= {item['section_path'] for item in selected}
    assert sum(len(item['text']) for item in selected) <= 4000
    assert len(selected) <= 10


def test_sibling_family_is_explicitly_incomplete_when_budget_cannot_hold_methods():
    parent = node('Обслуживание > Регенерация фильтра', 'Регенерация фильтра имеет девять методов.')
    children = [node(parent.metadata['section_path'] + f' > Метод {number}',
                     f'Метод {number}: ' + 'Обязательные требования к подготовке, выполнению и завершению процедуры. ' * 5)
                for number in range(9)]
    selected = SectionEvidenceIndex([parent, *children]).select_v2('Как выполняется регенерация фильтра?',
        [hit(parent), hit(children[0]), hit(children[1])], top_k=10, max_chars=500)
    assert selected
    family = next(item['evidence_family'] for item in selected if item.get('evidence_family'))
    assert not family['complete']
    assert family['missing_children'] and family['total_children'] == 9
    assert family['incomplete_reason'] == 'payload-or-slot-budget'
    assert sum(len(item['text']) for item in selected) <= 500


def test_specific_named_method_does_not_expand_sibling_family():
    parent = node('Регенерация фильтра', 'Регенерация фильтра проводится двумя способами.')
    cold = node('Регенерация фильтра > Холодная промывка', 'Холодная промывка выполняется при комнатной температуре.')
    hot = node('Регенерация фильтра > Термическая обработка', 'Термическая обработка требует температуры выше 80 градусов.')
    selected = SectionEvidenceIndex([parent, cold, hot]).select_v2('Как выполняется холодная промывка фильтра?', [hit(cold)])
    assert selected and all(not item.get('evidence_family') for item in selected)
    assert all('выше 80' not in item['text'] for item in selected)


def test_prohibited_sibling_request_does_not_activate_completeness_bundle():
    parent = node('Регенерация фильтра', 'Регенерация фильтра проводится двумя способами.')
    cold = node('Регенерация фильтра > Холодная промывка', 'Холодная промывка фильтра выполняется при комнатной температуре.')
    hot = node('Регенерация фильтра > Термическая обработка', 'Термическая обработка фильтра требует высокой температуры.')
    selected = SectionEvidenceIndex([parent, cold, hot]).select_v2(
        'Регенерация фильтра; не рассматривай термическую обработку.', [hit(parent), hit(cold), hit(hot)])
    assert selected and all(not item.get('evidence_family') for item in selected)
    assert all('высокой температуры' not in item['text'] for item in selected)


def test_sibling_family_cannot_import_another_source_revision_or_descendant():
    parent = node('Регенерация фильтра', 'Регенерация фильтра выполняется двумя методами.', content_hash='new')
    children = [node('Регенерация фильтра > ' + name, name + ': режим обработки.', content_hash='new')
                for name in ['Промывка', 'Продувка']]
    excluded = [node('Регенерация фильтра > Секретный', 'Секретная обработка.', file='private.md', content_hash='new'),
                node('Регенерация фильтра > Старый', 'Старая обработка.', content_hash='old'),
                node('Регенерация фильтра > Удаленный', 'Удаленная обработка.', status='deleted', content_hash='new'),
                node('Регенерация фильтра > Промывка > Ограничения', 'Вложенные ограничения.', content_hash='new')]
    selected = SectionEvidenceIndex([parent, *children, *excluded]).select_v2('Какие способы регенерации фильтра?', [hit(parent)],
        allowed_sources={'novel-manual.md'})
    family = next(item['evidence_family'] for item in selected if item.get('evidence_family'))
    assert family['complete'] and family['total_children'] == 2
    assert set(family['expected_children']) == {child.metadata['section_path'] for child in children}
    assert all('Секретный' not in item['text'] and 'Старая обработка' not in item['text'] for item in selected)


def test_sibling_family_has_independent_count_and_character_bounds():
    parent = node('Регенерация фильтра', 'Регенерация фильтра выполняется несколькими методами.')
    children = [node(f'Регенерация фильтра > Метод {number}', f'Режим {number}: ' + 'Полная последовательность действий. ' * 20)
                for number in range(14)]
    selected = SectionEvidenceIndex([parent, *children]).select_v2('Какие способы регенерации фильтра?', [hit(parent)],
        top_k=24, max_chars=20000)
    family = next(item['evidence_family'] for item in selected if item.get('evidence_family'))
    assert not family['complete'] and family['incomplete_reason'] == 'family-limit'
    assert family['omitted_children_count'] == 2 and len(family['expected_children']) == 12
    assert sum(len(item['text']) for item in selected if item.get('evidence_family_id')) <= 6000


def test_overview_keeps_complete_applicability_and_native_calculation_cohort():
    applicability = 'Режим R4: фильтр отключен, давление ниже 2 бар; требуется полный цикл обработки.'
    equations = ['Подготовка: [MATH: A=B+C]', 'Промывка: [MATH: D=E*F]', 'Продувка: [MATH: G=H-I]']
    other_scope = 'Режим R8: допускается сокращенный цикл.'
    dictionary = 'Этапы обработки фильтра в режиме R4 описаны в терминологическом указателе. ' * 5
    text = '\n\n'.join([
        applicability, 'Формула:', *equations,
        'Где A — длительность подготовки.',
        other_scope, 'Промывка: [MATH: D=J*K]', 'Сушка: [MATH: L=M*N]',
        *([dictionary] * 40),
    ])
    question = 'Какие этапы обработки фильтра рассчитываются в режиме R4?'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 1100, _query_groups(question), {}, question=question, diagnostics=signals)
    assert applicability in excerpt and all(equation in excerpt for equation in equations)
    assert signals['scoped_cohort_blocks'] == [[0, 1, 2, 3, 4]]
    assert not signals['scoped_cohort_incomplete']
    assert len(excerpt) <= 1100


def test_category_cohort_does_not_import_neighbors_scope_instead_of_requested():
    first = ['Применение 2 ЦК: режим без планирования.', 'Этап альфа: [MATH: A=B]', 'Этап бета: [MATH: C=D]']
    second = ['Применение 5 ЦК: режим с обязательным планированием.',
              'Этап гамма: [MATH: E=F]', 'Этап дельта: [MATH: G=H]', 'Этап эпсилон: [MATH: I=J]']
    text = '\n\n'.join([*first, *second, 'Где E — плановый параметр.', *(['Описание услуг по 5 ЦК. ' * 20] * 40)])
    question = 'Какие этапы рассчитываются по 5 ЦК?'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 900, _query_groups(question), {}, question=question, diagnostics=signals)
    assert all(block in excerpt for block in second)
    assert signals['scoped_cohort_blocks'] == [[3, 4, 5, 6]]
    assert not signals['scoped_cohort_incomplete']


def test_too_small_budget_does_not_turn_cohort_into_partial_list():
    condition = 'Режим R4: требуется одновременное выполнение всех этапов.'
    equations = ['Подготовка: [MATH: A=B+C]', 'Промывка: [MATH: D=E*F]', 'Продувка: [MATH: G=H-I]']
    text = '\n\n'.join([condition, *equations, 'Где A — длительность.', 'Обработка фильтра. ' * 100])
    question = 'Какие этапы обработки в режиме R4?'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 85, _query_groups(question), {}, question=question, diagnostics=signals)
    assert not any(equation in excerpt for equation in equations)
    assert condition not in excerpt
    assert signals['scoped_cohort_incomplete'] == [{'anchor': 0, 'scope_categories': [], 'reason': 'payload-budget'}]


def test_targeted_quantity_question_keeps_symbolic_dependency_priority_not_whole_cohort():
    blocks = ['Режим R4: полный цикл обработки фильтра.',
              'Подготовка: [MATH: M=C*V]', 'Промывка: [MATH: D=E*F]', 'Продувка: [MATH: G=H-I]',
              '[MATH: C] | Концентрация реагента | мг/л',
              *(['Концентрация реагента — термин справочника. ' * 10] * 20),
              'Сумма эталонной величины и поправки:', '[MATH: C=K+Z]']
    question = 'Как определяется концентрация реагента?'
    signals = {}
    excerpt, _ = _focused_excerpt('\n\n'.join(blocks), 800, _query_groups(question), {}, question=question, diagnostics=signals)
    assert '[MATH: C=K+Z]' in excerpt
    assert not signals['scoped_cohort_blocks']


def test_definition_does_not_require_all_subsystems_with_matching_ancestor_topic():
    parent = node('Параметры калибровки датчика', 'Калибровка датчика — настройка измерительного канала по эталону.')
    children = [node('Параметры калибровки датчика > ' + name, name + ': настройки канала.')
                for name in ['Подсистема измерений', 'Подсистема хранения', 'Подсистема уведомлений']]
    selected = SectionEvidenceIndex([parent, *children]).select_v2('Что такое калибровка датчика?', [hit(parent)])
    assert selected and all('evidence_family' not in item for item in selected)
    overview = SectionEvidenceIndex([parent, *children]).select_v2('Какие параметры калибровки датчика?', [hit(parent)])
    assert any(item.get('evidence_family') for item in overview)


def test_complete_reconstruction_resets_previous_excerpt_and_family_snapshot():
    text = '\n\n'.join(['Калибровка датчика выполняется по эталону. ' * 5] * 8)
    original = node('Калибровка датчика', text)
    index = SectionEvidenceIndex([original])
    partial = index.select_v2('Калибровка датчика', [hit(original)], max_chars=500)
    assert partial[0]['evidence_excerpt']
    partial[0]['evidence_family'] = {'id': 'stale', 'complete': False}
    partial[0]['evidence_family_id'] = 'stale'
    complete = index.select_v2('Калибровка датчика', partial, max_chars=10000)
    assert complete[0]['text'] == index.sections[0]['text']
    assert all(key not in complete[0] for key in
               ('evidence_excerpt', 'evidence_truncated', 'evidence_block_indices', 'evidence_family', 'evidence_family_id'))


def test_negative_clause_never_consumes_the_next_query_line():
    positive, forbidden = _positive_query('Калибровка датчика; шум не смотри\nКоэффициент погрешности фотоприемника')
    assert forbidden == ['шум']
    assert 'Коэффициент погрешности фотоприемника' in positive


def test_application_envelope_keeps_subject_and_local_reference_without_host_label_noise():
    subject = 'Как определяется погрешность фотоприемника для 2 ЦК?'
    followup = 'Смотри только погрешность фотоприемника, шум не смотри'
    enriched = ('Тема диалога — вопрос пользователя: ' + subject + '\n'
                'Условия и уточнения пользователя: ' + followup + '\n'
                'Исключить из предмета ответа: шум\n'
                'Текущий запрос пользователя: ' + followup)
    positive, forbidden = _positive_query(enriched)
    assert 'фотоприемника' in positive and '2 ЦК' in positive
    assert 'пользователя' not in positive and 'диалога' not in positive
    assert set(forbidden) == {'шум'}
    origin = node('2 ЦК', 'Погрешность фотоприемника определяется так же как для 4 ЦК.')
    target = node('4 ЦК', 'Погрешность фотоприемника:\n\n[MATH: E=K+Z]')
    other = node('Шум', 'Шум фотоприемника вычисляется отдельным методом.')
    selected = SectionEvidenceIndex([origin, target, other]).select_v2(enriched,
        [hit(origin), hit(target), hit(other)], resolve_references=True)
    referenced = [item for item in selected if item.get('reference_from')]
    assert referenced and '[MATH: E=K+Z]' in referenced[0]['text']
    assert referenced[0]['scope_categories'] == ['4']
    assert referenced[0]['applicability'] == 'referenced'


def test_ordinary_topic_words_are_not_globally_removed_as_envelope_labels():
    question = 'Какие условия и уточнения пользователя сохраняет диалог?'
    positive, _ = _positive_query(question)
    assert positive == question


@pytest.mark.parametrize('leading_pipe', [False, True])
def test_definition_closure_packs_native_legend_rows_without_unrelated_table_cells(leading_pipe):
    formula = '[MATH: A=B+C]'
    correction = '[MATH: C] | Поправка на вязкость | л/ч\nТолько при ненулевой температуре раствора.'
    legend = '\n'.join([
        '[TABLE]', 'Обозначение | Описание | Единицы',
        '[MATH: A] | Скорость нейтрализации | л/ч',
        '[MATH: X] | Посторонняя настройка упаковки: ' + 'описание упаковки; ' * 120 + ' | шт',
        '[MATH: B] | Эталонная скорость | л/ч',
        '[MATH: Y] | Посторонняя настройка перевозки: ' + 'описание перевозки; ' * 120 + ' | шт',
        correction,
    ])
    if leading_pipe:
        legend = '\n'.join('| ' + line if '|' in line else line for line in legend.splitlines())
    text = '\n\n'.join([legend, 'Скорость нейтрализации:', formula, 'Сведения об упаковке. ' * 100])
    question = 'Скорость нейтрализации'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 800, _query_groups(question), {}, question=question, diagnostics=signals)
    assert formula in excerpt and correction in excerpt
    assert '[MATH: A] | Скорость нейтрализации | л/ч' in excerpt
    assert '[MATH: B] | Эталонная скорость | л/ч' in excerpt
    assert 'Посторонняя настройка' not in excerpt
    assert signals['definition_closure_incomplete'] is False
    assert signals['missing_definition_symbols'] == []
    assert len(excerpt) <= 800


def test_dependency_uses_full_native_run_chain_and_defining_chain_of_rhs_symbol():
    formula = '[MATH: A=B] [[MATHML_REF:root]] + [MATH: C]'
    dependency = '[MATH: C=D] [[MATHML_REF:support]] + [MATH: E]'
    legend = '\n'.join([
        '[TABLE]', 'Обозначение | Описание',
        '[MATH: A] | Коэффициент катализа', '[MATH: B] | Базовый коэффициент',
        '[MATH: C] | Поправка на температуру', '[MATH: D] | Лабораторная поправка',
        '[MATH: E] | Поправка датчика',
    ])
    text = '\n\n'.join([legend, 'Коэффициент катализа:', formula,
                         'Сведения о перевозке. ' * 100, 'Поправка на температуру:', dependency])
    question = 'Коэффициент катализа'
    blocks = _content_blocks(text)
    packages, _ = _formula_dependencies(blocks, question, _query_groups(question))
    root = next(package for package in packages if package['symbol'] == 'a')
    support = next(package for package in packages if package['symbol'] == 'c')
    assert root['rhs'] == {'b', 'c'}
    assert support['rhs'] == {'d', 'e'}
    signals = {}
    excerpt, _ = _focused_excerpt(text, 800, _query_groups(question), {}, question=question, diagnostics=signals)
    assert formula in excerpt and dependency in excerpt
    assert '[MATH: C] | Поправка на температуру' in excerpt
    assert set(signals['formula_dependency_blocks']) == {root['block'], support['block']}
    assert not signals['definition_closure_incomplete']


def test_native_run_dependency_parser_does_not_join_separate_paragraphs_or_equalities():
    text = '[MATH: A=B]\n\n+ [MATH: C]\n\n[MATH: D=E]\n[MATH: F=G]'
    assert _math_expressions(text) == ['A=B', 'C', 'D=E', 'F=G']
    assert _content_blocks(text) == ['[MATH: A=B]', '+ [MATH: C]', '[MATH: D=E]\n[MATH: F=G]']


@pytest.mark.parametrize('line_break', ['\n', '\n\n'])
def test_word_math_line_break_and_sum_remain_one_native_equation(line_break):
    formula = '[MATH: A=∑_sub(j=1)^(N)' + line_break + '(B_sub(j)+C_sub(j))]'
    legend = ('[TABLE]\nОбозначение | Описание\n[MATH: A] | Скорость осаждения\n'
              '[MATH: B_sub(j)] | Эталонная скорость\n[MATH: C_sub(j)] | Поправка датчика')
    text = '\n\n'.join([legend, 'Скорость осаждения:', formula, 'Сведения о перевозке. ' * 100])
    question = 'Скорость осаждения'
    assert formula in _content_blocks(text)
    assert len(_math_expressions(formula)) == 1
    signals = {}
    excerpt, _ = _focused_excerpt(text, 700, _query_groups(question), {}, question=question, diagnostics=signals)
    assert formula in excerpt
    assert '[MATH: C_sub(j)] | Поправка датчика' in excerpt
    assert not signals['definition_closure_incomplete']


def test_multiline_native_notation_cell_preserves_its_exact_row_and_conditions():
    definition = '[MATH: C_sub(\nactive)] | Поправка датчика\nТолько для режима R7.'
    legend = '\n'.join(['[TABLE]', 'Обозначение | Описание', '[MATH: A] | Показатель осаждения',
                        '[MATH: X] | ' + 'Посторонняя настройка. ' * 100, definition])
    formula = '[MATH: A=B+C_sub(active)]'
    text = '\n\n'.join([legend, 'Показатель осаждения:', formula, 'Описание прибора. ' * 100])
    question = 'Показатель осаждения'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 650, _query_groups(question), {}, question=question, diagnostics=signals)
    assert formula in excerpt and definition in excerpt
    assert 'Посторонняя настройка.' not in excerpt
    assert not signals['definition_closure_incomplete']


@pytest.mark.parametrize('oversized', ['row', 'condition'])
def test_oversized_required_definition_is_reported_without_slicing_its_conditions(oversized):
    definition = '[MATH: C] | Поправка датчика.'
    condition = 'Только после выполнения всех обязательных проверок.'
    if oversized == 'row':
        definition += ' Обязательная проверка; ' * 90
    else:
        condition += ' Обязательная проверка; ' * 90
    text = '\n\n'.join([
        '[TABLE]\nОбозначение | Описание\n[MATH: A] | Скорость осаждения\n[MATH: B] | Эталонная скорость\n' + definition,
        condition, 'Скорость осаждения:', '[MATH: A=B+C]',
    ])
    selected = SectionEvidenceIndex([node('Метод осаждения', text)]).select_v2(
        'Скорость осаждения', [], max_chars=600,
    )
    assert selected and '[MATH: A=B+C]' in selected[0]['text']
    assert '[MATH: C] | Поправка датчика.' not in selected[0]['text']
    signals = selected[0]['selection_signals']
    assert signals['definition_closure_incomplete'] is True
    assert signals['missing_definition_symbols'] == ['c']
    assert len(selected[0]['text']) <= 600


@pytest.mark.parametrize('fits', [False, True])
def test_ascii_definition_delimiter_is_not_a_dangling_equation_operator(fits):
    correction = '[MATH: C_sub(active)] - Поправка датчика.'
    if not fits:
        correction += ' Обязательная проверка; ' * 90
    text = '\n\n'.join([
        '[MATH: A] | Скорость осаждения\n[MATH: B] | Эталонная скорость', correction,
        'Скорость осаждения:', '[MATH: A=B+C_sub(active)]', 'Описание упаковки. ' * 100,
    ])
    question = 'Скорость осаждения'
    _, definitions = _formula_dependencies(_content_blocks(text), question, _query_groups(question))
    assert 'c_sub(active)' in definitions
    signals = {}
    excerpt, _ = _focused_excerpt(text, 650, _query_groups(question), {}, question=question, diagnostics=signals)
    assert '[MATH: A=B+C_sub(active)]' in excerpt
    assert (correction in excerpt) is fits
    assert signals['definition_closure_incomplete'] is not fits
    assert signals['missing_definition_symbols'] == ([] if fits else ['c_sub(active)'])


@pytest.mark.parametrize('fits', [False, True])
def test_independent_notation_labels_share_a_definition_without_becoming_an_equation(fits):
    definition = '[MATH: B] [[MATHML_REF:label]] [MATH: C] | Эталонная скорость и поправка датчика.'
    if not fits:
        definition += ' Обязательная проверка; ' * 90
    text = '\n\n'.join([
        '[TABLE]\nОбозначение | Описание\n[MATH: A] | Скорость осаждения\n' + definition,
        'Скорость осаждения:', '[MATH: A=B] + [MATH: C]', 'Описание упаковки. ' * 100,
    ])
    question = 'Скорость осаждения'
    _, definitions = _formula_dependencies(_content_blocks(text), question, _query_groups(question))
    assert definitions['b'] == definitions['c']
    assert _math_expressions('[MATH: B] [MATH: C]') == []
    signals = {}
    excerpt, _ = _focused_excerpt(text, 650, _query_groups(question), {}, question=question, diagnostics=signals)
    assert '[MATH: A=B] + [MATH: C]' in excerpt
    assert (definition in excerpt) is fits
    assert signals['definition_closure_incomplete'] is not fits
    assert signals['missing_definition_symbols'] == ([] if fits else ['b', 'c'])


def test_split_notation_rows_are_structural_boundaries_not_formula_lead_ins():
    legend = ('[TABLE]\nОбозначение | Описание\n'
              '[MATH: A] | Скорость осаждения\n[MATH: B] | Эталонная скорость\n'
              '[MATH: C] | Поправка датчика\n')
    legend += '\n'.join('[MATH: ' + symbol + '] | Посторонняя настройка ' + 'продолжение; ' * 18
                        for symbol in ('X', 'Y', 'Z'))
    formula = '[MATH: A=B+C]'
    text = legend + '\n\n' + formula + '\n\n' + 'Посторонний материал. ' * 100
    question = 'Скорость осаждения'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 800, _query_groups(question), {}, question=question, diagnostics=signals)
    assert formula in excerpt
    assert '[MATH: C] | Поправка датчика' in excerpt
    assert signals['formula_dependency_blocks'] == [_content_blocks(text).index(formula)]
    assert not signals['definition_closure_incomplete']


def test_all_contiguous_definition_conditions_are_atomic_even_after_second_paragraph():
    conditions = ['Только для режима R7.', 'Если датчик прошел калибровку.',
                  'При выполнении всех проверок: ' + 'обязательная проверка; ' * 90]
    text = '\n\n'.join([
        '[TABLE]\nОбозначение | Описание\n[MATH: A] | Скорость осаждения\n'
        '[MATH: B] | Эталонная скорость\n[MATH: C] | Поправка датчика',
        *conditions, 'Скорость осаждения:', '[MATH: A=B+C]',
    ])
    question = 'Скорость осаждения'
    signals = {}
    excerpt, _ = _focused_excerpt(text, 650, _query_groups(question), {}, question=question, diagnostics=signals)
    assert '[MATH: A=B+C]' in excerpt
    assert '[MATH: C] | Поправка датчика' not in excerpt
    assert signals['definition_closure_incomplete']
    assert signals['missing_definition_symbols'] == ['c']


@pytest.mark.parametrize('fits', [False, True])
def test_empty_markdown_notation_cell_continues_previous_definition_conditions(fits):
    condition = '| | Только для режима R7.' + ('' if fits else ' Обязательная проверка; ' * 90) + ' |'
    definition = '| [MATH: C] | Поправка датчика |\n' + condition
    text = '\n\n'.join([
        '[TABLE]\n| Обозначение | Описание |\n| [MATH: A] | Скорость осаждения |\n'
        '| [MATH: B] | Эталонная скорость |\n' + definition,
        'Скорость осаждения:', '[MATH: A=B+C]', 'Описание упаковки. ' * 100,
    ])
    question = 'Скорость осаждения'
    assert definition in _content_blocks(text)
    signals = {}
    excerpt, _ = _focused_excerpt(text, 650, _query_groups(question), {}, question=question, diagnostics=signals)
    assert '[MATH: A=B+C]' in excerpt
    assert (definition in excerpt) is fits
    assert signals['definition_closure_incomplete'] is not fits
    assert signals['missing_definition_symbols'] == ([] if fits else ['c'])


def test_inventory_type_table_is_reserved_before_a_method_family():
    parent = node('Маршрутизация', 'В системе существуют основные виды маршрутизации.')
    first = node('Маршрутизация > Режим А',
                 'Виды маршрутизации включают режим А для оперативной обработки сигналов.')
    second = node('Маршрутизация > Режим Б',
                  'Виды маршрутизации включают режим Б для отложенной обработки сигналов.')
    selector = node('Формы > Создание маршрутизации',
                    'Документ маршрутизации доступен для редактирования.\n\n[TABLE]\n'
                    'Поле | Описание\nТип | Выбор из списка: Кварц; Олива; Туман\n'
                    'Статус | Выбор из списка: Новый; Закрыт')
    result = SectionEvidenceIndex([parent, first, second, selector]).select_v2(
        'Какие виды маршрутизации реализованы в системе?',
        [hit(item) for item in (parent, first, second, selector)], top_k=3, max_chars=36000)
    selected = next(item for item in result if item['section_path'] == selector.metadata['section_path'])
    assert 'Кварц; Олива; Туман' in selected['text']
    assert selected['selection_signals']['inventory_enumeration_reserved']
    assert len(result) <= 3 and sum(len(item['text']) for item in result) <= 36000


def test_inventory_table_with_wrapped_conditions_is_not_pruned_to_fit():
    row = 'Тип | Выбор из списка: Янтарь; Лилия; Риф; Сфера\nДействует только после проверки оператора.'
    irrelevant = '\n'.join(f'Примечание {i} | ' + 'Нерелевантное описание. ' * 30 for i in range(25))
    prelude = '### Создание маршрутизации\n\nТипы допустимы только при условии действующей заявки.'
    selector = node('Формы > Создание маршрутизации',
                    prelude + '\n\n[TABLE]\nПоле | Описание\n'
                    + irrelevant + '\n' + row + '\nСтатус | Новый; Закрыт')
    noise = node('Справочник', 'Весь справочник типов маршрутизации объясняет различия основных категорий.')
    result = SectionEvidenceIndex([selector, noise]).select_v2(
        'Перечислить полный справочник типов маршрутизации и объяснить различия между ними',
        [hit(noise), hit(selector)], top_k=2, max_chars=900)
    assert not any('Янтарь; Лилия; Риф; Сфера' in item['text'] for item in result)
    assert result[0]['selection_signals']['inventory_enumeration_incomplete']
    assert result[0]['selection_signals']['missing_inventory_sections'][0]['reason'] == 'atomic-inventory-packet-budget'
    assert len(result) <= 2 and sum(len(item['text']) for item in result) <= 900


@pytest.mark.parametrize('placement', ['after-table', 'named-row', 'empty-label-row'])
@pytest.mark.parametrize('fits', [False, True])
def test_inventory_conditions_after_choices_are_atomic(placement, fits):
    condition = 'Перечисленные типы допустимы только при действующей заявке.'
    noise = '' if fits else '\n'.join(
        f'Поле {i} | ' + 'Описание дополнительного поля. ' * 30 for i in range(25)) + '\n'
    table = '[TABLE]\nПоле | Описание\n' + noise + 'Тип | Выбор из списка: Кварц; Олива'
    if placement == 'after-table':
        table += '\n\n' + condition
    elif placement == 'named-row':
        table += '\nПрименимость | ' + condition
    else:
        table += '\n | ' + condition
    selector = node('Формы > Создание маршрутизации', 'Документ маршрутизации.\n\n' + table)
    fallback = node('Маршрутизация', 'Общие сведения о маршрутизации без полного справочника.')
    meta = {}
    result = SectionEvidenceIndex([selector, fallback]).select_v2(
        'Какие виды маршрутизации?', [hit(selector), hit(fallback)],
        top_k=2, max_chars=900, selection_meta=meta)
    enumerations = [item for item in result if 'Кварц; Олива' in item['text']]
    if fits:
        assert len(enumerations) == 1
        assert condition in enumerations[0]['text']
        assert not meta and not any(item['selection_signals'].get('inventory_enumeration_incomplete') for item in result)
    else:
        assert not enumerations
        assert any(item['selection_signals'].get('inventory_enumeration_incomplete') for item in result)
    assert sum(len(item['text']) for item in result) <= 900


def test_inventory_packet_preserves_source_order_and_whole_matching_table():
    from evidence_selection import _inventory_enum_rows
    text = ('Условия создания маршрутизации.\n\n[TABLE]\nПоле | Описание\n'
            'Применимость | Только для подтвержденных заявок\n'
            'Тип | Выбор из списка: Кварц; Олива\n'
            ' | После проверки оператора\n\nПоследующее обязательное условие.')
    packet = '\n\n'.join(_inventory_enum_rows(
        {'section_path': 'Формы > Создание маршрутизации', 'text': text},
        'Какие виды маршрутизации?'))
    assert packet == text


def test_inventory_packet_keeps_repeated_governing_conditions_in_source_order():
    from evidence_selection import _inventory_enum_rows
    condition_a = 'Применимость следующей таблицы: только режим А.'
    condition_b = 'Применимость следующей таблицы: только режим Б.'
    first = '[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива'
    second = '[TABLE]\nПоле | Описание\nВид | Выбор из списка: Туман; Лилия'
    unrelated = '[TABLE]\nСтатус | Новый; Закрыт'
    text = '\n\n'.join([condition_a, first, condition_b, unrelated, condition_a, second])
    packet = _inventory_enum_rows(
        {'section_path': 'Формы > Создание маршрутизации', 'text': text},
        'Какие виды маршрутизации?')
    assert packet == [condition_a, first, condition_b, condition_a, second]
    assert packet.count(condition_a) == 2


def test_inventory_reservation_keeps_acl_deletion_and_revision_boundaries():
    path = 'Формы > Создание маршрутизации'
    def table(values):
        return 'Документ маршрутизации.\n\n[TABLE]\nПоле | Описание\nТип | Выбор из списка: ' + values
    active = node(path, table('Кварц; Олива'), file='public.md', revision_id='r2')
    old = node(path, table('Устаревший; Старый'), file='public.md', revision_id='r1')
    private = node(path, table('Закрытый; Секретный'), file='private.md')
    deleted = node(path, table('Удаленный; Неактивный'), file='public.md', status='deleted')
    result = SectionEvidenceIndex([active, old, private, deleted]).select_v2(
        'Какие виды маршрутизации?', [hit(active)], allowed_sources={'public.md'},
        top_k=2, max_chars=2000)
    text = '\n'.join(item['text'] for item in result)
    assert 'Кварц; Олива' in text
    assert not any(value in text for value in ('Устаревший', 'Закрытый', 'Удаленный'))
    assert all(item['file'] == 'public.md' and item.get('revision_id') == 'r2' for item in result)


def test_inventory_reservation_does_not_promote_open_or_unrelated_lists():
    from evidence_selection import _inventory_enum_rows
    question = 'Какие виды маршрутизации?'
    def item(path, text):
        return {'section_path': path, 'text': text}
    assert _inventory_enum_rows(item('Формы > Создание охлаждения',
        '[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива'), question) == []
    assert _inventory_enum_rows(item('Формы > Создание маршрутизации',
        '[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива и другие'), question) == []
    math = '[TABLE]\nПоле | Описание\nТип | Выбор из списка: [MATH: X\n\n+Y]; Олива'
    assert _inventory_enum_rows(item('Формы > Создание маршрутизации', math), question) == []


def test_inventory_path_does_not_reinterpret_negative_scope():
    positive, excluded = _positive_query('Рассматривай только A, B не рассматривай')
    assert excluded == ['B'] and 'A' in positive
    positive, excluded = _positive_query('Не только A, но и B')
    assert excluded == [] and 'A' in positive and 'B' in positive
    positive, excluded = _positive_query('Маршрутизация; не рассматривай A')
    assert excluded == ['A']


def test_inventory_mandatory_prelude_is_not_dropped_to_fit_a_small_budget():
    prelude = 'Типы допустимы только при условии ' + 'проверки действующей заявки ' * 40
    selector = node('Формы > Создание маршрутизации',
                    '### Создание маршрутизации\n\n' + prelude
                    + '\n\n[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива')
    fallback = node('Маршрутизация', 'Общие сведения о маршрутизации не содержат полного справочника.')
    result = SectionEvidenceIndex([selector, fallback]).select_v2(
        'Какие виды маршрутизации?', [hit(selector), hit(fallback)], top_k=2, max_chars=200)
    assert not any('Кварц; Олива' in item['text'] for item in result)
    signals = result[0]['selection_signals']
    assert signals['inventory_enumeration_incomplete']
    assert signals['missing_inventory_sections'][0]['reason'] == 'atomic-inventory-packet-budget'


def test_inventory_profile_is_prepared_once_per_selection(monkeypatch):
    import evidence_selection as selection
    original, calls = selection._inventory_subject, []
    def counted(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)
    monkeypatch.setattr(selection, '_inventory_subject', counted)
    nodes = [node(f'Справочник > Глава {i}', 'Обычное описание формы и доступных действий.')
             for i in range(30)]
    nodes.append(node('Формы > Создание маршрутизации',
                      '[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива'))
    SectionEvidenceIndex(nodes).select_v2('Какие виды маршрутизации?', [], max_chars=2000)
    assert len(calls) == 1


def test_inventory_overflow_without_a_source_carrier_is_metadata_only():
    import json
    prelude = 'Типы допустимы только при условии ' + 'проверки действующей заявки ' * 40
    selector = node('Формы > Создание маршрутизации',
                    prelude + '\n\n[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива',
                    file='catalog.md')
    fallback = node('Маршрутизация', 'Общие сведения о маршрутизации без полного перечня.',
                    file='general.md')
    meta = {}
    result = SectionEvidenceIndex([selector, fallback]).select_v2(
        'Какие виды маршрутизации?', [hit(selector), hit(fallback)],
        allowed_sources={'catalog.md', 'general.md'}, top_k=2, max_chars=200, selection_meta=meta)
    assert all(item['file'] == 'general.md' for item in result)
    assert all(not item['selection_signals'].get('inventory_enumeration_incomplete') for item in result)
    assert meta['inventory_enumeration_incomplete']
    assert set(meta['inventory_enumeration_scoped_warnings'][0]) == {'reason', 'required_chars', 'available_chars'}
    assert not any(value in json.dumps(meta, ensure_ascii=False)
                   for value in ('catalog.md', 'Создание маршрутизации', 'Кварц', 'Олива'))
    empty_meta = {}
    empty = SectionEvidenceIndex([selector]).select_v2(
        'Какие виды маршрутизации?', [hit(selector)], max_chars=1, selection_meta=empty_meta)
    assert empty == [] and empty_meta['inventory_enumeration_incomplete']


@pytest.mark.parametrize('slots', [1, 3, 10])
def test_inventory_reservations_do_not_raise_the_payload_slot_limit(slots):
    catalogs = [node(f'Формы > Создание маршрутизации {i}',
                     'Документ маршрутизации.\n\n[TABLE]\nПоле | Описание\n'
                     f'Тип | Выбор из списка: Кварц-{i}; Олива-{i}')
                for i in range(slots + 3)]
    selected = SectionEvidenceIndex(catalogs).select_v2(
        'Какие виды маршрутизации?', [hit(item) for item in catalogs],
        top_k=slots, max_chars=36000)
    assert len(selected) <= slots
    assert sum(len(item['text']) for item in selected) <= 36000


def test_non_inventory_question_skips_the_inventory_scan(monkeypatch):
    import evidence_selection as selection

    def unexpected_scan(*args, **kwargs):
        pytest.fail('An unrelated question must not scan inventory tables')

    monkeypatch.setattr(selection, '_inventory_enum_rows', unexpected_scan)
    source = node('Маршрутизация', 'Маршрутизация выполняется после проверки заявки.')
    result = SectionEvidenceIndex([source]).select_v2(
        'Как выполняется маршрутизация?', [hit(source)], max_chars=2000)
    assert result
