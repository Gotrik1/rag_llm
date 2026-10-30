"""Server structural framing must not erase a validated native reader quote."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from corrective_rag import assign_evidence
from evidence_selection import SectionEvidenceIndex

THEMES = [
 ('Sensor inspection interval', 'Sensor inspection', 'Maintenance scenario only.',
  'The sensor inspection interval is 12 hours after shutdown.'),
 ('Каков срок проверки заявок?', 'Проверка заявок', 'Для согласованных заявок после проверки допуска.',
  'Срок проверки заявки составляет 18 часов после регистрации.'),
]
FIELDS=('file','section_path','document_id','source_id','revision_id','version_id','document_version','content_hash','sha256')
def fixture(theme,variant='normal'):
 question,path,condition,rule=theme
 overview=(question+' Background registration and records.\n\n')*16
 body=condition+'\n\n'+rule
 marker='[SECTION: '+path+']'
 text=marker+'\n'+body
 if variant=='wrong-path':text='[SECTION: Different declared path]\n'+body
 if variant=='nested':text='Native preamble.\n\n'+marker+'\n'+body
 if variant=='code':text='```text\n'+marker+'\n'+body+'\n```'
 if variant=='nested-after-prefix':text=marker+'\nNative preamble.\n\n'+marker+'\n'+body
 if variant=='unfit':text=marker+'\n'+('Applicability retained in full. '*20)+'\n\n'+rule
 def node(value):return SimpleNamespace(text=value,metadata={'file_name':'policy.md','file_path':'/native/policy.md',
   'section_path':path,'section_title':path,'status':'active','revision_id':'r1'})
 nodes=[node(overview),node(text)];index=SectionEvidenceIndex(nodes)
 rows=assign_evidence([{'file':n.metadata['file_name'],'text':n.text,**{k:v for k,v in n.metadata.items() if k!='file_name'}} for n in nodes],max_chars=24000)
 hint={'evidence_id':rows[1]['evidence_id'],'quote':rows[1]['text'],'source':{k:rows[1][k] for k in FIELDS if k in rows[1]}}
 return index,rows,hint,question,body,marker,rule
def run(index,rows,hint,question,budget=12000):
 meta={};out=index.select_v2(question,rows,top_k=1,max_chars=budget,allowed_sources={'policy.md'},
   preferred_passages=(hint,),selection_meta=meta,resolve_references=False)
 return out,meta

@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('budget',[260,12000])
def test_trusted_matching_prefix_maps_while_raw_quote_and_conditions_stay_exact(theme,budget):
 index,rows,hint,q,body,marker,rule=fixture(theme);before=deepcopy((rows,hint,index.body_items))
 out,meta=run(index,rows,hint,q,budget)
 assert len(out)==1 and body in out[0]['text']
 assert out[0]['selection_signals']['source_reading_preferred'] is True
 assert meta['preferred_passage_retention']=={'requested_count':1,'retained_count':1,'unretained_count':0}
 assert hint['quote'].startswith(marker+'\n') and hint['quote'] not in out[0]['text']
 assert out[0]['file']=='policy.md' and out[0]['file_path']=='/native/policy.md' and out[0]['revision_id']=='r1'
 assert (rows,hint,index.body_items)==before

@pytest.mark.parametrize('theme',THEMES)
def test_body_only_pid_still_requires_the_entire_projected_origin_condition(theme):
 index,rows,hint,q,body,marker,rule=fixture(theme);hint['quote']=rule
 out,meta=run(index,rows,hint,q,260)
 assert len(out)==1 and body in out[0]['text']
 assert out[0]['selection_signals']['source_reading_preferred'] is True
 assert meta['preferred_passage_retention']['retained_count']==1

@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('variant',['wrong-path','nested','code','nested-after-prefix'])
def test_literal_wrong_nested_or_code_markers_cannot_gain_mapping(theme,variant):
 index,rows,hint,q,body,marker,rule=fixture(theme,variant)
 out,meta=run(index,rows,hint,q)
 assert not any(r['selection_signals']['source_reading_preferred'] for r in out)
 assert meta['preferred_passage_retention']['unretained_count']==1

@pytest.mark.parametrize('theme',THEMES)
@pytest.mark.parametrize('mutation',['file','revision_id','section_path','id','metadata-only','non-native-origin'])
def test_source_identity_raw_origin_and_material_quote_proof_are_mandatory(theme,mutation):
 index,rows,hint,q,body,marker,rule=fixture(theme)
 if mutation=='id':hint['evidence_id']='E999'
 elif mutation=='metadata-only':hint['quote']=marker
 elif mutation=='non-native-origin':
  index=SectionEvidenceIndex([SimpleNamespace(text=body,metadata={'file_name':'policy.md','file_path':'/native/policy.md',
    'section_path':theme[1],'section_title':theme[1],'status':'active','revision_id':'r1'})])
 elif mutation=='file':hint['source']['file']='foreign.md'
 else:hint['source'][mutation]='other-native-value'
 out,meta=run(index,rows,hint,q)
 assert not any(r['selection_signals']['source_reading_preferred'] for r in out)

@pytest.mark.parametrize('theme',THEMES)
def test_unfit_whole_origin_is_never_cut_to_naked_mapped_rule(theme):
 index,rows,hint,q,body,marker,rule=fixture(theme,'unfit');hint['quote']=rule
 out,meta=run(index,rows,hint,q,170)
 origin=rows[1]['text'].split('\n',1)[1]
 assert all(rule not in r['text'] or origin in r['text'] for r in out)
 assert not any(r['selection_signals']['source_reading_preferred'] for r in out)
 assert meta['preferred_passage_retention_incomplete'] is True
