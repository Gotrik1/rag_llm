"""Synthetic selection-boundary contracts; no database, model or HTTP calls."""
import ast
from copy import deepcopy
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from corrective_rag import EvidenceSelection, SearchPlan, assess_coverage, generation_prompt, run_corrective
from evidence_anchors import (evidence_anchor_payload, inventory_limits_after_filter,
                              inventory_carrier_signals, normalize_inventory_evidence,
                              normalize_inventory_selection_limits)
from evals import oracle_ablation
from search_focus import SearchFocus
from grounding_verifier import extract_answer_claims


FACT = 'Канал проходит два этапа контроля.'
QUESTION = 'Перечисли этапы контроля канала.'


def fragment(**extra):
    return {'file': 'public.md', 'source': 'Public.md', 'section_path': 'Контроль',
            'status': 'active', 'text': FACT, **extra}


def warning(required=200, available=100, **extra):
    return {'reason': 'atomic-inventory-packet-budget', 'required_chars': required,
            'available_chars': available, **extra}


def limits(required=200, **extra):
    return {'inventory_enumeration_incomplete': True,
            'inventory_enumeration_scoped_warnings': [warning(required, **extra)]}


def carrier(path='Контроль > Полный перечень'):
    return {'inventory_enumeration_incomplete': True,
            'missing_inventory_sections': [{'section_path': path, **warning()}]}


class LocalModel:
    def __init__(self):
        self.calls = []
        self.assessments = 0

    def __call__(self, prompt, purpose):
        self.calls.append((purpose, prompt))
        if purpose == 'planning':
            return json.dumps({'requirements': ['Этапы контроля'], 'queries': [QUESTION]})
        if purpose == 'answer':
            return FACT + ' [E1]'
        if prompt.startswith('Оцени полноту'):
            self.assessments += 1
            payload = json.loads(prompt.splitlines()[-1])
            passage_ids = [passage['passage_id'] for item in payload['evidence']
                           for passage in item['passages'] if passage['text'] == FACT]
            return json.dumps({'coverage': ([{'requirement_id': 'R1', 'passage_ids': passage_ids,
                                             'applicable': True}] if passage_ids else []),
                               'task_alignment': {'status': 'matched' if passage_ids else 'uncertain',
                                                  'passage_ids': passage_ids},
                               'missing_queries': [f'Уточнение этапов {self.assessments}'], 'conflicts': []})
        payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]
        if payload['answer'] != FACT + ' [E1]':
            pytest.fail('Positive inventory fake received an unexpected answer')
        passage_ids = [passage['passage_id'] for item in payload['evidence']
                       if item['evidence_id'] == 'E1'
                       for passage in item['passages'] if passage['text'] == FACT]
        if not passage_ids:
            pytest.fail('Positive inventory fake requires the cited fixture fact')
        return json.dumps({'claims': [{'claim_id': claim['claim_id'], 'support': 'supported',
                                      'passage_ids': passage_ids, 'task_relevance': 'matched'}
                                     for claim in extract_answer_claims(payload['answer'])]})


def run(select=None, rounds=0, **options):
    model = LocalModel()
    result = run_corrective(QUESTION, QUESTION, retrieve=lambda *args, **kwargs: [fragment()],
                            complete=model, select=select, max_additional_rounds=rounds, **options)
    return result, model


@pytest.mark.parametrize('with_flag', [False, True])
def test_known_missing_inventory_cannot_be_certified_by_valid_quote_or_model(with_flag):
    signals = carrier()
    if not with_flag:
        signals.pop('inventory_enumeration_incomplete')
    evidence = [{**fragment(selection_signals=signals), 'evidence_id': 'E1'}]
    model = LocalModel()
    result = assess_coverage(SearchPlan(('Этапы контроля',), (QUESTION,)), evidence, model)
    assert result['assessed'] and result['covered_requirements'] == ['R1']
    assert not result['complete']
    assert any('перечень' in text for text in result['conflicts'])
    payload = evidence_anchor_payload(evidence)[0]['selection_limits']
    assert payload['inventory_enumeration_incomplete']
    assert payload['missing_inventory_sections'][0]['section_path'] == 'Контроль > Полный перечень'
    assert 'missing_inventory_sections' in model.calls[0][1]


