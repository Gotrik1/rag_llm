"""Admission and prepack diagnostics must describe final observed transport.

Controlled fake verdicts below do not establish answer quality or entailment.
"""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from corrective_rag import (EvidenceSelection, assign_evidence, preference_retention_limits,
                            run_corrective)
from test_prepack_source_contract import (Pipeline, QUESTION, FACTS, evidence, reader_data,
                                         prompt_data)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('Truthfulness contract tests must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', fail)
    monkeypatch.setattr(socket, 'create_connection', fail)


def test_admission_counts_explain_first_fit_loss_without_changing_selection_policy():
    overview = evidence('O' * 23980)
    rule = evidence('R' * 47, path='Rule')
    before = deepcopy([overview, rule])
    meta = {}
    result = assign_evidence([overview, rule], max_chars=24000, admission_meta=meta)
    assert [row['text'] for row in result] == [overview['text']]
    assert meta['input_count'] == meta['ready_count'] == 2
    assert meta['admitted_count'] == 1 and meta['admitted_chars'] == 23980
    assert meta['budget_dropped_count'] == 1 and meta['budget_dropped_chars'] == 47
    assert meta['budget_dropped_sample'] == [
        {'text_sha256': hashlib.sha256(rule['text'].encode()).hexdigest(), 'chars': 47}]
    assert meta['deduplicated_count'] == meta['deleted_count'] == meta['empty_count'] == 0
    assert [overview, rule] == before


def test_admission_partitions_attempts_and_resets_the_supplied_trace_without_source_hints():
    first = evidence('alpha', file='PRIVATE_FILE_MUST_NOT_LEAK.md')
    values = [first, deepcopy(first), evidence('', status='deleted'),
              evidence('   '), evidence('b' * 10, path='Private location')]
    meta = {'stale_private_trace': 'must disappear'}
    result = assign_evidence(values, max_chars=8, admission_meta=meta)
    assert [row['text'] for row in result] == ['alpha']
    assert meta['input_count'] == 5
    assert meta['ready_count'] == 3
    assert meta['admitted_count'] == meta['deduplicated_count'] == meta['budget_dropped_count'] == 1
    assert meta['deleted_count'] == meta['empty_count'] == 1
    assert meta['admitted_chars'] == 5 and meta['budget_dropped_chars'] == 10
    assert meta['ready_count'] == (meta['admitted_count'] + meta['deduplicated_count']
                                   + meta['budget_dropped_count'])
    serialized = json.dumps(meta)
    assert 'stale_private_trace' not in meta
    assert all(value not in serialized for value in ('alpha', 'PRIVATE_FILE', 'Private location', 'E31'))


def test_deleted_priority_and_late_small_admission_keep_existing_first_fit_semantics():
    first = evidence('abcde')
    values = [evidence('x' * 11), first, {**first, 'status': 'deleted'},
              evidence('', status='deleted')]
    meta = {}
    with_meta = assign_evidence(values, max_chars=10, admission_meta=meta)
    without_meta = assign_evidence(values, max_chars=10)
    assert with_meta == without_meta
    assert [row['text'] for row in with_meta] == ['abcde']
    assert meta['deleted_count'] == 2 and meta['empty_count'] == 0
    assert meta['deduplicated_count'] == 0
    assert meta['budget_dropped_count'] == 1


def test_budget_drop_sample_is_bounded_unique_hashes_not_a_source_or_text_dump():
    texts = ['same oversized payload'] * 3 + [f'oversized payload {number}' for number in range(20)]
    values = [evidence(text, path=f'PRIVATE_LOCATION_{number}') for number, text in enumerate(texts)]
    meta = {}
    assert assign_evidence(values, max_chars=0, admission_meta=meta) == []
    assert meta['budget_dropped_count'] == len(values)
    assert meta['budget_dropped_chars'] == sum(map(len, texts))
    sample = meta['budget_dropped_sample']
    assert len(sample) == 8 and len({row['text_sha256'] for row in sample}) == 8
    expected = {hashlib.sha256(text.encode()).hexdigest(): len(text) for text in texts}
    assert all(set(row) == {'text_sha256', 'chars'} and expected[row['text_sha256']] == row['chars']
               for row in sample)
    assert 'PRIVATE_LOCATION' not in json.dumps(meta)
    assert 'oversized payload' not in json.dumps(meta)


