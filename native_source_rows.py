"""Source-owned table-row excerpts for definitions and calculation legends.

Only complete physical rows are offered. Candidate headers remain a literal
table quotation, never verified field labels or an applicability proof.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable

from evidence_anchors import evidence_anchor_payload
from grounding_verifier import normalize_formula
from table_model_view import table_model_view

VERSION = 'native-row-excerpts-v1'
_MATH = re.compile(r'\[MATH:\s*([^\[\]\r\n]+?)\](?:\s*\[\[MATHML_REF:[a-f0-9]{24}\]\])?')


@dataclass(frozen=True)
class NativeRow:
    """Program-owned native table-row rendering bound to E/P IDs, table alias, row number and a source digest."""
    row_id: str
    evidence_id: str
    passage_id: str
    table_alias: str
    row: int
    display: str
    source_unit_sha256: str
    header_status: str = 'explicit'

    def descriptor(self) -> dict:
        """Expose a row selector's coordinates and header status without duplicating the complete cell contents."""
        return {key: getattr(self, key) for key in
                ('row_id', 'evidence_id', 'passage_id', 'table_alias', 'row', 'header_status')}


def _whole(cell: dict) -> bool:
    return (not cell.get('merge_unresolved') and
            ('start' not in cell or cell['start'] == 0 and cell['end'] == cell['total_chars']) and
            ('merge_origin' not in cell or _whole(cell['merge_origin'])))


def _literal(text: str, render_math: Callable[[str], str]) -> str:
    # Unknown picture/OCR/transport syntax never becomes a guessed definition.
    if re.search(r'\[IMAGE_|[\x00-\x08\x0b\x0c\x0e-\x1f\u2028\u2029]', text):
        raise ValueError('unverified-row-content')
    def math(match):
        raw = match.group(1).strip()
        if '=' in raw:
            raise ValueError('equation-requires-native-formula-catalogue')
        rendered = render_math(raw)
        if normalize_formula(raw) != normalize_formula(rendered):
            raise ValueError('native-symbol-roundtrip-failed')
        return rendered
    value = _MATH.sub(math, text)
    if any(marker in value for marker in ('[MATH', 'MATHML_REF', '_sub(', '_sup(')):
        raise ValueError('unsupported-native-symbol')
    return value.replace('\r\n', ' ').replace('\r', ' ').replace('\n', ' ')


def _cell_text(cell, render_math):
    if not cell['text'] and 'merge_origin' in cell:
        origin = cell['merge_origin']
        return (_literal(origin['text'], render_math) +
                f' (объединённая ячейка: строка {origin["row"]}, колонка {origin["column"]})')
    return _literal(cell['text'], render_math) or '(пустая ячейка)'


def _display(unit, render_math):
    if unit['row_kind'] != 'body' or unit['header_status'] not in {'explicit','candidate'}:
        raise ValueError('row-needs-native-headers')
    if not all(_whole(cell) for role in ('headers', 'context', 'cells') for cell in unit[role]):
        raise ValueError('partial-or-unresolved-row')
    if unit['header_status'] == 'candidate':
        if not unit['headers'] or any(header['row'] >= unit['row'] for header in unit['headers']):
            raise ValueError('candidate-title-is-not-a-definition-row')
        # Do not turn an unmarked first row into established semantic labels.
        # The full two-row quotation keeps native column order and all words.
        headings=' | '.join(_cell_text(cell,render_math) for cell in unit['headers'])
        values=' | '.join(_cell_text(cell,render_math) for cell in unit['context']+unit['cells'])
        return ('Фрагмент исходной таблицы, порядок ячеек сохранён. '
                'Предшествующая строка: «'+headings+'». Исходная строка: «'+values+'».')
    values = []
    for role in ('context', 'cells'):
        for cell in unit[role]:
            headers = [header for header in unit['headers']
                       if header['column'] < cell['column'] + cell['span'] and
                       cell['column'] < header['column'] + header['span']]
            if not headers:
                raise ValueError('missing-native-header')
            label = ' / '.join(dict.fromkeys(_cell_text(header, render_math) for header in headers))
            prefix = 'Контекст исходной строки: ' if role == 'context' else ''
            values.append(prefix + label + ' — ' + _cell_text(cell, render_math))
    if not values:
        raise ValueError('empty-native-row')
    return '; '.join(values) + '.'


def build_native_rows(packet, render_math: Callable[[str], str]) -> tuple[NativeRow, ...]:
    """Offer only packet-bound native rows whose anchors and source geometry pass validation."""
    sources = packet.sources
    anchors = evidence_anchor_payload(sources)
    if anchors != json.loads(packet.anchors_json):
        raise ValueError('native-row-passage-binding-mismatch')
    offered = []
    for source, anchor in zip(sources, anchors):
        if not isinstance(table_model_view(source['text']), dict):
            continue
        tables, candidates = {}, {}
        for passage in anchor['passages']:
            view = table_model_view(passage['text'])
            if not isinstance(view, dict):
                continue
            for segment in view['source_segments']:
                if 'table' not in segment:
                    continue
                unit = segment['table']
                alias = tables.setdefault(unit['table_id'], f'T{len(tables)+1}')
                try:
                    display = _display(unit, render_math)
                except (ValueError, TypeError, RecursionError):
                    continue
                digest = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest()
                key = (unit['table_id'], unit['row'])
                candidates.setdefault(key, {}).setdefault(digest, (passage['passage_id'], alias, display,unit['header_status']))
        # Conflicting full versions of one physical row are not resolved by a
        # convenient first match. Identical repetitions share one source row.
        for (_, row), versions in candidates.items():
            if len(versions) != 1:
                continue
            digest, (pid, alias, display,header_status) = next(iter(versions.items()))
            offered.append(NativeRow(f'R{len(offered)+1}', source['evidence_id'], pid,
                                     alias, row, display, digest,header_status))
    return tuple(offered)
