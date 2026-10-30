"""Lossless JSON representation for a writer's evidence payload.

Only repeated, direct evidence source-name strings can be interned. Text,
provenance revisions, conditions and all other data remain in their records.
This is serialization, not evidence selection or semantic verification.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
import re
from typing import Any, Mapping


SOURCE_FIELDS = ('file', 'file_name', 'file_path')
SOURCE_INSTRUCTION = (
    '\nsource_ref в evidence указывает на source_catalogue: его file/file_name/file_path '
    'относятся к этому доказательству. Это только общие метаданные источника, не новые факты. '
    'В ответе ссылайся только на E-ID, не на source_ref.\n'
)


@dataclass(frozen=True)
class EncodedWriterPayload:
    """Serialized writer data and optional decoding instructions; interning changes transport, not source facts."""
    json_text: str
    instruction: str = ''
    interned: bool = False


def _compact(value: Any) -> str:
    # Match json.dumps' existing value semantics; change structural whitespace only.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _source_strings(item: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    # Absence, None, empty string and differently spelled paths are never normalized.
    return tuple((field, item[field]) for field in SOURCE_FIELDS
                 if field in item and isinstance(item[field], str))


def encode_writer_payload(payload: Any) -> EncodedWriterPayload:
    """Compact JSON and optionally intern repeated evidence source-name groups.

    The baseline uses standard JSON value semantics (for example, tuples become
    JSON arrays). The result carries its mode explicitly so a collision fallback
    cannot be mistaken for our encoding by the decoder. No input is mutated.
    """
    baseline = _compact(payload)
    fallback = EncodedWriterPayload(baseline)
    if not isinstance(payload, dict) or {'source_ref', 'source_catalogue'} & payload.keys():
        return fallback
    evidence = payload.get('evidence')
    if not isinstance(evidence, list) or any(
        isinstance(item, dict) and {'source_ref', 'source_catalogue'} & item.keys()
        for item in evidence
    ):
        return fallback
    groups = Counter(_source_strings(item) for item in evidence if isinstance(item, dict))
    repeated = {group for group, count in groups.items() if group and count >= 2}
    if not repeated:
        return fallback
    catalogue, refs, packed = {}, {}, []
    for item in evidence:
        group = _source_strings(item) if isinstance(item, dict) else ()
        if group not in repeated:
            packed.append(deepcopy(item))
            continue
        if group not in refs:
            ref = f'S{len(refs) + 1}'
            refs[group] = ref
            catalogue[ref] = dict(group)
        fields = dict(group)
        replacement = {}
        for key, value in item.items():
            if key in fields:
                if 'source_ref' not in replacement:
                    replacement['source_ref'] = refs[group]
            else:
                replacement[key] = deepcopy(value)
        packed.append(replacement)
    candidate = {}
    for key, value in payload.items():
        if key == 'evidence':
            candidate['source_catalogue'] = catalogue
            candidate[key] = packed
        else:
            candidate[key] = deepcopy(value)
    text = _compact(candidate)
    if len((SOURCE_INSTRUCTION + text).encode('utf-8')) >= len(baseline.encode('utf-8')):
        return fallback
    return EncodedWriterPayload(text, SOURCE_INSTRUCTION, True)


def decode_writer_payload(encoded: EncodedWriterPayload) -> Any:
    """Pure inverse in JSON value space; malformed interned references fail closed.

    For a compact-only result, reserved names retain their original meanings.
    The mode is deliberately not guessed from model/untrusted JSON keys.
    """
    if not isinstance(encoded, EncodedWriterPayload):
        raise TypeError('Expected an encoded writer payload')
    data = json.loads(encoded.json_text)
    if not encoded.interned:
        return data
    if not isinstance(data, dict) or 'source_ref' in data:
        raise ValueError('Invalid interned payload')
    catalogue, evidence = data.get('source_catalogue'), data.get('evidence')
    if not isinstance(catalogue, dict) or not catalogue or not isinstance(evidence, list):
        raise ValueError('Invalid source catalogue')
    for ref, fields in catalogue.items():
        if (not isinstance(ref, str) or re.fullmatch(r'S[1-9]\d*', ref) is None
                or not isinstance(fields, dict) or not fields
                or set(fields) - set(SOURCE_FIELDS)
                or any(not isinstance(value, str) for value in fields.values())):
            raise ValueError('Invalid source catalogue entry')
    used = Counter()
    restored = []
    for item in evidence:
        if not isinstance(item, dict) or 'source_ref' not in item:
            restored.append(item)
            continue
        ref = item['source_ref']
        if not isinstance(ref, str) or ref not in catalogue or 'source_catalogue' in item:
            raise ValueError('Unknown source reference')
        fields = catalogue[ref]
        if fields.keys() & item.keys():
            raise ValueError('Source field collision')
        used[ref] += 1
        row = {}
        for key, value in item.items():
            if key == 'source_ref':
                row.update(fields)
            else:
                row[key] = value
        restored.append(row)
    if set(used) != set(catalogue) or any(count < 2 for count in used.values()):
        raise ValueError('Unshared or unused source entry')
    return {key: restored if key == 'evidence' else value
            for key, value in data.items() if key != 'source_catalogue'}