def test_source_free_limits_reach_coverage_trace_and_generation_without_fake_evidence():
    def select(*args, **kwargs):
        return EvidenceSelection([fragment()], limits(source='HIDDEN-SOURCE', section_path='HIDDEN-PATH',
                                                     text='HIDDEN-TEXT', title='HIDDEN-TITLE'))
    result, model = run(select)
    assert not result.trace['coverage_complete']
    assert len(result.context) == 1 and 'selection_signals' not in result.context[0]
    assert result.trace['selection_limits'] == limits()
    assert result.trace['rounds'][0]['selection_limits'] == limits()
    serialized = json.dumps(result.trace, ensure_ascii=False)
    assert not any(value in serialized for value in ('HIDDEN-SOURCE', 'HIDDEN-PATH', 'HIDDEN-TEXT', 'HIDDEN-TITLE'))
    for purpose, prompt in model.calls:
        if purpose == 'answer' or prompt.startswith('Оцени полноту'):
            assert 'inventory_enumeration_scoped_warnings' in prompt
            assert not any(value in prompt for value in ('HIDDEN-SOURCE', 'HIDDEN-PATH', 'HIDDEN-TEXT', 'HIDDEN-TITLE'))


def test_changed_source_free_limits_invalidate_assessment_reuse_for_same_text():
    calls = []
    def select(*args, **kwargs):
        calls.append(1)
        return EvidenceSelection([fragment()], limits(100 * len(calls)) if len(calls) < 3 else {})
    result, model = run(select, rounds=2)
    assert model.assessments == 3
    assert [r['assessment_reused'] for r in result.trace['rounds']] == [False, False, False]
    assert result.trace['coverage_complete']
    assert result.trace['selection_limits'] == {}
    assert result.trace['rounds'][0]['selection_limits'] == limits(100)
    assert result.trace['rounds'][1]['selection_limits'] == limits(200)


def test_changed_carrier_missing_list_invalidates_assessment_reuse():
    calls = []
    def select(*args, **kwargs):
        calls.append(1)
        return [fragment(selection_signals=carrier(f'Контроль > Перечень {len(calls)}'))]
    result, model = run(select, rounds=1)
    assert model.assessments == 2
    assert [r['assessment_reused'] for r in result.trace['rounds']] == [False, False]
    assert not result.trace['coverage_complete']
    assert 'Перечень 2' in next(prompt for purpose, prompt in model.calls if purpose == 'answer')


def test_legacy_list_selector_and_unlimited_typed_selection_remain_supported():
    for select in (lambda *args, **kwargs: [fragment()], lambda *args, **kwargs: EvidenceSelection([fragment()])):
        result, model = run(select)
        assert result.trace['coverage_complete'] and model.assessments == 1
        assert result.context[0]['text'] == FACT


def test_independent_requests_do_not_retain_prior_selection_limits():
    first, _ = run(lambda *args, **kwargs: EvidenceSelection([fragment()], limits()))
    second, _ = run(lambda *args, **kwargs: EvidenceSelection([fragment()]))
    assert not first.trace['coverage_complete']
    assert second.trace['coverage_complete'] and second.trace['selection_limits'] == {}


def test_removed_carrier_is_coarsened_not_reattached_to_another_source():
    removed = fragment(file='removed.md', section_path='PRIVATE-LOCATOR', text='x' * 500,
                       selection_signals=carrier('PRIVATE-MISSING-SECTION'))
    kept = fragment(file='other.md')
    cleaned = inventory_limits_after_filter({}, [removed, kept], [kept])
    assert cleaned == limits()
    assert 'PRIVATE' not in json.dumps(cleaned) and 'removed.md' not in json.dumps(cleaned)
    assert 'selection_signals' not in kept
    result, model = run(lambda *args, **kwargs: EvidenceSelection([removed, fragment()]), max_context_chars=100)
    assert len(result.context) == 1 and result.context[0]['file'] == 'public.md'
    assert not result.trace['coverage_complete']
    assert result.trace['selection_limits'] == limits()
    assert 'PRIVATE' not in json.dumps(result.trace)
    assert all('PRIVATE' not in prompt for _, prompt in model.calls)


def test_retained_carrier_keeps_paths_only_in_its_own_anchor():
    selected = [{**fragment(selection_signals=carrier()), 'evidence_id': 'E1'},
                {**fragment(file='other.md'), 'evidence_id': 'E2'}]
    assert inventory_limits_after_filter({}, selected, selected) == {}
    payload = evidence_anchor_payload(selected)
    assert payload[0]['selection_limits']['missing_inventory_sections']
    assert 'selection_limits' not in payload[1]


