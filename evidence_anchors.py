"""Verbatim source passages addressable without model-generated quote text.

Anchors are local to an already ACL-filtered evidence snapshot. They are not
proof of entailment: selecting a real passage can still be semantically wrong.
No source content is dropped to satisfy a character target or output budget.
"""
from __future__ import annotations

import re
from bisect import bisect_right
from typing import Any, Mapping, Sequence


TARGET_PASSAGE_CHARS = 1200
PRESERVE_PARAGRAPH_CHARS = 2400
MAX_RESOLVED_PASSAGES = 32
_EVIDENCE_ID = re.compile(r"E[1-9]\d*\Z")
_MATH_START = re.compile(r"\$\$|\\\[|\\\(|\[MATH(?::|\])|(?<!\$)\$(?!\$)", re.I)
_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*(?:\r?\n)+")
_SENTENCE_BREAK = re.compile(r"[.!?;][ \t\r\n]+")


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


def _large_paragraph_units(text: str) -> list[str]:
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
    records = _records(evidence)
    anchors = build_evidence_anchors(records)
    return [{"evidence_id": item["evidence_id"], "file": item.get("file"), "section_path": item.get("section_path"),
             "applicability": item.get("applicability"),
             **{key:item[key] for key in ('reference_from','reference_target') if item.get(key) is not None},
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
