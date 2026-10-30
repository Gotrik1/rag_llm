"""Independent, network-free source-reader-to-packing trust-boundary checks.

Synthetic model stubs test transport and provenance, not model judgment quality.
"""
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest

from corrective_rag import run_corrective
from evidence_selection import SectionEvidenceIndex
from search_focus import SearchFocus, SearchOperation, SearchSubject
from source_reader import read_sources, selection_preferences, validated_writer_payload
from user_task import UserTask


QUESTION = 'Каковы порядок осмотра датчика и порядок хранения записи?'
FACTS = ('Осмотр датчика выполняют каждые 12 часов, только после остановки.',
         'Хранение записи продолжается 45 суток; без допуска удаление запрещено.')
PLAN = {'canonical_question': QUESTION, 'requirements': ['Порядок контроля'],
        'queries': [QUESTION], 'exclusions': []}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Prepack contract tests must not use the network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def evidence(text=FACTS[0], identifier='E31', path='Осмотр', **metadata):
    return {'file': 'control.md', 'text': text, 'evidence_id': identifier,
            'section_path': path, 'status': 'active', 'revision_id': 'r1', **metadata}


def reader_data(prompt):
    return json.loads(prompt.split('ДАННЫЕ ЧТЕНИЯ:\n', 1)[1])


def select_ids(data, *, kind='rule', evidence_ids=None):
    rows = [row for row in data['evidence']
            if evidence_ids is None or row['evidence_id'] in evidence_ids]
    cards = [{'id': f'C{number}', 'kind': kind,
              'passage_ids': [passage['passage_id'] for passage in row['passages']],
              'related_card_ids': [], 'requirement_ids': ['R1']}
             for number, row in enumerate(rows, 1)]
    return {'snapshot_id': data['snapshot_id'], 'cards': cards,
            'unresolved_requirement_ids': []}


class Reader:
    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def __call__(self, prompt, purpose):
        self.calls.append((purpose, prompt))
        assert purpose == 'source-reading'
        data = reader_data(prompt)
        result = select_ids(data)
        if self.transform is not None:
            result = self.transform(result)
        return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)


def reading_fixture(values=None):
    values = values if values is not None else [evidence(), evidence(FACTS[1], 'E32', 'Хранение')]
    task = UserTask(QUESTION, QUESTION)
    model = Reader()
    reading = read_sources(QUESTION, PLAN, values, model, user_task=task)
    assert reading.assessed
    return values, task, model, reading


def test_preferences_are_exact_source_quotes_not_free_claims_or_source_mutations():
    values, task, model, reading = reading_fixture()
    original = deepcopy((values, reading.to_dict()))

    hints = selection_preferences(reading, QUESTION, PLAN, values, user_task=task)

    assert isinstance(hints, tuple) and len(hints) == 2
    assert all(set(hint) == {'evidence_id', 'quote', 'source'} for hint in hints)
    assert [hint['quote'] for hint in hints] == list(FACTS)
    assert [hint['evidence_id'] for hint in hints] == ['E31', 'E32']
    assert all(hint['source']['file'] == 'control.md' for hint in hints)
    assert all(hint['source']['revision_id'] == 'r1' for hint in hints)
    assert len(model.calls) == 1  # Pure extraction makes no extra model call.
    hints[0]['source']['file'] = 'changed.md'
    assert (values, reading.to_dict()) == original


@pytest.mark.parametrize('field', ['quote', 'evidence_id', 'file', 'section_path',
                                  'revision_id', 'version_id', 'document_version',
                                  'content_hash', 'sha256', 'document_id', 'source_id'])
def test_tampered_reading_or_provenance_does_not_supply_hints(field):
    values, task, _, reading = reading_fixture()
    data = reading.to_dict()
    passage = data['quoted_passages']['E31:P1']
    if field in {'quote', 'evidence_id'}:
        passage[field] = 'changed'
    else:
        passage['source'][field] = 'changed'
    assert selection_preferences(data, QUESTION, PLAN, values, user_task=task) == ()


