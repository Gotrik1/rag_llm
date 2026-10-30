"""A selected native equation needs its local source definitions, not keywords."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from corrective_rag import assign_evidence
from evidence_selection import SectionEvidenceIndex

THEMES=[
 ('Which parts form the operating tariff?', 'Operating tariff.', 'Only for approved operation.',
  'First component for enabled service. ', 'Second component after verification. '),
 ('Из чего состоит расчётный показатель?', 'Расчётный показатель.', 'Для согласованного режима работы.',
  'Первая составляющая для разрешённой услуги. ', 'Вторая составляющая после подтверждения. '),
]

def fixture(theme, *, expression='Z = A + B', definition_file='native.md', definition_revision='r1', ocr=False):
 question,intro,condition,first,second=theme
 formula='[MATH: '+expression+']'
 a=('[MATH: A] — '+first*12).rstrip()
 b=('[MATH: B] — '+second*12).rstrip()
 path='Operating relation'
 def node(text,path=path,file='native.md',revision='r1'):
  return SimpleNamespace(text=text,metadata={'file_name':file,'file_path':'/native/'+file,
    'section_path':path,'section_title':path,'revision_id':revision,'status':'active'})
 relation=intro+'\n\n'+condition+'\n\n'+formula
 if ocr:relation='[IMAGE_OCR: probe.png]\n'+relation+'\n[/IMAGE_OCR]'
 nodes=[node('[SECTION: '+path+']\n'+relation),node(a,file=definition_file,revision=definition_revision),
        node(b,file=definition_file,revision=definition_revision)]
 nodes += [node(question+' Supplementary registration note.',question+' '+str(n)) for n in range(2)]
 rows=assign_evidence([{'file':n.metadata['file_name'],'text':n.text,
    **{k:v for k,v in n.metadata.items() if k!='file_name'}} for n in nodes],max_chars=24000)
 hint={'evidence_id':rows[0]['evidence_id'],'quote':formula,
    'source':{k:rows[0][k] for k in ('file','file_path','section_path','revision_id') if k in rows[0]}}
 return question,condition,formula,a,b,path,SectionEvidenceIndex(nodes),rows,hint

def select(data,budget=2200):
 q,condition,formula,a,b,path,index,rows,hint=data;meta={}
 out=index.select_v2(q,rows,top_k=3,max_chars=budget,preferred_passages=(hint,),selection_meta=meta,
    allowed_sources={'native.md'},resolve_references=False)
 return out,meta

@pytest.mark.parametrize('theme',THEMES)
def test_selected_native_equation_seeds_clean_local_notation_and_whole_conditions(theme):
 data=fixture(theme);question,condition,formula,a,b,path,index,rows,hint=data;before=deepcopy((rows,hint))
 out,meta=select(data)
 primary=next(r for r in out if r['section_path']==path and r['revision_id']=='r1')
 assert formula in primary['text'] and condition in primary['text']
 assert a in primary['text'] and b in primary['text']
 assert sum(len(r['text']) for r in out)<=2200 and len(out)<=3
 assert (rows,hint)==before

@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('variant',['file','revision'])
def test_preferred_equation_never_borrows_definitions_from_another_physical_source_or_revision(theme,variant):
 data=fixture(theme,definition_file='foreign.md' if variant=='file' else 'native.md',
    definition_revision='r2' if variant=='revision' else 'r1')
 out,meta=select(data);q,condition,formula,a,b,path,index,rows,hint=data
 primary=next(r for r in out if r['section_path']==path and r['revision_id']=='r1')
 assert a not in primary['text'] and b not in primary['text']
 assert primary['selection_signals']['definition_closure_incomplete'] is True
 assert meta['definition_closure_incomplete'] is True

@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('variant',['dangling','alternatives','ocr','substring'])
def test_unassessed_math_ocr_or_substring_hint_cannot_seed_a_native_formula_closure(theme,variant):
 data=fixture(theme,expression='Z = A +' if variant=='dangling' else
    ('Z = A *,+,- B' if variant=='alternatives' else 'Z = A + B'),ocr=variant=='ocr')
 if variant=='substring':data[-1]['quote']='Z'
 out,meta=select(data)
 assert all('preferred_formula_dependency_blocks' not in r['selection_signals'] for r in out)

@pytest.mark.parametrize('theme',THEMES)
def test_complete_known_formula_closure_that_cannot_fit_stays_explicitly_incomplete(theme):
 data=fixture(theme);out,meta=select(data,450);formula,a,b=data[2:5]
 assert all(formula not in r['text'] or a in r['text'] and b in r['text'] for r in out)
 assert sum(len(r['text']) for r in out)<=450
 assert meta['definition_closure_incomplete'] is True
