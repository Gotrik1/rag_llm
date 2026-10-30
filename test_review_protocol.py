"""Compact transport is not permission to add or certify claim verdicts."""
import json
from copy import deepcopy
import pytest
import ordinary_answer_guard as guard
from review_protocol import COLUMNS, compact_review_prompt, decode_compact_review, decode_review_response


FACT='The valve is opened only after the pump is stopped.'
def packet():
    return guard.build_source_packet([{'file':'neutral.md','section_path':'Procedure','text':FACT}],
        lambda _: 'neutral.md — раздел «Procedure»')


def test_transport_preserves_the_entire_source_and_answer_payload_and_all_field_constraints():
    native=packet();before=deepcopy(native)
    original=guard._review_prompt(FACT+' [E1]',native,'Explain the procedure.')
    compact=compact_review_prompt(original)
    assert original.split('\nДАННЫЕ:\n')[1] == compact.split('\nДАННЫЕ:\n')[1]
    assert compact.startswith(original.rsplit('\nJSON_SCHEMA:\n',1)[0])
    old=json.loads(original.split('\nJSON_SCHEMA:\n')[1].split('\nДАННЫЕ:\n')[0])
    new=json.loads(compact.split('\nJSON_SCHEMA:\n')[1].split('\nДАННЫЕ:\n')[0])
    fields=new['properties']['claims']['items']['prefixItems']
    for number,name in enumerate(COLUMNS[1:],1):
        assert fields[number]==old['properties']['claims']['items']['properties'][name]
    assert new['properties']['claims']['minItems']==new['properties']['claims']['maxItems']==1
    assert native==before


@pytest.mark.parametrize('support', ['supported','unsupported','contradicted','uncertain'])
def test_expansion_does_not_change_any_explicit_verdict_or_passage(support):
    row=['C1',support,['E1:P1'],'other_target','incomplete']
    raw={'claims':[row]};before=deepcopy(raw)
    assert decode_compact_review(json.dumps(raw)) == {'claims':[dict(zip(COLUMNS,row))]}
    assert raw==before


@pytest.mark.parametrize('row', [[],['C1','supported',[],'exact_target'],
    ['C1','supported',[],'exact_target','complete','extra'],
    ['C1',True,[],'exact_target','complete'],['C1','supported','E1:P1','exact_target','complete'],
    ['C1','supported',[],'guessed','complete'],['C1','supported',[],'exact_target',True]])
def test_missing_extra_or_malformed_values_fail_closed(row):
    with pytest.raises(ValueError):decode_compact_review(json.dumps({'claims':[row]}))


def test_duplicate_claims_keys_and_model_supplied_columns_cannot_change_field_meaning():
    row=['C1','supported',['E1:P1'],'exact_target','complete']
    with pytest.raises(ValueError):decode_compact_review(json.dumps({'claims':[row,row]}))
    with pytest.raises(ValueError):decode_compact_review('{"claims":[],"claims":[]}')
    with pytest.raises(ValueError):decode_compact_review(json.dumps({'columns':list(COLUMNS),'claims':[row]}))


def test_legacy_object_rows_are_preserved_for_the_same_strict_native_validator():
    value={'claims':[{'claim_id':'C1','support':'uncertain','passage_ids':[],
                     'source_binding':'unassessed','material_conditions':'unassessed'}]}
    assert decode_review_response(json.dumps(value))==value
    with pytest.raises(ValueError):decode_compact_review(json.dumps(value))
    with pytest.raises(ValueError):decode_review_response(json.dumps({'claims':[value['claims'][0],
        ['C2','supported',[],'exact_target','complete']]}))


@pytest.mark.parametrize('pid,support,accepted',[('E1:P1','supported',True),
    ('E99:P1','supported',False),('E1:P1','contradicted',False)])
def test_existing_publication_validator_still_checks_source_and_semantic_verdict(pid,support,accepted):
    native=packet()
    def review(prompt):
        compact_review_prompt(prompt)
        return decode_compact_review(json.dumps({'claims':[['C1',support,[pid],'exact_target','complete']]}))
    # Rephrasing prevents the exact-source-copy shortcut.
    result=guard.assess_answer('Open the valve only after stopping the pump. [E1]',native,
        'Explain the procedure.',reviewer=review)
    assert result.accepted is accepted
