"""Current-snapshot citation syntax aliases are not factual certificates."""
from copy import deepcopy
import json

import pytest

from grounding_verifier import (
    citation_ids, extract_answer_claims, task_publication_candidate, verify_grounding,
)
from user_task import UserTask


FIRST = 'Inspection context. ' + 'Native operating conditions remain visible. ' * 32
SECOND = 'Archive retention is 45 days.'
SOURCES = [
    {'evidence_id': 'E7', 'file': 'inspection.md', 'section_path': 'Procedure',
     'text': FIRST + '\n\n' + SECOND, 'revision_id': 'r1', 'status': 'active'},
    {'evidence_id': 'E42', 'file': 'archive.md', 'section_path': 'Storage',
     'text': 'Store the archive in the protected directory.', 'revision_id': 'r2', 'status': 'active'},
]
SOURCE_MAP = {row['evidence_id']: row for row in SOURCES}
TASK = UserTask('Describe retention.', 'Describe retention.')


def normalize(answer, sources=SOURCES):
    from answer_citations import normalize_answer_citations
    return normalize_answer_citations(answer, sources)


@pytest.mark.parametrize('marker', ['[E999:P1]', '[E7:P999]', '[E7:P1, E999:P1]', '[E7:P1]'])
def test_surviving_passage_markers_are_invalid_even_beside_a_valid_e_citation(marker):
    identifiers, invalid = citation_ids('[E7] ' + marker, SOURCE_MAP)
    assert identifiers == ['E7']
    assert marker in invalid


def test_multiple_current_passages_deduplicate_parent_ids_in_first_occurrence_order():
    result = normalize('Rule [E7:P1, E7:P2; E42:P1, E7].')
    assert result.answer == 'Rule [E7, E42].'
    assert result.converted_count == 3 and result.invalid_references == ()
    reversed_result = normalize('Rule [E42:P1; E7:P2; E42:P1].')
    assert reversed_result.answer == 'Rule [E42, E7].' and reversed_result.converted_count == 3


def test_case_aliases_never_change_numeric_identity_and_normalization_is_idempotent():
    before = deepcopy(SOURCES)
    first = normalize('Rule [e7:p2].')
    second = normalize(first.answer)
    assert first.answer == 'Rule [E7].' and first.converted_count == 1
    assert second.answer == first.answer and second.converted_count == 0
    assert SOURCES == before
    with pytest.raises((AttributeError, TypeError)):
        first.answer = 'Changed'


@pytest.mark.parametrize('marker', [
    '[E999:P1]', '[E7:P999]', '[E7:P0]', '[E7:P01]', '[E07:P1]',
    '[E7:P1x]', '[E7:P1.0]', '[E7:P1e2]', '[E7:P-1]', '[E7:PTrue]', '[ETrue:P1]',
    '[E7:P1, E999]', '[E7:P1, E999:P1]', '[False, E7:P1]', '[E7:P1; forged]',
    '[E70:P1]', '[E7:P10]', '[E7:P1,]', '[E7:P1 E42:P1]',
])
def test_unknown_malformed_or_mixed_groups_remain_atomic_and_explicitly_invalid(marker):
    result = normalize('Rule ' + marker + '.')
    assert result.answer == 'Rule ' + marker + '.' and result.converted_count == 0
    assert result.invalid_references == (marker,)


@pytest.mark.parametrize('literal', [
    '`[E7:P1]`', '``[E7:P1]``', '```text\n[E7:P1]\n```', '~~~\n[E7:P1]\n~~~',
    '```text\n[E7:P1]', '"[E7:P1]"', "'[E7:P1]'", '«[E7:P1]»', '“[E7:P1]”',
    '$[E7:P1]$', '$$[E7:P1]$$', r'\([E7:P1]\)', r'\[[E7:P1]\]',
    '[MATH: x_sub([E7:P1])]', '[MATH][E7:P1][/MATH]',
])
def test_quoted_literals_code_and_math_are_not_rewritten_as_service_citations(literal):
    from answer_citations import passage_citation_references
    result = normalize(literal)
    assert result.answer == literal and result.converted_count == 0 and result.invalid_references == ()
    assert passage_citation_references(literal) == ()


def test_literal_then_real_citation_keeps_only_the_real_alias_conversion():
    result = normalize('Literal `[E7:P1]`; actual rule [E42:P1].')
    assert result.answer == 'Literal `[E7:P1]`; actual rule [E42].'
    assert result.converted_count == 1


def test_math_delimiters_inside_closed_code_cannot_hide_a_later_real_citation():
    literal = '```text\n$$ [E7:P1]\n```'
    result = normalize(literal + '\n\nActual rule [E42:P1].')
    assert result.answer == literal + '\n\nActual rule [E42].'
    assert result.converted_count == 1


def test_math_delimiters_inside_a_quoted_literal_cannot_hide_a_later_real_citation():
    literal = '"literal $$ [E7:P1]"'
    result = normalize(literal + ' Actual rule [E42:P1].')
    assert result.answer == literal + ' Actual rule [E42].'
    assert result.converted_count == 1


def test_quote_closing_inside_inline_code_does_not_expose_the_rest_of_a_literal():
    literal = '«quoted `» [E999:P1]` text [E7:P1]»'
    result = normalize(literal + ' Actual [E42:P1].')
    assert result.answer == literal + ' Actual [E42].'
    assert result.invalid_references == ()


