"""Trusted reader order is a soft source signal, never a factual certificate."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from corrective_rag import assign_evidence
from evidence_selection import SectionEvidenceIndex
from test_preferred_structural_prefix import THEMES,FIELDS

def fixture(theme):
 q,path,condition,rule=theme
 records=[(q,q+'\n\nAncillary documentation and registration detail.'),
          (path,q+'\n\n'+condition+'\n\n'+rule)]
 nodes=[SimpleNamespace(text='[SECTION: '+p+']\n'+text,
    metadata={'file_name':'policy.md','file_path':'/native/policy.md','section_path':p,
              'section_title':p,'status':'active','revision_id':'r1'}) for p,text in records]
 rows=assign_evidence([{'file':n.metadata['file_name'],'text':n.text,
    **{k:v for k,v in n.metadata.items() if k!='file_name'}} for n in nodes],max_chars=24000)
 hints=[{'evidence_id':r['evidence_id'],'quote':r['text'],
         'source':{k:r[k] for k in FIELDS if k in r}} for r in rows]
 return SectionEvidenceIndex(nodes),rows,hints,q,condition,rule

@pytest.mark.parametrize('theme',THEMES)
def test_first_validated_reader_passage_precedes_a_stronger_same_topic_heading(theme):
 index,rows,hints,q,condition,rule=fixture(theme);ordered=(hints[1],hints[0]);before=deepcopy((rows,ordered))
 out=index.select_v2(q,rows,top_k=1,max_chars=12000,preferred_passages=ordered,resolve_references=False)
 assert len(out)==1 and condition in out[0]['text'] and rule in out[0]['text']
 assert out[0]['selection_signals']['source_reading_preferred'] is True
 assert (rows,ordered)==before

@pytest.mark.parametrize('theme',THEMES)
def test_reversing_trusted_order_changes_preference_without_touching_native_scores(theme):
 index,rows,hints,q,condition,rule=fixture(theme);before=deepcopy(rows)
 a=index.select_v2(q,rows,top_k=1,max_chars=12000,preferred_passages=tuple(hints),resolve_references=False)
 b=index.select_v2(q,rows,top_k=1,max_chars=12000,preferred_passages=tuple(reversed(hints)),resolve_references=False)
 assert a[0]['section_path']!=b[0]['section_path']
 assert rows==before

@pytest.mark.parametrize('theme',THEMES)
def test_forged_or_unmatched_early_hint_and_metadata_cannot_acquire_reader_order(theme):
 index,rows,hints,q,condition,rule=fixture(theme);bad=deepcopy(hints[1]);bad['evidence_id']='E999'
 rows[1]['_reader_preference_order']=0;rows[1]['_reader_preference_ordinal']=0
 out=index.select_v2(q,rows,top_k=1,max_chars=12000,preferred_passages=(bad,hints[0],hints[1]),resolve_references=False)
 assert out and out[0]['section_path']==rows[0]['section_path']
