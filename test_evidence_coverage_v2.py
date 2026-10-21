"""Document-independent coverage, applicability and rollback regressions."""
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex, category_ids, overview_sections, _focused_excerpt, _query_groups


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
