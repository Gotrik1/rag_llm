"""Original-offset Markdown dependencies, not semantic or citation judgments.

This module never decides whether a claim is true. It only prevents a caller
from publishing half of an original paragraph, list item, table or math unit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping


@dataclass(frozen=True)
class PublicationUnit:
    """An answer span that must be kept or removed atomically with its associated claim IDs."""
    start: int
    end: int
    claim_ids: tuple[str, ...]
    fully_claimed: bool


def publication_units(answer: str, claims: list[dict], headings: list[dict], *,
                      protected: Iterable[tuple[int, int]] = (),
                      formulas: Iterable[tuple[int, int]] = (),
                      citation_pattern: re.Pattern | None = None) -> list[PublicationUnit]:
    """Group exact original spans; never expand a claim's verification scope.

    ``fully_claimed`` rejects meaningful unassessed text, including the tail
    beyond a caller's claim limit. Only Markdown punctuation/whitespace may be
    retained between verified spans. Headings remain the caller's separately
    qualified context; sibling paragraphs do not become one all-or-none unit.
    """
    protected = list(protected)
    formulas = list(formulas)
    lines, offset, fence = [], 0, None
    for line in answer.splitlines(keepends=True):
        text = line.rstrip('\r\n')
        lines.append((offset, offset + len(text), text))
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', text)
        if fence is None:
            if marker and not any(a <= offset < b for a, b in protected):
                fence = (offset, marker[1][0], len(marker[1]))
        elif marker and marker[1][0] == fence[1] and len(marker[1]) >= fence[2] and not marker[2].strip():
            protected.append((fence[0], offset + len(line)))
            fence = None
        offset += len(line)
    if fence is not None:
        protected.append((fence[0], len(answer)))

    def inside(position):
        return any(a < position < b for a, b in protected)

    boundaries = {0, len(answer)}
    heading_spans = {(item['start'], item['end']) for item in headings}
    # Include unlisted headings too, so a claim-limit tail cannot be joined to
    # an earlier verified paragraph. Do not interpret code/math as headings.
    for start, end, text in lines:
        if not inside(start) and (re.match(r'^ {0,3}#{1,6}(?:[ \t]+|$)', text) or
                re.fullmatch(r'[ \t]*(?:\*\*[^\n]+\*\*|__[^\n]+__):?[ \t]*', text)):
            boundaries.update((start, end))
            heading_spans.add((start, end))
    for start, end in protected:
        # Protect blank lines in code/math, without separating inline math
        # from its containing original paragraph.
        if not answer[answer.rfind('\n', 0, start) + 1:start].strip():
            if answer[start:end].startswith(('```', '~~~')):
                boundaries.update((start, end))
    for match in re.finditer(r'\n[ \t]*\n+', answer):
        if not inside(match.start()) and not inside(match.end()):
            boundaries.update((match.start(), match.end()))

    # A top-level list item includes its indented continuations/nested list.
    # Blank paragraphs inside the same item must not escape its condition.
    item, blank = None, False
    item_spans = []
    for start, end, text in lines:
        if inside(start):
            continue
        marker = re.match(r'^( *)(?:[-*+]\s+|\d+[.)]\s+)', text)
        if marker:
            indent = len(marker[1])
            if item is None or indent <= item[1]:
                if item is not None:
                    item_spans.append((item[0], start))
                item = (start, indent)
            blank = False
        elif not text.strip():
            blank = True
        elif item is not None:
            # An unindented, nonblank line can be a CommonMark lazy
            # continuation of the same item paragraph. A blank separator or
            # explicit block opener ends that continuation, not indentation
            # alone. Otherwise withholding its parent must withhold it too.
            block_opener = ((start, end) in heading_spans or
                            re.match(r'^ {0,3}(?:`{3,}|~{3,}|>)', text))
            if len(text) - len(text.lstrip(' ')) <= item[1] and (blank or block_opener):
                item_spans.append((item[0], start))
                item = None
            blank = False
    if item is not None:
        item_spans.append((item[0], len(answer)))
    for start, end in item_spans:
        boundaries = {point for point in boundaries if not start < point < end}
        boundaries.update((start, end))

    spans = []
    ordered = sorted(boundaries)
    for start, end in zip(ordered, ordered[1:]):
        text = answer[start:end]
        if text.strip():
            spans.append([start + len(text) - len(text.lstrip()), end - len(text) + len(text.rstrip())])

    # A pre-existing C-ID may cross proposed structural boundaries. Never
    # publish or reject only one fragment of that original claim.
    joins = []
    for claim in claims:
        touched = [index for index, (a, b) in enumerate(spans) if a < claim['end'] and claim['start'] < b]
        if touched:
            joins.extend((index, index + 1) for index in range(touched[0], touched[-1]))
    # Only a syntactically continuing colon lead-in depends on a following
    # display formula. Ordinary neighbouring prose remains independent.
    for index in range(len(spans) - 1):
        a, b = spans[index]
        c, d = spans[index + 1]
        lead = answer[a:b]
        formula = answer[c:d]
        if citation_pattern is not None:
            lead = citation_pattern.sub('', lead)
            formula = citation_pattern.sub('', formula)
        starts_formula = formula.lstrip().startswith(('$$', '\\[', '[MATH:', '$', '\\(')) or any(
            c <= start < end <= d and not answer[c:start].strip(' \t\r\n`') for start, end in formulas)
        if ((a, b) not in heading_spans and not answer[b:c].strip() and lead.rstrip().endswith(':') and starts_formula):
            joins.append((index, index + 1))
    linked = {a for a, b in joins}
    merged = []
    for index, span in enumerate(spans):
        if index and index - 1 in linked:
            merged[-1][1] = span[1]
        else:
            merged.append(list(span))

    def scaffold(text):
        # No arbitrary operators, uncited text or numeric tail can become
        # verified just because it sits between two verified C-ID spans.
        return not text.strip() or all(re.fullmatch(r'[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*', line)
                                      for line in text.splitlines() if line.strip())

    result = []
    for start, end in merged:
        members = [claim for claim in claims if start < claim['end'] and claim['start'] < end]
        cursor, complete = start, True
        for claim in members:
            if not scaffold(answer[cursor:claim['start']]):
                complete = False
            cursor = max(cursor, claim['end'])
        if not scaffold(answer[cursor:end]):
            complete = False
        result.append(PublicationUnit(start, end, tuple(claim['claim_id'] for claim in members), complete))
    return result


def expand_rejected_spans(spans: Iterable[tuple[int, int]], units: list[PublicationUnit]) -> list[tuple[int, int]]:
    """An actionable rejection removes its whole original dependency unit."""
    spans = list(spans)
    return spans + [(unit.start, unit.end) for unit in units
                    if any(a < unit.end and unit.start < b for a, b in spans)]


def render_original_spans(answer: str, spans: Mapping[tuple[int, int], str]) -> str:
    """Keep original layout between surviving adjacent spans, not table gaps."""
    result, previous = [], None
    for (start, end), text in sorted(spans.items()):
        if previous is not None:
            gap = answer[previous:start]
            result.append(gap if not gap.strip() else '\n\n')
        result.append(text)
        previous = end
    return ''.join(result)
