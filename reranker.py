"""Optional cross-encoder reranking with a deterministic fail-open fallback."""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RerankerSettings:
    mode: str = "off"
    url: str = "http://llm-bridge:11434/api/rerank"
    model: str = "BAAI/bge-reranker-base"
    timeout_seconds: float = 8.0
    candidate_limit: int = 40
    model_weight: float = 0.75
    text_limit: int = 6000

    @classmethod
    def from_env(cls) -> "RerankerSettings":
        mode = os.getenv("RAG_RERANKER_MODE", "off").strip().lower()
        if mode not in {"off", "http"}:
            LOGGER.warning("Unknown RAG_RERANKER_MODE=%s; reranker is disabled", mode)
            mode = "off"
        return cls(
            mode=mode,
            url=os.getenv("RAG_RERANKER_URL", cls.url).strip(),
            model=os.getenv("RAG_RERANKER_MODEL", cls.model).strip(),
            timeout_seconds=max(0.1, float(os.getenv("RAG_RERANKER_TIMEOUT_SECONDS", cls.timeout_seconds))),
            candidate_limit=max(1, int(os.getenv("RAG_RERANKER_CANDIDATE_LIMIT", cls.candidate_limit))),
            model_weight=max(0.0, float(os.getenv("RAG_RERANKER_MODEL_WEIGHT", cls.model_weight))),
            text_limit=max(256, int(os.getenv("RAG_RERANKER_TEXT_LIMIT", cls.text_limit))),
        )


def candidate_text(item: dict[str, Any], limit: int = 6000) -> str:
    """Build the cross-encoder passage without leaking internal chunk identifiers."""
    parts = [
        str(item.get("section_path", "")).strip(),
        str(item.get("section_title", "")).strip(),
        str(item.get("text", "")).strip(),
    ]
    return "\n".join(part for part in parts if part)[:limit]


def _validated_scores(payload: Any, expected: int) -> list[float]:
    raw_scores = payload.get("scores") if isinstance(payload, dict) else None
    if not isinstance(raw_scores, list) or len(raw_scores) != expected:
        raise ValueError("reranker response must contain one score per document")
    scores = [float(value) for value in raw_scores]
    if not all(math.isfinite(value) for value in scores):
        raise ValueError("reranker returned a non-finite score")
    return scores


def _bounded_score(value: float) -> float:
    """Map arbitrary cross-encoder logits to a stable zero-to-one contribution."""
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


class HttpReranker:
    """Call a private cross-encoder service and preserve the input on any failure."""

    def __init__(self, settings: RerankerSettings | None = None):
        self.settings = settings or RerankerSettings.from_env()

    @property
    def enabled(self) -> bool:
        return self.settings.mode == "http" and bool(self.settings.url)

    def rerank(self, question: str, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not self.enabled or len(ranked) < 2:
            return ranked
        candidate_count = min(len(ranked), self.settings.candidate_limit)
        candidates = ranked[:candidate_count]
        request_body = json.dumps(
            {
                "query": question,
                "documents": [candidate_text(item, self.settings.text_limit) for item in candidates],
                "model": self.settings.model,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            self.settings.url,
            data=request_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            scores = _validated_scores(payload, candidate_count)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
            LOGGER.warning("Cross-encoder reranker failed open: %s", type(exc).__name__)
            return ranked

        enriched: list[dict[str, Any]] = []
        for item, model_score in zip(candidates, scores, strict=True):
            result = dict(item)
            heuristic_score = float(item.get("score", 0.0))
            bounded_score = _bounded_score(model_score)
            result["heuristic_score"] = round(heuristic_score, 6)
            result["reranker_score"] = round(model_score, 6)
            # Keep tested domain/category penalties authoritative.  The model can
            # promote a strong passage, but cannot erase a multi-point hard
            # penalty introduced by deterministic business rules.
            result["score"] = round(
                heuristic_score + self.settings.model_weight * bounded_score,
                6,
            )
            enriched.append(result)
        enriched.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        return [*enriched, *ranked[candidate_count:]]
