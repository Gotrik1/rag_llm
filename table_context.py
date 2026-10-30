"""Source-bound table units; no inferred business roles or model instructions.

Checksums detect accidental rebinding inside the pipeline, not source authenticity.
All strings remain untrusted document data. Unmarked headers stay candidates.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable


TABLE_UNIT_PREFIX = '[TABLE_ROW_V1] '
TABLE_SCHEMA = 'table-grid-v1'
MAX_TABLE_UNITS = 8192
MAX_TABLE_SERIALIZED_CHARS = 16 * 1024 * 1024


class TableContextError(ValueError):
    """Invalid or over-budget structural table data that must not be reduced to an unbound bare cell."""
    pass


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _integer(value) -> bool:
    return type(value) is int and value >= 0


def _cell_shape(cell, *, fragment=False) -> bool:
    if not isinstance(cell, dict):
        return False
    allowed = {'row', 'column', 'span', 'text', 'merge_origin', 'merge_unresolved'}
    if fragment:
        allowed |= {'start', 'end', 'total_chars'}
    if set(cell) - allowed or ('merge_unresolved' in cell and type(cell['merge_unresolved']) is not bool):
        return False
    if not all(_integer(cell.get(k)) for k in ('row', 'column', 'span')) or cell['span'] < 1:
        return False
    if not isinstance(cell.get('text'), str):
        return False
    if fragment:
        if not all(_integer(cell.get(k)) for k in ('start', 'end', 'total_chars')):
            return False
        if not (cell['start'] <= cell['end'] <= cell['total_chars']):
            return False
        if len(cell['text']) != cell['end'] - cell['start']:
            return False
    origin = cell.get('merge_origin')
    return origin is None or (_cell_shape(origin) and origin['column'] == cell['column']
                              and origin['span'] == cell['span'] and origin['row'] < cell['row'])


def parse_table_unit(text: str) -> dict | None:
    """Validate the serialized table envelope, checksum and native cell/header geometry; invalid units return None."""
    if not isinstance(text, str) or not text.startswith(TABLE_UNIT_PREFIX) or '\n' in text.strip():
        return None
    try:
        envelope = json.loads(text[len(TABLE_UNIT_PREFIX):])
        if not isinstance(envelope, dict) or set(envelope) != {'payload', 'sha256'}:
            return None
        payload = envelope['payload']
        if not isinstance(payload, dict) or set(payload) != {
            'schema', 'table_id', 'row', 'row_kind', 'header_status', 'headers', 'cells', 'context', 'warnings'
        }:
            return None
        if envelope['sha256'] != hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest():
            return None
        if payload['schema'] != TABLE_SCHEMA or not re.fullmatch(r'[0-9a-f]{64}', str(payload['table_id'])):
            return None
        if not _integer(payload['row']) or payload['row_kind'] not in {'header', 'body'}:
            return None
        if payload['header_status'] not in {'explicit', 'candidate', 'unknown'}:
            return None
        header_fragments = False
        for key, fragment in (('headers', False), ('cells', True), ('context', True)):
            if not isinstance(payload[key], list):
                return None
            intervals = {}
            for cell in payload[key]:
                # Whole headers keep their original shape. A bounded projected
                # header is an exact source slice, never a shortened label that
                # can masquerade as the complete column definition.
                is_header_fragment = key == 'headers' and isinstance(cell, dict) and bool(
                    {'start', 'end', 'total_chars'} & set(cell))
                if not _cell_shape(cell, fragment=fragment or is_header_fragment):
                    return None
                if is_header_fragment:
                    if cell['start'] == cell['end']:
                        return None
                    header_fragments = True
                row_intervals = intervals.setdefault(cell['row'], [])
                if any(cell['column'] < end and start < cell['column'] + cell['span'] for start, end in row_intervals):
                    return None
                row_intervals.append((cell['column'], cell['column'] + cell['span']))
        if not payload['cells'] or any(x['row'] != payload['row'] for x in payload['cells'] + payload['context']):
            return None
        if any(x['row'] > payload['row'] for x in payload['headers']):
            return None
        if not isinstance(payload['warnings'], list) or not all(isinstance(x, str) for x in payload['warnings']):
            return None
        if header_fragments and (payload['header_status'] != 'unknown' or
                                 'header-view-fragmented-over-budget' not in payload['warnings']):
            return None
        return payload
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def serialize_table_unit(payload: dict) -> str:
    """Encode one structural row with a checksum and reject payloads that fail the same parser contract."""
    envelope = {'payload': payload, 'sha256': hashlib.sha256(_canonical(payload).encode('utf-8')).hexdigest()}
    text = TABLE_UNIT_PREFIX + _canonical(envelope)
    if parse_table_unit(text) is None:
        raise TableContextError('Invalid structural table unit')
    return text


def table_unit_texts(text: str) -> list[str]:
    # JSON permits literal Unicode paragraph separators inside quoted strings.
    # Only physical LF/CRLF delimit our records, never str.splitlines().
    """Extract validated physical-line table units without splitting JSON string content on Unicode separators."""
    return [line.removesuffix('\r') for line in text.split('\n')
            if parse_table_unit(line.removesuffix('\r')) is not None]


def map_table_prose(text: str, transform_plain: Callable[[str], str]) -> str:
    """Apply legacy cleanup only outside serialized source units.

    Standalone malformed units fail closed. Unit bytes and physical line
    boundaries survive even when the old prose transform strips whitespace.
    """
    if '[TABLE_ROW_V1]' not in text:
        return transform_plain(text)
    parts, pending, found = [], [], False

    def flush():
        if not pending:
            return
        raw = ''.join(pending)
        value = transform_plain(raw)
        if raw.startswith(('\n', '\r\n')) and value and not value.startswith(('\n', '\r\n')):
            value = '\n' + value
        if raw.endswith('\n') and value and not value.endswith('\n'):
            value += '\n'
        parts.append(value)
        pending.clear()

    for line in re.findall(r'[^\n]*\n|[^\n]+$', text):
        if line.strip().startswith('[TABLE_ROW_V1]'):
            if parse_table_unit(line.strip()) is None:
                raise TableContextError('Invalid table unit cannot be normalized as prose')
            flush()
            parts.append(line)
            found = True
        else:
            pending.append(line)
    flush()
    return ''.join(parts) if found else transform_plain(text)


def formula_source_segments(text: str) -> list[str]:
    """Decode only validated units, never stitch cells or attest a cut cell.

    Consumers must inspect returned segments independently. Joining them would
    fabricate cross-cell equations. This is not a replacement for source text
    in quotations, reader/judge input or user-facing citations.
    """
    if TABLE_UNIT_PREFIX.strip() not in text:
        return [text]
    result, ordinary, cells = [], [], {}

    def collect(unit, cell):
        key = (unit['table_id'], cell['row'], cell['column'], cell['span'], cell.get('total_chars', len(cell['text'])))
        cells.setdefault(key, []).append((cell.get('start', 0), cell.get('end', len(cell['text'])), cell['text']))
    for line in text.split('\n'):
        physical = line.removesuffix('\r')
        if physical.startswith(TABLE_UNIT_PREFIX.strip()):
            if ordinary:
                result.append('\n'.join(ordinary))
                ordinary = []
            unit = parse_table_unit(physical)
            if unit is None:
                raise TableContextError('Invalid table unit cannot attest a formula')
            for cell in unit['headers']:
                collect(unit, cell)
            for cell in unit['cells'] + unit['context']:
                collect(unit, cell)
                if cell.get('merge_origin'):
                    collect(unit, cell['merge_origin'])
        else:
            ordinary.append(line)
    if ordinary:
        result.append('\n'.join(ordinary))
    for key, fragments in cells.items():
        assembled, valid = '', True
        for start, end, value in sorted(fragments):
            if start > len(assembled) or assembled[start:min(end, len(assembled))] != value[:max(0, min(end, len(assembled)) - start)]:
                valid = False
                break
            if end > len(assembled):
                assembled += value[len(assembled) - start:]
        if valid and len(assembled) == key[-1]:
            # This reconstructs one exact source cell only. Other tables,
            # revisions, rows and columns cannot complete its missing suffix.
            result.append(assembled)
    return result


def _validated_rows(record: dict) -> list[dict]:
    rows = record.get('rows')
    if not isinstance(rows, list) or not rows:
        raise TableContextError('Table has no structural rows')
    result, previous_row, merge_origins = [], -1, {}
    for row in rows:
        index = row.get('row_index')
        if not _integer(index) or index <= previous_row or type(row.get('is_header')) is not bool:
            raise TableContextError('Invalid table row coordinates')
        cells, end = [], 0
        if not isinstance(row.get('cells'), list):
            raise TableContextError('Invalid table cells')
        next_origins = {}
        for raw in row['cells']:
            column, span, value = raw.get('column_index'), raw.get('column_span'), raw.get('text')
            if not _integer(column) or not _integer(span) or span < 1 or column < end or not isinstance(value, str):
                raise TableContextError('Invalid or overlapping table cells')
            merge = raw.get('vertical_merge')
            if merge not in {None, 'restart', 'continue'}:
                raise TableContextError('Invalid vertical merge')
            cell = {'row': index, 'column': column, 'span': span, 'text': value}
            key = (column, span)
            if merge == 'restart':
                next_origins[key] = dict(cell)
            elif merge == 'continue':
                origin = merge_origins.get(key)
                if origin is not None:
                    cell['merge_origin'] = origin
                    next_origins[key] = origin
                else:
                    cell['merge_unresolved'] = True
            cells.append(cell)
            end = column + span
        result.append({'row_index': index, 'is_header': row['is_header'], 'cells': cells})
        previous_row, merge_origins = index, next_origins
    return result


def _fragment(cell: dict, start=0, end=None) -> dict:
    value = cell['text']
    end = len(value) if end is None else end
    return {**cell, 'text': value[start:end], 'start': start, 'end': end, 'total_chars': len(value)}


def _header_fragment(cell: dict, start: int, end: int) -> dict:
    """Slice a header projection without rebasing its native source offsets."""
    origin = cell.get('start', 0)
    return {**cell, 'text': cell['text'][start:end], 'start': origin + start,
            'end': origin + end, 'total_chars': cell.get('total_chars', len(cell['text']))}


def build_table_units(record: dict, *, source_identity: str, tokenizer: Callable,
                      max_tokens: int, max_chars: int = 2400) -> list[str]:
    """Bound each row/cell continuation with the same exact source header view."""
    if not source_identity or max_tokens < 1 or max_chars < 1:
        raise TableContextError('Invalid table context limits/source identity')
    rows = _validated_rows(record)
    digest = hashlib.sha256(_canonical(record).encode('utf-8')).hexdigest()
    table_id = hashlib.sha256(f'{source_identity}\0{digest}'.encode('utf-8')).hexdigest()
    headers, explicit = [], True
    for row in rows:
        if not row['is_header']:
            break
        headers.extend(row['cells'])
    if not headers:
        explicit = False
        headers = rows[0]['cells']
    warnings = list(record.get('warnings') or [])
    if not all(isinstance(x, str) for x in warnings):
        raise TableContextError('Invalid table warnings')
    if any(row['is_header'] for row in rows[1:]) and not rows[0]['is_header']:
        warnings.append('non-leading-header-row')
    ambiguous = [warning for warning in warnings if warning != 'no-explicit-header']
    if any(cell.get('merge_unresolved') for row in rows for cell in row['cells']):
        ambiguous.append('orphan-vertical-merge')
    status = 'unknown' if ambiguous else ('explicit' if explicit else 'candidate')
    if not explicit:
        warnings.append('first-row-is-an-unverified-header-candidate')

    def fits(value: str) -> bool:
        return len(value) <= max_chars and len(tokenizer(value)) <= max_tokens

    result, serialized_chars = [], 0
    source_chars = max(len(record.get('text', '')), sum(len(cell['text']) for row in rows for cell in row['cells']))
    expansion_budget = min(MAX_TABLE_SERIALIZED_CHARS, max(65536, source_chars * 128))

    def append_unit(value):
        nonlocal serialized_chars
        if len(result) >= MAX_TABLE_UNITS or serialized_chars + len(value) > expansion_budget:
            raise TableContextError('Table context expansion exceeds bounded ingestion budget; no partial table was published')
        result.append(value)
        serialized_chars += len(value)

    for row in rows:
        if not row['cells']:
            continue
        row_headers = [dict(x) for x in headers if x['row'] < row['row_index']]
        base = {'schema': TABLE_SCHEMA, 'table_id': table_id, 'row': row['row_index'],
                'row_kind': 'header' if row['is_header'] else 'body', 'header_status': status,
                'headers': row_headers, 'cells': [], 'context': [], 'warnings': warnings}
        whole = serialize_table_unit({**base, 'cells': [_fragment(x) for x in row['cells']]})
        if fits(whole):
            append_unit(whole)
            continue
        # Blank native cells are preserved in the source row units. Repeating
        # all of them as headings carries no additional text and can otherwise
        # turn Word layout grids into hundreds of one-cell context packets.
        if any(not header['text'] for header in row_headers):
            base = {**base, 'headers': [header for header in row_headers if header['text']],
                    'warnings': [*warnings, 'blank-header-cells-not-repeated-in-projected-view']}
        def emit_cell(cell, cell_base, context, *, start=0, allow_projection=True):
            # A wide table need not repeat unrelated column headings in every
            # cell continuation. Project only when the full literal header view
            # itself leaves no content budget. Source row/column/span stay exact;
            # all header cells (including blanks) also have their own row units.
            def project_headers():
                selected = [header for header in row_headers
                            if header['column'] < cell['column'] + cell['span']
                            and cell['column'] < header['column'] + header['span']]
                projected = [header for header in selected if header['text']]
                notes = [*warnings, 'header-view-projected-to-cell-columns']
                if len(projected) != len(selected):
                    notes.append('blank-header-cells-not-repeated-in-projected-view')
                return {**base, 'headers': projected, 'warnings': notes}

            first = True
            while first or start < len(cell['text']):
                first = False
                minimum = _fragment(cell, start, min(start + 1, len(cell['text'])))
                # Reserve real content, not just an empty fragment. Later
                # offsets/escaping can cost more than the initial prefix.
                while context and not fits(serialize_table_unit({**cell_base, 'cells': [minimum], 'context': context})):
                    context.pop()
                if not fits(serialize_table_unit({**cell_base, 'cells': [minimum], 'context': context})):
                    if allow_projection:
                        cell_base = project_headers()
                    if not fits(serialize_table_unit({**cell_base, 'cells': [minimum], 'context': []})):
                        # A merged body cell can overlap many separate headings.
                        # Keep every heading in bounded views, repeating the same
                        # source cell. An oversized single heading is itself
                        # sliced with exact offsets; partial text is never
                        # presented as a complete/verified column definition.
                        partial_base = {**cell_base, 'header_status': 'unknown',
                            'warnings': list(dict.fromkeys([
                                *cell_base['warnings'], 'header-view-partial-over-budget']))}
                        reserved = _fragment(cell, start, min(start + 128, len(cell['text'])))
                        groups, group = [], []
                        for header in cell_base['headers']:
                            trial = [*group, header]
                            if not fits(serialize_table_unit({**partial_base, 'headers': trial, 'cells': [reserved]})):
                                if group:
                                    groups.append(group)
                                    group = []
                            if fits(serialize_table_unit({**partial_base, 'headers': [header], 'cells': [minimum]})):
                                group.append(header)
                                continue
                            if group:
                                groups.append(group)
                                group = []
                            # Both axes stay literal and source-bound. Header
                            # parts are separate views of the same body cell,
                            # not new independent rows or inferred business roles.
                            fragment_base = {**partial_base, 'warnings': list(dict.fromkeys([
                                *partial_base['warnings'], 'header-view-fragmented-over-budget']))}
                            header_start = 0
                            while header_start < len(header['text']):
                                seed = _header_fragment(header, header_start, header_start + 1)
                                seed_text = serialize_table_unit({**fragment_base, 'headers': [seed], 'cells': [minimum]})
                                if not fits(seed_text):
                                    raise TableContextError('Table header/context exceeds chunk budget; no bare cell was published')
                                reserve = reserved
                                if not fits(serialize_table_unit({**fragment_base, 'headers': [seed], 'cells': [reserve]})):
                                    reserve = minimum
                                best, header_end = seed, header_start + 1
                                low, high = header_end + 1, len(header['text'])
                                while low <= high:
                                    middle = (low + high) // 2
                                    candidate = _header_fragment(header, header_start, middle)
                                    text = serialize_table_unit({**fragment_base, 'headers': [candidate], 'cells': [reserve]})
                                    if fits(text):
                                        best, header_end, low = candidate, middle, middle + 1
                                    else:
                                        high = middle - 1
                                groups.append([best])
                                header_start = header_end
                        if group:
                            groups.append(group)
                        if not groups:
                            raise TableContextError('Table header/context exceeds chunk budget; no bare cell was published')
                        for headers_part in groups:
                            part_base = {**partial_base, 'headers': headers_part}
                            if any('start' in header for header in headers_part):
                                part_base['warnings'] = list(dict.fromkeys([
                                    *part_base['warnings'], 'header-view-fragmented-over-budget']))
                            emit_cell(cell, part_base, [],
                                      start=start, allow_projection=False)
                        return
                # Token counts are not monotone in substring length (BPE can
                # merge a longer suffix into fewer tokens). Seed the proven
                # minimum so a failed binary probe cannot discard valid data.
                minimum_end = min(start + 1, len(cell['text']))
                best = (serialize_table_unit({**cell_base, 'cells': [minimum], 'context': context}), minimum_end)
                low, high = minimum_end + 1, len(cell['text'])
                while low <= high:
                    middle = (low + high) // 2
                    candidate = serialize_table_unit({**cell_base, 'cells': [_fragment(cell, start, middle)], 'context': context})
                    if fits(candidate):
                        best, low = (candidate, middle), middle + 1
                    else:
                        high = middle - 1
                if best is None or (best[1] == start and start < len(cell['text'])):
                    raise TableContextError('Table context leaves no room for cell content')
                append_unit(best[0])
                start = best[1]
        pending = []
        for cell in row['cells']:
            candidate = serialize_table_unit({**base, 'cells': [*pending, _fragment(cell)]})
            if fits(candidate):
                pending.append(_fragment(cell))
                continue
            if pending:
                append_unit(serialize_table_unit({**base, 'cells': pending}))
                pending = []
            if fits(serialize_table_unit({**base, 'cells': [_fragment(cell)]})):
                pending.append(_fragment(cell))
                continue
            # Optional other cells keep literal row context without assigning a key/actor role.
            context = [_fragment(x) for x in row['cells'] if x is not cell and 0 < len(x['text']) <= 128]
            emit_cell(cell, base, context)
        if pending:
            append_unit(serialize_table_unit({**base, 'cells': pending}))
    return result


def protect_table_regions(text: str, tables: list[dict]) -> tuple[str, dict[str, dict]]:
    """Hide verified raw table ranges before heuristic section detection."""
    parts, slots, offset = [], {}, 0
    for ordinal, record in enumerate(sorted(tables, key=lambda x: x.get('start_char', -1))):
        start, end = record.get('start_char'), record.get('end_char')
        if not _integer(start) or not _integer(end) or not offset <= start < end <= len(text):
            raise TableContextError('Invalid/overlapping table source offsets')
        if text[start:end] != record.get('text'):
            raise TableContextError('Table region does not match its source text')
        marker = '[RAG_TABLE_SLOT_' + hashlib.sha256(f'{ordinal}\0{text[start:end]}'.encode('utf-8')).hexdigest() + ']'
        if marker in text or marker in slots:
            raise TableContextError('Table placeholder collision')
        parts.extend([text[offset:start], '\n\n' + marker + '\n\n'])
        slots[marker] = record
        offset = end
    parts.append(text[offset:])
    return ''.join(parts), slots


def _markdown_cells(line: str) -> list[str]:
    value = line.strip()
    if value.startswith('|'):
        value = value[1:]
    if value.endswith('|') and not value.endswith('\\|'):
        value = value[:-1]
    cells, current, index, in_code = [], [], 0, False
    while index < len(value):
        char = value[index]
        if char == '\\' and index + 1 < len(value) and value[index + 1] == '|':
            current.append('|')
            index += 2
            continue
        if char == '`':
            in_code = not in_code
        if char == '|' and not in_code:
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append(''.join(current).strip())
    return cells


def markdown_tables(text: str) -> list[dict]:
    """Locate explicit Markdown tables outside code fences, retaining source offsets and row-width warnings."""
    lines = text.splitlines(keepends=True)
    positions, position = [], 0
    for line in lines:
        positions.append(position)
        position += len(line)
    records, index, fence = [], 0, None
    while index < len(lines):
        stripped = lines[index].strip()
        match = re.match(r'^(`{3,}|~{3,})', stripped)
        if match:
            if fence is None:
                fence = match.group(1)
            elif match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            index += 1
            continue
        if fence or index + 1 >= len(lines) or '|' not in lines[index]:
            index += 1
            continue
        header, separator = _markdown_cells(lines[index]), _markdown_cells(lines[index + 1])
        if len(header) < 2 or len(separator) != len(header) or not all(re.fullmatch(r':?-{3,}:?', x) for x in separator):
            index += 1
            continue
        start, end, values, warnings = index, index + 2, [header], []
        while end < len(lines) and lines[end].strip() and '|' in lines[end]:
            cells = _markdown_cells(lines[end])
            if len(cells) != len(header):
                warnings.append('markdown-row-width-mismatch')
            values.append(cells)
            end += 1
        begin_char = positions[start]
        end_char = positions[end] if end < len(lines) else len(text)
        records.append({'table_id': len(records), 'start_char': begin_char, 'end_char': end_char,
                        'text': text[begin_char:end_char], 'warnings': warnings, 'rows': [
                            {'row_index': row_index, 'is_header': row_index == 0, 'cells': [
                                {'column_index': c, 'column_span': 1, 'text': value, 'vertical_merge': None}
                                for c, value in enumerate(row)]} for row_index, row in enumerate(values)]})
        index = end
    return records
