"""Network-free contracts for lossless writer payload serialization."""
from copy import deepcopy
from dataclasses import replace
import json
import random

import pytest

from prompt_payload import (
    SOURCE_INSTRUCTION, decode_writer_payload, encode_writer_payload,
)


LONG = 'Каталог исходных методик / ' + 'Подробное неизменённое название источника ' * 10 + '.docx'


def sample():
    return {'question': 'Основное правило и исключение?', 'user_task': {'question': 'Точная задача'},
            'coverage': {'assessed': False, 'missing_requirements': ['R2']},
            'evidence': [
                {'evidence_id': 'E7', 'file': LONG, 'file_name': '', 'file_path': LONG,
                 'revision_id': 'r1', 'section_path': 'Основное правило',
                 'text': '  A = B × C\n\tесли условие α; иначе — нет.  ',
                 'quotes': ['Дословная цитата\nс условием.'], 'unknown': {'source_ref': 'nested-data'}},
                {'evidence_id': 'E2', 'file': LONG, 'file_name': '', 'file_path': LONG,
                 'revision_id': 'r2', 'section_path': 'Исключение', 'text': 'Исключение: только при β.',
                 'evidence_family': {'members': ['E7', 'E2'], 'complete': False}},
            ], 'native_formulae': [{'latex': r'C = Q\cdot T', 'definitions': ['Q — объём']}],
            'source_reading': {'quoted_passages': {'E7:P1': {'quote': 'Полное неизменённое условие'}}}}


def test_lossless_roundtrip_preserves_every_value_order_and_input():
    original = sample()
    before = deepcopy(original)
    encoded = encode_writer_payload(original)
    packed = json.loads(encoded.json_text)
    assert encoded.interned and encoded.instruction == SOURCE_INSTRUCTION
    assert decode_writer_payload(encoded) == original == before
    assert [row['evidence_id'] for row in packed['evidence']] == ['E7', 'E2']
    assert list(packed['source_catalogue']) == ['S1']
    assert all(row['source_ref'] == 'S1' for row in packed['evidence'])
    assert packed['source_catalogue']['S1'] == {'file': LONG, 'file_name': '', 'file_path': LONG}
    for old, new in zip(original['evidence'], packed['evidence']):
        assert {k: v for k, v in old.items() if k not in {'file', 'file_name', 'file_path'}} == {
            k: v for k, v in new.items() if k != 'source_ref'}


@pytest.mark.parametrize('kind', ['absent', 'none', 'empty', 'false', 'number', 'container'])
def test_source_presence_none_empty_and_nonstring_values_are_distinct(kind):
    value = {'none': None, 'empty': '', 'false': False, 'number': 7, 'container': {'path': LONG}}.get(kind)
    original = sample()
    for row in original['evidence']:
        row.pop('file_name')
        if kind != 'absent':
            row['file_name'] = deepcopy(value)
    encoded = encode_writer_payload(original)
    decoded = decode_writer_payload(encoded)
    assert decoded == original
    assert ('file_name' in decoded['evidence'][0]) is (kind != 'absent')
    if kind != 'absent':
        assert type(decoded['evidence'][0]['file_name']) is type(value)


def test_different_source_spelling_and_field_presence_never_merge():
    original = sample()
    rows = original['evidence']
    rows[1]['file'] = LONG.upper()
    rows += [deepcopy(rows[0]), deepcopy(rows[1])]
    rows[2]['evidence_id'], rows[3]['evidence_id'] = 'E9', 'E4'
    encoded = encode_writer_payload(original)
    packed = json.loads(encoded.json_text)
    assert len(packed['source_catalogue']) == 2
    assert packed['evidence'][0]['source_ref'] != packed['evidence'][1]['source_ref']
    assert decode_writer_payload(encoded) == original


@pytest.mark.parametrize('location,key,value', [
    ('root', 'source_catalogue', None), ('root', 'source_ref', 'S1'),
    ('row', 'source_ref', None), ('row', 'source_catalogue', {}),
])
def test_reserved_name_collision_is_compact_only_and_decodes_without_guessing(location, key, value):
    original = sample()
    target = original if location == 'root' else original['evidence'][0]
    target[key] = value
    encoded = encode_writer_payload(original)
    assert not encoded.interned and not encoded.instruction
    assert decode_writer_payload(encoded) == original


@pytest.mark.parametrize('original', [
    {}, {'evidence': []}, {'evidence': None}, {'evidence': 'unknown'},
    {'evidence': [{'file': 'a'}, {'file': 'a'}]},
    {'evidence': [{'file': LONG}, {'file': LONG + 'different'}]},
    {'evidence': [None, False, 2, 'opaque', {'file': {'unknown': 'object'}}]},
])
def test_tiny_unrepeated_or_unknown_evidence_is_compact_only(original):
    before = deepcopy(original)
    encoded = encode_writer_payload(original)
    assert not encoded.interned and encoded.instruction == ''
    assert encoded.json_text == json.dumps(original, ensure_ascii=False, separators=(',', ':'))
    assert decode_writer_payload(encoded) == original == before


def test_byte_saving_gate_includes_the_actual_instruction():
    # JSON alone can save a little, but adding the reading instruction would cost more.
    original = {'evidence': [{'file': 'x' * 120}, {'file': 'x' * 120}]}
    compact = json.dumps(original, ensure_ascii=False, separators=(',', ':'))
    assert len(SOURCE_INSTRUCTION.encode('utf-8')) > 120
    assert not encode_writer_payload(original).interned
    encoded = encode_writer_payload(sample())
    baseline = json.dumps(sample(), ensure_ascii=False, separators=(',', ':'))
    assert len((encoded.instruction + encoded.json_text).encode('utf-8')) < len(baseline.encode('utf-8'))