def test_unknown_warning_reason_cannot_smuggle_source_names():
    unsafe = limits()
    unsafe['inventory_enumeration_scoped_warnings'][0]['reason'] = 'SECRET-FILE.md / SECRET-PATH'
    normalized = normalize_inventory_selection_limits(unsafe)
    assert normalized['inventory_enumeration_incomplete']
    assert 'SECRET' not in json.dumps(normalized)
    assert normalized['inventory_enumeration_scoped_warnings'][0]['reason'] == 'inventory-selection-incomplete'


def test_empty_selection_preserves_coarse_trace_without_generating_evidence():
    result, model = run(lambda *args, **kwargs: EvidenceSelection([], limits()))
    assert result.context == [] and result.trace['selection_limits'] == limits()
    assert not result.trace['coverage_complete']
    assert all(purpose != 'answer' for purpose, _ in model.calls)
    assert 'не поместился целиком' in result.answer
    assert 'Не удалось найти' not in result.answer
    assert '[E' not in result.answer and 'public.md' not in result.answer


def test_agent_select_callback_allocates_fresh_metadata_for_every_invocation(monkeypatch):
    # Execute only the local callback, without importing a service/DB/model client.
    tree = ast.parse(Path(__file__).with_name('rag_agent.py').read_text(encoding='utf-8'))
    outer = next(n for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == 'RAGAgent'
                 for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_ask_corrective')
    callback = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'select')
    snapshots = []
    class Index:
        def select_v2(self, *args, selection_meta, **kwargs):
            snapshots.append(selection_meta)
            assert selection_meta == {}
            if len(snapshots) == 1:
                selection_meta.update(limits())
            return [fragment()]
    namespace = {'self': SimpleNamespace(_section_evidence_index=Index()), 'scope': {},
                 'matching_explicit_sources': lambda *args: set(), 'TOP_K': 10, 'os': os,
                 'filter_scoped_evidence': lambda evidence, *args, **kwargs: evidence,
                 'refine_prism_query': lambda *args: None, 'EvidenceSelection': EvidenceSelection,
                 'SearchFocus': SearchFocus}
    exec(compile(ast.Module(body=[callback], type_ignores=[]), '<agent select callback>', 'exec'), namespace)
    first, second = (namespace['select'](QUESTION, [], [], search_focus=None, constraint_question=QUESTION)
                     for _ in range(2))
    assert snapshots[0] is not snapshots[1]
    assert first.selection_limits == limits() and second.selection_limits == {}


def test_replay_whitelist_supports_scoped_carrier_rows_but_rejects_nested_source_identity():
    row = fragment(selection_signals=carrier())
    normalized = oracle_ablation.normalize_evidence(row, {'public.md': 'Public.md'})
    assert normalized['selection_signals'] == carrier()
    row['selection_signals']['missing_inventory_sections'][0]['file'] = 'private.md'
    with pytest.raises(oracle_ablation.AblationError, match='inventory'):
        oracle_ablation.normalize_evidence(row, {'public.md': 'Public.md'})


def test_replay_arm_carries_only_source_free_limits_without_model_calls():
    settings = {'model': 'synthetic', 'temperature': 0, 'seed': 42,
                'thinking': False, 'max_output_tokens': 0}
    arm = oracle_ablation._arm([fragment()], QUESTION, SearchPlan(('Контроль',), (QUESTION,)),
                               {'public.md': 'Public.md'}, settings, '', 1000, 30000,
                               selection_limits=limits(source='PRIVATE-SOURCE', section_path='PRIVATE-PATH'))
    assert arm['selection_limits'] == limits()
    prompt = arm['request']['messages'][0]['content']
    assert 'inventory_enumeration_scoped_warnings' in prompt
    assert 'PRIVATE' not in prompt


