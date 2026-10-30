"""A broad calculation request needs native variant details, not just a parent list."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex, _query_groups, _sibling_family


THEMES = (
    ('складские операции', 'сбор за размещение отправлений',
     'Как рассчитывается сбор за размещение отправлений в складских операциях?',
     ('текущая масса', 'текущий объем', 'фиксированная ставка', 'историческая масса отдельно',
      'историческая масса суммарно', 'историческая сумма отдельно', 'историческая сумма суммарно')),
    ('канцелярские операции', 'показатель проверки заявок',
     'Как рассчитывается показатель проверки заявок в канцелярских операциях?',
     ('прямой подсчет', 'текущая длительность', 'фиксированный норматив', 'предыдущее число отдельно',
      'предыдущее число суммарно', 'предыдущая длительность отдельно', 'предыдущая длительность суммарно')),
)


def corpus(theme, *, parent_node):
    domain, topic, question, labels = theme
    root = f'Руководство > {domain} > {topic}'
    family = root + f' > Алгоритмы расчета: {topic}'
    rows = []

    def row(path, text, score):
        return {'file': 'procedures.md', 'file_path': '/owned/procedures.md', 'section_path': path,
                'section_title': path.rsplit(' > ', 1)[-1], 'text': text, 'score': score,
                'source_id': 'owned-source-A', 'revision_id': 'revision-A', 'status': 'active'}

    rows.append(row(root, f'{topic.capitalize()} применяется в разделе «{domain}». Допустимы семь вариантов:\n\n'
                    + '\n\n'.join(f'- {label}' for label in labels), 15))
    if parent_node:
        rows.append(row(family, f'Алгоритмы расчета: {topic}. Каждый вариант имеет отдельную базу и условие применения.', 14))
    children = []
    for i, label in enumerate(labels):
        text = (f'{topic.capitalize()}: вариант «{label}» использует исходную величину q_{i+1}. '
                f'Условие варианта: проверяется признак Z_{i+1}; результат отличается от прочих способов.\n\n'
                f'$$ F_{{{i+1}}} = {11+i} \\cdot q_{{{i+1}}} $$\n\n'
                f'Здесь q_{i+1} — исходная величина данного варианта; единица результата — условная единица {i+1}.')
        child = row(family + ' > ' + label, text, 13 - i if i < 3 else 1)
        rows.append(child)
        children.append(child)
    for i in range(7):
        rows.append(row(f'Справка > Рабочая область {i}',
                        f'Для раздела «{domain}» рабочая область показывает {topic}. '
                        f'Выполняется настройка расчета и просмотр списка документов. Организационная запись {i}.', 8))
    nodes = [SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows]
    ranked = sorted(rows, key=lambda item: -item['score'])
    return SectionEvidenceIndex(nodes), ranked, children, family


@pytest.mark.parametrize('theme', THEMES)
@pytest.mark.parametrize('parent_node', (False, True))
@pytest.mark.parametrize('references', (False, True))
def test_broad_calculation_retains_all_native_variant_definitions(theme, parent_node, references):
    index, ranked, children, family = corpus(theme, parent_node=parent_node)
    original = deepcopy(ranked)
    selected = index.select_v2(theme[2], ranked, top_k=10, max_chars=36000, resolve_references=references)
    found = {row['section_path']: row for row in selected}
    assert all(child['section_path'] in found and found[child['section_path']]['text'] == child['text'] for child in children)
    packets = [row['evidence_family'] for row in selected if row.get('evidence_family')]
    assert packets and packets[0]['complete'] is True
    assert packets[0]['parent_section_path'] == family
    assert set(packets[0]['selected_children']) == {child['section_path'] for child in children}
    assert packets[0]['total_children'] == 7
    assert len(selected) <= 10
    assert ranked == original


@pytest.mark.parametrize('theme', THEMES)
def test_specific_variant_is_not_a_broad_calculation_family(theme):
    index, _, children, _ = corpus(theme, parent_node=True)
    requested = children[3]
    question = theme[2].rstrip('?') + f' по варианту «{theme[3][3]}»?'
    selected = index.select_v2(question, [requested], top_k=10, max_chars=36000, resolve_references=False)
    assert any(row['text'] == requested['text'] for row in selected)
    assert not any(row.get('evidence_family', {}).get('total_children') == 7 for row in selected)


@pytest.mark.parametrize('theme', THEMES)
def test_inherited_context_does_not_come_from_an_unrelated_ancestor(theme):
    index, ranked, _, _ = corpus(theme, parent_node=True)
    wrong_domain = 'прочие процессы'
    unrelated = []
    for row in ranked:
        if ' > Алгоритмы расчета:' in row['section_path']:
            unrelated.append({**row, 'section_path': row['section_path'].replace(theme[0], wrong_domain),
                              'text': row['text'].replace(theme[0], wrong_domain),
                              'selection_reason': 'retrieval-rank', 'evidence_role': 'main',
                              '_heading_coverage': .8, '_local_coverage': .8, '_score': 10})
    sections = [{**row, 'position': i} for i, row in enumerate(unrelated)]
    result = _sibling_family(sections, unrelated, _query_groups(theme[2]), lambda item: True, frozenset())
    assert result is None, 'A matching topic in another hierarchy cannot borrow this domain from elsewhere'


@pytest.mark.parametrize('theme', THEMES)
def test_family_reservation_respects_whole_source_exclusion(theme):
    index, ranked, _, _ = corpus(theme, parent_node=True)
    assert index.select_v2(theme[2], ranked, excluded_sources={'procedures.md'}, resolve_references=False) == []


@pytest.mark.parametrize('theme', THEMES)
def test_insufficient_budget_is_observable_and_does_not_claim_complete_family(theme):
    index, ranked, children, _ = corpus(theme, parent_node=True)
    meta = {}
    selected = index.select_v2(theme[2], ranked, top_k=10, max_chars=1000,
                                resolve_references=False, selection_meta=meta)
    assert sum(len(row['text']) for row in selected) <= 1000
    assert all(row.get('evidence_family', {}).get('complete') is not True for row in selected)
    assert meta.get('family_coverage_incomplete') is True or any(
        row.get('evidence_family', {}).get('complete') is False for row in selected)
    assert not all(any(child['text'] == row['text'] for row in selected) for child in children)


@pytest.mark.parametrize('theme', THEMES)
def test_family_version_compatibility_does_not_merge_another_revision(theme):
    index, ranked, _, family = corpus(theme, parent_node=True)
    candidates = [{**row, 'selection_reason': 'retrieval-rank', 'evidence_role': 'main',
                   '_heading_coverage': .8, '_local_coverage': .8, '_score': 10}
                  for row in ranked if row['section_path'].startswith(family)]
    foreign = {**candidates[-1], 'revision_id': 'revision-B', 'text': 'Факт другой редакции.'}
    sections = [{**row, 'position': i} for i, row in enumerate([*candidates, foreign])]
    result = _sibling_family(sections, candidates, _query_groups(theme[2]), lambda item: True, frozenset())
    if result is not None:
        assert all(row['revision_id'] == 'revision-A' for row in result['children'])
        assert all(row['text'] != foreign['text'] for row in result['children'])


@pytest.mark.parametrize('theme', THEMES)
def test_negative_variant_request_is_not_expanded_to_the_whole_family(theme):
    index, ranked, children, _ = corpus(theme, parent_node=True)
    excluded_phrase = 'историческую массу' if theme is THEMES[0] else 'предыдущее число'
    question = theme[2] + f' Не рассматривай {excluded_phrase}.'
    selected = index.select_v2(question, ranked, resolve_references=False)
    assert not any(row.get('evidence_family', {}).get('complete') is True for row in selected)
    assert not {children[3]['section_path'], children[4]['section_path']} & {row['section_path'] for row in selected}


@pytest.mark.parametrize('theme', THEMES)
def test_unknown_parent_cannot_certify_conflicting_child_revisions(theme):
    _, ranked, children, family = corpus(theme, parent_node=True)
    parent = next(row for row in ranked if row['section_path'] == family)
    parent = {key: value for key, value in parent.items() if key not in ('revision_id', 'source_id')}
    parent.update(selection_reason='subject-heading', evidence_role='main',
                  _heading_coverage=1, _local_coverage=1, _score=10)
    conflicting = {**children[-1], 'revision_id': 'revision-B', 'text': 'Иное условие другой редакции.'}
    sections = [{**row, 'position': i} for i, row in enumerate([parent, *children, conflicting])]
    meta = {}
    family_result = _sibling_family(sections, [parent], _query_groups(theme[2]),
                                      lambda item: True, frozenset(), diagnostics=meta)
    assert family_result is None
    assert meta.get('family_coverage_incomplete') is True


@pytest.mark.parametrize('theme', THEMES)
def test_inherited_family_needs_more_than_a_display_filename(theme):
    _, ranked, _, family = corpus(theme, parent_node=True)
    candidates = []
    for row in ranked:
        if row['section_path'].startswith(family):
            item = {key: value for key, value in row.items() if key not in ('file_path', 'source_id', 'revision_id')}
            item.update(selection_reason='retrieval-rank', evidence_role='main',
                        _heading_coverage=.8, _local_coverage=.8, _score=10)
            candidates.append(item)
    sections = [{**row, 'position': i} for i, row in enumerate(candidates)]
    assert _sibling_family(sections, candidates, _query_groups(theme[2]), lambda item: True, frozenset()) is None


def two_profile_selection(*, independent_subject):
    from search_focus import SearchFocus, SearchOperation, SearchSubject
    _, first, _, first_family = corpus(THEMES[0], parent_node=True)
    _, second, _, second_family = corpus(THEMES[1], parent_node=True)
    second = [{**row, 'file': 'other.md', 'file_path': '/owned/other.md', 'source_id': 'other-source'}
              for row in second if row['section_path'].startswith(second_family)]
    parent = next(row for row in second if row['section_path'] == second_family)
    parent = {key: value for key, value in parent.items() if key not in ('revision_id', 'source_id')}
    second = [parent, *(row for row in second if row['section_path'] != second_family)]
    conflict = {**second[-1], 'revision_id': 'revision-B', 'text': 'Иное условие другой редакции.'}
    rows = [*first, *second, conflict]
    nodes = [SimpleNamespace(text=row['text'], metadata={**row, 'file_name': row['file']}) for row in rows]
    if independent_subject:
        subjects = (SearchSubject(THEMES[0][1]), SearchSubject(THEMES[1][1]))
        question = THEMES[0][2] + ' ' + THEMES[1][2]
    else:
        subjects = (SearchSubject(THEMES[0][1], (THEMES[1][1],)),)
        question = THEMES[0][2]
    focus = SearchFocus(SearchOperation.CALCULATE, subjects)
    meta = {}
    selected = SectionEvidenceIndex(nodes).select_v2(question, [*first, parent], search_focus=focus,
        constraint_question=question, selection_meta=meta, resolve_references=False)
    family = next(row['evidence_family'] for row in selected if row.get('evidence_family'))
    assert family['parent_section_path'] == first_family and family['complete'] is True
    return meta


def test_unselected_generated_alternative_does_not_poison_chosen_primary_family():
    meta = two_profile_selection(independent_subject=False)
    assert meta.get('family_coverage_incomplete') is not True


def test_independently_requested_second_subject_keeps_its_unknown_coverage():
    meta = two_profile_selection(independent_subject=True)
    assert meta.get('family_coverage_incomplete') is True
