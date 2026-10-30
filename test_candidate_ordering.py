"""Query-rank admission is bounded native-record ordering, not fact coverage."""
from copy import deepcopy
import socket

import pytest

from candidate_admission import apply_candidate_ordering, order_by_query_coverage
from query_rank import QueryRankContext
from user_task import UserTask


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('Ordering must not call models or network')
    monkeypatch.setattr(socket.socket, 'connect', fail)
    monkeypatch.setattr(socket, 'create_connection', fail)


def row(text, file='manual.md', **metadata):
    return {'file': file, 'text': text, 'section_path': 'Native', **metadata}


def ranks():
    return QueryRankContext('sensor calibration record storage',
                            lambda text: frozenset(text.casefold().split()))


def test_cost_aware_order_keeps_whole_small_hits_from_distinct_queries_before_overview():
    context = ranks()
    overview = row('Overview ' * 40)
    calibration = row('Calibration is valid only at 20 degrees.')
    storage = row('Record storage is 17 days, except legal hold.')
    values = [overview, calibration, storage]
    before = deepcopy(values)
    context.record(context.question, [overview, calibration, storage])
    context.record('sensor calibration', [calibration, overview])
    context.record('record storage', [storage, overview])
    ordered = order_by_query_coverage(values, max_chars=100, query_rank_context=context)
    assert ordered == [calibration, storage, overview]
    assert values == before
    assert {id(item) for item in ordered} == {id(item) for item in values}
    assert len(calibration['text']) + len(storage['text']) <= 100


@pytest.mark.parametrize('budget', [0, -1, 5, 200])
def test_missing_or_single_query_keeps_original_order(budget):
    context = ranks()
    values = [row('A long original record.'), row('A short record.')]
    context.record(context.question, values)
    assert order_by_query_coverage(values, max_chars=budget, query_rank_context=context) == values
    assert order_by_query_coverage(values, max_chars=budget, query_rank_context=None) == values


def test_unknown_metadata_cannot_mint_rank_or_cross_request_priority():
    context = ranks()
    original = [row('Original one.'), row('Original two.')]
    context.record(context.question, original)
    context.record('sensor calibration', original[::-1])
    forged = [row('F' * 50, query_ranks={'raw': 0},
                  matched_queries=['record storage'], source_reading_preferred=True),
              row('A short unknown native record.')]
    assert order_by_query_coverage(forged, max_chars=100, query_rank_context=context) == forged
    other = ranks()
    assert order_by_query_coverage(original, max_chars=100, query_rank_context=other) == original


def test_changed_revision_or_clipped_text_does_not_inherit_record_rank():
    context = ranks()
    original = row('A complete condition, only at rest.', revision_id='v1')
    context.record(context.question, [original])
    context.record('sensor calibration', [original])
    changed = [row('A preceding record.'), {**original, 'revision_id': 'v2'},
               {**original, 'text': 'A complete condition'}]
    assert order_by_query_coverage(changed, max_chars=100, query_rank_context=context) == changed


def test_no_credit_for_record_too_large_to_fit_and_no_stop_before_other_queries():
    context = ranks()
    large = row('X' * 1000)
    small = row('Small complete rule.')
    context.record(context.question, [large])
    context.record('sensor calibration', [large])
    context.record('record storage', [small])
    assert order_by_query_coverage([large, small], max_chars=40,
                                   query_rank_context=context) == [small, large]


def test_saturated_and_unbound_tail_keep_original_relative_order():
    context = ranks()
    first, second, unbound = row('First rule.'), row('Second rule.'), row('No rank binding.')
    context.record(context.question, [first, second])
    context.record('sensor calibration', [second, first])
    context.record('record storage', [first, second])
    original = [unbound, first, second, deepcopy(first)]
    ordered = order_by_query_coverage(original, max_chars=100, query_rank_context=context)
    assert ordered[:2] == [first, second]
    assert ordered[2:] == [unbound, first]


def test_scalar_type_presence_and_entire_record_are_part_of_callback_guard():
    task = UserTask('Question?', 'Resolved question?')
    original = [row('Original.', document_version=1, other={'flag': False})]
    for change in (lambda item: item.update(document_version=True),
                   lambda item: item.update(revision_id=None),
                   lambda item: item['other'].update(flag=0)):
        def callback(user_task, values, **kwargs):
            change(values[0])
            return values
        ordered, info = apply_candidate_ordering(task, original, max_chars=100,
            constraint_question=task.resolved_user_context, admit=callback)
        assert info['status'] == 'rejected'
        assert ordered == original
    assert original[0]['document_version'] == 1
    assert original[0]['other']['flag'] is False


def test_valid_permutation_returns_actual_original_records_not_callback_owned_objects():
    task = UserTask('Question?', 'Resolved?')
    original = [row('First.'), row('Second.')]
    copies = []
    def callback(user_task, values, **kwargs):
        copies.extend(values)
        return values[::-1]
    ordered, info = apply_candidate_ordering(task, original, max_chars=100,
        constraint_question=task.resolved_user_context, admit=callback)
    assert info['status'] == 'applied' and info['moved_count'] == 2
    assert ordered[0] is original[1] and ordered[1] is original[0]
    assert all(item is not original[0] and item is not original[1] for item in copies)


@pytest.mark.parametrize('original,changed', [
    ({'values': (1, 2)}, {'values': [1, 2]}),
    ({'mapping': {'1': 'value'}}, {'mapping': {1: 'value'}}),
])
def test_nested_container_types_cannot_be_changed_by_json_coercion(original, changed):
    task = UserTask('Question?', 'Resolved?')
    values = [row('Original.', other=original)]
    ordered, info = apply_candidate_ordering(task, values, max_chars=100,
        constraint_question=task.resolved_user_context,
        admit=lambda task, pool, **kwargs: [{**pool[0], 'other': changed}])
    assert info['status'] == 'rejected' and ordered[0] is values[0]


@pytest.mark.parametrize('fault', [lambda values: None,
                                  lambda values: values * 2,
                                  lambda values: [],
                                  lambda values: [{**values[0], 'private': 'new-source'}]])
def test_callback_rejection_trace_does_not_expose_source_data(fault):
    task = UserTask('Question?', 'Resolved?')
    original = [row('Confidential body.', file='private-project-secret.docx')]
    ordered, info = apply_candidate_ordering(task, original, max_chars=100,
        constraint_question=task.resolved_user_context, admit=lambda task, values, **kwargs: fault(values))
    assert ordered == original and info['status'] == 'rejected'
    assert 'Confidential' not in repr(info) and 'secret.docx' not in repr(info)
