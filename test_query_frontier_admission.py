"""Paraphrase admission must preserve native scope and the original query."""
from copy import deepcopy
import socket

import pytest

from candidate_admission import order_by_query_frontiers
from query_rank import QueryRankContext


def row(text, **meta):
    return {'file':'neutral.md','section_path':'Rules','text':text,**meta}


def context(question='what makes up the cooling fee'):
    return QueryRankContext(question,lambda value:frozenset(value.casefold().split()))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def fail(*args,**kwargs):
        raise AssertionError('Candidate admission must not call a network/model')
    monkeypatch.setattr(socket.socket,'connect',fail)


def test_legitimate_paraphrase_is_not_lost_to_a_shared_word_gate():
    ctx=context();noise=[row(f'Peripheral record {n}.') for n in range(20)]
    main=row('Cooling fee consists of energy, maintenance and dispatch costs.')
    values=[*noise,main];before=deepcopy(values)
    ctx.record(ctx.question,noise)
    ctx.record('composition of refrigeration charge',[main,*noise])
    result=order_by_query_frontiers(values,max_chars=2000,query_rank_context=ctx)
    assert result.index(main)<3
    assert values==before and {id(x) for x in result}=={id(x) for x in values}


def test_channels_keep_their_local_depth_instead_of_collapsing_minimum_rank_ties():
    ctx=context();one,two,three,four=[row(x) for x in ('one','two','three','four')]
    ctx.record(ctx.question,[one,two]);ctx.record(ctx.question,[three,four])
    ctx.record('composition of refrigeration charge',[one,two,three,four])
    result=order_by_query_frontiers([one,two,three,four],max_chars=100,
                                   query_rank_context=ctx,max_slots=4)
    assert result.index(three)<result.index(four)
    assert result[:3]==[one,two,three]


def test_repeating_one_channel_cannot_buy_additional_priority():
    values=[row(f'Rule {n}') for n in range(12)]
    def order(repeats):
        ctx=context();ctx.record(ctx.question,values)
        for _ in range(repeats):ctx.record('fee composition',values[::-1])
        return order_by_query_frontiers(values,max_chars=1000,query_rank_context=ctx)
    assert order(1)==order(100)


def test_foreign_clipped_deleted_or_changed_revision_records_cannot_inherit_priority():
    ctx=context();original=row('Only the complete native instruction.',revision_id='v1')
    ctx.record(ctx.question,[original]);ctx.record('fee composition',[original])
    values=[row('First native record.'),{**original,'revision_id':'v2'},
            {**original,'text':'Only the complete'}, {**original,'status':'deleted'},
            {**original,'file':'private.md'},row('Forged',matched_queries=['fee composition'])]
    assert order_by_query_frontiers(values,max_chars=2000,query_rank_context=ctx)==values


def test_new_request_has_no_rank_receipts_from_another_chat():
    values=[row('First.'),row('Second.')];old=context()
    old.record(old.question,values);old.record('fee composition',values[::-1])
    assert order_by_query_frontiers(values,max_chars=100,query_rank_context=context())==values


def test_soft_ordering_cannot_restore_a_filtered_private_record():
    ctx=context();allowed=row('Public complete rule.');private=row('Private rule.',file='private.md')
    ctx.record(ctx.question,[private,allowed]);ctx.record('fee composition',[private,allowed])
    result=order_by_query_frontiers([allowed],max_chars=1000,query_rank_context=ctx)
    assert result==[allowed]


@pytest.mark.parametrize('budget',[0,-1,5])
def test_unfit_records_remain_whole_and_in_original_order(budget):
    ctx=context();values=[row('A complete large first rule.'),row('A complete second rule.')]
    ctx.record(ctx.question,values[::-1]);ctx.record('fee composition',values[::-1])
    before=deepcopy(values)
    assert order_by_query_frontiers(values,max_chars=budget,query_rank_context=ctx)==values
    assert values==before


def test_single_query_and_empty_titles_do_not_create_a_diversity_vote():
    ctx=context();values=[row('Rules'),row('A complete instruction.')]
    ctx.record(ctx.question,values[::-1])
    assert order_by_query_frontiers(values,max_chars=100,query_rank_context=ctx)==values
    ctx.record('fee composition',[values[0]])
    assert order_by_query_frontiers(values,max_chars=100,query_rank_context=ctx)==values


def test_original_query_keeps_half_of_the_bounded_frontier_despite_many_rewrites():
    ctx=context();raw=[row(f'Raw {n}.') for n in range(10)]
    rewritten=[row(f'Paraphrase {n}.') for n in range(10)]
    ctx.record(ctx.question,raw)
    for n,item in enumerate(rewritten):ctx.record(f'rewording number {n}',[item])
    values=[*raw,*rewritten]
    result=order_by_query_frontiers(values,max_chars=1000,query_rank_context=ctx,max_slots=10)
    assert sum(item in raw for item in result[:10])==5
    assert len(result)==len(values)


def test_native_section_introduction_precedes_its_ranked_special_cases():
    ctx=context();rare=row('For the remote cooling circuit, an additional fee applies.')
    main=row('# Rules\n\nCooling fee consists of energy, maintenance and dispatch costs.')
    noise=[row(f'Unrelated {n}.',section_path=f'Other {n}') for n in range(12)]
    ctx.record(ctx.question,[rare,*noise,main])
    ctx.record('composition of refrigeration charge',[rare,*noise,main])
    result=order_by_query_frontiers([rare,*noise,main],max_chars=2000,query_rank_context=ctx)
    assert result.index(main)<result.index(rare)


def test_a_header_from_another_revision_cannot_become_the_owner_of_a_continuation():
    ctx=context();rare=row('Special condition.',revision_id='r1')
    foreign=row('# Rules\n\nAnother version definition.',revision_id='r2')
    values=[rare,foreign]
    ctx.record(ctx.question,values);ctx.record('fee composition',values)
    assert order_by_query_frontiers(values,max_chars=2000,query_rank_context=ctx)==values


def test_unranked_header_or_ambiguous_two_headers_cannot_claim_query_priority():
    ctx=context();rare=row('Special condition.')
    main=row('# Rules\n\nMain definition.')
    ctx.record(ctx.question,[rare]);ctx.record('fee composition',[rare])
    assert order_by_query_frontiers([rare,main],max_chars=2000,query_rank_context=ctx)==[rare,main]
    ctx=context();other=row('# Rules\n\nAnother candidate introduction.')
    values=[rare,main,other]
    ctx.record(ctx.question,values);ctx.record('fee composition',values)
    assert order_by_query_frontiers(values,max_chars=2000,query_rank_context=ctx)==values


def test_structural_header_without_body_is_not_a_reader_candidate_vote():
    ctx=context();main=row('A complete rule.');empty=row('[SECTION: Rules]\n# Rules')
    ctx.record(ctx.question,[main]);ctx.record('fee composition',[empty])
    assert order_by_query_frontiers([empty,main],max_chars=2000,query_rank_context=ctx)==[empty,main]
