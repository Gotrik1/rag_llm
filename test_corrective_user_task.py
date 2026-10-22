"""Task authority contracts, using new synthetic facts rather than corpus gold."""
from dataclasses import FrozenInstanceError
import json

import pytest

import corrective_rag
from corrective_rag import SearchPlan, assess_coverage, run_corrective
from user_task import UserTask


STORAGE = 'The storage fee is seven tokens per box.'
QUESTION = 'What is the Velora surcharge?'
EVIDENCE = [{'evidence_id':'E1','file':'Storage.md','section_path':'Storage fee','text':STORAGE}]


def payload(prompt, marker):
    return json.JSONDecoder().raw_decode(prompt[prompt.index(marker):])[0]


def coverage_response(status='matched', passage_ids=None):
    return {'coverage':[{'requirement_id':'R1','passage_ids':['E1:P1'],'applicable':True}],
            'task_alignment':{'status':status,'passage_ids':['E1:P1'] if passage_ids is None else passage_ids},
            'missing_queries':['Look up the Velora surcharge'], 'conflicts':[]}


def test_user_task_retains_exact_unicode_whitespace_and_is_immutable():
    raw = '  А как её считать?\r\n'
    context = 'Предыдущий пользователь: Величина Ź-42\n' + (' контекст ' * 1000)
    task = UserTask(raw,context)
    assert task.to_payload() == {'question':raw,'resolved_user_context':context}
    with pytest.raises(FrozenInstanceError):
        task.question = 'rewritten'
    detached = task.to_payload()
    detached['question'] = 'changed'
    assert task.question == raw


@pytest.mark.parametrize('raw,context',[(None,'q'),('q',None),(12,'q'),('q',{})])
def test_user_task_does_not_coerce_untrusted_non_string_inputs(raw,context):
    with pytest.raises(TypeError):
        UserTask(raw,context)


@pytest.mark.parametrize('alignment',[
    None, {}, {'status':'unknown','passage_ids':[]},
    {'status':'matched','passage_ids':[]},
    {'status':'matched','passage_ids':['E9:P1']},
    {'status':'matched','passage_ids':['E1:P1','E1:P1']},
    {'status':'matched','passage_ids':'E1:P1'},
    {'status':'matched','passage_ids':[1]},
])
def test_missing_or_malformed_alignment_cannot_certify_plan_coverage(alignment):
    response = coverage_response()
    response['task_alignment'] = alignment
    result = assess_coverage(SearchPlan(('Storage fee',),('Storage fee',)),EVIDENCE,
                             lambda *_:json.dumps(response),user_task=UserTask(QUESTION,QUESTION))
    assert result['covered_requirements'] == ['R1']
    assert result['task_alignment']['status'] == 'unassessed'
    assert not result['complete']
    assert not result['conflicts']  # Unassessed is not a factual contradiction.


@pytest.mark.parametrize('status',['mismatch','uncertain'])
def test_real_storage_quote_does_not_answer_an_unrelated_original_subject(status):
    calls = []
    def complete(prompt,purpose):
        calls.append((purpose,payload(prompt,'{"requirements":')))
        return json.dumps(coverage_response(status,[]))
    result = assess_coverage(SearchPlan(('Storage fee',),('Storage fee',)),EVIDENCE,complete,
                             user_task=UserTask(QUESTION,QUESTION))
    assert result['covered_requirements'] == ['R1']
    assert result['task_alignment'] == {'status':status,'passage_ids':[]}
    assert not result['complete'] and result['assessed']
    assert calls[0][1]['user_task']['question'] == QUESTION
    assert calls[0][1]['requirements'][0]['text'] == 'Storage fee'
    assert len(calls) == 1


def test_direct_legacy_coverage_without_task_preserves_old_contract():
    response = coverage_response()
    del response['task_alignment']
    result = assess_coverage(SearchPlan(('Storage fee',),('Storage fee',)),EVIDENCE,
                             lambda *_:json.dumps(response))
    assert result['complete'] and 'task_alignment' not in result


