"""Generic Word table provenance; no corpus names or domain-specific columns."""

import copy
import json
import zipfile
from unittest.mock import patch

import pytest

import document_ingestion
import docx_parser


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def _p(text):
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _cell(text, properties=''):
    return f'<w:tc><w:tcPr>{properties}</w:tcPr>{_p(text)}</w:tc>'


def _row(*cells, properties=''):
    return f'<w:tr><w:trPr>{properties}</w:trPr>{"".join(cells)}</w:tr>'


def _table(*rows, width=2):
    grid = '<w:tblGrid>' + '<w:gridCol w:w="1000"/>' * width + '</w:tblGrid>'
    return '<w:tbl>' + grid + ''.join(rows) + '</w:tbl>'


def _docx(tmp_path, body, name='grid.docx'):
    path = tmp_path / name
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return path


def _read(path):
    return docx_parser.read_docx_structured(path)


def _marked_table():
    return _table(
        _row(_cell('Origin'), _cell('Entry'), properties='<w:tblHeader/>'),
        _row(_cell('Organization'), _cell('Workspace')),
    )


def test_structured_reader_keeps_exact_offsets_and_legacy_text(tmp_path):
    source = _docx(tmp_path, _p('Before') + _marked_table() + _p('After'))
    parsed = _read(source)
    assert docx_parser.read_docx_text(source) == parsed.text
    assert len(parsed.tables) == 1
    table = parsed.tables[0]
    assert table['table_id'] == 0
    assert parsed.text[table['start_char']:table['end_char']] == table['text']
    assert table['text'] == '[TABLE]\nOrigin | Entry\nOrganization | Workspace'
    assert table['rows'][0]['is_header'] is True
    assert table['rows'][1]['is_header'] is False
    assert table['rows'][1]['cells'] == [
        {'column_index': 0, 'column_span': 1, 'text': 'Organization', 'vertical_merge': None},
        {'column_index': 1, 'column_span': 1, 'text': 'Workspace', 'vertical_merge': None},
    ]
    assert not table['warnings']


@pytest.mark.parametrize('header', ['', '<w:tblHeader w:val="0"/>', '<w:tblHeader w:val="false"/>'])
def test_first_row_is_not_guessed_to_be_header(tmp_path, header):
    table = _table(_row(_cell('Title-like label'), _cell('Another label'), properties=header))
    parsed = _read(_docx(tmp_path, table))
    assert parsed.tables[0]['rows'][0]['is_header'] is False
    assert 'no-explicit-header' in parsed.tables[0]['warnings']


def test_explicit_header_and_grid_coordinates_not_cell_ordinals(tmp_path):
    table = _table(
        _row(_cell('Group', '<w:gridSpan w:val="2"/>'), _cell('Tail'), properties='<w:tblHeader/>'),
        _row(_cell('Middle'), properties='<w:gridBefore w:val="1"/><w:gridAfter w:val="1"/>'),
        width=3,
    )
    parsed = _read(_docx(tmp_path, table)).tables[0]
    assert [(cell['column_index'], cell['column_span']) for cell in parsed['rows'][0]['cells']] == [(0, 2), (2, 1)]
    assert parsed['rows'][1]['cells'][0]['column_index'] == 1
    assert parsed['rows'][1]['grid_before'] == 1
    assert parsed['rows'][1]['grid_after'] == 1
    assert not parsed['warnings']


def test_vertical_merge_states_do_not_invent_continuation_value(tmp_path):
    table = _table(
        _row(_cell('A', '<w:vMerge w:val="restart"/>'), _cell('B'), properties='<w:tblHeader/>'),
        _row(_cell('', '<w:vMerge/>'), _cell('C')),
    )
    parsed = _read(_docx(tmp_path, table)).tables[0]
    assert parsed['rows'][0]['cells'][0]['vertical_merge'] == 'restart'
    assert parsed['rows'][1]['cells'][0]['vertical_merge'] == 'continue'
    assert parsed['rows'][1]['cells'][0]['text'] == ''
    assert not parsed['warnings']


@pytest.mark.parametrize('properties, warning', [
    ('<w:vMerge/>', 'orphan-vertical-merge'),
    ('<w:vMerge w:val="guess"/>', 'invalid-vertical-merge'),
    ('<w:gridSpan w:val="0"/>', 'invalid-grid-span'),
    ('<w:gridSpan w:val="abc"/>', 'invalid-grid-span'),
    ('<w:hMerge w:val="restart"/>', 'unsupported-horizontal-merge'),
])
def test_invalid_merge_is_explicit_not_silent(tmp_path, properties, warning):
    table = _table(_row(_cell('A', properties), _cell('B'), properties='<w:tblHeader/>'))
    parsed = _read(_docx(tmp_path, table)).tables[0]
    assert any(item.startswith(warning) for item in parsed['warnings'])
    assert 'A' in parsed['text'] and 'B' in parsed['text']


