"""Adversarial publication contracts for the actual ordinary Python writer.

Neutral native tables exercise cell binding, full calculation conditions and
source-role relationships. These are not prompt-string or helper-API tests.
"""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import rag_agent
from response_cache import ResponseCache
from table_context import serialize_table_unit


SUPPORTED = 'Результат записывают в журнал протокола.'
CITATION = ' [Источник: neutral-protocol.md — раздел «Порядок контроля»]'
ALL_POINTS = 'суммы измерений по всем контрольным точкам'
WINDOW = 'только в разрешённые интервалы протокола'
DEFINITION = ('Индекс Z равен среднему арифметическому суточных максимумов '
              + ALL_POINTS + ' ' + WINDOW + '.')
FORMULA = r'Z = \frac{1}{D}\sum_{d\in W}\max_{t\in T_d}\sum_{p\in P} x_{p,t}'


class MemoryRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value

    def scan_iter(self, *args, **kwargs):
        raise AssertionError('Fidelity must not scan a shared cache')

    def delete(self, *args, **kwargs):
        raise AssertionError('Fidelity must not clear a shared cache')


def cell(row, column, text):
    return {'row': row, 'column': column, 'span': 1, 'text': text,
            'start': 0, 'end': len(text), 'total_chars': len(text)}


def native_row(headers, values, *, identity):
    return serialize_table_unit({
        'schema': 'table-grid-v1', 'table_id': hashlib.sha256(identity.encode()).hexdigest(),
        'row': 1, 'row_kind': 'body', 'header_status': 'explicit',
        'headers': [{'row': 0, 'column': n, 'span': 1, 'text': value}
                    for n, value in enumerate(headers)],
        'cells': [cell(1, n, value) for n, value in enumerate(values)],
        'context': [], 'warnings': [],
    })


def evidence(text):
    return [{'file': 'neutral-protocol.md', 'section_path': 'Порядок контроля',
             'section_title': 'Порядок контроля', 'source_id': 'neutral-source',
             'revision_id': 'revision-A', 'content_hash': 'revision-A',
             'status': 'active', 'score': 1.0, 'text': text + '\n\n' + SUPPORTED}]


def writer(draft):
    agent = object.__new__(rag_agent.RAGAgent)
    agent._generation_state = SimpleNamespace(from_cache=False)
    agent._docstore_mtime_ns = 87654321
    agent.llm_model = 'synthetic-ordinary-fidelity-model'
    agent.llm = SimpleNamespace(complete=Mock(return_value=draft),
        metadata=SimpleNamespace(context_window=32768))
    agent.response_cache = ResponseCache(client=MemoryRedis(), ttl_seconds=120)
    agent.response_cache.set = Mock(wraps=agent.response_cache.set)
    return agent


def generate(agent, question, context, calculations=()):
    before = deepcopy(context)
    answer = agent._generate_grounded_answer(question, context, list(calculations),
        evidence_question=question, cache_enabled=True, selection_mode='coverage')
    assert isinstance(answer, str) and answer.strip(), 'Ordinary guard returns an honest useful string, not 500 or empty output'
    assert context == before, 'Publication checks cannot rewrite native evidence or caller metadata'
    assert agent.llm.complete.call_count <= 3, 'Any repair must be bounded'
    return answer


def published_and_cached(agent, answer):
    outputs = [answer]
    for call in agent.response_cache.set.call_args_list:
        value = call.args[1] if len(call.args) >= 2 else call.kwargs.get('value')
        assert isinstance(value, dict) and isinstance(value.get('answer'), str)
        outputs.append(value['answer'])
    return outputs


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Ordinary fidelity contracts must not call models, network or databases')
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE', 'off')
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


MATRICES = (
    ('Сверка', 'Режим Альфа', 'Режим Бета', 'Да (с повторной проверкой)',
     'Для режима Бета требуется повторная проверка.'),
    ('Контроль формы', 'Класс Лира', 'Класс Вега', 'Да (с дополнительной подписью)',
     'Для класса Вега требуется дополнительная подпись.'),
)


@pytest.mark.parametrize('feature,left,right,qualified,unsupported', MATRICES)
@pytest.mark.parametrize('cache_enabled', (False, True), ids=('fresh', 'cache-eligible'))
def test_neighbor_cell_parenthetical_is_not_published_or_cached_as_target_fact(
        feature, left, right, qualified, unsupported, cache_enabled):
    context = evidence(native_row(['Признак', left, right], [feature, qualified, 'Да'],
                                  identity=feature))
    agent = writer(unsupported + ' ' + SUPPORTED + CITATION)
    before = deepcopy(context)
    answer = agent._generate_grounded_answer(
        f'Какие условия указаны для {right} в строке «{feature}»?', context, [],
        cache_enabled=cache_enabled, selection_mode='coverage')
    assert isinstance(answer, str) and answer.strip()
    assert context == before
    assert agent.llm.complete.call_count <= 3
    for output in published_and_cached(agent, answer):
        assert unsupported not in output, 'A neighboring cell does not establish this positive target statement'
        assert SUPPORTED in output, 'Reject only the unsupported relationship, retain the supported source fact'


