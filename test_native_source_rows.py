from copy import deepcopy
from dataclasses import replace
import pytest

from native_source_rows import build_native_rows
from ordinary_answer_guard import build_source_packet
from rag_agent import format_formula_for_katex
from table_context import build_table_units, parse_table_unit, serialize_table_unit
from test_table_context import sample_table


def unit():
    return next(parse_table_unit(text) for text in build_table_units(sample_table(),
        source_identity='native-A',tokenizer=list,max_tokens=2400) if parse_table_unit(text)['row']==1)


def packet(value=None,**extra):
    source={'file':'neutral.md','section_path':'Native','text':serialize_table_unit(value or unit()),**extra}
    return build_source_packet([source],lambda row:'neutral.md — раздел «Native»')


def set_text(cell,text):
    cell.update(text=text,start=0,end=len(text),total_chars=len(text))


def test_full_native_row_preserves_actual_role_headers_values_and_source_binding():
    native=packet();before=native.source_json
    rows=build_native_rows(native,format_formula_for_katex)
    assert len(rows)==1
    row=rows[0]
    assert row.evidence_id=='E1' and row.passage_id.startswith('E1:P') and row.table_alias=='T1'
    assert 'Data source — Organization Z' in row.display and 'Storage — Manual entry' in row.display
    assert 'operator' not in row.display and native.source_json==before
    assert 'display' not in row.descriptor()


def test_scalar_math_is_typeset_only_after_an_exact_normalized_roundtrip():
    value=unit();set_text(value['cells'][0],'[MATH: Q_sub(a,2)]')
    rows=build_native_rows(packet(value),format_formula_for_katex)
    assert rows and '$Q_{a,2}$' in rows[0].display
    assert not build_native_rows(packet(value),lambda raw:'$Q_{a,3}$')


@pytest.mark.parametrize('kind',['unknown-header','partial-header','partial-cell',
    'unresolved-merge','ocr','unknown-math','equation'])
def test_incomplete_or_unverified_material_is_not_upgraded_to_a_native_definition(kind):
    value=unit()
    if kind=='unknown-header':value['header_status']='unknown'
    elif kind=='partial-header':
        h=value['headers'][0];h.update(start=0,end=len(h['text']),total_chars=len(h['text'])+9)
        value['header_status']='unknown';value['warnings']=['header-view-fragmented-over-budget']
    elif kind=='partial-cell':
        c=value['cells'][0];c.update(start=0,end=len(c['text']),total_chars=len(c['text'])+9)
    elif kind=='unresolved-merge':value['cells'][0]['merge_unresolved']=True
    elif kind=='ocr':set_text(value['cells'][0],'[IMAGE_OCR: uncertain]')
    elif kind=='unknown-math':set_text(value['cells'][0],'[[MATHML_REF:'+'a'*24+']]')
    elif kind=='equation':set_text(value['cells'][0],'[MATH: X=A+B]')
    assert not build_native_rows(packet(value),format_formula_for_katex)


def test_complete_candidate_headers_are_quoted_as_data_and_never_upgraded_to_verified_roles():
    value=unit();value['header_status']='candidate'
    rows=build_native_rows(packet(value),format_formula_for_katex)
    assert len(rows)==1 and rows[0].header_status=='candidate'
    assert 'Исходная строка' in rows[0].display and 'Organization Z' in rows[0].display
    assert 'Data source — Organization Z' not in rows[0].display


def test_different_revisions_remain_separate_and_same_row_number_cannot_cross_sources():
    sources=[{'file':'neutral.md','section_path':'Native','revision_id':revision,
        'text':serialize_table_unit(unit())} for revision in ('v1','v2')]
    native=build_source_packet(sources,lambda row:'neutral.md — раздел «Native»')
    rows=build_native_rows(native,format_formula_for_katex)
    assert len(rows)==2 and {r.evidence_id for r in rows}=={'E1','E2'}
    assert rows[0].row_id!=rows[1].row_id


def test_forged_passage_binding_and_plain_marker_examples_have_no_copy_authority():
    native=packet()
    forged=replace(native,anchors_json='[]')
    with pytest.raises(ValueError,match='passage-binding'):
        build_native_rows(forged,format_formula_for_katex)
    source={'file':'neutral.md','text':'```text\n'+serialize_table_unit(unit())+'\n```'}
    assert not build_native_rows(build_source_packet([source],lambda row:'neutral.md'),format_formula_for_katex)


def test_conflicting_complete_payloads_for_one_physical_row_are_not_arbitrarily_chosen():
    one=unit();two=deepcopy(one);set_text(two['cells'][1],'A different organization')
    source={'file':'neutral.md','text':serialize_table_unit(one)+'\n\n'+serialize_table_unit(two)}
    native=build_source_packet([source],lambda row:'neutral.md')
    assert not build_native_rows(native,format_formula_for_katex)
