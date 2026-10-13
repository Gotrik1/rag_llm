"""Select complete logical sections before assembling model evidence.

The index is a view of the existing docstore, not a second knowledge base.
Source visibility is enforced before heading lookup or relationship expansion.
"""
from __future__ import annotations

import hashlib
import math
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
    "задать", "задаются", "задавать", "настроить", "настраиваются", "создать", "создаются",
    "рассчитать", "рассчитывать", "начислить", "начисляются", "применяются", "используются",
))
_CATEGORY_STEMS = frozenset(_STEMMER.stem(word) for word in ('первой', 'второй', 'третьей', 'четвертой', 'пятой', 'шестой'))
_LOOKUP_WORDS = frozenset(_STEMMER.stem(word) for word in (
    'сколько', 'какова', 'каков', 'каковы', 'стоит', 'стоить', 'стоимость', 'цена',
    'нужно', 'нужен', 'нужна', 'равен', 'равна', 'укажи', 'сообщи',
))


@lru_cache(maxsize=8192)
def term_keys(text: str) -> frozenset[str]:
    # Leading section numbers are not topics, but "2 ЦК" is a topic with an
    # essential category number. Never normalize it to the ambiguous "ЦК".
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s+(?!цк\b)", "", text, flags=re.I)
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
        # An inverted body index also covers documents with no headings.
        # Russian stemming avoids losing a fact when ящик becomes ящика.
        self.body_items = []
        self.body_postings: dict[str, set[int]] = {}
        for node in nodes:
            metadata = node.metadata or {}
            text = node.text or ''
            file = str(metadata.get('file_name', metadata.get('file_path', '')))
            if not file or not text.strip() or metadata.get('status') == 'deleted':
                continue
            position = len(self.body_items)
            self.body_items.append({**metadata, 'file': file, 'text': text})
            for key in term_keys(re.sub(r'\[SECTION:[^\]]*\]', '', text)):
                self.body_postings.setdefault(key, set()).add(position)
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

    def body_seeds(self, question: str, limit: int = 24) -> list[dict]:
        """Find positive subject matches, independently of vector similarity.

        Generic price/action words cannot anchor a match. Require at least two
        subject words and a rare anchor, so incidental mentions in a large
        manual do not swamp an exact fact in a small document.
        """
        keys = term_keys(question) - _LOOKUP_WORDS
        if len(keys) < 2:
            return []
        count = max(1, len(self.body_items))
        weights = {key: math.log1p(count / (1 + len(self.body_postings.get(key, ())))) for key in keys}
        positions = set().union(*(self.body_postings.get(key, set()) for key in keys))
        results = []
        for position in positions:
            item = self.body_items[position]
            matched = {key for key in keys if position in self.body_postings.get(key, ())}
            rare_anchor = any(len(self.body_postings[key]) <= max(2, count * 0.1) for key in matched)
            if len(matched) < 2 or len(matched) / len(keys) < 0.75 or not rare_anchor:
                continue
            coverage = len(matched) / len(keys)
            score = 3.0 + coverage + min(2.0, sum(weights[key] for key in matched) / len(keys))
            results.append({**item, 'score': round(score, 4),
                            'matched_queries': ['body-lexical'],
                            'lexical_subject_keys': sorted(keys), 'lexical_coverage': coverage})
        return sorted(results, key=lambda item: (-item['score'], len(item['text']), source_key(item['file'])))[:limit]

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
                or (kind == 'постановлен' and bool(re.search(r'(?:^|[\W_])пп[\W_]*\d', str(item.get('file', '')).casefold())))
            ) and item.get("status") != "deleted"

        keys = term_keys(question)
        categories = keys & _CATEGORY_STEMS
        headings = []
        for section in self.sections:
            if not visible(section):
                continue
            heading_keys = section["heading_keys"]
            if categories and not (categories & heading_keys):
                continue
            overlap = keys & heading_keys
            if not keys or not overlap:
                continue
            # A leaf heading must cover the subject itself. An ancestor match
            # alone would incorrectly promote every nested exceptional case.
            coverage = len(overlap) / len(keys)
            if coverage < 0.65:
                continue
            # Long normative opening paragraphs contain definitions and
            # notation. Extra words must not outweigh matching the subject.
            score = coverage * 8 + len(overlap) * 0.3 - min(6, len(heading_keys - keys)) * 0.15
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
        # Preserve strong full-subject body matches when no governing heading
        # was found. Do not fill the evidence with unrelated price/cost hits.
        direct = [item for item in ranked if visible(item) and item.get('lexical_coverage') == 1.0]
        if not roots and direct:
            candidates.extend(direct)
        subject_keys = set(direct[0].get('lexical_subject_keys', ())) if direct and not roots else set()
        candidates.extend(
            item for item in ranked if visible(item) and (
                not roots or keys.issubset(term_keys(
                    str(item.get("section_path", "")) + " " + str(item.get("text", ""))
                ))
            ) and (not subject_keys or subject_keys.issubset(term_keys(str(item.get('text', '')))))
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


def overview_sections(question: str, context: list[dict]) -> list[dict]:
    """Plan an overview from immediate children, not from domain-specific facts."""
    value = question.casefold()
    if not re.search(r"что|какие|обзор|опиши|расскажи", value) or re.search(r"что\s+такое", value):
        return []
    roots = [item for item in context if item.get('selection_reason') == 'subject-heading']
    if not roots:
        return []
    root = roots[0]
    prefix = root['section_path'] + ' > '
    children = [item for item in context if source_key(item.get('file')) == source_key(root.get('file'))
                and str(item.get('section_path', '')).startswith(prefix)
                and ' > ' not in item['section_path'][len(prefix):]
                and item.get('evidence_role') != 'exception']
    return children if len(children) >= 2 else []


def missing_overview_sections(answer: str, sections: list[dict]) -> list[dict]:
    headings = re.findall(r'(?m)^\s*(?:#{1,6}\s+|\*\*)([^\n]+)', answer)
    heading_keys = [term_keys(heading.strip('* ')) for heading in headings]
    return [item for item in sections if not any(
        term_keys(str(item.get('section_title', ''))) <= keys for keys in heading_keys
    )]
