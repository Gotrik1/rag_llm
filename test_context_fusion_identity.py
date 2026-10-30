"""Hybrid source fusion must preserve exact native text/provenance bindings."""
from copy import deepcopy

import pytest

from web_ui import _fuse_contexts


def record(text, **metadata):
    return {'file': 'guide.txt', 'section_path': 'Rule', 'status': 'active',
            'text': text, 'score': 1.0, **metadata}


@pytest.mark.parametrize('same_flow', [False, True])
def test_equal_prefix_is_not_proof_of_a_duplicate(same_flow):
    prefix = 'Shared introductory source text. ' * 25
    first = record(prefix + ' Apply the rate before delivery.')
    second = record(prefix + ' Apply the rate after delivery.')
    before = deepcopy([first, second])
    assert first['text'][:500] == second['text'][:500]
    result = _fuse_contexts([first, second] if same_flow else [first],
                            [] if same_flow else [second], top_k=10)
    assert {row['text'] for row in result} == {first['text'], second['text']}
    assert len(result) == 2
    assert [first, second] == before


@pytest.mark.parametrize('field,left,right', [
    ('revision_id', 'r1', 'r2'),
    ('document_id', 'doc-a', 'doc-b'),
    ('source_id', 'upload-a', 'upload-b'),
    ('page_number', 1, 2),
    ('page_label', 'I', 'II'),
    ('file', 'one/guide.txt', 'two/guide.txt'),
    ('file_path', 'one/guide.txt', 'two/guide.txt'),
    ('section_path', 'Rule', 'RULE'),
    ('version_id', 1, '1'),
])
def test_equal_text_does_not_merge_different_source_bindings(field, left, right):
    first, second = record('The value is seven.'), record('The value is seven.')
    first[field], second[field] = left, right
    result = _fuse_contexts([first], [second], top_k=10)
    assert len(result) == 2
    assert result[0][field] == left and type(result[0][field]) is type(left)
    assert result[1][field] == right and type(result[1][field]) is type(right)


@pytest.mark.parametrize('second_text', ['X = A + B.', 'x = a + b.', 'X= A + B.'])
def test_whitespace_and_symbol_case_are_not_rewritten_for_equality(second_text):
    first = record('X=A+B.')
    second = record(second_text)
    result = _fuse_contexts([first], [second], top_k=10)
    assert [row['text'] for row in result] == [first['text'], second_text]


@pytest.mark.parametrize('value', [None, '', 0, False])
def test_absent_locator_is_not_an_explicit_falsey_locator(value):
    first, second = record('Exact source text.'), record('Exact source text.', page_number=value)
    result = _fuse_contexts([first], [second], top_k=10)
    assert len(result) == 2
    assert 'page_number' not in result[0]
    assert 'page_number' in result[1] and type(result[1]['page_number']) is type(value)


def test_proven_exact_duplicates_keep_python_priority_without_mutating_sources():
    first = record('Exact source text.', revision_id='r1', page_number=2)
    second = {**first, 'score': 99.0}
    before = deepcopy([first, second])
    result = _fuse_contexts([first], [second], top_k=10)
    assert len(result) == 1
    assert result[0]['flow_origin'] == 'python'
    assert result[0]['score'] == 1.0
    assert [first, second] == before


def test_opaque_identity_never_proves_equality_even_for_a_reused_record():
    item = record('Exact source text.', source_id={'opaque': 'upload-a'})
    result = _fuse_contexts([item], [item], top_k=10)
    assert len(result) == 2
    assert [row['flow_origin'] for row in result] == ['python', 'rust']
    assert 'flow_origin' not in item
