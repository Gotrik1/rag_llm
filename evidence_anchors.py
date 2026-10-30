"""Verbatim source passages addressable without model-generated quote text.

Anchors are local to an already ACL-filtered evidence snapshot. They are not
proof of entailment: selecting a real passage can still be semantically wrong.
No source content is dropped to satisfy a character target or output budget.
"""
from __future__ import annotations

import re
import hashlib
from bisect import bisect_right
from itertools import chain
from typing import Any, Mapping, Sequence

from table_context import parse_table_unit


TARGET_PASSAGE_CHARS = 1200
PRESERVE_PARAGRAPH_CHARS = 2400
MAX_RESOLVED_PASSAGES = 32
_EVIDENCE_ID = re.compile(r"E[1-9]\d*\Z")
_MATH_START = re.compile(r"\$\$|\\\[|\\\(|\[MATH(?::|\])|(?<!\$)\$(?!\$)", re.I)
_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*(?:\r?\n)+")
_SENTENCE_BREAK = re.compile(r"[.!?;][ \t\r\n]+")
_INVENTORY_WARNING_REASONS = ('atomic-inventory-packet-budget', 'inventory-selection-incomplete')
_MAX_INVENTORY_CARRIER_ROWS = 8
_MAX_INVENTORY_SECTION_PATH_CHARS = 512


def _inventory_warning(row):
    """A scoped warning has no source identity, including inside its values."""
    if not isinstance(row, Mapping):
        return None
    if not all(type(row.get(key)) is int and row[key] >= 0
               for key in ('required_chars', 'available_chars')):
        return None
    reason = row.get('reason')
    return {'reason': reason if reason == 'atomic-inventory-packet-budget' else 'inventory-selection-incomplete',
            'required_chars': row['required_chars'], 'available_chars': row['available_chars']}


def _inventory_budget_order(row):
    return (row['required_chars'] - row['available_chars'],
            row['required_chars'], row['available_chars'])


def _representative_inventory_warnings(rows):
    """At most one real worst-budget row per reason, never an exhaustive list."""
    worst = {}
    for row in rows:
        warning = _inventory_warning(row)
        if warning is None:
            continue
        reason = warning['reason']
        if reason not in worst or _inventory_budget_order(warning) > _inventory_budget_order(worst[reason]):
            worst[reason] = warning
    return [worst[reason] for reason in _INVENTORY_WARNING_REASONS if reason in worst]


def inventory_carrier_signals(signals):
    """Keep bounded representative locators only on the retained source carrier."""
    if not isinstance(signals, Mapping):
        return {}
    rows = signals.get('missing_inventory_sections')
    incomplete = signals.get('inventory_enumeration_incomplete') is True or bool(rows)
    if not incomplete:
        return {}
    missing = []
    for row in rows if isinstance(rows, list) else ():
        warning = _inventory_warning(row)
        path = row.get('section_path') if isinstance(row, Mapping) else None
        if (warning and isinstance(path, str) and path.strip()
                and len(path) <= _MAX_INVENTORY_SECTION_PATH_CHARS):
            candidate = {'section_path': path, **warning}
            if candidate not in missing:
                missing.append(candidate)
                missing.sort(key=lambda item: (*_inventory_budget_order(item),
                                               item['reason'], item['section_path']), reverse=True)
                del missing[_MAX_INVENTORY_CARRIER_ROWS:]
    return {'inventory_enumeration_incomplete': True, 'missing_inventory_sections': missing}


def normalize_inventory_evidence(evidence):
    """Share eight representative locators; never trim text or change its scope."""
    result = []
    remaining = _MAX_INVENTORY_CARRIER_ROWS
    for item in evidence:
        record = dict(item)
        signals = item.get('selection_signals')
        if isinstance(signals, Mapping):
            record['selection_signals'] = {
                key: value for key, value in signals.items()
                if key not in ('inventory_enumeration_incomplete', 'missing_inventory_sections')}
            inventory = inventory_carrier_signals(signals)
            if inventory:
                inventory['missing_inventory_sections'] = inventory['missing_inventory_sections'][:remaining]
                remaining -= len(inventory['missing_inventory_sections'])
            record['selection_signals'].update(inventory)
        result.append(record)
    return result


def normalize_inventory_selection_limits(limits):
    """Allow only bounded source-free representatives across selection boundaries."""
    if not isinstance(limits, Mapping):
        return {}
    rows = limits.get('inventory_enumeration_scoped_warnings')
    if limits.get('inventory_enumeration_incomplete') is not True and not rows:
        return {}
    return {'inventory_enumeration_incomplete': True,
            'inventory_enumeration_scoped_warnings':
                _representative_inventory_warnings(rows if isinstance(rows, list) else ())}


