"""Offline navigation contracts; search matches never certify source evidence."""
from copy import deepcopy
import re

import pytest
from nltk.stem.snowball import SnowballStemmer

from document_navigation import DocumentNavigationProvider, NavigationError


def features(value):
    return frozenset(re.findall(r'[\w]+', value.casefold()))


def row(text='Проверка датчика каждые 12 часов.', file='manual.md', **metadata):
    return {'file': file, 'text': text, 'section_path': 'Порядок > Проверка', **metadata}


def provider(records, **kwargs):
    return DocumentNavigationProvider(records, features=features, **kwargs)


def all_pages(call, **kwargs):
    items, seen, cursor = [], set(), None
    while True:
        page = call(cursor=cursor, **kwargs)
        items.extend(page['items'])
        cursor = page['next_cursor']
        if cursor is None:
            assert len(items) == page['total']
            return items
        assert cursor not in seen
        seen.add(cursor)


def first_node(navigation):
    doc = navigation.list_documents()['items'][0]
    return navigation.outline(doc['doc_id'])['items'][0]


def test_all_documents_and_native_records_are_reachable_with_pagination():
    records = [row(file=f'folder{i}/manual.md', section_path=f'Раздел {n}')
               for i in range(7) for n in range(4)]
    navigation = provider(records)
    docs = all_pages(navigation.list_documents, limit=2)
    assert len(docs) == 7
    read = []
    for doc in docs:
        nodes = all_pages(navigation.outline, doc_id=doc['doc_id'], limit=3)
        assert len(nodes) == doc['node_count'] == 4
        read.extend(navigation.read(node['node_id']) for node in nodes)
    assert read == records


def test_search_paginates_beyond_first_page_without_hidden_top_k():
    navigation = provider([row(section_path=f'Раздел {i}') for i in range(23)])
    matches = all_pages(navigation.search, query='датчика', limit=4)
    assert len(matches) == 23
    assert len({item['node_id'] for item in matches}) == 23
    assert all('text' in item['matched_fields'] for item in matches)


def test_read_returns_full_original_and_detaches_all_nested_metadata():
    original = row(text='A' * 40000 + ' НЕ УДАЛЯТЬ ПОСЛЕДНЕЕ УСЛОВИЕ.',
                   revision_id=0, metadata={'nested': ['original']})
    expected = deepcopy(original)
    navigation = provider([original])
    handle = first_node(navigation)['node_id']
    original['text'] = 'changed'
    original['metadata']['nested'][0] = 'changed'
    result = navigation.read(handle)
    assert result == expected
    result['metadata']['nested'].append('changed')
    assert navigation.read(handle) == expected
    assert not any(key in result for key in ('node_id', 'doc_id'))


def test_supplied_features_support_true_russian_inflections_and_headingless_body():
    stemmer = SnowballStemmer('russian')
    russian = lambda value: frozenset(stemmer.stem(word) for word in re.findall(r'[а-яё]+', value.casefold()))
    navigation = DocumentNavigationProvider(
        [row('Калибровка выполняется датчиками.', section_path='')], features=russian)
    found = navigation.search('калибровки датчиков')['items']
    assert len(found) == 1 and found[0]['score'] == 1
    assert found[0]['matched_fields'] == ['text']
    assert navigation.read(found[0]['node_id'])['text'] == 'Калибровка выполняется датчиками.'


@pytest.mark.parametrize('field', ['revision_id', 'version_id', 'document_version'])
def test_absent_null_and_scalar_revision_types_never_merge(field):
    versions = ({}, {field: None}, {field: ''}, {field: False}, {field: 0},
                {field: 0.0}, {field: '0'})
    navigation = provider([row(**version) for version in versions])
    docs = navigation.list_documents()['items']
    assert len(docs) == len(versions)
    for doc, version in zip(docs, versions):
        assert (field in doc['source']) == (field in version)
        if field in version:
            assert type(doc['source'][field]) is type(version[field])
    assert len({doc['doc_id'] for doc in docs}) == len(docs)


