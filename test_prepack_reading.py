from copy import deepcopy
import json,socket
import pytest
from prepack_reading import read_candidate_frontiers
from query_rank import QueryRankContext
from user_task import UserTask


def row(text,file='neutral.md',**meta):
    return {'file':file,'section_path':'Rules','text':text,**meta}


def ranks(question):return QueryRankContext(question,lambda text:frozenset(text.casefold().split()))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(socket.socket,'connect',lambda *a:(_ for _ in ()).throw(AssertionError('No network')))


def response(prompt,purpose):
    assert purpose=='source-reading'
    data=json.loads(prompt.split('ДАННЫЕ ЧТЕНИЯ:\n',1)[1])
    eid=data['evidence'][0]['evidence_id']
    return json.dumps({'snapshot_id':data['snapshot_id'],'cards':[{'id':'C1','kind':'rule',
        'passage_ids':[eid+':P1'],'related_card_ids':[],'requirement_ids':['R1']}],
        'unresolved_requirement_ids':[]})


def test_one_semantic_call_keeps_the_entire_native_pool_for_final_selection():
    task=UserTask('Explain the cooling fee.','Explain the cooling fee.')
    items=[row('Main cooling fee rule.'),row('A separate auxiliary rule.',section_path='Other')]
    before=deepcopy(items);ctx=ranks(task.resolved_user_context);ctx.record(ctx.question,items)
    calls=[]
    def complete(*args):calls.append(args);return response(*args)
    result=read_candidate_frontiers(task,items,rank_context=ctx,max_chars=1000,complete=complete)
    assert len(calls)==1 and result.diagnostics['assessed']
    assert result.preferences and len(result.pool)==len(items)
    assert items==before and not result.diagnostics['applied']
    assert [{k:v for k,v in x.items() if k!='evidence_id'} for x in result.pool]==before


def test_resolved_context_cannot_replace_the_exact_raw_user_task():
    task=UserTask('And its conditions?','Previous USER: cooling fee. Current USER: And its conditions?')
    items=[row('The cooling fee requires a meter.')]
    def complete(prompt,purpose):
        data=json.loads(prompt.split('ДАННЫЕ ЧТЕНИЯ:\n',1)[1])
        assert data['user_task']==task.to_payload()
        assert data['question']==task.question
        return response(prompt,purpose)
    result=read_candidate_frontiers(task,items,rank_context=ranks(task.resolved_user_context),
                                    max_chars=1000,complete=complete)
    assert result.diagnostics['assessed']


def test_private_rank_history_cannot_resurrect_a_filtered_document():
    task=UserTask('Explain the cooling fee.','Explain the cooling fee.')
    public=row('A complete public rule.');private=row('SECRET_OWNED_BY_ANOTHER_CHAT',file='private.md')
    ctx=ranks(task.resolved_user_context);ctx.record(ctx.question,[private,public])
    ctx.record('refrigeration charge',[private,public])
    def complete(prompt,purpose):
        assert 'SECRET_OWNED_BY_ANOTHER_CHAT' not in prompt and 'private.md' not in prompt
        return response(prompt,purpose)
    result=read_candidate_frontiers(task,[public],rank_context=ctx,max_chars=1000,complete=complete)
    assert len(result.pool)==1 and result.pool[0]['file']=='neutral.md'


@pytest.mark.parametrize('failure',['malformed','foreign-passage','provider-error'])
def test_failed_reading_has_no_preferences_and_does_not_retry_or_cut_native_text(failure):
    task=UserTask('Explain.','Explain.');items=[row('Complete native rule. Only for this object.')];calls=[]
    def complete(prompt,purpose):
        calls.append(1)
        if failure=='provider-error':raise TimeoutError('Not for diagnostics')
        if failure=='malformed':return 'not JSON'
        data=json.loads(prompt.split('ДАННЫЕ ЧТЕНИЯ:\n',1)[1])
        return json.dumps({'snapshot_id':data['snapshot_id'],'cards':[{'id':'C1','kind':'rule',
            'passage_ids':['E99:P1'],'related_card_ids':[],'requirement_ids':['R1']}],
            'unresolved_requirement_ids':[]})
    result=read_candidate_frontiers(task,items,rank_context=ranks(task.resolved_user_context),
                                    max_chars=1000,complete=complete)
    assert len(calls)==1 and not result.preferences
    assert result.pool[0]['text']==items[0]['text'] and not result.diagnostics['applied']


@pytest.mark.parametrize('limit',[0,-1,1])
def test_no_model_call_for_empty_or_unfit_input(limit):
    task=UserTask('Explain.','Explain.')
    def unexpected(*args):raise AssertionError('No fitting source: no model call')
    result=read_candidate_frontiers(task,[row('A complete native condition.')],rank_context=ranks('Explain.'),
                                    max_chars=limit,complete=unexpected)
    assert not result.preferences and not result.diagnostics['assessed']
