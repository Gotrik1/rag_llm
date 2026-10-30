"""Reader hints must survive native reassembly and the existing fair budget."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from corrective_rag import assign_evidence
from evidence_selection import SectionEvidenceIndex


QUESTION = 'Sensor inspection interval'
RULE = 'Period: 12 hours after shutdown.'
OVERVIEW = ('Sensor inspection interval is documented in the operational '
            'instructions and registered in the control register.')
SOURCE_FIELDS = ('file', 'section_path', 'document_id', 'source_id', 'revision_id',
                 'version_id', 'document_version', 'content_hash', 'sha256')


def make_node(text, path='Sensor inspection', **metadata):
    return SimpleNamespace(text=text, metadata={
        'file_name': 'policy.md', 'section_path': path, 'section_title': path,
        'status': 'active', 'revision_id': 'r1', **metadata})


def prepare(nodes):
    rows = assign_evidence([
        {'file': node.metadata['file_name'], 'text': node.text,
         **{key: value for key, value in node.metadata.items() if key != 'file_name'}}
        for node in nodes], max_chars=24000)
    return SectionEvidenceIndex(nodes), rows


def preference(row, quote=None):
    return {'evidence_id': row['evidence_id'], 'quote': row['text'] if quote is None else quote,
            'source': {key: row[key] for key in SOURCE_FIELDS if key in row}}


def select(index, rows, hints, *, max_chars=12000, top_k=10, selection_meta=None):
    return index.select_v2(QUESTION, rows, top_k=top_k, max_chars=max_chars,
        allowed_sources={'policy.md'}, preferred_passages=hints,
        selection_meta=selection_meta, resolve_references=False)


def test_second_legitimate_origin_id_can_prefer_the_same_reassembled_native_section():
    index, rows = prepare([make_node(OVERVIEW), make_node(RULE)])
    hints = (preference(rows[1]),)
    before = deepcopy((rows, hints))
    result = select(index, rows, hints, top_k=1)
    assert len(result) == 1 and RULE in result[0]['text']
    assert result[0]['selection_signals']['source_reading_preferred'] is True
    assert (rows, hints) == before


def test_16k_pool_retains_late_reader_quote_within_existing_12k_fair_budget():
    nodes = [make_node((OVERVIEW + '\n\n') * 15), make_node(RULE)]
    nodes += [make_node(
        f'Sensor inspection interval background information for control {number}. '
        + 'General documentation and registration. ' * 38,
        f'Sensor inspection background {number}') for number in range(9)]
    index, rows = prepare(nodes)
    assert len(rows) == 11 and 15000 < sum(len(row['text']) for row in rows) < 24000
    metadata = {}
    result = select(index, rows, (preference(rows[1]),), selection_meta=metadata)
    primary = next(row for row in result if row['section_path'] == 'Sensor inspection')
    assert RULE in primary['text']
    assert primary['selection_signals']['source_reading_preferred'] is True
    assert len(primary['text']) <= primary['selection_signals']['fair_budget_chars']
    assert sum(len(row['text']) for row in result) <= 12000 and len(result) <= 10
    assert any('background' in row['section_path'] for row in result)
    assert metadata['preferred_passage_retention'] == {
        'requested_count': 1, 'retained_count': 1, 'unretained_count': 0}


def test_hint_reserves_entire_native_paragraph_and_neighboring_applicability():
    condition = 'Только после остановки датчика и получения допуска.'
    rule = 'Интервал составляет 12 часов; журнал осмотра сохраняют без изменений.'
    index, rows = prepare([make_node((OVERVIEW + '\n\n') * 8),
                           make_node(condition + '\n\n' + rule)])
    result = select(index, rows, (preference(rows[1], rule),), max_chars=260, top_k=1)
    assert len(result) == 1
    assert condition in result[0]['text'] and rule in result[0]['text']
    assert result[0]['selection_signals']['source_reading_preferred'] is True
    assert len(result[0]['text']) <= 260


def test_unfit_native_paragraph_is_not_carved_around_a_short_hinted_substring():
    long_rule = 'Period: 12 hours after shutdown. ' + 'Keep the original operating conditions. ' * 12
    index, rows = prepare([make_node(OVERVIEW), make_node(long_rule)])
    metadata = {}
    result = select(index, rows, (preference(rows[1], 'Period: 12 hours after shutdown.'),),
                    max_chars=170, top_k=1, selection_meta=metadata)
    assert all('Period: 12 hours after shutdown.' not in row['text'] or long_rule in row['text']
               for row in result)
    assert not any(row['selection_signals']['source_reading_preferred'] for row in result)
    assert sum(len(row['text']) for row in result) <= 170
    assert metadata['preferred_passage_retention_incomplete'] is True
    assert metadata['preferred_passage_retention'] == {
        'requested_count': 1, 'retained_count': 0, 'unretained_count': 1}


def test_both_legitimate_origin_ids_survive_reassembly_without_exposing_internal_sets():
    index, rows = prepare([make_node(OVERVIEW), make_node(RULE)])
    metadata = {}
    result = select(index, rows, tuple(preference(row) for row in rows),
                    top_k=1, selection_meta=metadata)
    assert len(result) == 1 and all(row['text'] in result[0]['text'] for row in rows)
    assert metadata['preferred_passage_retention'] == {
        'requested_count': 2, 'retained_count': 2, 'unretained_count': 0}
    assert not any(key.startswith('_reader_') for key in result[0])


def test_unfit_applicability_packet_never_publishes_a_naked_hinted_rule():
    condition = 'Только после остановки датчика ' + 'и проверки допуска ' * 12 + '.'
    index, rows = prepare([make_node(OVERVIEW), make_node(condition + '\n\n' + RULE)])
    metadata = {}
    result = select(index, rows, (preference(rows[1], RULE),), max_chars=170,
                    top_k=1, selection_meta=metadata)
    assert all(RULE not in row['text'] or condition in row['text'] for row in result)
    assert not any(row['selection_signals']['source_reading_preferred'] for row in result)
    assert metadata['preferred_passage_retention_incomplete'] is True
    assert sum(len(row['text']) for row in result) <= 170


@pytest.mark.parametrize('context', [
    'Для аварийного режима. ' + 'Допуск должен быть подтвержден до начала осмотра. ' * 6,
    'Context Zeta. ' + 'This origin record applies to the maintenance scenario only. ' * 6,
])
def test_unfit_origin_record_cannot_lose_context_outside_the_guard_vocabulary(context):
    index, rows = prepare([make_node((OVERVIEW + '\n\n') * 8),
                           make_node(context + '\n\n' + RULE)])
    if context.startswith('Для'):
        assert len(context) == 323  # Original review counterexample, not a new threshold.
    metadata = {}
    result = select(index, rows, (preference(rows[1], RULE),), max_chars=260,
                    top_k=1, selection_meta=metadata)
    assert all(RULE not in row['text'] or context in row['text'] for row in result)
    assert not any(row['selection_signals']['source_reading_preferred'] for row in result)
    assert metadata['preferred_passage_retention_incomplete'] is True
    assert metadata['preferred_passage_retention'] == {
        'requested_count': 1, 'retained_count': 0, 'unretained_count': 1}
    assert sum(len(row['text']) for row in result) <= 260


def test_fit_origin_context_packet_is_retained_without_lexical_condition_recognition():
    context = 'Context Zeta: maintenance scenario only.'
    index, rows = prepare([make_node((OVERVIEW + '\n\n') * 8),
                           make_node(context + '\n\n' + RULE)])
    metadata = {}
    result = select(index, rows, (preference(rows[1], RULE),), max_chars=260,
                    top_k=1, selection_meta=metadata)
    assert len(result) == 1 and context in result[0]['text'] and RULE in result[0]['text']
    assert result[0]['selection_signals']['source_reading_preferred'] is True
    assert metadata['preferred_passage_retention']['retained_count'] == 1


def test_selected_native_math_keeps_existing_applicability_and_where_blocks():
    condition = 'При остановленном датчике и наличии допуска.'
    formula = '$$T = 12 + D$$'
    definition = 'Где D — длительность контрольной проверки.'
    index, rows = prepare([make_node((OVERVIEW + '\n\n') * 8),
                           make_node('\n\n'.join((condition, formula, definition)))])
    result = select(index, rows, (preference(rows[1], formula),), max_chars=260, top_k=1)
    assert len(result) == 1
    assert all(part in result[0]['text'] for part in (condition, formula, definition))
    assert result[0]['selection_signals']['source_reading_preferred'] is True


def test_service_marker_mapping_keeps_raw_hint_and_index_bound_native_bytes():
    index, rows = prepare([make_node('[SECTION: Sensor inspection]\n' + OVERVIEW),
                           make_node('[SECTION: Sensor inspection]\n' + RULE)])
    metadata = {}
    hints = (preference(rows[1]),)
    before = deepcopy((rows, hints, index.body_items))
    result = select(index, rows, hints, top_k=1, selection_meta=metadata)
    assert result and RULE in result[0]['text']
    assert result[0]['selection_signals']['source_reading_preferred'] is True
    assert 'preferred_passage_retention_incomplete' not in metadata
    assert metadata['preferred_passage_retention']['retained_count'] == 1
    assert hints[0]['quote'] == '[SECTION: Sensor inspection]\n' + RULE
    assert hints[0]['quote'] not in result[0]['text']
    assert result[0]['file'] == 'policy.md' and result[0]['revision_id'] == 'r1'
    assert (rows, hints, index.body_items) == before


@pytest.mark.parametrize('field', SOURCE_FIELDS[2:])
def test_each_revision_identity_field_remains_an_exact_hint_binding(field):
    identity = {key: 'native-' + key for key in SOURCE_FIELDS[2:]}
    index, rows = prepare([make_node(OVERVIEW, **identity), make_node(RULE, **identity)])
    hint = preference(rows[1])
    hint['source'][field] = 'different-' + field
    result = select(index, rows, (hint,), top_k=1)
    assert result and not any(row['selection_signals']['source_reading_preferred'] for row in result)


@pytest.mark.parametrize('mutation', ['id', 'revision', 'source', 'quote', 'forged-origin'])
def test_foreign_or_forged_hint_never_gains_a_native_origin(mutation):
    index, rows = prepare([make_node(OVERVIEW), make_node(RULE)])
    hint = preference(rows[1])
    if mutation == 'id':
        hint['evidence_id'] = 'E999'
    elif mutation == 'revision':
        hint['source']['revision_id'] = 'r2'
    elif mutation == 'source':
        hint['source']['file'] = 'foreign.md'
    elif mutation == 'quote':
        hint['quote'] = OVERVIEW  # Same reconstructed section, but not this origin's text.
    else:
        hint['evidence_id'] = 'E999'
        for row in rows:
            row['_reader_origin_ids'] = {'E999'}
            row['_reader_preferences'] = (hint,)
            row['_reader_contexts'] = {('E999', RULE): {RULE}}
    result = select(index, rows, (hint,), top_k=1)
    assert result and not any(row['selection_signals']['source_reading_preferred'] for row in result)
    assert not any(key.startswith('_reader_') for row in result for key in row)


def test_no_hints_leaves_native_packing_and_selection_metadata_unchanged():
    index, rows = prepare([make_node((OVERVIEW + '\n\n') * 15), make_node(RULE)])
    metadata = {}
    result = select(index, rows, (), max_chars=170, top_k=1, selection_meta=metadata)
    assert RULE not in result[0]['text']
    assert result[0]['selection_signals']['source_reading_preferred'] is False
    assert 'preferred_passage_retention' not in metadata
