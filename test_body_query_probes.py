"""Existing bounded query probes may propose evidence, never change its scope."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from evidence_selection import SectionEvidenceIndex
from rag_agent import (RAGAgent, _body_probe_queries, _round_robin_body_probes,
                       _preserve_body_probe_metadata, _retrieval_record_identity)
from search_focus import SearchFocus, SearchOperation, SearchSubject


QUESTION = ('Сопоставь срок действия пароля оператора и срок хранения резервной копии. '
            'Укажи ограничения, порядок контроля и оформи результат таблицей.')
QUERIES = ('пароль оператора', 'резервная копия')
FACTS = ('Пароль оператора действует девяносто дней.',
         'Резервная копия хранится четырнадцать суток.')


def node(text, file='policy.md', path='', **metadata):
    return SimpleNamespace(text=text, metadata={
        'file_name': file, 'section_path': path, 'status': 'active', **metadata})


def agent_for(nodes):
    agent = object.__new__(RAGAgent)
    agent._all_nodes = nodes
    agent._refresh_docstore_if_changed = Mock()
    retriever = Mock()
    retriever.retrieve.return_value = []
    agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
    return agent, retriever


def recording_index(agent, monkeypatch):
    index = SectionEvidenceIndex(agent._all_nodes)
    agent._section_evidence_index = index
    agent._section_evidence_nodes = agent._all_nodes
    spy = Mock(wraps=index.body_seeds)
    monkeypatch.setattr(index, 'body_seeds', spy)
    return spy


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('Body-query probe tests must not use a model or network')

    monkeypatch.setattr('requests.sessions.Session.request', unexpected)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_existing_queries_reach_both_neutral_sections_in_final_packed_context(mode):
    nodes = [node(FACTS[0], path='Раздел А'), node(FACTS[1], path='Раздел Б')]
    index = SectionEvidenceIndex(nodes)
    # Neither the original compound subject nor headings can find a seed.
    assert index.body_seeds(QUESTION, mode=mode or 'legacy') == []
    agent, retriever = agent_for(nodes)

    context = agent.retrieve_context(
        QUESTION, retrieval_queries=QUERIES, selection_mode=mode,
        source_aliases={'policy.md': 'policy.md'})

    assert {item['section_path'] for item in context} == {'Раздел А', 'Раздел Б'}
    assert all(any(fact in item['text'] for item in context) for fact in FACTS)
    assert retriever.retrieve.call_count > 0


def test_probe_normalization_bounds_deduplication_and_no_mutation():
    queries = [QUESTION, '  пароль\n оператора ', 'ПАРОЛЬ ОПЕРАТОРА', '', 'x',
               None, False, 7, {}, 'z' * 501, *[f'Датчик {i}' for i in range(12)]]
    before = deepcopy(queries)
    result = _body_probe_queries(queries, (QUESTION,))
    assert result == ('пароль оператора', 'Датчик 0', 'Датчик 1', 'Датчик 2', 'Датчик 3', 'Датчик 4')
    assert queries == before
    assert _body_probe_queries(['a' * 500, 'b' * 501], ()) == ('a' * 500,)
    assert _body_probe_queries([' ' * 1000 + 'ab'], ()) == ()


@pytest.mark.parametrize('queries', [None, 'пароль оператора', {'query': 'пароль оператора'}, {'пароль оператора'}])
def test_probe_normalization_does_not_coerce_invalid_containers(queries):
    assert _body_probe_queries(queries, ()) == ()


def candidate(label):
    return {'file': 'policy.md', 'text': label, 'status': 'active'}


def candidate_key(item):
    return _retrieval_record_identity(item)


def test_round_robin_has_one_shared_budget_and_preserves_each_probe_order():
    groups = [[candidate(f'{group}:{number}') for number in range(30)] for group in range(6)]
    before = deepcopy(groups)
    result = _round_robin_body_probes(groups, 30)
    assert len(result) == 30  # Not six independent 30-candidate quotas.
    assert [item['text'] for item in result[:7]] == ['0:0', '1:0', '2:0', '3:0', '4:0', '5:0', '0:1']
    assert all(sum(item['text'].startswith(f'{group}:') for item in result) == 5 for group in range(6))
    assert groups == before
    assert result[0] is groups[0][0]
    assert _round_robin_body_probes(groups, 0) == []


def test_round_robin_skips_existing_and_shared_duplicates_before_taking_turn():
    original = candidate('original')
    shared = candidate('shared')
    groups = [[original, shared, candidate('a')], [original, shared, candidate('b')]]
    seen = {candidate_key(original)}
    assert [item['text'] for item in _round_robin_body_probes(groups, 3, seen)] == ['shared', 'b', 'a']
    assert seen == {candidate_key(original)}


def test_exact_native_metadata_survives_later_seed_without_replacing_its_priority():
    body = {**candidate(FACTS[0]), 'section_path': 'Раздел А', 'revision_id': 'r1',
            'lexical_coverage': 1.0, 'lexical_subject_keys': ['оператор', 'пароль'],
            'matched_queries': ['body-lexical'], 'score': 6.0}
    incoming = {**candidate(FACTS[0]), 'section_path': 'Раздел А', 'revision_id': 'r1',
                'matched_queries': ['explicit-source'], 'score': 3.0, 'priority': 'project'}
    before = deepcopy((body, incoming))

    result = _preserve_body_probe_metadata(body, incoming)

    assert result['lexical_coverage'] == 1.0
    assert result['lexical_subject_keys'] == body['lexical_subject_keys']
    assert result['matched_queries'] == ['explicit-source', 'body-lexical']
    assert result['score'] == 3.0 and result['priority'] == 'project'
    assert (body, incoming) == before


@pytest.mark.parametrize('field', ['file', 'text', 'section_path', 'status', 'document_id',
                                  'source_id', 'revision_id', 'version_id', 'document_version',
                                  'content_hash', 'sha256'])
def test_body_metadata_never_crosses_file_fulltext_location_status_or_revision(field):
    body = {**candidate('x' * 700 + ' original condition'), 'section_path': 'Раздел А',
            'matched_queries': ['body-coverage'], 'lexical_coverage': 1.0,
            'lexical_subject_groups': [['оператор'], ['пароль']]}
    incoming = {key: value for key, value in body.items() if not key.startswith('lexical_')}
    incoming['matched_queries'] = ['explicit-source']
    incoming[field] = ('x' * 700 + ' different condition') if field == 'text' else 'different'
    before = deepcopy((body, incoming))

    result = _preserve_body_probe_metadata(body, incoming)

    assert not any(key.startswith('lexical_') for key in result)
    assert result['matched_queries'] == ['explicit-source']
    assert (body, incoming) == before


def test_body_metadata_does_not_promote_nonbody_or_mix_later_lexical_signatures():
    body = {**candidate(FACTS[0]), 'matched_queries': ['body-coverage'],
            'lexical_coverage': 1.0, 'lexical_subject_groups': [['пароль']]}
    incoming = {**candidate(FACTS[0]), 'matched_queries': ['another-body'],
                'lexical_coverage': 0.7, 'lexical_subject_groups': [['оператор']]}
    result = _preserve_body_probe_metadata(body, incoming)
    assert result['lexical_coverage'] == 0.7
    assert result['lexical_subject_groups'] == [['оператор']]
    assert _preserve_body_probe_metadata({**body, 'matched_queries': ['arbitrary']}, incoming) == incoming


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_raw_and_canonical_are_kept_but_extra_queries_are_bounded(mode, monkeypatch):
    agent, _ = agent_for([node(FACTS[0], path='Раздел А')])
    spy = recording_index(agent, monkeypatch)
    canonical = 'Проверь срок действия пароля оператора.'
    extras = tuple(f'Датчик Альфа {number}' for number in range(20))

    agent.retrieve_context(canonical, subject_question=QUESTION, constraint_question=QUESTION,
        retrieval_queries=(QUESTION, canonical, *extras), source_aliases={'policy.md': 'policy.md'},
        selection_mode=mode, evidence_facets=['ТРЕБОВАНИЕ НЕ ПОИСКОВЫЙ ПРОБНИК'])

    calls = spy.call_args_list
    assert [call.args[0] for call in calls[:2]] == [QUESTION, canonical]
    assert [call.args[0] for call in calls[2:8]] == list(extras[:6])
    assert not any(call.args[0] in extras[6:] for call in calls)
    assert all(call.kwargs['constraint_question'] == QUESTION for call in calls)
    assert all(call.kwargs['allowed_sources'] == {'policy.md'} for call in calls)
    assert not any('ТРЕБОВАНИЕ' in call.args[0] for call in calls)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_typed_focus_never_runs_fallback_body_queries(mode, monkeypatch):
    agent, _ = agent_for([node(FACTS[0], path='Раздел А'), node(FACTS[1], path='Раздел Б')])
    spy = recording_index(agent, monkeypatch)
    focus = SearchFocus(SearchOperation.COMPARE, tuple(SearchSubject(value) for value in QUERIES))

    context = agent.retrieve_context(QUESTION, retrieval_queries=('непроверенная гипотеза',),
        source_aliases={'policy.md': 'policy.md'}, selection_mode=mode,
        search_focus=focus, constraint_question=QUESTION)

    assert all(call.args[0] == QUESTION for call in spy.call_args_list)
    assert all(call.kwargs['search_focus'] is focus for call in spy.call_args_list)
    if mode == 'coverage':
        assert all(any(fact in item['text'] for item in context) for fact in FACTS)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_extra_probes_keep_raw_explicit_acl_exclusions_and_deleted_boundaries(mode, monkeypatch):
    nodes = [node(FACTS[0], path='Раздел А'), node(FACTS[1], path='Раздел Б'),
             *(node('Служебная запись содержит прочие сведения.', path=f'Шум {i}') for i in range(30)),
             *(node(fact + ' ЗАКРЫТО.', file='private.md') for _ in range(100) for fact in FACTS),
             *(node(fact + ' ДРУГОЙ ФАЙЛ.', file='other.md') for fact in FACTS),
             *(node(fact + ' ИСКЛЮЧЕНО.', file='blocked.md') for fact in FACTS),
             node(FACTS[0] + ' УДАЛЕНО.', path='Удаленная запись', status='deleted')]
    agent, _ = agent_for(nodes)
    spy = recording_index(agent, monkeypatch)
    raw = 'По файлу policy.md ' + QUESTION
    aliases = {name: name for name in ('policy.md', 'other.md', 'blocked.md')}
    excluded = {'blocked.md'}
    inputs = deepcopy((aliases, excluded))

    context = agent.retrieve_context('По файлу other.md ' + QUESTION, subject_question=raw,
        constraint_question=raw, retrieval_queries=(*QUERIES, 'По файлу private.md пароль оператора'),
        source_aliases=aliases, excluded_sources=excluded, selection_mode=mode)

    assert context and {item['file'] for item in context} == {'policy.md'}
    assert all(any(fact in item['text'] for item in context) for fact in FACTS)
    assert all(not any(marker in item['text'] for marker in ('ЗАКРЫТО', 'ДРУГОЙ ФАЙЛ', 'ИСКЛЮЧЕНО', 'УДАЛЕНО')) for item in context)
    assert all(call.kwargs['allowed_sources'] == {'policy.md'} for call in spy.call_args_list)
    assert all(call.kwargs['excluded_sources'] == excluded for call in spy.call_args_list)
    assert all(call.kwargs['constraint_question'] == raw for call in spy.call_args_list)
    assert (aliases, excluded) == inputs


@pytest.mark.parametrize('mode', [None, 'coverage'])
@pytest.mark.parametrize('aliases,excluded', [({}, set()), ({'policy.md': 'policy.md'}, {'policy.md'})])
def test_empty_scope_and_excluded_explicit_source_cannot_be_reopened(mode, aliases, excluded):
    agent, _ = agent_for([node(FACTS[0], path='Раздел А'), node(FACTS[1], path='Раздел Б')])
    raw = 'По файлу policy.md ' + QUESTION
    assert agent.retrieve_context(raw, retrieval_queries=QUERIES, selection_mode=mode,
        source_aliases=aliases, excluded_sources=excluded, constraint_question=raw) == []


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_probe_cannot_replace_users_category_in_final_context(mode):
    nodes = [node('Для 6 ЦК ' + FACTS[0], path='Раздел А'),
             node('Для 6 ЦК ' + FACTS[1], path='Раздел Б'),
             node('Для 3 ЦК пароль оператора действует один день.', path='Раздел В'),
             *(node('Служебная запись содержит прочие сведения.', path=f'Шум {i}') for i in range(30))]
    agent, _ = agent_for(nodes)
    raw = 'Для 6 ЦК ' + QUESTION
    without_added_probes = agent.retrieve_context(raw, retrieval_queries=(),
        selection_mode=mode, source_aliases={'policy.md': 'policy.md'}, constraint_question=raw)
    assert all('Для 3 ЦК' not in item['text'] for item in without_added_probes)

    context = agent.retrieve_context(raw, retrieval_queries=(*QUERIES, 'Для 3 ЦК пароль оператора'),
        selection_mode=mode, source_aliases={'policy.md': 'policy.md'}, constraint_question=raw)

    assert context and all('Для 3 ЦК' not in item['text'] for item in context)
    assert all(any(fact in item['text'] for item in context) for fact in FACTS)


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_authoritative_category_eligibility_precedes_rarity_and_quota(mode):
    raw = 'Для 6 ЦК каков лимит отпуска сотрудника?'
    probe = 'Лимит отпуска сотрудника'
    correct = 'Для 6 ЦК лимит отпуска сотрудника составляет двадцать семь дней.'
    nodes = [node('Для 3 ЦК лимит отпуска сотрудника.', path=f'Раздел {i}') for i in range(100)]
    nodes += [node(correct, path='Раздел А')]
    nodes += [node('Служебная запись содержит прочие сведения.', path=f'Шум {i}') for i in range(1000)]
    index = SectionEvidenceIndex(nodes)

    result = index.body_seeds(probe, limit=1, mode=mode,
        allowed_sources={'policy.md'}, constraint_question=raw)

    assert [item['text'] for item in result] == [correct]
    assert index.body_seeds(probe, limit=1, mode=mode)[0]['text'] != correct


@pytest.mark.parametrize('mode', ['legacy', 'coverage'])
def test_unknown_category_scope_is_not_invented_and_constraints_are_not_probe_categories(mode):
    raw = 'Для 6 ЦК каков лимит отпуска сотрудника?'
    generic = 'Лимит отпуска сотрудника составляет двадцать семь дней.'
    index = SectionEvidenceIndex([node(generic),
        node('Для 3 ЦК лимит отпуска сотрудника составляет один день.')])
    result = index.body_seeds('Лимит отпуска сотрудника', mode=mode, constraint_question=raw)
    assert [item['text'] for item in result] == [generic]


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_current_call_only_queries_do_not_reuse_prior_plan_during_recovery(mode, monkeypatch):
    agent, _ = agent_for([node(FACTS[0], path='Раздел А'), node(FACTS[1], path='Раздел Б')])
    spy = recording_index(agent, monkeypatch)
    aliases = {'policy.md': 'policy.md'}
    assert agent.retrieve_context(QUESTION, retrieval_queries=QUERIES,
        source_aliases=aliases, selection_mode=mode, constraint_question=QUESTION)
    spy.reset_mock()

    # Corrective recovery intentionally supplies only the original USER query.
    context = agent.retrieve_context(QUESTION, retrieval_queries=(QUESTION,),
        source_aliases=aliases, selection_mode=mode, constraint_question=QUESTION)

    assert context == []
    assert all(call.args[0] == QUESTION for call in spy.call_args_list)


@pytest.mark.parametrize('mode', [None, 'coverage'])
def test_specific_aspects_survive_repeated_whole_subject_overviews_in_final_packing(mode):
    """Independent review counterexample: candidate recall is not final recall.

    Kept as a failing regression until an explicitly reviewed packing contract
    is implemented; body thresholds/focus must not be tuned around this case.
    """
    raw = 'Срок действия пароля оператора и срок хранения резервной копии.'
    overview = (raw + ' учитываются в политике; подробные значения установлены '
                'в другом разделе. Запись ')
    nodes = [node(overview + str(number) + '.', path=f'Обзор {number}') for number in range(20)]
    nodes += [node(FACTS[0], path='Порядок А'), node(FACTS[1], path='Порядок Б')]
    nodes += [node('Служебная запись содержит прочие сведения.', path=f'Шум {i}') for i in range(300)]
    agent, _ = agent_for(nodes)
    trace = {}

    context = agent.retrieve_context(raw, top_k=3, retrieval_queries=QUERIES,
        source_aliases={'policy.md': 'policy.md'}, selection_mode=mode,
        constraint_question=raw, retrieval_trace=trace)

    assert trace['visible_candidates'] == 22
    assert {'Порядок А', 'Порядок Б'} <= {item['section'] for item in trace['candidates']}
    assert all(any(fact in item['text'] for item in context) for fact in FACTS)
