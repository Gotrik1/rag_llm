"""Pure optional-view contracts; no model, network or application integration."""
from copy import deepcopy
import json

import pytest

from table_context import TABLE_UNIT_PREFIX, parse_table_unit, serialize_table_unit
from table_model_view import table_model_view


def payload():
    text = 'Ячейка  е\u0308 / \\frac{A_1}{B} \\n "цитата"\t\u2028\u2029 [TABLE_ROW_V1]'
    return {
        'schema': 'table-grid-v1', 'table_id': 'a' * 64, 'row': 4,
        'row_kind': 'body', 'header_status': 'unknown',
        'headers': [
            {'row': 0, 'column': 0, 'span': 2, 'text': 'Заголовок\nсо строкой'},
            {'row': 1, 'column': 2, 'span': 3, 'text': 'Возможный заголовок'},
        ],
        'cells': [{'row': 4, 'column': 2, 'span': 3, 'text': text,
                   'start': 2, 'end': len(text) + 2, 'total_chars': len(text) + 9,
                   'merge_origin': {'row': 1, 'column': 2, 'span': 3, 'text': 'Исходное объединение'},
                   'merge_unresolved': True}],
        'context': [{'row': 4, 'column': 0, 'span': 2, 'text': 'Условие строки',
                     'start': 0, 'end': 14, 'total_chars': 14, 'merge_unresolved': False}],
        'warnings': ['неизвестный заголовок', 'fragment-continuation', 'неизвестный заголовок'],
    }


@pytest.mark.parametrize('source', ['', 'Обычный текст', '  текст\r\n\n',
    'Формула \\(K=K_s*K_t\\)\u2028и \\frac{x}{y}', 'Юникод е\u0308 \u2029', 'x\ry'])
def test_plain_source_is_returned_exactly_and_by_identity(source):
    assert table_model_view(source) is source


def test_all_complex_payload_values_survive_json_roundtrip_without_envelope():
    original = payload()
    snapshot = deepcopy(original)
    unit = serialize_table_unit(original)
    result = table_model_view(unit)
    assert result == {'source_segments': [{'table': original}]}
    assert json.loads(json.dumps(result, ensure_ascii=False)) == result
    assert result['source_segments'][0]['table'] == parse_table_unit(unit)
    assert original == snapshot
    assert set(result['source_segments'][0]) == {'table'}
    assert set(result['source_segments'][0]['table']) == set(original)
    assert result['source_segments'][0]['table']['table_id'] == 'a' * 64


@pytest.mark.parametrize('status', ['explicit', 'candidate', 'unknown'])
def test_header_status_and_candidate_values_are_never_upgraded(status):
    original = payload()
    original['header_status'] = status
    assert table_model_view(serialize_table_unit(original))['source_segments'][0]['table'] == original


@pytest.mark.parametrize('ending', ['\n', '\r\n'])
def test_prose_blank_lines_line_endings_and_table_exterior_whitespace_are_exact(ending):
    original = payload()
    unit = serialize_table_unit(original)
    before = 'Начало\u2028е\u0308  ' + ending + ending
    after = '\t ' + ending + ending + 'Конец\u2029'
    source = before + unit + after
    assert table_model_view(source) == {'source_segments': [
        {'text': before}, {'table': original}, {'text': after}]}


@pytest.mark.parametrize('padding', ['', ' ', '\t\r', '\r\n', '\n\n'])
def test_eof_padding_is_retained_without_trimming_to_validate(padding):
    original = payload()
    expected = [{'table': original}]
    if padding:
        expected.append({'text': padding})
    assert table_model_view(serialize_table_unit(original) + padding) == {'source_segments': expected}


def test_separate_units_fragments_and_table_ids_are_never_joined_or_aliased():
    first, second = payload(), payload()
    second['table_id'] = 'b' * 64
    second['cells'][0].update(start=30, end=30 + len(second['cells'][0]['text']),
                              total_chars=90 + len(second['cells'][0]['text']))
    source = serialize_table_unit(first) + '\n\n' + serialize_table_unit(second)
    assert table_model_view(source) == {'source_segments': [
        {'table': first}, {'text': '\n\n'}, {'table': second}]}


