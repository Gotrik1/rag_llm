"""Normalize service citation syntax against one supplied evidence snapshot.

This is not an entailment check. The caller must supply the same immutable,
already ACL/version-filtered context to normalization, judging and publication.
A PID reused in a different live snapshot cannot identify its former meaning.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import re
from typing import Iterable, Mapping

from evidence_anchors import _MATH_START, _closing, _word_math_end, build_evidence_anchors


_GROUP = re.compile(r'(?<!\[)\[(?P<body>[^\[\]\r\n]*)\](?!\])')
_P_MARKER = re.compile(r'(?:^|[,;])[ \t]*E[^\s,:;\[\]]*[ \t]*:[ \t]*P', re.I)
_TOKEN = re.compile(r'(?:(?P<pid>E[1-9]\d*:P[1-9]\d*)|(?P<eid>E[1-9]\d*))\Z', re.I)
_MAX_GROUP_CHARS = 4096
_MAX_GROUP_TOKENS = 32


@dataclass(frozen=True)
class CitationNormalization:
    """Canonicalized citation text plus conversion count and unresolved original references; no source IDs are invented."""
    answer: str
    # Number of P-token occurrences rewritten, not unique resulting E-IDs.
    converted_count: int
    invalid_references: tuple[str, ...] = ()


def _escaped(text: str, position: int) -> bool:
    start = position
    while start and text[start - 1] == '\\':
        start -= 1
    return bool((position - start) % 2)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    spans, fence, position = [], None, 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*?)[\r\n]*$', line)
        if fence is None:
            if marker:
                fence = position, marker[1][0], len(marker[1])
        elif marker and marker[1][0] == fence[1] and len(marker[1]) >= fence[2] and not marker[2].strip():
            spans.append((fence[0], position + len(line)))
            fence = None
        position += len(line)
    if fence is not None:
        spans.append((fence[0], len(text)))
    fence_starts = [start for start, _ in spans]

    # Balanced inline code uses a matching backtick run, not a single-character
    # replacement that could accidentally expose literal brackets as citations.
    runs = list(re.finditer(r'(?<!`)(`+)(?!`)', text))
    next_runs, last_run = {}, {}
    for match in reversed(runs):
        length = len(match[1])
        if length in last_run:
            next_runs[match.start()] = last_run[length]
        last_run[length] = match.end()
    end = 0
    for match in runs:
        number = bisect_right(fence_starts, match.start()) - 1
        if match.start() < end or number >= 0 and match.start() < spans[number][1]:
            continue
        close = next_runs.get(match.start())
        if close is not None:
            end = close
            spans.append((match.start(), end))

    protected = []
    for start, stop in sorted(spans):
        if protected and start <= protected[-1][1]:
            protected[-1] = protected[-1][0], max(stop, protected[-1][1])
        else:
            protected.append((start, stop))
    pairs = {'"': '"', "'": "'", '«': '»', '“': '”', '‘': '’'}
    # Index eligible closing quotes once. Repeated unmatched opening quotes
    # must not each rescan the whole remaining answer. Code ranges remain
    # literal even when they contain characters matching an outer quote.
    closers = {character: [] for character in pairs.values()}
    interval = 0
    for match in re.finditer(r'''["'»”’]''', text):
        offset, character = match.start(), match[0]
        while interval < len(protected) and protected[interval][1] <= offset:
            interval += 1
        if interval < len(protected) and protected[interval][0] <= offset:
            continue
        if _escaped(text, offset) or (
            character in {"'", '’'} and offset + 1 < len(text) and text[offset + 1].isalnum()
        ):
            continue
        closers[character].append(offset)

    position, interval = 0, 0
    while position < len(text):
        while interval < len(protected) and protected[interval][1] <= position:
            interval += 1
        if interval < len(protected) and protected[interval][0] <= position:
            position = protected[interval][1]
            continue
        math = _MATH_START.match(text, position)
        if math is not None and not _escaped(text, position):
            token = math[0]
            if token.upper() == '[MATH:':
                stop = _word_math_end(text, position)
            elif token.upper() == '[MATH]':
                close = re.search(r'\[/MATH\]', text[math.end():], re.I)
                stop = math.end() + close.end() if close else len(text)
            elif token in {'$$', '\\[', '\\('}:
                closing = {'$$': '$$', '\\[': '\\]', '\\(': '\\)'}[token]
                stop = _closing(text, closing, math.end()) or len(text)
            else:
                stop = _closing(text, '$', math.end())
                if stop is not None and '\n' in text[math.end():stop]:
                    stop = None  # Match the native passage parser's currency rule.
            if stop is not None:
                spans.append((position, stop))
                position = stop
                continue
        opening = text[position]
        if opening not in pairs or _escaped(text, position) or (
            opening in {'"', "'", '‘'} and position and text[position - 1].isalnum()
        ):
            position += 1
            continue
        eligible = closers[pairs[opening]]
        number = bisect_right(eligible, position)
        if number < len(eligible):
            closing = eligible[number] + 1
            spans.append((position, closing))
            position = closing
        else:
            position += 1

    merged = []
    for start, stop in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(stop, merged[-1][1])
        else:
            merged.append((start, stop))
    return merged


def _passage_groups(answer: str):
    if not isinstance(answer, str):
        raise TypeError('Citation normalization requires an answer string')
    spans = _protected_spans(answer)
    starts = [start for start, _ in spans]
    for match in _GROUP.finditer(answer):
        if not _P_MARKER.search(match['body']):
            continue
        number = bisect_right(starts, match.start()) - 1
        if number >= 0 and match.start() < spans[number][1]:
            continue
        # Markdown image/link labels and link definitions are literal syntax,
        # not the service's standalone bracketed source references.
        if (match.start() and answer[match.start() - 1] == '!') or answer[match.end():match.end() + 1] in {'(', ':'}:
            continue
        yield match


def passage_citation_references(answer: str) -> tuple[str, ...]:
    """Surviving P-groups are invalid service syntax until full-answer normalization."""
    return tuple(dict.fromkeys(match[0] for match in _passage_groups(answer)))


def normalize_answer_citations(answer: str, evidence: Iterable[Mapping]) -> CitationNormalization:
    """Convert only trusted current PIDs; leave every invalid group intact.

    Normalize the complete initial/repair draft BEFORE extracting claim spans,
    building a judge prompt or binding an answer hash. Never reuse raw-draft
    judgments after this transformation. No input/context is mutated or cached.
    """
    groups = tuple(_passage_groups(answer))
    if not groups:
        return CitationNormalization(answer, 0)
    try:
        records = list(evidence)
        anchors = build_evidence_anchors(records)
        if any(record.get('status') == 'deleted' for record in records):
            raise ValueError('Deleted records are not a current evidence context')
        known_sources = {record['evidence_id'] for record in records}
    except (TypeError, ValueError, KeyError):
        return CitationNormalization(answer, 0, tuple(dict.fromkeys(match[0] for match in groups)))

    chunks, previous, converted, invalid = [], 0, 0, []
    for match in groups:
        chunks.append(answer[previous:match.start()])
        tokens = re.split(r'[,;]', match['body']) if len(match['body']) <= _MAX_GROUP_CHARS else []
        parents, group_count, valid = [], 0, bool(tokens) and len(tokens) <= _MAX_GROUP_TOKENS
        for token in tokens if valid else ():
            parsed = _TOKEN.fullmatch(token.strip())
            if parsed is None:
                valid = False
                break
            if parsed['pid']:
                anchor = anchors.get(parsed['pid'].upper())
                if anchor is None:
                    valid = False
                    break
                parent = anchor['evidence_id']
                group_count += 1
            else:
                parent = parsed['eid'].upper()
                if parent not in known_sources:
                    valid = False
                    break
            if parent not in parents:
                parents.append(parent)
        if valid:
            chunks.append('[' + ', '.join(parents) + ']')
            converted += group_count
        else:
            chunks.append(match[0])
            invalid.append(match[0])
        previous = match.end()
    chunks.append(answer[previous:])
    return CitationNormalization(''.join(chunks), converted, tuple(dict.fromkeys(invalid)))