class TruthPipeline(Pipeline):
    def __init__(self, mode='both', **kwargs):
        values = [evidence(FACTS[0], 'E31', 'Native'),
                  evidence(FACTS[1], 'E32', 'Native'),
                  evidence('Служебная запись содержит описание другого этапа.', 'E33', 'Other')]
        super().__init__([values], **kwargs)
        self.mode = mode

    def complete(self, prompt, purpose):
        if purpose != 'source-reading':
            return super().complete(prompt, purpose)
        self.calls.append((purpose, prompt))
        data = reader_data(prompt)
        self.reader_inputs.append(data)
        cards = []
        for number, row in enumerate(data['evidence'][:2], 1):
            cards.append({'id': f'C{number}', 'kind': 'rule',
                'passage_ids': [p['passage_id'] for p in row['passages']],
                'related_card_ids': [], 'requirement_ids': ['R1']})
        return json.dumps({'snapshot_id': data['snapshot_id'], 'cards': cards,
                           'unresolved_requirement_ids': []}, ensure_ascii=False)

    def select(self, question, pool, requirements, **kwargs):
        self.select_calls.append((deepcopy(pool), deepcopy(kwargs)))
        if self.mode == 'ignore':
            return pool[-1:]
        if self.mode == 'unrelated_flag':
            chosen = deepcopy([pool[0], pool[-1]])
            chosen[-1]['selection_signals'] = {'source_reading_preferred': True}
            return chosen
        chosen = deepcopy(pool[:1] if self.mode in {'one', 'changed_condition', 'changed_revision'} else pool[:2])
        if self.mode == 'aggregate':
            chosen = [{**chosen[0], 'text': '\n\n'.join(row['text'] for row in chosen)}]
        if self.mode == 'changed_condition':
            chosen[0]['text'] = chosen[0]['text'].replace('после остановки', 'при работе')
        if self.mode == 'changed_revision':
            chosen[0]['revision_id'] = 'r2'
        if self.mode != 'no_native_flag':
            for row in chosen:
                row['selection_signals'] = {'source_reading_preferred': True}
        return chosen


def prepack_trace(result):
    return result.trace['source_reading']


def test_valid_hints_ignored_by_a_custom_selector_are_not_reported_as_applied():
    result = TruthPipeline('ignore').run(prepack_source_reading=True, read_before_answer=True)
    trace = prepack_trace(result)
    assert trace['applied'] is False
    assert trace['validated_hint_count'] == 2
    assert trace['retained_preferred_passage_count'] == 0
    assert trace['native_packer_preferred_evidence_count'] == 0
    assert trace['all_preferred_passages_retained'] is False


def test_unrelated_native_flag_and_an_unflagged_retained_quote_do_not_jointly_prove_application():
    result = TruthPipeline('unrelated_flag').run(prepack_source_reading=True, read_before_answer=True)
    trace = prepack_trace(result)
    assert trace['validated_hint_count'] == 2
    assert trace['retained_preferred_passage_count'] == 1
    assert trace['native_packer_preferred_evidence_count'] == 1
    assert trace['applied'] is False
    assert trace['all_preferred_passages_retained'] is False


@pytest.mark.parametrize('flag', [None, 0, 1, 'true', 'false', []])
def test_a_non_boolean_native_flag_is_not_counted_as_native_application(flag):
    class MalformedFlag(TruthPipeline):
        def select(self, *args, **kwargs):
            chosen = super().select(*args, **kwargs)
            for row in chosen:
                row['selection_signals'] = {'source_reading_preferred': flag}
            return chosen
    result = MalformedFlag('no_native_flag').run(prepack_source_reading=True, read_before_answer=True)
    trace = prepack_trace(result)
    assert trace['retained_preferred_passage_count'] == 2
    assert trace['native_packer_preferred_evidence_count'] == 0
    assert trace['applied'] is False