@pytest.mark.parametrize('feature,left,right,qualified,unsupported', MATRICES)
@pytest.mark.parametrize('target', ('left', 'right'))
def test_valid_target_cell_binding_and_its_literal_value_survive(
        feature, left, right, qualified, unsupported, target):
    context = evidence(native_row(['Признак', left, right], [feature, qualified, 'Да'],
                                  identity=feature))
    label, value = (left, qualified) if target == 'left' else (right, 'Да')
    correct = f'Для «{label}» в строке «{feature}» записано «{value}».'
    agent = writer(correct + ' ' + SUPPORTED + CITATION)
    answer = generate(agent, f'Что записано для {label} в строке «{feature}»?', context)
    for output in published_and_cached(agent, answer):
        assert f'«{value}»' in output, 'Do not discard a valid target-cell value or its own qualifier'
        assert label in output
        assert SUPPORTED in output


def calculation_context(kind):
    if kind == 'formula':
        text = '[MATH: ' + FORMULA + ']\n\n' + DEFINITION
    else:
        text = native_row(['Обозначение', 'Описание', 'Единица', 'Источник данных'],
            ['Z', DEFINITION, 'условная единица', 'Журнал измерений'], identity='index-Z-glossary')
    return evidence(text)


@pytest.mark.parametrize('kind', ('formula', 'glossary'))
@pytest.mark.parametrize('unsupported', (
    'Для Z максимумы выбирают по всем часам суток.',
    'Для Z используют измерения одной выбранной контрольной точки.',
), ids=('wrong-time-cohort', 'wrong-point-cohort'))
def test_calculation_condition_conjunction_survives_unsafe_formula_or_glossary_draft(kind, unsupported):
    context = calculation_context(kind)
    agent = writer(unsupported + ' ' + SUPPORTED + CITATION)
    answer = generate(agent, 'Как определяется индекс Z и по каким измерениям его рассчитывают?',
                      context, (FORMULA,) if kind == 'formula' else ())
    for output in published_and_cached(agent, answer):
        assert unsupported not in output
        assert ALL_POINTS in output, 'The aggregation over all points is part of the calculation definition'
        assert WINDOW in output, 'The allowed time cohort is a conjunctive condition, not optional background'
        assert SUPPORTED in output


@pytest.mark.parametrize('kind', ('formula', 'glossary'))
def test_full_calculation_definition_is_retained_instead_of_blanket_refusal(kind):
    context = calculation_context(kind)
    agent = writer(DEFINITION + ' ' + SUPPORTED + CITATION)
    answer = generate(agent, 'Как определяется индекс Z и по каким измерениям его рассчитывают?',
                      context, (FORMULA,) if kind == 'formula' else ())
    for output in published_and_cached(agent, answer):
        assert DEFINITION in output
        assert SUPPORTED in output


DATA_SOURCE = 'Журнал лаборатории Север'
EXPLICIT_AUTHORITY = 'Предел R утверждает комиссия приёмки.'


def authority_context(*, explicit=False):
    text = native_row(['Параметр', 'Описание', 'Единица', 'Источник данных'],
        ['R', 'Предельное значение показателя.', 'условная единица', DATA_SOURCE],
        identity='threshold-R')
    if explicit:
        text += '\n\n' + EXPLICIT_AUTHORITY
    return evidence(text)


@pytest.mark.parametrize('verb', ('утверждает', 'устанавливает'))
def test_data_source_field_does_not_establish_approval_or_setting_authority(verb):
    unsupported = f'Лаборатория Север {verb} предел R.'
    agent = writer(unsupported + f' Источник данных — «{DATA_SOURCE}». ' + SUPPORTED + CITATION)
    answer = generate(agent, 'Откуда берут предел R и кто его утверждает?', authority_context())
    for output in published_and_cached(agent, answer):
        assert unsupported not in output, 'A data-source field does not name an approving actor'
        assert DATA_SOURCE in output, 'Retain the established data-source relation'
        assert SUPPORTED in output


def test_explicit_authority_quote_is_not_rejected_with_the_source_column():
    correct = EXPLICIT_AUTHORITY + f' Источник данных — «{DATA_SOURCE}». ' + SUPPORTED + CITATION
    agent = writer(correct)
    answer = generate(agent, 'Откуда берут предел R и кто его утверждает?', authority_context(explicit=True))
    for output in published_and_cached(agent, answer):
        assert EXPLICIT_AUTHORITY in output
        assert DATA_SOURCE in output
        assert SUPPORTED in output


def test_data_source_quote_alone_is_a_supported_fact_not_a_reason_for_refusal():
    correct = f'Источник данных для R — «{DATA_SOURCE}». ' + SUPPORTED + CITATION
    agent = writer(correct)
    answer = generate(agent, 'Какой источник данных указан для R?', authority_context())
    for output in published_and_cached(agent, answer):
        assert f'«{DATA_SOURCE}»' in output
        assert SUPPORTED in output