@pytest.mark.parametrize('change', ['body', 'revision', 'source', 'location', 'question',
                                  'plan', 'resolved_context', 'limits'])
def test_preferences_cannot_rebind_a_stale_reading_to_current_call(change):
    values, task, _, reading = reading_fixture()
    current, question, plan, limits = deepcopy(values), QUESTION, deepcopy(PLAN), None
    if change == 'body':
        current[0]['text'] = FACTS[0].replace('после остановки', 'во время работы')
    elif change == 'revision':
        current[0]['revision_id'] = 'r2'
    elif change == 'source':
        current[0]['file'] = 'other.md'
    elif change == 'location':
        current[0]['section_path'] = 'Другой раздел'
    elif change == 'question':
        question += ' Дополнительное условие.'
    elif change == 'plan':
        plan['requirements'] = ['Иное требование']
    elif change == 'resolved_context':
        task = UserTask(QUESTION, 'Другое содержание текущего диалога')
    elif change == 'limits':
        limits = {'inventory_enumeration_incomplete': True}
    assert selection_preferences(reading, question, plan, current, limits, user_task=task) == ()


def test_uncertainty_only_is_not_promoted_to_positive_source_preference():
    values = [evidence()]
    task = UserTask(QUESTION, QUESTION)
    model = Reader(lambda response: {**response, 'cards': [
        {**card, 'kind': 'uncertainty'} for card in response['cards']]})
    reading = read_sources(QUESTION, PLAN, values, model, user_task=task)
    assert reading.assessed
    assert selection_preferences(reading, QUESTION, PLAN, values, user_task=task) == ()


def test_large_valid_selection_keeps_exact_quotes_beyond_compact_writer_budget():
    full = ('Полное условие осмотра датчика сохраняется без сокращения. ' * 190)
    values, task, model, reading = reading_fixture([evidence(full)])
    assert validated_writer_payload(reading, QUESTION, PLAN, values,
                                    max_chars=8192, user_task=task) is None
    hints = selection_preferences(reading, QUESTION, PLAN, values, user_task=task)
    assert hints and ''.join(hint['quote'] for hint in hints) == full
    assert len(model.calls) == 1


def packed_fixture():
    overview = ('Осмотр датчика и хранение записи регулируются общим порядком. '
                'Подробные интервалы и условия приведены в отдельном разделе. ')
    pool = [evidence(overview + chr(ord('А') + number) + '.', f'E{number + 1}',
                     f'Обзор {chr(ord("А") + number)}') for number in range(12)]
    pool += [evidence(), evidence(FACTS[1], 'E32', 'Хранение')]
    nodes = [SimpleNamespace(text=row['text'], metadata={key: value for key, value in row.items()
             if key not in {'text', 'evidence_id'}} | {'file_name': row['file']}) for row in pool]
    index = SectionEvidenceIndex(nodes)
    focus = SearchFocus(SearchOperation.COMPARE,
                        (SearchSubject('осмотр датчика'), SearchSubject('хранение записи')))
    return index, pool, focus


def hint(row):
    source_fields = ('file', 'section_path', 'document_id', 'source_id', 'revision_id',
                     'version_id', 'document_version', 'content_hash', 'sha256')
    return {'evidence_id': row['evidence_id'], 'quote': row['text'],
            'source': {key: deepcopy(row[key]) for key in source_fields if key in row}}


def pack(index, pool, focus, hints=()):
    return index.select_v2(QUESTION, pool, top_k=2, max_chars=2500,
        allowed_sources={'control.md'}, search_focus=focus,
        constraint_question=QUESTION, preferred_passages=hints)


def packed_payload(rows):
    return [(row['file'], row.get('section_path'), row['text']) for row in rows]


