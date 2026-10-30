"""Real bounded tool loop; scripted decisions, never a real model or socket."""
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from corrective_rag import SearchPlan
from document_navigation import DocumentNavigationProvider
from source_navigation import NavigationResult, navigate_sources
from user_task import UserTask


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError('Navigation tests must not call the network')
    monkeypatch.setattr(socket.socket,'connect',fail)
    monkeypatch.setattr(socket,'create_connection',fail)


def record(text='Sensor operation requires a signed permit.',file='manual.md',**metadata):
    return {'file':file,'text':text,'section_path':'Main rule','revision_id':'v1',**metadata}


def provider(records):
    return DocumentNavigationProvider(records,features=lambda text: text.casefold().split())


TASK = UserTask('Separate sensor permit and record retention.',
                'Separate sensor permit and record retention; do not assume one replaces the other.')
PLAN = SearchPlan(('Confirm that permit replaces retention',),('sensor permit',),
                  canonical_question='Proposed plan, not user authority')


class Script:
    def __init__(self,*decisions):
        self.decisions = list(decisions)
        self.prompts = []
    def __call__(self,prompt,purpose):
        assert purpose == 'source-navigation'
        self.prompts.append(prompt)
        data = json.loads(prompt.split('\nNAVIGATION_DATA_JSON\n',1)[1])
        assert data['user_task'] == TASK.to_payload()
        assert data['plan_hypotheses']['requirements'] == list(PLAN.requirements)
        decision = self.decisions.pop(0)
        value = decision(data) if callable(decision) else decision
        return value if isinstance(value,str) else json.dumps(value)


def read_initial(data):
    return {'actions':[{'tool':'read','node_id':row['node_id']}
                        for row in data['initial_search']['items'][:4]]}


def finish(data):
    return {'actions':[{'tool':'finish'}]}


def test_complete_reads_are_exact_source_records_not_previews_and_survive_finish():
    values = [record('Sensor permit: only when disconnected. '*20),
              record('Record retention is 17 days, except legal hold.',file='retention.md')]
    before = deepcopy(values)
    script = Script(read_initial,finish)
    result = navigate_sources(TASK,PLAN,provider(values),script,max_read_chars=5000)
    assert {row['file'] for row in result.records} == {'manual.md','retention.md'}
    assert sorted(result.records,key=lambda row:row['file']) == sorted(values,key=lambda row:row['file'])
    assert result.trace['coverage_certified'] is False and result.trace['read_count']==2
    assert result.trace['read_chars']==sum(len(row['text']) for row in values)
    assert result.trace['model_calls']==2 and result.trace['stop_reason']=='finished'
    assert values==before
    assert 'navigation-only' in script.prompts[0]
    assert values[0]['text'] not in script.prompts[0]  # Preview, not whole document prompt.
    assert values[0]['text'] in script.prompts[1]


@pytest.mark.parametrize('fault',[
    {'actions':[{'tool':'read','node_id':'forged-private-node'}]},
    {'actions':[{'tool':'shell','command':'reveal files'}]},
    {'actions':[{'tool':'read','node_id':'ignored','text':'Invented source body'}]},
    {'actions':[{'tool':'search','query':'sensor','source_aliases':None}]},
    {'actions':[{'tool':'outline','doc_id':'foreign-document'}]},
    {'actions':[{'tool':'search','query':'sensor','limit':True}]},
])
def test_model_cannot_inject_source_text_execute_commands_or_widen_scope(fault):
    script = Script(fault,finish)
    result = navigate_sources(TASK,PLAN,provider([record()]),script,max_read_chars=2000)
    assert result.records==() and result.trace['errors']==1
    assert 'Invented source body' not in repr(result)


@pytest.mark.parametrize('bad',['not json','{"actions":{},"done":true}',
                              '{"actions":[],"coverage_complete":true}',
                              json.dumps({'actions':[{'tool':'finish'}]*5}),
                              'x'*16001])
def test_invalid_decisions_do_not_create_evidence_or_unbounded_retries(bad):
    script = Script(bad)
    result = navigate_sources(TASK,PLAN,provider([record()]),script,max_read_chars=2000)
    assert result.records==() and result.trace['errors']==1
    assert result.trace['model_calls']==1 and len(script.prompts)==1