def test_byte_gate_does_not_mistake_russian_instruction_characters_for_bytes():
    original = {'evidence': [{'file': 'x' * 300}, {'file': 'x' * 300}]}
    compact = json.dumps(original, ensure_ascii=False, separators=(',', ':'))
    hypothetical = json.dumps({'source_catalogue': {'S1': {'file': 'x' * 300}},
        'evidence': [{'source_ref': 'S1'}, {'source_ref': 'S1'}]}, separators=(',', ':'))
    saved = len(compact) - len(hypothetical)
    assert len(SOURCE_INSTRUCTION) < saved < len(SOURCE_INSTRUCTION.encode('utf-8'))
    assert not encode_writer_payload(original).interned


def test_collision_fallback_that_looks_encoded_is_not_decoded_as_a_reference():
    original = {'source_catalogue': {'S1': {'file': LONG}},
                'evidence': [{'source_ref': 'S1', 'text': 'Not an encoded payload.'}]}
    encoded = encode_writer_payload(original)
    assert not encoded.interned
    assert decode_writer_payload(encoded) == original


def test_mixed_unknown_rows_and_partial_source_strings_remain_lossless():
    original = sample()
    original['evidence'] += [None, True, 'opaque', 2, ['nested', {'file': LONG}]]
    original['evidence'][0]['file_name'] = None
    original['evidence'][1].pop('file_name')
    encoded = encode_writer_payload(original)
    assert encoded.interned
    decoded = decode_writer_payload(encoded)
    assert decoded == original
    assert decoded['evidence'][0]['file_name'] is None
    assert 'file_name' not in decoded['evidence'][1]


def test_deterministic_varied_json_roundtrips_without_input_mutation():
    rng = random.Random(2941)
    for number in range(200):
        original = {'question': 'Вопрос\n' + str(number), 'unknown': [None, True, 1.25, {'text': '\tβ\n'}],
                    'evidence': []}
        for index in range(rng.randrange(1, 9)):
            row = {'evidence_id': f'E{index + 1}', 'text': f'Условие {index}: α\n  β\t',
                   'revision_id': rng.choice(['r1', 'r2']), 'ordered': [3, 1, 2]}
            for field in ('file', 'file_name', 'file_path'):
                if rng.randrange(5):
                    row[field] = rng.choice([LONG, LONG.upper(), '', None, False, 7, ['unknown']])
            original['evidence'].append(row)
        if number % 17 == 0:
            original['source_catalogue'] = None
        before = deepcopy(original)
        encoded = encode_writer_payload(original)
        assert encode_writer_payload(original) == encoded
        assert decode_writer_payload(encoded) == original == before
        baseline = json.dumps(original, ensure_ascii=False, separators=(',', ':'))
        assert len((encoded.instruction + encoded.json_text).encode('utf-8')) <= len(baseline.encode('utf-8'))


@pytest.mark.parametrize('mutation', ['unknown-ref', 'null-ref', 'field-collision', 'bad-catalogue', 'unused', 'single-use'])
def test_decoder_rejects_tampered_references_instead_of_guessing(mutation):
    encoded = encode_writer_payload(sample())
    data = json.loads(encoded.json_text)
    if mutation == 'unknown-ref':
        data['evidence'][0]['source_ref'] = 'S404'
    elif mutation == 'null-ref':
        data['evidence'][0]['source_ref'] = None
    elif mutation == 'field-collision':
        data['evidence'][0]['file'] = 'forged'
    elif mutation == 'bad-catalogue':
        data['source_catalogue']['S1']['revision_id'] = 'not-internable'
    elif mutation == 'unused':
        data['source_catalogue']['S2'] = {'file': 'unused'}
    else:
        data['evidence'].pop()
    forged = replace(encoded, json_text=json.dumps(data, ensure_ascii=False))
    with pytest.raises(ValueError):
        decode_writer_payload(forged)


def writer_json(prompt):
    start = prompt.index('{"question":')
    return json.JSONDecoder().raw_decode(prompt[start:])[0]


def test_generation_prompt_uses_same_lossless_representation_and_eid_citations():
    from corrective_rag import SearchPlan, generation_prompt
    original = sample()
    before = deepcopy(original)
    evidence = original['evidence']
    prompt = generation_prompt(original['question'], SearchPlan(('Правило',), ('Поиск',)), evidence,
                               {'assessed': False, 'complete': False})
    data = writer_json(prompt)
    assert 'source_catalogue' in data and SOURCE_INSTRUCTION in prompt
    assert 'Используй только фактические ID из evidence: [E7], [E2].' in prompt
    assert data['evidence'][0]['evidence_id'] == 'E7' and data['evidence'][1]['evidence_id'] == 'E2'
    assert original == before
    for old, new in zip(evidence, data['evidence']):
        assert new['text'] == old['text'] and new['revision_id'] == old['revision_id']


def test_generation_prompt_tiny_payload_does_not_add_catalogue_instructions():
    from corrective_rag import SearchPlan, generation_prompt
    prompt = generation_prompt('Вопрос?', SearchPlan(('Правило',), ('Поиск',)),
        [{'evidence_id': 'E1', 'file': 'tiny.md', 'text': 'Точное условие.'}], {})
    assert 'source_catalogue' not in writer_json(prompt)
    assert SOURCE_INSTRUCTION not in prompt