def test_two_fragments_of_same_cell_are_kept_separate_and_in_source_order():
    first, second = payload(), payload()
    first['cells'] = [{'row': 4, 'column': 2, 'span': 1, 'text': 'A=', 'start': 0, 'end': 2, 'total_chars': 5}]
    second['cells'] = [{'row': 4, 'column': 2, 'span': 1, 'text': 'B+C', 'start': 2, 'end': 5, 'total_chars': 5}]
    source = serialize_table_unit(second) + '\n' + serialize_table_unit(first)
    assert table_model_view(source) == {'source_segments': [
        {'table': second}, {'text': '\n'}, {'table': first}]}


def test_mutating_one_result_cannot_change_input_or_later_projection():
    original = payload()
    unit = serialize_table_unit(original)
    result = table_model_view(unit)
    result['source_segments'][0]['table']['cells'][0]['text'] = 'changed'
    assert table_model_view(unit)['source_segments'][0]['table'] == original


def invalid_checksum():
    value = json.loads(serialize_table_unit(payload())[len(TABLE_UNIT_PREFIX):])
    value['sha256'] = '0' * 64
    return TABLE_UNIT_PREFIX + json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize('bad', [
    '[TABLE_ROW_V1', '[TABLE_ROW_V1]', '[TABLE_ROW_V1] ', '[TABLE_ROW_V1] {',
    '[TABLE_ROW_V1] {"payload":', '[TABLE_ROW_V2] {}',
    '  [TABLE_ROW_V1] {}', 'quoted "[TABLE_ROW_V1] {}"',
    '[TABLE_ROW_V1] {} trailing prose',
])
def test_bad_quoted_embedded_and_incomplete_markers_cause_whole_source_fallback(bad):
    valid = serialize_table_unit(payload())
    for source in (bad, valid + '\n' + bad, bad + '\n' + valid):
        assert table_model_view(source) is source


@pytest.mark.parametrize('make_bad', [
    lambda valid: invalid_checksum(),
    lambda valid: ' ' + valid,
    lambda valid: '\t' + valid,
    lambda valid: 'prefix ' + valid,
    lambda valid: '"' + valid + '"',
    lambda valid: valid + ' suffix',
    lambda valid: json.dumps(valid, ensure_ascii=False),
    lambda valid: valid.replace('"schema":"table-grid-v1"', '"schema":"unknown"'),
])
def test_valid_row_never_leaks_from_a_partially_invalid_source(make_bad):
    valid = serialize_table_unit(payload())
    bad = make_bad(valid)
    assert bad != valid
    source = 'До\n' + valid + '\n' + bad + '\nПосле'
    assert table_model_view(source) is source


def test_literal_marker_inside_a_verified_cell_is_only_cell_data():
    original = payload()
    assert '[TABLE_ROW_V1]' in original['cells'][0]['text']
    assert table_model_view(serialize_table_unit(original))['source_segments'][0]['table'] == original


def test_physical_newline_inside_broken_json_is_not_repaired():
    unit = serialize_table_unit(payload())
    broken = unit.replace('"table-grid-v1"', '"table-\ngrid-v1"')
    assert table_model_view(broken) is broken


@pytest.mark.parametrize('fence', [
    '```text', '```', '~~~', '~~~python', ' ```text', '  ```',
    '   ~~~text', '   ~~~', '`' * 12 + 'text', '~' * 24,
])
def test_any_physical_fence_causes_whole_source_fallback(fence):
    unit = serialize_table_unit(payload())
    for source in (
        fence + '\n' + unit + '\n```',
        unit + '\n' + fence,
        'Unrelated code\n' + fence + '\nordinary code\n```\n' + unit,
    ):
        assert table_model_view(source) is source


def test_fence_without_any_table_marker_is_exact_plain_source():
    source = '```text\nordinary data\n```\r\n'
    assert table_model_view(source) is source


def test_fences_inside_verified_cells_remain_exact_table_data():
    original = payload()
    text = '```text\n[not a physical outer line]\n```\r\n   ~~~\n' + '~' * 24
    original['cells'][0].update(text=text, start=2, end=len(text) + 2,
                               total_chars=len(text) + 9)
    unit = serialize_table_unit(original)
    assert table_model_view(unit) == {'source_segments': [{'table': original}]}


@pytest.mark.parametrize('source', [None, 1, True, {}, []])
def test_non_source_values_are_not_coerced(source):
    with pytest.raises(TypeError, match='Source must be a string'):
        table_model_view(source)
