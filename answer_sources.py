"""Present retrieval evidence as API source metadata without changing its ranking.

Canonical sections assembled from the native document index need not have a
retriever score. ``None`` communicates that absence; inventing a zero or using
an internal packing priority would misrepresent how the source was selected.
The evidence records remain owned by retrieval and are never mutated here.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


def build_answer_sources(context: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Return source labels, optional scores and bounded native-text previews.

    ``file`` and ``text`` remain required evidence fields. Missing, invalid or
    non-finite ranking metadata is represented as JSON ``null``; this helper
    does not select documents, grant access, or certify the answer's accuracy.
    Citation links are enriched separately by the HTTP application adapter.
    """
    sources = []
    for item in context:
        value = item.get('score')
        try:
            score = float(value) if value is not None and not isinstance(value, bool) else None
        except (TypeError, ValueError, OverflowError):
            score = None
        if score is not None and not math.isfinite(score):
            score = None
        sources.append({
            'file': item['file'],
            'section': item.get('section_path', ''),
            'score': score,
            'text_preview': item['text'][:120],
        })
    return sources
