"""A numbered Markdown heading is one immutable context, never sentence debris."""
import pytest
from grounding_verifier import _answer_structure


@pytest.mark.parametrize('title',['#### 1. Alpha mode','#### 1. Режим А','**1. Alpha mode**',
    '### Section 1. Alpha mode'])
def test_numbered_heading_is_not_split_into_uncited_claim_fragments(title):
    text=title+'\n\nThe action is permitted only after shutdown. [E1]'
    claims,headings=_answer_structure(text)
    assert len(claims)==1
    assert claims[0]['text']=='The action is permitted only after shutdown. [E1]'
    assert len(headings)==1 and headings[0]['text']==title
    assert claims[0]['context_ids']==[headings[0]['context_id']]


def test_unsupported_heading_condition_remains_in_immutable_body_context():
    title='#### 1. Allowed even while the pump is running'
    text=title+'\n\nOpen the valve. [E1]'
    claims,headings=_answer_structure(text)
    assert len(claims)==1
    assert headings[0]['text']==title and text[headings[0]['start']:headings[0]['end']]==title
    assert claims[0]['context_ids']==['H1']


def test_orphan_factual_heading_still_requires_its_own_verification():
    claims,_=_answer_structure('#### 1. The valve can always be opened')
    assert len(claims)>=1 and any('always' in row['text'] for row in claims)


def test_ordinary_numbered_list_body_and_its_two_sentences_are_not_suppressed():
    claims,_=_answer_structure('1. Open the valve. Stop the pump. [E1]')
    assert len(claims)==2
    assert claims[0]['text']=='1. Open the valve.'
    assert claims[1]['text']=='Stop the pump. [E1]'
