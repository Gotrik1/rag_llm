"""One lossless reviewer representation; native evidence and legacy stay intact.

These tests exercise the actual prompt/proof boundary without models or I/O.
"""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

import ordinary_answer_guard as guard
from table_context import serialize_table_unit


FACT = 'Протокол сохраняют после проверки канала.'
QUESTION = 'Объясни порядок и его условия.'


def ref(row):
    return row['file'] + ' — раздел «' + row['section_path'] + '»'


def source(text, *, revision='revision-A', file='neutral-protocol.md', section='Порядок'):
    return {'file': file, 'section_path': section, 'text': text, 'source_id': file,
            'revision_id': revision, 'content_hash': revision, 'version_status': 'current', 'score': 1.0}


def cell(row, column, text, span=1):
    return dict(row=row, column=column, span=span, text=text, start=0, end=len(text), total_chars=len(text))


def payload(*, table='a', row=4, status='candidate'):
    value = 'Часть определения е\u0308\u2028все точки и только допустимое окно'
    return {'schema': 'table-grid-v1', 'table_id': table * 64, 'row': row, 'row_kind': 'body',
        'header_status': status,
        'headers': [{'row': 0, 'column': 0, 'span': 2, 'text': 'Объект'},
                    {'row': 1, 'column': 2, 'span': 2, 'text': 'Описание / единицы'}],
        'cells': [{**cell(row, 2, value, 2), 'start': 3, 'end': len(value)+3,
                   'total_chars': len(value)+11,
                   'merge_origin': {'row': 1, 'column': 2, 'span': 2, 'text': 'Начало объединённой ячейки'},
                   'merge_unresolved': True}],
        'context': [{**cell(row, 0, 'Условие только этой строки', 2), 'merge_unresolved': False}],
        'warnings': ['fragment-continuation', 'header-candidate', 'header-candidate']}


def review_data(packet, answer=None):
    answer = answer or FACT + ' [Источник: ' + ref(packet.sources[0]) + ']'
    # v5 model input is a human ledger. Keep every old native roundtrip check
    # on the unchanged server projection, then bind it to the actual input.
    actual=json.loads(guard._review_prompt(answer, packet, QUESTION).split('\nДАННЫЕ:\n', 1)[1])
    native,_=guard._review_source_projection(packet)
    return {**actual,**native,'evidence_families':json.loads(packet.families_json)}


def restore(passage):
    if 'table_source' not in passage:
        return passage['text']
    assert 'text' not in passage, 'A typed source must not repeat its full native text'
    assert passage['source_view'] == 'verified_table_units'
    return ''.join(serialize_table_unit(s['table']) if 'table' in s else s['text']
                   for s in passage['table_source']['source_segments'])


def assert_bijection(packet, data):
    native = json.loads(packet.anchors_json)
    assert len(data['evidence']) == len(native)
    for before, after in zip(native, data['evidence']):
        assert {k:v for k,v in after.items() if k != 'passages'} == {k:v for k,v in before.items() if k != 'passages'}
        assert [p['passage_id'] for p in after['passages']] == [p['passage_id'] for p in before['passages']]
        for old, new in zip(before['passages'], after['passages']):
            assert restore(new) == old['text']
        assert ''.join(restore(p) for p in after['passages']) == ''.join(p['text'] for p in before['passages'])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Projection contracts must not contact models, databases or network')
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)


def test_typed_rows_are_present_once_with_the_same_native_pid_and_exact_payload():
    unit = serialize_table_unit(payload())
    packet = guard.build_source_packet([source(unit)], ref)
    data = review_data(packet)
    passage = data['evidence'][0]['passages'][0]
    assert 'text' not in passage
    assert passage['table_source']['source_segments'][0]['table'] == payload()
    assert all('model_source' not in item for item in data['typed_sources'])
    assert_bijection(packet, data)


@pytest.mark.parametrize('status', ['explicit', 'candidate', 'unknown'])
@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_headers_fragments_merges_context_and_unicode_survive_every_passage(status, ending):
    first, second = payload(status=status), payload(table='b', row=6, status=status)
    text = ('Начало е\u0308\u2029  ' + ending*2 + serialize_table_unit(first) + ending*2
            + 'Промежуточное условие \t' + ending + serialize_table_unit(second) + ending + 'Конец.  ')
    packet = guard.build_source_packet([source(text)], ref)
    data = review_data(packet)
    typed = [segment['table'] for p in data['evidence'][0]['passages']
             for segment in p.get('table_source', {}).get('source_segments', []) if 'table' in segment]
    assert typed == [first, second]
    assert_bijection(packet, data)