def test_nested_content_does_not_become_outer_rows_or_columns(tmp_path):
    nested = _table(_row(_cell('INNER-A'), _cell('INNER-B')))
    outer_cell = f'<w:tc>{_p("Outer")}{nested}{_p("Tail")}</w:tc>'
    outer = _table(_row(outer_cell, _cell('Outer-B'), properties='<w:tblHeader/>'))
    parsed = _read(_docx(tmp_path, outer)).tables[0]
    assert len(parsed['rows']) == 1
    assert len(parsed['rows'][0]['cells']) == 2
    assert 'INNER-A' in parsed['rows'][0]['cells'][0]['text']
    assert 'INNER-B' in parsed['rows'][0]['cells'][0]['text']
    assert parsed['rows'][0]['cells'][1]['text'] == 'Outer-B'
    assert any(item.startswith('nested-table') for item in parsed['warnings'])


def test_duplicate_table_text_has_distinct_verified_document_offsets(tmp_path):
    source = _docx(tmp_path, _marked_table() + _p('Boundary') + _marked_table())
    parsed = _read(source)
    assert len(parsed.tables) == 2
    assert [table['table_id'] for table in parsed.tables] == [0, 1]
    assert parsed.tables[0]['end_char'] < parsed.tables[1]['start_char']
    for table in parsed.tables:
        assert parsed.text[table['start_char']:table['end_char']] == table['text']


def test_ingestion_persists_grid_and_parses_images_only_once(tmp_path):
    source = _docx(tmp_path, _p('Lead') + _marked_table())
    with patch.object(document_ingestion, 'CACHE_DIR', tmp_path / 'cache'), \
         patch.object(docx_parser, '_read_embedded_images', return_value={}) as images:
        result = document_ingestion.ingest_document(source)
        cached = document_ingestion.ingest_document(source)
    images.assert_called_once()
    assert result.tables == cached.tables
    assert len(cached.tables) == 1
    assert cached.text[cached.tables[0]['start_char']:cached.tables[0]['end_char']] == cached.tables[0]['text']
    assert document_ingestion.INGESTION_CACHE_VERSION == 'v10-image-occurrences'


def test_normalization_realigns_table_and_cell_text_not_raw_offsets(tmp_path):
    source = _docx(tmp_path, _p('Lead') + _marked_table())
    parsed = _read(source)
    table = copy.deepcopy(parsed.tables[0])
    raw = '\r\n\r\n' + parsed.text.replace(' | ', '  |  ').replace('\n', '\r\n') + '\r\n'
    table['text'] = table['text'].replace(' | ', '  |  ').replace('\n', '\r\n')
    table['start_char'] = raw.index(table['text'])
    table['end_char'] = table['start_char'] + len(table['text'])
    returned = docx_parser.DocxReadResult(raw, [table])
    with patch.object(document_ingestion, 'CACHE_DIR', tmp_path / 'cache'), \
         patch.object(document_ingestion, '_read_with_xml', return_value=returned):
        result = document_ingestion.ingest_document(source)
    normalized_table = result.tables[0]
    assert result.text[normalized_table['start_char']:normalized_table['end_char']] == normalized_table['text']
    assert '\r' not in normalized_table['text']
    assert '  ' not in normalized_table['text']


def test_legacy_string_backend_and_document_constructor_stay_compatible(tmp_path):
    source = _docx(tmp_path, _p('Unused'))
    with patch.object(document_ingestion, 'CACHE_DIR', tmp_path / 'cache'), \
         patch.object(document_ingestion, '_read_with_xml', return_value='Legacy text'):
        result = document_ingestion.ingest_document(source)
    assert result.text == 'Legacy text'
    assert result.tables == []
    assert document_ingestion.IngestedDocument('A', 'xml', []).tables == []


@pytest.mark.parametrize('corruption', ['offset', 'columns', 'shape', 'missing', 'unhashable-merge',
                                        'nonboolean-header', 'overlap', 'cell-text', 'row-shape'])
