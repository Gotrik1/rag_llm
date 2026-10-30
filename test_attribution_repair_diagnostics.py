"""Repair receives the exact rejected clause, never an invented source binding."""
import json
import ordinary_answer_guard as guard


def test_uncited_supported_clause_is_located_but_not_automatically_certified():
    source={'file':'guide.md','section_path':'Process','text':'The pump is stopped before opening the valve.'}
    packet=guard.build_source_packet([source],lambda _: 'guide.md — раздел «Process»')
    answer='Stop the pump before opening the valve.'
    verdict={'claims':[{'claim_id':'C1','support':'supported','passage_ids':['E1:P1'],
        'source_binding':'exact_target','material_conditions':'complete'}]}
    checked=guard.assess_answer(answer,packet,'Explain the procedure.',reviewer=lambda _:verdict)
    assert not checked.accepted and checked.repairable and checked.reviewed
    assert checked.diagnostics=={'code':'declared_source_binding_failed','claims':[
        {'claim_id':'C1','claim_text':answer,'declared_source_ids':[],
         'reason':'support_not_bound_to_author_declared_source'}]}
    prompt=guard.repair_prompt('Explain the procedure.',packet,answer,checked)
    assert json.dumps(checked.diagnostics,ensure_ascii=False,sort_keys=True,separators=(',',':')) in prompt
    assert checked.answer==answer


def test_an_uncertain_verdict_is_not_changed_into_a_supported_citation_repair():
    source={'file':'guide.md','section_path':'Process','text':'The valve is closed.'}
    packet=guard.build_source_packet([source],lambda _: 'guide.md — раздел «Process»')
    verdict={'claims':[{'claim_id':'C1','support':'uncertain','passage_ids':[],
        'source_binding':'unassessed','material_conditions':'unassessed'}]}
    checked=guard.assess_answer('The valve can always be opened.',packet,'Explain.',reviewer=lambda _:verdict)
    assert not checked.accepted
    assert checked.diagnostics['code']=='typed_claim_review_failed'
    assert checked.diagnostics['claims'][0]['support']=='uncertain'
    assert 'passage_ids' not in checked.diagnostics['claims'][0]
