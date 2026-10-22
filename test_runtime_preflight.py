"""Pure runtime gates use synthetic inputs only; no corpus or model access."""
from copy import deepcopy
import hashlib
import json

import pytest

from evals.runtime_preflight import (EMBEDDING_SERVICE, PRIVATE_BRIDGE, PreflightError,
    validate_aliases, validate_effective_runtime, validate_model_identity, validate_qa_paths,
    validate_fresh_runtime, validate_runtime_flags, validate_runtime_report, validate_vector_readiness,
    verify_docstore_snapshot)


def flags(**updates):
    value = {'RAG_CORRECTIVE_PIPELINE': 'on', 'RAG_REFERENCE_EXPANSION': 'on',
        'RAG_SOURCE_READING': 'on', 'RAG_TASK_SCOPE_CHECK': 'on', 'RAG_RERANKER_MODE': 'off',
        'RAG_PLANNER_THINKING': 'false', 'RAG_ALLOW_PLANNER_THINKING': 'false',
        'RAG_MAX_OUTPUT_TOKENS': '-1', 'RAG_EVIDENCE_MAX_CHARS': '36000',
        'RAG_PLANNER_TOKEN_BUDGET': '4096', 'RAG_PLANNER_TIMEOUT_SECONDS': '120',
        'QA_EXPECTED_CONTEXT_WINDOW': '32768', 'QDRANT_URL': 'http://qdrant:6333',
        'QDRANT_COLLECTION': 'synthetic_sections', 'EMBED_MODEL': 'synthetic-embed'}
    return {**value, **updates}


def environment(**updates):
    return {**flags(), 'OLLAMA_BASE_URL': PRIVATE_BRIDGE,
            'QA_EMBEDDING_BASE_URL': EMBEDDING_SERVICE, **updates}


def document(source='public.md', text='Synthetic source statement.', **meta):
    return {'__data__': {'metadata': {'file_name': source, 'status': 'active', **meta}, 'text': text}}


def corpus(*documents):
    return json.dumps({'docstore/data': {str(number): record for number, record in enumerate(documents)}}).encode()


def check_corpus(raw, aliases=None, expected=None):
    return verify_docstore_snapshot(raw, expected or hashlib.sha256(raw).hexdigest(), aliases or {'public.md': 'Public'})


def report(*statuses, mode='corrective'):
    rows = [{'id': f'case-{number}', 'latency_s': 0.5, 'answer': 'Synthetic answer', 'context': [],
             'trace': {'mode': mode, 'plan': {'search_focus_status': status}}}
            for number, status in enumerate(statuses)]
    return {'mode': mode, 'selected_case_ids': [row['id'] for row in rows], 'cases': rows,
            'summary': {'completed': len(rows), 'errors': 0}}


def check_report(value, mode='corrective'):
    return validate_runtime_report(value, expected_ids=list(value['selected_case_ids']), mode=mode)


def test_complete_explicit_flags_are_normalized_without_mutating_selected_arm():
    config = flags(RAG_EVIDENCE_MAX_CHARS=36000)
    frozen = deepcopy(config)
    normalized = validate_runtime_flags(config, mode='corrective')
    assert normalized['RAG_EVIDENCE_MAX_CHARS'] == '36000'
    assert config == frozen


@pytest.mark.parametrize('name', list(flags()))
def test_every_arm_flag_is_mandatory_not_inherited(name):
    config = flags()
    config.pop(name)
    with pytest.raises(PreflightError, match='runtime-config-keys'):
        validate_runtime_flags(config, mode='corrective')


@pytest.mark.parametrize('name,value', [
    ('RAG_CORRECTIVE_PIPELINE', 'off'), ('RAG_REFERENCE_EXPANSION', 'yes'),
    ('RAG_PLANNER_THINKING', 'true'), ('RAG_ALLOW_PLANNER_THINKING', 'true'),
    ('RAG_RERANKER_MODE', 'disabled'), ('RAG_RERANKER_MODE', 'http'),
    ('RAG_EVIDENCE_MAX_CHARS', '-1'), ('RAG_EVIDENCE_MAX_CHARS', '0'),
    ('RAG_PLANNER_TOKEN_BUDGET', '32768'), ('QA_EXPECTED_CONTEXT_WINDOW', '4096'),
    ('RAG_MAX_OUTPUT_TOKENS', '0'), ('RAG_MAX_OUTPUT_TOKENS', '-2'),
    ('RAG_PLANNER_TIMEOUT_SECONDS', 'nan'), ('RAG_PLANNER_TIMEOUT_SECONDS', '0'),
    ('QDRANT_URL', 'https://external.example'), ('QDRANT_COLLECTION', '../outside'),
    ('EMBED_MODEL', ''), ('RAG_SOURCE_READING', False),
])
def test_invalid_off_or_redirected_runtime_cannot_masquerade_as_candidate(name, value):
    with pytest.raises(PreflightError):
        validate_runtime_flags(flags(**{name: value}), mode='corrective')


