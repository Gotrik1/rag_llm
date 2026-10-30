"""Oversized literal headers remain bounded, source-bound, and explicitly partial."""
from copy import deepcopy

import pytest

from table_context import (TableContextError, build_table_units, formula_source_segments,
                           parse_table_unit, serialize_table_unit)
from table_model_view import table_model_view


def native_table(*, explicit=True, merged=False, body='Native value and joint condition.'):
    long_header='[IMAGE_OCR: transcription, not verified]\n'+('Neutral qualifier / λ; ' * 95)+'\n[/IMAGE_OCR]'
    headers=['Source label', long_header]
    cells=[{'column_index':i,'column_span':1,'text':value,'vertical_merge':None}
           for i,value in enumerate(headers)]
    body_cells=([{'column_index':0,'column_span':2,'text':body,'vertical_merge':None}] if merged else
                [{'column_index':0,'column_span':1,'text':'Neutral item','vertical_merge':None},
                 {'column_index':1,'column_span':1,'text':body,'vertical_merge':None}])
    return {'table_id':0,'text':'\n'.join(headers)+'\n'+body,'warnings':[],
            'rows':[{'row_index':0,'is_header':explicit,'cells':cells},
                    {'row_index':1,'is_header':False,'cells':body_cells}]}


def built(table, *, identity='native-A', max_tokens=1100, max_chars=2400):
    texts=build_table_units(table,source_identity=identity,tokenizer=list,
                           max_tokens=max_tokens,max_chars=max_chars)
    assert all(len(text)<=min(max_tokens,max_chars) for text in texts)
    units=[parse_table_unit(text) for text in texts]
    assert all(units)
    return texts,units


def reconstruct(fragments):
    """Require source-bound, gap-free, consistent literal coverage, not substrings."""
    assert fragments
    totals={x.get('total_chars',len(x['text'])) for x in fragments}
    assert len(totals)==1
    value=''
    for cell in sorted(fragments,key=lambda x:(x.get('start',0),x.get('end',len(x['text'])))):
        start,end=cell.get('start',0),cell.get('end',len(cell['text']))
        assert len(cell['text'])==end-start and start<=len(value)
        overlap=max(0,min(end,len(value))-start)
        assert value[start:start+overlap]==cell['text'][:overlap]
        value+=cell['text'][max(0,len(value)-start):]
    assert len(value)==totals.pop()
    return value


@pytest.mark.parametrize('explicit',[True,False])
@pytest.mark.parametrize('merged',[True,False])
def test_oversized_header_and_body_preserve_exact_complete_source_with_unknown_views(explicit,merged):
    table=native_table(explicit=explicit,merged=merged,body='Literal body; conditions stay together. '*12)
    original=deepcopy(table)
    texts,units=built(table)
    assert table==original
    table_ids={unit['table_id'] for unit in units}
    assert len(table_ids)==1
    header_cells=[cell for unit in units if unit['row']==0 for cell in unit['cells']]
    for native in table['rows'][0]['cells']:
        assert reconstruct([cell for cell in header_cells if cell['column']==native['column_index']])==native['text']
    body=[unit for unit in units if unit['row']==1]
    partial=[unit for unit in body if any('start' in h for h in unit['headers'])]
    assert partial
    for unit in partial:
        assert unit['header_status']=='unknown'
        assert 'header-view-fragmented-over-budget' in unit['warnings']
        assert unit['headers'] and unit['cells']
        assert all(h['row']==0 for h in unit['headers'])
    projected=[h for unit in partial for h in unit['headers'] if h['column']==1]
    assert reconstruct(projected)==table['rows'][0]['cells'][1]['text']
    for native in table['rows'][1]['cells']:
        fragments=[cell for unit in body for cell in unit['cells'] if cell['column']==native['column_index']]
        assert all(cell['span']==native['column_span'] for cell in fragments)
        assert reconstruct(fragments)==native['text']
    # Native w:tblHeader provenance remains on original header rows; partial
    # projected body views cannot upgrade an unmarked first row to a header.
    assert {unit['row_kind'] for unit in units if unit['row']==0}==({'header'} if explicit else {'body'})
    decoded=table_model_view('\n'.join(texts))
    assert [segment['table'] for segment in decoded['source_segments'] if 'table' in segment]==units