def test_valid_preferences_deliver_both_native_facts_and_their_conditions_to_packing():
    index, pool, focus = packed_fixture()
    before = deepcopy(pool)
    hints = tuple(hint(row) for row in pool[-2:])
    result = pack(index, pool, focus, hints)
    assert all(any(fact in row['text'] for row in result) for fact in FACTS)
    assert all(row['text'] in {value['text'] for value in pool} for row in result)
    assert all(row['file'] == 'control.md' for row in result)
    assert pool == before


@pytest.mark.parametrize('change', ['no_id', 'wrong_id', 'source', 'location', 'revision',
                                  'version', 'content_hash', 'quote', 'conditions'])
def test_invalid_preferred_passage_cannot_change_native_packing(change):
    index, pool, focus = packed_fixture()
    bad = hint(pool[-1])
    if change == 'no_id':
        bad.pop('evidence_id')
    elif change == 'wrong_id':
        bad['evidence_id'] = 'E999'
    elif change == 'source':
        bad['source']['file'] = 'unrelated.md'
    elif change == 'location':
        bad['source']['section_path'] = 'Иное расположение'
    elif change == 'revision':
        bad['source']['revision_id'] = 'r2'
    elif change == 'version':
        bad['source']['version_id'] = 'v2'
    elif change == 'content_hash':
        bad['source']['content_hash'] = 'different'
    elif change == 'quote':
        bad['quote'] += ' Модель придумала дополнительное правило.'
    elif change == 'conditions':
        bad['quote'] = bad['quote'].replace('без допуска удаление запрещено', 'удаление разрешено')
    assert packed_payload(pack(index, pool, focus, (bad,))) == packed_payload(pack(index, pool, focus))


@pytest.mark.parametrize('boundary', ['not_allowed', 'excluded', 'deleted'])
def test_a_native_valid_hint_never_overrides_visibility_or_deleted_boundaries(boundary):
    index, pool, focus = packed_fixture()
    hidden = evidence('Осмотр датчика разрешается каждые 789 часов.', 'E99', 'Скрытый порядок',
                      file='hidden.md', status='deleted' if boundary == 'deleted' else 'active')
    pool.append(hidden)
    nodes = [SimpleNamespace(text=row['text'], metadata={key: value for key, value in row.items()
             if key not in {'text', 'evidence_id'}} | {'file_name': row['file']}) for row in pool]
    index = SectionEvidenceIndex(nodes)
    allowed = {'control.md'} if boundary == 'not_allowed' else {'control.md', 'hidden.md'}
    excluded = {'hidden.md'} if boundary == 'excluded' else set()
    result = index.select_v2(QUESTION, pool, top_k=2, max_chars=2500,
        allowed_sources=allowed, excluded_sources=excluded, search_focus=focus,
        constraint_question=QUESTION, preferred_passages=(hint(hidden),))
    assert all(row['file'] == 'control.md' and row.get('status') != 'deleted' for row in result)
    assert all('789' not in row['text'] for row in result)


def test_preference_cannot_displace_an_independent_still_pending_typed_subject():
    _, _, focus = packed_fixture()
    pool = [evidence(FACTS[0] + f' Порядок {number}.', f'E{number + 1}', f'Осмотр {number}')
            for number in range(6)]
    pool.append(evidence(FACTS[1], 'E32', 'Хранение'))
    nodes = [SimpleNamespace(text=row['text'], metadata={key: value for key, value in row.items()
             if key not in {'text', 'evidence_id'}} | {'file_name': row['file']}) for row in pool]
    result = pack(SectionEvidenceIndex(nodes), pool, focus,
                  tuple(hint(row) for row in pool[:-1]))
    assert any(FACTS[0] in row['text'] for row in result)
    assert any(FACTS[1] in row['text'] for row in result)


def prompt_data(prompt, prefix):
    start = prompt.index(prefix)
    return json.JSONDecoder().raw_decode(prompt[start:])[0]


