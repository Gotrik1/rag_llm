"""Lossless compact transport for the ordinary claim review, opt-in by caller.

This changes no verdict, source binding or publication policy. The original
validator must still assess every expanded model row against its native packet.
"""
from __future__ import annotations
import json
import re

VERSION = 'ordinary-claim-matrix-v1'
COLUMNS = ('claim_id', 'support', 'passage_ids', 'source_binding', 'material_conditions')


def compact_review_prompt(prompt: str) -> str:
    """Replace explicit claim objects with a bounded column matrix while retaining the original review requirements."""
    head, tail = prompt.rsplit('\nJSON_SCHEMA:\n', 1)
    schema_text, data_text = tail.split('\nДАННЫЕ:\n', 1)
    schema = json.loads(schema_text)
    data = json.loads(data_text)
    original = schema['properties']['claims']['items']
    if set(original['properties']) != set(COLUMNS) or set(original['required']) != set(COLUMNS):
        raise ValueError('Unsupported ordinary claim schema')
    claims = data['answer_claims']
    if not isinstance(claims, list) or not 1 <= len(claims) <= 100:
        raise ValueError('Unbounded claim matrix')
    ids = [row['claim_id'] for row in claims]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate program claim ID')
    fields = [original['properties'][name] for name in COLUMNS]
    # IDs are a closed program-owned set. The server still checks exact
    # coverage/uniqueness, source/PID ownership and every semantic verdict.
    fields[0] = {**fields[0], 'enum': ids}
    schema['properties']['claims'].update(minItems=len(claims), maxItems=len(claims),
        items={'type': 'array', 'prefixItems': fields, 'items': False,
               'minItems': len(COLUMNS), 'maxItems': len(COLUMNS)})
    instruction = ('\nКОМПАКТНЫЙ ПРОТОКОЛ ' + VERSION + ': в claims верни массив строк-массивов. '
        'Порядок пяти полей каждой строки строго: ' + ', '.join(COLUMNS) + '. '
        'У каждой строки должны быть все пять значений; passage_ids остаётся массивом исходных PID. '
        'Не возвращай имена полей внутри строк claims. Остальные поля ответа сохраняют исходную схему. '
        'Это только формат передачи: оцени каждый claim и все его условия полностью. '
        'Верни компактный JSON без отступов и пояснений.\n')
    return (head + instruction + '\nJSON_SCHEMA:\n' +
        json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(',', ':')) +
        '\nДАННЫЕ:\n' + data_text)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def decode_compact_review(response: str) -> dict:
    """Expand only complete unique matrix rows with explicit verdicts; semantic validation remains the caller's responsibility."""
    if not isinstance(response, str) or len(response) > 1024 * 1024:
        raise ValueError('Invalid review transport')
    text = response.strip()
    fence = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.I)
    value = json.loads(fence.group(1) if fence else text, object_pairs_hook=_unique_object)
    if (not isinstance(value, dict) or set(value) not in
            ({'claims'}, {'claims', 'family_coverage'}) or not isinstance(value['claims'], list)
            or not 1 <= len(value['claims']) <= 100):
        raise ValueError('Invalid claim matrix envelope')
    rows = []
    ids = set()
    for row in value['claims']:
        if (not isinstance(row, list) or len(row) != len(COLUMNS)
                or not isinstance(row[0], str) or not re.fullmatch(r'C[1-9]\d*', row[0])
                or row[0] in ids or row[1] not in ('supported', 'unsupported', 'contradicted', 'uncertain')
                or not isinstance(row[2], list) or len(row[2]) > 32
                or any(not isinstance(pid, str) for pid in row[2])
                or row[3] not in ('exact_target', 'other_target', 'unassessed')
                or row[4] not in ('complete', 'incomplete', 'unassessed')):
            raise ValueError('Invalid explicit claim verdict row')
        ids.add(row[0])
        rows.append(dict(zip(COLUMNS, row, strict=True)))
    return {**value, 'claims': rows}


def decode_review_response(response: str) -> dict:
    """Accept the former explicit-object wire without relaxing its validator.

    Some providers still return the old schema. No field is invented here;
    mixed formats, duplicate keys and malformed matrices remain errors.
    """
    if not isinstance(response, str) or len(response) > 1024 * 1024:
        raise ValueError('Invalid review transport')
    text = response.strip()
    fence = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', text, re.I)
    value = json.loads(fence.group(1) if fence else text, object_pairs_hook=_unique_object)
    if (isinstance(value, dict) and isinstance(value.get('claims'), list)
            and value['claims'] and all(isinstance(row, dict) for row in value['claims'])):
        return value  # Original publication validator owns the unchanged object schema.
    return decode_compact_review(text)