def test_exact_file_path_and_all_document_revision_fields_bind_document_identity():
    fields = ['file_path', 'document_id', 'source_id', 'revision_id', 'version_id',
              'document_version', 'content_hash', 'sha256']
    records = [row()] + [row(**{key: 'distinct'}) for key in fields]
    navigation = provider(records)
    assert navigation.list_documents()['total'] == len(records)
    assert [doc['source'] for doc in navigation.list_documents()['items']] == [
        {key: item[key] for key in ('file', *fields) if key in item} for item in records]


def test_deleted_empty_and_nonstring_text_are_excluded_before_visibility():
    records = [row(), row(file='deleted.md', status='deleted'),
               row(file='empty.md', text=' \n'), row(file='none.md', text=None),
               {'file': 'missing.md'}]
    navigation = provider(records)
    assert navigation.list_documents()['total'] == 1
    assert navigation.search('датчика')['total'] == 1


def test_authorized_supplied_scope_is_the_only_source_of_documents():
    allowed = row(file='/tenant-A/manual.md')
    foreign = provider([row(file='/tenant-B/manual.md')])
    navigation = provider([allowed])
    assert [item['file'] for item in navigation.list_documents()['items']] == [allowed['file']]
    with pytest.raises(NavigationError):
        navigation.read(first_node(foreign)['node_id'])
    assert provider([]).list_documents() == {'items': [], 'next_cursor': None, 'total': 0}


def test_identical_snapshot_instances_cannot_share_handles_or_cursors():
    records = [row(file=f'manual{i}.md') for i in range(3)]
    first, second = provider(records), provider(records)
    foreign_doc = first.list_documents(limit=1)
    with pytest.raises(NavigationError):
        second.outline(foreign_doc['items'][0]['doc_id'])
    with pytest.raises(NavigationError):
        second.list_documents(cursor=foreign_doc['next_cursor'])
    with pytest.raises(NavigationError):
        second.read(first_node(first)['node_id'])


def test_source_metadata_cannot_forge_navigation_handles_or_modify_snapshot():
    foreign = provider([row()])
    foreign_node = first_node(foreign)
    supplied = row(node_id=foreign_node['node_id'], doc_id=foreign_node['doc_id'],
                   source={'revision_id': 'forged'})
    navigation = provider([supplied])
    actual = first_node(navigation)
    assert actual['node_id'] != foreign_node['node_id']
    with pytest.raises(NavigationError):
        navigation.read(supplied['node_id'])
    assert navigation.read(actual['node_id']) == supplied
    actual['source']['revision_id'] = 'forged'
    assert 'revision_id' not in first_node(navigation)['source']


def test_cursors_cannot_change_method_query_document_or_offset():
    navigation = provider([row(section_path=f'Раздел {i}') for i in range(4)] + [row(file='other.md')])
    doc = navigation.list_documents()['items'][0]['doc_id']
    cursor = navigation.search('датчика', doc_id=doc, limit=1)['next_cursor']
    for call in (
        lambda: navigation.search('проверка', doc_id=doc, cursor=cursor),
        lambda: navigation.search('датчика', cursor=cursor),
        lambda: navigation.outline(doc, cursor=cursor),
        lambda: navigation.list_documents(cursor=cursor),
        lambda: navigation.search('датчика', doc_id=doc, cursor=cursor.replace(':1:', ':0:')),
    ):
        with pytest.raises(NavigationError):
            call()


@pytest.mark.parametrize('limit', [0, 101, True, -1, 1.5, '2'])
def test_page_limits_fail_closed(limit):
    navigation = provider([row()])
    with pytest.raises(NavigationError):
        navigation.list_documents(limit=limit)


def test_search_projection_never_rewrites_read_and_heading_is_navigation_only():
    original = row('[SECTION:Порядок]\nПроверка', section_title='Проверка')
    navigation = provider([original], native_text=lambda text: re.sub(r'\[SECTION:[^]]*\]\s*', '', text))
    node = first_node(navigation)
    assert node['is_heading_only']
    assert navigation.read(node['node_id']) == original
    assert 'text' not in node
    assert 'evidence_id' not in node and 'citation' not in node


