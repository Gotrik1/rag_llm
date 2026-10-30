"""Pure category parsing cache must not capture scope decisions or source objects."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import Mock

import pytest
import evidence_selection as selector


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr('socket.socket.connect', Mock(side_effect=AssertionError('No network')))
    cached=getattr(selector,'_cached_category_ids',None)
    if cached is not None:cached.cache_clear()
    yield
    if cached is not None:cached.cache_clear()


def test_repeated_scope_strings_are_parsed_once_without_caching_the_mutable_record(monkeypatch):
    expand = Mock(wraps=selector._expand_ordinal_categories)
    monkeypatch.setattr(selector, '_expand_ordinal_categories', expand)
    item={'section_path':'Unique neutral scope test > 3 ЦК','text':'Literal original text.'}
    before=deepcopy(item)
    for _ in range(8):assert selector._scope_categories(item)==frozenset({'3'})
    assert expand.call_count==1
    assert item==before
    item['section_path']='Unique neutral scope test > 6 ЦК'
    assert selector._scope_categories(item)==frozenset({'6'})


@pytest.mark.parametrize('value', ['1 ЦК','6ЦК','1–6 ЦК','5 и 6 ЦК','первой ценовой категории',
    'Второй ЦК','Первый платёж','A 123','', 'ЦК 6', '1 ЦК\n\n6 ЦК'])
def test_cached_and_direct_parser_are_exactly_equal(value):
    assert selector._category_ids(value)==selector._parse_category_ids(value)
    assert isinstance(selector._category_ids(value),frozenset)


def test_long_literal_input_bypasses_cache_and_is_never_truncated(monkeypatch):
    value='Neutral prose. ' * 1000 + ' 6 ЦК'
    cached=Mock(side_effect=AssertionError('Long source retained in cache'))
    monkeypatch.setattr(selector,'_cached_category_ids',cached)
    assert selector._category_ids(value)==frozenset({'6'})
    cached.assert_not_called()


def test_cache_is_bounded_and_has_only_literal_string_keys():
    assert selector._cached_category_ids.cache_parameters()=={'maxsize':16384,'typed':False}
    values=['Snapshot A > 1 ЦК','Snapshot B > 6 ЦК','Snapshot A > 1 ЦК']*20
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(selector._category_ids,values))
    assert results==[frozenset({'1'}),frozenset({'6'}),frozenset({'1'})]*20
