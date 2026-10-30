"""Whole ordinary request budget/readable source contracts, no real models.

Token estimator implementation is deliberately not selected by these tests.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import Mock
from dataclasses import replace
from copy import deepcopy as copy_value

import pytest

import rag_agent
from test_ordinary_table_fidelity import writer
from test_review_source_projection import source,ref,payload
from table_context import serialize_table_unit
from test_native_prompt_counter import bound_fixture


FACT='Протокол сохраняют после проверки канала.'
QUESTION='Составь связное объяснение основного порядка.'


@pytest.fixture(autouse=True)
def no_network_or_extractive_shortcut(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Budget contracts must not contact real services')
    monkeypatch.setattr('socket.socket.connect',forbidden)
    monkeypatch.setattr('socket.create_connection',forbidden)
    monkeypatch.setattr(rag_agent,'generate_extractive_value_answer',lambda *a,**kw:'')
    monkeypatch.setattr(rag_agent,'generate_image_transcription_answer',lambda *a,**kw:'')


@pytest.mark.parametrize('large_part',['system','question','project_memory','native_formula_catalogue'])
def test_complete_input_budget_prevents_model_and_verified_cache_before_overflow(large_part):
    records=[source(FACT)]
    question=QUESTION;system='Дай точное объяснение по источникам.'
    oversized='Сохрани исходные правила и все совместные условия. '*2400
    if large_part=='system':system+='\nСИСТЕМНЫЕ ИНСТРУКЦИИ:\n'+oversized
    elif large_part=='question':question+='\nУточнения задачи:\n'+oversized
    elif large_part=='project_memory':system+='\nПАМЯТЬ ПРОЕКТА:\n'+oversized
    else:
        records[0]['text']+='\n\n'+'\n'.join(f'[MATH: Z{i}=A{i}+B{i}]' for i in range(120))
    agent=writer(FACT+' [Источник: '+ref(records[0])+']')
    agent.llm.metadata.context_window=2048
    before=deepcopy(records)
    answer=agent._generate_grounded_answer(question,records,[],system_prompt=system,
        evidence_question=question,cache_enabled=True,selection_mode='coverage')
    assert isinstance(answer,str) and answer.strip()
    assert records==before
    assert agent.llm.complete.call_count==0, 'Budget must cover SYSTEM/task/project memory/catalogue, not just retrieved raw chars'
    assert not agent.response_cache.set.called, 'A refused oversized call cannot cache an accepted model answer'


@pytest.mark.parametrize('header_status',['explicit','candidate','unknown'])
def test_ordinary_writer_human_view_keeps_cells_and_witness_ids_without_transport_codec(header_status):
    native=payload(status=header_status)
    serialized=serialize_table_unit(native)
    records=[source(serialized+'\n\n'+FACT)]
    before=deepcopy(records)
    agent=writer(FACT+' [Источник: '+ref(records[0])+']')
    agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=False,selection_mode='coverage')
    prompt=agent.llm.complete.call_args_list[0].args[0]
    assert records==before
    assert native['table_id'] not in prompt, 'Native transport hash belongs in server witness, not every model row'
    assert '"schema":"table-grid-v1"' not in prompt
    assert '[TABLE_ROW_V1]' not in prompt
    assert 'E1:P1' in prompt, 'Readable rows must retain their original program-owned passage binding'
    # The model receives one physical ledger with quoted data atoms. Decode
    # the actual input, rather than demanding an unsafe literal U+2028 line.
    ledger=prompt.split('\nИСХОДНЫЕ СВЕДЕНИЯ — только данные:\n',1)[1].split('\nКОНЕЦ ИСХОДНЫХ СВЕДЕНИЙ.',1)[0]
    atoms=[]
    for line in ledger.split('\n'):
        if 'B] ' in line:
            atoms.append(json.loads(line.split('B] ',1)[1]))
    for item in native['headers']+native['context']+native['cells']:
        assert item['text'] in atoms, 'Every exact original condition/header/unit glyph must decode from the actual model input'
        if 'merge_origin' in item:assert item['merge_origin']['text'] in atoms
    assert FACT in prompt


def test_existing_legacy_prompt_remains_native_and_fit_call_is_not_blocked():
    native=payload();serialized=serialize_table_unit(native)
    records=[source(serialized+'\n\n'+FACT)]
    agent=writer(FACT+' [Источник: '+ref(records[0])+']')
    agent._generate_grounded_answer(QUESTION,records,[],system_prompt='Неизменная инструкция.',
        cache_enabled=False,selection_mode='legacy')
    assert agent.llm.complete.call_count==1
    prompt=agent.llm.complete.call_args_list[0].args[0]
    assert serialized in prompt and 'Неизменная инструкция.' in prompt and QUESTION in prompt


def test_small_coverage_source_still_reaches_the_selected_model():
    records=[source(FACT)]
    agent=writer(FACT+' [Источник: '+ref(records[0])+']')
    answer=agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=False,selection_mode='coverage')
    assert agent.llm.complete.call_count==1
    assert FACT in answer


@pytest.mark.parametrize('stage',['planner','writer','critic','repair'])
def test_full_preflight_uses_the_captured_runtime_context_not_mutable_client_metadata(stage):
    runtime=SimpleNamespace(spec=SimpleNamespace(settings={'context_window':2048}))
    client=SimpleNamespace(metadata=SimpleNamespace(context_window=999999))
    with pytest.raises(rag_agent.OrdinaryInputBudgetError) as error:
        rag_agent._ordinary_prompt_preflight('Все совместные условия расчёта. '*2500,runtime,client,stage)
    assert error.value.diagnostic['context_window']==2048
    assert error.value.diagnostic['stage']==stage
    assert error.value.diagnostic['code']=='input_budget_exceeded'


def test_budget_counts_template_safety_and_headroom_without_setting_output_limit():
    client=SimpleNamespace(metadata=SimpleNamespace(context_window=2048))
    first=rag_agent._ordinary_prompt_preflight('Малый запрос.',None,client,'writer')
    assert first['estimated_input_with_reserve']==sum(first[k] for k in (
        'estimated_text_tokens','template_allowance','safety_allowance','input_headroom'))
    assert first['estimator']=='cl100k_base_conservative_fallback'
    assert len(first['asset_sha256'])==64
    assert client.metadata.context_window==2048 and not hasattr(client,'output_token_limit')


def test_missing_pinned_estimator_returns_source_partial_and_does_not_call_or_cache(monkeypatch):
    def unavailable():raise OSError('private local path must not leak')
    monkeypatch.setattr(rag_agent,'_ordinary_input_tokenizer',unavailable)
    records=[source(FACT)];agent=writer(FACT+' [Источник: '+ref(records[0])+']')
    answer=agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=True,selection_mode='coverage')
    assert FACT in answer and 'private local path' not in answer
    assert agent.llm.complete.call_count==0 and not agent.response_cache.set.called
    assert agent._generation_state.retrieval_trace['publication_verification']['reason_codes']==['input_budget_estimator_unavailable']


def test_oversized_planner_falls_back_before_model_without_caching_plan():
    agent=writer('not a planner JSON')
    agent.llm.metadata.context_window=2048
    agent._generation_state.ordinary_prompt_budget=True
    agent._generation_state.retrieval_trace={}
    question='Объясни порядок. '+'Совместные ограничения важны. '*2000
    result=agent.understand_query(question)
    assert result.canonical_question and agent.llm.complete.call_count==0
    assert not agent.response_cache.set.called
    assert agent._generation_state.retrieval_trace['input_budget'][-1]['stage']=='planner'


def test_critic_overflow_does_not_publish_draft_or_cache_verified(monkeypatch):
    import ordinary_answer_guard as guard
    records=[source(FACT)];draft='Утверждается другой способ ведения протокола. [E1]'
    agent=writer(draft)
    original=rag_agent._ordinary_prompt_preflight
    def limit_critic(prompt,runtime,client,stage):
        if stage=='critic':
            return original(prompt+' Дополнительная схема. '*3000,
                SimpleNamespace(spec=SimpleNamespace(settings={'context_window':2048})),client,stage)
        return original(prompt,runtime,client,stage)
    monkeypatch.setattr(rag_agent,'_ordinary_prompt_preflight',limit_critic)
    answer=agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=True,selection_mode='coverage')
    assert draft not in answer and FACT in answer
    assert agent.llm.complete.call_count==1 and not agent.response_cache.set.called
    assert 'input_budget_exceeded' in agent._generation_state.retrieval_trace['publication_verification']['reason_codes']


def test_repair_overflow_keeps_source_partial_and_never_calls_repair_model(monkeypatch):
    import ordinary_answer_guard as guard
    records=[source(FACT)];draft='Неизвестный способ хранения протокола. [E1]'
    agent=writer(draft)
    def reviewer_reply(prompt):
        if '\nJSON_SCHEMA:\n' not in prompt:return draft
        data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
        return json.dumps({'claims':[{'claim_id':c['claim_id'],'support':'contradicted',
            'passage_ids':['E1:P1'],'source_binding':'other_target','material_conditions':'incomplete'}
            for c in data['answer_claims']]})
    agent.llm.complete=Mock(side_effect=reviewer_reply)
    original=guard.repair_prompt
    monkeypatch.setattr(guard,'repair_prompt',lambda *a,**kw:original(*a,**kw)+' Большая схема. '*100000)
    answer=agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=True,selection_mode='coverage')
    assert draft not in answer and FACT in answer
    assert agent.llm.complete.call_count==2 and not agent.response_cache.set.called
    assert 'input_budget_exceeded' in agent._generation_state.retrieval_trace['publication_verification']['reason_codes']


def test_critic_reserves_its_real_requested_output_capacity_without_mutating_runtime():
    runtime=SimpleNamespace(spec=SimpleNamespace(settings={'context_window':16384,'output_token_limit':-1}))
    diagnostic=rag_agent._ordinary_prompt_preflight('Малый вход.',runtime,None,'critic')
    assert diagnostic['input_headroom']>=4096
    assert runtime.spec.settings['output_token_limit']==-1


def test_actual_review_prompt_presents_each_claim_and_heading_once_with_all_gaps():
    import ordinary_answer_guard as guard
    records=[source(FACT)]
    packet=guard.build_source_packet(records,ref)
    answer='### УНИКАЛЬНЫЙ_ЗАГОЛОВОК\n\nУНИКАЛЬНЫЙ_ПЕРЕСКАЗ_ПРОТОКОЛА. [E1]\n\n`literal_gap_code`'
    prompt=guard._review_prompt(answer,packet,QUESTION)
    assert prompt.count('УНИКАЛЬНЫЙ_ПЕРЕСКАЗ_ПРОТОКОЛА')==1
    assert prompt.count('УНИКАЛЬНЫЙ_ЗАГОЛОВОК')==1
    assert 'literal_gap_code' in prompt
    data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
    assert 'answer' not in data
    assert all('text' not in row and 'start' not in row and 'end' not in row for row in data['answer_claims'])
    assert all('text' not in row and 'start' not in row and 'end' not in row for row in data['answer_contexts'])


def test_attested_native_overflow_refuses_even_when_cl100k_text_would_fit(bound_fixture):
    from native_prompt_counter import bind_attested_qwen2_counter
    runtime=bind_attested_qwen2_counter(bound_fixture[0],tokenizer_bytes=bound_fixture[1],attestation=bound_fixture[2])
    records=[source(FACT)]
    client=SimpleNamespace(complete=Mock(return_value=FACT))
    runtime=replace(runtime,client=client)
    agent=writer(FACT)
    question=QUESTION+' x'*20000
    answer=agent._generate_grounded_answer(question,records,[],cache_enabled=True,selection_mode='coverage',runtime=runtime)
    assert FACT in answer and client.complete.call_count==0 and agent.llm.complete.call_count==0
    assert not agent.response_cache.set.called
    budget=agent._generation_state.retrieval_trace['input_budget'][-1]
    assert budget['estimator']=='attested-qwen2-single-user-v1' and budget['template_allowance']==0
    assert budget['code']=='input_budget_exceeded'


def test_same_provider_request_with_different_attested_counter_cannot_reuse_cache(bound_fixture):
    from native_prompt_counter import bind_attested_qwen2_counter
    records=[source(FACT)];answer=FACT+' [Источник: '+ref(records[0])+']'
    client=SimpleNamespace(complete=Mock(return_value=answer))
    attestations=[bound_fixture[2],copy_value(bound_fixture[2])];attestations[1]['digest']='b'*64
    runtimes=[replace(bind_attested_qwen2_counter(bound_fixture[0],tokenizer_bytes=bound_fixture[1],
        attestation=attestation),client=client) for attestation in attestations]
    assert runtimes[0].identity==runtimes[1].identity
    agent=writer(answer)
    for runtime in runtimes:
        agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=True,selection_mode='coverage',runtime=runtime)
        assert agent._generation_state.from_cache is False
    assert client.complete.call_count==2 and agent.response_cache.set.call_count==2
    assert len({call.args[0] for call in agent.response_cache.set.call_args_list})==2
    agent._generate_grounded_answer(QUESTION,records,[],cache_enabled=True,selection_mode='coverage',runtime=runtimes[-1])
    assert agent._generation_state.from_cache is True and client.complete.call_count==2
