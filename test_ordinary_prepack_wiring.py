"""Ordinary retrieval must read only the authorized pool, once, before packing."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rag_agent import RAGAgent
from test_candidate_pool_boundary import agent_for,node,QUESTION,FACTS,PROBES
from test_prepack_reading import response
from user_task import UserTask


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv('RAG_PREPACK_SOURCE_READING','on')
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION','off')
    monkeypatch.setenv('RAG_EVIDENCE_MAX_CHARS','36000')
    monkeypatch.setattr('socket.socket.connect',Mock(side_effect=AssertionError('No network')))


def setup():
    agent=agent_for([node(FACTS[0],path='Порядок А'),
        node('PRIVATE_WORD_OTHER_PROJECT',file='private.md',path='Порядок Б')])
    agent.llm.complete=Mock(side_effect=response)
    return agent


def retrieve(agent,**kwargs):
    return agent.retrieve_context(QUESTION,retrieval_queries=PROBES,selection_mode='coverage',
        source_aliases={'control.md':'Control'},**kwargs)


def test_authorized_pool_is_read_once_before_actual_native_packing():
    agent=setup();calls=[]
    def complete(prompt):
        assert 'PRIVATE_WORD_OTHER_PROJECT' not in prompt and 'private.md' not in prompt
        calls.append(prompt);return response(prompt,'source-reading')
    agent.llm.complete=complete;trace={}
    result=retrieve(agent,retrieval_trace=trace)
    assert len(calls)==1 and result
    assert all(item['file']=='control.md' for item in result)
    assert trace['source_reading']['assessed'] and trace['source_reading']['preferred_passages']>0
    assert trace['source_reading']['applied']


def test_exact_raw_followup_and_resolved_user_scope_reach_the_reader():
    agent=setup();task=UserTask('А какие условия?',QUESTION)
    def complete(prompt):
        data=json.loads(prompt.split('ДАННЫЕ ЧТЕНИЯ:\n',1)[1])
        assert data['user_task']==task.to_payload() and data['question']==task.question
        return response(prompt,'source-reading')
    agent.llm.complete=complete
    assert retrieve(agent,user_task=task)


def test_candidate_pool_only_does_not_run_a_second_reader():
    agent=setup();agent.llm.complete=Mock(side_effect=AssertionError('A different orchestrator owns this reader'))
    assert retrieve(agent,candidate_pool_only=True)
    agent.llm.complete.assert_not_called()


def test_request_runtime_is_used_instead_of_a_mutated_shared_client():
    agent=setup();agent.llm.complete=Mock(side_effect=AssertionError('Shared provider must not be used'))
    stage=Mock(side_effect=lambda prompt,**kw:response(prompt,kw['purpose']))
    runtime=SimpleNamespace(client=SimpleNamespace(metadata=SimpleNamespace(context_window=32768)),
        complete_stage=stage,spec=SimpleNamespace(provider='scripted'))
    # This fixture isolates model routing from the independently tested token
    # counter: a complete runtime settings object is unnecessary for this call.
    from unittest.mock import patch
    with patch('rag_agent._ordinary_prompt_preflight',return_value={'code':'input_budget_fits'}):
        assert retrieve(agent,runtime=runtime)
    stage.assert_called_once();agent.llm.complete.assert_not_called()
    assert stage.call_args.kwargs['max_tokens']==4096 and stage.call_args.kwargs['purpose']=='source-reading'


def test_disabled_following_request_does_not_keep_a_previous_reader_status(monkeypatch):
    agent=setup();agent.llm.complete=lambda prompt:response(prompt,'source-reading');trace={}
    retrieve(agent,retrieval_trace=trace);assert 'source_reading' in trace
    monkeypatch.setenv('RAG_PREPACK_SOURCE_READING','off')
    agent.llm.complete=Mock(side_effect=AssertionError('Reader disabled'))
    retrieve(agent,retrieval_trace=trace)
    assert 'source_reading' not in trace
    agent.llm.complete.assert_not_called()


def test_incompatible_user_scope_cannot_be_silently_replaced_by_a_rewrite():
    agent=setup()
    with pytest.raises(ValueError,match='immutable retrieval scope'):
        retrieve(agent,user_task=UserTask(QUESTION,'A different topic.'))