@pytest.mark.parametrize(('mode', 'retained', 'native', 'applied', 'complete'), [
    ('one', 1, 1, True, False),
    ('both', 2, 2, True, True),
    ('aggregate', 2, 1, True, True),
    ('no_native_flag', 2, 0, False, True),
    ('changed_condition', 0, 1, False, False),
    ('changed_revision', 0, 1, False, False),
])
def test_prepack_applied_and_retention_counts_use_final_exact_native_evidence(
        mode, retained, native, applied, complete):
    model = TruthPipeline(mode)
    result = model.run(prepack_source_reading=True, read_before_answer=True)
    trace = prepack_trace(result)
    assert trace['validated_hint_count'] == 2
    assert trace['retained_preferred_passage_count'] == retained
    assert trace['native_packer_preferred_evidence_count'] == native
    assert trace['applied'] is applied
    assert trace['all_preferred_passages_retained'] is complete
    if mode == 'aggregate':
        assert result.context[0]['evidence_id'] not in {
            hint['evidence_id'] for hint in model.select_calls[0][1]['preferred_passages']}


def test_candidate_admission_trace_is_recorded_before_the_reader_and_final_context_budget():
    model = TruthPipeline('both')
    result = run_corrective(QUESTION, QUESTION, retrieve=model.retrieve, complete=model.complete,
        select=model.select, max_additional_rounds=0, max_context_chars=12000,
        prepack_source_reading=True, read_before_answer=True)
    admission = result.trace['rounds'][0]['retrieval']['candidate_admission']
    assert admission['input_count'] == admission['ready_count'] == admission['admitted_count'] == 3
    assert admission['admitted_chars'] == sum(len(row['text']) for row in model.select_calls[0][0])
    assert admission['budget_dropped_count'] == 0
    assert len(model.reader_inputs[0]['evidence']) == 3


def test_runtime_candidate_admission_uses_twice_final_budget_and_reports_the_lost_rule():
    model = TruthPipeline('both')
    model.batches = [[evidence('O' * 23980), evidence('R' * 47, path='Rule')]]
    result = run_corrective(QUESTION, QUESTION, retrieve=model.retrieve, complete=model.complete,
        select=model.select, max_additional_rounds=0, max_context_chars=12000,
        prepack_source_reading=True, read_before_answer=True)
    admission = result.trace['rounds'][0]['retrieval']['candidate_admission']
    assert admission['ready_count'] == 2
    assert admission['admitted_count'] == 1 and admission['admitted_chars'] == 23980
    assert admission['budget_dropped_count'] == 1 and admission['budget_dropped_chars'] == 47
    assert len(model.reader_inputs[0]['evidence']) == 1
    assert model.reader_inputs[0]['evidence'][0]['passages'][0]['text'] == 'O' * 23980


def test_retention_counts_are_measured_after_the_final_assign_budget_drop():
    model = TruthPipeline('both')
    budget = max(map(len, FACTS))
    result = run_corrective(QUESTION, QUESTION, retrieve=model.retrieve, complete=model.complete,
        select=model.select, max_additional_rounds=0, max_context_chars=budget,
        prepack_source_reading=True, read_before_answer=True)
    assert len(model.select_calls[0][0]) == 2
    assert len(result.context) == 1
    trace = prepack_trace(result)
    assert trace['validated_hint_count'] == 2
    assert trace['retained_preferred_passage_count'] == 1
    assert trace['native_packer_preferred_evidence_count'] == 1
    assert trace['applied'] is True and trace['all_preferred_passages_retained'] is False


