"""Optional model-facing table values; never a replacement for raw evidence.

No caller is wired to this helper. Canonical source, IDs, ACL/revision binding
and quote resolution remain owned by the caller. A verified envelope checksum
is not source authenticity. Document strings remain untrusted data.
"""
from __future__ import annotations

import re

from table_context import TABLE_UNIT_PREFIX, parse_table_unit


_MARKER_STEM = '[TABLE_ROW'
_FENCE_LINE = re.compile(r' {0,3}(?:`{3,}|~{3,})')


def table_model_view(source: str) -> str | dict:
    """Project whole verified physical rows atomically, preserving payload values.

    Only LF/CRLF are transport line boundaries; Unicode line separators may be
    literal cell data. Never strip a line to make it valid. TABLE_ROW-looking
    text outside a valid whole unit makes the entire source a plain-string
    fallback. No rows, fragments, headers or repeated context are combined.
    Any physical Markdown fence conservatively opts the whole source out,
    including unrelated fences: this is not a Markdown parser or authenticity
    check. Escaped fences inside verified JSON cell values remain cell data.
    """
    if not isinstance(source, str):
        raise TypeError('Source must be a string')
    if _MARKER_STEM not in source:
        return source

    segments, prose = [], []
    lines = source.split('\n')
    if any(_FENCE_LINE.match(line) for line in lines):
        return source
    for index, line in enumerate(lines):
        newline = '\n' if index < len(lines) - 1 else ''
        if _MARKER_STEM not in line:
            prose.append(line + newline)
            continue
        if not line.startswith(TABLE_UNIT_PREFIX):
            return source
        payload = parse_table_unit(line)
        if payload is None:
            return source
        if prose:
            text = ''.join(prose)
            if text:
                segments.append({'text': text})
            prose.clear()
        segments.append({'table': payload})
        # Retain valid JSON's exterior whitespace and original line endings as
        # prose. Validation above consumed the unchanged full line, not a trim.
        padding = re.search(r'[ \t\r]*$', line).group()
        prose.append(padding + newline)

    if prose:
        text = ''.join(prose)
        if text:
            segments.append({'text': text})
    return {'source_segments': segments} if any('table' in row for row in segments) else source
