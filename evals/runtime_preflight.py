"""Pure, fail-closed checks for a private runtime experiment, not production.

Passing these gates proves inputs and execution shape, never answer quality or
the semantic identity of a live vector collection. No I/O occurs in helpers.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import PurePosixPath
import re


class PreflightError(ValueError):
    """Static diagnostic codes; response bodies and documents are never errors."""


SWITCHES = ('RAG_CORRECTIVE_PIPELINE', 'RAG_REFERENCE_EXPANSION',
            'RAG_SOURCE_READING', 'RAG_TASK_SCOPE_CHECK')
POSITIVE_INTS = ('RAG_EVIDENCE_MAX_CHARS', 'RAG_PLANNER_TOKEN_BUDGET',
                 'QA_EXPECTED_CONTEXT_WINDOW')
REQUIRED_FLAGS = frozenset((*SWITCHES, *POSITIVE_INTS, 'RAG_RERANKER_MODE',
    'RAG_PLANNER_THINKING', 'RAG_ALLOW_PLANNER_THINKING', 'RAG_MAX_OUTPUT_TOKENS',
    'RAG_PLANNER_TIMEOUT_SECONDS', 'QDRANT_URL', 'QDRANT_COLLECTION', 'EMBED_MODEL'))
OPTIONAL_FLAGS = frozenset(('LLM_REQUEST_TIMEOUT', 'RAG_RERANKER_URL'))
PRIVATE_BRIDGE = 'http://127.0.0.1:18086'
EMBEDDING_SERVICE = 'http://llm-bridge:11434'
VLLM_SERVICE = 'http://vllm:8000'
QDRANT_SERVICE = 'http://qdrant:6333'


def validate_fresh_runtime(imported_modules):
    if any(name in imported_modules for name in ('rag_agent', 'runtime_paths')):
        raise PreflightError('runtime-imported-before-config-lock')


def validate_runtime_flags(value, *, mode):
    if mode not in {'legacy', 'corrective'} or not isinstance(value, dict):
        raise PreflightError('invalid-runtime-config')
    if not REQUIRED_FLAGS <= set(value) or set(value) - REQUIRED_FLAGS - OPTIONAL_FLAGS:
        raise PreflightError('runtime-config-keys')
    if any(type(item) not in {str, int, float} for item in value.values()):
        raise PreflightError('runtime-config-value')
    flags = {key: str(item) for key, item in value.items()}
    if any(flags[key] not in {'on', 'off'} for key in SWITCHES):
        raise PreflightError('runtime-switch-value')
    if flags['RAG_CORRECTIVE_PIPELINE'] != ('on' if mode == 'corrective' else 'off'):
        raise PreflightError('pipeline-mode-mismatch')
    for key in ('RAG_PLANNER_THINKING', 'RAG_ALLOW_PLANNER_THINKING'):
        if flags[key] not in {'true', 'false'}:
            raise PreflightError('thinking-value')
    if flags['RAG_PLANNER_THINKING'] != flags['RAG_ALLOW_PLANNER_THINKING']:
        raise PreflightError('thinking-gate-mismatch')
    if flags['RAG_RERANKER_MODE'] not in {'off', 'http'}:
        raise PreflightError('reranker-mode')
    if flags['RAG_RERANKER_MODE'] == 'http' and flags.get('RAG_RERANKER_URL') != EMBEDDING_SERVICE + '/api/rerank':
        raise PreflightError('reranker-destination')
    for key in POSITIVE_INTS:
        if not re.fullmatch(r'[1-9]\d*', flags[key]):
            raise PreflightError('positive-integer-config')
    if (int(flags['QA_EXPECTED_CONTEXT_WINDOW']) <= 4096
            or int(flags['RAG_PLANNER_TOKEN_BUDGET']) >= int(flags['QA_EXPECTED_CONTEXT_WINDOW'])):
        raise PreflightError('planner-budget-outside-context')
    if flags['RAG_MAX_OUTPUT_TOKENS'] != '-1' and not re.fullmatch(r'[1-9]\d*', flags['RAG_MAX_OUTPUT_TOKENS']):
        raise PreflightError('output-token-config')
    for key in ('RAG_PLANNER_TIMEOUT_SECONDS', 'LLM_REQUEST_TIMEOUT'):
        if key in flags:
            try:
                number = float(flags[key])
            except ValueError:
                raise PreflightError('positive-timeout-config') from None
            if not math.isfinite(number) or number <= 0:
                raise PreflightError('positive-timeout-config')
    if flags['QDRANT_URL'] != QDRANT_SERVICE:
        raise PreflightError('qdrant-destination')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', flags['QDRANT_COLLECTION']):
        raise PreflightError('qdrant-collection')
    if not flags['EMBED_MODEL'].strip() or flags['EMBED_MODEL'] != flags['EMBED_MODEL'].strip():
        raise PreflightError('embedding-model')
    return flags


def validate_effective_runtime(flags, environment):
    if any(environment.get(key) != value for key, value in flags.items()):
        raise PreflightError('effective-runtime-mismatch')
    if environment.get('OLLAMA_BASE_URL') != PRIVATE_BRIDGE:
        raise PreflightError('generation-destination')
    if environment.get('RAG_PLANNER_BASE_URL') not in {None, '', PRIVATE_BRIDGE}:
        raise PreflightError('planner-destination')
    if environment.get('QA_EMBEDDING_BASE_URL') != EMBEDDING_SERVICE:
        raise PreflightError('embedding-destination')
    if environment.get('VLLM_BASE_URL', VLLM_SERVICE) != VLLM_SERVICE:
        raise PreflightError('vllm-destination')


def validate_qa_paths(root, paths):
    base = PurePosixPath(str(root))
    if (not base.is_absolute() or len(base.parts) != 3 or base.parts[1] != 'tmp'
            or not base.name.startswith('ragqa-') or base.name == 'ragqa-'):
        raise PreflightError('qa-root')
    for value in paths.values():
        candidate = PurePosixPath(str(value))
        if (not candidate.is_absolute() or '..' in candidate.parts
                or not candidate.is_relative_to(base)):
            raise PreflightError('qa-path-escape')
    return {name: str(PurePosixPath(str(value))) for name, value in paths.items()}


def validate_aliases(value):
    if (not isinstance(value, dict) or not value or any(
            not isinstance(key, str) or not key.strip() or key != key.casefold()
            or '/' in key or '\\' in key or not isinstance(title, str) or not title.strip()
            for key, title in value.items()) or len(set(value.values())) != len(value)):
        raise PreflightError('alias-manifest')
    return dict(value)


def verify_docstore_snapshot(raw, expected_sha256, aliases):
    if not isinstance(raw, bytes):
        raise PreflightError('docstore-bytes')
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_sha256):
        raise PreflightError('docstore-expected-hash')
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PreflightError('docstore-hash-mismatch')
    import json
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PreflightError('docstore-json') from None
    records = value.get('docstore/data') if isinstance(value, dict) else None
    if not isinstance(records, dict) or not records:
        raise PreflightError('docstore-empty')
    allowed = set(validate_aliases(aliases))
    active_sources, active_nodes = set(), 0
    for record in records.values():
        data = record.get('__data__', record) if isinstance(record, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get('metadata'), dict):
            continue
        meta = data['metadata']
        source = str(meta.get('file_name') or meta.get('file_path') or '').replace('\\', '/').rsplit('/', 1)[-1].casefold()
        if (source not in allowed or meta.get('status') == 'deleted'
                or not isinstance(data.get('text'), str) or not data['text'].strip()):
            continue
        active_sources.add(source)
        active_nodes += 1
    if not active_nodes or active_sources != allowed:
        raise PreflightError('docstore-missing-active-allowed-source')
    return {'sha256': expected_sha256, 'stored_records': len(records),
            'active_allowed_nodes': active_nodes, 'active_allowed_sources': len(active_sources),
            'allowed_sources': len(allowed), 'vector_content_hash_verified': False}


def validate_model_identity(payload, *, bridge_model, requested_model, expected_context_window):
    if not isinstance(requested_model, str) or not requested_model or bridge_model != requested_model:
        raise PreflightError('bridge-model-mismatch')
    if type(expected_context_window) is not int or expected_context_window <= 0:
        raise PreflightError('expected-context-window')
    rows = payload.get('data') if isinstance(payload, dict) else None
    matching = [row for row in rows if isinstance(row, dict) and row.get('id') == requested_model] if isinstance(rows, list) else []
    if len(matching) != 1:
        raise PreflightError('upstream-model-mismatch')
    actual = matching[0].get('max_model_len')
    if type(actual) is not int or actual <= 0 or actual != expected_context_window:
        raise PreflightError('upstream-context-unverified-or-mismatch')
    return {'model': requested_model, 'max_model_len': actual, 'context_verified_from_upstream': True}


def validate_vector_readiness(info):
    if not isinstance(info, dict) or info.get('status') not in {'green', 'yellow'}:
        raise PreflightError('vector-collection-not-ready')
    points = info.get('points_count')
    if type(points) is not int or points <= 0:
        raise PreflightError('vector-collection-empty-or-unverified')
    return {'status': info['status'], 'points_count': points, 'vector_content_hash_verified': False}


def validate_runtime_report(report, *, expected_ids, mode):
    if (not isinstance(report, dict) or mode not in {'legacy', 'corrective'}
            or not expected_ids or len(set(expected_ids)) != len(expected_ids)):
        raise PreflightError('runtime-report-shape')
    if report.get('mode') != mode or report.get('selected_case_ids') != expected_ids:
        raise PreflightError('runtime-report-selection')
    summary = report.get('summary')
    if (not isinstance(summary, dict) or type(summary.get('completed')) is not int
            or summary['completed'] != len(expected_ids) or type(summary.get('errors')) is not int
            or summary['errors'] != 0):
        raise PreflightError('runtime-report-incomplete-or-errors')
    cases = report.get('cases')
    if (not isinstance(cases, list) or len(cases) != len(expected_ids)
            or any(not isinstance(row, dict) for row in cases)
            or [row.get('id') for row in cases] != expected_ids):
        raise PreflightError('runtime-report-cases')
    counts = dict.fromkeys(('valid', 'absent', 'rejected', 'unknown'), 0)
    for row in cases:
        latency = row.get('latency_s')
        if ('error' in row or type(latency) not in {int, float} or not math.isfinite(latency)
                or latency < 0 or not isinstance(row.get('answer'), str)
                or not isinstance(row.get('context'), list)):
            raise PreflightError('runtime-case-incomplete')
        trace = row.get('trace')
        if mode == 'corrective':
            if not isinstance(trace, dict) or trace.get('mode') != 'corrective':
                raise PreflightError('runtime-case-pipeline-mismatch')
            plan = trace.get('plan')
            status = plan.get('search_focus_status') if isinstance(plan, dict) else None
            counts[status if isinstance(status, str) and status in counts else 'unknown'] += 1
    return {'gate': 'execution-shape-only-not-answer-quality', 'completed': len(expected_ids),
            'errors': 0, 'focus_status_counts': counts, 'answer_quality_certified': False}