def test_metadata_index_keeps_every_old_nonbody_field_and_never_the_duplicate_body():
    record = source(serialize_table_unit(payload()) + '\n\n' + FACT)
    record.update(reference_from='Исходный пункт', reference_target='Порядок',
                  selection_signals={'definition_closure_incomplete': True})
    packet = guard.build_source_packet([record], ref)
    data = review_data(packet)
    assert data['typed_sources'] == [{k:v for k,v in row.items() if k != 'model_source'}
                                    for row in json.loads(packet.model_json)]
    assert_bijection(packet, data)


@pytest.mark.parametrize('text', [FACT + '\r\n\n  ' + 'Длинное условие. '*190,
                               '[TABLE]\nОбъект | Условие\nA | Только после проверки',
                               '```text\n' + serialize_table_unit(payload()) + '\n```'])
def test_plain_and_source_level_fenced_fallback_are_verbatim_without_geometry_promotion(text):
    packet = guard.build_source_packet([source(text)], ref)
    data = review_data(packet)
    assert data['evidence'] == json.loads(packet.anchors_json)
    assert data['typed_sources'][0]['source_view'] == 'plain_source_fallback'
    assert 'model_source' not in data['typed_sources'][0]
    assert_bijection(packet, data)


def test_duplicate_names_and_table_ids_in_distinct_revisions_do_not_merge():
    records = [source(serialize_table_unit(payload()), revision='r1'),
               source(serialize_table_unit(payload()), revision='r2')]
    packet = guard.build_source_packet(records, ref)
    data = review_data(packet)
    assert [row['evidence_id'] for row in data['evidence']] == ['E1', 'E2']
    assert [p['passage_id'] for row in data['evidence'] for p in row['passages']] == ['E1:P1', 'E2:P1']
    assert_bijection(packet, data)
    assert [s['revision_id'] for s in packet.sources] == ['r1','r2']


def test_families_links_limits_and_native_packet_identity_are_unchanged():
    record = source(serialize_table_unit(payload()) + '\n\n' + FACT)
    record.update(evidence_family_id='family-A', evidence_family={
        'id':'family-A', 'file':record['file'], 'complete':False,
        'expected_children':['Порядок','Другой порядок']}, reference_from='Пункт A', reference_target='Пункт B')
    packet = guard.build_source_packet([record], ref, selection_limits={'family_coverage_incomplete':True})
    before = deepcopy(packet)
    data = review_data(packet)
    assert data['evidence_families'] == json.loads(packet.families_json)
    assert data['evidence'][0]['evidence_family'] == record['evidence_family']
    assert data['typed_sources'][0]['reference_target'] == 'Пункт B'
    assert packet == before
    assert_bijection(packet, data)