class TaskModel:
    def __init__(self, *, wrong_subject=False):
        self.calls = []
        self.wrong_subject = wrong_subject

    def __call__(self,prompt,stage):
        self.calls.append((stage,prompt))
        if stage == 'planning':
            return json.dumps({'canonical_question':'What is the storage fee?',
                               'requirements':['Storage fee'],'queries':['storage fee'],
                               'search_focus':{'operation':'lookup','subjects':[{'text':'storage fee'}]}})
        if stage == 'answer':
            return STORAGE + ' [E1]'
        if 'Проверь доказательность ответа' in prompt:
            data = payload(prompt,'{"question":')
            return json.dumps({'claims':[{'claim_id':row['claim_id'],'support':'supported',
                                          'passage_ids':['E1:P1'],
                                          'task_relevance':'mismatch' if self.wrong_subject else 'matched'}
                                         for row in data['answer_claims']]})
        return json.dumps(coverage_response('mismatch' if self.wrong_subject else 'matched',
                                            [] if self.wrong_subject else ['E1:P1']))


def test_wrong_plan_cannot_publish_unrelated_grounded_fact_and_recovery_revokes_focus():
    model, searches = TaskModel(wrong_subject=True), []
    def retrieve(task,queries,requirements,trace,*,search_focus,constraint_question):
        searches.append((task,queries,requirements,search_focus,constraint_question))
        return EVIDENCE
    result = run_corrective(QUESTION,QUESTION,retrieve=retrieve,complete=model)
    assert len(searches) == 2
    assert searches[0][3] is not None
    assert searches[1] == (QUESTION,[QUESTION],[QUESTION],None,QUESTION)
    assert [row['task_recovery'] for row in result.trace['rounds']] == [False,True]
    assert not result.trace['coverage_complete']
    assert STORAGE not in result.answer
    assert 'Не удалось надёжно сопоставить' in result.answer
    assert len([stage for stage,_ in model.calls if stage == 'planning']) == 1
    assert len([stage for stage,_ in model.calls if stage == 'answer']) == 2
    checks = result.trace['verification']['initial']['checks']
    assert any(row['kind']=='claim' and row['status']=='judge_supported' for row in checks)
    assert any(row['kind']=='relevance' and row['status']=='mismatch' for row in checks)


def test_same_authoritative_object_reaches_coverage_writer_judge_and_repair(monkeypatch):
    import grounding_verifier
    seen = []
    for module,name in [(corrective_rag,'assess_coverage'),(corrective_rag,'generation_prompt'),
                        (grounding_verifier,'build_grounding_judge_prompt'),
                        (grounding_verifier,'verify_grounding'),(grounding_verifier,'safe_partial_answer')]:
        original = getattr(module,name)
        def observed(*args,_fn=original,_name=name,**kwargs):
            seen.append((_name,kwargs['user_task']))
            return _fn(*args,**kwargs)
        monkeypatch.setattr(module,name,observed)
    original_repair = grounding_verifier.GroundingReport.repair_prompt
    def repair(self,**kwargs):
        seen.append(('repair_prompt',kwargs['user_task']))
        return original_repair(self,**kwargs)
    monkeypatch.setattr(grounding_verifier.GroundingReport,'repair_prompt',repair)
    raw = 'How is it calculated?'
    resolved = 'Previous USER question: What is the Velora surcharge?\nCurrent USER question: ' + raw
    model = TaskModel(wrong_subject=True)
    run_corrective(raw,resolved,retrieve=lambda *a,**k:EVIDENCE,complete=model)
    task = seen[0][1]
    assert all(value is task for _,value in seen)
    assert task == UserTask(raw,resolved)
    assert {'assess_coverage','generation_prompt','build_grounding_judge_prompt','verify_grounding',
            'safe_partial_answer','repair_prompt'} == {name for name,_ in seen}
    for stage,prompt in model.calls:
        if stage != 'planning':
            assert resolved in prompt.replace('\\n','\n')
            assert prompt.count('"user_task":') == 1


def test_successful_assessment_has_no_extra_model_call_or_unnecessary_repair():
    model = TaskModel()
    result = run_corrective('What is the storage fee?','What is the storage fee?',
                            retrieve=lambda *a,**k:EVIDENCE,complete=model)
    assert result.answer == STORAGE + ' [E1]'
    assert result.trace['coverage_complete']
    assert result.trace['verification']['initial']['assessment_complete']
    assert [stage for stage,_ in model.calls] == ['planning','evidence-review','answer','evidence-review']