class Pipeline:
    """Explicit positive fake verdicts test transport, never semantic accuracy."""
    def __init__(self, batches=None, *, invalid_reading=False, first_coverage='complete'):
        self.batches = batches or [[evidence()]]
        self.invalid_reading = invalid_reading
        self.first_coverage = first_coverage
        self.calls = []
        self.retrieval_calls = []
        self.select_calls = []
        self.reader_inputs = []
        self.writer_inputs = []
        self.coverage_count = 0

    def retrieve(self, question, queries, requirements, trace, *, search_focus, constraint_question):
        number = min(len(self.retrieval_calls), len(self.batches) - 1)
        self.retrieval_calls.append((question, list(queries), constraint_question))
        return deepcopy(self.batches[number])

    def select(self, question, pool, requirements, **kwargs):
        self.select_calls.append((deepcopy(pool), deepcopy(kwargs)))
        return pool[-1:]

    def complete(self, prompt, purpose):
        self.calls.append((purpose, prompt))
        if purpose == 'planning':
            return json.dumps(PLAN, ensure_ascii=False)
        if purpose == 'source-reading':
            data = reader_data(prompt)
            self.reader_inputs.append(data)
            if self.invalid_reading:
                return '{malformed'
            return json.dumps(select_ids(data), ensure_ascii=False)
        if purpose == 'answer':
            data = prompt_data(prompt, '{"question":')
            self.writer_inputs.append(data)
            first = data['evidence'][0]
            return first['text'] + ' [' + first['evidence_id'] + ']'
        assert purpose == 'evidence-review'
        if 'Проверь доказательность ответа' in prompt:
            data = prompt_data(prompt, '{"question":')
            rows = []
            for claim in data['answer_claims']:
                passages = [passage['passage_id'] for row in data['evidence']
                            if row['evidence_id'] in claim['cited_evidence_ids']
                            for passage in row['passages']]
                rows.append({'claim_id': claim['claim_id'], 'support': 'supported',
                             'passage_ids': passages, 'task_relevance': 'matched'})
            return json.dumps({'claims': rows})
        data = prompt_data(prompt, '{"requirements":')
        first_pid = next(p['passage_id'] for row in data['evidence'] for p in row['passages'])
        initial = self.coverage_count == 0
        self.coverage_count += 1
        covered = not (initial and self.first_coverage == 'missing')
        alignment = 'mismatch' if initial and self.first_coverage == 'mismatch' else 'matched'
        return json.dumps({
            'coverage': [{'requirement_id': 'R1', 'passage_ids': [first_pid],
                          'applicable': True}] if covered else [],
            'task_alignment': {'status': alignment, 'passage_ids': [first_pid]},
            'missing_queries': ['Уточнение порядка контроля'] if not covered else [],
            'conflicts': [],
        })

    def run(self, *, selector=True, **options):
        return run_corrective(QUESTION, QUESTION, retrieve=self.retrieve, complete=self.complete,
            select=self.select if selector else None, max_additional_rounds=1, **options)


def stages(model):
    return [purpose for purpose, _ in model.calls]


def test_prepack_moves_one_reader_call_before_selection_and_never_reuses_pool_writer_guide():
    model = Pipeline([[evidence(), evidence(FACTS[1], 'E32', 'Хранение')]])
    result = model.run(prepack_source_reading=True, read_before_answer=True)
    assert stages(model) == ['planning', 'source-reading', 'evidence-review', 'answer', 'evidence-review']
    assert len(model.select_calls) == 1
    pool, kwargs = model.select_calls[0]
    hints = kwargs['preferred_passages']
    assert {hint['evidence_id'] for hint in hints} == {row['evidence_id'] for row in pool}
    assert {hint['quote'] for hint in hints} == set(FACTS)
    assert len(model.reader_inputs[0]['evidence']) == 2
    assert len(model.writer_inputs[0]['evidence']) == 1
    assert model.writer_inputs[0]['evidence'] == result.context
    assert 'source_reading' not in model.writer_inputs[0]
    assert result.answer == FACTS[1] + ' [' + result.context[0]['evidence_id'] + ']'