def test_replay_preparation_reads_actual_trace_limits_without_rubric_leak(tmp_path):
    aliases = {'public.md': 'Public.md'}
    docstore = {'docstore/data': {'node': {'__data__': {
        'id_': 'node', 'metadata': {'file_name': 'public.md', 'status': 'active'}, 'text': FACT}}}}
    archive = {'scope': 'ready-global-only', 'runtime': {'model': 'synthetic'},
               'source_manifest': [{'file_name': 'Public.md', 'content_sha256': '1' * 64}],
               'cases': [{'id': 'synthetic', 'question': QUESTION, 'context': [fragment()],
                          'trace': {'plan': {'requirements': ['Контроль'], 'queries': [QUESTION]},
                                    'selection_limits': limits(source='PRIVATE-SOURCE')}}]}
    paths = {name: tmp_path / (name + '.json') for name in ('archive', 'oracle', 'docstore', 'aliases')}
    for name, value in (('archive', archive), ('docstore', docstore), ('aliases', aliases)):
        paths[name].write_text(json.dumps(value), encoding='utf-8')
    oracle = {'scope': 'ready-global-only', 'schema_version': 1,
              'docstore_sha256': oracle_ablation.digest(paths['docstore'].read_bytes()),
              'aliases_sha256': oracle_ablation.digest(paths['aliases'].read_bytes()),
              'cases': [{'id': 'synthetic', 'evidence': [{'source': 'public.md', 'node_id': 'node'}],
                         'required_facts': ['PRIVATE-RUBRIC']}]}
    paths['oracle'].write_text(json.dumps(oracle), encoding='utf-8')
    prepared = oracle_ablation.prepare(*(paths[name] for name in ('archive', 'oracle', 'docstore', 'aliases')),
                                       runtime_id='synthetic-runtime', model_revision='synthetic-revision')
    actual, oracle = (prepared['cases'][0]['arms'][arm] for arm in ('actual', 'oracle'))
    assert actual['selection_limits'] == limits() and oracle['selection_limits'] == {}
    assert 'inventory_enumeration_scoped_warnings' in actual['request']['messages'][0]['content']
    for arm in (actual, oracle):
        assert 'PRIVATE' not in arm['request']['messages'][0]['content']


