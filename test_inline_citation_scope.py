"""A terminal paragraph-line citation can declare its preceding sentence group."""
from copy import deepcopy
import pytest
from citation_scopes import compile_publication
from grounding_verifier import extract_answer_claims


def source(eid,file,revision='r1'):
    return {'evidence_id':eid,'file':file,'section_path':'Procedure','revision_id':revision,
        'source_reference':file+' — раздел «Procedure»','text':'Alpha requires shutdown. Beta is recorded.'}


SOURCES=[source('E1','first.md'),source('E2','second.md')]
def binding(projection,phrase):
    rows=[x for x in extract_answer_claims(projection.verification) if phrase in x['text']]
    assert len(rows)==1
    return projection.bindings[rows[0]['claim_id']]


@pytest.mark.parametrize('prefix',['','2. **Operation**: ','- **Operation**: '])
@pytest.mark.parametrize('citation',['[E1]','[E1][E2]'])
def test_terminal_citation_covers_all_sentences_of_its_own_line(prefix,citation):
    text=prefix+'Alpha requires shutdown. Beta is recorded. '+citation
    before=deepcopy(SOURCES);p=compile_publication(text,SOURCES)
    assert p.display==text and SOURCES==before
    expected=['E1','E2'] if citation=='[E1][E2]' else ['E1']
    assert binding(p,'Alpha requires')==expected
    assert binding(p,'Beta is recorded')==expected


@pytest.mark.parametrize('separator',['\n\n','\n2. '])
def test_no_backfill_across_blank_paragraph_or_sibling_item(separator):
    p=compile_publication('Alpha requires shutdown.'+separator+'Beta is recorded. [E1]',SOURCES)
    assert binding(p,'Alpha requires')==[]


def test_earlier_different_source_remains_local_and_cannot_be_laundered():
    p=compile_publication('Alpha requires shutdown. [E2] Beta is recorded. [E1]',SOURCES)
    # The existing clause parser treats this as one compound unit. A new
    # terminal declaration must not override its existing two-source binding.
    assert not any(scope.get('kind')=='terminal-line-citation' for scope in __import__('json').loads(p.scopes_json))
    assert binding(p,'Alpha requires')==['E2','E1']


@pytest.mark.parametrize('text',['Alpha requires shutdown. Beta is recorded. [E99]',
    '> Alpha requires shutdown. Beta is recorded. [E1]',
    '| Alpha requires shutdown. Beta is recorded. | [E1] |',
    '```text\nAlpha requires shutdown. Beta is recorded. [E1]\n```'])
def test_unknown_source_quote_table_and_code_do_not_gain_group_scope(text):
    p=compile_publication(text,SOURCES)
    assert not any(scope.get('kind')=='terminal-line-citation' for scope in __import__('json').loads(p.scopes_json))


def test_different_revisions_of_the_same_document_cannot_form_a_group_scope():
    records=[source('E1','first.md','r1'),source('E2','first.md','r2')]
    p=compile_publication('Alpha requires shutdown. Beta is recorded. [E1][E2]',records)
    assert binding(p,'Alpha requires')==[]
