"""A source-only list footer declares one parent list scope, never global truth."""
from copy import deepcopy

import pytest

from citation_scopes import compile_publication
from grounding_verifier import extract_answer_claims


def source(eid, section, *, revision='r1'):
    return {'evidence_id':eid,'file':'neutral-rules.md','file_path':'/owned/neutral-rules.md',
        'source_id':'neutral-rules','revision_id':revision,'section_path':section,
        'source_reference':'neutral-rules.md — раздел «'+section+'»','text':section+' native rules.'}


SOURCES=[source('E1','Alpha'),source('E2','Beta'),source('E3','Settings')]


def bindings(projection, phrase):
    rows=[x for x in extract_answer_claims(projection.verification) if phrase in x['text']]
    assert len(rows)==1, (phrase, rows)
    return projection.bindings[rows[0]['claim_id']]


@pytest.mark.parametrize('bullet',['-','+','*'])
def test_each_numbered_item_owns_its_source_footer_and_full_descendant_claims(bullet):
    text=(f'Unrelated introduction.\n\n1. **Alpha operation**:\n'
          f'   {bullet} Alpha uses all points.\n   {bullet} Alpha uses the permitted time window.\n   {bullet} [E1]\n\n'
          f'2. **Beta operation**:\n   {bullet} Beta uses another rule.\n   {bullet} [E2]\n\nUnrelated tail.')
    before=deepcopy(SOURCES);projection=compile_publication(text,SOURCES)
    assert projection.display==text and SOURCES==before
    for phrase in ['Alpha operation','Alpha uses all','Alpha uses the permitted']:
        assert bindings(projection,phrase)==['E1']
    for phrase in ['Beta operation','Beta uses another']:
        assert bindings(projection,phrase)==['E2']
    assert bindings(projection,'Unrelated introduction')==[] and bindings(projection,'Unrelated tail')==[]


def test_exact_document_locator_footer_uses_same_native_binding_as_an_eid():
    text='1. **Alpha operation**:\n   - Alpha uses all points.\n   - [Источник: neutral-rules.md — раздел «Alpha»]'
    projection=compile_publication(text,SOURCES)
    assert bindings(projection,'Alpha uses all')==['E1'] and projection.unresolved_reference_count==0


def test_unordered_settings_list_binds_only_its_own_contiguous_list_not_intro():
    text='Unrelated introduction.\n\nSettings:\n- Count is three.\n- Time is explicit.\n- [E3]\n\nUnrelated tail.'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Count is three')==['E3'] and bindings(p,'Time is explicit')==['E3']
    assert bindings(p,'Unrelated introduction')==[] and bindings(p,'Unrelated tail')==[]


def test_sibling_numbered_item_never_receives_the_later_sibling_footer():
    text='1. **Alpha operation**:\n   - Alpha is unbound.\n\n2. **Beta operation**:\n   - Beta has a rule.\n   - [E2]'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Alpha is unbound')==[] and bindings(p,'Beta has a rule')==['E2']


def test_nested_numbered_owner_does_not_donate_source_to_outer_claims():
    text=('1. **Outer operation**:\n   - Outer is unbound.\n   1. **Inner operation**:\n'
          '      - Inner has a rule.\n      - [E1]\n\n2. **Sibling**:\n   - Sibling is unbound.')
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Outer is unbound')==[] and bindings(p,'Inner has a rule')==['E1']
    assert bindings(p,'Sibling is unbound')==[]


@pytest.mark.parametrize('heading',['### Separate section','Separate condition:','**Separate condition**'])
def test_hard_and_soft_heading_break_the_previous_list_owner(heading):
    text='1. **Alpha operation**:\n   - Earlier is unbound.\n\n'+heading+'\n\n- Later has a rule.\n- [E1]'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Earlier is unbound')==[] and bindings(p,'Later has a rule')==['E1']