def test_explicitly_disabled_prepack_is_identical_to_legacy_stage_transport():
    control, explicit = Pipeline(), Pipeline()
    expected = control.run(read_before_answer=True)
    actual = explicit.run(read_before_answer=True, prepack_source_reading=False)
    assert explicit.calls == control.calls
    assert explicit.select_calls == control.select_calls
    assert all('preferred_passages' not in kwargs for _, kwargs in explicit.select_calls)
    assert actual.context == expected.context and actual.answer == expected.answer


def test_invalid_prepack_reading_falls_back_without_second_reading_or_extra_keyword():
    model = Pipeline(invalid_reading=True)
    result = model.run(prepack_source_reading=True, read_before_answer=True)
    assert stages(model).count('source-reading') == 1
    assert 'preferred_passages' not in model.select_calls[0][1]
    assert 'source_reading' not in model.writer_inputs[0]
    assert result.context and FACTS[0] in result.answer


def test_prepack_without_selector_has_no_reader_call_and_no_stale_writer_guide():
    model = Pipeline()
    result = model.run(selector=False, prepack_source_reading=True, read_before_answer=True)
    assert 'source-reading' not in stages(model)
    assert 'source_reading' not in model.writer_inputs[0]
    assert result.context


@pytest.mark.parametrize('changed_pool', [False, True])
def test_task_recovery_revokes_first_pool_hints_even_when_reading_could_rebind(changed_pool):
    second = evidence(FACTS[0].replace('12 часов', '18 часов'), 'E91')
    model = Pipeline([[evidence()], [second] if changed_pool else [evidence()]],
                     first_coverage='mismatch')
    model.run(prepack_source_reading=True, read_before_answer=True)
    assert len(model.select_calls) == 2
    assert model.select_calls[0][1]['preferred_passages']
    assert 'preferred_passages' not in model.select_calls[1][1]
    assert stages(model).count('source-reading') == 1
    assert all('source_reading' not in row for row in model.writer_inputs)


def test_unchanged_snapshot_can_reuse_validated_hints_without_new_reader_call():
    model = Pipeline(first_coverage='missing')
    model.run(prepack_source_reading=True, read_before_answer=True)
    assert len(model.select_calls) == 2
    assert model.select_calls[0][1]['preferred_passages']
    assert model.select_calls[1][1]['preferred_passages'] == model.select_calls[0][1]['preferred_passages']
    assert stages(model).count('source-reading') == 1


def test_changed_snapshot_revokes_hints_without_task_recovery_or_another_reader_call():
    second = evidence(FACTS[0].replace('12 часов', '18 часов'), 'E91')
    model = Pipeline([[evidence()], [second]], first_coverage='missing')
    result = model.run(prepack_source_reading=True, read_before_answer=True)
    assert len(model.select_calls) == 2
    assert model.select_calls[0][1]['preferred_passages']
    assert 'preferred_passages' not in model.select_calls[1][1]
    assert all(not row['task_recovery'] for row in result.trace['rounds'])
    assert stages(model).count('source-reading') == 1


def test_reading_is_current_request_local_when_same_fixture_handles_a_new_question_run():
    model = Pipeline()
    model.run(prepack_source_reading=True, read_before_answer=True)
    model.batches = [[evidence(FACTS[0].replace('12 часов', '24 часа'))]]
    model.coverage_count = 0
    model.run(prepack_source_reading=True, read_before_answer=True)
    assert stages(model).count('source-reading') == 2
    assert model.reader_inputs[0]['snapshot_id'] != model.reader_inputs[1]['snapshot_id']
    second_hints = model.select_calls[-1][1]['preferred_passages']
    assert any('24 часа' in hint['quote'] for hint in second_hints)
    assert all(hint['quote'] != FACTS[0] for hint in second_hints)