def test_explicit_baseline_flags_and_http_reranker_destination_are_supported():
    config = flags(RAG_CORRECTIVE_PIPELINE='off', RAG_SOURCE_READING='off',
        RAG_TASK_SCOPE_CHECK='off', RAG_RERANKER_MODE='http',
        RAG_RERANKER_URL=EMBEDDING_SERVICE + '/api/rerank')
    assert validate_runtime_flags(config, mode='legacy') == config


@pytest.mark.parametrize('name,value', [
    ('RAG_SOURCE_READING', 'off'), ('RAG_CORRECTIVE_PIPELINE', 'off'),
    ('OLLAMA_BASE_URL', 'http://llm-bridge:11434'),
    ('RAG_PLANNER_BASE_URL', 'http://previous-bridge:11434'),
    ('QA_EMBEDDING_BASE_URL', 'http://external.example'),
    ('VLLM_BASE_URL', 'http://external.example'),
])
def test_effective_environment_must_match_arm_and_destinations(name, value):
    with pytest.raises(PreflightError):
        validate_effective_runtime(flags(), environment(**{name: value}))


def test_unset_or_private_planner_destination_is_allowed_to_be_locked():
    validate_effective_runtime(flags(), environment())
    validate_effective_runtime(flags(), environment(RAG_PLANNER_BASE_URL=PRIVATE_BRIDGE))


@pytest.mark.parametrize('root,path', [
    ('/tmp/ragqa-test', '/tmp/ragqa-other/data'), ('/tmp/ragqa-test', '/app/.data'),
    ('/tmp/ragqa-test', '/tmp/ragqa-test/../outside'), ('/tmp/ragqa-test', 'relative'),
    ('/tmp', '/tmp/data'), ('/tmp/ragqa-', '/tmp/ragqa-/data'),
    ('/tmp/ragqa-test/nested', '/tmp/ragqa-test/nested/data'),
])
def test_qa_paths_reject_roots_and_escapes(root, path):
    with pytest.raises(PreflightError):
        validate_qa_paths(root, {'data': path})


def test_qa_paths_allow_only_private_root_descendants():
    assert validate_qa_paths('/tmp/ragqa-test', {'data': '/tmp/ragqa-test/data'}) == {'data': '/tmp/ragqa-test/data'}


def test_exact_snapshot_requires_all_allowed_active_sources_but_not_private_decoys():
    raw = corpus(document('PUBLIC.md'), document('second.md'), document('private.md'),
                 document('public.md', status='deleted'))
    observed = check_corpus(raw, {'public.md': 'Public', 'second.md': 'Second'})
    assert observed['stored_records'] == 4
    assert observed['active_allowed_nodes'] == 2
    assert observed['active_allowed_sources'] == 2
    assert observed['vector_content_hash_verified'] is False


@pytest.mark.parametrize('raw', [b'{}', corpus(), corpus(document(text='')), corpus(document(status='deleted')),
                                 corpus(document('private.md')), b'not-json'])
def test_empty_missing_or_invalid_corpus_fails_before_model_initialization(raw):
    with pytest.raises(PreflightError):
        check_corpus(raw)


def test_partial_alias_coverage_or_wrong_hash_is_not_a_valid_snapshot():
    raw = corpus(document())
    with pytest.raises(PreflightError, match='missing-active'):
        check_corpus(raw, {'public.md': 'Public', 'missing.md': 'Missing'})
    with pytest.raises(PreflightError, match='hash-mismatch'):
        check_corpus(raw, expected='0' * 64)


@pytest.mark.parametrize('aliases', [{}, {'PUBLIC.md': 'Public'}, {'../public.md': 'Public'},
                                     {'public.md': ''}, {'public.md': 'Same', 'second.md': 'Same'}])
def test_bad_alias_manifest_cannot_expand_or_empty_frozen_scope(aliases):
    with pytest.raises(PreflightError):
        validate_aliases(aliases)


def test_model_and_actual_maximum_context_are_verified_not_taken_from_env_alone():
    payload = {'data': [{'id': 'synthetic-model', 'max_model_len': 32768}]}
    identity = validate_model_identity(payload, bridge_model='synthetic-model',
        requested_model='synthetic-model', expected_context_window=32768)
    assert identity['context_verified_from_upstream']


