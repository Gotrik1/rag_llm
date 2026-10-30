"""Short model references must render to exact, source-bound user locators."""
from copy import deepcopy
from unittest.mock import Mock

import pytest
import ordinary_answer_guard as guard
import rag_agent
from citation_scopes import compile_publication
from test_ordinary_formula_fidelity import make_agent


FACT = 'The selected mode uses the measured quantity from the previous period.'
PATH = 'General procedure > Configuration > Mode with a long parent path'
REFERENCE = 'Guide.docx — раздел «' + PATH + '»'
LEGACY_INSTRUCTION = ('После каждого самостоятельного факта или группы пунктов ставь ровно одну цитату '
    'из соответствующего блока контекста в формате [Источник: имя файла — точный раздел или пункт]. '
    'Не заменяй точный раздел названием документа и не повторяй одну и ту же цитату подряд.')


def context():
    return [{'file': 'native-guide.docx', 'document_title': 'Guide.docx', 'score': 1.0,
        'section_path': PATH, 'text': FACT, 'revision_id': 'version-one'}]


def packet():
    return guard.build_source_packet(context(), lambda _: REFERENCE)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', Mock(side_effect=AssertionError('No network')))
    monkeypatch.setattr(rag_agent, 'generate_extractive_value_answer', lambda *a, **kw: '')
    monkeypatch.setattr(rag_agent, 'generate_image_transcription_answer', lambda *a, **kw: '')


def test_coverage_writer_asks_for_source_ids_without_conflicting_human_locator_instruction():
    agent = make_agent(FACT + ' [E1]')
    agent._generate_grounded_answer('Explain the selected mode.', context(), [], selection_mode='coverage')
    prompt = agent.llm.complete.call_args_list[0].args[0]
    assert LEGACY_INSTRUCTION not in prompt
    assert 'Проставляй маркеры с фактическими E-ID' in prompt
    assert 'не выбирает подтверждающий источник за тебя' in prompt
    assert 'После фактического утверждения указывай краткую текстовую ссылку вида' not in prompt
    assert 'Сохраняй маркеры вида [С_sub(...)]' not in prompt
    assert 'используй только блок "РАЗРЕШЕННЫЕ РАСЧЕТНЫЕ ПРАВИЛА"' not in prompt


def test_explicit_user_system_prompt_is_preserved_as_supplied():
    agent = make_agent(FACT + ' [E1]')
    custom='Custom project policy, including literal examples and required units.'
    agent._generate_grounded_answer('Explain the selected mode.', context(), [],
        system_prompt=custom, selection_mode='coverage')
    assert agent.llm.complete.call_args_list[0].args[0].startswith(custom+'\n\n')


def test_legacy_writer_retains_its_existing_instruction():
    agent = make_agent(FACT)
    agent._generate_grounded_answer('Explain the selected mode.', context(), [], selection_mode='legacy')
    prompt = agent.llm.complete.call_args_list[0].args[0]
    assert LEGACY_INSTRUCTION in prompt
    assert 'Проставляй маркеры с фактическими E-ID' not in prompt


def test_initial_and_repair_contracts_offer_only_actual_packet_source_ids():
    original = deepcopy(context()); native = packet()
    initial = guard.writer_contract(native)
    repaired = guard.repair_prompt('Explain mode.', native, FACT, guard.GuardResult(FACT, False, ('invalid_citation',)))
    for instruction in (initial, repaired):
        assert 'Проставляй маркеры с фактическими E-ID' in instruction
        assert 'Допустимые маркеры: [E1].' in instruction
        assert '[E2]' not in instruction
    assert context() == original


def test_empty_packet_never_invents_a_source_id_example():
    native = guard.build_source_packet([], lambda _: REFERENCE)
    instruction = guard.writer_contract(native)
    assert 'Доказательства не переданы' in instruction
    assert '[E1]' not in instruction


def test_source_id_preserves_exact_parent_locator_and_existing_unknown_leaf_still_fails():
    native = packet(); before = native.source_json
    rendered = guard.render_formula_ids(FACT + ' [E1]', native)
    assert rendered == FACT + ' [Источник: ' + REFERENCE + ']'
    projection = compile_publication(rendered, native.sources)
    assert projection.unresolved_reference_count == 0
    assert projection.bindings and all(ids == ['E1'] for ids in projection.bindings.values())
    shortened = FACT + ' [Источник: Guide.docx — раздел «Mode with a long parent path»]'
    assert compile_publication(shortened, native.sources).unresolved_reference_count == 1
    assert native.source_json == before


def test_source_ids_do_not_repair_unknown_group_or_transform_quoted_examples():
    native = packet()
    answer = FACT + ' [E1, E99]\n\n```text\nLiteral example [E1].\n```'
    assert guard.render_formula_ids(answer, native) == answer
    assert not guard.assess_answer(FACT + ' [E99]', native, 'Explain mode.').accepted
