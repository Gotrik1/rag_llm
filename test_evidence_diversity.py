"""Pure request-local ranking checks; no similarity result proves facts."""
from copy import deepcopy

from evidence_diversity import priority_multiplier


TEXT = ('Оператор открывает журнал событий оборудования. Сотрудник выбирает запись журнала '
        'и просматривает сведения операции. Экран отображает автора события, название операции, '
        'результат проверки, комментарий исполнителя, состояние задания. Сотрудник завершает просмотр '
        'журнала событий оборудования и возвращается к панели управления.')


def row(text=TEXT, **values):
    return {'file': 'uploads/policy.md', 'revision_id': 'revision-a', 'text': text, **values}


def test_near_copy_is_soft_even_with_extra_nonconditional_sentence():
    candidate = row(TEXT + ' Дополнительные сведения доступны на экране.')
    trace = {}
    result = priority_multiplier(candidate, [row()], trace=trace)
    assert 0 < result < 1
    assert trace['strongest_overlap'] >= .85
    assert trace['semantic_equivalence'] is False
    assert TEXT not in str(trace)


def test_unrelated_payload_and_exact_different_revision_have_no_penalty():
    assert priority_multiplier(row('Маршрутизация пакетов выполняется отдельным сетевым устройством.'), [row()]) == 1
    assert priority_multiplier(row(version_id='new'), [row(version_id='old')]) == 1


def test_cache_is_request_local_and_invalidates_changed_text_and_metadata():
    candidate, selected, cache = row(), [row()], {}
    before = deepcopy((candidate, selected))
    first = priority_multiplier(candidate, selected, cache=cache)
    assert 0 < first < 1 and len(cache) == 1
    assert priority_multiplier(candidate, selected, cache=cache) == first
    assert (candidate, selected) == before
    candidate['text'] += ' Если оборудование остановлено, проверка запрещена.'
    assert priority_multiplier(candidate, selected, cache=cache) == 1
    candidate['text'] = TEXT
    candidate['revision_id'] = 'revision-b'
    assert priority_multiplier(candidate, selected, cache=cache) == 1


def test_file_name_and_nested_metadata_are_supported_without_basename_aliasing():
    candidate = {'text': TEXT, 'metadata': {'file_name': 'uploads/policy.md', 'revision_id': 'revision-a'}}
    assert 0 < priority_multiplier(candidate, [row()]) < 1
    assert priority_multiplier(candidate, [row(file='elsewhere/policy.md')]) == 1


def test_unknown_revision_numeric_metadata_does_not_make_a_revision():
    candidate = {'file': 'policy.md', 'document_id': 'document-a', 'source_id': 'upload-a', 'text': TEXT}
    trace = {}
    assert priority_multiplier(candidate, [deepcopy(candidate)], trace=trace) == 1
    assert trace['reason'] == 'unknown_revision'


def test_paragraph_structure_and_token_order_are_not_flattened():
    reordered = ' '.join(reversed(TEXT.split()))
    assert priority_multiplier(row(reordered), [row()]) == 1
    candidate = row(TEXT.replace('. ', '.\n\n'))
    # Markers remain part of the shingle stream even when no conflict gate is
    # triggered; changing structure is not normalized into an exact repeat.
    trace = {}
    priority_multiplier(candidate, [row()], trace=trace)
    assert trace['strongest_overlap'] < 1


def test_protected_selected_packet_does_not_demote_plain_candidate():
    assert priority_multiplier(row(), [row(reference_target={'locator': '2.1'})]) == 1
    assert priority_multiplier(row(_family_id='native-family'), [row()]) == 1
    assert priority_multiplier(row(_inventory_rows=['native table']), [row()]) == 1