def inventory_limits_after_filter(limits, before, after):
    """A removed carrier leaves a coarse warning, never its source/path/name.

    Inputs are an already scoped selection and its retained subset. This does
    not look up documents or reattach a carrier to unrelated evidence.
    """
    result = normalize_inventory_selection_limits(limits)
    identity = lambda item: tuple(item.get(key) for key in (
        'file', 'section_path', 'document_id', 'source_id', 'revision_id', 'version_id',
        'document_version', 'content_hash', 'sha256'))
    retained = {identity(item) for item in after
                if inventory_carrier_signals(item.get('selection_signals'))}
    for item in before:
        signals = inventory_carrier_signals(item.get('selection_signals'))
        if not signals or identity(item) in retained:
            continue
        result['inventory_enumeration_incomplete'] = True
        # Read original rows: a long/omitted locator still carries a real budget
        # warning, but no source identity may cross the removed-carrier boundary.
        rows = item.get('selection_signals', {}).get('missing_inventory_sections')
        result['inventory_enumeration_scoped_warnings'] = _representative_inventory_warnings(chain(
            result.get('inventory_enumeration_scoped_warnings', []),
            rows if isinstance(rows, list) else ()))
    return result


def reconcile_evidence_families(evidence, provenance=None):
    """Recheck collection completeness after ACL filtering and budget packing.

    Reattach the single family record if its old carrier was removed, but only
    to a surviving member from the same source. Fully removed sources expose no
    family metadata. This does not certify the content's semantic completeness.
    """
    records = [dict(item) for item in evidence]
    families = {item['evidence_family']['id']:dict(item['evidence_family'])
                for item in (provenance if provenance is not None else evidence)
                if isinstance(item.get('evidence_family'),dict) and item['evidence_family'].get('id')}
    for item in records:
        item.pop('evidence_family',None)
    for identifier, family in families.items():
        expected = list(dict.fromkeys(str(path) for path in family.get('expected_children',[])))[:12]
        members = [item for item in records if item.get('file') == family.get('file') and
                   (item.get('evidence_family_id') == identifier or item.get('section_path') in expected)]
        carriers = members or [item for item in records if item.get('file') == family.get('file')]
        if not carriers:
            continue
        signatures = {item.get('section_path'): item for item in family.get('expected_members',[])
                      if isinstance(item,dict) and item.get('section_path') in expected}
        if not signatures and provenance is not None:
            # Compatibility for an older family record: the prior snapshot is
            # still available, so bind the retained locators to its revision
            # and native text rather than silently accepting another version.
            from evidence_selection import _VERSION_FIELDS
            signatures = {item.get('section_path'): {
                'revision': {field:item[field] for field in _VERSION_FIELDS if item.get(field)},
                'text_sha256':hashlib.sha256(str(item.get('text','')).strip().encode()).hexdigest(),
            } for item in provenance if item.get('file') == family.get('file')
                and item.get('section_path') in expected}
        incompatible = []
        for item in members:
            signature = signatures.get(item.get('section_path'))
            if signature is None:
                continue
            mismatch = any(item.get(field) != value for field,value in signature.get('revision',{}).items())
            if not item.get('evidence_excerpt') and signature.get('text_sha256'):
                mismatch |= hashlib.sha256(str(item.get('text','')).strip().encode()).hexdigest() != signature['text_sha256']
            if mismatch:
                incompatible.append(item.get('section_path'))
        present = {item.get('section_path') for item in members if item.get('section_path') not in incompatible}
        missing = [path for path in expected if path not in present]
        excerpted = [item.get('section_path') for item in members if item.get('evidence_excerpt')]
        total = int(family.get('total_children',len(expected)))
        complete = not missing and not excerpted and not incompatible and len(present) == total and not family.get('omitted_children_count',0)
        family.update(complete=complete,selected_children=[path for path in expected if path in present],
                      missing_children=missing,excerpted_children=excerpted,incompatible_children=list(dict.fromkeys(incompatible)))
        if not complete:
            family['incomplete_reason'] = ('revision-or-content-mismatch' if incompatible else
                                           family.get('incomplete_reason') or 'post-filter-or-payload-budget')
        else:
            family['incomplete_reason'] = None
        carriers[0]['evidence_family'] = family
    return records


