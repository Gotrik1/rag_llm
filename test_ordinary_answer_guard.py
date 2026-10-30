"""Immutable source projection, checked review protocol and cache witnesses."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import rag_agent
from ordinary_answer_guard import (build_source_packet, assess_answer, render_formula_ids,
    cache_proof, valid_cache_proof, publish_draft)
from table_context import serialize_table_unit


FACT = 'Запись хранится в журнале проверки.'
REFERENCE = ' [Источник: protocol.md — раздел «Порядок»]'


def ref(row):
    return row['file'] + ' — раздел «' + row['section_path'] + '»'


def source(text=FACT):
    return {'file': 'protocol.md', 'section_path': 'Порядок', 'source_id': 'source-A',
        'revision_id': 'revision-A', 'text': text, 'score': 1.0}


def unit():
    return serialize_table_unit({'schema': 'table-grid-v1', 'table_id': 'a' * 64,
        'row': 1, 'row_kind': 'body', 'header_status': 'explicit',
        'headers': [{'row': 0, 'column': n, 'span': 1, 'text': value}
                    for n, value in enumerate(['Параметр', 'Источник данных'])],
        'cells': [{'row': 1, 'column': n, 'span': 1, 'text': value,
                   'start': 0, 'end': len(value), 'total_chars': len(value)}
                  for n, value in enumerate(['Порог', 'Реестр наблюдений'])],
        'context': [], 'warnings': []})


def review_payload(prompt, *, support='supported', binding='exact_target', conditions='complete'):
    payload = json.loads(prompt.split('\nДАННЫЕ:\n', 1)[1])
    passages = [p['passage_id'] for item in payload['evidence'] for p in item['passages']]
    return {'claims': [{'claim_id': row['claim_id'], 'context_ids': row.get('context_ids', []),
        'support': support, 'passage_ids': passages, 'source_binding': binding,
        'material_conditions': conditions} for row in payload['answer_claims']]}


def test_source_packet_detaches_all_native_fields_without_rewriting_text():
    text = 'Начало\r\n\n' + unit() + '\r\n' + FACT
    context = [source(text)]
    before = deepcopy(context)
    packet = build_source_packet(context, ref)
    assert context == before and packet.sources[0]['text'] == text
    assert json.loads(packet.model_json)[0]['source_view'] == 'verified_table_units'
    assert json.loads(packet.model_json)[0]['model_source']['source_segments'][1]['table']['cells'][1]['text'] == 'Реестр наблюдений'
    context[0]['text'] = 'Changed after capture'
    returned = packet.sources
    returned[0]['revision_id'] = 'tampered'
    assert packet.sources[0]['text'] == text
    assert packet.sources[0]['revision_id'] == 'revision-A'
    with pytest.raises(FrozenInstanceError):
        packet.digest = 'tampered'


def test_legacy_plain_table_fallback_is_explicit_and_lossless():
    text = '[TABLE]\nПоле | Значение\nR | 7\n'
    packet = build_source_packet([source(text)], ref)
    projection = json.loads(packet.model_json)[0]
    assert projection['source_view'] == 'plain_source_fallback'
    assert projection['model_source'] == text
    assert packet.sources[0]['text'] == text


def test_review_projection_keeps_plain_bytes_once_in_stable_passage_witnesses():
    from ordinary_answer_guard import _review_prompt
    text = ('Строка с условиями\r\n\n[TABLE]\nПоле | Источник\nQ | Журнал\n\n'
        + ('Длинное условие сохраняется полностью. ' * 160) + '\n\n[MATH: X=A+B]')
    packet = build_source_packet([source(text)], ref)
    original_sources, digest = packet.source_json, packet.digest
    before = json.loads(packet.anchors_json)
    prompt = _review_prompt('Пересказ.' + REFERENCE, packet, 'Какое правило?')
    actual = json.loads(prompt.split('\nДАННЫЕ:\n', 1)[1])
    from ordinary_answer_guard import _review_source_projection, _human_source_projection
    data, _ = _review_source_projection(packet)
    human, _ = _human_source_projection(packet)
    assert human['human_sources'] in prompt and 'typed_sources' not in actual
    assert all(set(p)=={'passage_id'} for s in actual['evidence'] for p in s['passages'])
    assert data['evidence'] == before
    assert ''.join(row['text'] for row in data['evidence'][0]['passages']) == text
    assert [row['passage_id'] for row in data['evidence'][0]['passages']] == [row['passage_id'] for row in before[0]['passages']]
    assert data['typed_sources'][0]['source_view'] == 'plain_source_fallback'
    assert 'model_source' not in data['typed_sources'][0]
    assert packet.source_json == original_sources and packet.digest == digest


def test_review_projection_keeps_the_entire_verified_table_view():
    from ordinary_answer_guard import _review_prompt
    from table_context import serialize_table_unit
    packet = build_source_packet([source(unit() + '\n\n' + FACT)], ref)
    before = deepcopy(packet)
    view = json.loads(packet.model_json)[0]
    anchors = json.loads(packet.anchors_json)[0]['passages']
    prompt = _review_prompt('Пересказ.' + REFERENCE, packet, 'Какой источник?')
    actual = json.loads(prompt.split('\nДАННЫЕ:\n', 1)[1])
    from ordinary_answer_guard import _review_source_projection, _human_source_projection
    data, _ = _review_source_projection(packet)
    human, _ = _human_source_projection(packet)
    assert human['human_sources'] in prompt and '[TABLE_ROW_V1]' not in prompt
    assert all(set(p)=={'passage_id'} for s in actual['evidence'] for p in s['passages'])
    assert data['typed_sources'][0] == {key:value for key, value in view.items() if key != 'model_source'}
    passages = data['evidence'][0]['passages']
    assert [row['passage_id'] for row in passages] == [row['passage_id'] for row in anchors]
    restored = []
    for passage, anchor in zip(passages, anchors):
        if 'table_source' in passage:
            assert 'text' not in passage
            text = ''.join(serialize_table_unit(segment['table']) if 'table' in segment else segment['text']
                           for segment in passage['table_source']['source_segments'])
        else:
            text = passage['text']
        assert text == anchor['text']
        restored.append(text)
    assert ''.join(restored) == packet.sources[0]['text']
    assert packet == before


@pytest.mark.parametrize('formula', [
    '$$F_sub(a,2)=2*V_sub(a,2)$$', r'\[F_sub(a,2)=2*V_sub(a,2)\]',
    r'$$F=\frac{a$$', '$$F_sub(a,2)=2*V_sub(a,2)',
])
def test_partial_does_not_render_internal_or_invalid_source_math(formula):
    records = [source(formula + '\n\n' + FACT)]
    before = deepcopy(records)
    packet = build_source_packet(records, ref)
    result = publish_draft('Непроверенный пересказ.' + REFERENCE, packet,
        'Объясни правило', reviewer=lambda _: None)
    assert not result.accepted and formula not in result.answer
    assert '_sub(' not in result.answer and r'\frac{a' not in result.answer
    assert FACT in result.answer and REFERENCE.strip() in result.answer
    assert records == before and packet.sources[0]['text'] == records[0]['text']


@pytest.mark.parametrize('formula', [r'$$F_{a,2}=2\times V_{a,2}$$', r'\[F=A+B\]'])
def test_partial_preserves_valid_native_markdown_math_without_changing_its_formula(formula):
    packet = build_source_packet([source(formula + '\n\n' + FACT)], ref)
    result = publish_draft('Непроверенный пересказ.' + REFERENCE, packet,
        'Объясни правило', reviewer=lambda _: None)
    assert not result.accepted
    assert formula in result.answer and FACT in result.answer and REFERENCE.strip() in result.answer


@pytest.mark.parametrize('typed', (False, True), ids=('plain-source','typed-source'))
@pytest.mark.parametrize('math', ('[MATH: X=A+B]', '$$X=A+B$$'))
def test_partial_hides_mathml_transport_refs_but_keeps_source_math_and_prose(typed, math):
    text = (unit() + '\n\n' if typed else '') + math + ' [[MATHML_REF:abc123def456abc123def456]]\n\n' + FACT
    records = [source(text)]
    packet = build_source_packet(records, ref)
    original = packet.source_json
    result = publish_draft('Непроверенный пересказ.' + REFERENCE, packet,
        'Объясни правило', reviewer=lambda _: None)
    assert not result.accepted and '[[MATHML_REF:' not in result.answer
    assert '$$X=A+B$$' in result.answer and FACT in result.answer and REFERENCE.strip() in result.answer
    assert packet.source_json == original and packet.sources[0]['text'] == text and records[0]['text'] == text


def test_partial_preserves_literal_mathml_marker_examples_and_noncanonical_text():
    canonical='[[MATHML_REF:0123456789abcdef01234567]]'
    noncanonical='[[MATHML_REF:documentation-example]]'
    text=('Служебная ссылка '+canonical+'\n\n'
        'Пример синтаксиса: `'+canonical+'`.\n\n'
        '```text\n'+canonical+'\n```\n\n'
        +noncanonical+'\n\n'+FACT)
    packet=build_source_packet([source(text)],ref)
    original=packet.source_json
    result=publish_draft('Непроверенный пересказ.'+REFERENCE,packet,'Объясни синтаксис',reviewer=lambda _:None)
    assert not result.accepted and result.answer.count(canonical)==2
    assert '`'+canonical+'`' in result.answer and '```text\n'+canonical+'\n```' in result.answer
    assert noncanonical in result.answer and FACT in result.answer and REFERENCE.strip() in result.answer
    assert packet.source_json==original and packet.sources[0]['text']==text


def test_formula_id_is_rendered_by_the_source_catalogue_and_bound_to_its_document():
    packet = build_source_packet([source('[MATH: X=A+B]')], ref)
    text = render_formula_ids('[[FORMULA:F1]]', packet)
    assert packet.formulae[0]['display_latex'] in text
    assert '[Источник: ' + ref(source()) + ']' in text
    assert '[[FORMULA:' not in text


def test_formula_ids_inside_literal_code_are_not_executed():
    packet = build_source_packet([source('[MATH: X=A+B]')], ref)
    literal = '`[[FORMULA:F1]]`\n\n```text\n[[FORMULA:F1]]\n```'
    assert render_formula_ids(literal, packet) == literal


@pytest.mark.parametrize('marker', ['[[FORMULA:F99]]', '[[FORMULA:F0]]', '[[FORMULA:missing]]'])
def test_unknown_formula_id_is_never_a_published_mathematical_assertion(marker):
    packet = build_source_packet([source('[MATH: X=A+B]\n\n' + FACT)], ref)
    result = publish_draft(marker + REFERENCE, packet, 'Какое правило?',
        reviewer=lambda _: {'claims': []}, repairer=lambda _: marker)
    assert not result.accepted
    assert marker not in result.answer and FACT in result.answer


def test_typed_valid_claim_verdict_uses_real_passages_not_free_text_quotes():
    packet = build_source_packet([source(unit() + '\n\n' + FACT)], ref)
    draft = 'Источник данных порога — Реестр наблюдений.' + REFERENCE
    result = assess_answer(draft, packet, 'Откуда берут порог?', review_payload)
    assert result.accepted and result.reviewed


@pytest.mark.parametrize('legacy_six_fields',(False,True),ids=('new-five','old-six'))
def test_judgment_keeps_headed_claim_binding_owned_by_the_program(legacy_six_fields):
    packet=build_source_packet([source(unit())],ref)
    draft='### Источник данных\n\nИсточник данных порога — Реестр наблюдений.'+REFERENCE
    def reviewer(prompt):
        value=review_payload(prompt)
        if not legacy_six_fields:
            for row in value['claims']:row.pop('context_ids')
        return value
    result=assess_answer(draft,packet,'Какой источник?',reviewer)
    assert result.accepted and result.reviewed


@pytest.mark.parametrize('legacy_six_fields',(False,True),ids=('new-five','old-six'))
def test_claim_judgment_cannot_borrow_a_passage_from_another_cited_source(legacy_six_fields):
    packet=build_source_packet([source(unit()),{**source('Порог берут из другого реестра.'),
        'file':'other.md','source_id':'other-source'}],ref)
    def reviewer(prompt):
        value=review_payload(prompt)
        for row in value['claims']:
            row['passage_ids']=['E2:P1']
            if not legacy_six_fields:row.pop('context_ids')
        return value
    result=assess_answer('Источник данных порога — Реестр наблюдений.'+REFERENCE,
        packet,'Какой источник?',reviewer)
    assert not result.accepted and result.reasons==('typed_claim_or_condition_unverified',)


def test_new_judgment_schema_keeps_program_owned_fields_in_readonly_input():
    from ordinary_answer_guard import _review_prompt
    prompt=_review_prompt('Источник данных порога — Реестр наблюдений.'+REFERENCE,
        build_source_packet([source(unit())],ref),'Какой источник?')
    schema=json.loads(prompt.split('\nJSON_SCHEMA:\n',1)[1].split('\nДАННЫЕ:\n',1)[0])
    row=schema['properties']['claims']['items']
    assert set(row['required'])=={'claim_id','support','passage_ids','source_binding','material_conditions'}
    assert 'context_ids' not in row['properties'] and 'context_ids' not in row['required']
    data=json.loads(prompt.split('\nДАННЫЕ:\n',1)[1])
    assert 'context_ids' in data['answer_claims'][0]
    assert data['answer_claims'][0]['cited_source_ids']==['E1']


@pytest.mark.parametrize(('binding', 'conditions'), [('other_target', 'complete'), ('exact_target', 'incomplete')])
def test_supported_verdict_cannot_override_wrong_target_or_missing_condition(binding, conditions):
    packet = build_source_packet([source(unit())], ref)
    result = assess_answer('Источник порога — Реестр.' + REFERENCE, packet, 'Какой источник?',
        lambda prompt: review_payload(prompt, binding=binding, conditions=conditions))
    assert not result.accepted and result.repairable


@pytest.mark.parametrize('mutation', ['missing-field', 'unknown-passage', 'duplicate-claim', 'extra-envelope'])
def test_malformed_review_cannot_certify_a_claim(mutation):
    packet = build_source_packet([source(unit())], ref)
    def reviewer(prompt):
        result = review_payload(prompt)
        if mutation == 'missing-field': result['claims'][0].pop('material_conditions')
        if mutation == 'unknown-passage': result['claims'][0]['passage_ids'] = ['E99:P1']
        if mutation == 'duplicate-claim': result['claims'].append(dict(result['claims'][0]))
        if mutation == 'extra-envelope': result['approved'] = True
        return result
    result = assess_answer('Источник порога — Реестр.' + REFERENCE, packet, 'Какой источник?', reviewer)
    assert not result.accepted


def test_one_repair_is_followed_by_verification_and_preserves_a_useful_partial():
    packet = build_source_packet([source(unit() + '\n\n' + FACT)], ref)
    bad = 'Реестр утверждает порог.' + REFERENCE
    reviewer = Mock(side_effect=lambda prompt: review_payload(prompt, binding='other_target'))
    repairer = Mock(return_value=bad)
    result = publish_draft(bad, packet, 'Кто утверждает порог?', reviewer=reviewer, repairer=repairer)
    assert reviewer.call_count == 2 and repairer.call_count == 1
    assert not result.accepted and bad not in result.answer and FACT in result.answer


@pytest.mark.parametrize(('mode','code','check'), [
    ('call-failed','reviewer_call_failed',None),
    ('bad-json','review_invalid_json',None),
    ('missing-rows','review_incomplete','claim_rows'),
    ('extra-envelope','review_schema_invalid','envelope'),
    ('bad-row','review_schema_invalid','claim_schema'),
    ('wrong-id','review_schema_invalid','claim_ids'),
    ('wrong-context','review_schema_invalid','context_binding'),
])
def test_review_failure_diagnostics_are_phase_specific_without_raw_private_text(mode,code,check):
    packet=build_source_packet([source(unit())],ref)
    def reviewer(prompt):
        if mode=='call-failed':raise TimeoutError('synthetic-private-token and endpoint')
        if mode=='bad-json':return '{"synthetic-private-token":'
        value=review_payload(prompt)
        if mode=='missing-rows':value['claims']=[]
        if mode=='extra-envelope':value['private']='synthetic-private-token'
        if mode=='bad-row':value['claims'][0].pop('source_binding')
        if mode=='wrong-id':value['claims'][0]['claim_id']='C99'
        if mode=='wrong-context':value['claims'][0]['context_ids']=['H99']
        return value
    result=assess_answer('Источник порога — Реестр.'+REFERENCE,packet,'Какой источник?',reviewer)
    assert not result.accepted and result.reasons==('semantic_review_unavailable',)
    assert result.diagnostics['code']==code
    if check:assert result.diagnostics['check']==check
    if mode=='call-failed':assert result.diagnostics['exception_type']=='TimeoutError'
    if mode=='bad-json':
        assert result.diagnostics['exception_type']=='JSONDecodeError'
        assert result.diagnostics['json_line']>0 and result.diagnostics['json_column']>0
    assert 'synthetic-private' not in json.dumps(result.diagnostics)


@pytest.mark.parametrize('legacy_six_fields',(False,True),ids=('new-five','old-six'))
def test_real_runtime_stage_http_completion_is_accepted_by_the_guard(monkeypatch,legacy_six_fields):
    from ordinary_answer_guard import _review_prompt
    from test_provider_runtime import stage_http,production_ollama_capture
    packet=build_source_packet([source(unit())],ref)
    draft='Источник данных порога — Реестр наблюдений.'+REFERENCE
    review=review_payload(_review_prompt(draft,packet,'Какой источник?'))
    if not legacy_six_fields:
        for row in review['claims']:row.pop('context_ids')
    body={'model':'same-model','done':True,'done_reason':'stop','response':json.dumps(review,ensure_ascii=False)}
    sent=stage_http(monkeypatch,body)
    runtime=production_ollama_capture()
    identity=runtime.identity
    result=assess_answer(draft,packet,'Какой источник?',lambda prompt:runtime.complete_stage(
        prompt,max_tokens=4096,timeout=120,thinking=False,purpose='evidence-review'))
    assert result.accepted and result.reviewed and len(sent)==1
    assert sent[0]['json']['purpose']=='evidence-review' and sent[0]['json']['options']['num_predict']==4096
    assert sent[0]['timeout']==120 and sent[0]['trust_env'] is False
    assert runtime.identity==identity and runtime.spec.settings['output_token_limit']==-1


def test_real_runtime_incomplete_http_response_produces_private_failure_subcode(monkeypatch):
    from test_provider_runtime import stage_http,production_ollama_capture
    packet=build_source_packet([source(unit())],ref)
    sent=stage_http(monkeypatch,{'model':'same-model','done':True,'done_reason':'length',
        'response':'synthetic-private-token'})
    runtime=production_ollama_capture()
    result=assess_answer('Источник порога — Реестр.'+REFERENCE,packet,'Какой источник?',
        lambda prompt:runtime.complete_stage(prompt,4096,120,thinking=False,purpose='evidence-review'))
    assert not result.accepted and len(sent)==1
    assert result.diagnostics=={'code':'reviewer_call_failed','exception_type':'RuntimeInvocationError'}


def test_guard_failure_subcode_reaches_request_trace_without_raw_response():
    draft='Источник порога — Реестр.'+REFERENCE
    agent=guard_agent(draft)
    agent.llm.complete.side_effect=[draft,'synthetic-private-token']
    answer=agent._generate_grounded_answer('Какой источник?',[source(unit())],[],selection_mode='coverage')
    trace=agent._generation_state.retrieval_trace['publication_verification']
    assert trace['status']=='partial' and trace['review_diagnostics']['code']=='review_invalid_json'
    assert 'synthetic-private' not in json.dumps(trace) and 'synthetic-private' not in answer


def test_cache_proof_binds_policy_packet_question_and_final_answer():
    packet = build_source_packet([source()], ref)
    answer = FACT + REFERENCE
    result = assess_answer(answer, packet, 'Что сохраняется?')
    assert result.accepted
    proof = cache_proof(packet, 'Что сохраняется?', answer, result)
    assert valid_cache_proof(proof, packet, 'Что сохраняется?', answer)
    assert not valid_cache_proof(proof, packet, 'Другой вопрос', answer)
    assert not valid_cache_proof(proof, packet, 'Что сохраняется?', answer + ' Изменение')
    changed = source(); changed['revision_id'] = 'revision-B'
    assert not valid_cache_proof(proof, build_source_packet([changed], ref), 'Что сохраняется?', answer)
    assert not valid_cache_proof({**proof, 'version': 'old'}, packet, 'Что сохраняется?', answer)


def test_review_policy_upgrade_invalidates_proofs_without_changing_native_packet_digest():
    packet=build_source_packet([source()],ref)
    assert packet.digest=='7ab1dbf87c160ca2d19231e1358f3ebe17b276d9facb22d1ca3c14594d26b840'
    answer=FACT+REFERENCE
    result=assess_answer(answer,packet,'Что сохраняется?')
    proof=cache_proof(packet,'Что сохраняется?',answer,result)
    assert valid_cache_proof(proof,packet,'Что сохраняется?',answer)
    assert not valid_cache_proof({**proof,'version':'ordinary-source-packet-v1'},packet,'Что сохраняется?',answer)


@pytest.mark.parametrize('reference', [
    ' [Источник: unrelated.md — раздел «Порядок»]', ' [E99]',
])
def test_verbatim_copy_cannot_borrow_an_unrelated_or_unknown_source(reference):
    other = {**source('Иное правило.'), 'file': 'unrelated.md', 'source_id': 'source-B'}
    packet = build_source_packet([source(), other], ref)
    assert not assess_answer(FACT + reference, packet, 'Что сохраняется?').accepted


def test_plain_verbatim_prefix_cannot_discard_an_atomic_condition():
    packet = build_source_packet([source(FACT + ' Только после завершения осмотра.')], ref)
    assert not assess_answer(FACT + REFERENCE, packet, 'Когда сохраняется запись?').accepted
    assert assess_answer(FACT + ' Только после завершения осмотра.' + REFERENCE,
        packet, 'Когда сохраняется запись?').accepted


def test_a_second_plain_paragraph_can_contain_a_material_condition():
    condition = 'Применяется только в согласованном окне.'
    packet = build_source_packet([source('Порог равен 5.\n\n' + condition)], ref)
    assert not assess_answer('Порог равен 5.' + REFERENCE, packet, 'Как применять порог?').accepted
    assert assess_answer('Порог равен 5.' + REFERENCE + '\n\n' + condition + REFERENCE,
        packet, 'Как применять порог?').accepted


def test_source_free_incomplete_family_limit_is_captured_not_read_again():
    limits = {'family_coverage_incomplete': True, 'family_coverage_incomplete_reason': 'budget'}
    packet = build_source_packet([source()], ref, selection_limits=limits)
    limits.clear()
    result = assess_answer(FACT + REFERENCE, packet, 'Расскажи все варианты')
    assert not result.accepted and result.reasons == ('source_condition_or_family_incomplete',)


def family_packet():
    records = [source('Первый вариант: параметр задают вручную.'),
               {**source('Второй вариант: параметр берут из реестра.'), 'section_path': 'Второй вариант'}]
    family = {'id': 'family-A', 'file': 'protocol.md', 'complete': True,
        'expected_children': [row['section_path'] for row in records],
        'expected_members': [{'section_path': row['section_path'],
            'revision': {'revision_id': row['revision_id']},
            'text_sha256': hashlib.sha256(row['text'].strip().encode()).hexdigest()} for row in records]}
    records[0]['evidence_family'] = family
    for row in records: row['evidence_family_id'] = family['id']
    return records, build_source_packet(records, ref)


@pytest.mark.parametrize('mutation', ['missing-member', 'label-only', 'wrong-revision', 'changed-source-text'])
def test_family_ledger_cannot_be_satisfied_by_labels_or_unbound_members(mutation):
    records, packet = family_packet()
    if mutation == 'wrong-revision': records[1]['revision_id'] = 'revision-B'
    if mutation == 'changed-source-text': records[1]['text'] += ' Дополнение'
    if mutation in {'wrong-revision', 'changed-source-text'}: packet = build_source_packet(records, ref)
    draft = 'Первый вариант.' + REFERENCE
    def reviewer(prompt):
        value = review_payload(prompt)
        value['family_coverage'] = [{'family_id': 'family-A', 'section_path': row['section_path'],
            'status': 'label_only' if mutation == 'label-only' else 'detailed',
            'claim_ids': [value['claims'][0]['claim_id']], 'passage_ids': [f'E{n}:P1']}
            for n, row in enumerate(records, 1)]
        if mutation == 'missing-member': value['family_coverage'].pop()
        return value
    assert not assess_answer(draft, packet, 'Объясни все варианты', reviewer).accepted


def test_family_ledger_cannot_borrow_passages_from_another_file_at_the_same_path():
    records, _ = family_packet()
    records.append({**records[1], 'file': 'unrelated.md', 'source_id': 'foreign-source',
        'text': 'Второй вариант: применяют независимое правило.', 'evidence_family_id': 'foreign-family'})
    packet = build_source_packet(records, ref)
    def reviewer(prompt):
        value = review_payload(prompt)
        value['family_coverage'] = [{'family_id': 'family-A', 'section_path': row['section_path'],
            'status': 'detailed', 'claim_ids': [value['claims'][0]['claim_id']],
            'passage_ids': ['E1:P1' if n == 0 else 'E3:P1']} for n, row in enumerate(records[:2])]
        return value
    draft = ('Первый вариант: параметр задают вручную. Второй вариант: параметр берут из реестра.'
        ' [E1, E2, E3]')
    assert not assess_answer(draft, packet, 'Объясни все варианты', reviewer).accepted


def family_review(prompt):
    data = json.loads(prompt.split('\nДАННЫЕ:\n', 1)[1])
    value = review_payload(prompt)
    for row, original in zip(value['claims'], data['answer_claims']):
        row['passage_ids'] = [p['passage_id'] for source in data['evidence']
            if source['evidence_id'] in original['cited_source_ids'] for p in source['passages']]
    value['family_coverage'] = []
    for member in data['family_review_members']:
        linked = [row for row in value['claims'] if row['claim_id'] in member['eligible_claim_ids']]
        value['family_coverage'].append({'member_id': member['member_id'],
            'status': 'detailed', 'claim_ids': [row['claim_id'] for row in linked],
            'passage_ids': list(member['allowed_passage_ids'])})
    return value


@pytest.mark.parametrize('legacy_six_fields',(False,True),ids=('new-five','old-six'))
def test_complete_family_details_with_their_own_native_witnesses_remain_usable(legacy_six_fields):
    records, packet = family_packet()
    answer = '\n\n'.join(row['text'] + f' [E{n}]' for n, row in enumerate(records, 1))
    def reviewer(prompt):
        value=family_review(prompt)
        if not legacy_six_fields:
            for row in value['claims']:row.pop('context_ids')
        return value
    result = assess_answer(answer, packet, 'Объясни все варианты', reviewer)
    assert result.accepted and result.reviewed


def test_a_judge_cannot_certify_bare_native_labels_as_detailed_algorithms():
    records, packet = family_packet()
    answer = '\n\n'.join(row['section_path'] + f'. [E{n}]' for n, row in enumerate(records, 1))
    assert not assess_answer(answer, packet, 'Объясни все варианты', family_review).accepted


def test_repaired_text_is_rechecked_and_the_verified_repair_can_be_published():
    packet = build_source_packet([source(unit())], ref)
    calls = []
    def reviewer(prompt):
        calls.append(prompt)
        return review_payload(prompt, binding='other_target' if len(calls) == 1 else 'exact_target')
    repairer = Mock(return_value='Источник данных порога — Реестр наблюдений.' + REFERENCE)
    result = publish_draft('Реестр утверждает порог.' + REFERENCE, packet,
        'Кто утверждает порог?', reviewer=reviewer, repairer=repairer)
    assert result.accepted and len(calls) == 2 and repairer.call_count == 1
    assert result.answer.startswith('Источник данных порога')


def test_an_unlisted_review_tail_is_unassessed_instead_of_silently_certified():
    packet = build_source_packet([source(unit())], ref)
    answer = '\n\n'.join(f'Параметр {n} утверждает реестр.' + REFERENCE for n in range(101))
    reviewer = Mock(side_effect=review_payload)
    result = assess_answer(answer, packet, 'Объясни параметры', reviewer)
    assert not result.accepted and 'claim_review_capacity_unassessed' in result.reasons
    reviewer.assert_not_called()


def test_long_complete_native_copy_is_not_subject_to_a_final_answer_limit():
    text = '\n\n'.join(f'Запись {n} сохраняют после проверки.' for n in range(120))
    packet = build_source_packet([source(text)], ref)
    answer = '\n\n'.join(paragraph + REFERENCE for paragraph in text.split('\n\n'))
    result = assess_answer(answer, packet, 'Перечисли все записи')
    assert result.accepted and result.answer == answer


def guard_agent(answer):
    from test_ordinary_formula_fidelity import make_agent
    return make_agent(answer)


def test_packet_is_captured_before_generation_mutates_callers_source_and_trace():
    records = [source()]
    packet = build_source_packet(records, rag_agent.human_source_reference)
    agent = guard_agent(FACT + REFERENCE)
    agent._generation_state.retrieval_trace = {'selection_limits': {}}
    def writer(_):
        records[0]['text'] = 'Изменённое пользователем содержимое после начала запроса.'
        agent._generation_state.retrieval_trace['selection_limits']['family_coverage_incomplete'] = True
        return FACT + REFERENCE
    agent.llm.complete.side_effect = writer
    answer = agent._generate_grounded_answer('Что сохраняется?', records, [],
        selection_mode='coverage', cache_enabled=True)
    assert answer == FACT + REFERENCE
    stored = agent.response_cache.set.call_args.args[1]
    assert stored['ordinary_guard']['packet_sha256'] == packet.digest
    assert stored['answer'] == answer


def test_verified_cache_hit_does_not_append_an_unchecked_formula(monkeypatch):
    agent = guard_agent(FACT + REFERENCE)
    context = [source()]
    answer = agent._generate_grounded_answer('Что сохраняется?', context, [],
        selection_mode='coverage', cache_enabled=True)
    monkeypatch.setattr(rag_agent, 'ensure_requested_formula_coverage', Mock(
        side_effect=AssertionError('Verified cached answer must be returned without semantic changes')))
    assert agent._generate_grounded_answer('Что сохраняется?', context, [],
        selection_mode='coverage', cache_enabled=True) == answer
    assert agent.last_generation_from_cache and agent.llm.complete.call_count == 1


def test_changed_native_revision_invalidates_coverage_cache_without_purging():
    agent = guard_agent(FACT + REFERENCE)
    context = [source()]
    agent._generate_grounded_answer('Что сохраняется?', context, [], selection_mode='coverage', cache_enabled=True)
    context[0]['revision_id'] = 'revision-B'
    agent._generate_grounded_answer('Что сохраняется?', context, [], selection_mode='coverage', cache_enabled=True)
    assert not agent.last_generation_from_cache
    assert agent.llm.complete.call_count == 2 and len(agent.response_cache.data) == 2


def test_a_reused_trace_does_not_carry_family_limits_into_the_next_retrieval():
    from test_applied_selector_mode import retrieval_agent, QUESTION
    agent, index = retrieval_agent()
    def select(*args, **kwargs):
        assert kwargs['selection_meta'] == {}
        return []
    index.select_v2.side_effect = select
    trace = {'selection_limits': {'family_coverage_incomplete': True,
        'family_coverage_incomplete_reason': 'prior-request'}}
    agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,), selection_mode='coverage', retrieval_trace=trace)
    assert trace['selection_limits'] == {}