def test_bad_cached_bindings_trigger_reparse_not_silent_bad_metadata(tmp_path, corruption):
    source = _docx(tmp_path, _marked_table())
    with patch.object(document_ingestion, 'CACHE_DIR', tmp_path / 'cache'):
        original = document_ingestion.ingest_document(source)
        cache_path = document_ingestion._cache_path(source)
        payload = json.loads(cache_path.read_text(encoding='utf-8'))
        if corruption == 'offset':
            payload['tables'][0]['start_char'] += 1
        elif corruption == 'columns':
            payload['tables'][0]['rows'][0]['cells'][0]['column_index'] = -1
        elif corruption == 'shape':
            payload['tables'] = {'not': 'a-list'}
        elif corruption == 'unhashable-merge':
            payload['tables'][0]['rows'][0]['cells'][0]['vertical_merge'] = []
        elif corruption == 'nonboolean-header':
            payload['tables'][0]['rows'][0]['is_header'] = 'true'
        elif corruption == 'overlap':
            payload['tables'][0]['rows'][0]['cells'][1]['column_index'] = 0
        elif corruption == 'cell-text':
            payload['tables'][0]['rows'][0]['cells'][0]['text'] = 'Wrong cell'
        elif corruption == 'row-shape':
            payload['tables'][0]['rows'][0] = []
        else:
            del payload['tables']
        cache_path.write_text(json.dumps(payload), encoding='utf-8')
        with patch.object(document_ingestion, '_read_with_xml', wraps=document_ingestion._read_with_xml) as reader:
            result = document_ingestion.ingest_document(source)
        reader.assert_called_once()
    assert result.tables == original.tables


def test_invalid_backend_table_mapping_is_dropped_with_warning(tmp_path):
    source = _docx(tmp_path, _marked_table())
    parsed = _read(source)
    parsed.tables[0]['end_char'] += 2
    with patch.object(document_ingestion, 'CACHE_DIR', tmp_path / 'cache'), \
         patch.object(document_ingestion, '_read_with_xml', return_value=parsed):
        result = document_ingestion.ingest_document(source)
    assert not result.tables
    assert any('table-grid' in warning for warning in result.warnings)


@pytest.mark.parametrize('row_properties, warning', [
    ('<w:gridBefore w:val="bad"/>', 'invalid-grid-before'),
    ('<w:gridAfter w:val="-1"/>', 'invalid-grid-after'),
    ('<w:tblHeader w:val="maybe"/>', 'invalid-header-marker'),
])
def test_invalid_row_properties_are_warned_without_invented_roles(tmp_path, row_properties, warning):
    table = _table(_row(_cell('A'), _cell('B'), properties=row_properties))
    parsed = _read(_docx(tmp_path, table)).tables[0]
    assert any(item.startswith(warning) for item in parsed['warnings'])
    assert parsed['rows'][0]['is_header'] is False


def test_sdt_wrappers_keep_table_row_cell_boundaries(tmp_path):
    row = _row(_cell('Header'), _cell('Value'), properties='<w:tblHeader/>')
    table = '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:sdt><w:sdtContent>' + row + '</w:sdtContent></w:sdt></w:tbl>'
    source = _docx(tmp_path, '<w:sdt><w:sdtContent>' + table + '</w:sdtContent></w:sdt>')
    parsed = _read(source).tables[0]
    assert len(parsed['rows']) == 1
    assert len(parsed['rows'][0]['cells']) == 2
    assert not parsed['warnings']


def test_vertical_continuation_cannot_borrow_across_tables(tmp_path):
    first = _table(_row(_cell('A', '<w:vMerge w:val="restart"/>'), _cell('B'), properties='<w:tblHeader/>'))
    second = _table(_row(_cell('C', '<w:vMerge/>'), _cell('D'), properties='<w:tblHeader/>'))
    parsed = _read(_docx(tmp_path, first + second))
    assert not parsed.tables[0]['warnings']
    assert any(item.startswith('orphan-vertical-merge') for item in parsed.tables[1]['warnings'])


def test_repeated_headers_stay_separate_from_data_with_warning(tmp_path):
    table = _table(
        _row(_cell('A'), _cell('B'), properties='<w:tblHeader/>'),
        _row(_cell('C'), _cell('D')),
        _row(_cell('E'), _cell('F'), properties='<w:tblHeader/>'),
    )
    parsed = _read(_docx(tmp_path, table)).tables[0]
    assert [row['is_header'] for row in parsed['rows']] == [True, False, True]
    assert any(item.startswith('non-leading-header-row') for item in parsed['warnings'])


def test_unsupported_row_wrapper_preserves_text_but_warns(tmp_path):
    row = _row(_cell('A'), _cell('B'), properties='<w:tblHeader/>')
    table = '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:ins>' + row + '</w:ins></w:tbl>'
    parsed = _read(_docx(tmp_path, table))
    assert 'A | B' in parsed.text
    assert any(item.startswith('unsupported-table-wrapper') for item in parsed.tables[0]['warnings'])


def test_unsupported_cell_wrapper_preserves_text_but_warns(tmp_path):
    cell = '<w:tc><w:ins>' + _p('Wrapped cell') + '</w:ins></w:tc>'
    table = _table(_row(cell, _cell('B'), properties='<w:tblHeader/>'))
    parsed = _read(_docx(tmp_path, table)).tables[0]
    assert parsed['rows'][0]['cells'][0]['text'] == 'Wrapped cell'
    assert any(item.startswith('unsupported-cell-wrapper') for item in parsed['warnings'])
