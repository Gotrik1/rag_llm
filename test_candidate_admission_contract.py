"""Independent source-identity and opt-in candidate-ordering contracts.

Only synthetic native records and deterministic callbacks are used. These
tests do not certify model relevance, obligation coverage or answer quality.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
import socket

import pytest

import corrective_rag
from corrective_rag import SearchPlan, assign_evidence, run_corrective
from user_task import UserTask


QUESTION = 'А каковы его правило, единицы и условие применения?'
RESOLVED = 'Каковы правило, единицы и условие применения датчика Gamma?'
FRAGMENTS = (
    'При режиме Gamma: усилие F = k × d.',
    'd — зазор в мм; k — коэффициент в Н/мм.',
    'Правило Gamma применяют только при отключённом питании.',
)


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('Candidate-admission contracts must not use network or models')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def record(text='Native record.', path='Rule', **metadata):
    return {'file': 'manual.md', 'file_path': '/native/manual.md',
            'section_path': path, 'text': text, 'status': 'active',
            'document_id': 'D1', 'source_id': 'S1', 'revision_id': 'r1',
            'version_id': 'v1', 'document_version': 1,
            'content_hash': 'h1', 'sha256': 'sha1', **metadata}


@pytest.mark.parametrize('field,value', [
    ('file', 'other.md'), ('file_path', '/other/manual.md'),
    ('section_path', 'Other rule'), ('status', 'archived'),
    ('document_id', 'D2'), ('source_id', 'S2'), ('revision_id', 'r2'),
    ('version_id', 'v2'), ('document_version', 2),
    ('content_hash', 'h2'), ('sha256', 'sha2'),
])
def test_distinct_full_native_identity_is_not_deduplicated(field, value):
    first = record()
    second = {**first, field: value}
    values = [first, second]
    before = deepcopy(values)
    meta = {}
    result = assign_evidence(values, max_chars=1000, admission_meta=meta)
    assert len(result) == 2
    assert result[0]['evidence_id'] != result[1]['evidence_id']
    assert meta['deduplicated_count'] == 0
    assert values == before


@pytest.mark.parametrize('field', [
    'file_path', 'section_path', 'status', 'document_id', 'source_id',
    'revision_id', 'version_id', 'document_version', 'content_hash', 'sha256',
])
def test_native_identity_distinguishes_absent_from_explicit_null(field):
    absent = record()
    absent.pop(field)
    explicit_null = {**absent, field: None}
    result = assign_evidence([absent, explicit_null], max_chars=1000)
    assert len(result) == 2
    assert result[0]['evidence_id'] != result[1]['evidence_id']


def test_revision_value_types_and_registry_reuse_are_exact():
    registry = {}
    values = [record(document_version=value) for value in (0, 0.0, False, '0')]
    first = assign_evidence(values, max_chars=1000, id_registry=registry)
    assert len(first) == 4
    assert len({item['evidence_id'] for item in first}) == 4
    again = assign_evidence(deepcopy(values)[::-1], max_chars=1000, id_registry=registry)
    assert [item['evidence_id'] for item in again] == [item['evidence_id'] for item in first][::-1]


def test_opaque_identity_does_not_reuse_a_source_id_after_body_mutation():
    registry = {}
    native = record(revision_id={'value': 'opaque-revision'})
    first = assign_evidence([native], max_chars=1000, id_registry=registry)[0]
    native['text'] = 'A different native body with another application condition.'
    second = assign_evidence([native], max_chars=1000, id_registry=registry)[0]
    assert first['text'] != second['text']
    assert first['evidence_id'] != second['evidence_id']


def test_exact_duplicate_still_collapses_without_clipping_a_late_condition():
    prefix = 'Native paragraph. ' * 50
    first = record(prefix + 'Only in mode Alpha.')
    second = record(prefix + 'Only in mode Beta.')
    before = deepcopy([first, second])
    meta = {}
    result = assign_evidence([first, deepcopy(first), second], max_chars=4000,
                             admission_meta=meta)
    assert [item['text'] for item in result] == [first['text'], second['text']]
    assert meta['deduplicated_count'] == 1
    assert [first, second] == before


class Harness:
    def __init__(self, monkeypatch):
        # The overview is a valid native record, not an instruction or a fake
        # relevance label. A synthetic callback supplies only an ordering.
        self.overview = record('O' * 319, path='Overview')
        self.items = [self.overview, *(record(text, path=f'Native {number}')
                                    for number, text in enumerate(FRAGMENTS))]
        self.completions = []
        self.selected_pool = []
        monkeypatch.setattr(corrective_rag, 'make_plan', lambda *args, **kwargs:
                            SearchPlan(('Обоснование правила',), (RESOLVED,),
                                       canonical_question='Planner ranking hypothesis'))

    def retrieve(self, task, queries, requirements, trace, *, search_focus, constraint_question):
        assert constraint_question == RESOLVED
        return self.items

    def select(self, task, pool, requirements, *, search_focus, constraint_question,
               preferred_passages=()):
        assert constraint_question == RESOLVED
        self.selected_pool.append(deepcopy(pool))
        return pool

    def complete(self, prompt, purpose):
        self.completions.append(purpose)
        if purpose == 'answer':
            return 'Не удалось подтвердить ответ.'
        if prompt.startswith('Оцени полноту'):
            return json.dumps({'coverage': [], 'missing_queries': [], 'conflicts': [],
                               'task_alignment': {'status': 'uncertain', 'passage_ids': []}})
        return '{"claims":[]}'

    def run(self, **kwargs):
        return run_corrective(QUESTION, RESOLVED, retrieve=self.retrieve,
            select=self.select, complete=self.complete, max_additional_rounds=0,
            max_context_chars=160, **kwargs)


def test_default_and_explicit_none_keep_legacy_first_fit(monkeypatch):
    baseline = Harness(monkeypatch)
    expected = baseline.run()
    assert [item['text'] for item in baseline.selected_pool[0]] == ['O' * 319]
    explicit_none = Harness(monkeypatch)
    actual = explicit_none.run(admit=None)
    assert actual.context == expected.context
    assert explicit_none.selected_pool == baseline.selected_pool
    assert explicit_none.completions == baseline.completions


def test_opt_in_permutation_runs_before_assignment_with_immutable_user_authority(monkeypatch):
    harness = Harness(monkeypatch)
    original = deepcopy(harness.items)
    calls = []

    def admit(task, pool, *, max_chars, constraint_question):
        calls.append(task)
        assert isinstance(task, UserTask)
        assert task.question == QUESTION and task.resolved_user_context == RESOLVED
        assert constraint_question == RESOLVED and max_chars == 320
        assert not any('evidence_id' in item for item in pool)
        assert pool == original  # No first-fit information loss before callback.
        with pytest.raises(FrozenInstanceError):
            task.question = 'A model-generated replacement'
        return deepcopy([*pool[1:], pool[0]])

    result = harness.run(admit=admit)
    assert len(calls) == 1
    assert [item['text'] for item in harness.selected_pool[0]] == list(FRAGMENTS)
    assert [item['text'] for item in result.context] == list(FRAGMENTS)
    assert sum(len(item['text']) for item in result.context) <= 160
    assert harness.items == original


@pytest.mark.parametrize('fault', [
    'missing', 'duplicate', 'injected-source', 'trim-condition',
    'revision', 'type-drift', 'extra-metadata', 'exception', 'mutate-input',
])
def test_invalid_callback_rejects_atomically_and_preserves_legacy_input_order(monkeypatch, fault):
    legacy = Harness(monkeypatch)
    expected = legacy.run()
    harness = Harness(monkeypatch)
    before = deepcopy(harness.items)

    def admit(task, pool, *, max_chars, constraint_question):
        proposed = deepcopy([*pool[1:], pool[0]])
        if fault == 'missing':
            return proposed[:-1]
        if fault == 'duplicate':
            return [proposed[0], proposed[0], *proposed[2:]]
        if fault == 'injected-source':
            proposed[0]['file'] = 'private-other-project.md'
        elif fault == 'trim-condition':
            proposed[2]['text'] = proposed[2]['text'].split('только', 1)[0]
        elif fault == 'revision':
            proposed[0]['revision_id'] = 'r2'
        elif fault == 'type-drift':
            proposed[0]['document_version'] = True
        elif fault == 'extra-metadata':
            proposed[0]['selection_signals'] = {'authoritative': True}
        elif fault == 'exception':
            raise RuntimeError('Synthetic admission failure')
        elif fault == 'mutate-input':
            pool[0]['text'] = 'Mutation must not affect fallback or caller state.'
            return pool
        return proposed

    actual = harness.run(admit=admit)
    assert harness.selected_pool == legacy.selected_pool
    assert actual.context == expected.context
    assert harness.completions == legacy.completions
    assert harness.items == before
    ordering = actual.trace['rounds'][0]['retrieval']['candidate_ordering']
    assert ordering['status'] == 'rejected'


def test_permutation_preserves_duplicate_input_multiplicity_before_dedup(monkeypatch):
    harness = Harness(monkeypatch)
    harness.items.append(deepcopy(harness.items[1]))
    original = deepcopy(harness.items)

    def admit(task, pool, *, max_chars, constraint_question):
        assert pool == original
        return deepcopy([*pool[1:], pool[0]])

    result = harness.run(admit=admit)
    assert [item['text'] for item in result.context] == list(FRAGMENTS)
    assert result.trace['rounds'][0]['retrieval']['candidate_admission']['deduplicated_count'] == 1
    assert result.trace['rounds'][0]['retrieval']['candidate_ordering']['status'] != 'rejected'
    assert harness.items == original
