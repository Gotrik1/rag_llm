"""Neutral output schema must retain real H-ID input without weakening ack."""
import json

import pytest

from grounding_verifier import (
    build_grounding_judge_prompt, extract_answer_claims,
    task_publication_candidate, verify_grounding,
)
from user_task import UserTask


TASK = UserTask('Describe the control procedure.', 'Describe the control procedure.')
SOURCES = [
    {'evidence_id': 'E7', 'file': 'inspection.md', 'text': 'Inspect every hour.'},
    {'evidence_id': 'E42', 'file': 'archive.md', 'text': 'Store the records for 45 days.'},
]


def prompt_parts(answer, sources=SOURCES, **kwargs):
    prompt = build_grounding_judge_prompt(answer, sources, user_task=TASK, **kwargs)
    schema = json.JSONDecoder().raw_decode(prompt.split('JSON Schema: ', 1)[1])[0]
    payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]
    return prompt, schema['properties']['claims']['items'], payload


def test_headless_first_claim_cannot_hide_the_required_headed_output_schema():
    answer = 'Inspect every hour [E7].\n\n## Archive\n\nStore the records for 45 days [E42].'
    _, row_schema, payload = prompt_parts(answer)
    rows = payload['answer_claims']
    assert len(rows) == 2
    assert rows[0]['context_ids'] == []
    assert rows[1]['context_ids'] == ['H1']
    assert 'context_ids' in row_schema['required']
    assert row_schema['properties']['context_ids']['items']['pattern'] == r'^H[1-9]\d*$'


def test_schema_never_assigns_verdicts_or_first_passages_to_current_claims():
    answer = 'Inspect every hour [E7].\n\n## Archive\n\nStore the records for 45 days [E42].'
    prompt, row_schema, payload = prompt_parts(answer)
    assert [row['cited_evidence_ids'] for row in payload['answer_claims']] == [['E7'], ['E42']]
    assert [item['passages'][0]['passage_id'] for item in payload['evidence']] == ['E7:P1', 'E42:P1']
    assert set(row_schema['properties']['support']['enum']) == {'supported', 'unsupported', 'contradicted', 'uncertain'}
    assert set(row_schema['properties']['task_relevance']['enum']) == {'matched', 'mismatch', 'uncertain'}
    assert not any('support' in row or 'task_relevance' in row for row in payload['answer_claims'])
    assert '"support":"supported"' not in prompt
    assert '"task_relevance":"matched"' not in prompt


def test_nested_actual_context_ids_are_ordered_and_no_sample_is_duplicated():
    answer = ('Inspect every hour [E7].\n\n# Archive\n## Retention conditions\n\n'
              'Store the records for 45 days [E42].')
    _, row_schema, payload = prompt_parts(answer)
    rows = payload['answer_claims']
    assert len(rows) == 2
    assert rows[1]['context_ids'] == ['H1', 'H2']
    assert 'context_ids' in row_schema['required']
    assert len({row['claim_id'] for row in rows}) == len(rows)
    headed = '# Archive\n## Retention conditions\n\nStore the records for 45 days [E42].'
    _, _, headed_payload = prompt_parts(headed)
    headed_rows = headed_payload['answer_claims']
    assert len(headed_rows) == 1 and headed_rows[0]['context_ids'] == ['H1', 'H2']


def test_noncontiguous_heading_catalogue_does_not_invent_h1_for_the_sample():
    answer = '# Detached title\n\n---\n\n# Live title\n\nInspect every hour [E7].'
    _, row_schema, payload = prompt_parts(answer)
    rows = payload['answer_claims']
    assert [heading['context_id'] for heading in payload['heading_contexts']] == ['H2']
    assert len(rows) == 2 and rows[1]['context_ids'] == ['H2']
    assert rows[0]['context_ids'] == []
    assert 'context_ids' in row_schema['required']
    assert 'H1' not in json.dumps(row_schema)


@pytest.mark.parametrize('body', ['Uncited statement.', 'Unrecognized source [E999].'])
def test_headed_without_a_current_citation_has_no_fake_passages_or_ready_verdict(body):
    answer = 'Inspect every hour [E7].\n\n## Context\n\n' + body
    _, row_schema, payload = prompt_parts(answer)
    rows = payload['answer_claims']
    assert len(rows) == 2
    assert rows[1]['claim_id'] == 'C2'
    assert rows[1]['context_ids'] == ['H1']
    assert rows[1]['cited_evidence_ids'] == []
    assert 'support' not in rows[1]
    assert 'passage_ids' in row_schema['required']


def test_scope_schema_fields_are_required_without_preset_decisions():
    answer = 'Inspect every hour [E7].\n\n## Archive\n\nStore the records for 45 days [E42].'
    prompt, row_schema, payload = prompt_parts(answer, excluded_topics=('Unrelated topic',))
    assert len(payload['answer_claims']) == 2
    assert {'scope_disposition', 'excluded_topic_ids'} <= set(row_schema['required'])
    assert set(row_schema['properties']['scope_disposition']['enum']) == {'in_scope', 'excluded', 'mixed'}
    assert '"scope_disposition":"in_scope"' not in prompt


def test_empty_and_headless_payloads_use_no_invented_contexts():
    _, row_schema, payload = prompt_parts('')
    assert payload['answer_claims'] == []
    assert 'context_ids' in row_schema['required']
    _, _, payload = prompt_parts('Inspect every hour [E7].')
    assert len(payload['answer_claims']) == 1 and payload['answer_claims'][0]['context_ids'] == []


@pytest.mark.parametrize('reply', ['schema_echo', 'missing', 'duplicate', 'uncertain', 'missing_context'])
def test_schema_is_not_a_verdict_and_incomplete_rows_do_not_certify_headed_answer(reply):
    answer = '# Operating condition\n\nInspect every hour [E7].'
    _, row_schema, _ = prompt_parts(answer)
    valid = {'claim_id': 'C1', 'context_ids': ['H1'], 'support': 'supported',
             'passage_ids': ['E7:P1'], 'task_relevance': 'matched'}
    rows = [valid]
    if reply == 'schema_echo':
        rows = [row_schema]
    elif reply == 'missing':
        rows = []
    elif reply == 'duplicate':
        rows = [valid, dict(valid)]
    elif reply == 'uncertain':
        rows = [{**valid, 'support': 'uncertain', 'passage_ids': []}]
    else:
        rows = [{key: value for key, value in valid.items() if key != 'context_ids'}]
    report = verify_grounding(answer, SOURCES, claims=rows, user_task=TASK)
    assert not report.assessment_complete
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0


@pytest.mark.parametrize('ack', ['omitted', None, True, 'H1', [], ['H999'], ['H1', 'H1'], ['H2', 'H1']])
def test_missing_or_malformed_ack_is_still_fail_closed_after_schema_change(ack):
    answer = '# Operating condition\n\nInspect every hour [E7].'
    candidate = extract_answer_claims(answer)[0]
    row = {'claim_id': candidate['claim_id'], 'support': 'supported',
           'passage_ids': ['E7:P1'], 'task_relevance': 'matched'}
    if ack != 'omitted':
        row['context_ids'] = ack
    report = verify_grounding(answer, SOURCES, claims=[row], user_task=TASK)
    assert report.assessed_claim_count == 0 and report.assessment_complete is False
    assert report.task_relevance['claim_counts']['unassessed'] == 1
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0