def fragment_payload():
    return {'schema':'table-grid-v1','table_id':'a'*64,'row':1,'row_kind':'body',
            'header_status':'unknown','warnings':['header-view-fragmented-over-budget'],
            'headers':[{'row':0,'column':1,'span':1,'text':'ABC','start':0,'end':3,'total_chars':6}],
            'cells':[{'row':1,'column':1,'span':1,'text':'V','start':0,'end':1,'total_chars':1}],
            'context':[]}


def test_partial_header_codec_roundtrip_preserves_offsets_and_original_field_values():
    payload=fragment_payload()
    assert parse_table_unit(serialize_table_unit(payload))==payload


@pytest.mark.parametrize('mutation',[
    lambda p:p.update(header_status='explicit'),
    lambda p:p.update(header_status='candidate'),
    lambda p:p.update(warnings=[]),
    lambda p:p['headers'][0].update(end=2),
    lambda p:p['headers'][0].update(total_chars=2),
    lambda p:p['headers'][0].pop('end'),
    lambda p:p['headers'][0].update(start=-1),
    lambda p:p['headers'][0].update(start=3,end=3,text=''),
    lambda p:p['headers'][0].update(total_chars=True),
    lambda p:p['headers'][0].update(source_role='invented'),
    lambda p:p['headers'][0].update(row=2),
    lambda p:p['headers'].append(dict(p['headers'][0],start=3,end=6,text='DEF')),
])
def test_malformed_or_confident_partial_header_is_rejected(mutation):
    payload=fragment_payload();mutation(payload)
    with pytest.raises(TableContextError):serialize_table_unit(payload)


def test_partial_header_cannot_attest_full_formula_without_complete_same_physical_coverage():
    left=fragment_payload();right=deepcopy(left)
    left['headers'][0].update(text='[MATH:X=',start=0,end=8,total_chars=12)
    right['headers'][0].update(text='Y+Z]',start=8,end=12,total_chars=12)
    a,b=serialize_table_unit(left),serialize_table_unit(right)
    assert '[MATH:X=Y+Z]' in formula_source_segments(a+'\n'+b)
    assert '[MATH:X=Y+Z]' not in formula_source_segments(a)
    right['table_id']='b'*64
    assert '[MATH:X=Y+Z]' not in formula_source_segments(a+'\n'+serialize_table_unit(right))


def test_inconsistent_overlap_or_gap_cannot_complete_source_header():
    left=fragment_payload();right=deepcopy(left)
    left['headers'][0].update(text='[MATH:X=',start=0,end=8,total_chars=12)
    right['headers'][0].update(text='Q=Y+Z]',start=6,end=12,total_chars=12)
    joined=serialize_table_unit(left)+'\n'+serialize_table_unit(right)
    assert not any('[MATH:' in segment for segment in formula_source_segments(joined))
    right['headers'][0].update(text='+Z]',start=9,end=12,total_chars=12)
    joined=serialize_table_unit(left)+'\n'+serialize_table_unit(right)
    assert not any('[MATH:' in segment for segment in formula_source_segments(joined))


def test_same_native_table_in_another_physical_source_never_aliases_header_fragments():
    table=native_table()
    _,first=built(table,identity='physical-revision-A')
    _,second=built(table,identity='physical-revision-B')
    assert {unit['table_id'] for unit in first}.isdisjoint({unit['table_id'] for unit in second})


def test_impossible_minimum_budget_still_fails_without_bare_body_cells():
    with pytest.raises(TableContextError):
        built(native_table(),max_tokens=450,max_chars=450)
