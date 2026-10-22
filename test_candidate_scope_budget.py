"""Visibility must precede body rarity statistics and candidate quotas."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evidence_selection import SectionEvidenceIndex
from rag_agent import RAGAgent


QUESTION = 'Каков лимит отпуска сотрудника?'
SUBJECT = 'Лимит отпуска сотрудника.'
ANSWER = 'Лимит отпуска сотрудника составляет двадцать семь дней после завершения испытательного срока.'


def node(text, file='visible.txt', **metadata):
    return SimpleNamespace(text=text, metadata={'file_name': file, 'status': 'active', **metadata})


def crowded_nodes(private_count=100, filler_count=1000):
    return [node(ANSWER),
            *(node(SUBJECT, f'private-{number}.txt') for number in range(private_count)),
            *(node('Служебная запись содержит прочие сведения.', f'other-{number}.txt')
              for number in range(filler_count))]


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_inaccessible_short_matches_cannot_consume_body_quota(mode):
    nodes = crowded_nodes()
    index = SectionEvidenceIndex(nodes)
    expected = SectionEvidenceIndex(nodes[:1]).body_seeds(QUESTION, limit=30, mode=mode)
    actual = index.body_seeds(QUESTION, limit=30, mode=mode, allowed_sources={'visible.txt'})
    assert actual == expected
    assert [item['file'] for item in actual] == ['visible.txt']
    assert actual[0]['text'] == ANSWER


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_inaccessible_repetition_cannot_change_visible_rarity_gate(mode):
    # No filler: before early eligibility, 101 repetitions made every subject
    # term common and suppressed the channel even without reaching the cap.
    index = SectionEvidenceIndex(crowded_nodes(filler_count=0))
    actual = index.body_seeds(QUESTION, mode=mode, allowed_sources={'visible.txt'})
    assert len(actual) == 1
    assert actual[0]['lexical_coverage'] == 1


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_excluded_sources_apply_before_body_quota_with_no_allowlist(mode):
    nodes = crowded_nodes()
    excluded = {item.metadata['file_name'] for item in nodes[1:]}
    result = SectionEvidenceIndex(nodes).body_seeds(QUESTION, mode=mode, excluded_sources=excluded)
    assert [item['file'] for item in result] == ['visible.txt']


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_empty_allowlist_excluded_wins_and_deleted_never_enters(mode):
    index = SectionEvidenceIndex([node(ANSWER), node(ANSWER, 'deleted.txt', status='deleted')])
    assert index.body_seeds(QUESTION, mode=mode, allowed_sources=set()) == []
    assert index.body_seeds(QUESTION, mode=mode, allowed_sources={'visible.txt'},
                            excluded_sources={'VISIBLE.TXT'}) == []
    assert index.body_seeds(QUESTION, mode=mode, allowed_sources={'deleted.txt'}) == []
    assert [item['file'] for item in index.body_seeds(QUESTION, mode=mode,
                                                    allowed_sources={'VISIBLE.TXT'})] == ['visible.txt']


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_many_accessible_documents_keep_original_quota_and_index(mode):
    # Keep the pre-existing rare-anchor prerequisite satisfied: the fix must
    # not turn a subject repeated throughout the whole visible corpus rare.
    nodes = [*(node(ANSWER, f'available-{number}.txt') for number in range(100)),
             *(node('Служебная запись содержит прочие сведения.', f'available-other-{number}.txt')
               for number in range(1000))]
    index = SectionEvidenceIndex(nodes)
    postings, items = index.body_postings, index.body_items
    allowed = {item.metadata['file_name'] for item in nodes}
    original_allowed, original_nodes = allowed.copy(), deepcopy([item.metadata for item in nodes])
    result = index.body_seeds(QUESTION, limit=30, mode=mode, allowed_sources=allowed)
    assert len(result) == 30
    assert index.body_postings is postings and index.body_items is items
    assert allowed == original_allowed
    assert [item.metadata for item in nodes] == original_nodes
    assert index.body_seeds(QUESTION, mode=mode) == index.body_seeds(
        QUESTION, mode=mode, allowed_sources=None, excluded_sources=None)


def test_select_v2_internal_bootstrap_scopes_before_cap_without_ranked_rescue():
    index = SectionEvidenceIndex(crowded_nodes())
    result = index.select_v2(QUESTION, [], allowed_sources={'visible.txt'}, top_k=10,
                             resolve_references=False)
    assert len(result) == 1
    assert result[0]['file'] == 'visible.txt'
    assert result[0]['text'] == ANSWER
    assert index.select_v2(QUESTION, [], allowed_sources=set(), resolve_references=False) == []


def test_select_v2_keeps_ranked_revision_preference_and_denied_sources_out():
    nodes = [node(ANSWER + ' Старая редакция.', revision_id='old'),
             node(ANSWER + ' Действующая редакция.', revision_id='new'),
             *crowded_nodes()[1:]]
    preferred = {'file': 'visible.txt', 'text': nodes[1].text, 'status': 'active', 'revision_id': 'new'}
    result = SectionEvidenceIndex(nodes).select_v2(
        QUESTION, [preferred], allowed_sources={'visible.txt'}, top_k=10, resolve_references=False)
    assert result
    assert all(item['file'] == 'visible.txt' and item.get('revision_id') == 'new' for item in result)
    assert all('Старая редакция' not in item['text'] for item in result)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_retrieve_context_body_wiring_survives_private_crowding_without_network(mode):
    agent = object.__new__(RAGAgent)
    agent._all_nodes = crowded_nodes()
    agent._refresh_docstore_if_changed = Mock()
    retriever = Mock()
    retriever.retrieve.return_value = []
    agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
    result = agent.retrieve_context(QUESTION, retrieval_queries=(), source_aliases={'visible.txt': 'Visible policy'},
                                    selection_mode=mode)
    assert result and all(item['file'] == 'visible.txt' for item in result)
    assert any(item['text'] == ANSWER for item in result)


def test_retrieve_context_passes_explicit_scope_and_exclusions_to_body_lookup(monkeypatch):
    agent = object.__new__(RAGAgent)
    agent._all_nodes = [node(ANSWER), node(ANSWER, 'another.txt')]
    agent._refresh_docstore_if_changed = Mock()
    retriever = Mock()
    retriever.retrieve.return_value = []
    agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
    index = SectionEvidenceIndex(agent._all_nodes)
    agent._section_evidence_index = index
    agent._section_evidence_nodes = agent._all_nodes
    spy = Mock(wraps=index.body_seeds)
    monkeypatch.setattr(index, 'body_seeds', spy)
    excluded = {'private.txt'}
    agent.retrieve_context('В visible.txt каков лимит отпуска сотрудника?', retrieval_queries=(),
                           source_aliases={'visible.txt': 'Visible policy', 'another.txt': 'Another policy'},
                           excluded_sources=excluded, selection_mode='coverage')
    assert spy.call_args_list
    for call in spy.call_args_list:
        assert call.kwargs['allowed_sources'] == {'visible.txt'}
        assert call.kwargs['excluded_sources'] == excluded
