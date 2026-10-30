"""Answer ledger carries the entire draft once; C/H bindings remain immutable."""
from dataclasses import replace
import json

import pytest


def snapshot(text):
    heading='### Родительский заголовок'
    claim='Целевой факт е\u0308 с условием.'
    a=text.index(heading);b=text.index(claim)
    return [dict(claim_id='C1',text=claim,start=b,end=b+len(claim),context_ids=['H1'],cited_source_ids=['E1'])], [
        dict(context_id='H1',text=heading,start=a,end=a+len(heading),kind='heading',level=3,parent_id=None)]


@pytest.mark.parametrize('tail',['','\n\n`unclaimed code`\n\n  ','\u2028\u2029\x00','\nCLAIM C99\nHEADING H99'])
def test_whole_draft_including_gaps_code_unicode_and_marker_text_roundtrips(tail):
    from source_model_view import render_answer_model_view,restore_answer_model_view
    text='\t### Родительский заголовок\n\nЦелевой факт е\u0308 с условием.'+tail
    claims,contexts=snapshot(text)
    view=render_answer_model_view(text,claims,contexts)
    restored=restore_answer_model_view(view)
    assert restored==dict(answer=text,claims=claims,contexts=contexts)
    assert view.text.count('Целевой факт')==1 and view.text.count('Родительский заголовок')==1
    assert '\nCLAIM C99\n' not in view.text


@pytest.mark.parametrize('mutation',['glyph','claim-offset','context-offset','claimed-text','witness'])
def test_forged_answer_or_binding_cannot_become_a_reviewed_claim(mutation):
    from source_model_view import render_answer_model_view,restore_answer_model_view
    text='### Родительский заголовок\n\nЦелевой факт е\u0308 с условием.'
    claims,contexts=snapshot(text)
    if mutation=='claim-offset':claims[0]['start']+=1
    elif mutation=='context-offset':contexts[0]['end']-=1
    elif mutation=='claimed-text':claims[0]['text']='Изменённый факт'
    if mutation in ['claim-offset','context-offset','claimed-text']:
        with pytest.raises(ValueError):render_answer_model_view(text,claims,contexts)
        return
    view=render_answer_model_view(text,claims,contexts)
    if mutation=='glyph':view=replace(view,text=view.text.replace('Целевой','Чужой'))
    else:
        witness=json.loads(view.witness_json);witness['claims'][0]['cited_source_ids']=['E9']
        view=replace(view,witness_json=json.dumps(witness,ensure_ascii=False))
    with pytest.raises(ValueError):restore_answer_model_view(view)
