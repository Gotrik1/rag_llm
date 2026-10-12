"""Select complete logical sections before assembling model evidence.

The index is a view of the existing docstore, not a second knowledge base.
Source visibility is enforced before heading lookup or relationship expansion.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path

from nltk.stem.snowball import RussianStemmer

_STEMMER = RussianStemmer()
_STOP = {
    "а", "в", "во", "и", "из", "на", "по", "к", "с", "со", "для", "от", "о", "об",
    "это", "что", "как", "какой", "какая", "какие", "каких", "каком", "чем", "ли",
    "такое", "такой", "таком", "почему", "где", "когда", "есть", "надо", "можно",
    "расскажи", "объясни", "приведи", "покажи", "дай", "напиши", "полный", "все",
    "бывают", "бывает", "ведется", "ведутся", "реализованы", "система", "системе",
    "документ", "документах", "документации", "инструкция", "инструкции",
}
_EXCEPTION = re.compile(r"исключени|специальн\w* случа|особ\w* случа|частн\w* случа", re.I)
_GENERIC_STEMS = frozenset(_STEMMER.stem(word) for word in (
    "рассчитывается", "расчет", "определяется", "постановления", "методики", "сравни", "отличаются",
))


@lru_cache(maxsize=8192)
def term_keys(text: str) -> frozenset[str]:
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s+", "", text)
    category_words = {"1": "первой", "2": "второй", "3": "третьей", "4": "четвертой", "5": "пятой", "6": "шестой"}
    text = re.sub(r"([1-6])\s+и\s+([1-6])\s*цк", lambda m: f"{category_words[m[1]]} ценовой категории и {category_words[m[2]]} ценовой категории", text, flags=re.I)
    text = re.sub(r"(?<!\d)([1-6])\s*цк", lambda m: f"{category_words[m[1]]} ценовой категории", text, flags=re.I)
    words = re.findall(r"[а-яёa-z]+|\d+", text.casefold().replace("ё", "е"))
    return frozenset(_STEMMER.stem(word) for word in words if word not in _STOP) - _GENERIC_STEMS


def source_key(value: object) -> str:
    return Path(str(value or "")).name.casefold()


def requested_source_kind(question: str) -> str | None:
    value = question.casefold()
    if re.search(r"(?:по|согласно|из|смотреть|только)\s+(?:норм\w*\s+)?постановлен\w*", value):
        return "постановлен"
    if re.search(r"(?:по|согласно|из|только)\s+методик\w*", value):
        return "методик"
    return None


def evidence_role(item: dict) -> str:
    leaf = str(item.get("section_title") or item.get("section_path", "").rsplit(">", 1)[-1])
    if _EXCEPTION.search(leaf):
        return "exception"
    prefix = re.sub(r"\[SECTION:[^\]]*\]", "", str(item.get("text", ""))).strip()[:250]
    if re.match(r"(?:В виде исключения|За исключением|В особых случаях|Только для)\b", prefix, re.I):
        return "exception"
    if re.search(r"определени|термин|виды|назначени", leaf, re.I):
        return "definition"
    return "main"


def evidence_id(item: dict) -> str:
    return hashlib.sha256((source_key(item.get("file")) + "\n" + str(item.get("text", ""))).encode()).hexdigest()


class SectionEvidenceIndex:
    """Group split paragraphs by physical document and complete section path."""

    def __init__(self, nodes: list):
        groups: OrderedDict[tuple[str, str], dict] = OrderedDict()
        for position, node in enumerate(nodes):
            metadata = node.metadata or {}
            path = str(metadata.get("section_path", "")).strip()
            marker = re.search(r"\[SECTION:\s*([^\]]+)\]", node.text or "")
            if marker and path.endswith("..."):
                path = marker.group(1).strip()
            file = str(metadata.get("file_name", metadata.get("file_path", "")))
            if not path or not file or metadata.get("status") == "deleted":
                continue
            key = (source_key(file), path)
            group = groups.setdefault(key, {
                **metadata, "file": file, "section_path": path,
                "section_title": metadata.get("section_title") or path.rsplit(">", 1)[-1].strip(),
                "position": position, "parts": [], "seen": set(),
            })
            body = re.sub(r"\[SECTION:[^\]]*\]\s*", "", node.text or "").strip()
            if body and body not in group["seen"]:
                group["parts"].append(body)
                group["seen"].add(body)
        self.sections = []
        for group in groups.values():
            parts = group.pop("parts")
            group.pop("seen")
            if not parts:
                continue
            group["text"] = "\n\n".join(parts)
            group["heading_keys"] = term_keys(str(group["section_title"]))
            # Normative point numbers identify locations, not their subject.
            # The first paragraph of a point carries its topic; later text may
            # contain rare exceptions and must not override that main topic.
            if re.fullmatch(r"Пункт \d+(?:\.\d+)*", group["section_path"].rsplit(" > ", 1)[-1]):
                group["heading_keys"] = term_keys(group["text"].split("\n\n", 1)[0][:500])
            self.sections.append(group)

    def select(
        self, question: str, ranked: list[dict], *, excluded_sources: set[str] | None = None,
        allowed_sources: set[str] | None = None, top_k: int = 10, max_chars: int = 36000,
    ) -> list[dict]:
        excluded = {source_key(value) for value in (excluded_sources or ())}
        allowed = {source_key(value) for value in (allowed_sources or ())}
        kind = requested_source_kind(question)

        def visible(item: dict) -> bool:
            key = source_key(item.get("file"))
            return key not in excluded and (allowed_sources is None or key in allowed) and (
                not kind or kind in str(item.get("file", "")).casefold()
            ) and item.get("status") != "deleted"

        keys = term_keys(question)
        headings = []
        for section in self.sections:
            if not visible(section):
                continue
            heading_keys = section["heading_keys"]
            overlap = keys & heading_keys
            if not keys or not overlap:
                continue
            # A leaf heading must cover the subject itself. An ancestor match
            # alone would incorrectly promote every nested exceptional case.
            coverage = len(overlap) / len(keys)
            if coverage < 0.65:
                continue
            score = coverage * 4 + len(overlap) * 0.3 - len(heading_keys - keys) * 0.15
            if heading_keys == keys:
                score += 2
            if evidence_role(section) == "exception" and not _EXCEPTION.search(question):
                score -= 3
            headings.append((score, section))
        headings.sort(key=lambda pair: (-pair[0], pair[1]["position"]))
        roots = [section for score, section in headings[:3] if score >= headings[0][0] - 0.6] if headings else []
        candidates: list[dict] = []
        for root in roots:
            candidates.append({**root, "score": 1.0, "selection_reason": "subject-heading"})
        # Expand each exact subject into its own children before considering
        # unrelated semantic hits. Preserve document order and sibling breadth.
        children = []
        for root in roots:
            prefix = root["section_path"] + " > "
            for section in self.sections:
                if visible(section) and source_key(section["file"]) == source_key(root["file"]) and section["section_path"].startswith(prefix):
                    children.append({**section, "score": 0.9, "selection_reason": "subject-child"})
        children.sort(key=lambda item: (item["section_path"].count(">"), item["position"]))
        candidates.extend(children)
        candidates.extend(
            item for item in ranked if visible(item) and (
                not roots or keys.issubset(term_keys(
                    str(item.get("section_path", "")) + " " + str(item.get("text", ""))
                ))
            )
        )
        selected, seen, used = [], set(), 0
        limit = max(top_k, min(24, len(roots) + len(children)))
        for candidate in candidates:
            item = {key: value for key, value in candidate.items() if key not in {"heading_keys", "position"}}
            identity = evidence_id(item)
            if identity in seen:
                continue
            text = str(item.get("text", ""))
            # Whole-section packing has a separate input budget. Never cut an
            # equation mid-token or silently turn a partial section into whole.
            remaining = max_chars - used
            if remaining < 300:
                break
            if len(text) > remaining:
                blocks, size = [], 0
                for block in text.split("\n\n"):
                    if size + len(block) + 2 > remaining:
                        break
                    blocks.append(block)
                    size += len(block) + 2
                if not blocks:
                    continue
                item["text"] = "\n\n".join(blocks)
                item["evidence_truncated"] = True
            item["evidence_role"] = evidence_role(item)
            item.setdefault("selection_reason", "retrieval-rank")
            used += len(item["text"])
            selected.append(item)
            seen.add(identity)
            if len(selected) >= limit:
                break
        if not _EXCEPTION.search(question):
            selected.sort(key=lambda item: item["evidence_role"] == "exception")
        return selected


def generation_evidence_prompt(context: list[dict]) -> str:
    roles = {"main": "основное правило", "definition": "определение", "exception": "частный случай или исключение"}
    return "\n".join(
        f"- {item.get('file', '')}, {item.get('section_path', '')}: "
        f"{roles.get(item.get('evidence_role'), 'подтверждающий фрагмент')}"
        for item in context
    )
