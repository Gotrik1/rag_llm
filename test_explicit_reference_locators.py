"""Pure syntax/authority regressions for explicit same-document references.

Synthetic text only: a literal source binding is not semantic entailment.
No private corpus, document-name routing, model or network is needed here.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from evidence_selection import SectionEvidenceIndex, _reference_clauses, _section_locator


SUBJECTS = ('Контроль отправлений', 'Обработка заявок')
TARGET_BODY = 'Значение показателя задается отдельно. Запись сохраняется по утвержденному шаблону.'


@pytest.fixture(autouse=True)
def no_runtime_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Reference-locator tests must not use models or network')

    monkeypatch.setattr('socket.socket.connect', forbidden)
    monkeypatch.setattr('socket.create_connection', forbidden)
    monkeypatch.setenv('RAG_REFERENCE_EXPANSION', 'off')


def section_links(text):
    return [reference for reference in _reference_clauses(text) if reference['kind'] == 'section']


def node(title, text=None, *, file='local-guide.md', prefix='Руководство', **metadata):
    if text is None:
        text = TARGET_BODY + '\n\nКод записи: ' + title + '.'
    return SimpleNamespace(
        text=text, metadata={'file_name': file, 'section_path': prefix + ' > ' + title,
                             'section_title': title, 'status': 'active', 'content_hash': 'r1',
                             **metadata})


def select(subject, reference, targets, **options):
    origin = node(subject, subject + ': ' + reference)
    records = [origin, *targets]
    before = deepcopy(records)
    index = SectionEvidenceIndex(records)
    seeds = index.body_seeds(subject, mode='coverage')
    assert seeds and all(item['section_path'] == origin.metadata['section_path'] for item in seeds)
    seeds_before = deepcopy(seeds)
    rows = index.select_v2(subject, seeds, resolve_references=True, top_k=10, max_chars=36000, **options)
    assert records == before and seeds == seeds_before
    return rows, origin


def linked(rows):
    return [row for row in rows if row.get('reference_from')]


@pytest.mark.parametrize('title,expected', (
    ('4 Общие условия', '4'),
    ('4(2) Особые условия', '4(2)'),
    ('4.2 Другие условия', '4.2'),
    ('Пункт 4(2)', '4(2)'),
    ('Подпункт 4(2)', '4(2)'),
    ('Раздел 5.3 Операции', '5.3'),
))
def test_target_locator_preserves_the_complete_numbered_identity(title, expected):
    assert _section_locator({'section_title': title}) == expected


def test_parenthesized_dotted_and_plain_locators_are_not_aliases():
    assert len({_section_locator({'section_title': title})
                for title in ('Пункт 4', 'Пункт 4(2)', 'Пункт 4.2')}) == 3


@pytest.mark.parametrize('token', ('4/2', '4а', '4(2', '4(2)а'))
def test_unsupported_or_incomplete_locator_tokens_never_fall_back_to_numeric_prefix(token):
    assert _section_locator({'section_title': token + ' Условия'}) == ''
    assert section_links('Значение установлено в пункте ' + token + ' настоящего документа.') == []


@pytest.mark.parametrize('text', (
    'Значение установлено в пункте 4(2) настоящего документа.',
    'См. пункт 4(2) настоящего документа.',
    'Порядок применяется согласно подпункту 4(2) настоящих Правил.',
    'Значение установлено в п. 4(2) данного документа.',
    'Значение установлено в пункте 4(2) настоящих Правил; (в ред. постановления № 123).',
))
def test_local_parenthesized_reference_is_exact_and_retains_literal_source_quote(text):
    assert section_links(text) == [{'kind': 'section', 'locator': '4(2)', 'quote': text}]


@pytest.mark.parametrize('text', (
    'См. п. 5.3.',
    'Порядок приведен в пункте 5.3 настоящего документа.',
    'Согласно разделу 5.3 данного Регламента применяются заданные условия.',
))
def test_existing_dotted_and_explicit_same_document_references_remain_valid(text):
    assert section_links(text) == [{'kind': 'section', 'locator': '5.3', 'quote': text}]


@pytest.mark.parametrize('text', (
    'Режим определяется пунктом 7 статьи 23.1 Федерального закона «О хранении записей».',
    'Режим определяется в пункте 7 другого документа «Порядок учета».',
    'Режим определяется в разделе 7 Регламента «Внешний контроль».',
    'Режим определяется в пункте 7 Правил, утвержденных постановлением № 123.',
    'Согласно документу «Порядок учета», применяется пункт 7.',
    'Режим определяется в пункте 7 статьи 2 настоящего документа.',
))
def test_external_authority_or_unsupported_article_namespace_is_not_a_flat_local_section(text):
    assert section_links(text) == []


@pytest.mark.parametrize('text', (
    'Режим определяется пунктом 7 статьи 23.1 Федерального закона «О хранении записей»; '
    'этапы описаны в пункте 5.3 настоящего документа.',
    'Этапы описаны в пункте 5.3 настоящего документа; '
    'режим определяется пунктом 7 статьи 23.1 Федерального закона «О хранении записей».',
    'Режим описан в разделе 7 Регламента «Внешний контроль». '
    'Этапы описаны в пункте 5.3 настоящего документа.',
))
def test_mixed_paragraph_keeps_its_separate_explicit_local_reference(text):
    assert section_links(text) == [{'kind': 'section', 'locator': '5.3', 'quote': text}]


@pytest.mark.parametrize('text', (
    'Пункт 5.3 Операции',
    'Согласно статье 4 Федерального закона применяются общие условия.',
    'Показатель проверяется 4 раза.',
))
def test_a_heading_article_or_ordinary_number_does_not_create_a_local_section_edge(text):
    assert section_links(text) == []


@pytest.mark.parametrize('subject', SUBJECTS)
def test_reference_selects_only_exact_parenthesized_target_not_plain_or_dotted_neighbor(subject):
    targets = [node('Пункт 4'), node('Пункт 4.2'), node('Пункт 4(2)')]
    rows, _ = select(subject, 'Значение установлено в пункте 4(2) настоящего документа.', targets)
    references = linked(rows)
    assert [item['section_title'] for item in references] == ['Пункт 4(2)']
    assert references[0]['reference_target'] == {'kind': 'section', 'locator': '4(2)'}
    assert all(item['section_title'] not in {'Пункт 4', 'Пункт 4.2'} for item in rows)


@pytest.mark.parametrize('subject', SUBJECTS)
def test_missing_exact_parenthesized_target_never_falls_back_to_plain_or_dotted_neighbor(subject):
    rows, _ = select(subject, 'Значение установлено в пункте 4(2) настоящего документа.',
                     [node('Пункт 4'), node('Пункт 4.2')])
    assert linked(rows) == []
    assert [item['section_title'] for item in rows] == [subject]


@pytest.mark.parametrize('subject', SUBJECTS)
def test_external_and_local_links_in_one_block_do_not_import_external_numbered_neighbor(subject):
    text = ('Режим определяется пунктом 7 статьи 23.1 Федерального закона «О хранении записей»; '
            'этапы описаны в пункте 5.3 настоящего документа.')
    rows, origin = select(subject, text, [node('Пункт 7'), node('Пункт 5.3')])
    references = linked(rows)
    assert [item['section_title'] for item in references] == ['Пункт 5.3']
    assert references[0]['reference_from'][0]['quote'] == origin.text
    assert all(item['section_title'] != 'Пункт 7' for item in rows)


def test_existing_local_dotted_target_preserves_source_version_and_quote():
    rows, origin = select(SUBJECTS[0], 'См. п. 5.3 настоящего документа.', [node('Пункт 5.3')])
    references = linked(rows)
    assert len(references) == 1
    assert references[0]['file'] == 'local-guide.md' and references[0]['content_hash'] == 'r1'
    assert references[0]['reference_target'] == {'kind': 'section', 'locator': '5.3'}
    assert references[0]['reference_from'][0]['quote'] == origin.text


@pytest.mark.parametrize('guard', ('other-source', 'deleted', 'different-revision', 'empty-acl', 'excluded-source'))
def test_local_reference_cannot_bypass_existing_source_visibility_or_version_guard(guard):
    metadata = ({'file': 'other-guide.md'} if guard == 'other-source' else
                {'status': 'deleted'} if guard == 'deleted' else
                {'content_hash': 'r2'} if guard == 'different-revision' else {})
    options = ({'allowed_sources': set()} if guard == 'empty-acl' else
               {'excluded_sources': {'local-guide.md'}} if guard == 'excluded-source' else {})
    rows, _ = select(SUBJECTS[0], 'См. пункт 5.3 настоящего документа.',
                     [node('Пункт 5.3', **metadata)], **options)
    assert linked(rows) == []
    assert all(item['section_title'] != 'Пункт 5.3' for item in rows)


def test_external_article_cannot_supply_authority_for_cross_category_import():
    # Existing category syntax only; the synthetic text contains no domain fact.
    subject = SUBJECTS[0] + ' для 1 ЦК'
    text = (subject + ': режим определяется пунктом 7 статьи 23.1 Федерального закона '
            '«О хранении записей»; этапы описаны в пункте 5.3 настоящего документа.')
    origin = node(SUBJECTS[0], text, prefix='1 ЦК')
    local = node('Пункт 5.3', prefix='1 ЦК')
    other_category = node('Пункт 7', prefix='6 ЦК')
    index = SectionEvidenceIndex([origin, local, other_category])
    seeds = index.body_seeds(subject, mode='coverage')
    rows = index.select_v2(subject, seeds, resolve_references=True,
                            constraint_question=subject, top_k=10, max_chars=36000)
    assert [item['section_title'] for item in linked(rows)] == ['Пункт 5.3']
    assert all(item['scope_categories'] == ['1'] for item in rows)


@pytest.mark.parametrize('text', (
    'Значение установлено в подпункте 7 пункта 1.1 приложения 1.',
    'Значение установлено в подпункте 7 пункта 1.1 настоящего документа.',
    'Значение установлено в пункте 7 приложения 1 настоящего документа.',
    'Значение установлено в пункте 7 раздела 1.1 настоящего документа.',
    'В приложении 1, пункте 7 настоящего документа установлены условия.',
))
def test_nested_container_chain_is_unresolved_not_a_flat_child_or_parent_reference(text):
    assert section_links(text) == []


def test_unresolved_nested_chain_does_not_erase_separate_explicit_local_reference():
    text = ('См. подпункт 7 пункта 1.1 приложения 1; '
            'отдельно см. пункт 5.3 настоящего документа.')
    assert section_links(text) == [{'kind': 'section', 'locator': '5.3', 'quote': text}]


def test_separately_coordinated_flat_references_are_not_mistaken_for_a_hierarchy():
    text = 'См. пункт 5.3, пункт 7 настоящего документа.'
    assert section_links(text) == [
        {'kind': 'section', 'locator': '5.3', 'quote': text},
        {'kind': 'section', 'locator': '7', 'quote': text},
    ]


@pytest.mark.parametrize('subject', SUBJECTS)
def test_nested_reference_never_falls_back_to_either_available_flat_number(subject):
    text = ('См. подпункт 7 пункта 1.1 приложения 1; '
            'отдельно см. пункт 5.3 настоящего документа.')
    rows, _ = select(subject, text, [node('Пункт 7'), node('Пункт 1.1'), node('Пункт 5.3')])
    assert [item['section_title'] for item in linked(rows)] == ['Пункт 5.3']
    assert all(item['section_title'] not in {'Пункт 7', 'Пункт 1.1'} for item in rows)


@pytest.mark.parametrize('text', (
    'Значение установлено в подпункте 7 п. 1.1 приложения 1.',
    'Значение установлено в подп. 7 пункта 1.1 настоящего документа.',
    'Значение установлено в пункте 7 разд. 1.1 настоящего документа.',
    'Значение установлено в разд. 1.1 подпункте 7 настоящего документа.',
    'Значение установлено в п. 7 п. 1.1 приложения 1.',
))
def test_abbreviated_nested_chain_cannot_fall_back_to_flat_child_or_parent(text):
    assert section_links(text) == []


def test_abbreviated_nested_chain_preserves_a_separate_explicit_local_reference():
    text = ('См. подпункт 7 п. 1.1 приложения 1; '
            'отдельно см. п. 5.3 настоящего документа.')
    assert section_links(text) == [{'kind': 'section', 'locator': '5.3', 'quote': text}]


def test_coordinated_abbreviated_flat_references_are_not_a_container_chain():
    text = 'См. п. 5.3, п. 7 настоящего документа.'
    assert section_links(text) == [
        {'kind': 'section', 'locator': '5.3', 'quote': text},
        {'kind': 'section', 'locator': '7', 'quote': text},
    ]


@pytest.mark.parametrize('subject', SUBJECTS)
def test_abbreviated_nested_reference_never_imports_available_flat_numbers(subject):
    text = ('См. подпункт 7 п. 1.1 приложения 1; '
            'отдельно см. п. 5.3 настоящего документа.')
    rows, _ = select(subject, text, [node('Пункт 7'), node('Пункт 1.1'), node('Пункт 5.3')])
    assert [item['section_title'] for item in linked(rows)] == ['Пункт 5.3']
    assert all(item['section_title'] not in {'Пункт 7', 'Пункт 1.1'} for item in rows)


@pytest.mark.parametrize('text', (
    'Согласно Регламенту от 01.01.2020 № 123, применяется пункт 7.',
    'Согласно Инструкции от 1 января 2020 года N 42, применяется пункт 7.',
))
def test_dated_named_document_authority_is_not_a_flat_local_reference(text):
    assert section_links(text) == []


def test_dated_external_authority_does_not_erase_a_separate_local_reference():
    text = ('Согласно Регламенту от 01.01.2020 № 123, применяется пункт 7; '
            'этапы описаны в пункте 5.3 настоящего документа.')
    assert section_links(text) == [{'kind': 'section', 'locator': '5.3', 'quote': text}]


def test_dated_external_reference_never_imports_an_available_flat_number():
    rows, _ = select(SUBJECTS[0],
                     'Согласно Регламенту от 01.01.2020 № 123, применяется пункт 7.', [node('Пункт 7')])
    assert linked(rows) == []
    assert all(item['section_title'] != 'Пункт 7' for item in rows)


@pytest.mark.parametrize('self_word', ('настоящему', 'данному'))
@pytest.mark.parametrize('qualification', ('', ' от 01.01.2020 № 123'))
def test_dated_or_bare_explicit_self_prefix_remains_local(self_word, qualification):
    text = f'Согласно {self_word} Регламенту{qualification}, применяется пункт 7.'
    assert section_links(text) == [{'kind': 'section', 'locator': '7', 'quote': text}]


def test_double_dot_target_locator_is_unresolved_not_a_numeric_prefix():
    assert _section_locator({'section_title': '4..2 Условия'}) == ''


def test_double_dot_reference_is_unresolved_not_a_numeric_prefix():
    assert section_links('Значение установлено в пункте 4..2 настоящего документа.') == []


def test_single_sentence_period_preserves_a_valid_integer_locator():
    text = 'См. пункт 4.'
    assert _section_locator({'section_title': '4. Условия'}) == '4'
    assert section_links(text) == [{'kind': 'section', 'locator': '4', 'quote': text}]


@pytest.mark.parametrize('container', (
    'настоящей статьи', 'данной статьи', 'настоящего раздела',
    'данного приложения', 'настоящей части',
))
def test_explicit_self_container_does_not_establish_a_flat_namespace(container):
    assert section_links(f'Значение установлено в пункте 7 {container}.') == []


@pytest.mark.parametrize('document', ('настоящего документа', 'данного Регламента'))
def test_explicit_self_document_still_establishes_local_authority(document):
    text = f'Значение установлено в пункте 7 {document}.'
    assert section_links(text) == [{'kind': 'section', 'locator': '7', 'quote': text}]