@pytest.mark.parametrize('rows,bridge,window', [
    ([], 'synthetic-model', 32768),
    ([{'id': 'wrong-model', 'max_model_len': 32768}], 'synthetic-model', 32768),
    ([{'id': 'synthetic-model'}], 'synthetic-model', 32768),
    ([{'id': 'synthetic-model', 'max_model_len': '32768'}], 'synthetic-model', 32768),
    ([{'id': 'synthetic-model', 'max_model_len': 32768}], 'different-bridge', 32768),
    ([{'id': 'synthetic-model', 'max_model_len': 16384}], 'synthetic-model', 32768),
])
def test_missing_wrong_or_unverified_model_context_fails(rows, bridge, window):
    with pytest.raises(PreflightError):
        validate_model_identity({'data': rows}, bridge_model=bridge,
            requested_model='synthetic-model', expected_context_window=window)


def test_report_records_bad_or_missing_focus_without_certifying_quality_or_rejecting_fallback():
    result = check_report(report('valid', 'absent', 'rejected', None))
    assert result['focus_status_counts'] == {'valid': 1, 'absent': 1, 'rejected': 1, 'unknown': 1}
    assert result['answer_quality_certified'] is False
    assert result['gate'] == 'execution-shape-only-not-answer-quality'


@pytest.mark.parametrize('field,value', [('completed', -1), ('completed', 0), ('completed', 2),
                                       ('errors', -1), ('errors', 1), ('errors', False)])
def test_incomplete_error_or_negative_counts_are_not_success(field, value):
    result = report('valid')
    result['summary'][field] = value
    with pytest.raises(PreflightError):
        check_report(result)


@pytest.mark.parametrize('mutation', ['case_error', 'missing_answer', 'wrong_pipeline', 'missing_context',
                                     'negative_latency', 'missing_case', 'extra_noncase', 'different_selection'])
def test_each_selected_case_must_complete_correct_full_pipeline(mutation):
    result = report('valid')
    if mutation == 'case_error':
        result['cases'][0]['error'] = 'SyntheticError'
    elif mutation == 'missing_answer':
        result['cases'][0].pop('answer')
    elif mutation == 'wrong_pipeline':
        result['cases'][0]['trace']['mode'] = 'legacy'
    elif mutation == 'missing_context':
        result['cases'][0].pop('context')
    elif mutation == 'negative_latency':
        result['cases'][0]['latency_s'] = -1
    elif mutation == 'missing_case':
        result['cases'] = []
    elif mutation == 'extra_noncase':
        result['cases'].append(None)
    else:
        result['selected_case_ids'] = ['different-case']
    with pytest.raises(PreflightError):
        validate_runtime_report(result, expected_ids=['case-0'], mode='corrective')


def test_negative_controls_may_have_empty_evidence_but_not_missing_corpus():
    result = check_report(report('valid'))
    assert result['completed'] == 1
    assert check_report(report('absent', mode='legacy'), mode='legacy')['completed'] == 1


@pytest.mark.parametrize('name', ['rag_agent', 'runtime_paths'])
def test_runtime_imported_before_configuration_lock_is_rejected(name):
    with pytest.raises(PreflightError, match='runtime-imported-before-config-lock'):
        validate_fresh_runtime({name: object()})


def test_pure_helpers_do_not_prevent_a_fresh_runtime_process():
    validate_fresh_runtime({'evals.runtime_preflight': object(), 'json': object()})


@pytest.mark.parametrize('status', ['green', 'yellow'])
def test_vector_readiness_accepts_nonempty_ready_collection_without_claiming_content_identity(status):
    result = validate_vector_readiness({'status': status, 'points_count': 4, 'private_metadata': 'ignored'})
    assert result == {'status': status, 'points_count': 4, 'vector_content_hash_verified': False}


@pytest.mark.parametrize('info', [None, {}, {'points_count': 4}, {'status': None, 'points_count': 4},
    {'status': 'red', 'points_count': 4}, {'status': 'green'}, {'status': 'green', 'points_count': 0},
    {'status': 'yellow', 'points_count': -1}, {'status': 'green', 'points_count': True},
    {'status': 'green', 'points_count': None}, {'status': 'green', 'points_count': '4'}])
def test_red_empty_or_unverified_vector_collection_cannot_pass_full_hybrid_gate(info):
    with pytest.raises(PreflightError):
        validate_vector_readiness(info)
