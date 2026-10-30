import json
import pytest
from answer_draft_protocol import render
from native_source_rows import NativeRow

def encoded(blocks):return json.dumps({'blocks':blocks})

def test_model_source_choices_are_rendered_exactly_without_donating_another_source():
    wire=encoded([{'text':'Cooling fee consists of energy and maintenance.','source_ids':['E2']},
                  {'text':'[[FORMULA:F1]]','source_ids':['E1']}])
    text=render(wire,{'E1','E2'},{'F1':'E1'})
    assert text=='Cooling fee consists of energy and maintenance. [E2]\n\n[[FORMULA:F1]] [E1]'

@pytest.mark.parametrize('block',[
    {'text':'Native rule.','source_ids':[]},
    {'text':'Native rule.','source_ids':['E99']},
    {'text':'Native rule.','source_ids':['E1','E1']},
    {'text':'Native rule. [E2]','source_ids':['E1']},
    {'text':'[[FORMULA:F99]]','source_ids':['E1']},
    {'text':'Native rule.\nUnbound second paragraph.','source_ids':['E1']},
    {'text':'','source_ids':['E1']},
    {'text':'Native rule.','source_ids':['E1'],'invented_proof':True},
])
def test_invalid_authorship_cannot_be_repaired_by_the_renderer(block):
    with pytest.raises(ValueError):render(encoded([block]),{'E1','E2'},{'F1':'E1'})

@pytest.mark.parametrize('wire',['{"blocks":[],"blocks":[]}',
    '{"blocks":[{"text":"A","text":"B","source_ids":["E1"]}]}',
    'An unstructured answer [E1].','{"blocks":[]}'])
def test_ambiguous_or_missing_wire_structure_is_rejected(wire):
    with pytest.raises(ValueError):render(wire,{'E1'},{})


def test_table_rows_keep_separate_author_source_choices_and_literal_cell_values():
    table={'headers':['Quantity','Value'],'rows':[
        {'cells':['Cooling','Seven | measured'],'source_ids':['E1']},
        {'cells':['Heating','Eleven'],'source_ids':['E2']}]}
    text=render(encoded([table]),{'E1','E2'},{})
    assert '| Quantity | Value | Источник |' in text
    assert '| Cooling | Seven \\| measured | [E1] |' in text
    assert '| Heating | Eleven | [E2] |' in text
    assert '[E2]' not in text.splitlines()[2]


@pytest.mark.parametrize('row',[
    {'cells':['Only one cell'],'source_ids':['E1']},
    {'cells':['Cooling','Seven'],'source_ids':['E99']},
    {'cells':['Cooling','Seven'],'source_ids':[]},
    {'cells':['Cooling','[[FORMULA:F99]]'],'source_ids':['E1']},
    {'cells':['Cooling','Seven\nEleven'],'source_ids':['E1']},
    {'cells':['Cooling','Seven'],'source_ids':['E1'],'hidden_owner':'E2'},
])
def test_malformed_table_or_unknown_row_provenance_cannot_be_silently_repaired(row):
    with pytest.raises(ValueError):render(encoded([{'headers':['Quantity','Value'],'rows':[row]}]),{'E1'},{})


def test_renderer_has_no_small_paragraph_count_or_answer_length_quota():
    blocks=[{'text':f'Complete native paragraph number {n}.','source_ids':['E1']} for n in range(350)]
    result=render(encoded(blocks),{'E1'},{})
    assert result.count('[E1]')==350 and 'number 349.' in result


def test_native_formula_owner_must_be_selected_by_the_author_not_filled_by_the_renderer():
    with pytest.raises(ValueError,match='formula-source-not-selected-by-author'):
        render(encoded([{'text':'[[FORMULA:F1]]','source_ids':['E2']}]),{'E1','E2'},{'F1':'E1'})


def test_complete_native_definition_is_copied_without_actor_or_condition_rephrasing():
    row=NativeRow('R1','E1','E1:P1','T1',1,
        'Data source — Organization Z; Condition — only at rest; Unit — kW.','a'*64)
    result=render(encoded([{'row_id':'R1','source_ids':['E1']}]),{'E1','E2'},{},{'R1':row})
    assert result==row.display+' [E1]' and 'operator' not in result


@pytest.mark.parametrize('block',[
    {'row_id':'R99','source_ids':['E1']},
    {'row_id':'R1','source_ids':['E2']},
    {'row_id':'R1','source_ids':['E1'],'display':'A substituted organization.'},
])
def test_model_cannot_forge_native_row_contents_or_borrow_another_source(block):
    row=NativeRow('R1','E1','E1:P1','T1',1,'Data source — Organization Z.','a'*64)
    with pytest.raises(ValueError):render(encoded([block]),{'E1','E2'},{},{'R1':row})