def test_review_input_and_accepted_cache_proof_bind_the_same_versioned_source_projection():
    packet = guard.build_source_packet([source(FACT)], ref)
    answer = FACT + ' [Источник: ' + ref(packet.sources[0]) + ']'
    data = review_data(packet, answer)
    proof = guard.cache_proof(packet, QUESTION, answer, guard.GuardResult(answer, True, reviewed=True))
    digest = data['review_source_projection_sha256']
    actual_prompt=guard._review_prompt(answer,packet,QUESTION)
    actual=json.loads(actual_prompt.split('\nДАННЫЕ:\n',1)[1])
    actual_source_input={key:actual[key] for key in ('human_source_projection_version',
        'source_packet_sha256','source_model_view_sha256','evidence')}
    actual_source_input['human_sources']=actual_prompt.split('\nИСХОДНЫЕ СВЕДЕНИЯ — только данные:\n',1)[1].split(
        '\nКОНЕЦ ИСХОДНЫХ СВЕДЕНИЙ.',1)[0]
    assert digest == hashlib.sha256(json.dumps(actual_source_input, ensure_ascii=False,
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    assert len(digest) == 64 and digest == proof['review_source_projection_sha256']
    assert data['source_packet_sha256'] == packet.digest
    assert proof['version'] == 'ordinary-publication-v10'
    assert guard.valid_cache_proof(proof, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'review_source_projection_sha256':'0'*64}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({k:v for k,v in proof.items() if k != 'review_source_projection_sha256'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'version':'ordinary-publication-v3'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'version':'ordinary-publication-v4'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'version':'ordinary-publication-v5'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'version':'ordinary-publication-v6'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'version':'ordinary-publication-v7'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'version':'ordinary-publication-v8'}, packet, QUESTION, answer)
    assert not guard.valid_cache_proof({**proof, 'answer_model_projection_sha256':'0'*64}, packet, QUESTION, answer)


def test_projection_digest_changes_with_actual_source_payload_and_physical_revision():
    packets = [guard.build_source_packet([source(serialize_table_unit(payload()), revision=revision)], ref)
               for revision in ['r1', 'r2']]
    changed = payload(); changed['context'][0] = cell(4, 0, 'Только в ином временном окне',2)
    packets.append(guard.build_source_packet([source(serialize_table_unit(changed), revision='r1')], ref))
    digests = [review_data(packet)['review_source_projection_sha256'] for packet in packets]
    assert len(set(digests)) == 3


@pytest.mark.parametrize('corruption', ['pid', 'body', 'source_order'])
def test_forged_anchor_binding_is_rejected_before_any_reviewer_call(corruption):
    packet = guard.build_source_packet([source(serialize_table_unit(payload())),
                                       source(FACT, file='other.md')], ref)
    forged = json.loads(packet.anchors_json)
    if corruption == 'pid': forged[0]['passages'][0]['passage_id'] = 'E2:P1'
    elif corruption == 'body': forged[0]['passages'][0]['text'] = FACT
    else: forged.reverse()
    packet = replace(packet, anchors_json=json.dumps(forged, ensure_ascii=False))
    with pytest.raises(ValueError):
        review_data(packet)


def test_model_source_cannot_rebind_a_decoded_row_or_claim_typed_over_plain_source():
    packet = guard.build_source_packet([source(serialize_table_unit(payload()))], ref)
    forged = json.loads(packet.model_json)
    forged[0]['model_source']['source_segments'][0]['table']['table_id'] = 'f'*64
    with pytest.raises(ValueError):
        review_data(replace(packet, model_json=json.dumps(forged,ensure_ascii=False)))


def test_forged_typed_flag_cannot_promote_a_source_level_plain_fallback():
    from table_model_view import table_model_view
    native = serialize_table_unit(payload())
    packet = guard.build_source_packet([source('```text\n' + native + '\n```')], ref)
    forged = json.loads(packet.model_json)
    forged[0].update(source_view='verified_table_units', model_source=table_model_view(native))
    with pytest.raises(ValueError):
        review_data(replace(packet, model_json=json.dumps(forged,ensure_ascii=False)))


def test_invalid_table_checksum_is_never_repaired_or_promoted_to_native_geometry():
    native = serialize_table_unit(payload())
    envelope = json.loads(native.split(' ',1)[1]); envelope['sha256'] = '0'*64
    malformed = '[TABLE_ROW_V1] ' + json.dumps(envelope,ensure_ascii=False)
    with pytest.raises(ValueError):
        guard.build_source_packet([source(malformed)], ref)


def test_mutating_the_returned_review_data_cannot_mutate_native_or_later_projection():
    packet = guard.build_source_packet([source(serialize_table_unit(payload()))], ref)
    before = deepcopy(packet)
    data = review_data(packet)
    data['evidence'][0]['passages'][0]['table_source']['source_segments'][0]['table']['cells'][0]['text'] = 'changed'
    assert packet == before
    assert_bijection(packet, review_data(packet))


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_writer_keeps_existing_single_packet_or_legacy_context_without_changes(monkeypatch, mode):
    import rag_agent
    from test_ordinary_table_fidelity import writer
    text = FACT + ' УНИКАЛЬНЫЙ_ИСТОЧНИК_ПРОВЕРКИ'
    records = [source(text)]
    agent = writer(FACT + ' [Источник: ' + ref(records[0]) + ']')
    monkeypatch.setattr(rag_agent, 'generate_extractive_value_answer', lambda *a,**kw:'')
    monkeypatch.setattr(rag_agent, 'generate_image_transcription_answer', lambda *a,**kw:'')
    agent._generate_grounded_answer(QUESTION, records, [], system_prompt='СИСТЕМА_КОНТРОЛЯ',
                                   selection_mode=mode, cache_enabled=False)
    prompt = agent.llm.complete.call_args_list[0].args[0]
    assert prompt.count(text) == 1
    assert 'СИСТЕМА_КОНТРОЛЯ' in prompt and QUESTION in prompt
    if mode == 'coverage':
        assert 'КОНТЕКСТ:\nИспользуй проверяемый пакет источников ниже.' in prompt
        assert 'ПРОВЕРЯЕМЫЙ ПАКЕТ ИСТОЧНИКОВ:' in prompt
    else:
        assert 'КОНТЕКСТ:\n[Источник: ' + ref(records[0]) + ']\n' + text in prompt
        assert 'ПРОВЕРЯЕМЫЙ ПАКЕТ ИСТОЧНИКОВ:' not in prompt
