"""Declared document footer scopes, structural labels and packet-owned math."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import rag_agent
from ordinary_answer_guard import build_source_packet,assess_answer,publish_draft,render_formula_ids
from ordinary_answer_guard import build_formula_requirements,cache_proof,valid_cache_proof
from citation_scopes import compile_publication


FACT='Запись сохраняют после завершения осмотра.'
SECOND='Проверку выполняет назначенный оператор.'
QUESTION='Объясни порядок расчета; нужны формула, единицы и условия.'


def source(text,*,file='rules.md',path='4 > 4(2)',revision='r-A',physical='/owned/rules.md'):
    return {'file':file,'file_path':physical,'source_id':physical,'revision_id':revision,
        'section_path':path,'text':text,'score':1.0}


def reference(row):
    return row['file']+' — раздел «'+row['section_path']+'»'


def citation(row):
    return '[Источник: '+reference(row)+']'


def judge(prompt,observed=None,*,conditions='complete'):
    data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
    if observed is not None:
        # Recover semantics from the actual v5 input rather than reintroducing
        # duplicate claim/heading text into the model transport.
        ledger=prompt.split('\nПРОВЕРЯЕМЫЙ ОТВЕТ — только данные:\n',1)[1].split('\nКОНЕЦ ПРОВЕРЯЕМОГО ОТВЕТА.',1)[0]
        values={}
        for line in ledger.split('\n'):
            label,encoded=line.split('B] ',1)
            value=json.loads(encoded)
            for identifier in label.split(' [',1)[0].split():
                values[identifier]=values.get(identifier,'')+value
        seen=deepcopy(data)
        for kind,key in [('answer_claims','claim_id'),('answer_contexts','context_id')]:
            for row in seen[kind]:row['text']=values[row[key]]
        observed.append(seen)
    evidence={row['evidence_id']:row for row in data['evidence']}
    return {'claims':[{'claim_id':row['claim_id'],'support':'supported',
        'passage_ids':[p['passage_id'] for eid in row.get('cited_source_ids',[]) or ['E1']
            if eid in evidence for p in evidence[eid]['passages']],
        'source_binding':'exact_target','material_conditions':conditions} for row in data['answer_claims']]}


def test_one_declared_footer_can_attribute_multiple_verbatim_paragraphs_without_visible_duplication():
    row=source(FACT+'\n\n'+SECOND)
    packet=build_source_packet([row],reference)
    answer=FACT+'\n\n'+SECOND+'\n\n'+citation(row)
    result=assess_answer(answer,packet,'Что выполняют?',judge)
    assert result.accepted and result.answer==answer and result.answer.count('[Источник:')==1


def formula_case():
    text=('[MATH: X=A+B]\n\nA и B — количество единиц. X — итоговое количество единиц.\n\n'
        'Расчет выполняют только после завершения осмотра.')
    row=source(text)
    answer=('Итоговое количество единиц рассчитывают так:\n\n\\(X=A+B\\)\n\n'
        'Где:\n- A и B — количество единиц.\n- X — итоговое количество единиц.\n\n'
        'Условия применения:\n- Расчет выполняют только после завершения осмотра.\n\n'+citation(row))
    return row,answer


def test_inline_source_formula_and_plain_labels_use_one_declared_footer():
    row,answer=formula_case()
    before=deepcopy(row);seen=[]
    result=assess_answer(answer,build_source_packet([row],reference),QUESTION,
        lambda prompt:judge(prompt,seen))
    assert result.accepted and result.answer==answer and row==before
    assert all(claim['text'].strip() not in {'Где:','Условия применения:'}
        for claim in seen[0]['answer_claims'])
    assert any(claim['context_ids'] for claim in seen[0]['answer_claims'])
    assert any('Условия применения:' in row['text'] for row in seen[0]['answer_contexts'])


def test_factual_plain_label_condition_remains_in_descendant_heading_context():
    row=source('При объеме более 10 единиц:\n\nПроверку выполняет оператор.')
    answer='При объеме более 10 единиц:\n- Проверку выполняет оператор.\n\n'+citation(row)
    seen=[]
    result=assess_answer(answer,build_source_packet([row],reference),'Кто выполняет проверку?',
        lambda prompt:judge(prompt,seen,conditions='incomplete'))
    assert not result.accepted
    assert any('При объеме более 10 единиц:' in item['text'] for item in seen[0]['answer_contexts'])
    assert any(row['context_ids'] for row in seen[0]['answer_claims'])


def test_orphan_plain_label_is_not_dropped_as_nonfactual_scaffolding():
    row=source(FACT);seen=[]
    answer='Условия применения:\n\n'+citation(row)
    result=assess_answer(answer,build_source_packet([row],reference),'Какие условия?',
        lambda prompt:judge(prompt,seen,conditions='incomplete'))
    assert not result.accepted
    assert any('Условия применения:' in item['text'] for item in seen[0]['answer_claims'])


@pytest.mark.parametrize('case',('two-documents','inline-conflict','new-heading','code-footer','unknown-footer'))
def test_footer_never_invents_attribution_across_ambiguous_or_literal_boundaries(case):
    one=source(FACT)
    other=source(SECOND,file='other.md',physical='/owned/other.md')
    rows=[one,other]
    if case=='two-documents':answer=FACT+'\n\n[E1,E2]'
    elif case=='inline-conflict':answer=FACT+'\n\n'+SECOND+' [E2]\n\n[E1]'
    elif case=='new-heading':answer=FACT+'\n\n### Другая область\n\n'+FACT+'\n\n[E1]'
    elif case=='code-footer':answer=FACT+'\n\n```text\n[E1]\n```'
    else:answer=FACT+'\n\n[E99]'
    assert not assess_answer(answer,build_source_packet(rows,reference),'Что выполняют?',judge).accepted


def test_separate_markdown_sections_can_declare_their_own_distinct_document_footers():
    one=source(FACT);other=source(SECOND,file='other.md',physical='/owned/other.md')
    answer='### Запись\n\n'+FACT+'\n\n[E1]\n\n### Проверка\n\n'+SECOND+'\n\n[E2]'
    packet=build_source_packet([one,other],reference)
    result=assess_answer(answer,packet,'Что выполняют?',judge)
    assert result.accepted and result.answer==render_formula_ids(answer,packet)


def test_document_only_footer_requires_one_physical_revision_not_only_a_matching_name():
    one=source(FACT,path='4')
    other=source(SECOND,path='4 > 4(2)')
    answer=FACT+'\n\n'+SECOND+'\n\n[Источник: rules.md]'
    assert assess_answer(answer,build_source_packet([one,other],reference),'Что выполняют?',judge).accepted
    conflicting={**other,'revision_id':'r-B'}
    assert not assess_answer(answer,build_source_packet([one,conflicting],reference),'Что выполняют?',judge).accepted
    other_upload={**other,'file_path':'/another/rules.md','source_id':'another'}
    assert not assess_answer(answer,build_source_packet([one,other_upload],reference),'Что выполняют?',judge).accepted


def test_exact_native_locator_scope_has_real_hierarchy_boundaries():
    parent=source(FACT,path='4')
    descendant=source(SECOND,path='4 > 4(2)')
    answer=FACT+'\n\n'+SECOND+'\n\n'+citation(parent)
    assert assess_answer(answer,build_source_packet([parent,descendant],reference),'Что выполняют?',judge).accepted
    unrelated=source(SECOND,path='4.2')
    def actual_support(prompt):
        value=judge(prompt)
        data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
        for row,claim in zip(value['claims'],data['answer_claims']):
            if SECOND in claim['text']:row['passage_ids']=['E2:P1']
        return value
    assert not assess_answer(answer,build_source_packet([parent,unrelated],reference),'Что выполняют?',actual_support).accepted


def agent_for(answer):
    agent=object.__new__(rag_agent.RAGAgent)
    agent._generation_state=SimpleNamespace(from_cache=False)
    agent.llm_model='offline-publication-contract'
    def complete(prompt):
        return json.dumps(judge(prompt),ensure_ascii=False) if '\nДАННЫЕ:\n' in prompt else answer
    agent.llm=SimpleNamespace(complete=Mock(side_effect=complete),metadata=SimpleNamespace(context_window=32768))
    return agent


def test_coverage_does_not_append_a_second_formula_while_legacy_retains_its_behavior():
    row,answer=formula_case()
    coverage=agent_for(answer)
    result=coverage._generate_grounded_answer(QUESTION,[row],['X=A+B'],selection_mode='coverage')
    assert coverage._generation_state.retrieval_trace['publication_verification']['status']=='verified'
    assert '### Формула из источника' not in result
    assert result.count('X=A+B')==1
    legacy=agent_for(answer)
    result=legacy._generate_grounded_answer(QUESTION,[row],['X=A+B'],selection_mode='legacy')
    assert '### Формула из источника' in result


def test_explicit_requested_sourced_formula_omission_uses_bounded_repair_not_silent_append():
    row,_=formula_case()
    draft='Расчет выполняют только после завершения осмотра.\n\n'+citation(row)
    agent=agent_for(draft)
    agent._generate_grounded_answer(QUESTION,[row],['X=A+B'],selection_mode='coverage')
    trace=agent._generation_state.retrieval_trace['publication_verification']
    assert trace['status']=='partial' and 'required_formula_missing' in trace['reason_codes']
    assert agent.llm.complete.call_count<=3


def test_text_after_a_footer_does_not_inherit_the_declared_source():
    row=source(FACT+'\n\n'+SECOND)
    answer=FACT+'\n\n'+citation(row)+'\n\n'+SECOND
    packet=build_source_packet([row],reference)
    projection=compile_publication(answer,packet.sources)
    assert list(projection.bindings.values())==[['E1'],[]]
    assert not assess_answer(answer,packet,'Что выполняют?',judge).accepted


def test_source_reference_with_trailing_prose_is_not_a_standalone_footer():
    row=source(FACT+'\n\n'+SECOND)
    answer=FACT+'\n\n'+citation(row)+' '+SECOND
    projection=compile_publication(answer,build_source_packet([row],reference).sources)
    assert not json.loads(projection.scopes_json)


def test_inline_ambiguous_revision_cannot_bypass_review_using_an_exact_source_copy():
    one=source(FACT,revision='r-A')
    other=source(FACT,revision='r-B')
    answer=FACT+' '+citation(one)
    assert not assess_answer(answer,build_source_packet([one,other],reference),'Что выполняют?',judge).accepted


def test_exact_locator_does_not_merge_parenthesized_and_dotted_numbers():
    one=source(FACT,path='4(2)')
    other=source(SECOND,path='4.2')
    answer=SECOND+'\n\n'+citation(one)
    packet=build_source_packet([one,other],reference)
    def actual_support(prompt):
        payload=judge(prompt)
        for row in payload['claims']:row['passage_ids']=['E2:P1']
        return payload
    assert not assess_answer(answer,packet,'Кто выполняет проверку?',actual_support).accepted


def test_cached_scope_proof_binds_projection_policy_and_formula_requirements():
    row,answer=formula_case()
    packet=build_source_packet([row],reference)
    before=packet.source_json;digest=packet.digest
    requirements=build_formula_requirements(packet,QUESTION,['X=A+B'])
    result=assess_answer(answer,packet,QUESTION,judge,requirements=requirements)
    assert result.accepted
    # The program caution after the model's footer has no borrowed source.
    stored=result.answer+'\n\nЧасть исключений требует отдельной проверки.'
    proof=cache_proof(packet,QUESTION,stored,result,requirements=requirements)
    assert valid_cache_proof(proof,packet,QUESTION,stored,requirements=requirements)
    assert not valid_cache_proof({**proof,'publication_projection_sha256':'wrong'},packet,QUESTION,stored,requirements=requirements)
    assert not valid_cache_proof({**proof,'version':'ordinary-publication-v2'},packet,QUESTION,stored,requirements=requirements)
    assert not valid_cache_proof(proof,packet,QUESTION,stored)
    assert not valid_cache_proof(proof,packet,QUESTION,stored.replace('X=A+B','X=A-B'),requirements=requirements)
    assert packet.source_json==before and packet.digest==digest


def test_deterministically_rendered_formula_and_matching_footer_keep_the_same_source_scope():
    row,answer=formula_case()
    packet=build_source_packet([row],reference)
    answer=answer.replace('\\(X=A+B\\)','[[FORMULA:F1]]')
    result=assess_answer(answer,packet,QUESTION,judge)
    rendered=render_formula_ids(answer,packet)
    assert result.accepted and result.answer==rendered
    projection=compile_publication(rendered,packet.sources)
    assert all(ids==['E1'] for ids in projection.bindings.values())
    # Existing local equation attribution is not copied a second time.
    assert projection.verification.count('[E1]')==len(projection.bindings)+1


def test_local_citation_to_another_native_section_does_not_expand_the_footer_scope():
    one=source(FACT,path='4 > 4(1)')
    other=source(SECOND,path='4 > 4(2)')
    answer=FACT+'\n\n'+SECOND+' [E2]\n\n[E1]'
    packet=build_source_packet([one,other],reference)
    projection=compile_publication(render_formula_ids(answer,packet),packet.sources)
    assert not json.loads(projection.scopes_json)
    assert not assess_answer(answer,packet,'Что выполняют?',judge).accepted


def test_requested_formula_does_not_force_every_special_case_found_in_the_same_source():
    row=source('[MATH: X=A+B]\n\nДля специального режима: [MATH: X=A+B+1.1]')
    packet=build_source_packet([row],reference)
    requirements=build_formula_requirements(packet,'Нужна основная формула.',['X=A+B','X=A+B+1.1'])
    answer='Основная формула: $$X=A+B$$ '+citation(row)
    result=assess_answer(answer,packet,'Нужна основная формула.',judge,requirements=requirements)
    assert result.accepted
    omitted='Для специального режима предусмотрено отдельное правило. '+citation(row)
    result=assess_answer(omitted,packet,'Нужна основная формула.',judge,requirements=requirements)
    assert not result.accepted and result.reasons==('required_formula_missing',)