def many_warnings(count=400):
    return [warning(1000 + index, index % 7,
                    reason='atomic-inventory-packet-budget' if index % 2 == 0 else 'PRIVATE-REASON',
                    file='alpha.md' if index < count // 2 else 'beta.md', section_path='PRIVATE-PATH')
            for index in range(count)]


def many_carrier_rows(count=400):
    return [{'section_path': f'Area > Item {index}', **warning(1000 + index, index % 7)}
            for index in range(count)]


def generation_data(prompt):
    return json.loads(next(line for line in prompt.splitlines() if line.startswith('{"question"')))


def test_400_source_free_warnings_keep_one_real_worst_pair_per_sanitized_reason():
    raw = {'inventory_enumeration_incomplete': False,
           'inventory_enumeration_scoped_warnings': many_warnings()}
    frozen = deepcopy(raw)
    normalized = normalize_inventory_selection_limits(raw)
    assert normalized['inventory_enumeration_incomplete']
    selected = normalized['inventory_enumeration_scoped_warnings']
    assert len(selected) == 2
    for reason in ('atomic-inventory-packet-budget', 'inventory-selection-incomplete'):
        candidates = [row for row in frozen['inventory_enumeration_scoped_warnings']
                      if (row['reason'] == 'atomic-inventory-packet-budget') ==
                      (reason == 'atomic-inventory-packet-budget')]
        worst = max(candidates, key=lambda row: (row['required_chars'] - row['available_chars'],
                                                row['required_chars'], row['available_chars']))
        assert {'reason': reason, 'required_chars': worst['required_chars'],
                'available_chars': worst['available_chars']} in selected
    for _ in range(5):
        assert normalize_inventory_selection_limits(normalized) == normalized
    assert normalize_inventory_selection_limits({**raw, 'inventory_enumeration_scoped_warnings':
                                                 list(reversed(raw['inventory_enumeration_scoped_warnings']))}) == normalized
    assert raw == frozen
    assert len(json.dumps(normalized)) < 400 and 'PRIVATE' not in json.dumps(normalized)


def test_400_carrier_rows_are_bounded_real_locators_without_mutation():
    raw = {'inventory_enumeration_incomplete': True, 'missing_inventory_sections': many_carrier_rows()}
    raw['missing_inventory_sections'].append({'section_path': 'X' * 513, **warning(10**9, 0)})
    frozen = deepcopy(raw)
    normalized = inventory_carrier_signals(raw)
    assert normalized['inventory_enumeration_incomplete']
    selected = normalized['missing_inventory_sections']
    assert len(selected) == 8
    assert all(row in frozen['missing_inventory_sections'] for row in selected)
    assert all(len(row['section_path']) <= 512 for row in selected)
    assert 'X' * 512 not in {row['section_path'] for row in selected}
    for _ in range(5):
        assert inventory_carrier_signals(normalized) == normalized
    assert inventory_carrier_signals({**raw, 'missing_inventory_sections':
                                      list(reversed(raw['missing_inventory_sections']))}) == normalized
    assert raw == frozen


@pytest.mark.parametrize('path_chars,retained', [(512, True), (513, False)])
def test_carrier_path_is_kept_exactly_or_omitted_not_truncated(path_chars, retained):
    signals = carrier('P' * path_chars)
    normalized = inventory_carrier_signals(signals)
    assert normalized['inventory_enumeration_incomplete']
    rows = normalized['missing_inventory_sections']
    assert bool(rows) is retained
    if retained:
        assert rows[0]['section_path'] == signals['missing_inventory_sections'][0]['section_path']
    evidence = [{**fragment(selection_signals=signals), 'evidence_id': 'E1'}]
    assert not assess_coverage(SearchPlan(('Контроль',), (QUESTION,)), evidence, LocalModel())['complete']


def test_generation_caps_raw_carriers_and_hoists_coverage_limits_once_on_copies():
    signals = {'heading_coverage': 0.75, 'ordinary_metadata': ['unchanged'],
               'inventory_enumeration_incomplete': True, 'missing_inventory_sections': many_carrier_rows()}
    evidence = [{**fragment(selection_signals=signals), 'evidence_id': 'E1'}]
    raw_limits = {'inventory_enumeration_incomplete': True,
                  'inventory_enumeration_scoped_warnings': many_warnings()}
    coverage = {'assessed': True, 'complete': False, 'selection_limits': raw_limits}
    frozen = deepcopy((evidence, raw_limits, coverage))
    prompt = generation_prompt(QUESTION, SearchPlan(('Контроль',), (QUESTION,)), evidence, coverage,
                               selection_limits=raw_limits)
    payload = generation_data(prompt)
    assert 'selection_limits' not in payload['coverage']
    assert len(payload['selection_limits']['inventory_enumeration_scoped_warnings']) == 2
    assert len(payload['evidence'][0]['selection_signals']['missing_inventory_sections']) == 8
    assert payload['evidence'][0]['text'] == FACT
    assert payload['evidence'][0]['file'] == 'public.md'
    assert payload['evidence'][0]['selection_signals']['ordinary_metadata'] == ['unchanged']
    assert payload['evidence'][0]['selection_signals']['heading_coverage'] == 0.75
    assert len(prompt) < 12000 and 'PRIVATE' not in prompt
    assert 'representative' in prompt and 'не исчерпывающий' in prompt
    assert (evidence, raw_limits, coverage) == frozen
    assert normalize_inventory_evidence(normalize_inventory_evidence(evidence)) == payload['evidence']
    # A standalone caller that supplies limits only through coverage loses none.
    without_top_level = generation_data(generation_prompt(
        QUESTION, SearchPlan(('Контроль',), (QUESTION,)), evidence, coverage))
    assert without_top_level['selection_limits'] == payload['selection_limits']


def test_assessment_caps_400_carriers_and_warnings_without_approving_completeness():
    evidence = [{**fragment(selection_signals={'missing_inventory_sections': many_carrier_rows()}),
                 'evidence_id': 'E1'}]
    raw_limits = {'inventory_enumeration_scoped_warnings': many_warnings()}
    frozen = deepcopy((evidence, raw_limits))
    model = LocalModel()
    coverage = assess_coverage(SearchPlan(('Контроль',), (QUESTION,)), evidence, model,
                               selection_limits=raw_limits)
    assert coverage['assessed'] and not coverage['complete']
    payload = json.loads(model.calls[0][1].splitlines()[-1])
    assert len(payload['selection_limits']['inventory_enumeration_scoped_warnings']) == 2
    assert len(payload['evidence'][0]['selection_limits']['missing_inventory_sections']) == 8
    assert len(model.calls[0][1]) < 10000
    assert (evidence, raw_limits) == frozen


def test_filter_downgrade_preserves_worst_original_pair_even_when_locator_was_omitted():
    rows = many_carrier_rows()
    rows.append({'section_path': 'PRIVATE-' * 100, **warning(10**9, 123)})
    removed = fragment(file='alpha.md', selection_signals={
        'inventory_enumeration_incomplete': True, 'missing_inventory_sections': rows})
    removed2 = fragment(file='beta.md', selection_signals=carrier('PRIVATE-SECOND'))
    kept = fragment(file='retained.md')
    before = [removed, removed2, kept]
    frozen = deepcopy(before)
    coarse = inventory_limits_after_filter({}, before, [kept])
    assert coarse == {'inventory_enumeration_incomplete': True,
                      'inventory_enumeration_scoped_warnings': [warning(10**9, 123)]}
    # Keep the actual available count from that same source row, not a synthetic
    # pair assembled from the largest required and smallest available values.
    assert coarse['inventory_enumeration_scoped_warnings'] == [warning(10**9, 123)]
    assert 'PRIVATE' not in json.dumps(coarse) and '.md' not in json.dumps(coarse)
    assert inventory_limits_after_filter(coarse, [], []) == coarse
    assert inventory_limits_after_filter({}, [kept], [kept]) == {}
    assert before == frozen and 'selection_signals' not in kept


def test_400_warning_rounds_remain_isolated_after_budget_filter():
    removed = fragment(file='alpha.md', text='x' * 500,
                       selection_signals={'missing_inventory_sections': many_carrier_rows()})
    raw_limits = {'inventory_enumeration_scoped_warnings': many_warnings()}
    frozen = deepcopy((removed, raw_limits))
    invocations = []
    def select(*args, **kwargs):
        invocations.append(1)
        return (EvidenceSelection([removed, fragment()], raw_limits) if len(invocations) == 1
                else EvidenceSelection([fragment()]))
    result, model = run(select, rounds=1, max_context_chars=100)
    assert model.assessments == 2 and result.trace['coverage_complete']
    first, second = result.trace['rounds']
    assert len(first['selection_limits']['inventory_enumeration_scoped_warnings']) == 2
    assert first['selection_limits']['inventory_enumeration_incomplete']
    assert second['selection_limits'] == {} and result.trace['selection_limits'] == {}
    assert [row['assessment_reused'] for row in result.trace['rounds']] == [False, False]
    assert (removed, raw_limits) == frozen


def test_changed_nonrepresentative_carrier_row_still_invalidates_coverage_reuse():
    snapshots = []
    def select(*args, **kwargs):
        rows = many_carrier_rows()
        rows[0]['section_path'] = f'Area > Low priority {len(snapshots)}'
        item = fragment(selection_signals={'missing_inventory_sections': rows})
        snapshots.append(deepcopy(item))
        return [item]
    result, model = run(select, rounds=1)
    assert inventory_carrier_signals(snapshots[0]['selection_signals']) == inventory_carrier_signals(
        snapshots[1]['selection_signals'])
    assert model.assessments == 2
    assert [row['assessment_reused'] for row in result.trace['rounds']] == [False, False]
    assert len(result.context[0]['selection_signals']['missing_inventory_sections']) == 8
    assert len(snapshots[0]['selection_signals']['missing_inventory_sections']) == 400


def test_ten_carriers_share_eight_locator_rows_without_dropping_source_or_other_metadata():
    rows = many_carrier_rows()
    for index, row in enumerate(rows):
        row['section_path'] = 'P' * (512 - len(str(index))) + str(index)
    evidence = [{**fragment(file=f'allowed-{index}.md', source=f'Allowed {index}',
                           selection_signals={'heading_coverage': index / 10,
                                              'inventory_enumeration_incomplete': True,
                                              'missing_inventory_sections': deepcopy(rows)}),
                 'evidence_id': f'E{index + 1}'} for index in range(10)]
    frozen = deepcopy(evidence)
    normalized = normalize_inventory_evidence(evidence)
    assert sum(len(row['selection_signals']['missing_inventory_sections']) for row in normalized) == 8
    assert all(row['selection_signals']['inventory_enumeration_incomplete'] for row in normalized)
    assert all(row['text'] == FACT and row['file'] == frozen[index]['file'] and
               row['source'] == frozen[index]['source'] and
               row['selection_signals']['heading_coverage'] == index / 10
               for index, row in enumerate(normalized))
    assert normalize_inventory_evidence(normalized) == normalized
    payload = evidence_anchor_payload(evidence)
    assert len(payload) == 10
    assert sum(len(row['selection_limits']['missing_inventory_sections']) for row in payload) == 8
    assert all(row['selection_limits']['inventory_enumeration_incomplete'] for row in payload)
    prompt = generation_prompt(QUESTION, SearchPlan(('Контроль',), (QUESTION,)), evidence, {'complete': False})
    generated = generation_data(prompt)
    assert generated['evidence'] == normalized
    assert len(prompt) < 16000
    assert evidence == frozen
