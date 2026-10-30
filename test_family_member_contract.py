"""Member IDs are server-owned; claim support still needs exact native evidence."""
from copy import deepcopy
import json

import pytest

import ordinary_answer_guard as guard
from test_ordinary_answer_guard import family_packet, source, ref


def packet_with_settings():
    records,_=family_packet()
    records.append({**source('Параметры задают явно в настройках.'),'section_path':'Настройки',
                    'source_id':'source-settings'})
    return records,guard.build_source_packet(records,ref)


def answer(records):
    return '\n\n'.join(row['text']+f' [E{n}]' for n,row in enumerate(records,1))


def reply(prompt):
    data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
    pids={row['evidence_id']:[p['passage_id'] for p in row['passages']] for row in data['evidence']}
    return {'claims':[{'claim_id':row['claim_id'],'support':'supported',
        'passage_ids':[p for eid in row['cited_source_ids'] for p in pids[eid]],
        'source_binding':'exact_target','material_conditions':'complete'} for row in data['answer_claims']],
        'family_coverage':[{'member_id':row['member_id'],'status':'detailed',
            'claim_ids':row['eligible_claim_ids'],'passage_ids':row['allowed_passage_ids']}
            for row in data['family_review_members']]}


def test_members_are_exact_native_children_not_all_sources_and_ordinary_settings_still_reviewed():
    records,packet=packet_with_settings();before=deepcopy(packet)
    prompt=guard._review_prompt(answer(records),packet,'Объясни варианты и параметры.')
    data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1]);members=data['family_review_members']
    assert [row['member_id'] for row in members]==['M1','M2']
    assert [row['source_ids'] for row in members]==[['E1'],['E2']]
    assert [row['eligible_claim_ids'] for row in members]==[['C1'],['C2']]
    assert [row['allowed_passage_ids'] for row in members]==[['E1:P1'],['E2:P1']]
    assert [row['claim_id'] for row in data['answer_claims']]==['C1','C2','C3']
    assert data['answer_claims'][2]['cited_source_ids']==['E3']
    assert packet==before
    result=guard.assess_answer(answer(records),packet,'Объясни варианты и параметры.',reviewer=reply)
    assert result.accepted and result.reviewed


@pytest.mark.parametrize('mutation',['missing','extra-settings','duplicate','unknown','cross-source','cross-claim','label-only','settings-unsupported'])
def test_unknown_or_crossbound_member_cannot_certify_family_or_hide_ordinary_claim(mutation):
    records,packet=packet_with_settings()
    def reviewer(prompt):
        value=reply(prompt);rows=value['family_coverage']
        if mutation=='missing':rows.pop()
        elif mutation=='extra-settings':rows.append({'member_id':'M3','status':'detailed','claim_ids':['C3'],'passage_ids':['E3:P1']})
        elif mutation=='duplicate':rows[1]['member_id']='M1'
        elif mutation=='unknown':rows[1]['member_id']='M99'
        elif mutation=='cross-source':rows[0]['passage_ids']=['E2:P1']
        elif mutation=='cross-claim':rows[0]['claim_ids']=['C2']
        elif mutation=='label-only':rows[0]['status']='label_only'
        else:value['claims'][2]['support']='unsupported';value['claims'][2]['passage_ids']=[]
        return value
    result=guard.assess_answer(answer(records),packet,'Объясни варианты и параметры.',reviewer=reviewer)
    assert not result.accepted
    with pytest.raises(ValueError):guard.cache_proof(packet,'Объясни варианты и параметры.',result.answer,result)


@pytest.mark.parametrize('mutation',['revision','foreign-file','native-text'])
def test_member_id_does_not_upgrade_a_different_physical_revision_or_source_text(mutation):
    records,_=family_packet()
    if mutation=='revision':records[0]['revision_id']='changed'
    elif mutation=='foreign-file':records[0]['file']='other.md'
    else:records[0]['text']='Изменённый алгоритм другой редакции.'
    packet=guard.build_source_packet(records,ref)
    def reviewer(prompt):
        value=reply(prompt)
        value['family_coverage'][0].update(status='detailed',claim_ids=['C1'],passage_ids=['E1:P1'])
        return value
    assert not guard.assess_answer(answer(records),packet,'Объясни варианты.',reviewer=reviewer).accepted


def test_member_response_cannot_smuggle_an_expanded_parent_identity_or_unknown_keys():
    records,packet=packet_with_settings()
    def reviewer(prompt):
        value=reply(prompt)
        value['family_coverage'][0].update(family_id='family-A',section_path='Настройки')
        return value
    assert not guard.assess_answer(answer(records),packet,'Объясни варианты.',reviewer=reviewer).accepted


def test_member_contract_is_bound_into_cache_proof_and_v5_is_stale():
    records,packet=packet_with_settings();draft=answer(records)
    result=guard.assess_answer(draft,packet,'Объясни варианты.',reviewer=reply)
    assert result.accepted
    proof=guard.cache_proof(packet,'Объясни варианты.',result.answer,result)
    assert guard.valid_cache_proof(proof,packet,'Объясни варианты.',result.answer)
    assert len(proof['family_review_contract_sha256'])==64
    assert not guard.valid_cache_proof({**proof,'family_review_contract_sha256':'0'*64},packet,'Объясни варианты.',result.answer)
    assert not guard.valid_cache_proof({**proof,'version':'ordinary-publication-v5'},packet,'Объясни варианты.',result.answer)


def test_no_family_keeps_the_existing_claim_schema_without_extra_required_model_work():
    records=[source('Параметр задают явно.')];packet=guard.build_source_packet(records,ref)
    prompt=guard._review_prompt('Условия задают вручную. [E1]',packet,'Как задают параметр?')
    data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
    assert not data.get('family_review_members')
    schema=json.loads(prompt.split('\nJSON_SCHEMA:\n',1)[1].split('\nДАННЫЕ:\n',1)[0])
    assert 'family_coverage' not in schema['required']