def test_outdented_prose_breaks_list_scope_and_later_footer_does_not_bind_it():
    text='1. **Alpha operation**:\n   - Earlier is unbound.\n\nOutdented prose.\n\n- Later has a rule.\n- [E1]'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Earlier is unbound')==[] and bindings(p,'Outdented prose')==[]
    assert bindings(p,'Later has a rule')==['E1']


def test_footer_is_backward_only_even_inside_the_same_list_owner():
    text='1. **Alpha operation**:\n   - Before has a rule.\n   - [E1]\n   - After is unbound.'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Before has a rule')==['E1'] and bindings(p,'After is unbound')==[]


@pytest.mark.parametrize('footer',['   > - [E1]','   - `[E1]`','   - "[E1]"','   - [E1] says everything is true.',
                                   '   - [E99]','   2. [E1]','   \\- [E1]'])
def test_quoted_unknown_ordered_or_non_source_only_line_does_not_declare_scope(footer):
    p=compile_publication('1. **Alpha operation**:\n   - Alpha is unbound.\n'+footer,SOURCES)
    assert bindings(p,'Alpha is unbound')==[]


def test_source_footer_inside_code_cannot_bind_an_external_list_claim():
    text='1. **Alpha operation**:\n   - Alpha is unbound.\n\n```text\n   - [E1]\n```'
    assert bindings(compile_publication(text,SOURCES),'Alpha is unbound')==[]


def test_conflicting_inline_revision_blocks_footer_donation_but_preserves_explicit_citation():
    records=[source('E1','Alpha',revision='r1'),source('E2','Alpha',revision='r2')]
    text='1. **Alpha operation**:\n   - Explicit belongs to another revision. [E2]\n   - Unbound remains unknown.\n   - [E1]'
    p=compile_publication(text,records)
    assert bindings(p,'Explicit belongs')==['E2']
    assert bindings(p,'Unbound remains')==[]


def test_ambiguous_human_reference_across_revisions_never_assigns_an_owner():
    records=[source('E1','Alpha',revision='r1'),source('E2','Alpha',revision='r2')]
    text='1. **Alpha operation**:\n   - Alpha is unbound.\n   - [Источник: neutral-rules.md — раздел «Alpha»]'
    p=compile_publication(text,records)
    assert bindings(p,'Alpha is unbound')==[] and p.unresolved_reference_count==1


def test_nested_unordered_footer_never_donates_to_the_outer_numbered_item():
    text='1. **Outer operation**:\n   - Outer is unbound.\n      - Inner has a rule.\n      - [E1]'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Outer is unbound')==[]


def test_indented_nested_fence_cannot_declare_an_outer_list_footer():
    text='1. **Outer operation**:\n   - Outer is unbound.\n\n      ```text\n      - [E1]\n      ```'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Outer is unbound')==[]


def test_root_indented_code_list_is_not_an_author_attribution_declaration():
    text='Code example:\n\n    - Literal code example.\n    - [E1]'
    p=compile_publication(text,SOURCES)
    assert bindings(p,'Literal code example')==[]


def test_scope_projection_version_invalidates_old_cache_proof_without_flushing(monkeypatch):
    import citation_scopes
    import ordinary_answer_guard as guard
    row=source('E1','Alpha');row['text']='Alpha uses all points.'
    packet=guard.build_source_packet([row],lambda r:r['source_reference'])
    text='1. **Alpha operation**:\n   - Alpha uses all points.\n   - [E1]'
    result=guard.GuardResult(text,True)
    current=guard.cache_proof(packet,'Which operation?',text,result)
    assert guard.valid_cache_proof(current,packet,'Which operation?',text)
    version=citation_scopes.PROJECTION_VERSION
    monkeypatch.setattr(citation_scopes,'PROJECTION_VERSION','declared-source-scope-v1')
    old=guard.cache_proof(packet,'Which operation?',text,result)
    monkeypatch.setattr(citation_scopes,'PROJECTION_VERSION',version)
    assert old['publication_projection_sha256']!=current['publication_projection_sha256']
    assert not guard.valid_cache_proof(old,packet,'Which operation?',text)
