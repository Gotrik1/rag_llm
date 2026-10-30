"""Docstore reload must publish one index for the exact loaded node snapshot."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import rag_agent
from test_candidate_pool_boundary import agent_for, node, QUESTION, PROBES


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', Mock(side_effect=AssertionError('No network')))
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


def retrieve(agent, aliases):
    return agent.retrieve_context(QUESTION, top_k=1, retrieval_queries=PROBES,
        selection_mode='coverage', source_aliases=aliases,
        constraint_question=QUESTION, candidate_pool_only=True)


def stored(text, file='control.md'):
    value = node(text, file=file)
    value.id_ = file
    return value


def test_first_retrieval_reuses_the_index_built_by_docstore_loading(monkeypatch):
    agent = agent_for()
    agent.docstore = SimpleNamespace(docs={'one': stored('Осмотр датчика проводят после остановки.')})
    constructor = Mock(wraps=rag_agent.SectionEvidenceIndex)
    monkeypatch.setattr(rag_agent, 'SectionEvidenceIndex', constructor)
    agent._load_nodes_from_docstore()
    loaded = agent._section_evidence_index
    retrieve(agent, {'control.md': 'control.md'})
    assert constructor.call_count == 1
    assert agent._section_evidence_index is loaded
    assert agent._section_evidence_nodes is agent._all_nodes


def test_reload_replaces_old_native_evidence_without_rebuilding_again_on_query(monkeypatch):
    agent = agent_for()
    constructor = Mock(wraps=rag_agent.SectionEvidenceIndex)
    monkeypatch.setattr(rag_agent, 'SectionEvidenceIndex', constructor)
    agent.docstore = SimpleNamespace(docs={'old': stored('Осмотр датчика старой редакции.', file='old.md')})
    agent._load_nodes_from_docstore()
    old_index = agent._section_evidence_index
    agent.docstore = SimpleNamespace(docs={'new': stored('Осмотр датчика новой редакции.', file='new.md')})
    agent._load_nodes_from_docstore()
    retrieve(agent, {'new.md': 'new.md'})
    assert constructor.call_count == 2
    assert agent._section_evidence_index is not old_index
    assert {row['file'] for row in agent._section_evidence_index.body_items} == {'new.md'}
    assert agent._section_evidence_nodes is agent._all_nodes


def test_empty_replacement_docstore_clears_old_nodes_and_index():
    agent = agent_for([node('Old native evidence.', file='old.md')])
    agent.docstore = SimpleNamespace(docs={})
    agent._load_nodes_from_docstore()
    assert agent._all_nodes == []
    assert agent._section_evidence_index.body_items == []
    assert agent._section_evidence_nodes is agent._all_nodes