def _escaped(text: str, position: int) -> bool:
    index = position - 1
    while index >= 0 and text[index] == "\\":
        index -= 1
    return (position - index - 1) % 2 == 1


def _closing(text: str, token: str, start: int) -> int | None:
    position = text.find(token, start)
    while position >= 0:
        if not _escaped(text, position):
            return position + len(token)
        position = text.find(token, position + len(token))
    return None


def _word_math_end(text: str, start: int) -> int:
    """Balance nested brackets in Word markers, including LaTeX array indices."""
    depth = 1
    for position in range(start + 1, len(text)):
        if _escaped(text, position):
            continue
        if text[position] == "[":
            depth += 1
        elif text[position] == "]":
            depth -= 1
            if depth == 0:
                return position + 1
    return len(text)  # An incomplete equation must never be split or guessed.


def _math_spans(text: str) -> list[tuple[int, int]]:
    result = []
    end = 0
    for match in _MATH_START.finditer(text):
        start, token = match.start(), match.group(0)
        if start < end or _escaped(text, start):
            continue
        if token.upper() == "[MATH:":
            end = _word_math_end(text, start)
        elif token.upper() == "[MATH]":
            close = re.search(r"\[/MATH\]", text[match.end():], re.I)
            end = match.end() + close.end() if close else len(text)
        elif token == "$$":
            end = _closing(text, "$$", match.end()) or len(text)
        elif token == "\\[":
            end = _closing(text, "\\]", match.end()) or len(text)
        elif token == "\\(":
            end = _closing(text, "\\)", match.end()) or len(text)
        else:
            close = _closing(text, "$", match.end())
            # Unpaired currency symbols must not protect the rest of a manual.
            if close is None or "\n" in text[match.end():close]:
                continue
            end = close
        result.append((start, end))
    return result


def _safe_break(position: int, spans: list[tuple[int, int]], starts: list[int]) -> bool:
    index = bisect_right(starts, position) - 1
    return index < 0 or not spans[index][0] < position < spans[index][1]


def _table_bound_units(text: str, split_plain):
    """Preserve table-line bytes and their column context in every passage.

    The unit carries its own complete provenance and header context. It can be
    larger than a passage target, like an unsplittable formula, but cannot be
    divided into apparently independent header/value quotations. Whitespace
    outside units is retained as well so concatenated anchors remain verbatim.
    """
    if '[TABLE_ROW_V1]' not in text:
        return None
    runs, pending, found = [], [], False
    # Only physical LF delimits serialized units; Unicode separators inside
    # an otherwise valid JSON cell string must not break its table context.
    for line in re.findall(r'[^\n]*\n|[^\n]+$', text):
        if line.strip().startswith('[TABLE_ROW_V1]'):
            if parse_table_unit(line.strip()) is None:
                raise ValueError('invalid-table-unit')
            if pending:
                runs.append((False, ''.join(pending)))
                pending = []
            runs.append((True, line))
            found = True
        else:
            pending.append(line)
    if pending:
        runs.append((False, ''.join(pending)))
    if not found:
        return None
    result, prefix = [], ''
    for table, run in runs:
        if not run.strip():
            if result:
                result[-1] += run
            else:
                prefix += run
            continue
        units = [run] if table else split_plain(run)
        if prefix and units:
            units[0] = prefix + units[0]
            prefix = ''
        result.extend(units)
    if prefix:
        if result:
            result[-1] += prefix
        else:
            result.append(prefix)
    return result


def _large_paragraph_units(text: str) -> list[str]:
    table_units = _table_bound_units(text, _large_paragraph_units)
    if table_units is not None:
        return table_units
    if len(text) <= PRESERVE_PARAGRAPH_CHARS:
        return [text]
    spans = _math_spans(text)
    starts = [start for start, _ in spans]
    sentence = [match.end() for match in _SENTENCE_BREAK.finditer(text) if _safe_break(match.end(), spans, starts)]
    lines = [match.end() for match in re.finditer(r"\r?\n", text) if _safe_break(match.end(), spans, starts)]
    spaces = [match.end() for match in re.finditer(r"\s+", text) if _safe_break(match.end(), spans, starts)]
    result, start = [], 0
    while len(text) - start > PRESERVE_PARAGRAPH_CHARS:
        target = start + TARGET_PASSAGE_CHARS
        lower, upper = start + TARGET_PASSAGE_CHARS // 2, start + PRESERVE_PARAGRAPH_CHARS
        cut = None
        for points in (sentence, lines, spaces):
            choices = [point for point in points if lower <= point <= upper]
            if choices:
                cut = min(choices, key=lambda point: (abs(point - target), point))
                break
        if cut is None:
            # Oversized formulas and unsplittable tokens are allowed to exceed
            # the target. Resume only after their next safe boundary.
            cut = next((point for point in spaces if point > upper), None)
        if cut is None:
            break
        result.append(text[start:cut])
        start = cut
    result.append(text[start:])
    return result