def test_document_restricted_search_does_not_widen_on_miss():
    navigation = provider([row('Первый датчик.', file='one.md'), row('Второй манометр.', file='two.md')])
    first, second = navigation.list_documents()['items']
    assert navigation.search('манометр', doc_id=first['doc_id'])['total'] == 0
    assert navigation.search('манометр', doc_id=second['doc_id'])['total'] == 1
    with pytest.raises(NavigationError):
        navigation.search('манометр', doc_id='unknown')


def test_outline_keeps_native_headings_and_headingless_records_without_synthetic_text():
    navigation = provider([row('Изначальный текст.', section_path=''),
                           row('Условие.', section_path='Глава > Особый раздел')])
    doc = navigation.list_documents()['items'][0]
    assert doc['title'] == doc['file'] == 'manual.md'
    nodes = navigation.outline(doc['doc_id'])['items']
    assert [node['section_path'] for node in nodes] == ['', 'Глава > Особый раздел']
    assert all('text' not in node for node in nodes)


def test_invalid_identity_metadata_fails_instead_of_merging_versions():
    with pytest.raises(NavigationError, match='invalid-source-identity'):
        provider([row(revision_id={'untrusted': 'value'})])
    with pytest.raises(NavigationError, match='invalid-native-record'):
        provider([row(revision_id=float('nan'))])


def test_search_preview_is_bounded_near_body_match_and_does_not_replace_full_read():
    text = 'Предисловие. ' * 100 + 'Уникальныйдатчик работает по условию. ' + 'Продолжение. ' * 100
    navigation = provider([row(text)])
    match = navigation.search('Уникальныйдатчик')['items'][0]
    assert 'Уникальныйдатчик' in match['preview']
    assert len(match['preview']) <= 300
    assert match['preview_kind'] == 'navigation-only' and match['preview_truncated']
    assert navigation.read(match['node_id'])['text'] == text


def test_preview_token_scanning_only_runs_for_returned_search_page():
    calls = []
    def counted(value):
        calls.append(value)
        return features(value)
    navigation = DocumentNavigationProvider([row('датчик ' * 100) for _ in range(9)], features=counted)
    calls.clear()
    page = navigation.search('датчик', limit=2)
    # One query extraction plus one first matching word per returned row.
    assert len(page['items']) == 2 and calls == ['датчик'] * 3


def test_crafted_cursor_unicode_and_wrong_types_are_rejected_cleanly():
    navigation = provider([row(file='one.md'), row(file='two.md')])
    valid = navigation.list_documents(limit=1)['next_cursor']
    for cursor in (True, -1, valid.rsplit(':', 1)[0] + ':я' * 64):
        with pytest.raises(NavigationError):
            navigation.list_documents(cursor=cursor)


def test_existing_empty_stem_keeps_native_record_readable_without_search_match():
    from evidence_selection import term_keys

    assert term_keys('ь') == frozenset({''})
    original = row('ь', revision_id=0, metadata={'original': ['ь']})
    navigation = DocumentNavigationProvider([original], features=term_keys)
    assert navigation.list_documents()['total'] == 1
    node = first_node(navigation)
    assert navigation.read(node['node_id']) == original
    assert navigation.search('ь')['total'] == 0


def test_empty_string_and_normal_feature_keys_do_not_abort_or_add_false_matches():
    navigation = DocumentNavigationProvider(
        [row('датчик')], features=lambda value: features(value) | {''})
    found = navigation.search('датчик')['items']
    assert len(found) == 1 and found[0]['matched_terms'] == ['датчик']
    assert navigation.search('манометр')['total'] == 0


def test_nonstring_feature_keys_still_fail_closed_when_empty_strings_are_allowed():
    for invalid in (None, 0, False):
        with pytest.raises(NavigationError, match='invalid-lexical-features'):
            DocumentNavigationProvider([row()], features=lambda value: {'', invalid})