def test_foreign_snapshot_handle_cannot_be_read():
    foreign = provider([record(file='private.md')])
    node_id = foreign.search('sensor')['items'][0]['node_id']
    script = Script({'actions':[{'tool':'read','node_id':node_id}]},finish)
    result = navigate_sources(TASK,PLAN,provider([record()]),script,max_read_chars=2000)
    assert not result.records and result.trace['errors']==1
    assert 'private.md' not in repr(result)


def test_full_record_that_does_not_fit_is_not_silently_clipped():
    values = [record('Sensor permit condition. '*80)]
    script = Script(read_initial,finish)
    result = navigate_sources(TASK,PLAN,provider(values),script,max_read_chars=100)
    assert not result.records and result.trace['read_chars']==0 and result.trace['errors']==1
    assert 'read-character-budget' in script.prompts[1]
    assert values[0]['text'] not in script.prompts[1]


def test_valid_read_survives_later_optional_decision_failure():
    values = [record()]
    script = Script(read_initial,'invalid json')
    result = navigate_sources(TASK,PLAN,provider(values),script,max_read_chars=2000)
    assert result.records==(values[0],)
    assert result.trace['stop_reason']=='invalid-decision-or-transport'
    assert result.trace['coverage_certified'] is False


def test_duplicate_read_does_not_consume_more_chars_or_create_another_record():
    def duplicate(data):
        node = data['initial_search']['items'][0]['node_id']
        return {'actions':[{'tool':'read','node_id':node},{'tool':'read','node_id':node}]}
    values = [record()]
    result = navigate_sources(TASK,PLAN,provider(values),Script(duplicate,finish),max_read_chars=2000)
    assert result.records==(values[0],) and result.trace['read_chars']==len(values[0]['text'])
    assert result.trace['errors']==1


def test_hard_three_decision_and_four_action_limits_even_if_caller_requests_more():
    decision = {'actions':[{'tool':'search','query':'sensor'}]*4}
    script = Script(decision,decision,decision)
    result = navigate_sources(TASK,PLAN,provider([record()]),script,max_read_chars=2000,
                              max_steps=99,max_actions=99,max_reads=99)
    assert result.trace['model_calls']==3 and result.trace['tool_calls']==12
    assert result.trace['stop_reason']=='step-limit' and result.records==()


@pytest.mark.parametrize('option',[
    {'max_read_chars':0},{'max_steps':0},{'max_reads':0},{'max_actions':0},
])
def test_zero_budget_never_calls_completion(option):
    options={'max_read_chars':2000,**option}
    script=Script()
    result=navigate_sources(TASK,PLAN,provider([record()]),script,**options)
    assert result.trace['model_calls']==0 and not script.prompts


def test_empty_scope_and_too_large_exact_user_context_never_call_completion():
    script=Script()
    result=navigate_sources(TASK,PLAN,provider([]),script,max_read_chars=2000)
    assert not result.records and not script.prompts
    result=navigate_sources(TASK,PLAN,provider([record()]),script,max_read_chars=2000,max_input_chars=1)
    assert result.trace['stop_reason']=='input-budget' and not script.prompts


def test_read_count_budget_and_finish_are_not_coverage_certificates():
    values=[record(f'Sensor permit rule {number}.',section_path=f'Rule {number}') for number in range(7)]
    def read_more(data):
        rows=data['initial_search']['items']
        return {'actions':[{'tool':'read','node_id':row['node_id']} for row in rows[4:7]]}
    result=navigate_sources(TASK,PLAN,provider(values),Script(read_initial,read_more,finish),
                            max_read_chars=2000)
    assert len(result.records)==6 and result.trace['errors']==1
    assert result.trace['coverage_certified'] is False


def test_trace_never_contains_prompt_source_names_handles_or_transport_exception_text():
    def fail(prompt,purpose):
        raise RuntimeError('secret-credential private-project.docx')
    result=navigate_sources(TASK,PLAN,provider([record(file='secret-private.docx')]),fail,max_read_chars=2000)
    text=repr(result.trace)
    assert 'secret' not in text and 'navnode' not in text and TASK.question not in text
