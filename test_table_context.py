import json
import unittest

from table_context import (
    TableContextError, build_table_units, markdown_tables, parse_table_unit,
    protect_table_regions, serialize_table_unit, formula_source_segments,
)


def sample_table():
    text = '[TABLE]\nSymbol | Data source | Storage\nX | Organization Z | Manual entry'
    return {'table_id': 0, 'text': text, 'start_char': 0, 'end_char': len(text), 'warnings': [], 'rows': [
        {'row_index': 0, 'is_header': True, 'cells': [
            {'column_index': i, 'column_span': 1, 'text': value, 'vertical_merge': None}
            for i, value in enumerate(['Symbol', 'Data source', 'Storage'])]},
        {'row_index': 1, 'is_header': False, 'cells': [
            {'column_index': i, 'column_span': 1, 'text': value, 'vertical_merge': None}
            for i, value in enumerate(['X', 'Organization Z', 'Manual entry'])]},
    ]}


class TableContextTests(unittest.TestCase):
    def units(self, table, **kwargs):
        return build_table_units(table, source_identity='physical-source-A', tokenizer=list,
                                 max_tokens=2400, **kwargs)

    def test_each_row_carries_source_headers_not_inferred_actor(self):
        rows = [parse_table_unit(x) for x in self.units(sample_table())]
        row = next(x for x in rows if x['row'] == 1)
        self.assertEqual(row['header_status'], 'explicit')
        self.assertEqual([x['text'] for x in row['headers']], ['Symbol', 'Data source', 'Storage'])
        self.assertEqual([x['text'] for x in row['cells']], ['X', 'Organization Z', 'Manual entry'])
        self.assertNotIn('operator', json.dumps(row))

    def test_column_permutation_preserves_actual_header_value_pair(self):
        table = sample_table()
        for row in table['rows']:
            row['cells'] = [row['cells'][2], row['cells'][0], row['cells'][1]]
            for i, cell in enumerate(row['cells']):
                cell['column_index'] = i
        row = next(parse_table_unit(x) for x in self.units(table) if parse_table_unit(x)['row'] == 1)
        headers = {x['column']: x['text'] for x in row['headers']}
        self.assertEqual([(headers[x['column']], x['text']) for x in row['cells']],
                         [('Storage', 'Manual entry'), ('Symbol', 'X'), ('Data source', 'Organization Z')])

    def test_unmarked_first_row_is_only_candidate_and_is_not_dropped(self):
        table = sample_table()
        table['rows'][0]['is_header'] = False
        rows = [parse_table_unit(x) for x in self.units(table)]
        self.assertEqual({x['row'] for x in rows}, {0, 1})
        self.assertTrue(all(x['header_status'] == 'candidate' for x in rows))

    def test_long_cell_fragments_have_headers_coordinates_and_complete_coverage(self):
        table = sample_table()
        original = 'literal | source\n' * 500
        table['rows'][1]['cells'][1]['text'] = original
        texts = self.units(table)
        self.assertTrue(all(len(x) <= 2400 for x in texts))
        cells = [c for text in texts for c in parse_table_unit(text)['cells']
                 if c['row'] == 1 and c['column'] == 1]
        self.assertGreater(len(cells), 1)
        self.assertEqual(''.join(c['text'] for c in cells), original)
        self.assertEqual(cells[0]['start'], 0)
        self.assertEqual(cells[-1]['end'], len(original))
        for text in texts:
            unit = parse_table_unit(text)
            if unit['row'] == 1:
                self.assertTrue(any(h['text'] == 'Data source' for h in unit['headers']))

    def test_wide_header_projects_only_exact_overlapping_columns_without_cell_loss(self):
        table = sample_table()
        table['rows'][0]['cells'] = [
            {'column_index': i, 'column_span': 1, 'text': f'Exact native header {i} ' + 'h' * 60, 'vertical_merge': None}
            for i in range(24)]
        table['rows'][1]['cells'] = [
            {'column_index': i, 'column_span': 1, 'text': f'Value {i}', 'vertical_merge': None}
            for i in range(24)]
        units = [parse_table_unit(text) for text in self.units(table)]
        body = [unit for unit in units if unit['row'] == 1]
        self.assertTrue(body)
        self.assertTrue(all(unit['header_status'] == 'explicit' for unit in body))
        for unit in body:
            self.assertIn('header-view-projected-to-cell-columns', unit['warnings'])
            cell = unit['cells'][0]
            self.assertEqual([h['column'] for h in unit['headers']], [cell['column']])
            self.assertEqual(unit['headers'][0]['text'], table['rows'][0]['cells'][cell['column']]['text'])
        self.assertEqual({cell['column']: cell['text'] for unit in body for cell in unit['cells']},
                         {i: f'Value {i}' for i in range(24)})

    def test_wide_blank_candidate_view_does_not_invent_headers_or_lose_empty_cells(self):
        table = sample_table()
        table['rows'][0]['is_header'] = False
        table['rows'][0]['cells'] = [
            {'column_index': i, 'column_span': 1, 'text': 'Note:' if i == 0 else '', 'vertical_merge': None}
            for i in range(33)]
        table['rows'][1]['cells'] = [
            {'column_index': 0, 'column_span': 33, 'text': 'Exact note content', 'vertical_merge': None}]
        units = [parse_table_unit(text) for text in self.units(table, max_chars=1000)]
        first = {cell['column']: cell['text'] for unit in units if unit['row'] == 0 for cell in unit['cells']}
        self.assertEqual(first, {i: 'Note:' if i == 0 else '' for i in range(33)})
        body = next(unit for unit in units if unit['row'] == 1)
        self.assertEqual(body['header_status'], 'candidate')
        self.assertEqual([header['text'] for header in body['headers']], ['Note:'])
        self.assertIn('blank-header-cells-not-repeated-in-projected-view', body['warnings'])
        self.assertEqual(body['cells'][0]['span'], 33)

    def test_optional_context_cannot_use_all_space_before_first_cell_character(self):
        table = sample_table()
        table['rows'][1]['cells'] = [
            {'column_index': i, 'column_span': 1, 'text': 'value ' * 10, 'vertical_merge': None}
            for i in range(27)]
        units = [parse_table_unit(text) for text in self.units(table)]
        body = [unit for unit in units if unit['row'] == 1]
        self.assertEqual({cell['column'] for unit in body for cell in unit['cells']}, set(range(27)))
        for column in range(27):
            cells = [cell for unit in body for cell in unit['cells'] if cell['column'] == column]
            self.assertEqual(''.join(cell['text'] for cell in cells), 'value ' * 10)

    def test_merged_wide_cell_keeps_all_header_views_but_marks_them_partial(self):
        table = sample_table()
        table['rows'][0]['cells'] = [
            {'column_index': i, 'column_span': 1, 'text': f'Exact heading {i} ' + 'h' * 70, 'vertical_merge': None}
            for i in range(24)]
        table['rows'][1]['cells'] = [
            {'column_index': 0, 'column_span': 24, 'text': 'Native merged text', 'vertical_merge': None}]
        units = [parse_table_unit(text) for text in self.units(table)]
        body = [unit for unit in units if unit['row'] == 1]
        self.assertGreater(len(body), 1)
        self.assertTrue(all(unit['header_status'] == 'unknown' for unit in body))
        self.assertTrue(all('header-view-partial-over-budget' in unit['warnings'] for unit in body))
        self.assertEqual({header['column']: header['text'] for unit in body for header in unit['headers']},
                         {header['column_index']: header['text'] for header in table['rows'][0]['cells']})
        self.assertTrue(all(cell['span'] == 24 for unit in body for cell in unit['cells']))
        self.assertIn('Native merged text', formula_source_segments('\n'.join(self.units(table))))

    def test_nonmonotone_tokenizer_cannot_discard_a_proven_one_character_fragment(self):
        table = sample_table()
        table['rows'][0]['cells'] = table['rows'][0]['cells'][:1]
        table['rows'][1]['cells'] = [
            {'column_index': 0, 'column_span': 1, 'text': 'ABCD', 'vertical_merge': None}]

        def tokenizer(text):
            unit = parse_table_unit(text)
            if unit['row'] == 1:
                return [0] * (1 if len(unit['cells'][0]['text']) == 1 else 5000)
            return [0]

        texts = build_table_units(table, source_identity='native-source', tokenizer=tokenizer, max_tokens=2400)
        self.assertEqual(''.join(cell['text'] for text in texts for cell in parse_table_unit(text)['cells']
                                 if cell['row'] == 1), 'ABCD')

    def test_blank_layout_grid_is_packed_without_a_packet_per_empty_cell(self):
        table = sample_table()
        table['rows'] = [
            {'row_index': row, 'is_header': False, 'cells': [
                {'column_index': column, 'column_span': 1,
                 'text': 'Native label' if column == 0 else '', 'vertical_merge': None}
                for column in range(33)]} for row in range(10)]
        texts = self.units(table)
        self.assertLess(len(texts), 80)
        actual = {(cell['row'], cell['column']): cell['text']
                  for text in texts for cell in parse_table_unit(text)['cells']}
        self.assertEqual(actual, {(row, column): 'Native label' if column == 0 else ''
                                 for row in range(10) for column in range(33)})

    def test_expansion_guard_rejects_before_returning_a_partial_table(self):
        from unittest.mock import patch
        table = sample_table()
        table['rows'][1]['cells'][1]['text'] = 'Native value' * 300
        with patch('table_context.MAX_TABLE_UNITS', 1):
            with self.assertRaisesRegex(TableContextError, 'expansion exceeds'):
                self.units(table)

    def test_coordinate_or_header_tampering_rejected(self):
        text = self.units(sample_table())[-1]
        self.assertIsNotNone(parse_table_unit(text))
        self.assertIsNone(parse_table_unit(text.replace('Organization Z', 'Actor invented')))
        self.assertIsNone(parse_table_unit(text.replace('"column":1', '"column":9')))

    def test_physical_source_identity_isolates_same_filename_and_table(self):
        table = sample_table()
        a = parse_table_unit(self.units(table)[-1])['table_id']
        b = parse_table_unit(build_table_units(table, source_identity='physical-source-B',
                                              tokenizer=list, max_tokens=2400)[-1])['table_id']
        self.assertNotEqual(a, b)

    def test_table_region_protected_before_section_detection(self):
        table = sample_table()
        table['text'] += '\n[SECTION: Not a real section]'
        table['end_char'] = len(table['text'])
        text, slots = protect_table_regions(table['text'], [table])
        self.assertNotIn('[SECTION:', text)
        self.assertEqual(len(slots), 1)
        self.assertEqual(next(iter(slots.values()))['text'], table['text'])

    def test_invalid_source_offsets_fail_not_bind_to_another_table(self):
        table = sample_table()
        table['start_char'] = 1
        with self.assertRaises(TableContextError):
            protect_table_regions(table['text'], [table])

    def test_oversized_header_is_not_silently_removed(self):
        table = sample_table()
        original = 'header ' * 1000
        table['rows'][0]['cells'][1]['text'] = original
        texts = self.units(table)
        self.assertTrue(all(len(text) <= 2400 for text in texts))
        body = [parse_table_unit(text) for text in texts if parse_table_unit(text)['row'] == 1]
        fragments = [header for unit in body for header in unit['headers'] if header['column'] == 1]
        self.assertGreater(len(fragments), 1)
        reconstructed = ''
        for start, end, value in sorted({(part['start'], part['end'], part['text']) for part in fragments}):
            self.assertEqual(start, len(reconstructed))
            self.assertEqual(end - start, len(value))
            reconstructed += value
        self.assertEqual(reconstructed, original)
        for unit in body:
            if any(header['column'] == 1 for header in unit['headers']):
                self.assertEqual(unit['header_status'], 'unknown')
                self.assertIn('header-view-fragmented-over-budget', unit['warnings'])
                self.assertTrue(all(header['total_chars'] == len(original) for header in unit['headers']))
                self.assertEqual(''.join(cell['text'] for cell in unit['cells']), 'Organization Z')

    def test_markdown_explicit_separator_and_escaped_pipe(self):
        source = '# Section\n\n| Field | Value |\n| --- | --- |\n| A | source \\| literal |\n\nEnd'
        records = markdown_tables(source)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(source[record['start_char']:record['end_char']], record['text'])
        self.assertTrue(record['rows'][0]['is_header'])
        self.assertEqual(record['rows'][1]['cells'][1]['text'], 'source | literal')

    def test_fenced_code_is_not_promoted_to_table(self):
        self.assertEqual(markdown_tables('```md\nA | B\n--- | ---\nX | Y\n```'), [])

    def test_invalid_overlapping_cells_rejected(self):
        table = sample_table()
        table['rows'][1]['cells'][1]['column_index'] = 0
        with self.assertRaises(TableContextError):
            self.units(table)

    def test_whole_cell_can_be_reassembled_only_from_complete_source_bound_coverage(self):
        table = sample_table()
        original = '[MATH:R=A+B]' + ' exact source condition' * 500
        table['rows'][1]['cells'][1]['text'] = original
        units = self.units(table)
        self.assertIn(original, formula_source_segments('\n'.join(units)))
        partial = [text for text in units if parse_table_unit(text)['row'] == 1][0]
        self.assertNotIn(original, formula_source_segments(partial))
        self.assertFalse(any('[MATH:R=A+B]' in text for text in formula_source_segments(partial)))

    def test_fragments_from_different_physical_sources_cannot_complete_a_cell(self):
        table = sample_table()
        original = '[MATH:R=A+B]' + ' exact source condition' * 500
        table['rows'][1]['cells'][1]['text'] = original
        units = [text for text in self.units(table) if parse_table_unit(text)['row'] == 1]
        target = max(index for index, text in enumerate(units)
                     if any(cell['column'] == 1 for cell in parse_table_unit(text)['cells']))
        payload = parse_table_unit(units[target])
        payload['table_id'] = 'f' * 64
        mixed = list(units)
        mixed[target] = serialize_table_unit(payload)
        self.assertFalse(original in formula_source_segments('\n'.join(mixed)),
                         'A cell must not be completed from fragments of different physical sources')
