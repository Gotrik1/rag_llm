"""Independent semantic-boundary contracts for soft evidence packing priority.

These are synthetic component checks, not corpus recall or answer-quality tests.
No lexical similarity result is a certificate of semantic equivalence.
"""
from copy import deepcopy
import math

import pytest

from evidence_diversity import priority_multiplier


PARAGRAPH = (
    'При аварийном режиме проверка оборудования выполняется каждые 90 минут. '
    'Оператор фиксирует результат проверки в журнале и подтверждает выполнение '
    'контрольной операции. Допускается работа оборудования мощностью 30 кВт. '
    'Без подтверждения начальника запуск не разрешается. '
    'Расчётная величина определяется выражением P = U * I.'
)


def evidence(text=PARAGRAPH, **metadata):
    return {'file': 'policy.md', 'revision_id': 'r1', 'text': text, **metadata}


def assert_unpenalized(candidate, selected):
    before = deepcopy((candidate, selected))
    trace = {}
    assert priority_multiplier(candidate, selected, trace=trace) == 1.0
    assert trace.get('semantic_equivalence') is False
    assert (candidate, selected) == before


def test_identical_same_source_revision_has_only_soft_positive_penalty():
    candidate = evidence()
    selected = [evidence()]
    before = deepcopy((candidate, selected))
    trace = {}

    multiplier = priority_multiplier(candidate, selected, trace=trace)

    assert math.isfinite(multiplier) and 0.0 < multiplier < 1.0
    assert trace.get('semantic_equivalence') is False
    assert trace.get('strongest_selected_index') == 0
    assert trace.get('compared_count') == 1
    assert (candidate, selected) == before


@pytest.mark.parametrize(('old', 'new'), [
    ('90 минут', '30 минут'),
    ('90 минут', '90.5 минут'),
    ('30 кВт', '30 МВт'),
    ('30 кВт', '30 кВт·ч'),
    ('не разрешается', 'разрешается'),
    ('Без подтверждения', 'При подтверждении'),
    ('аварийном режиме', 'штатном режиме'),
    ('P = U * I', 'p = U * I'),
    ('P = U * I', 'P = U + I'),
])
def test_operative_differences_are_not_treated_as_redundancy(old, new):
    assert old in PARAGRAPH
    assert_unpenalized(evidence(PARAGRAPH.replace(old, new)), [evidence()])


def test_same_numbers_in_different_operational_order_are_not_redundant():
    common = ('Оператор выполняет контрольную проверку оборудования, записывает '
              'результат в журнал и подтверждает выполнение процедуры. ')
    first = common + 'Порог предупреждения 30 кВт, порог отключения 90 кВт.'
    second = common + 'Порог предупреждения 90 кВт, порог отключения 30 кВт.'
    assert_unpenalized(evidence(second), [evidence(first)])


def test_negation_forms_are_not_interchangeable_stopwords():
    common = ('Оператор фиксирует результат проверки оборудования в журнале '
              'и подтверждает выполнение контрольной операции. ')
    assert_unpenalized(evidence(common + 'Проверка не требуется.'),
                       [evidence(common + 'Проверка проводится без остановки.')])


@pytest.mark.parametrize('field', ['section_title', 'section_path'])
@pytest.mark.parametrize(('first', 'second'), [
    ('При аварийном режиме', 'При штатном режиме'),
    ('Аварийный режим', 'Штатный режим'),
])
def test_operative_heading_context_is_not_lost_when_body_is_identical(field, first, second):
    body = ('Проверка оборудования выполняется каждые 90 минут. '
            'Оператор фиксирует результат в журнале и подтверждает выполнение '
            'контрольной операции. Допускается мощность 30 кВт.')
    assert_unpenalized(evidence(body, **{field: second}),
                       [evidence(body, **{field: first})])


def test_even_unnamed_numbers_are_not_silently_assumed_to_be_noise():
    assert_unpenalized(evidence(PARAGRAPH + ' Запись 1.'),
                       [evidence(PARAGRAPH + ' Запись 0.')])


@pytest.mark.parametrize('field', [
    'document_id', 'source_id', 'revision_id', 'version_id', 'document_version',
    'content_hash', 'sha256',
])
def test_changed_identity_or_revision_field_is_not_redundancy(field):
    selected = evidence(**{field: 'first'})
    candidate = evidence(**{field: 'second'})
    assert_unpenalized(candidate, [selected])


@pytest.mark.parametrize('metadata', [
    {}, {'document_id': 'doc1'}, {'source_id': 'source1'},
    {'document_id': 'doc1', 'source_id': 'source1'},
])
def test_missing_revision_proof_never_becomes_same_revision_by_assumption(metadata):
    candidate = {'file': 'policy.md', 'text': PARAGRAPH, **metadata}
    assert_unpenalized(candidate, [deepcopy(candidate)])


@pytest.mark.parametrize('other_source', [
    'other-policy.md', 'project-a/policy.md', 'project-b/policy.md',
])
def test_same_words_in_another_physical_source_are_not_redundancy(other_source):
    assert_unpenalized(evidence(file=other_source), [evidence()])


def test_same_basename_does_not_merge_different_physical_paths():
    assert_unpenalized(evidence(file='project-a/policy.md'),
                       [evidence(file='project-b/policy.md')])


@pytest.mark.parametrize('metadata', [
    {'evidence_family': {'id': 'family1', 'complete': True}},
    {'evidence_family_id': 'family1'},
    {'reference_from': [{'section_path': 'Origin'}]},
    {'reference_target': {'kind': 'section', 'locator': '2.1'}},
    {'selection_signals': {'inventory_enumeration_reserved': True}},
    {'selection_signals': {'inventory_enumeration_incomplete': True}},
    {'selection_signals': {'scoped_cohort_incomplete': True}},
    {'selection_signals': {'definition_closure_incomplete': True}},
])
def test_atomic_family_reference_and_complete_inventory_do_not_get_soft_demoted(metadata):
    assert_unpenalized(evidence(**metadata), [evidence()])


def test_empty_selection_and_request_local_cache_do_not_change_input_or_scope():
    candidate = evidence()
    before = deepcopy(candidate)
    cache = {}
    assert priority_multiplier(candidate, [], cache=cache) == 1.0
    penalized = priority_multiplier(candidate, [evidence()], cache=cache)
    assert 0.0 < penalized < 1.0
    # Prepared similarity features are not reusable authority for another source.
    assert priority_multiplier(evidence(file='other.md'), [evidence()], cache=cache) == 1.0
    assert candidate == before


def test_repeated_calls_are_deterministic_and_cannot_discard_the_candidate():
    candidate = evidence()
    selected = [evidence('Независимое описание другого процесса.'), evidence()]
    results = [priority_multiplier(candidate, selected) for _ in range(3)]
    assert results[0] == results[1] == results[2]
    assert 0.0 < results[0] < 1.0