@pytest.mark.parametrize('prefix', [
    '«unfinished ' * 5000,
    ''.join('`' * length + ' plain ' for length in range(1, 400)),
], ids=['unmatched-quotes-60k', 'unmatched-backticks-82k'])
def test_long_unmatched_literal_delimiters_do_not_hide_a_later_real_alias(prefix):
    result = normalize(prefix + ' Actual [E42:P1].')
    assert result.answer == prefix + ' Actual [E42].'
    assert result.converted_count == 1 and result.invalid_references == ()


@pytest.mark.parametrize('literal', ['`[E999:P1]`', '«[E999:P1]»', '$$[E999:P1]$$'])
def test_unknown_passage_inside_a_literal_is_not_a_service_reference(literal):
    assert citation_ids(literal, SOURCE_MAP)[1] == []
    assert normalize(literal).invalid_references == ()


@pytest.mark.parametrize('literal', ['[E7:P1](https://example.invalid)', '![E7:P1](image.png)',
                                    '[E7:P1]: https://example.invalid', '[[E7:P1]]'])
def test_markdown_link_image_and_nested_labels_are_not_service_aliases(literal):
    assert normalize(literal).answer == literal
    assert normalize(literal).converted_count == 0


def test_same_filename_different_current_revisions_keep_distinct_parent_ids():
    context = [{**row, 'file': 'physical.md'} for row in SOURCES]
    result = normalize('Rules [E42:P1, E7:P1].', context)
    assert result.answer == 'Rules [E42, E7].'


def test_group_token_budget_is_atomic_and_never_silently_clips_passages():
    valid = '[' + ','.join(['E7:P1'] * 32) + ']'
    assert normalize(valid).answer == '[E7]' and normalize(valid).converted_count == 32
    invalid = '[' + ','.join(['E7:P1'] * 33) + ']'
    result = normalize(invalid)
    assert result.answer == invalid and result.invalid_references == (invalid,)


def test_legacy_e_only_groups_are_unchanged():
    result = normalize('Rule [e7;E42] and [E999].')
    assert result.answer == 'Rule [e7;E42] and [E999].'
    assert result.converted_count == 0 and result.invalid_references == ()
    assert citation_ids(result.answer, SOURCE_MAP) == (['E7', 'E42'], ['E999'])


@pytest.mark.parametrize('bad_context', [[], [SOURCES[0], SOURCES[0]], True,
    [{'evidence_id': True, 'file': 'bad.md', 'text': 'Invalid ID.'}],
    [{**SOURCES[0], 'status': 'deleted'}],
])
def test_invalid_or_unavailable_current_context_never_partially_normalizes(bad_context):
    result = normalize('Rule [E7:P1].', bad_context)
    assert result.answer == 'Rule [E7:P1].' and result.converted_count == 0
    assert result.invalid_references == ('[E7:P1]',)


def test_stale_passage_removed_from_current_snapshot_cannot_fall_back_to_its_parent():
    current = [{**SOURCES[0], 'text': SECOND, 'revision_id': 'r2'}]
    result = normalize('Rule [E7:P2].', current)
    assert result.answer == 'Rule [E7:P2].' and result.invalid_references == ('[E7:P2]',)


def test_same_live_pid_is_only_a_syntax_alias_not_a_cross_snapshot_or_truth_certificate():
    current = [{**SOURCES[0], 'text': 'Different current rule.', 'revision_id': 'r2'}]
    result = normalize('Invented content [E7:P1].', current)
    assert result.answer == 'Invented content [E7].'
    assert verify_grounding(result.answer, current).assessed_claim_count == 0


def test_initial_and_repair_drafts_are_independently_normalized_before_claim_offsets():
    for raw in (SECOND + ' [E7:P2]', 'Corrected: ' + SECOND + ' [E7:P2]'):
        result = normalize(raw)
        claim = extract_answer_claims(result.answer)[0]
        assert result.answer[claim['start']:claim['end']] == claim['text']
        report = verify_grounding(result.answer, SOURCES, claims=[{
            'claim_id': claim['claim_id'], 'support': 'supported', 'passage_ids': ['E7:P2'],
            'task_relevance': 'matched', 'context_ids': [],
        }], user_task=TASK)
        assert report.assessment_complete
        assert task_publication_candidate(result.answer, report, user_task=TASK).verified_claim_count == 1


def test_unknown_passage_cannot_hide_behind_a_valid_source_in_strict_publication():
    answer = SECOND + ' [E7] [E999:P1]'
    claim = extract_answer_claims(answer)[0]
    report = verify_grounding(answer, SOURCES, claims=[{
        'claim_id': claim['claim_id'], 'support': 'supported', 'passage_ids': ['E7:P2'],
        'task_relevance': 'matched', 'context_ids': [],
    }], user_task=TASK)
    assert any(check.kind == 'citation' and check.status == 'invalid_citation'
               and check.text == '[E999:P1]' for check in report.checks)
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0


@pytest.mark.parametrize('answer', [None, True, 7, {}, []])
def test_non_string_answers_are_not_coerced_into_citations(answer):
    with pytest.raises(TypeError):
        normalize(answer)