@pytest.mark.parametrize('changed_pool', [False, True])
def test_recovery_clears_all_current_round_hint_and_application_counts(changed_pool):
    model = TruthPipeline('both', first_coverage='mismatch')
    if changed_pool:
        model.batches.append([evidence(FACTS[0].replace('12 часов', '24 часа'), path='Native')])
    result = model.run(prepack_source_reading=True, read_before_answer=True)
    assert len(result.trace['rounds']) == 2
    first = result.trace['rounds'][0]['retrieval']['prepack_source_reading']
    second = result.trace['rounds'][1]['retrieval']['prepack_source_reading']
    assert first['validated_hint_count'] == 2
    assert second['validated_hint_count'] == 0
    assert second['retained_preferred_passage_count'] == 0
    assert second['native_packer_preferred_evidence_count'] == 0
    assert second['applied'] is False
    assert second['all_preferred_passages_retained'] is False
    assert prepack_trace(result)['validated_hint_count'] == 0
    assert [purpose for purpose, _ in model.calls].count('source-reading') == 1


def test_native_retention_limits_copy_only_consistent_counts_and_derive_incomplete():
    raw = {'preferred_passage_retention': {'requested_count': 2, 'retained_count': 2,
                                         'unretained_count': 0},
           'preferred_passage_retention_incomplete': True,
           'file': 'PRIVATE_SOURCE', 'quote': 'PRIVATE_QUOTE'}
    before = deepcopy(raw)
    result = preference_retention_limits(raw)
    assert result == {'preferred_passage_retention': {'requested_count': 2, 'retained_count': 2,
                                                      'unretained_count': 0},
                      'preferred_passage_retention_incomplete': False}
    result['preferred_passage_retention']['retained_count'] = 0
    assert raw == before


@pytest.mark.parametrize('counts', [
    None, [], {}, {'requested_count': 1, 'retained_count': 1},
    {'requested_count': 1, 'retained_count': 0, 'unretained_count': 0},
    {'requested_count': True, 'retained_count': 1, 'unretained_count': 0},
    {'requested_count': 1.0, 'retained_count': 1, 'unretained_count': 0},
    {'requested_count': '1', 'retained_count': 1, 'unretained_count': 0},
    {'requested_count': 0, 'retained_count': 1, 'unretained_count': -1},
    {'requested_count': 1025, 'retained_count': 1025, 'unretained_count': 0},
    {'requested_count': 1, 'retained_count': 1, 'unretained_count': 0, 'source': 'PRIVATE_SOURCE'},
])
def test_malformed_or_source_bearing_native_count_rows_are_not_coerced(counts):
    assert preference_retention_limits({'preferred_passage_retention': counts}) == {}


def test_native_retention_diagnostics_cannot_become_arbitrary_coverage_metadata():
    class Limited(TruthPipeline):
        def select(self, *args, **kwargs):
            rows = super().select(*args, **kwargs)
            return EvidenceSelection(rows, {
                'preferred_passage_retention': {'requested_count': 2, 'retained_count': 1,
                                                'unretained_count': 1},
                'source': 'PRIVATE_SOURCE_MUST_NOT_LEAK',
                'quote': 'PRIVATE_QUOTE_MUST_NOT_LEAK',
            })
    model = Limited('one')
    result = model.run(prepack_source_reading=True, read_before_answer=True)
    native = result.trace['rounds'][0]['retrieval']['native_preferred_passage_retention']
    assert set(native) == {'preferred_passage_retention', 'preferred_passage_retention_incomplete'}
    assert native['preferred_passage_retention']['requested_count'] == 2
    assert native['preferred_passage_retention_incomplete'] is True
    assert 'PRIVATE_SOURCE' not in json.dumps(result.trace)
    assert 'PRIVATE_QUOTE' not in json.dumps(result.trace)
    for purpose, prompt in model.calls:
        if purpose == 'evidence-review' and prompt.startswith('Оцени полноту'):
            data = prompt_data(prompt, '{"requirements":')
            assert 'preferred_passage_retention' not in data.get('selection_limits', {})
            assert 'PRIVATE_SOURCE' not in prompt and 'PRIVATE_QUOTE' not in prompt
