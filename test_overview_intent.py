"""Synthetic overview-intent regressions, not source or answer-quality gold.

Two neutral six-child families exercise the actual application selector.  The
real-corpus reproduction belongs only in the diagnostic artifact, never here.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from corrective_rag import SearchPlan, generation_prompt
from evidence_selection import SectionEvidenceIndex
from search_focus import SearchFocus, SearchOperation, SearchSubject
from test_source_semantics import _payload_parts


TOPICS = (
    ('route', 'Настройки маршрута', 'настройках маршрута', (
        'Начальная точка', 'Конечная точка', 'Остановки',
        'Время отправления', 'Резервирование', 'Согласование',
    )),
    ('inspection', 'Параметры проверки', 'параметрах проверки', (
        'Порог предупреждения', 'Порог отказа', 'Периодичность',
        'Журнал результатов', 'Назначение оператора', 'Подтверждение',
    )),
)
OPERATIONS = (None, SearchOperation.LOOKUP, SearchOperation.ENUMERATE)
SPELLINGS = ('ведется', 'ведётся')


@pytest.fixture(autouse=True)
def no_runtime_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Overview-intent tests must not use models or network')

    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


def family(topic, *, file=None, prefix='', status='active'):
    key, title, _, children = topic
    file = file or key + '.md'
    parent = prefix + title
    records = [(parent, 'Записи сохраняются по версиям и действуют с указанной даты.')]
    records.extend((parent + ' > ' + child,
                    f'Значение блока {number} указывается отдельно. '
                    'Изменение действует с даты подтверждения.')
                   for number, child in enumerate(children, 1))
    return [SimpleNamespace(
        id_=f'{file}:{number}', text=text,
        metadata={'file_name': file, 'section_path': path,
                  'section_title': path.rsplit(' > ', 1)[-1],
                  'status': status, 'revision_id': 'r1', 'source_id': file,
                  'page_number': number + 1})
            for number, (path, text) in enumerate(records)]


def focus_for(topic, operation):
    return (SearchFocus(operation, (SearchSubject(topic[1]),))
            if operation is not None else None)


def question_for(topic, spelling):
    return f'Что {spelling} в {topic[2]}?'


def select(index, question, *, focus=None, **options):
    # Genuine body probes: no fabricated lexical_coverage or retrieval rank.
    seeds = index.body_seeds(question, mode='coverage', search_focus=focus)
    before = deepcopy(seeds)
    result = index.select_v2(question, seeds, search_focus=focus,
                             top_k=options.pop('top_k', 10),
                             max_chars=options.pop('max_chars', 36000), **options)
    assert seeds == before
    return result


def child_paths(selected, parent):
    prefix = parent + ' > '
    return {item['section_path'] for item in selected
            if item['section_path'].startswith(prefix)}


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic[0])
@pytest.mark.parametrize('operation', OPERATIONS, ids=('untyped', 'lookup', 'enumerate'))
@pytest.mark.parametrize('spelling', SPELLINGS, ids=('e', 'yo'))
def test_both_spellings_preserve_all_six_native_overview_children(topic, operation, spelling):
    nodes = family(topic)
    before = deepcopy(nodes)
    index = SectionEvidenceIndex(nodes)
    question = question_for(topic, spelling)
    focus = focus_for(topic, operation)
    focus_before = deepcopy(focus)

    selected = select(index, question, focus=focus)

    expected = {node.metadata['section_path'] for node in nodes}
    assert {item['section_path'] for item in selected} == expected
    assert len(child_paths(selected, topic[1])) == 6
    assert len(selected) == 7
    assert not any(item.get('evidence_truncated') for item in selected)
    original = {node.metadata['section_path']: node for node in nodes}
    for item in selected:
        node = original[item['section_path']]
        assert item['text'] == node.text
        assert item['file'] == node.metadata['file_name']
        assert item['source_id'] == node.metadata['source_id']
        assert item['revision_id'] == node.metadata['revision_id']
        assert item['page_number'] == node.metadata['page_number']
    assert nodes == before and focus == focus_before
    assert question == question_for(topic, spelling)


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic[0])
def test_a_bare_topic_is_not_automatically_an_overview(topic):
    selected = select(SectionEvidenceIndex(family(topic)), topic[1])
    assert [item['section_path'] for item in selected] == [topic[1]]


@pytest.mark.parametrize('spelling', SPELLINGS, ids=('e', 'yo'))
@pytest.mark.parametrize('scope', ('allow-public', 'exclude-private', 'allow-none', 'deleted-private'))
def test_overview_expansion_never_bypasses_source_visibility(spelling, scope):
    topic = TOPICS[0]
    public = family(topic, file='public.md')
    private = family(topic, file='private.md',
                     status='deleted' if scope == 'deleted-private' else 'active')
    options = {
        'allow-public': {'allowed_sources': {'public.md'}},
        'exclude-private': {'excluded_sources': {'private.md'}},
        'allow-none': {'allowed_sources': set()},
        'deleted-private': {},
    }[scope]
    selected = select(SectionEvidenceIndex([*public, *private]),
                      question_for(topic, spelling),
                      focus=focus_for(topic, SearchOperation.ENUMERATE), **options)
    if scope == 'allow-none':
        assert selected == []
    else:
        assert len(selected) == 7
        assert {item['file'] for item in selected} == {'public.md'}
        assert child_paths(selected, topic[1]) == {
            node.metadata['section_path'] for node in public[1:]}


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic[0])
@pytest.mark.parametrize('spelling', SPELLINGS, ids=('e', 'yo'))
def test_explicit_topic_exclusion_is_not_restored_by_overview_expansion(topic, spelling):
    excluded_child = topic[3][2]  # A distinct term, not a shared sibling noun.
    question = question_for(topic, spelling) + '; не рассматривай ' + excluded_child
    selected = select(SectionEvidenceIndex(family(topic)), question,
                      focus=focus_for(topic, SearchOperation.ENUMERATE))
    expected = {topic[1] + ' > ' + child for child in topic[3] if child != excluded_child}
    assert child_paths(selected, topic[1]) == expected


@pytest.mark.parametrize('spelling', SPELLINGS, ids=('e', 'yo'))
def test_existing_category_guard_remains_authoritative_over_typed_subject(spelling):
    # The existing parser's actual category syntax is tested honestly; these
    # labels do not introduce any energy-domain facts into either topic.
    topic = TOPICS[0]
    allowed = family(topic, file='one.md', prefix='1 ЦК > ')
    disallowed = family(topic, file='six.md', prefix='6 ЦК > ')
    question = question_for(topic, spelling) + ' Для 1 ЦК.'
    selected = select(SectionEvidenceIndex([*allowed, *disallowed]), question,
                      focus=focus_for(topic, SearchOperation.ENUMERATE),
                      constraint_question=question)
    assert selected and {item['file'] for item in selected} == {'one.md'}
    assert child_paths(selected, '1 ЦК > ' + topic[1]) == {
        node.metadata['section_path'] for node in allowed[1:]}


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic[0])
@pytest.mark.parametrize('spelling', SPELLINGS, ids=('e', 'yo'))
def test_overview_never_exceeds_the_existing_character_budget(topic, spelling):
    nodes = family(topic)
    selected = select(SectionEvidenceIndex(nodes), question_for(topic, spelling),
                      focus=focus_for(topic, SearchOperation.ENUMERATE), max_chars=240)
    assert selected and sum(len(item['text']) for item in selected) <= 240
    assert len(child_paths(selected, topic[1])) < 6
    original = {node.metadata['section_path']: node.text for node in nodes}
    assert all(item['text'] == original[item['section_path']] for item in selected)


@pytest.mark.parametrize('limits', ({'top_k': 0}, {'max_chars': 0}))
@pytest.mark.parametrize('spelling', SPELLINGS, ids=('e', 'yo'))
def test_zero_existing_limits_still_mean_no_evidence(spelling, limits):
    topic = TOPICS[0]
    assert select(SectionEvidenceIndex(family(topic)), question_for(topic, spelling),
                  focus=focus_for(topic, SearchOperation.ENUMERATE), **limits) == []


@pytest.mark.parametrize('topic', TOPICS, ids=lambda topic: topic[0])
def test_original_yo_question_and_source_text_remain_literal_in_writer_payload(topic):
    question = question_for(topic, 'ведётся')
    nodes = family(topic)
    nodes[0].text += ' Поле «учёт» сохраняется дословно.'
    selected = select(SectionEvidenceIndex(nodes), question,
                      focus=focus_for(topic, SearchOperation.ENUMERATE))
    evidence = [dict(item, evidence_id=f'E{number}') for number, item in enumerate(selected, 1)]
    plan = SearchPlan((question,), (question,), canonical_question=question)
    before = deepcopy((plan, evidence))

    _, _, payload, _ = _payload_parts(generation_prompt(question, plan, evidence, {}))

    assert (plan, evidence) == before
    assert payload['question'] == question and 'ведётся' in payload['question']
    assert payload['evidence'] == evidence
    assert any('«учёт»' in item['text'] for item in payload['evidence'])
