"""Native retrieval merging must not invent equality or transplant provenance.

These component regressions use synthetic native record shapes, not private
corpus files, models, live retrievers or databases.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_candidate_pool_boundary import agent_for, node
from rag_agent import (RAGAgent, _retrieval_record_identity, _round_robin_body_probes,
                       _preserve_body_probe_metadata, effective_chunk_metadata,
                       relationship_expansion_results)


QUESTION = 'Правила определения цены'
PREFIX = 'Общие условия применения расчетного правила. ' * 20


@pytest.fixture(autouse=True)
def no_runtime_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Native identity regressions must not use network or models')
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


def retrieve(hits, *, scores=None, aliases=None, excluded=None):
    agent = agent_for(hits=hits)
    if scores is not None:
        agent._build_query_engine.return_value.retriever.retrieve.return_value = [
            SimpleNamespace(node=hit, score=score) for hit, score in zip(hits, scores)]
    if aliases is None:
        aliases = {hit.metadata['file_name'].casefold(): hit.metadata['file_name'] for hit in hits}
    result = agent.retrieve_context(
        QUESTION, top_k=10, retrieval_queries=(QUESTION,),
        source_aliases=aliases, excluded_sources=excluded, candidate_pool_only=True)
    return result


def test_shared_700_character_prefix_does_not_erase_different_native_suffixes():
    hits = [node(PREFIX + ' Основная ставка равна двадцати.', path='Правило'),
            node(PREFIX + ' Основная ставка равна тридцати.', path='Правило')]
    before = deepcopy([(hit.text, hit.metadata) for hit in hits])
    assert hits[0].text[:700] == hits[1].text[:700]
    result = retrieve(hits)
    assert len(result) == 2
    assert {item['text'] for item in result} == {hit.text for hit in hits}
    assert [(hit.text, hit.metadata) for hit in hits] == before


def test_higher_scoring_prefix_collision_keeps_text_and_locator_from_one_native_record():
    hits = [node(PREFIX + ' Условие применяется до поставки.', path='Пункт 7'),
            node(PREFIX + ' Условие применяется после поставки.', path='Пункт 9')]
    result = retrieve(hits, scores=(0.5, 0.9))
    native_pairs = {(hit.text, hit.metadata['section_path']) for hit in hits}
    assert len(result) == 2
    assert all((item['text'], item['section_path']) in native_pairs for item in result)
    assert result[0]['section_path'] == 'Пункт 9'
    assert result[0]['text'] == hits[1].text


@pytest.mark.parametrize('field,left,right', [
    ('section_path', 'Пункт 7', 'Пункт 9'),
    ('revision_id', 'r1', 'r2'),
    ('version_id', 1, '1'),
    ('document_id', 'doc-a', 'doc-b'),
    ('source_id', 'upload-a', 'upload-b'),
    ('file_path', 'source-a/manual.md', 'source-b/manual.md'),
    ('page_number', 1, 2),
    ('page_label', 'I', 'II'),
])
def test_same_text_with_different_native_locator_or_revision_is_not_duplicate(field, left, right):
    first = node(PREFIX + ' Значение сохраняется.', path='Пункт')
    second = node(first.text, path='Пункт')
    first.metadata[field], second.metadata[field] = left, right
    result = retrieve([first, second])
    assert len(result) == 2
    assert {type(item[field]).__name__ + ':' + str(item[field]) for item in result} == {
        type(left).__name__ + ':' + str(left), type(right).__name__ + ':' + str(right)}


def test_proven_exact_duplicate_uses_max_score_without_mutating_native_inputs():
    first = node(PREFIX + ' Условие применяется до поставки.', path='Пункт 7', revision_id='r1')
    second = node(first.text, path='Пункт 7', revision_id='r1')
    before = deepcopy([(hit.text, hit.metadata) for hit in (first, second)])
    result = retrieve([first, second], scores=(0.5, 0.9))
    assert len(result) == 1
    assert result[0]['raw_score'] == 0.9
    assert result[0]['text'] == first.text
    assert result[0]['section_path'] == first.metadata['section_path']
    assert [(hit.text, hit.metadata) for hit in (first, second)] == before


def test_opaque_identity_metadata_does_not_prove_equality_even_when_reused():
    opaque = {'revision': 'not-a-scalar-identity'}
    hit = node(PREFIX + ' Условие применяется до поставки.', path='Пункт 7', revision_id=opaque)
    before = deepcopy(hit.metadata)
    result = retrieve([hit, hit])
    assert len(result) == 2
    assert hit.metadata == before


def test_native_merge_does_not_widen_existing_source_acl_or_exclusions():
    visible = node(PREFIX + ' Разрешенная запись.', file='visible.md', path='Пункт 7')
    foreign = node(PREFIX + ' Чужая запись.', file='foreign.md', path='Пункт 7')
    blocked = node(PREFIX + ' Исключенная запись.', file='blocked.md', path='Пункт 7')
    result = retrieve([visible, foreign, blocked],
                      aliases={'visible.md': 'Visible', 'blocked.md': 'Blocked'}, excluded={'blocked.md'})
    assert len(result) == 1
    assert result[0]['file'] == 'visible.md'


def test_body_probe_budget_uses_the_same_exact_identity_as_the_surrounding_merge():
    existing = {'file': 'manual.md', 'text': PREFIX + ' Правило до поставки.', 'section_path': 'Пункт 7'}
    distinct = {**existing, 'text': PREFIX + ' Правило после поставки.'}
    another = {**existing, 'text': PREFIX + ' Запрет без согласования.', 'section_path': 'Пункт 9'}
    seen = {_retrieval_record_identity(existing)}
    result = _round_robin_body_probes([[existing, distinct], [existing, another]], 2, seen)
    assert result == [distinct, another]
    assert seen == {_retrieval_record_identity(existing)}


def test_opaque_merge_identity_is_a_fresh_retained_token_not_a_stable_coercion():
    item = {'file': 'manual.md', 'text': 'Native content.', 'revision_id': {'opaque': True}}
    first, second = _retrieval_record_identity(item), _retrieval_record_identity(item)
    assert type(first) is object and type(second) is object and first is not second


@pytest.mark.parametrize('field', ['file_path', 'revision_id', 'version_id', 'page_number',
                                  'page_label', 'start_char_idx', 'end_char_idx', 'chunk_id'])
@pytest.mark.parametrize('value', [None, '', 0, False, {'opaque': 'not-identity'}])
def test_effective_metadata_preserves_optional_native_identity_by_presence_and_type(field, value):
    metadata = {'section_path': 'Пункт 7', field: value}
    before = deepcopy(metadata)
    result = effective_chunk_metadata('Условие сохраняется.', metadata)
    assert field in result
    assert type(result[field]) is type(value) and result[field] == value
    assert metadata == before


@pytest.mark.parametrize('value', [None, '', 0, False])
def test_retrieval_preserves_absent_versus_explicit_falsey_revision(value):
    first = node(PREFIX + ' Условие сохраняется.', path='Пункт 7')
    second = node(first.text, path='Пункт 7', revision_id=value)
    result = retrieve([first, second])
    assert len(result) == 2
    assert sum('revision_id' in item for item in result) == 1
    explicit = next(item for item in result if 'revision_id' in item)
    assert type(explicit['revision_id']) is type(value) and explicit['revision_id'] == value


def test_body_provenance_never_crosses_page_or_absent_vs_null_identity():
    body = {'file': 'manual.md', 'text': 'Native content.', 'section_path': 'Пункт',
            'matched_queries': ['body-lexical'], 'lexical_coverage': 1.0, 'page_number': 1}
    for incoming in ({**body, 'page_number': 2}, {**body, 'revision_id': None}):
        incoming = {key: value for key, value in incoming.items() if key != 'lexical_coverage'}
        incoming['matched_queries'] = ['native-vector']
        assert _preserve_body_probe_metadata(body, incoming) is incoming
        assert 'lexical_coverage' not in incoming


def test_relationship_candidates_keep_page_revision_and_falsey_native_identity():
    seed = {'file': 'control.md', 'text': 'Дочернее правило.', 'section_path': 'Правило > Проверка',
            'parent_section': 'Правило', 'status': 'active', 'score': 1.0}
    nodes = [node('Условие основного правила.', path='Правило', revision_id=None, page_number=page)
             for page in (1, 2)]
    result = relationship_expansion_results(nodes, [seed], 'Порядок проверки')
    assert len(result) == 2
    assert {item['page_number'] for item in result} == {1, 2}
    assert all('revision_id' in item and item['revision_id'] is None for item in result)


def test_exact_section_seeds_do_not_collapse_same_text_on_different_pages():
    agent = object.__new__(RAGAgent)
    agent._all_nodes = [node('Расчетное правило пункта.', path='Пункт 7.1', revision_id=0, page_number=page)
                        for page in (1, 2)]
    result = agent._exact_section_seed_results('Пункт 7.1')
    assert len(result) == 2
    assert {item['page_number'] for item in result} == {1, 2}
    assert all(item['revision_id'] == 0 and type(item['revision_id']) is int for item in result)


def test_merging_a_body_seed_and_native_hit_does_not_mutate_seed_channel_annotations():
    hit = node('Расчетное правило сохраняет величину.', path='Правило')
    seed = {'file': 'control.md', 'text': hit.text, 'section_path': 'Правило', 'status': 'active',
            'matched_queries': ['body-lexical'], 'lexical_coverage': 1.0, 'score': 0.5}
    before = deepcopy(seed)
    agent = agent_for(hits=[hit])
    agent._section_evidence_index.body_seeds = Mock(return_value=[seed])
    result = agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,),
        source_aliases={'control.md': 'Control'}, candidate_pool_only=True)
    assert len(result) == 1
    assert seed == before
