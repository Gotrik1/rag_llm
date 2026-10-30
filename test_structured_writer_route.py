"""Typed authorship is opt-in and keeps the actual evidence guard authoritative."""
import json
from unittest.mock import Mock

import pytest
import rag_agent
import ordinary_answer_guard as guard
from test_ordinary_formula_fidelity import make_agent

FACT='The measured quantity applies only to the selected object and hour.'
CONTEXT=[{'file':'neutral.md','section_path':'Native rule','text':FACT,'score':1.0}]

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr('socket.socket.connect',Mock(side_effect=AssertionError('No network')))
    monkeypatch.setenv('RAG_STRUCTURED_WRITER','on')


def wire(text=FACT,ids=('E1',)):
    return json.dumps({'blocks':[{'text':text,'source_ids':list(ids)}]})


def test_exact_native_block_is_published_with_model_selected_source_and_cached_proof():
    agent=make_agent(wire())
    answer=agent._generate_grounded_answer('Explain the measured quantity.',CONTEXT,[],
        selection_mode='coverage',cache_enabled=True)
    assert FACT in answer and 'Источник: neutral.md' in answer
    assert '{"blocks"' not in answer
    assert agent.last_retrieval_trace()['publication_verification']['status']=='verified'
    assert agent.response_cache.set.called
    assert 'evidence-bound-blocks-v2-native-rows' in agent.llm.complete.call_args_list[0].args[0]


@pytest.mark.parametrize('raw',[wire('A fabricated foreign conclusion.',('E99',)),
    wire('A fabricated foreign conclusion.',()),'{"blocks":[]}', 'A free-form answer [E1].'])
def test_invalid_draft_never_gets_an_assigned_source_or_a_verified_cache_entry(raw):
    agent=make_agent(raw)
    answer=agent._generate_grounded_answer('Explain the measured quantity.',CONTEXT,[],
        selection_mode='coverage',cache_enabled=True)
    assert 'A fabricated foreign conclusion.' not in answer
    assert agent.last_retrieval_trace()['publication_verification']['reason_codes']==['writer_protocol_invalid']
    assert not agent.response_cache.set.called
    assert agent.llm.complete.call_count==1


def test_semantic_repair_uses_the_same_typed_authoring_contract(monkeypatch):
    agent=make_agent(wire());seen=[]
    def publication(answer,packet,question,*,repairer,**kwargs):
        repaired=repairer('Correct the unsupported claim.');seen.append(repaired)
        return guard.GuardResult(repaired,True)
    monkeypatch.setattr(rag_agent,'publish_draft',publication)
    agent._generate_grounded_answer('Explain the measured quantity.',CONTEXT,[],selection_mode='coverage')
    assert FACT in seen[0] and '[E1]' in seen[0]
    assert 'evidence-bound-blocks-v2-native-rows' in agent.llm.complete.call_args_list[-1].args[0]


@pytest.mark.parametrize('mode',['legacy','coverage'])
def test_disabled_writer_preserves_the_existing_generation_transport(monkeypatch,mode):
    monkeypatch.setenv('RAG_STRUCTURED_WRITER','off')
    agent=make_agent(FACT+' [E1]')
    agent._generate_grounded_answer('Explain the measured quantity.',CONTEXT,[],selection_mode=mode)
    assert 'evidence-bound-blocks-v2-native-rows' not in agent.llm.complete.call_args_list[0].args[0]


def test_model_cannot_rebind_a_fact_to_another_document_even_with_a_lying_critic():
    agent=make_agent(wire(ids=('E2',)))
    def complete(prompt):
        if prompt.startswith('ПРОВЕРКА ПУБЛИКАЦИИ'):
            return json.dumps({'claims':[['C1','supported',['E1:P1'],'exact_target','complete']]})
        return wire(ids=('E2',))
    agent.llm.complete=Mock(side_effect=complete)
    context=[*CONTEXT,{'file':'unrelated.md','section_path':'Other','text':'A different native fact.','score':1.0}]
    agent._generate_grounded_answer('Explain the measured quantity.',context,[],selection_mode='coverage',cache_enabled=True)
    assert agent.last_retrieval_trace()['publication_verification']['status']=='partial'
    assert not agent.response_cache.set.called
