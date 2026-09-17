"""Soft knowledge-domain routing for retrieval without changing document ACLs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


RULES_PATH = Path(__file__).with_name("config") / "retrieval_domains.json"
VALID_RETRIEVAL_FOCUS = {"auto", "rre", "orem", "both"}


@dataclass(frozen=True)
class DomainRouting:
    focus: str
    weights: dict[str, float]
    matched_terms: dict[str, tuple[str, ...]]
    policy_version: str

    @property
    def active_domains(self) -> tuple[str, ...]:
        if self.focus == "both":
            return ("rre", "orem")
        if self.focus in {"rre", "orem"}:
            return (self.focus,)
        return tuple(domain for domain in ("rre", "orem") if self.matched_terms.get(domain))


@lru_cache(maxsize=1)
def load_domain_rules() -> dict:
    return json.loads(RULES_PATH.read_text(encoding="utf-8"))


def normalize_retrieval_focus(value: object) -> str:
    focus = str(value or "auto").strip().casefold()
    return focus if focus in VALID_RETRIEVAL_FOCUS else "auto"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold().replace("ё", "е")).strip()


def infer_document_domains(file_name: str, metadata: dict | None = None) -> tuple[str, ...]:
    """Infer multi-label domain tags; explicit registry metadata wins."""
    metadata = metadata if isinstance(metadata, dict) else {}
    explicit = metadata.get("knowledge_domains")
    if isinstance(explicit, str):
        explicit = [explicit]
    if isinstance(explicit, list):
        domains = tuple(dict.fromkeys(
            str(item).strip().casefold() for item in explicit
            if str(item).strip().casefold() in {"rre", "orem", "shared"}
        ))
        if domains:
            return domains

    searchable = _normalized(" ".join((file_name, str(metadata.get("title") or ""))))
    rules = load_domain_rules()
    matched = []
    for domain in ("rre", "orem"):
        terms = rules.get("domains", {}).get(domain, {}).get("document_terms", [])
        if any(_normalized(term) in searchable for term in terms):
            matched.append(domain)
    return tuple(matched) or ("shared",)


def route_query(question: str, focus: str = "auto") -> DomainRouting:
    """Return soft multi-label weights; no domain is used as an access filter."""
    focus = normalize_retrieval_focus(focus)
    rules = load_domain_rules()
    version = str(rules.get("version") or "unknown")
    normalized = _normalized(question)
    matches: dict[str, tuple[str, ...]] = {}
    for domain in ("rre", "orem"):
        terms = rules.get("domains", {}).get(domain, {}).get("question_terms", [])
        hits = tuple(str(term) for term in terms if _normalized(term) in normalized)
        matches[domain] = hits

    if focus == "rre":
        weights = {"rre": 1.0, "orem": 0.20, "shared": 0.65}
    elif focus == "orem":
        weights = {"rre": 0.20, "orem": 1.0, "shared": 0.65}
    elif focus == "both":
        weights = {"rre": 0.90, "orem": 0.90, "shared": 0.70}
    else:
        rre_hits, orem_hits = len(matches["rre"]), len(matches["orem"])
        if rre_hits and orem_hits:
            weights = {"rre": 0.90, "orem": 0.90, "shared": 0.70}
        elif rre_hits:
            weights = {"rre": 1.0, "orem": 0.35, "shared": 0.70}
        elif orem_hits:
            weights = {"rre": 0.35, "orem": 1.0, "shared": 0.70}
        else:
            weights = {"rre": 0.65, "orem": 0.65, "shared": 0.80}
    return DomainRouting(focus, weights, matches, version)


def source_domains_for(file_name: object, source_domains: dict[str, Iterable[str]] | None) -> tuple[str, ...]:
    key = Path(str(file_name or "")).name.casefold()
    values = (source_domains or {}).get(key, ("shared",))
    result = tuple(str(value).casefold() for value in values if str(value).casefold() in {"rre", "orem", "shared"})
    return result or ("shared",)


def apply_domain_routing(
    ranked: list[dict],
    source_domains: dict[str, Iterable[str]] | None,
    routing: DomainRouting,
    top_k: int,
) -> list[dict]:
    """Boost direct-domain evidence and reserve bounded cross-domain coverage."""
    enriched: list[dict] = []
    for item in ranked:
        domains = source_domains_for(item.get("file"), source_domains)
        domain_weight = max(routing.weights.get(domain, 0.0) for domain in domains)
        candidate = dict(item)
        candidate["knowledge_domains"] = list(domains)
        candidate["domain_score"] = round(domain_weight, 3)
        candidate["score"] = round(float(item.get("score", 0.0)) + 0.45 * domain_weight, 4)
        enriched.append(candidate)
    enriched.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)

    # At most one reserved item per relevant business domain. This prevents a
    # large OREM or RRE manual from crowding out the other domain while leaving
    # the remaining positions to normal relevance ranking.
    reserved: list[dict] = []
    reserved_ids: set[int] = set()
    for domain in routing.active_domains:
        match = next((item for item in enriched if domain in item.get("knowledge_domains", [])), None)
        if match is not None:
            reserved.append(match)
            reserved_ids.add(id(match))
    return [*reserved, *(item for item in enriched if id(item) not in reserved_ids)][:max(top_k * 3, top_k)]