def _passages(text: str) -> list[str]:
    table_units = _table_bound_units(text, _passages)
    if table_units is not None:
        return table_units
    if not text.strip():
        return []
    spans = _math_spans(text)
    starts = [start for start, _ in spans]
    boundaries = [match.end() for match in _PARAGRAPH_BREAK.finditer(text)
                  if _safe_break(match.end(), spans, starts)]
    blocks, start = [], 0
    for end in (*boundaries, len(text)):
        if end > start:
            blocks.extend(_large_paragraph_units(text[start:end]))
        start = end
    # Group adjacent short paragraphs without rewriting separators or content.
    packed, pending = [], ""
    for block in blocks:
        if pending and len(pending) + len(block) > TARGET_PASSAGE_CHARS:
            packed.append(pending)
            pending = ""
        pending += block
    if pending:
        packed.append(pending)
    return packed


def _records(evidence: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    records, identifiers = [], set()
    for item in evidence:
        if not isinstance(item, Mapping):
            raise ValueError("Evidence records must be mappings")
        identifier = item.get("evidence_id")
        if not isinstance(identifier, str) or not _EVIDENCE_ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("Evidence IDs must be unique canonical E-number identifiers")
        text = item.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("Evidence text must be a source string")
        identifiers.add(identifier)
        records.append(item)
    return records


def build_evidence_anchors(evidence: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return stable per-EID passage IDs mapping to exact contiguous source text."""
    result = {}
    for item in _records(evidence):
        identifier = item["evidence_id"]
        for number, text in enumerate(_passages(item.get("text") or ""), 1):
            result[f"{identifier}:P{number}"] = {
                "evidence_id": identifier, "text": text,
                "file": item.get("file"), "section_path": item.get("section_path"),
            }
    return result


def evidence_anchor_payload(evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compact source payload for selecting passage IDs instead of typing quotes."""
    records = normalize_inventory_evidence(_records(evidence))
    anchors = build_evidence_anchors(records)
    return [{"evidence_id": item["evidence_id"], "file": item.get("file"), "section_path": item.get("section_path"),
             "applicability": item.get("applicability"),
             **{key:item[key] for key in ('reference_from','reference_target','evidence_family') if item.get(key) is not None},
             **({'selection_limits':{key:item['selection_signals'][key] for key in
                    ('scoped_cohort_incomplete','definition_closure_incomplete','missing_definition_symbols')
                    if item['selection_signals'].get(key)}
                    | inventory_carrier_signals(item.get('selection_signals'))}
                if any(item.get('selection_signals',{}).get(key) for key in
                       ('scoped_cohort_incomplete','definition_closure_incomplete',
                        'inventory_enumeration_incomplete','missing_inventory_sections')) else {}),
             "version_status": item.get("version_status", item.get("status")),
             "passages": [{"passage_id": passage_id, "text": passage["text"]}
                          for passage_id, passage in anchors.items() if passage["evidence_id"] == item["evidence_id"]]}
            for item in records]


def resolve_passages(
    passage_ids: Any, evidence: Sequence[Mapping[str, Any]],
) -> tuple[list[str], list[dict[str, str]]] | None:
    """Resolve a strict selection to trusted quotes; reject the entire bad list.

    Model-supplied quote strings are never consumed by this API. IDs refer only
    to the current supplied snapshot, never a global cache or another chat.
    """
    if not isinstance(passage_ids, list) or not 1 <= len(passage_ids) <= MAX_RESOLVED_PASSAGES:
        return None
    if any(not isinstance(value, str) for value in passage_ids) or len(set(passage_ids)) != len(passage_ids):
        return None
    try:
        anchors = build_evidence_anchors(evidence)
    except (ValueError, TypeError):
        return None
    if any(value not in anchors for value in passage_ids):
        return None
    identifiers, quotes = [], []
    for passage_id in passage_ids:
        passage = anchors[passage_id]
        identifier = passage["evidence_id"]
        if identifier not in identifiers:
            identifiers.append(identifier)
        quotes.append({"evidence_id": identifier, "quote": passage["text"]})
    return identifiers, quotes
