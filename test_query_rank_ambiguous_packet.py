"""A packed pathless fallback cannot bypass an ambiguous native binding."""
from copy import deepcopy

import pytest

from query_rank import QueryRankContext, _NativeWitnessBuilder, native_identity


QUESTION = 'alpha beta gamma'


def source(**metadata):
    return {'file': 'policy.md', 'section_path': 'Policy', 'status': 'active',
            'document_version': 1, 'text': 'A complete independently retrieved native rule.',
            **metadata}


def witness_for(records):
    builder = _NativeWitnessBuilder()
    for record in records:
        builder.add(record)
    return builder.freeze()


def context_for(records):
    context = QueryRankContext(QUESTION, lambda text: frozenset(text.lower().split()))
    context.record(QUESTION, records)
    context.record('alpha beta', records)
    return context


@pytest.mark.parametrize('reverse_index', [False, True])
@pytest.mark.parametrize('located_first', [False, True])
def test_ambiguous_packet_rejection_preserves_exact_located_rows_in_both_orders(
        reverse_index, located_first):
    raw = source()
    located = [source(file_path='/native/policy.md'), source(file_path='/other/policy.md')]
    native = witness_for(list(reversed(located)) if reverse_index else located)
    context = context_for([raw, *located])
    raw_origins = context.origins(raw)
    located_origins = [context.origins(item) for item in located]
    assert raw_origins and all(located_origins)
    assert len(set().union(raw_origins, *located_origins)) == 3
    packed = {**raw, 'selection_reason': 'retrieval-rank'}

    def accept_located():
        for item, origins in zip(located, located_origins):
            context.register_packed(item, origins, native)

    if located_first:
        accept_located()
    context.register_packed(packed, raw_origins, native)
    if not located_first:
        accept_located()

    assert not context.origins(packed)
    assert not context.origins(deepcopy(packed))
    assert not context.retained(packed, raw_origins)
    assert not context.receipt_origins(packed, raw_origins)
    allocator = context.allocator()
    assert allocator.active
    allocator.accept(packed, raw_origins)
    assert not allocator.covered
    for item, origins in zip(located, located_origins):
        assert context.origins(item) == origins
        assert context.retained(item, origins) == origins
        allocator.accept(item, origins)
    assert allocator.covered
    assert native_identity(raw) in context._rows  # Retrieval history is not rewritten.
    assert context.ranks(raw_origins)


def test_only_trusted_ambiguous_packet_is_rejected_not_other_raw_history():
    ambiguous = source()
    ordinary = source(text='An unrelated valid raw result.', section_path='Other policy')
    context = context_for([ambiguous, ordinary])
    ambiguous_origins, ordinary_origins = context.origins(ambiguous), context.origins(ordinary)
    packed = {**ambiguous, 'selection_reason': 'retrieval-rank'}
    context.register_packed(packed, ambiguous_origins, {'ambiguous': True})
    assert context.origins(packed) == ambiguous_origins
    native = witness_for([source(file_path='/native/policy.md'), source(file_path='/other/policy.md')])
    context.register_packed(packed, ambiguous_origins, native)
    assert not context.origins(packed)
    context.register_packed(ordinary, ordinary_origins, native)
    assert context.origins(ordinary) == ordinary_origins
    assert context.retained(ordinary, ordinary_origins) == ordinary_origins
    assert context.ranks(ordinary_origins)


def test_ambiguous_rejection_survives_receipt_budget_exhaustion_and_later_witness(monkeypatch):
    import query_rank
    raw = source()
    context = context_for([raw])
    origins = context.origins(raw)
    native = witness_for([source(file_path='/native/policy.md'), source(file_path='/other/policy.md')])
    monkeypatch.setattr(query_rank, 'MAX_PACK_RECEIPTS', 0)
    context.register_packed(raw, origins, native)
    assert not context.origins(raw)
    single_source = witness_for([source(file_path='/native/policy.md')])
    context.register_packed(raw, origins, single_source)
    assert not context.origins(raw)
    assert not context.native_ancestry(raw, origins, single_source)
    assert not context._packed_receipts
    fresh = context_for([raw])
    assert fresh.origins(raw) == origins  # No rejection leaks into another request.
