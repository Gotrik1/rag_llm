"""Pure schema/immutability tests; no domain facts or model/service calls."""
from dataclasses import FrozenInstanceError, asdict
import json

import pytest

from search_focus import (
    MAX_ALTERNATIVES, MAX_PHRASE_CHARS, MAX_PHRASE_WORDS, MAX_SUBJECTS,
    SearchFocus, SearchOperation, SearchSubject, parse_search_focus,
)


def payload(**updates):
    return {'operation': 'enumerate', 'subjects': [{'text': 'режимы датчика', 'alternatives': ['типы режимов датчика']}], **updates}


def test_valid_focus_is_a_typed_immutable_value_and_json_safe():
    parsed = parse_search_focus(payload())
    assert parsed.status == 'valid' and parsed.reason is None
    assert parsed.focus == SearchFocus(SearchOperation.ENUMERATE, (SearchSubject('режимы датчика', ('типы режимов датчика',)),))
    assert isinstance(parsed.focus.subjects, tuple)
    assert isinstance(parsed.focus.subjects[0].alternatives, tuple)
    with pytest.raises(FrozenInstanceError):
        parsed.focus.operation = SearchOperation.LOOKUP
    with pytest.raises(FrozenInstanceError):
        parsed.focus.subjects[0].text = 'другой предмет'
    with pytest.raises(FrozenInstanceError):
        parsed.status = 'rejected'
    assert json.loads(json.dumps(asdict(parsed)))['focus']['operation'] == 'enumerate'


@pytest.mark.parametrize('operation', list(SearchOperation))
def test_accepts_every_documented_operation(operation):
    parsed = parse_search_focus(payload(operation=operation.value))
    assert parsed.focus.operation is operation


def test_missing_field_is_absent_but_an_explicit_null_is_rejected():
    absent = parse_search_focus(None, present=False)
    assert absent.focus is None and absent.status == 'absent' and absent.reason is None
    null = parse_search_focus(None)
    assert null.focus is None and null.status == 'rejected'


@pytest.mark.parametrize('invalid', [None, True, 4, 'enumerate', [], {},
    {'subjects': [{'text': 'датчик'}]}, {'operation': 'lookup'},
    {**payload(), 'answer': 'недопустимые данные'},
    {**payload(), 'allowed_sources': ['private.docx']},
])
def test_rejects_invalid_top_level_shape_without_interpreting_authority(invalid):
    result = parse_search_focus(invalid)
    assert result.status == 'rejected' and result.focus is None
    assert result.reason == 'invalid-shape'


@pytest.mark.parametrize('operation', [None, True, 5, {}, [], 'delete', 'ENUMERATE', ' enumerate ', ''])
def test_rejects_unknown_or_coerced_operations(operation):
    result = parse_search_focus(payload(operation=operation))
    assert result.status == 'rejected' and result.reason == 'invalid-operation'


@pytest.mark.parametrize('subjects', [None, {}, 'датчик', (), [], [None], ['датчик'], [42],
    [{'alternatives': ['датчик']}], [{'text': 'датчик', 'fact': 'ложное поле'}],
    [{'text': 'датчик', 'required': True}],
])
def test_subjects_are_a_nonempty_bounded_array_of_exact_object_shapes(subjects):
    result = parse_search_focus(payload(subjects=subjects))
    assert result.status == 'rejected' and result.focus is None


@pytest.mark.parametrize('phrase', [None, True, 5, {}, [], '', '   ', 'одна\nдве', '\nдатчик',
    'датчик\n', 'датчик\tпроверка', 'датчик\x00', 'датчик\u200b',
    'x' * (MAX_PHRASE_CHARS + 1), ' '.join(['термин'] * (MAX_PHRASE_WORDS + 1))])
def test_rejects_invalid_or_oversized_phrases_instead_of_clipping(phrase):
    result = parse_search_focus(payload(subjects=[{'text': phrase}]))
    assert result.status == 'rejected' and result.reason == 'invalid-phrase'


@pytest.mark.parametrize('alternatives', [None, True, {}, (), 'синоним', [None], [''], ['другое\nслово'],
    [f'синоним {i}' for i in range(MAX_ALTERNATIVES + 1)]])
def test_rejects_malformed_alternatives_atomically(alternatives):
    result = parse_search_focus(payload(subjects=[{'text': 'датчик', 'alternatives': alternatives}]))
    assert result.status == 'rejected' and result.focus is None


def test_omitted_alternatives_are_an_immutable_empty_tuple():
    result = parse_search_focus(payload(subjects=[{'text': 'датчик'}]))
    assert result.focus.subjects == (SearchSubject('датчик'),)


def test_bounds_accept_the_limit_and_reject_overflow_without_pruning_subjects():
    subjects = [{'text': f'предмет {i}', 'alternatives': [f'синоним {i} {j}' for j in range(MAX_ALTERNATIVES)]}
                for i in range(MAX_SUBJECTS)]
    assert parse_search_focus(payload(subjects=subjects)).status == 'valid'
    assert parse_search_focus(payload(subjects=[*subjects, {'text': 'ещё один'}])).status == 'rejected'
    assert parse_search_focus(payload(subjects=[{'text': 'x' * MAX_PHRASE_CHARS}])).status == 'valid'
    assert parse_search_focus(payload(subjects=[{'text': ' '.join(['термин'] * MAX_PHRASE_WORDS)}])).status == 'valid'


@pytest.mark.parametrize('subjects', [
    [{'text': 'датчик', 'alternatives': ['ДАТЧИК']}],
    [{'text': 'датчик', 'alternatives': ['тип режима', 'ТИП  режима']}],
    [{'text': 'тип режима'}, {'text': 'ТИП  режима'}],
])
def test_rejects_duplicate_phrases_and_subjects_without_silent_deduplication(subjects):
    result = parse_search_focus(payload(subjects=subjects))
    assert result.status == 'rejected' and result.reason in {'duplicate-phrase', 'duplicate-subject'}


def test_preserves_numbers_units_negative_qualifiers_and_unknown_identifiers_verbatim():
    phrase = 'коэффициент QX-42 при −2,5 °C без подогрева'
    result = parse_search_focus(payload(subjects=[{'text': phrase, 'alternatives': ['QX-42 10 кПа']}]))
    assert result.focus.subjects[0].text == phrase
    assert result.focus.subjects[0].alternatives == ('QX-42 10 кПа',)


def test_only_outer_spaces_are_trimmed_and_input_is_not_mutated():
    original = payload(subjects=[{'text': '  режим  датчика  ', 'alternatives': ['  тип  режима  ']}])
    result = parse_search_focus(original)
    assert result.focus.subjects[0] == SearchSubject('режим  датчика', ('тип  режима',))
    assert original['subjects'][0]['text'] == '  режим  датчика  '


def test_direct_construction_cannot_smuggle_mutable_lists_or_untyped_subjects():
    with pytest.raises(ValueError):
        SearchSubject('датчик', ['синоним'])
    with pytest.raises(ValueError):
        SearchFocus(SearchOperation.LOOKUP, [SearchSubject('датчик')])
    with pytest.raises(ValueError):
        SearchFocus(SearchOperation.LOOKUP, ('датчик',))
    with pytest.raises(ValueError):
        SearchFocus('lookup', (SearchSubject('датчик'),))
