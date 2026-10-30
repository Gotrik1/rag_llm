"""Bounded repair must know which judged claim failed without inventing support."""
from copy import deepcopy
import json
import pytest
import ordinary_answer_guard as guard

@pytest.mark.parametrize('support,binding,conditions',[
    ('uncertain','unassessed','unassessed'),('unsupported','unassessed','unassessed'),
    ('contradicted','other_target','incomplete'),('supported','other_target','complete'),
    ('supported','exact_target','incomplete')])
def test_failed_semantic_claim_is_reported_verbatim_with_original_verdict(support,binding,conditions):
    source={'file':'process.md','section_path':'Procedure','text':'The valve opens after the pump stops.'}
    packet=guard.build_source_packet([source],lambda _: 'process.md — раздел «Procedure»')
    answer='The valve can always be opened. [E1]';before=packet.source_json
    verdict={'claims':[{'claim_id':'C1','support':support,'passage_ids':['E1:P1'] if support=='supported' else [],
        'source_binding':binding,'material_conditions':conditions}]};original=deepcopy(verdict)
    result=guard.assess_answer(answer,packet,'Explain the procedure.',reviewer=lambda _:verdict)
    assert not result.accepted and result.repairable and result.reviewed
    assert result.diagnostics is not None
    row=result.diagnostics['claims'][0]
    assert row['claim_id']=='C1' and row['claim_text']==answer
    assert (row['support'],row['source_binding'],row['material_conditions'])==(support,binding,conditions)
    assert 'passage_ids' not in row,'The repair hint does not assign evidence for a rejected claim'
    assert packet.source_json==before and verdict==original
    prompt=guard.repair_prompt('Explain the procedure.',packet,result.answer,result)
    assert json.dumps(result.diagnostics,ensure_ascii=False,sort_keys=True,separators=(',',':')) in prompt

def test_repair_receives_failed_claim_and_can_replace_it_with_supported_native_text():
    source={'file':'process.md','section_path':'Procedure','text':'The valve opens after the pump stops.'}
    packet=guard.build_source_packet([source],lambda _: 'process.md — раздел «Procedure»')
    first='The valve can always be opened. [E1]';prompts=[]
    verdict={'claims':[{'claim_id':'C1','support':'uncertain','passage_ids':[],
        'source_binding':'unassessed','material_conditions':'unassessed'}]}
    def repair(prompt):
        prompts.append(prompt)
        assert 'typed_claim_review_failed' in prompt and '"support":"uncertain"' in prompt
        return source['text']+' [E1]'
    result=guard.publish_draft(first,packet,'Explain the procedure.',reviewer=lambda _:verdict,repairer=repair)
    assert result.accepted and len(prompts)==1
