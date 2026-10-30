"""A topic heading is not permission to erase a validated full-subject body hit.

Synthetic native records exercise the current application selector; no private
corpus, model, network, database or embedding calls are needed.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex, new_query_rank_context, term_keys
from test_candidate_pool_boundary import agent_for


@pytest.fixture(autouse=True)
def no_runtime_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Direct-body selector tests must not use a model or network')
    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


def node(file, path, text, **metadata):
    return SimpleNamespace(text=text, metadata={
        'file_name': file, 'section_path': path, 'section_title': path.rsplit(' > ', 1)[-1],
        'status': 'active', **metadata})


TOPICS = [
    ('планового аванса', 'Плановый аванс равен произведению объема и договорной цены. Оплата вносится до начала месяца.'),
    ('плановой доставки', 'Сумма плановой доставки равна произведению расстояния и ставки. Оплата вносится до начала месяца.'),
]


def packet(subject=TOPICS[0][0], text=TOPICS[0][1], *, with_root=True):
    question = 'Как рассчитывается стоимость ' + subject + '?'
    primary = node('primary.md', 'Основное правило', text, revision_id='r1')
    conditional = node('conditional.md', 'Стоимость ' + subject + ' при временном графике',
                       'При временном графике стороны согласуют даты отдельно.')
    index = SectionEvidenceIndex([primary, conditional] if with_root else [primary])
    seeds = index.body_seeds(question)
    assert [item['file'] for item in seeds] == ['primary.md']
    assert seeds[0]['lexical_coverage'] == 1.0 and seeds[0]['matched_queries'] == ['body-lexical']
    assert not term_keys(question).issubset(term_keys(primary.text + ' ' + primary.metadata['section_path']))
    return question, index, primary, conditional, seeds


@pytest.mark.parametrize('subject,text', TOPICS)
def test_governing_heading_cannot_erase_actual_full_subject_body_seed(subject, text):
    question, index, primary, conditional, seeds = packet(subject, text)
    before = deepcopy(seeds)
    selected = index.select(question, seeds, top_k=10, max_chars=36000)
    main = [item for item in selected if item['file'] == 'primary.md']
    assert len(main) == 1
    assert main[0]['text'] == primary.text and main[0]['section_path'] == primary.metadata['section_path']
    assert main[0]['revision_id'] == 'r1'
    assert selected[0]['file'] == conditional.metadata['file_name']  # Existing root priority is unchanged.
    assert seeds == before


@pytest.mark.parametrize('subject,text', TOPICS)
def test_existing_no_heading_direct_body_behavior_is_preserved(subject, text):
    question, index, primary, _, seeds = packet(subject, text, with_root=False)
    selected = index.select(question, seeds)
    assert len(selected) == 1 and selected[0]['text'] == primary.text


def test_repeated_exact_direct_seed_is_retained_only_once():
    question, index, _, _, seeds = packet()
    selected = index.select(question, [*seeds, deepcopy(seeds[0])])
    assert [item['file'] for item in selected].count('primary.md') == 1
    assert len(selected) == 2


@pytest.mark.parametrize('options', [
    {'allowed_sources': {'conditional.md'}},
    {'excluded_sources': {'primary.md'}},
    {'allowed_sources': set()},
])
def test_direct_seed_never_bypasses_source_acl_or_exclusions(options):
    question, index, _, _, seeds = packet()
    selected = index.select(question, seeds, **options)
    assert all(item['file'] != 'primary.md' for item in selected)
    if options.get('allowed_sources') == set():
        assert selected == []


def test_deleted_direct_seed_is_not_restored_by_a_heading():
    question, index, _, _, seeds = packet()
    seeds[0]['status'] = 'deleted'
    assert all(item['file'] != 'primary.md' for item in index.select(question, seeds))


def test_explicit_document_kind_still_excludes_a_full_subject_manual_seed():
    question = 'Как рассчитывается стоимость планового аванса по постановлению?'
    primary = node('manual.md', 'Основное правило', TOPICS[0][1])
    governing = node('Постановление.md', 'Стоимость планового аванса', 'Применяется установленный порядок.')
    index = SectionEvidenceIndex([primary, governing])
    seeds = index.body_seeds(question)
    assert any(item['file'] == 'manual.md' and item['lexical_coverage'] == 1.0 for item in seeds)
    selected = index.select(question, seeds)
    assert selected and {item['file'] for item in selected} == {'Постановление.md'}


def test_explicit_document_request_keeps_the_existing_application_scope():
    question, _, primary, conditional, _ = packet()
    agent = agent_for(nodes=[primary, conditional], hits=[primary, conditional])
    selected = agent.retrieve_context('Из conditional.md: ' + question,
        retrieval_queries=(question,), source_aliases={'primary.md': 'Primary', 'conditional.md': 'Conditional'})
    assert selected and {item['file'] for item in selected} == {'conditional.md'}


def test_full_body_match_cannot_bypass_explicit_category_applicability():
    question = 'Как рассчитывается стоимость планового аванса по 1 ЦК?'
    wrong_category = node('primary.md', '6 ЦК > Основное правило',
        '6 ЦК. Плановый аванс равен произведению объема и цены. Отличие от 1 ЦК указано отдельно.')
    governing = node('conditional.md', 'Стоимость планового аванса по 1 ЦК', 'Для первого варианта действует порядок.')
    index = SectionEvidenceIndex([wrong_category, governing])
    seeds = index.body_seeds(question)  # Deliberately unscoped earlier seed; final guard must still hold.
    assert any(item['file'] == 'primary.md' and item['lexical_coverage'] == 1.0 for item in seeds)
    selected = index.select(question, seeds, constraint_question=question)
    assert selected and all(item['file'] != 'primary.md' for item in selected)


def test_marginal_query_coverage_is_not_a_full_user_subject_body_match():
    question = 'Как рассчитывается стоимость планового аванса?'
    weak = node('weak.md', 'Отпуск энергии', 'Порядок отпуска электроэнергии утвержден отдельным регламентом.')
    root = node('conditional.md', 'Стоимость планового аванса при временном графике',
                'При временном графике стороны согласуют даты отдельно.')
    index = SectionEvidenceIndex([weak, root])
    seed = index.body_seeds('Порядок отпуска электроэнергии')[0]
    context = new_query_rank_context(question)
    context.record(question, [seed])
    context.record('Порядок выплаты планового аванса', [{'file': 'conditional.md', 'text': root.text, **root.metadata}])
    assert seed['lexical_coverage'] == 1.0
    assert context.allocator().active and context.allocator().gain(context.origins(seed)) > 0
    assert 'аванс' not in term_keys(seed['text']) and 'планов' not in term_keys(seed['text'])
    selected = index.select(question, [seed], constraint_question=question, query_rank_context=context)
    assert selected and all(item['file'] != 'weak.md' for item in selected)


@pytest.mark.parametrize('change', ['partial-subject', 'weak-coverage'])
def test_partial_or_weak_seed_does_not_bypass_a_topic_heading(change):
    question, index, _, _, seeds = packet()
    if change == 'partial-subject':
        seeds[0]['text'] = 'Аванс указан в календаре.'
    else:
        seeds[0]['lexical_coverage'] = 0.75
    assert all(item['file'] != 'primary.md' for item in index.select(question, seeds))


def test_single_subject_does_not_gain_the_new_root_bypass():
    _, index, _, _, seeds = packet()
    selected = index.select('Какова стоимость аванса?', seeds)
    assert all(item['file'] != 'primary.md' for item in selected)


def test_existing_top_k_and_root_order_are_not_increased_by_direct_body_retention():
    question, index, _, _, seeds = packet()
    selected = index.select(question, seeds, top_k=1)
    assert len(selected) == 1 and selected[0]['file'] == 'conditional.md'


def test_existing_budget_truncates_only_on_whole_paragraph_boundaries():
    paragraph = TOPICS[0][1]
    text = paragraph + '\n\n' + '$$P = Q \\times C$$ ' * 80
    question, index, _, _, seeds = packet(text=text)
    selected = index.select(question, seeds, max_chars=500)
    main = next(item for item in selected if item['file'] == 'primary.md')
    assert main['text'] == paragraph and main['evidence_truncated'] is True
    assert sum(len(item['text']) for item in selected) <= 500
