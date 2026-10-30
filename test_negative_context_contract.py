"""Neutral negative-topic packing regressions, not semantic answer gold.

An excluded independent fact must not leak merely because its source section
also contains a requested fact. Conversely, exclusion must not delete words
from a native condition, equation or structured table packet.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import (SectionEvidenceIndex, _content_blocks, _positive_query,
                                new_query_rank_context)
from table_context import parse_table_unit, serialize_table_unit
from search_focus import SearchFocus, SearchOperation, SearchSubject


TOPICS = (
    {
        'id': 'requests', 'heading': 'Обработка заявок',
        'subject': 'Обработка оперативных заявок', 'excluded': 'архивные заявки',
        'primary': ('Обработка оперативных заявок выполняется оператором '
                    'только при наличии подтвержденной карточки.'),
        'unrelated': 'Архивные заявки хранят 90 дней после закрытия.',
        'qualified': ('Обработка оперативных заявок выполняется без открытия архивных заявок; '
                      'это правило действует только при наличии подтвержденной карточки.'),
        'scope_guard': 'Данный порядок не применяется к архивным заявкам.',
    },
    {
        'id': 'parcels', 'heading': 'Доставка посылок',
        'subject': 'Доставка внутренних посылок', 'excluded': 'международные посылки',
        'primary': ('Доставка внутренних посылок подтверждается оператором '
                    'только после сверки адреса.'),
        'unrelated': 'Международные посылки проходят таможенную проверку в течение 30 дней.',
        'qualified': ('Доставка внутренних посылок не включает международные посылки; '
                      'это правило действует только после сверки адреса.'),
        'scope_guard': 'Данный порядок не применяется к международным посылкам.',
    },
)
FILLER = 'Справочная запись описывает общие параметры журнала и оформление карточки. ' * 60
LOCAL_QUOTE = 'См. пункт 5.3 настоящего документа.'
TARGET_TEXT = 'Подтверждение фиксируют в журнале отдельной записью с датой проверки.'


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Synthetic negative-context tests must not use models or network')

    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


def node(title, text, **metadata):
    return SimpleNamespace(text=text, metadata={
        'file_name': 'neutral-procedure.md', 'section_path': 'Руководство > ' + title,
        'section_title': title, 'source_id': 'neutral-source', 'revision_id': 'revision-A',
        'content_hash': 'revision-A', 'status': 'active', **metadata,
    })


def question(topic, *, exclude=True):
    return topic['subject'] + ('; не рассматривай ' + topic['excluded'] if exclude else '')


def select(topic, text, *, references=False, max_chars=36000, exclude=True, **options):
    origin = node(topic['heading'], text)
    target = node('5.3 Подтверждение', TARGET_TEXT)
    records = [origin, target]
    original = deepcopy(records)
    index = SectionEvidenceIndex(records)
    query = question(topic, exclude=exclude)
    seeds = index.body_seeds(query, mode='coverage')
    before = deepcopy(seeds)
    result = index.select_v2(query, seeds, resolve_references=references,
                             top_k=2, max_chars=max_chars, **options)
    assert records == original and seeds == before
    assert sum(len(item['text']) for item in result) <= max_chars
    for item in result:
        assert item['file'] == 'neutral-procedure.md'
        assert item['revision_id'] == 'revision-A'
        assert item['source_id'] == 'neutral-source'
    return result


def payload(result):
    return '\n\n'.join(item['text'] for item in result)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
@pytest.mark.parametrize('references', (False, True), ids=('plain', 'with-local-reference'))
@pytest.mark.parametrize('max_chars', (36000, 420), ids=('full-section', 'excerpt'))
def test_independent_excluded_paragraph_is_not_sent_with_the_positive_fact(topic, references, max_chars):
    text = '\n\n'.join((topic['primary'], topic['unrelated'], FILLER, LOCAL_QUOTE))
    result = select(topic, text, references=references, max_chars=max_chars)

    assert result and topic['primary'] in payload(result)
    origin = next(item for item in result if item['section_title'] == topic['heading'])
    if max_chars == 420:
        assert origin.get('evidence_excerpt') is True
    if origin['text'] != text:
        # Omitting an excluded source block is an excerpt even when the full
        # original section would have fitted the original character budget.
        assert origin.get('evidence_excerpt') is True
        assert origin.get('evidence_truncated') is True
        native = _content_blocks(text)
        indices = origin['evidence_block_indices']
        assert indices == sorted(set(indices))
        assert 1 not in indices  # The excluded paragraph's ORIGINAL position.
        assert all(native[number] in origin['text'] for number in indices)
        assert {number for number, block in enumerate(native) if block in origin['text']} == set(indices)
    assert origin['excluded_topics'] == [topic['excluded']]
    if references:
        assert any(item.get('reference_from') and item['text'] == TARGET_TEXT for item in result)
    assert topic['unrelated'] not in payload(result)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
@pytest.mark.parametrize('references', (False, True), ids=('plain', 'with-local-reference'))
def test_the_same_source_without_exclusion_keeps_both_native_facts(topic, references):
    text = '\n\n'.join((topic['primary'], topic['unrelated'], LOCAL_QUOTE))
    result = select(topic, text, exclude=False, references=references)
    assert topic['primary'] in payload(result) and topic['unrelated'] in payload(result)
    assert any(item.get('reference_from') for item in result) is references


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
@pytest.mark.parametrize('references', (False, True), ids=('plain', 'with-local-reference'))
def test_mixed_native_condition_is_not_rewritten_to_remove_excluded_words(topic, references):
    text = '\n\n'.join((topic['qualified'], LOCAL_QUOTE))
    result = select(topic, text, references=references)
    assert result and topic['qualified'] in payload(result)
    assert all(block in _content_blocks(text) or block == TARGET_TEXT
               for item in result for block in _content_blocks(item['text']))


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
@pytest.mark.parametrize('references', (False, True), ids=('plain', 'with-local-reference'))
def test_adjacent_native_scope_guard_is_not_treated_as_an_unrelated_excluded_fact(topic, references):
    # No requested discriminator appears in this guard. An unconditional
    # forbidden-word filter would remove an applicability restriction.
    text = '\n\n'.join((topic['primary'], topic['scope_guard'], LOCAL_QUOTE))
    result = select(topic, text, references=references)
    assert topic['primary'] in payload(result) and topic['scope_guard'] in payload(result)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_formula_and_its_native_qualification_are_kept_as_whole_units(topic):
    equation = '$$\nP=A+\n\nB\n$$'
    legend = 'Где P — итог, A и B — исходные значения; изменять знак сложения нельзя.'
    text = '\n\n'.join((FILLER, topic['qualified'], equation, legend, FILLER))
    result = select(topic, text, max_chars=1000)
    actual = payload(result)
    assert topic['qualified'] in actual and equation in actual and legend in actual
    assert actual.count('$$') % 2 == 0


def table_unit(topic):
    headers = ('Предмет', 'Условие применения')
    values = (topic['subject'], topic['qualified'])
    data = {
        'schema': 'table-grid-v1', 'table_id': 'b' * 64, 'row': 1,
        'row_kind': 'body', 'header_status': 'explicit',
        'headers': [{'row': 0, 'column': number, 'span': 1, 'text': text}
                    for number, text in enumerate(headers)],
        'cells': [{'row': 1, 'column': number, 'span': 1, 'text': text,
                   'start': 0, 'end': len(text), 'total_chars': len(text)}
                  for number, text in enumerate(values)],
        'context': [], 'warnings': [],
    }
    unit = serialize_table_unit(data)
    assert parse_table_unit(unit) == data
    return unit


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_table_cell_condition_and_header_binding_remain_literal(topic):
    unit = table_unit(topic)
    text = '\n\n'.join((topic['primary'], unit))
    result = select(topic, text)
    actual = payload(result)
    assert topic['primary'] in actual and unit in actual
    retained = next(block for block in _content_blocks(actual) if block.startswith('[TABLE_ROW_V1]'))
    assert parse_table_unit(retained) == parse_table_unit(unit)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
@pytest.mark.parametrize('visibility', ('empty-acl', 'excluded-source'))
def test_negative_topic_handling_does_not_bypass_source_visibility(topic, visibility):
    options = ({'allowed_sources': set()} if visibility == 'empty-acl' else
               {'excluded_sources': {'neutral-procedure.md'}})
    text = '\n\n'.join((topic['primary'], topic['unrelated']))
    assert select(topic, text, references=True, **options) == []


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_original_query_stays_literal_and_exclusion_is_not_a_positive_search_subject(topic):
    query = question(topic)
    positive, excluded = _positive_query(query)
    assert topic['subject'] in positive and topic['excluded'] not in positive
    assert excluded == [topic['excluded']]
    assert query == question(topic)


SOURCE_FIELDS = ('file', 'section_path', 'document_id', 'source_id', 'revision_id',
                 'version_id', 'document_version', 'content_hash', 'sha256')


def ranked_separate_origins(topic):
    records = [node(topic['heading'], topic['primary']), node(topic['heading'], topic['unrelated'])]
    index = SectionEvidenceIndex(records)
    rows = [{**item, 'evidence_id': f'E{number}'}
            for number, item in enumerate(index.body_items, 1)]
    return index, rows


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_negative_only_reader_preference_cannot_force_a_forbidden_fact_or_claim_retention(topic):
    index, rows = ranked_separate_origins(topic)
    hint = {'evidence_id': rows[1]['evidence_id'], 'quote': rows[1]['text'],
            'source': {key: rows[1][key] for key in SOURCE_FIELDS if key in rows[1]}}
    before = deepcopy((rows, hint))
    metadata = {}
    result = index.select_v2(question(topic), rows, top_k=1, max_chars=36000,
                             preferred_passages=(hint,), selection_meta=metadata,
                             resolve_references=False)
    assert result and topic['primary'] in payload(result)
    assert topic['unrelated'] not in payload(result)
    assert not any(item['selection_signals']['source_reading_preferred'] for item in result)
    assert metadata['preferred_passage_retention'] == {
        'requested_count': 1, 'retained_count': 0, 'unretained_count': 1}
    assert metadata['preferred_passage_retention_incomplete'] is True
    assert (rows, hint) == before


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_negative_origin_is_not_credited_after_a_native_positive_projection(topic):
    index, rows = ranked_separate_origins(topic)
    query = question(topic)
    ranks = new_query_rank_context(query)
    ranks.record(query, rows)
    ranks.record(topic['subject'] + ' проверка', [rows[0]])
    positive_origins, negative_origins = ranks.origins(rows[0]), ranks.origins(rows[1])
    assert positive_origins and negative_origins and not positive_origins & negative_origins
    before = deepcopy(rows)

    result = index.select_v2(query, rows, top_k=1, max_chars=36000,
                             query_rank_context=ranks, resolve_references=False)

    assert result and topic['primary'] in payload(result)
    assert topic['unrelated'] not in payload(result)
    assert any(ranks.origins(item) & positive_origins for item in result)
    assert all(not ranks.origins(item) & negative_origins for item in result)
    assert rows == before


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_atomic_native_packet_conflict_is_kept_literal_and_explicitly_unresolved(topic):
    # The existing formula reader binds its native lead-in to the equation.
    # It is unsafe to delete a lead-in word/paragraph and certify applicability.
    formula = '$$P=A+B$$'
    legend = 'Где P — итог, A и B — исходные значения.'
    text = '\n\n'.join((topic['primary'], topic['unrelated'], formula, legend))
    metadata = {}
    result = select(topic, text, selection_meta=metadata)
    assert all(part in payload(result) for part in (topic['primary'], topic['unrelated'], formula, legend))
    assert any(item['selection_signals']['negative_scope_unresolved'] for item in result)
    assert metadata['negative_scope_unresolved'] is True
    assert 'neutral-procedure.md' not in str(metadata) and 'Руководство' not in str(metadata)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_unfit_atomic_conflict_cannot_be_reintroduced_as_a_naked_formula(topic):
    formula = '$$P=A+B$$'
    legend = 'Где P — итог, A и B — исходные значения.'
    text = '\n\n'.join((topic['primary'], topic['unrelated'], formula, legend))
    metadata = {}
    result = select(topic, text, max_chars=140, selection_meta=metadata)
    assert all(formula not in item['text'] and legend not in item['text'] for item in result)
    assert metadata['negative_scope_unresolved'] is True
    assert metadata['negative_scope_atomic_conflict_incomplete'] is True
    assert 'neutral-procedure.md' not in str(metadata) and 'Руководство' not in str(metadata)


def test_atomic_inventory_packet_keeps_its_native_prelude_and_reports_unresolved_scope():
    prelude = 'Маршрутизация не использует хранение журнала; это условие действует для всех допустимых типов.'
    table = '[TABLE]\nПоле | Описание\nТип | Выбор из списка: Кварц; Олива'
    item = node('Создание маршрутизации', '### Создание маршрутизации\n\n' + prelude + '\n\n' + table)
    index = SectionEvidenceIndex([item])
    query = 'Какие виды маршрутизации; не рассматривай хранение журнала.'
    metadata = {}
    result = index.select_v2(query, index.body_seeds(query, mode='coverage'),
                             resolve_references=False, selection_meta=metadata)
    assert result and prelude in payload(result) and table in payload(result)
    signals = result[0]['selection_signals']
    assert signals['inventory_enumeration_reserved'] is True
    assert signals['negative_scope_checked'] is False
    assert signals['negative_scope_unresolved'] is True
    assert metadata['negative_scope_unresolved'] is True


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
def test_generated_focus_cannot_reclassify_an_excluded_fact_as_the_positive_user_subject(topic):
    text = '\n\n'.join((topic['primary'], topic['unrelated']))
    focus = SearchFocus(SearchOperation.LOOKUP, (SearchSubject(topic['excluded']),))
    result = select(topic, text, search_focus=focus)
    assert result and topic['primary'] in payload(result)
    assert topic['unrelated'] not in payload(result)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic['id'])
@pytest.mark.parametrize('references', (False, True), ids=('plain', 'with-local-reference'))
@pytest.mark.parametrize('max_chars', (36000, 600), ids=('full-section', 'excerpt'))
def test_native_exception_clause_is_retained_as_unresolved_applicability(topic, references, max_chars):
    exception = topic['excluded'].capitalize() + ' являются исключением из данного порядка.'
    text = '\n\n'.join((topic['primary'], exception, FILLER, LOCAL_QUOTE))
    metadata = {}
    result = select(topic, text, references=references, max_chars=max_chars, selection_meta=metadata)
    assert result and topic['primary'] in payload(result)
    assert exception in payload(result)
    assert any(item['selection_signals']['negative_scope_unresolved'] for item in result)
    assert metadata['negative_scope_unresolved'] is True
    assert 'neutral-procedure.md' not in str(metadata) and 'Руководство' not in str(metadata)
    if references:
        assert any(item.get('reference_from') and item['text'] == TARGET_TEXT for item in result)
