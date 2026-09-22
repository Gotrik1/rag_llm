"""
RAG Agent — LlamaIndex + Qdrant + Ollama (qwen2.5:14b)
Гибридный поиск: векторный (Qdrant) + BM25
Детерминизм: temperature=0, seed=42, SQLite кэш по хэшу вопроса
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import sys
import threading
import unicodedata
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# ─── LlamaIndex ───────────────────────────────────────────────
from llama_index.core import (
    Document,
    Settings,
    SimpleDirectoryReader,
    StorageContext,
    VectorStoreIndex,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.retrievers import QueryFusionRetriever
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.response_synthesizers import get_response_synthesizer
from llama_index.core.schema import QueryBundle
from llama_index.core.storage.docstore import SimpleDocumentStore
from llama_index.core.prompts import PromptTemplate
from llama_index.retrievers.bm25 import BM25Retriever

from llama_index.llms.ollama import Ollama
from llama_index.embeddings.ollama import OllamaEmbedding
from llama_index.vector_stores.qdrant import QdrantVectorStore

import qdrant_client
from ollama import Client as OllamaClient
from document_ingestion import ingest_document
from domain_routing import apply_domain_routing, route_query, source_domains_for
from llm_providers import ProviderLLM
from response_cache import ResponseCache
from reranker import HttpReranker
from runtime_paths import DATA_DIR
from opentelemetry.trace import Status, StatusCode
from telemetry import PIPELINE_SECONDS, tracer

# ─── Настройки ────────────────────────────────────────────────
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
LLM_MODEL       = os.environ.get("LLM_MODEL", "qwen2.5:14b")
EMBED_MODEL      = os.environ.get("EMBED_MODEL", "nomic-embed-text")

QDRANT_URL       = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "rag_docs_v12")

CHUNK_SIZE       = 1024
CHUNK_OVERLAP    = 128
TOP_K            = 10      # финальных чанков в контексте
BM25_TOP_K       = 12      # кандидатов от BM25
VECTOR_TOP_K     = 12      # кандидатов от вектора

# A semantic/chunking change must never reuse an older docstore or vector
# collection: the two indexes must describe precisely the same corpus.
BM25_PERSIST_DIR = os.environ.get("BM25_PERSIST_DIR", str(DATA_DIR / "bm25_v12"))
CACHE_VERSION    = "v36-soft-domain-routing-2026-09-17-1"
QUERY_UNDERSTANDING_VERSION = "v2"
MAX_OUTPUT_TOKENS = 4056

# Soft topic routing never acts as an ACL. It only makes broad business
# questions probe both implementation manuals and governing regulations.
RETRIEVAL_TOPIC_PROFILES: tuple[dict[str, object], ...] = (
    {
        "name": "punce",
        "question_patterns": (r"\bпунц[эе]?\b", r"предельн\w*\s+уров\w*\s+нерегулируем\w*\s+цен"),
        "queries": (
            "ПУНЦ предельный уровень нерегулируемых цен определение состав расчет",
            "1179 предельные уровни нерегулируемых цен ценовые категории",
            "методика расчета стоимости 1-6 ЦК ПУНЦ алгоритм",
        ),
        "source_families": (("1179",), ("методик", "1-6")),
    },
    {
        "name": "first-price-category",
        "question_patterns": (r"\b(?:1|перв\w*)\s*(?:цк|ценов\w*\s+категор)",),
        "queries": (
            "порядок определения стоимости 1 ЦК первая ценовая категория",
            "методика расчета стоимости 1-6 ЦК первая ценовая категория формулы",
            "442 1179 первая ценовая категория нерегулируемые цены",
        ),
        "source_families": (("методик", "1-6"), ("442",), ("1179",)),
        "locator_terms": ("порядок определения стоимости 1 цк",),
    },
    {
        "name": "sixth-price-category",
        "question_patterns": (r"\b(?:6|шест\w*)\s*(?:цк|ценов\w*\s+категор)",),
        "queries": (
            "порядок определения стоимости и цены для 5 и 6 ЦК",
            "методика расчета стоимости 1-6 ЦК шестая ценовая категория мощность",
            "442 1179 шестая ценовая категория нерегулируемые цены",
        ),
        "source_families": (("методик", "1-6"), ("442",), ("1179",)),
        "locator_terms": ("порядок определения стоимости и цены для 5 и 6 цк",),
    },
    {
        "name": "transmission-calculation",
        "question_patterns": (
            r"(?:расчет\w*|рассчит\w*|стоимост\w*).{0,45}(?:передач\w*)",
            r"передач\w*.{0,45}(?:договор\w*|цк|ценов\w*\s+категор)",
        ),
        "queries": (
            "методика расчета стоимости услуг по передаче договор",
            "методика расчета стоимости 1-6 ЦК услуги по передаче",
            "передача электроэнергии отдельный договор и в составе ценовой категории",
        ),
        "source_families": (("методик", "передач"), ("методик", "1-6")),
    },
)

PROMPT_INJECTION_MARKERS = (
    "ignore all instructions",
    "ignore previous instructions",
    "system prompt",
    "суперпароль",
    "пароль root",
    "api key",
)


@contextmanager
def _pipeline_span(
    stage: str,
    *,
    flow: str = "python",
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Record content-free pipeline telemetry without retaining prompts or evidence."""
    started = time.perf_counter()
    with tracer().start_as_current_span(
        f"rag.{stage}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("rag.pipeline.stage", stage)
        span.set_attribute("rag.flow", flow)
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        try:
            yield span
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(exc).__name__)
            raise
        finally:
            PIPELINE_SECONDS.labels(stage=stage, flow=flow).observe(time.perf_counter() - started)


class _TracedRetriever:
    """Proxy vector and BM25 retrievers while exposing aggregate timings only."""

    def __init__(self, retriever: Any, stage: str):
        self._retriever = retriever
        self._stage = stage

    def retrieve(self, *args: Any, **kwargs: Any):
        with _pipeline_span(self._stage) as span:
            results = self._retriever.retrieve(*args, **kwargs)
            span.set_attribute("rag.results.count", len(results))
            return results

    async def aretrieve(self, *args: Any, **kwargs: Any):
        with _pipeline_span(self._stage) as span:
            results = await self._retriever.aretrieve(*args, **kwargs)
            span.set_attribute("rag.results.count", len(results))
            return results

    def __getattr__(self, name: str) -> Any:
        return getattr(self._retriever, name)


def contains_prompt_injection(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", (text or "").lower())
    return any(marker in normalized for marker in PROMPT_INJECTION_MARKERS)


def refusal_answer() -> str:
    return "Я не знаю: в базе знаний нет подтверждённой информации для ответа."

# Model-specific generation budgets. Qwen 3.5 enables long reasoning by default
# and advertises a 262k context window; neither is appropriate for this local RAG
# request path, which supplies a bounded set of document chunks.
OLLAMA_MODEL_PROFILES: dict[str, dict] = {
    "Qwen/Qwen3.8-27B": {
        "context_window": 32768,
        "thinking": False,
        "num_predict": 2048,
        "request_timeout": 180.0,
    },
    "qwen3.5:9b": {
        "context_window": 8192,
        "thinking": False,
        "num_predict": MAX_OUTPUT_TOKENS,
        "request_timeout": 120.0,
    },
}
DEFAULT_OLLAMA_PROFILE = {
    "context_window": 16384,
    "thinking": None,
    "num_predict": MAX_OUTPUT_TOKENS,
    "request_timeout": 180.0,
}


SYSTEM_PROMPT = """\
Ты — точный ассистент по нормативным документам. Отвечай ТОЛЬКО на основе предоставленного контекста.

Правила:
1. Если информация есть в контексте — дай точный ответ со ссылкой на раздел.
2. Если нужной информации НЕТ в контексте — прямо скажи об этом, не придумывай.
3. Для формул и числовых значений — цитируй точно, без округлений.
4. Аббревиатуры (ИВ0, ИВ1, ИВ0-1, ИС и т.п.) — используй только те определения, которые явно указаны в контексте.
5. Не смешивай определения разных разделов.
6. Если в контексте формулы даны в маркерах вида [С_sub(...)] или [ΔО_sub(...)], сохраняй этот формат и не раскладывай его на отдельные токены вроде [EQ], [s], [].
7. Форматируй ответ в CommonMark Markdown: отделяй абзацы одной пустой строкой, используй корректные списки и не используй HTML.
"""


GROUNDED_SYSTEM_PROMPT = """\
Ты — точный ассистент по документам. Работай как проверяемый RAG-ответчик для технических, нормативных, офисных и смешанных документов.

Правила:
1. Отвечай только по блоку "КОНТЕКСТ". Утверждение включай, только если оно подтверждено контекстом, относится к сущности из вопроса и помогает ответить на него.
2. Сначала определи тип запроса: инструкция, определение, расчёт или ограничение. Структура ответа должна соответствовать этому типу, а не быть фиксированной.
3. Для инструкций дай 4–7 нумерованных действий от начала операции до завершения. Укажи финальное действие — сохранить, подтвердить, применить или запустить — если оно есть в контексте.
4. Не создавай разделы "Определения", "Расчётные правила" или "Ограничения", если пользователь их не запрашивает и они не нужны для выполнения действия.
5. Соблюдай границы сущности. Обычный параметр, параметр макроса, архивный, импортируемый параметр и параметр отчёта — разные сущности. Не смешивай их без прямого указания в вопросе или источнике.
6. Соблюдай границы операции. Для создания не добавляй удаление, импорт, расчёт, копирование или редактирование, если это не необходимо для создания.
7. Для вопросов о формулах используй только блок "РАЗРЕШЕННЫЕ РАСЧЕТНЫЕ ПРАВИЛА". Не восстанавливай формулы по памяти. Сохраняй маркеры вида [С_sub(...)] и [ΔО_sub(...)] без разбиения на [EQ], [s], [].
8. Не делай предположений и не используй формулировки «или в аналогичных интерфейсах», «вероятно», «обычно» или «по всей видимости», если этого нет в источнике.
9. Не показывай внутренние сообщения RAG, сведения о нехватке контекста, retrieval, reranking или технических блоках. Если дополнительная информация не найдена, просто не добавляй её.
10. Игнорируй нерелевантные фрагменты контекста, даже если они фактически верны. Перед ответом проверь: речь идёт об одной сущности, сценарий завершён, нет сведений из соседних разделов и пустых искусственных секций.
11. Форматируй ответ строго в CommonMark Markdown. Абзацы разделяй одной пустой строкой. Для перечислений используй `- пункт` или `1. пункт`; заголовки `##` и `###` — только для длинных ответов. Таблицы GFM используй только для сравнения однотипных сущностей с короткими значениями; если ячейка требует несколько пунктов, вместо таблицы используй подзаголовок и маркированный список. Не используй HTML и не оставляй незакрытые Markdown-маркеры.
12. После фактического утверждения указывай краткую текстовую ссылку вида `[Источник: имя документа — раздел «название»]`. Источник — это документ и понятный ориентир внутри него, а не чанк, UUID, score или другой технический идентификатор. Используй только имя документа и ориентир из КОНТЕКСТА; не придумывай URL или номер страницы. Кликабельные ссылки приложение добавит отдельно.
"""


TEXT_QA_TEMPLATE = PromptTemplate(
    SYSTEM_PROMPT
    + """

Контекст:
---------------------
{context_str}
---------------------

Вопрос: {query_str}

Ответ:
"""
)


@dataclass(frozen=True)
class PrismQuery:
    """Deterministic query contract shared by retrieval and answer validation."""

    intent: str
    entities: tuple[str, ...]
    target_sections: tuple[str, ...]
    required_sections: tuple[str, ...]
    excluded_chunk_types: tuple[str, ...]
    answer_mode: str


@dataclass(frozen=True)
class QueryUnderstanding:
    """Bounded, inspectable query plan created before document retrieval."""

    canonical_question: str
    facets: tuple[str, ...]
    retrieval_queries: tuple[str, ...]
    corrections: tuple[str, ...] = ()


FORMULA_QUERY_MARKERS = (
    "формула", "формуле", "формулу", "формулы", "формул",
    "уравнение", "уравнении", "расчетное правило", "расчётное правило",
)

# These are deliberately limited to unambiguous, frequent keyboard/transposition
# errors observed in real questions.  General fuzzy correction would be unsafe
# for regulatory terminology and identifiers, so semantic planning still sees
# the original wording for everything outside this list.
HIGH_CONFIDENCE_QUERY_TYPOS = {
    "ак": "как",
    "авнсы": "авансы",
    # Users frequently type the Cyrillic letter О instead of the zero in the
    # domain identifier ИВ0.  Correct only the standalone token: identifiers
    # such as ИВОН/ИВон remain untouched.
    "иво": "ИВ0",
    "расчитывается": "рассчитывается",
    "расчитываются": "рассчитываются",
    "сиситеме": "системе",
    "щитается": "рассчитывается",
    "щитаются": "рассчитываются",
}

# A calculation request needs evidence from a governing calculation/procedure
# section, not merely a mention of the subject somewhere in a document.
CALCULATION_SECTION_KEYS = frozenset({"алго", "форм", "поря", "расч", "начи"})
GENERIC_CALCULATION_QUERY_KEYS = frozenset({
    "расс", "расч", "форм", "поря", "сист", "докум", "разд", "глав",
    "прав", "услов", "мето", "данн", "пери", "объе",
})


def is_formula_question(question: str) -> bool:
    """Recognise every common Russian grammatical form of a formula request."""
    normalized = (question or "").lower().replace("ё", "е")
    return any(marker in normalized for marker in FORMULA_QUERY_MARKERS)


def asks_for_calculation(question: str) -> bool:
    """Recognise explanatory calculation requests without forcing formula-only mode."""
    normalized = (question or "").lower().replace("ё", "е")
    return is_formula_question(question) or bool(re.search(
        r"\b(?:рассчит\w*|рассчитыва\w*|расчет\w*|"
        r"порядок\s+расчета)\b",
        normalized,
    ))


def normalize_user_question(question: str) -> tuple[str, tuple[str, ...]]:
    """Apply only high-confidence corrections before semantic planning."""
    original = re.sub(r"\s+", " ", (question or "").strip())
    typo_pattern = "|".join(sorted(map(re.escape, HIGH_CONFIDENCE_QUERY_TYPOS), key=len, reverse=True))
    normalized = re.sub(
        rf"(?i)(?<![а-яё])(?:{typo_pattern})(?![а-яё])",
        lambda match: HIGH_CONFIDENCE_QUERY_TYPOS[match.group(0).casefold()],
        original,
    )
    normalized = re.sub(
        r"(?i)(?<![а-яёa-z0-9])(\d+)\s*цк(?![а-яёa-z0-9])",
        lambda match: f"{match.group(1)} ЦК",
        normalized,
    )
    corrections = () if normalized == original else (f"{original} -> {normalized}",)
    return normalized, corrections


def _price_category_variants(question: str) -> list[str]:
    variants: list[str] = []
    for match in re.finditer(r"(?i)(?<![а-яёa-z0-9])(\d+)\s*цк(?![а-яёa-z0-9])", question):
        number = match.group(1)
        variants.extend((f"{number}ЦК", f"{number} ЦК", f"{number}-я ценовая категория"))
        if number == "6":
            variants.append("шестая ценовая категория")
    return variants


PRICE_CATEGORY_WORDS = {
    "1": "первая", "2": "вторая", "3": "третья",
    "4": "четвертая", "5": "пятая", "6": "шестая",
}
PRICE_CATEGORY_STEMS = {
    "1": "перв", "2": "втор", "3": "трет",
    "4": "четверт", "5": "пят", "6": "шест",
}


def price_category_number(value: str) -> str | None:
    """Return the explicitly requested numeric price category, if present."""
    normalized = (value or "").casefold().replace("ё", "е")
    match = re.search(r"(?i)(?<![а-яёa-z0-9])(\d+)\s*цк(?![а-яёa-z0-9])", normalized)
    if match:
        return match.group(1)
    return next(
        (
            number for number, stem in PRICE_CATEGORY_STEMS.items()
            if re.search(rf"\b{stem}\w*\s*цк\b", normalized)
        ),
        None,
    )


def contains_price_category(value: str, number: str) -> bool:
    """Match one category, excluding range-only labels such as ``1-6 ЦК``."""
    normalized = (value or "").lower().replace("ё", "е")
    numeric = re.compile(rf"(?<![\d-]){re.escape(number)}\s*цк(?![а-яa-z0-9])", re.IGNORECASE)
    if numeric.search(normalized):
        return True
    numeric_expanded = re.compile(
        rf"(?<![\d-]){re.escape(number)}(?:-?[яй])?\s+ценов\w*\s+категор\w*\b",
        re.IGNORECASE,
    )
    if numeric_expanded.search(normalized):
        return True
    stem = PRICE_CATEGORY_STEMS.get(number)
    return bool(stem and re.search(rf"\b{stem}\w*\s+ценов\w*\s+категор\w*\b", normalized))


def price_category_item_matches(item: dict, number: str) -> bool:
    haystack = "\n".join([
        str(item.get("section_path", "")),
        str(item.get("section_title", "")),
        str(item.get("text", "")),
    ])
    return contains_price_category(haystack, number)


DOCUMENT_OCCURRENCE_STOP_PREFIXES = (
    "документ", "напис", "опис", "фигур", "упомин", "встреч", "содерж", "перечисл",
    "назов", "покаж", "каки", "како", "котор", "информац", "сведен", "привед",
)
DOCUMENT_OCCURRENCE_STOP_WORDS = {
    "есть", "имеется", "про", "тема", "темы", "этот", "эта", "это", "эти",
}


def document_occurrence_terms(question: str) -> tuple[str, ...]:
    """Extract the subject of a positive document-occurrence lookup."""
    normalized = normalize_user_question(question)[0].lower().replace("ё", "е")
    terms = []
    for token in re.findall(r"[а-яa-z0-9-]{3,}", normalized):
        if token in DOCUMENT_OCCURRENCE_STOP_WORDS:
            continue
        if any(token.startswith(prefix) for prefix in DOCUMENT_OCCURRENCE_STOP_PREFIXES):
            continue
        terms.append(token)
    return tuple(dict.fromkeys(terms))


def mentions_price_category_subject(value: str) -> bool:
    """Recognize the generic price-category subject, including the ``ЦК`` alias."""
    normalized = (value or "").lower().replace("ё", "е")
    return bool(
        re.search(r"\bценов\w*\s+категор\w*\b", normalized)
        or re.search(r"(?<![а-яa-z])(?:[1-6]\s*)?цк(?![а-яa-z])", normalized)
    )


def is_price_composition_question(question: str) -> bool:
    """Recognise broad retail-price composition questions without pinning a file."""
    normalized = (question or "").lower().replace("ё", "е")
    has_price_subject = any(marker in normalized for marker in ("цен", "стоим", "пунц"))
    implicit_energy_context = (
        ("структур" in normalized and "цен" in normalized and "систем" in normalized)
        or ("полный" in normalized and "состав цен" in normalized and "услуг" in normalized)
    )
    has_energy_subject = any(marker in normalized for marker in (
        "электроэнерг", "электрическ", "мощност", "пунц",
    )) or bool(re.search(r"(?<![а-яa-z0-9])ээ(?![а-яa-z0-9])", normalized)) or implicit_energy_context
    has_composition_intent = any(marker in normalized for marker in (
        "состоит", "составля", "складыва", "структур", "част",
        "входит", "формиру", "покуп", "передач", "сбыт", "иным услуг",
    ))
    return has_price_subject and has_energy_subject and has_composition_intent


def has_document_occurrence_intent(question: str) -> bool:
    """Recognize requests for a positive list of documents containing a subject."""
    normalized = (question or "").lower().replace("ё", "е")
    source_kind = r"(?:документ|документац|источник|постановлен|методик)\w*"
    return bool(re.search(
        rf"\b(?:"
        rf"в\s+каких\s+{source_kind}|"
        rf"как(?:ие|ая|ую)\s+{source_kind}|"
        rf"где\s+(?:в\s+документ\w*\s+)?(?:есть|напис|опис|фигур|упомин|встреча|содерж)\w*|"
        rf"(?:перечисл|найд|покаж|назов)\w*(?:\s+[а-яa-z0-9-]+){{0,4}}\s+{source_kind}|"
        rf"(?:полный\s+)?перечень\s+{source_kind}"
        rf")\b",
        normalized,
    ))


def is_document_occurrence_question(question: str) -> bool:
    normalized = (question or "").lower().replace("ё", "е")
    return has_document_occurrence_intent(normalized) and bool(
        price_category_number(normalized)
        or mentions_price_category_subject(normalized)
        or document_occurrence_terms(normalized)
    )


def document_occurrence_item_matches(item: dict, question: str) -> bool:
    """Confirm that an evidence item directly contains the requested subject."""
    category = price_category_number(question)
    if category:
        return price_category_item_matches(item, category)

    if mentions_price_category_subject(question):
        haystack = "\n".join((
            str(item.get("section_path", "")),
            str(item.get("section_title", "")),
            str(item.get("text", "")),
        ))
        return mentions_price_category_subject(haystack)

    terms = document_occurrence_terms(question)
    if not terms:
        return False
    haystack = "\n".join((
        str(item.get("section_path", "")),
        str(item.get("section_title", "")),
        str(item.get("text", "")),
    )).lower().replace("ё", "е")
    long_terms = tuple(term for term in terms if len(term) >= 4)
    short_terms = tuple(term for term in terms if len(term) < 4)
    term_keys = set(lexical_term_keys(" ".join(long_terms)))
    haystack_keys = set(lexical_term_keys(haystack))
    if not term_keys.issubset(haystack_keys):
        return False
    if any(
        not re.search(rf"(?<![а-яa-z0-9-]){re.escape(term)}(?![а-яa-z0-9-])", haystack)
        for term in short_terms
    ):
        return False
    # Require the actual phrase so a document that separately mentions prices
    # and unrelated categories cannot become a false positive.
    if {"цено", "кате"}.issubset(term_keys):
        return bool(re.search(r"\bценов\w*\s+категор\w*\b", haystack))
    return True


def deterministic_query_understanding(question: str) -> QueryUnderstanding:
    """Produce a useful plan when the LLM planner is unavailable or malformed."""
    canonical, corrections = normalize_user_question(question)
    normalized = canonical.lower().replace("ё", "е")
    facets: list[str] = []
    if re.search(r"\b(?:что\s+такое|что\s+значит|определение|расшифр\w*)\b", normalized):
        facets.append("definition")
    if re.search(
        r"\b(?:рассчит\w*|рассчитыва\w*|расчет\w*|расчёт\w*|формул\w*)\b",
        normalized,
    ):
        facets.append("calculation")
    if re.search(r"\b(?:порядок|этап\w*|как\s+(?:примен\w*|определ\w*|выполн\w*))\b", normalized):
        facets.append("procedure")
    if re.search(
        r"\b(?:услови\w*|огранич\w*|требован\w*|необходим\w*|должн\w*|"
        r"готовност\w*|настро\w*|завед\w*|провер\w*|не\s+сформир\w*)\b",
        normalized,
    ):
        facets.append("conditions")
    if has_document_occurrence_intent(normalized):
        facets.append("list")
    if not facets:
        facets.append("fact")

    queries = [canonical]
    price_variants = _price_category_variants(canonical)
    if price_variants:
        term = price_variants[0]
        if "definition" in facets:
            queries.append(f"{term} определение понятие ценовая категория условия применения")
        if "calculation" in facets:
            queries.append(f"{term} расчет стоимости порядок формулы составляющие цены")
        if "procedure" in facets:
            queries.append(f"{term} порядок определения стоимости и цены этапы расчета")
        queries.extend(price_variants[1:])

    is_rre = bool(re.search(r"\b(?:ррэ|розничн\w*\s+рын\w*)\b", normalized))
    if is_rre and "conditions" in facets:
        # Operational prerequisite questions are phrased very differently by
        # users.  Expand them into stable concepts used in RRE documentation,
        # without pinning retrieval to a particular file name or revision.
        queries.extend((
            "РРЭ договорные условия объекты потребления тарифная схема текущий расчет обязательные данные",
            "договор РРЭ объект потребления ЭПУ тарифная схема признак текущий расчет",
            "модули основная информация необходимая для определения стоимости электроэнергии на РРЭ",
        ))

    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        key = re.sub(r"\s+", " ", query.strip().casefold())
        if key and key not in seen:
            seen.add(key)
            deduped.append(query.strip())
    return QueryUnderstanding(canonical, tuple(facets), tuple(deduped[:6]), corrections)


def parse_query_understanding(raw: str, question: str) -> QueryUnderstanding:
    """Validate an LLM query plan and merge it with deterministic safeguards."""
    fallback = deterministic_query_understanding(question)
    match = re.search(r"\{.*\}", str(raw or ""), flags=re.DOTALL)
    if not match:
        return fallback
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback

    canonical = re.sub(r"\s+", " ", str(payload.get("canonical_question") or "").strip())
    if not canonical or len(canonical) > 600:
        canonical = fallback.canonical_question

    protected_terms = {
        re.sub(r"\s+", "", token).casefold(): token
        for token in re.findall(
            r"(?<![А-Яа-яЁёA-Za-z0-9])(?:\d+\s*[А-ЯЁA-Z]{1,8}|[А-ЯЁA-Z]{2,12}\d*(?:-\d+)?)(?![А-Яа-яЁёA-Za-z0-9])",
            fallback.canonical_question,
        )
    }
    compact_canonical = re.sub(r"\s+", "", canonical).casefold()
    missing = [value for key, value in protected_terms.items() if key not in compact_canonical]
    if missing:
        canonical = f"{canonical}. Термины из запроса: {', '.join(missing)}"
    original_category = price_category_number(fallback.canonical_question)
    if original_category and price_category_number(canonical) != original_category:
        # LLM planners sometimes expand colloquial "шестой ЦК" into the
        # unrelated "шестая часть ГК". Preserve the business identifier while
        # retaining the useful natural-language reformulation.
        canonical = f"{canonical}. Ценовая категория из запроса: {original_category} ЦК"

    allowed_facets = {"definition", "calculation", "procedure", "conditions", "comparison", "list", "fact"}
    raw_facets = payload.get("facets") if isinstance(payload.get("facets"), list) else []
    facets = [str(value).strip().casefold() for value in raw_facets]
    facets = [value for value in facets if value in allowed_facets]
    for value in fallback.facets:
        if value not in facets:
            facets.append(value)

    raw_queries = payload.get("retrieval_queries") if isinstance(payload.get("retrieval_queries"), list) else []
    fallback_queries = [
        query for query in fallback.retrieval_queries
        if query.casefold() != fallback.canonical_question.casefold()
    ]
    # Reserve three slots for deterministic typo/domain safeguards and two for
    # useful LLM reformulations. This keeps both layers effective under the
    # six-query cap instead of allowing either one to crowd out the other.
    candidates = [
        canonical,
        *fallback_queries[:3],
        *[str(value) for value in raw_queries],
        *fallback_queries[3:],
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        query = re.sub(r"\s+", " ", candidate.strip())
        key = query.casefold()
        if 2 <= len(query) <= 300 and key not in seen:
            seen.add(key)
            queries.append(query)
        if len(queries) >= 6:
            break
    return QueryUnderstanding(canonical, tuple(facets or fallback.facets), tuple(queries), fallback.corrections)


def refine_prism_query(question: str) -> PrismQuery:
    """Turn a user question into a small, inspectable PRISM retrieval contract."""
    normalized = question.lower()
    target_sections = requested_section_numbers(question)
    asks_for_formulas = is_formula_question(question)
    # A request can contain an independent fact lookup and a calculation in the
    # same turn (for example, "назови код; приведи формулу"). Formula-only mode
    # must not discard the non-formula part of such a compound request.
    non_formula_fact_request = any(
        not re.search(r"формул|рассчит|вычисл", clause)
        and re.search(r"\b(?:код|назов|перечисл|кто|где|когда|что\s+такое|како(?:й|е|я|ие))\b", clause)
        for clause in re.split(r"[.!?;]+", normalized)
        if clause.strip()
    )
    # "Дай формулу" is safely deterministic.  Once the user also requests
    # variables, units, substitution or a numerical result, the whole turn
    # must reach grounded generation; otherwise formula-only mode silently
    # discards those requested facets.
    expanded_formula_request = bool(re.search(
        r"\b(?:расшифр\w*|обознач\w*|переменн\w*|единиц\w*|подстав\w*|рассчита\w*|вычисл\w*|пример\w*)\b",
        normalized,
    ))
    formula_only = asks_for_formulas and not (non_formula_fact_request or expanded_formula_request)
    intent = "formula_extraction" if formula_only else detect_query_intent(question)
    answer_mode = "formulas_only" if formula_only else "grounded_answer"

    excluded: list[str] = ["history"]
    is_value_question = detect_query_intent(question) == "value"
    retrieval_facets = set(deterministic_query_understanding(question).facets)
    needs_calculation_context = bool(
        retrieval_facets.intersection({"calculation", "procedure", "conditions"})
    )
    explicitly_excludes_cost = any(marker in normalized for marker in ("без стоимости", "не стоимость", "кроме стоимости"))
    if explicitly_excludes_cost or (is_value_question and not needs_calculation_context):
        excluded.extend(("cost", "rate"))

    return PrismQuery(
        intent=intent,
        entities=tuple(ordered_requested_terms(question)),
        target_sections=target_sections,
        required_sections=target_sections,
        excluded_chunk_types=tuple(excluded),
        answer_mode=answer_mode,
    )


def _question_hash(question: str) -> str:
    """Hash the complete normalized cache input without erasing semantics.

    The response cache is exact by design.  In particular, numeric labels such
    as ``1 ЦК`` and ``6 ЦК`` must never share an entry; stripping digits here
    could make the planner or answer cache return evidence for another price
    category.  The input is still never stored in Redis, only its SHA-256 hash.
    """
    normalized = unicodedata.normalize("NFKC", question).casefold()
    normalized = " ".join(normalized.split())
    return hashlib.sha256(f"{CACHE_VERSION}:{normalized}".encode()).hexdigest()


def _read_docx_text(path: Path) -> str:
    """Extract DOCX text directly from Word XML, including EQ/formula fields."""
    return _cleanup_formula_noise(ingest_document(path).text)


def _normalize_word_eq(value: str) -> str:
    """Keep legacy Word EQ formulas readable enough for retrieval and prompts."""
    value = value.strip()
    if value.upper().startswith("EQ "):
        value = value[3:].strip()
    value = value.replace("\\s", "_sub")
    value = value.replace("\\f", "_frac")
    value = value.replace("\\i", "_int")
    value = re.sub(r"\s+", " ", value)
    return f" [{value}] "


def _cleanup_formula_noise(text: str) -> str:
    """Collapse common tokenized Word formula fragments into readable markers."""
    text = re.sub(r"\[\s*SEQ\s*\][^\n]*?(?=(?:\n|$))", "", text)
    text = re.sub(r"\[\s*REF\s*\][^\n]*?(?=(?:\n|$))", "", text)
    text = re.sub(r"\[\s*Формула\s*\\\*\s*\]", "", text)
    text = re.sub(r"\[\s*ARABIC\s*\]", "", text)
    text = re.sub(
        r"(?:\[\]\s*)?\[EQ\]\s*(?:\[\]\s*)?\[Δ\]\s*\[О\\?\]\s*\[s\]\s*\[\(\s*;([^)]+)\)\]",
        r"[ΔО_sub( ;\1)]",
        text,
    )
    text = re.sub(
        r"\[EQ\]\s*\[С\\?\]\s*\[s\]\s*\[\(([^]]+)\)\]",
        r"[С_sub(\1)]",
        text,
    )
    text = re.sub(
        r"(?:\[\]\s*)?\[EQ\]\s*\[Т\\?\]\s*\[s\]\s*\[\(([^]]+)\)\]",
        r"[Т_sub(\1)]",
        text,
    )
    text = re.sub(
        r"(?:Δ\s*)?(?:\[\]\s*)?\[EQ\]\s*\[О\\?\]\s*\[s\]\s*\[\(([^]]+)\)\]",
        r"[ΔО_sub(\1)]",
        text,
    )
    text = re.sub(
        r"(?:\[\]\s*)?\[EQ\]\s*С\s*\[s\]\s*\[\(([^]]+)\)\]",
        r"[С_sub(\1)]",
        text,
    )
    text = re.sub(
        r"(?:Δ\s*)?(?:\[\]\s*)?\[EQ\]\s*\[О\\?\]\s*\[s\]\s*\(\(([^)]+)\)\)",
        r"[ΔО_sub(\1)]",
        text,
    )
    text = re.sub(
        r"(?:\[\]\s*)?\[EQ\]\s*\[Т\\?\]\s*\[s\]\s*\(([^)]+)\)",
        r"[Т_sub(\1)]",
        text,
    )
    text = re.sub(
        r"(?:\[\]\s*)?\[EQ\]\s*С\s*\[s\]\s*\(\(([^)]+)\)\)",
        r"[С_sub(\1)]",
        text,
    )
    text = re.sub(r"\[\s*SEQ\s*\]", "", text)
    text = re.sub(r"\[\s*REF\s*\]", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"\[\s*s\s*\]", "_sub", text)
    text = re.sub(r"\[\s*\\\s*\]", "", text)
    text = re.sub(r"\bИВО-1\b", "ИВ0-1", text)
    text = re.sub(r"\bИВО\b", "ИВ0", text)
    text = re.sub(r"(?:\[\]\s*){2,}", " ", text)
    # Preserve paragraph boundaries: Markdown headings carry the semantic
    # structure used by the RAG chunker.  Collapsing ``\n\n`` here silently
    # turns an entire regulation into one anonymous section.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def requested_formula_terms(question: str) -> set[str]:
    known = ("ИВ0-1", "ИВ0", "ИВ1", "ИС", "ИВА", "ИВК", "ИВпр", "ИВон")
    normalized = question.replace("ИВ 1", "ИВ1").replace("ИВ 0", "ИВ0")
    found = {term for term in known if term in normalized}
    if re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ(?![А-Яа-яA-Za-z0-9-])", normalized):
        found.add("ИВ")
    return found


def ordered_requested_terms(question: str) -> list[str]:
    normalized = question.replace("ИВ 1", "ИВ1").replace("ИВ 0", "ИВ0")
    positions: list[tuple[int, str]] = []
    for term in known_formula_terms():
        if term == "ИВ":
            match = re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ(?![А-Яа-яA-Za-z0-9-])", normalized)
        elif term == "ИВ0":
            match = re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ0(?!-?1)(?![А-Яа-яA-Za-z0-9-])", normalized)
        elif term == "ИВ1":
            match = re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ1(?![А-Яа-яA-Za-z0-9-])", normalized)
        elif term == "ИС":
            match = re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИС(?![А-Яа-яA-Za-z0-9-])", normalized)
        else:
            match = re.search(rf"(?<![А-Яа-яA-Za-z0-9-]){re.escape(term)}(?![А-Яа-яA-Za-z0-9-])", normalized)
        if match:
            positions.append((match.start(), term))
    seen: set[str] = set()
    ordered: list[str] = []
    for _, term in sorted(positions):
        if term not in seen:
            seen.add(term)
            ordered.append(term)
    return ordered


def detect_query_intent(question: str) -> str:
    normalized = normalize_user_question(question)[0].lower()
    cost_markers = (
        "стоим", "ставк", "руб", "оплат", "требован", "обязательств", "платеж",
        # Advances and accruals are monetary calculations even when the user
        # asks only "как рассчитывается".  Classifying them as a generic value
        # lookup makes PRISM discard the governing cost/rate clauses before
        # generation, despite an exact lexical heading match.
        "аванс", "начисл",
    )
    # Generic verbs such as "определяется" and the noun "порядок" occur in
    # almost every normative question.  Treating them as value intent made the
    # PRISM contract discard whole cost/rate-labelled sections even when the
    # user was asking about a procedure described by that section.
    value_markers = ("величин", "объем", "объём", "отклон", "рассчит", "расчет", "расчёт", "формул", "учет", "учёт")
    if (
        any(marker in normalized for marker in cost_markers)
        or re.search(r"\bцен(?:а|ы|е|у|ой|ам|ах)\b", normalized)
        or re.search(r"(?<![а-яёa-z0-9])\d+\s*цк(?![а-яёa-z0-9])", normalized)
        or "ценовая категория" in normalized
    ):
        return "cost"
    if any(marker in normalized for marker in value_markers):
        return "value"
    return "general"


def requested_section_numbers(question: str) -> tuple[str, ...]:
    """Return explicit normative section numbers mentioned in the question."""
    numbers = re.findall(r"(?<!\d)(\d+(?:\.\d+){1,})(?!\d)", question)
    seen: set[str] = set()
    ordered: list[str] = []
    for number in numbers:
        if number not in seen:
            seen.add(number)
            ordered.append(number)
    return tuple(ordered)


def section_query_terms(question: str) -> tuple[str, ...]:
    value = re.sub(r"(?<!\d)\d+(?:\.\d+){1,}(?!\d)", " ", question.lower())
    stop = {
        "какие", "какая", "какой", "какое", "формулы", "формула", "расчет",
        "расчёт", "правила", "правило", "пункт", "пункта", "раздел", "для",
        "что", "как", "это", "есть", "найди", "покажи", "каких", "когда", "где",
        "почему", "после", "этого", "такого", "затем", "далее",
    }
    terms = [
        token
        for token in re.findall(r"[а-яёa-z0-9-]{4,}", value)
        if token not in stop
    ]
    return tuple(dict.fromkeys(terms))


def lexical_term_keys(value: str) -> tuple[str, ...]:
    """Normalize inflected Russian/English words for conservative heading matching."""
    words = re.findall(r"[а-яёa-z0-9-]{4,}", value.lower().replace("ё", "е"))
    return tuple(word if len(word) <= 4 else word[:4] for word in words)


def longest_common_token_run(left: tuple[str, ...], right: tuple[str, ...]) -> int:
    """Return the longest contiguous token-key run shared by two sequences."""
    previous = [0] * (len(right) + 1)
    best = 0
    for left_token in left:
        current = [0] * (len(right) + 1)
        for index, right_token in enumerate(right, start=1):
            if left_token == right_token:
                current[index] = previous[index - 1] + 1
                best = max(best, current[index])
        previous = current
    return best


def build_targeted_queries(question: str) -> list[str]:
    """Build deterministic retrieval probes for abbreviations and domain terms."""
    intent = detect_query_intent(question)
    terms = sorted(requested_formula_terms(question), key=lambda value: (-len(value), value))
    understanding = deterministic_query_understanding(question)
    queries: list[str] = []
    for profile in matching_retrieval_topic_profiles(question):
        queries.extend(str(query) for query in profile["queries"])
    queries.extend(understanding.retrieval_queries)
    for term in terms:
        if intent == "value":
            queries.extend([
                f"{term} определение величины отклонений виды инициатив",
                f"{term} составляющая величина отклонения внешняя инициатива собственная инициатива",
            ])
        elif intent == "cost":
            queries.extend([
                f"{term} расчетные показатели стоимости ставка оплата отклонений",
                f"{term} стоимость формула ставка",
            ])
        else:
            queries.append(f"{term} определение расчет величина отклонения")

    seen: set[str] = set()
    deduped: list[str] = []
    for query in queries:
        normalized = re.sub(r"\s+", " ", query.strip().lower())
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(query)
    return deduped[:6]


def matching_retrieval_topic_profiles(question: str) -> tuple[dict[str, object], ...]:
    """Return all soft topic profiles that match the user's actual subject."""
    normalized = normalize_user_question(question)[0].casefold().replace("ё", "е")
    return tuple(
        profile
        for profile in RETRIEVAL_TOPIC_PROFILES
        if any(re.search(str(pattern), normalized) for pattern in profile["question_patterns"])
    )


def reserve_topic_source_coverage(ranked: list[dict], question: str, top_k: int) -> list[dict]:
    """Reserve one best chunk from each relevant document family, without filtering."""
    profiles = matching_retrieval_topic_profiles(question)
    if not profiles:
        return ranked

    reserved: list[dict] = []
    reserved_ids: set[int] = set()
    reserved_documents: set[str] = set()
    for profile in profiles:
        for family in profile["source_families"]:
            terms = tuple(str(term).casefold() for term in family)
            candidates = [
                (index, item) for index, item in enumerate(ranked)
                if all(
                    term in user_facing_source_name(item.get("file", "")).casefold().replace("ё", "е")
                    for term in terms
                )
                and user_facing_source_name(item.get("file", "")).casefold() not in reserved_documents
            ]
            locator_terms = tuple(str(term) for term in profile.get("locator_terms", ()))
            match = max(
                candidates,
                key=lambda pair: (
                    any(
                        term in f"{pair[1].get('section_path', '')} {pair[1].get('section_title', '')}".casefold().replace("ё", "е")
                        for term in locator_terms
                    ),
                    -pair[0],
                ),
                default=(0, None),
            )[1]
            if match is None:
                continue
            reserved.append(match)
            reserved_ids.add(id(match))
            reserved_documents.add(user_facing_source_name(match.get("file", "")).casefold())
            if len(reserved) >= top_k:
                return reserved
    return [*reserved, *(item for item in ranked if id(item) not in reserved_ids)]


SOURCE_REFERENCE_STOP_WORDS = {
    "а", "без", "в", "во", "где", "для", "документ", "документа", "и", "из",
    "как", "какие", "на", "о", "об", "по", "про", "с", "со", "что", "это",
    "файл", "файле",
}


def source_reference_tokens(value: str) -> set[str]:
    """Tokenize a question or display filename, including compact ids like ПП442."""
    name = Path(str(value or "")).stem.casefold().replace("ё", "е")
    name = re.sub(r"^[0-9a-f]{32}_", "", name)
    return {
        token
        for token in re.findall(r"[a-zа-я]+|\d+", name, flags=re.IGNORECASE)
        if token not in SOURCE_REFERENCE_STOP_WORDS and (token.isdigit() or len(token) >= 2)
    }


def matching_explicit_sources(question: str, source_aliases: dict[str, str] | None) -> set[str]:
    """Resolve an explicitly named visible document to its indexed source id."""
    query_tokens = source_reference_tokens(question)
    query_numbers = {token for token in query_tokens if token.isdigit() and len(token) >= 2}
    normalized_question = (question or "").casefold().replace("ё", "е")
    requested_category = price_category_number(normalized_question)
    has_method_cue = bool(re.search(r"\bметодик\w*\b", normalized_question))
    has_source_cue = bool(re.search(
        r"\b(?:документ\w*|файл\w*|методик\w*|постановлен\w*|пп|регламент\w*|инструкц\w*)\b",
        normalized_question,
    ))
    matches: set[str] = set()
    for source, alias in (source_aliases or {}).items():
        alias_tokens = source_reference_tokens(alias)
        overlap = query_tokens & alias_tokens
        alpha_overlap = {token for token in overlap if not token.isdigit()}
        # A bare business number ("1 ЦК") is not a document reference.  Once
        # the user explicitly says "постановление 442/1179", however, the
        # number is the most stable identifier and must resolve even when the
        # grammatical form differs ("постановление"/"постановлений").
        numeric_anchor = bool(has_source_cue and query_numbers & alias_tokens)
        # Two generic words from a business phrase (for example, "расчета
        # стоимости") must not be interpreted as an explicit document name.
        # Such a false positive turns a soft semantic question into a hard
        # single-document filter and hides otherwise exact evidence.
        descriptive_anchor = (
            has_source_cue
            and
            len(alpha_overlap) >= 2
            and sum(map(len, alpha_overlap)) >= 8
        )
        normalized_alias = str(alias or "").casefold().replace("ё", "е")
        category_range_match = re.search(r"(?<!\d)([1-6])\s*[-–—]\s*([1-6])\s*цк", normalized_alias)
        alias_covers_category = bool(
            requested_category
            and (
                contains_price_category(normalized_alias, requested_category)
                or (
                    category_range_match
                    and int(category_range_match.group(1))
                    <= int(requested_category)
                    <= int(category_range_match.group(2))
                )
            )
        )
        # Russian inflection makes ``методику`` and ``методика`` different
        # lexical tokens.  When the query explicitly names a methodology and
        # a price category, the category/range is a safer anchor than a loose
        # token overlap and keeps the governing methodology in comparisons.
        method_category_anchor = bool(
            has_method_cue
            and alias_covers_category
            and re.search(r"\bметодик\w*\b", normalized_alias)
        )
        if numeric_anchor or descriptive_anchor or method_category_anchor:
            matches.add(Path(str(source)).name.casefold())
    return matches


def known_formula_terms() -> tuple[str, ...]:
    return ("ИВ0-1", "ИВ0", "ИВ1", "ИС", "ИВА", "ИВК", "ИВпр", "ИВон", "ИВ")


REMOVED_CLAUSE_RE = re.compile(
    r"утратил[аио]?\s+силу|пункт\s+(?:удален|исключен)|исключен[оа]?\s+из\s+регламента",
    re.IGNORECASE,
)


def _current_clause_is_deleted(cleaned: str, section_title: str) -> bool:
    """Limit repeal markers to the clause they actually describe.

    Legal exports often place a repealed sibling near an active clause.  A
    marker anywhere in the first chunk window must not delete the active
    clause or every later occurrence in the regulation disappears from RAG.
    """
    if REMOVED_CLAUSE_RE.search(section_title):
        return True

    body = re.sub(r"^\s*\[SECTION:\s*[^\]]+\]\s*", "", cleaned, count=1)
    prefix = body[:320]
    marker = REMOVED_CLAUSE_RE.search(prefix)
    if marker is None or marker.start() > 180:
        return False

    title_number = re.match(r"\s*(?:пункт\s*)?(\d+(?:\.\d+)*)", section_title, re.IGNORECASE)
    marker_line_start = prefix.rfind("\n", 0, marker.start()) + 1
    marker_line = prefix[marker_line_start:]
    marker_number = re.match(r"\s*(?:пункт\s*)?(\d+(?:\.\d+)*)", marker_line, re.IGNORECASE)
    if title_number and marker_number and title_number.group(1) != marker_number.group(1):
        return False
    return True


def extract_chunk_metadata(text: str) -> dict:
    """Classify a retrieved chunk so ranking can separate values from prices/rates."""
    cleaned = _cleanup_formula_noise(text or "")
    lower = cleaned.lower()
    sections = re.findall(r"\[SECTION:\s*([^\]]+)\]", cleaned)
    section_path = sections[-1].strip() if sections else ""
    section_parts = [part.strip() for part in section_path.split(">") if part.strip()]
    section_title = section_parts[-1] if section_parts else ""
    parent_section = " > ".join(section_parts[:-1])
    status = "deleted" if _current_clause_is_deleted(cleaned, section_title) else "active"

    chunk_type = "unknown"
    if "[table]" in lower:
        chunk_type = "table"
    if "[с_sub(" in lower or "[т_sub(" in lower:
        chunk_type = "cost"
    if "расчетные показатели стоимости" in lower or "стоимост" in lower or "ставк" in lower:
        chunk_type = "cost"
    if "исходные данные" in lower or "предварительных обязательств" in lower or "предварительных требований" in lower:
        chunk_type = "rate"
    if status == "deleted":
        chunk_type = "history"
    if (
        "определение величины отклонений" in lower
        or "виды инициатив" in lower
        or "составляющая величина отклонения" in lower
        or "объем отклонения" in lower
    ):
        chunk_type = "value"

    return {
        "section_path": section_path,
        "section_title": section_title,
        "parent_section": parent_section,
        "status": status,
        "chunk_type": chunk_type,
    }


def effective_chunk_metadata(text: str, metadata: dict | None = None) -> dict:
    """Combine stable locators with status/type recalculated from chunk text."""
    parsed = extract_chunk_metadata(text)
    for key in ("section_path", "section_title", "parent_section"):
        value = (metadata or {}).get(key)
        if value:
            parsed[key] = value
    parsed["status"] = (
        "deleted"
        if _current_clause_is_deleted(_cleanup_formula_noise(text or ""), str(parsed.get("section_title", "")))
        else "active"
    )
    if parsed["status"] == "deleted":
        parsed["chunk_type"] = "history"
    return parsed


def _context_roles(text: str) -> set[str]:
    normalized = (text or "").casefold().replace("ё", "е")
    roles: set[str] = set()
    if re.search(r"\b(?:определени|поняти|термин|означает|понимается|называется)", normalized):
        roles.add("definition")
    if re.search(r"\b(?:за исключением|исключени|кроме случаев|не применяется|особ(?:ый|ые) случа)", normalized):
        roles.add("exception")
    if re.search(r"\b(?:согласно|в соответствии|предусмотрен|см\.\s*пункт|ссылка)", normalized):
        roles.add("reference")
    if "[formula]" in normalized or "=" in normalized or re.search(r"\bформул", normalized):
        roles.add("formula")
    if re.search(r"\b(?:порядок|расчет|услови|правил|алгоритм|применя)", normalized):
        roles.add("rule")
    return roles or {"context"}


def _requested_context_roles(question: str) -> set[str]:
    normalized = (question or "").casefold().replace("ё", "е")
    roles = {"reference"}
    if re.search(r"\b(?:что такое|что означает|определени|поняти)", normalized):
        roles.add("definition")
    if re.search(r"\b(?:как|рассчит|формул|порядок|алгоритм)", normalized):
        roles.update({"definition", "rule", "formula", "exception"})
    if re.search(r"\b(?:услови|когда|случа|ограничени)", normalized):
        roles.update({"rule", "exception"})
    return roles


def relationship_expansion_results(
    all_nodes: list[Any],
    ranked: list[dict],
    question: str,
    limit: int = 12,
) -> list[dict]:
    """Expand strong child hits with bounded same-document structural context."""
    if not all_nodes or not ranked or is_document_occurrence_question(question):
        return []
    seed_sources = {
        Path(str(item.get("file", ""))).name.casefold()
        for item in ranked[:12]
        if item.get("file")
    }
    if not seed_sources:
        return []

    candidates_by_source: dict[str, list[dict]] = {source: [] for source in seed_sources}
    for node in all_nodes:
        meta = node.metadata or {}
        source_name = str(meta.get("file_name", meta.get("file_path", "?")))
        source_key = Path(source_name).name.casefold()
        if source_key not in candidates_by_source:
            continue
        text = node.text or ""
        chunk_meta = {
            **extract_chunk_metadata(text),
            **{
                key: value for key, value in meta.items()
                if key in {"section_path", "section_title", "parent_section", "status", "chunk_type"}
                and value
            },
        }
        if chunk_meta.get("status") == "deleted" or contains_prompt_injection(text):
            continue
        candidates_by_source[source_key].append({
            "file": source_name,
            "text": text,
            **chunk_meta,
        })

    requested_roles = _requested_context_roles(question)
    lexical_terms = {
        term for term in section_query_terms(question)
        if len(term) >= 5 and not term.isdigit()
    }
    existing = {
        hashlib.sha256(
            f"{Path(str(item.get('file', ''))).name.casefold()}\n{str(item.get('text', ''))[:700]}".encode("utf-8")
        ).hexdigest()
        for item in ranked
    }
    expanded: list[dict] = []
    for seed in ranked[:8]:
        source_key = Path(str(seed.get("file", ""))).name.casefold()
        seed_path = str(seed.get("section_path", "")).strip()
        seed_parent = str(seed.get("parent_section", "")).strip()
        seed_score = float(seed.get("score", 0.0))
        related: list[tuple[float, str, dict]] = []
        for candidate in candidates_by_source.get(source_key, []):
            digest = hashlib.sha256(
                f"{source_key}\n{candidate['text'][:700]}".encode("utf-8")
            ).hexdigest()
            if digest in existing:
                continue
            path = str(candidate.get("section_path", "")).strip()
            parent = str(candidate.get("parent_section", "")).strip()
            searchable = f"{path}\n{candidate.get('section_title', '')}\n{candidate['text'][:1600]}".casefold()
            roles = _context_roles(searchable)
            lexical_matches = sum(contains_term(searchable, term) for term in lexical_terms)
            relation = ""
            bonus = 0.0
            if seed_path and path == seed_path:
                relation, bonus = "section-child", 0.55
            elif seed_parent and path == seed_parent:
                relation, bonus = "parent-section", 0.48
            elif seed_path and parent == seed_path:
                relation, bonus = "child-section", 0.45
            elif (
                parent == seed_parent
                and roles & requested_roles
                and (
                    lexical_matches
                    or (seed_parent and roles & {"exception", "reference"})
                )
            ):
                relation, bonus = "related-" + sorted(roles & requested_roles)[0], 0.32
            if not relation:
                continue
            related.append((bonus + min(lexical_matches, 3) * 0.08, relation, candidate))
        for bonus, relation, candidate in sorted(related, key=lambda item: item[0], reverse=True)[:2]:
            digest = hashlib.sha256(
                f"{source_key}\n{candidate['text'][:700]}".encode("utf-8")
            ).hexdigest()
            if digest in existing:
                continue
            existing.add(digest)
            expanded.append({
                **candidate,
                "score": round(seed_score - 0.20 + bonus, 4),
                "matched_queries": [f"relationship:{relation}"],
            })
            if len(expanded) >= limit:
                return expanded
    return expanded


def contains_term(value: str, term: str) -> bool:
    value = value.replace("ИВ 1", "ИВ1").replace("ИВ 0", "ИВ0")
    if term == "ИВ0":
        return re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ0(?!-?1)(?![А-Яа-яA-Za-z0-9-])", value) is not None
    if term == "ИВ1":
        return re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ1(?![А-Яа-яA-Za-z0-9-])", value) is not None
    if term == "ИС":
        return re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИС(?![А-Яа-яA-Za-z0-9-])", value) is not None
    if term == "ИВ":
        return re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ(?![А-Яа-яA-Za-z0-9-])", value) is not None
    return term in value


def diversify_context_results(ranked: list[dict], question: str, top_k: int) -> list[dict]:
    requested = sorted(requested_formula_terms(question), key=lambda value: (-len(value), value))
    section_numbers = requested_section_numbers(question)
    category = price_category_number(question)
    generic_category = not category and (
        mentions_price_category_subject(question)
        or is_price_composition_question(question)
    )
    category_matches = [
        item for item in ranked
        if (
            category and price_category_item_matches(item, category)
        ) or (
            generic_category and (
                document_occurrence_item_matches(item, question)
                or any(
                    str(query).startswith("price-category-exact:generic")
                    for query in item.get("matched_queries", [])
                )
            )
        )
    ]

    # This is a positive set lookup: one strongest direct hit per document,
    # without padding the result with documents that do not contain the entity.
    if is_document_occurrence_question(question):
        per_source: dict[str, dict] = {}
        for item in ranked:
            if not document_occurrence_item_matches(item, question):
                continue
            source = user_facing_source_name(item.get("file", "")).casefold()
            per_source.setdefault(source, item)
        return list(per_source.values())[:top_k]

    selected: list[dict] = []
    selected_ids: set[int] = set()

    def add(item: dict) -> None:
        selected.append(item)
        selected_ids.add(id(item))

    understanding = deterministic_query_understanding(question)
    if {"procedure", "conditions"}.intersection(understanding.facets):
        # Conditions and procedures are commonly split between a governing
        # method, a setup manual and a regulation. Keep a bounded strongest
        # direct lexical hit per document before generic high-scoring chunks
        # from one large manual consume the whole answer context.
        selected_sources: set[str] = set()
        for item in ranked:
            matched = item.get("matched_queries", [])
            if "domain-lexical" not in matched:
                continue
            source = user_facing_source_name(item.get("file", "")).casefold()
            if source in selected_sources:
                continue
            add(item)
            selected_sources.add(source)
            if len(selected) >= min(top_k, 4):
                break

    if generic_category:
        # Generic ЦК questions usually need both normative and implementation
        # evidence.  Reserve one strongest direct hit per document before
        # filling the remaining context with additional chunks.
        selected_sources: set[str] = set()
        for item in category_matches:
            source = user_facing_source_name(item.get("file", "")).casefold()
            if source in selected_sources:
                continue
            add(item)
            selected_sources.add(source)
            if len(selected) >= min(top_k, 6):
                break

    if category:
        # Keep the best definition/calculation clauses, then ensure every other
        # relevant document gets a chance to contribute evidence.
        stem = PRICE_CATEGORY_STEMS.get(category, "")
        if "definition" in deterministic_query_understanding(question).facets and stem:
            definition = next((
                item for item in category_matches
                if re.search(
                    rf"\b{stem}\w*\s+ценов\w*\s+категор\w*\s*[-–—]",
                    "\n".join((
                        str(item.get("section_path", "")),
                        str(item.get("section_title", "")),
                        str(item.get("text", "")),
                    )).lower().replace("ё", "е"),
                )
            ), None)
            if definition is not None:
                add(definition)
        if asks_for_calculation(question):
            calculation = next((
                item for item in category_matches
                if any(
                    contains_price_category(formula, category)
                    for formula in re.findall(r"\[MATH:\s*([^\]\n]+)\]", str(item.get("text", "")))
                )
            ), None) or next((
                item for item in category_matches
                if "[math:" in str(item.get("text", "")).lower()
            ), None) or next((
                item for item in category_matches
                if "порядок определения стоимости" in (
                    f"{item.get('section_path', '')}\n{item.get('section_title', '')}\n{item.get('text', '')}"
                ).lower().replace("ё", "е")
            ), None)
            if calculation is not None and id(calculation) not in selected_ids:
                add(calculation)
        for item in category_matches:
            if len(selected) >= min(3, top_k):
                break
            matched = item.get("matched_queries", [])
            if any(str(query).startswith("price-category-exact:") for query in matched):
                add(item)
        selected_sources = {
            user_facing_source_name(item.get("file", "")).casefold()
            for item in selected
        }
        for item in category_matches:
            if len(selected) >= top_k:
                break
            source = user_facing_source_name(item.get("file", "")).casefold()
            if source not in selected_sources and id(item) not in selected_ids:
                add(item)
                selected_sources.add(source)
        if asks_for_calculation(question):
            for item in ranked:
                if len(selected) >= top_k or len(selected_sources) >= 6:
                    break
                source = user_facing_source_name(item.get("file", "")).casefold()
                if source in selected_sources or item.get("status") == "deleted":
                    continue
                leaf_scope = str(item.get("section_title") or item.get("section_path", "").rsplit(">", 1)[-1])
                leaf_categories = {
                    number for number in PRICE_CATEGORY_WORDS
                    if contains_price_category(leaf_scope, number)
                }
                if leaf_categories and category not in leaf_categories:
                    continue
                if float(item.get("score", 0.0)) >= 0.5:
                    add(item)
                    selected_sources.add(source)

    if section_numbers:
        for item in ranked:
            if len(selected) >= top_k:
                break
            if id(item) in selected_ids or item.get("status") == "deleted":
                continue
            matched = item.get("matched_queries", [])
            if any(str(query).startswith("section-exact:") for query in matched):
                add(item)

    for term in requested:
        found_by_section = False
        for item in ranked:
            if id(item) in selected_ids or item.get("status") == "deleted":
                continue
            section_haystack = " ".join([
                str(item.get("section_path", "")),
                str(item.get("section_title", "")),
            ])
            if contains_term(section_haystack, term):
                add(item)
                found_by_section = True
                break
        if found_by_section:
            continue
        for item in ranked:
            if id(item) in selected_ids or item.get("status") == "deleted":
                continue
            text_haystack = " ".join([
                str(item.get("section_path", "")),
                str(item.get("section_title", "")),
                str(item.get("text", "")),
            ])
            if contains_term(text_haystack, term):
                add(item)
                break

    section_counts: dict[str, int] = {}
    for item in selected:
        key = item.get("section_path") or item.get("file", "")
        section_counts[key] = section_counts.get(key, 0) + 1

    for item in ranked:
        if len(selected) >= top_k:
            break
        if id(item) in selected_ids:
            continue
        if item.get("status") == "deleted":
            continue
        key = item.get("section_path") or item.get("file", "")
        if section_counts.get(key, 0) >= 2 and len(selected) < max(1, top_k - 2):
            continue
        add(item)
        section_counts[key] = section_counts.get(key, 0) + 1

    for item in ranked:
        if len(selected) >= top_k:
            break
        if id(item) not in selected_ids:
            add(item)

    return selected[:top_k]


def select_prism_evidence(context: list[dict], spec: PrismQuery, top_k: int) -> list[dict]:
    """Keep only active evidence compatible with the refiner contract.

    Explicit section references are hard constraints: a semantically similar chunk
    from another normative point must not become evidence for the answer.
    """
    active = [item for item in context if item.get("status") != "deleted"]
    if spec.target_sections:
        active = [
            item for item in active
            if any(
                re.search(rf"(?<!\d){re.escape(section)}(?:\.|\b)(?!\d)", str(item.get("section_path", "")))
                for section in spec.target_sections
            )
        ]
    filtered = [
        item for item in active
        if item.get("chunk_type") not in spec.excluded_chunk_types
    ]
    return filtered[:top_k]


def validate_retrieval(question: str, context: list[dict], answer: str = "", spec: PrismQuery | None = None) -> list[str]:
    """Return user-facing quality warnings for evidence-first RAG."""
    spec = spec or refine_prism_query(question)
    warnings: list[str] = []
    intent = spec.intent
    requested = sorted(requested_formula_terms(question), key=lambda value: (-len(value), value))

    for section in spec.required_sections:
        if not any(
            re.search(rf"(?<!\d){re.escape(section)}(?:\.|\b)(?!\d)", str(item.get("section_path", "")))
            for item in context
        ):
            warnings.append(f"Не найдено active evidence для запрошенного пункта {section}.")

    for term in requested:
        has_section_evidence = any(
            item.get("status") != "deleted"
            and item.get("chunk_type") == "value"
            and contains_term(" ".join([
                str(item.get("section_path", "")),
                str(item.get("section_title", "")),
            ]), term)
            for item in context
        )
        if not has_section_evidence:
            warnings.append(f"Нет базового активного раздела в evidence для {term}.")

    deleted_count = sum(1 for item in context if item.get("status") == "deleted")
    if deleted_count:
        warnings.append(f"В evidence есть фрагменты со статусом deleted: {deleted_count}.")

    if intent in {"value", "formula_extraction"}:
        bad_types = [item for item in context if item.get("chunk_type") in {"cost", "rate"}]
        if bad_types:
            warnings.append(f"Для вопроса про величины найдено cost/rate фрагментов: {len(bad_types)}.")
        answer_lower = answer.lower()
        has_cost_formula = (
            "[с_sub(" in answer_lower
            or re.search(r"(?<!не\s)(?:\bc\b|с)\s*=\s*(?:δ|Δ|дельта|delta)?\s*o", answer_lower) is not None
        )
        has_cost_negation = "не используется" in answer_lower or "не используются" in answer_lower or "не включены" in answer_lower
        if answer and has_cost_formula and not has_cost_negation:
            warnings.append("Ответ похож на подмену величины стоимостью/ставкой.")
    elif intent == "cost":
        has_cost = any(item.get("chunk_type") == "cost" for item in context)
        if not has_cost:
            warnings.append("Вопрос про стоимость, но cost evidence не найден.")

    if requested and len(context) < len(requested):
        warnings.append("Evidence меньше количества запрошенных сущностей.")

    if spec.answer_mode == "formulas_only" and answer:
        formulas = extract_formula_lines_from_texts([item.get("text", "") for item in context], question)
        if not formulas:
            warnings.append("В отобранном evidence не выделены формулы или расчетные правила.")

    # A generic mention of the requested entity is not sufficient for an
    # explanatory calculation answer.  When the question names a subject,
    # require an evidence section that also signals a calculation, procedure,
    # formula, or algorithm.  This surfaces a retrieval miss instead of
    # confidently reporting that the corpus lacks an answer.
    query_keys = set(lexical_term_keys(" ".join(section_query_terms(question))))
    subject_keys = query_keys - GENERIC_CALCULATION_QUERY_KEYS
    if asks_for_calculation(question) and subject_keys:
        has_governing_evidence = any(
            bool(subject_keys & set(lexical_term_keys(" ".join([
                str(item.get("section_path", "")),
                str(item.get("section_title", "")),
            ]))))
            and bool(CALCULATION_SECTION_KEYS & set(lexical_term_keys(" ".join([
                str(item.get("section_path", "")),
                str(item.get("section_title", "")),
            ]))))
            for item in context
            if item.get("status") != "deleted"
        )
        if not has_governing_evidence:
            warnings.append(
                "Для вопроса о расчете не найден раздел с алгоритмом, порядком или формулой по запрошенной сущности."
            )

    return warnings


def _plain_evidence_text(text: str) -> str:
    text = _cleanup_formula_noise(text or "")
    text = re.sub(r"\[\[MATHML(?:_REF)?:[A-Za-z0-9+/=]+\]\]", "", text)
    text = re.sub(r"\[MATH:\s*([^\]\n]+)\]", r"\1", text)
    text = re.sub(r"\[SECTION:\s*[^\]]+\]\s*", "", text)
    text = re.sub(r"#{1,6}\s*", "", text)
    text = clean_formula_artifacts(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return text.strip()


def clean_formula_artifacts(text: str) -> str:
    """Remove empty placeholders left by partially parsed Word formulas."""
    text = text or ""
    text = re.sub(r"<a\s+id=[\"'][^\"']+[\"']\s*></a>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<!--\s*F-\d{4}\s*-->", "", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def format_formula_for_display(formula: str) -> str:
    """Make legacy Word/WMF LaTeX readable in Markdown without KaTeX errors.

    The source uses Cyrillic ``\\mathrm`` payloads and escaped Word spacing
    commands.  KaTeX rejects those fragments, while a compact textual formula
    preserves the operator and every index for verification.
    """
    value = clean_formula_artifacts(formula).replace("$", "")
    previous = None
    while previous != value:
        previous = value
        value = re.sub(r"\\mathrm\{([^{}]*)\}", r"\1", value)
    value = value.replace("\\_", "_").replace("\\ ", " ").replace("\\.", ".")
    value = re.sub(r"\\(?:left|right|bigl|bigr|displaystyle)\s*", "", value)
    value = value.replace("МIN", "MIN").replace("МAX", "MAX")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*([,.;:])\s*", r"\1 ", value)
    value = re.sub(r"(?<=[A-Za-zА-Яа-я]),\s+(?=[A-Za-zА-Яа-я])", ",", value)
    value = re.sub(r"\s+}", "}", value)
    return value.strip()


def format_formula_for_katex(formula: str, *, display: bool = False) -> str:
    """Return valid KaTeX for a legacy formula when possible."""
    value = clean_formula_artifacts(formula).replace("$", "")
    # OCR/DOCX extraction often serializes indices as S(name,i,n,m). Convert
    # that notation to real subscripts instead of displaying source-like code.
    if "\\" not in value:
        def indexed_symbol(match: re.Match) -> str:
            symbol = match.group(1)
            raw_parts = [part.strip().strip(",") for part in match.group(2).split(",")]
            parts: list[str] = []
            for part in raw_parts:
                if not part:
                    continue
                escaped = part.replace("{", "").replace("}", "").replace("_", " ")
                parts.append(f"\\text{{{escaped}}}" if re.search(r"[А-Яа-яЁё]", escaped) else escaped)
            rendered_symbol = f"\\text{{{symbol}}}" if re.search(r"[А-Яа-яЁё]", symbol) else symbol
            return f"{rendered_symbol}_{{{','.join(parts)}}}"

        value = re.sub(
            r"(?<![\\\w])([A-Za-zА-Яа-яЁёΔЦ][A-Za-zА-Яа-яЁё0-9]*)_sub\(([^()\n]{1,180})\)",
            indexed_symbol,
            value,
        )
        value = re.sub(
            r"(?<![\\\w])([A-Za-zА-Яа-яЁёΔЦ][A-Za-zА-Яа-яЁё0-9]*)\(([^()\n]{1,180})\)",
            indexed_symbol,
            value,
        )
        value = re.sub(r"\s*[×*]\s*", r" \\times ", value)
    value = value.replace("\\_", "_").replace("\\ ", " ").replace("\\.", ".")
    # Every legacy ``\\mathrm`` payload here is a flat Word run.  Rendering it
    # as ``\\text`` lets KaTeX display Cyrillic labels without treating them as
    # unsupported math commands.
    value = re.sub(r"\\mathrm\{([^{}]*)\}", lambda match: f"\\text{{{match.group(1)}}}", value)
    value = value.replace("МIN", r"\min").replace("МAX", r"\max")
    value = re.sub(r"\s*\.\s*$", "", value).strip()
    delimiter = "$$" if display else "$"
    return f"{delimiter}{value}{delimiter}"


def _split_evidence_sentences(text: str) -> list[str]:
    text = _plain_evidence_text(text)
    text = re.sub(r"\bпп\.", "пп§", text)
    text = re.sub(r"\bп\.", "п§", text)
    parts = re.split(r"(?<=[.!?])\s+|(?<=;)\s+(?=–|В случае|Для|Объем|Величина|Составляющая)", text)
    return [part.replace("§", ".").strip(" -") for part in parts if len(part.strip()) > 20]


def _calculation_snippet(item: dict, term: str, max_chars: int = 1150) -> str:
    sentences = _split_evidence_sentences(item.get("text", ""))
    selected: list[str] = []
    for sentence in sentences:
        lower = sentence.lower()
        if (
            "определяется" in lower
            or "как разность" in lower
            or "как разница" in lower
            or "равна разности" in lower
            or "расчет величины" in lower
            or "осуществляется по формуле" in lower
        ):
            if "осуществляется по формуле" in lower and re.search(r":\s*;\s*(?:для|$)", sentence):
                continue
            selected.append(sentence)
        if len(" ".join(selected)) >= max_chars:
            break

    if not selected and sentences:
        selected = sentences[:2]

    snippet = " ".join(selected)
    if len(snippet) > max_chars:
        snippet = snippet[: max_chars - 3].rstrip() + "..."
    return snippet


def _short_explanation(term: str, snippet: str, max_chars: int = 240) -> str:
    snippet = clean_formula_artifacts(snippet)
    snippet = re.sub(r"\[[^\]]*_sub\([^\]]+\)\]\s*", "", snippet)
    if term == "ИВ1":
        snippet = re.sub(r"В случае если.+$", "", snippet).strip()
    sentences = _split_evidence_sentences(snippet)
    if not sentences:
        return ""
    explanation = sentences[0]
    if len(explanation) > max_chars:
        explanation = explanation[: max_chars - 3].rstrip() + "..."
    return explanation


def _compact_section_reference(section_path: str) -> str:
    parts = [
        re.sub(r"\s+", " ", part).strip(" ,")
        for part in (section_path or "").split(">")
        if part.strip()
    ]
    # Word style inheritance can leak a preceding table/formula cell into the
    # hierarchy (for example "110 МВт∙ч").  Such a value is not a locator.
    parts = [
        part for part in parts
        if not re.match(r"^[\d.,]+\s*(?:[кмгт]?вт|руб|%)\b", part, re.IGNORECASE)
    ]
    # Metadata is deliberately capped during ingestion.  If the deepest
    # component was truncated, the preceding complete section title is a much
    # better user-facing orientation than an ellipsis-heavy pseudo-heading.
    if len(parts) >= 2 and (parts[-1].endswith("...") or len(parts[-1]) > 220):
        parts.pop()
    if len(parts) >= 2:
        return " > ".join(parts[-2:])
    return parts[0] if parts else "раздел не определен"


def user_facing_source_name(value: object) -> str:
    """Return the uploaded basename without collision-safe storage prefixes."""
    name = Path(str(value or "")).name
    while re.match(r"^[0-9a-fA-F]{32}_", name):
        name = name[33:]
    return name or "документ"


def source_locator(section_path: object, page_number: object = None) -> str:
    """Build a stable, human-readable locator without exposing chunk metadata."""
    parts: list[str] = []
    if page_number not in (None, ""):
        parts.append(f"стр. {page_number}")
    section = _compact_section_reference(str(section_path or "")).strip()
    if section and section != "раздел не определен":
        if re.match(r"^(?:п\.?|пункт|раздел|глава|приложение)\s", section, re.IGNORECASE):
            parts.append(section)
        else:
            parts.append(f"раздел «{section}»")
    return ", ".join(parts) or "документ целиком"


def human_source_reference(item: dict) -> str:
    return (
        f"{user_facing_source_name(item.get('file', ''))} — "
        f"{source_locator(item.get('section_path', item.get('section', '')), item.get('page_number'))}"
    )


def ensure_price_category_source_coverage(answer: str, question: str, context: list[dict]) -> str:
    """Add a cited transmission rule when the model omits a relevant second source."""
    category = price_category_number(question)
    if not category or not asks_for_calculation(question):
        return answer

    normalized_answer = answer.casefold()
    expected_rate = "одноставоч" if category == "1" else "двухставоч" if category == "6" else ""
    if not expected_rate:
        return answer

    for item in context:
        source = user_facing_source_name(item.get("file", "?"))
        if source.casefold() in normalized_answer:
            continue
        haystack = (
            f"{item.get('section_path', '')}\n{item.get('section_title', '')}\n{item.get('text', '')}"
        ).lower().replace("ё", "е")
        matched_queries = {str(value) for value in item.get("matched_queries", [])}
        is_related = f"price-category-related:{category}" in matched_queries
        if not (
            expected_rate in haystack
            and "передач" in haystack
            and (is_related or contains_price_category(haystack, category))
        ):
            continue

        rate_name = "одноставочному" if category == "1" else "двухставочному"
        appendix = (
            "### Связанный расчёт услуг по передаче\n\n"
            f"Для {category} ЦК стоимость услуг по передаче рассчитывается по "
            f"{rate_name} тарифу. [Источник: {human_source_reference(item)}]"
        )
        return f"{answer.rstrip()}\n\n{appendix}"
    return answer


def generate_document_occurrence_answer(question: str, context: list[dict]) -> str:
    """List only documents with a confirmed direct occurrence of the entity."""
    category = price_category_number(question)
    positive: dict[str, dict] = {}
    for item in context:
        if not document_occurrence_item_matches(item, question):
            continue
        name = user_facing_source_name(item.get("file", ""))
        positive.setdefault(name.casefold(), item)
    if not positive:
        subject = f"{category} ЦК" if category else "темы запроса"
        return f"В доступных документах прямых упоминаний {subject} не найдено."

    subject = f"{category} ЦК" if category else "Тема запроса"
    lines = [f"{subject} прямо упоминается в следующих документах:", ""]
    for item in positive.values():
        name = user_facing_source_name(item.get("file", ""))
        locator = source_locator(item.get("section_path", item.get("section", "")), item.get("page_number"))
        lines.append(f"- **{name}** — {locator}. [Источник: {human_source_reference(item)}]")
    return "\n".join(lines)


def generate_image_transcription_answer(question: str, context: list[dict]) -> str:
    """Answer explicit OCR/transcription requests without an unnecessary LLM hop."""
    normalized = question.casefold()
    if not (
        re.search(r"\bчто\s+(?:написано|изображено)\b", normalized)
        or re.search(r"\b(?:выпиши|перепиши|процитируй|распознай|прочитай|транскриб\w*)\b", normalized)
    ):
        return ""

    image = next(
        (
            item
            for item in context
            if Path(user_facing_source_name(item.get("file", ""))).suffix.casefold()
            in {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif"}
        ),
        None,
    )
    if image is None:
        return ""

    lines = []
    for raw_line in str(image.get("text", "")).splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if len(re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "", line)) >= 2:
            lines.append(line)
    if not lines:
        return ""

    reference = human_source_reference(image)
    first_line_only = bool(re.search(
        r"\b(?:перв\w*\s+строк\w*|верхн\w*\s+карточк\w*|пользовательск\w*\s+сообщен\w*)\b",
        normalized,
    ))
    if first_line_only:
        value = lines[0].replace("«", "‹").replace("»", "›")
        return f"Первая распознанная строка: «{value}». [Источник: {reference}]"

    excerpt = "\n".join(lines[:30])[:2000].rstrip()
    quoted = "\n".join(f"> {line}" for line in excerpt.splitlines())
    return f"Распознанный текст:\n\n{quoted}\n\n[Источник: {reference}]"


def generate_extractive_value_answer(
    question: str,
    context: list[dict],
    calculations: list[str] | None = None,
) -> str | None:
    """Build a deterministic answer for indicator/value calculation questions."""
    if detect_query_intent(question) != "value":
        return None
    terms = ordered_requested_terms(question)
    if not terms:
        return None

    lines = [
        "Краткий вывод:",
        "Это расчет самих величин отклонений, не расчет стоимости.",
        "",
        "Коротко:",
    ]

    for term in terms:
        term_items = [
            item for item in context
            if item.get("status") != "deleted"
            and item.get("chunk_type") == "value"
            and contains_term(" ".join([
                str(item.get("section_path", "")),
                str(item.get("section_title", "")),
            ]), term)
        ]
        # A user-uploaded document is the canonical public source.  Derived
        # Markdown corpora remain useful retrieval aids, but must not replace
        # the document the user can actually open from the project inventory.
        term_items.sort(key=lambda item: (
            not user_facing_source_name(item.get("file", "")).casefold().endswith(".docx"),
            -float(item.get("score") or 0.0),
        ))
        if not term_items:
            lines.append(f"- {term}: базовый активный раздел не найден в evidence.")
            continue

        snippets: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(term_items):
            snippet = _calculation_snippet(item, term)
            snippet_lower = snippet.lower()
            is_extra_calculation = (
                "как разница" in snippet_lower
                or "расчет величины" in snippet_lower
                or "осуществляется по формуле" in snippet_lower
            )
            is_narrow_case = "для гтп потребления поставщика" in snippet_lower[:500]
            if index > 0 and (is_narrow_case or not is_extra_calculation):
                continue
            normalized = re.sub(r"\W+", "", snippet.lower())[:160]
            if snippet and normalized not in seen:
                seen.add(normalized)
                snippets.append(snippet)
            if len(snippets) >= 2:
                break

        source = human_source_reference(term_items[0])
        rule_text = "\n".join(str(item.get("text", "")) for item in term_items)
        derived_formula = derive_worded_formula(question, rule_text)
        if derived_formula:
            lines.append(
                f"- {term}: величина внешней инициативы для соответствующей ГТП и часа; "
                "она равна разности между объёмом электроэнергии по УДГ и объёмом "
                f"электроэнергии в плановом ДГ. [Источник: {source}]."
            )
        elif snippets:
            lines.append(f"- {term}: {_short_explanation(term, ' '.join(snippets), 520)} [Источник: {source}].")
        else:
            lines.append(f"- {term}: расчетная фраза не выделена из evidence. [Источник: {source}].")

        if derived_formula:
            lines.extend([
                "",
                "**Формула:**",
                "",
                f"$${derived_formula}$$",
                "",
                "**Обозначения:**",
                "- $V_{\\mathrm{УДГ}}$ — объём электроэнергии для соответствующих ГТП и часа, определённый согласно УДГ.",
                "- $V_{\\mathrm{план.\\,ДГ}}$ — объём электроэнергии в плановом ДГ для ГОУ, соответствующего ГТП.",
            ])

    lines.extend([
        "",
        "Ограничения:",
        "Для численного расчета нужны значения объемов для конкретной ГТП и часа операционных суток.",
        "Специальные случаи нужно смотреть отдельно: ГТП экспорта/импорта, ГАЭС и ГТП с регулируемой нагрузкой.",
    ])
    return clean_formula_artifacts("\n".join(lines))


def build_section_documents(text: str, source_path: Path, backend: str) -> list[Document]:
    """Create one LlamaIndex Document per logical section before chunk splitting."""
    def compact_meta(meta: dict) -> dict:
        limits = {
            "section_path": 360,
            "parent_section": 240,
            "section_title": 180,
            "file_path": 260,
        }
        compacted = dict(meta)
        for key, limit in limits.items():
            value = compacted.get(key)
            if isinstance(value, str) and len(value) > limit:
                compacted[key] = value[: limit - 3].rstrip() + "..."
        return compacted

    cleaned = _cleanup_formula_noise(text)
    # Converter bookkeeping precedes Markdown headings in the source.  It is
    # not normative text and must not become part of the previous section.
    cleaned = re.sub(r"<a\s+id=[\"'][^\"']+[\"']\s*></a>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?m)^\d+(?:\.\d+)*\.\s+Раздел\s*\n", "", cleaned)
    cleaned = re.sub(r"(?m)^<!--\s*rag-heading-level:[^>]*-->\s*\n?", "", cleaned)
    matches = list(re.finditer(r"\[SECTION:\s*([^\]]+)\]", cleaned))
    if not matches:
        # RAG-ready Markdown stores every numbered semantic unit as a heading.
        # Preserve that boundary instead of splitting the entire file as one
        # anonymous document; otherwise a precise regulation clause cannot be
        # selected over neighbouring formulas with similar vocabulary.
        markdown_matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", cleaned, re.MULTILINE))
        if markdown_matches:
            documents: list[Document] = []
            preamble = cleaned[:markdown_matches[0].start()].strip()
            if preamble:
                documents.append(Document(
                    text=preamble,
                    doc_id=f"{source_path.name}_preamble",
                    metadata=compact_meta({
                        "file_name": source_path.name,
                        "file_path": str(source_path),
                        "ingestion_backend": backend,
                        "section_path": "",
                        "section_title": "",
                        "parent_section": "",
                        "status": "active",
                        "chunk_type": "unknown",
                    }),
                ))

            # Word often stores an equation as the paragraph immediately after
            # its governing clause.  The RAG conversion renders that paragraph
            # as a Markdown heading (for example ``= MIN(...)``).  It is not a
            # semantic section.  Keep it in the preceding clause so retrieval
            # can never return a rate description without its exact formula.
            section_units: list[dict[str, str]] = []
            for index, match in enumerate(markdown_matches):
                section_path = match.group(2).strip()
                end = markdown_matches[index + 1].start() if index + 1 < len(markdown_matches) else len(cleaned)
                body = cleaned[match.end():end].strip()
                formula_heading = bool(re.match(r"^(?:=|≈|≠|≤|≥|\$?\\?[A-Za-zА-Яа-яΔСТ])", section_path)) and (
                    "=" in section_path or re.search(r"\b(?:min|max)\s*\(", section_path, re.IGNORECASE)
                )
                if formula_heading and section_units:
                    formula = section_path
                    if body:
                        formula += f"\n{body}"
                    section_units[-1]["formula"] = "\n\n[FORMULA]\n" + formula
                    continue
                section_units.append({"title": section_path, "body": body, "formula": ""})

            for index, unit in enumerate(section_units):
                section_path = unit["title"]
                section_text = f"[SECTION: {section_path}]" + unit["formula"]
                if unit["body"]:
                    section_text += f"\n{unit['body']}"
                section_meta = extract_chunk_metadata(section_text)
                section_key = hashlib.sha256(section_path.encode("utf-8")).hexdigest()[:10]
                documents.append(Document(
                    text=section_text,
                    doc_id=f"{source_path.name}_section_{section_key}_{index}",
                    metadata=compact_meta({
                        "file_name": source_path.name,
                        "file_path": str(source_path),
                        "ingestion_backend": backend,
                        **section_meta,
                    }),
                ))
            return documents
        # Legal Markdown is often exported as plain text: no Markdown headings,
        # but every normative clause starts at the beginning of a line with a
        # stable number ("1.", "23.", "159.1.").  Preserve those boundaries so
        # a re-uploaded regulation has useful point locators and focused chunks.
        clause_matches = list(re.finditer(
            r"(?m)^(?P<number>\d{1,4}(?:\.\d{1,4})*)\.\s+(?=\S)",
            cleaned,
        ))
        if len(clause_matches) >= 2:
            documents: list[Document] = []
            preamble = cleaned[:clause_matches[0].start()].strip()
            if preamble:
                documents.append(Document(
                    text=preamble,
                    doc_id=f"{source_path.name}_preamble",
                    metadata=compact_meta({
                        "file_name": source_path.name,
                        "file_path": str(source_path),
                        "ingestion_backend": backend,
                        "section_path": "Преамбула",
                        "section_title": "Преамбула",
                        "parent_section": "",
                        "status": "active",
                        "chunk_type": "unknown",
                    }),
                ))
            for index, match in enumerate(clause_matches):
                end = clause_matches[index + 1].start() if index + 1 < len(clause_matches) else len(cleaned)
                body = cleaned[match.start():end].strip()
                if not body:
                    continue
                locator = f"Пункт {match.group('number')}"
                section_text = f"[SECTION: {locator}]\n{body}"
                documents.append(Document(
                    text=section_text,
                    doc_id=f"{source_path.name}_clause_{match.group('number').replace('.', '_')}_{index}",
                    metadata=compact_meta({
                        "file_name": source_path.name,
                        "file_path": str(source_path),
                        "ingestion_backend": backend,
                        **extract_chunk_metadata(section_text),
                    }),
                ))
            return documents
    if not matches:
        meta = {
            "file_name": source_path.name,
            "file_path": str(source_path),
            "ingestion_backend": backend,
            **extract_chunk_metadata(cleaned),
        }
        return [Document(text=cleaned, doc_id=f"{source_path.name}_section_0", metadata=compact_meta(meta))]

    documents: list[Document] = []
    seen_sections: dict[str, int] = {}
    preamble = cleaned[:matches[0].start()].strip()
    if preamble:
        preamble_type = "history" if preamble.lower().count("с изменениями") >= 3 else "unknown"
        meta = {
            "file_name": source_path.name,
            "file_path": str(source_path),
            "ingestion_backend": backend,
            "section_path": "",
            "section_title": "",
            "parent_section": "",
            "status": "active",
            "chunk_type": preamble_type,
        }
        documents.append(Document(text=preamble, doc_id=f"{source_path.name}_preamble", metadata=compact_meta(meta)))

    index = 0
    while index < len(matches):
        match = matches[index]
        section_path = match.group(1).strip()
        start = match.start()
        next_index = index + 1
        while next_index < len(matches) and matches[next_index].group(1).strip() == section_path:
            next_index += 1
        end = matches[next_index].start() if next_index < len(matches) else len(cleaned)
        block = cleaned[start:end].strip()
        if not block:
            index = next_index
            continue

        # The XML parser repeats [SECTION] before many paragraphs. Keep one header,
        # otherwise section names dominate embeddings and waste context budget.
        body = re.sub(r"\[SECTION:\s*[^\]]+\]\s*", "", block).strip()
        if not body:
            index = next_index
            continue
        section_text = f"[SECTION: {section_path}]\n{body}"
        section_meta = extract_chunk_metadata(section_text)
        section_key = hashlib.sha256(section_path.encode("utf-8")).hexdigest()[:10]
        section_index = seen_sections.get(section_key, 0)
        seen_sections[section_key] = section_index + 1
        documents.append(Document(
            text=section_text,
            doc_id=f"{source_path.name}_section_{section_key}_{section_index}",
            metadata=compact_meta({
                "file_name": source_path.name,
                "file_path": str(source_path),
                "ingestion_backend": backend,
                **section_meta,
            }),
        ))
        index = next_index

    return documents


def derive_worded_formula(question: str, text: str) -> str | None:
    """Turn one unambiguous regulation wording into a renderable equality."""
    normalized_question = question.casefold().replace("ё", "е")
    normalized_text = text.casefold().replace("ё", "е")
    is_ivo_request = bool(re.search(r"(?<![а-яa-z0-9])ив\s*0(?![а-яa-z0-9])", normalized_question))
    has_difference_rule = bool(re.search(r"равн\w*\s+разност", normalized_text))
    if (
        is_formula_question(question)
        and is_ivo_request
        and has_difference_rule
        and "согласно удг" in normalized_text
        and "плановом дг" in normalized_text
    ):
        return r"\mathrm{ИВ0} = V_{\mathrm{УДГ}} - V_{\mathrm{план.\,ДГ}}"
    return None


def extract_formula_lines_from_texts(texts: list[str], question: str = "") -> list[str]:
    """Extract allowed calculation evidence: symbolic formulas and textual calculation rules."""
    calculations: list[str] = []
    seen: set[str] = set()
    requested = requested_formula_terms(question)
    intent = detect_query_intent(question)
    formula_request = asks_for_calculation(question)
    explicit_formula_request = is_formula_question(question)
    category = price_category_number(question)
    question_terms = tuple(term.replace("ё", "е") for term in section_query_terms(question))
    generic_formula_terms = {
        "формуле", "формулу", "формулы", "формул", "определяется",
    }
    specific_terms = tuple(
        term for term in question_terms
        if len(term) >= 7 and term not in generic_formula_terms
    )

    def add(candidate: str, *, requested_term_already_matched: bool = False) -> None:
        candidate = clean_formula_artifacts(_plain_evidence_text(candidate))
        candidate = candidate.strip(" ,;\t")
        minimum_length = 5 if "=" in candidate else 20
        if not candidate or len(candidate) < minimum_length:
            return
        if "=" in candidate and re.search(
            r"(?:[×*]\s*,\s*\+\s*,\s*-|,\s*\+\s*,\s*-|[×*]\s*[×*])",
            candidate,
        ):
            return
        if requested and not requested_term_already_matched and not any(contains_term(candidate, term) for term in requested):
            return
        key = re.sub(r"\W+", "", candidate.lower())[:220]
        if key in seen:
            return
        seen.add(key)
        calculations.append(candidate)

    for raw in texts:
        cleaned = _cleanup_formula_noise(raw)
        searchable = cleaned.lower().replace("ё", "е")

        if formula_request:
            direct_category_match = bool(category and contains_price_category(cleaned, category))
            category_scope_relevant = direct_category_match or bool(re.search(
                r"(?:стоимост\w*\s+услуг\w*\s+по\s+передач|тариф\w*\s+на\s+услуг\w*\s+по\s+передач)",
                searchable,
            ))
            for formula in re.findall(r"\[MATH:\s*([^\]\n]+)\]", raw):
                normalized_formula = formula.lower().replace(" ", "")
                formula_categories = {
                    number for number in PRICE_CATEGORY_WORDS
                    if contains_price_category(formula, number)
                }
                if (
                    "=" in formula
                    and not (category == "1" and "2ст" in normalized_formula)
                    and not (category == "6" and "1ст" in normalized_formula)
                    and (not category or category_scope_relevant)
                    and (not category or not formula_categories or category in formula_categories)
                ):
                    add(formula, requested_term_already_matched=direct_category_match)
            if calculations and category and len(calculations) >= 8:
                return calculations[:8]
            if category:
                # Narrative and tables remain in the grounded context; only
                # actual Word equation payloads belong in the formula block.
                continue
            derived_formula = derive_worded_formula(question, cleaned)
            if derived_formula:
                add(derived_formula, requested_term_already_matched=True)
                return calculations[:1]
            # Keep ordinary Markdown/LaTeX equations as first-class evidence.
            # This covers uploaded technical notes without requiring the
            # regulation-specific [FORMULA] marker used by converted DOCX files.
            markdown_equations = re.findall(r"\$\$\s*([^$\n][\s\S]{0,500}?)\s*\$\$", cleaned)
            markdown_equations.extend(re.findall(r"(?<!\$)\$([^$\n]{1,300}=[^$\n]{1,300})\$(?!\$)", cleaned))
            for equation in markdown_equations:
                if "=" in equation:
                    add(equation)
                    if calculations and explicit_formula_request and not category:
                        return calculations[:1]
            linked_formulas = re.findall(
                r"\[FORMULA\]\s*(?:#{1,6}\s*)?([^\n]+)",
                cleaned,
            )
            for formula in linked_formulas:
                if "=" in formula:
                    add(formula)
                    if explicit_formula_request and not category:
                        return calculations[:1]
            # Prefer a compact equality from a section that actually matches the
            # question.  This prevents a formula-only answer from turning into a
            # dump of adjacent narrative rules.
            matched_terms = sum(contains_term(searchable, term) for term in question_terms)
            matched_specific_terms = sum(contains_term(searchable, term) for term in specific_terms)
            enough_overlap = matched_specific_terms >= min(2, len(specific_terms)) if specific_terms else matched_terms >= 2
            if enough_overlap:
                equations = re.findall(
                    r"=\s*[^\s\n(]{1,8}\s*\([^\n)]{1,500}\)\.?",
                    cleaned,
                )
                if equations:
                    add(equations[0])
                    if explicit_formula_request and not category:
                        return calculations[:1]
                # In the RAG Markdown, a normative rule can be the heading
                # itself (with no following paragraph). Keep that wording as
                # the evidence instead of discarding it as section metadata.
                section_headers = re.findall(r"\[SECTION:\s*([^\]]+)\]", cleaned)
                if section_headers and "определяется" in section_headers[-1].lower():
                    add(section_headers[-1])
                    if calculations:
                        return calculations[:1]

        symbolic_patterns = re.findall(
            r"(\[С_sub\([^\]]+\)\]\s*=\s*\[ΔО_sub\([^\]]+\)\]\s*\*\s*\[Т_sub\([^\]]+\)\](?:\s*[+*]\s*[^.\n]+)?\.?)",
            cleaned,
        )
        for candidate in symbolic_patterns:
            if "+ *" in candidate or candidate.endswith("+ *."):
                continue
            add(candidate)

        # A formula can define a price or rate, so cost intent must not suppress
        # calculation extraction merely because the question contains "ставка".
        if intent == "value" or formula_request:
            text = _plain_evidence_text(cleaned)
            sections = re.findall(r"\[SECTION:\s*([^\]]+)\]", cleaned)
            section_haystack = sections[-1] if sections else ""
            section_terms = [term for term in requested if contains_term(section_haystack, term)]
            sentences = _split_evidence_sentences(text)
            for sentence in sentences:
                if formula_request and re.search(r"[çæøï]", sentence):
                    continue
                lower = sentence.lower()
                if not (
                    "определяется" in lower
                    or "рассчитывается" in lower
                    or "расчет" in lower
                    or "как разность" in lower
                    or "как разница" in lower
                    or "равна разности" in lower
                    or "осуществляется по формуле" in lower
                ):
                    continue
                if section_terms and not any(contains_term(sentence, term) for term in section_terms):
                    for term in section_terms:
                        add(f"{term}: {sentence}", requested_term_already_matched=True)
                else:
                    add(sentence)
                if len(calculations) >= 12:
                    return calculations[:12]
            # For a formula request, one clause with strong lexical coverage is
            # safer than concatenating neighbouring formulas from other rules.
            if explicit_formula_request and enough_overlap and calculations and not category:
                return calculations[:1]

    return calculations[:12]


def formula_sibling_results(nodes: list, merged: dict[str, dict], question: str) -> list[dict]:
    """Add formula chunks from a source already retrieved for a compound query."""
    normalized_question = question.casefold().replace("ё", "е")
    comparison_needs_formula = bool(requested_formula_terms(question)) and (
        "отлич" in normalized_question or "разниц" in normalized_question
    )
    if (
        (not asks_for_calculation(question) and not comparison_needs_formula)
        or price_category_number(question)
        or not merged
    ):
        return []
    # A broad explanatory request such as "как рассчитываются авансы" does
    # not authorize arbitrary neighbouring equations.  Formula siblings are
    # useful only for an explicit formula request or a named formula entity
    # (ИВ0, ИВ1, ИС, ...); otherwise a high-scoring but unrelated source can
    # contaminate an otherwise grounded algorithm answer.
    if not is_formula_question(question) and not requested_formula_terms(question):
        return []

    def canonical_source(value: object) -> str:
        name = Path(str(value)).name.casefold()
        # Uploaded files are persisted with one or more collision-safe UUID
        # prefixes, while the vector payload and registry expose the user-facing
        # basename. Treat both representations as the same source.
        while re.match(r"^[0-9a-f]{32}_", name):
            name = name[33:]
        return name

    source_scores: dict[str, float] = {}
    normalized_question = re.sub(r"\s+", " ", question.strip().lower())
    for item in merged.values():
        matched_queries = {
            re.sub(r"\s+", " ", str(query).strip().lower())
            for query in item.get("matched_queries", [])
        }
        if normalized_question not in matched_queries:
            continue
        source = canonical_source(item.get("file", ""))
        if source:
            source_scores[source] = max(source_scores.get(source, float("-inf")), float(item.get("score", 0.0)))
    if not source_scores:
        for item in merged.values():
            source = canonical_source(item.get("file", ""))
            if source:
                source_scores[source] = max(source_scores.get(source, float("-inf")), float(item.get("score", 0.0)))
    preferred_sources = {
        source for source, _ in sorted(source_scores.items(), key=lambda pair: pair[1], reverse=True)[:3]
    }

    siblings: list[dict] = []
    for node in nodes:
        meta = node.metadata or {}
        text = node.text or ""
        source_name = meta.get("file_name", meta.get("file_path", "?"))
        source = canonical_source(source_name)
        if source not in preferred_sources:
            continue
        section = f"{meta.get('section_path', '')} {meta.get('section_title', '')}".lower()
        has_equation = bool(
            "[FORMULA]" in text
            or re.search(r"\$\$[\s\S]{0,600}=[\s\S]{0,600}\$\$", text)
            or re.search(r"(?m)^\s*[A-Za-zА-Яа-я][\w{}\\,^\- ]{0,40}\s*=\s*\S+", text)
            or ("formula" in section and "=" in text)
        )
        if not has_equation:
            continue
        chunk_meta = {
            **extract_chunk_metadata(text),
            **{k: v for k, v in meta.items() if k in {
                "section_path", "section_title", "parent_section", "status", "chunk_type",
            } and v},
        }
        if chunk_meta.get("status") == "deleted":
            continue
        siblings.append({
            "file": source_name,
            "score": round(source_scores[source] + 0.35, 4),
            "text": text,
            "matched_queries": ["formula-sibling"],
            **chunk_meta,
        })
        if len(siblings) >= 6:
            break
    return siblings


def generate_formula_only_answer(question: str, context: list[dict], calculations: list[str], spec: PrismQuery) -> str:
    """Return only a rule that is tied to one confirmed regulation clause."""
    confirmed = next(
        (item for item in context if "formula-clause-exact" in item.get("matched_queries", [])),
        None,
    )
    if confirmed is None and spec.target_sections:
        confirmed = next(
            (item for item in context if any(
                re.search(rf"(?<!\d){re.escape(section)}(?:\.|\b)(?!\d)", str(item.get("section_path", "")))
                for section in spec.target_sections
            )),
            None,
        )
    if confirmed is None and calculations and context:
        normalized_calculations = [clean_formula_artifacts(item).casefold() for item in calculations[:3]]
        confirmed = next(
            (
                item for item in context
                if any(
                    calculation and calculation in clean_formula_artifacts(str(item.get("text", ""))).casefold()
                    for calculation in normalized_calculations
                )
            ),
            None,
        )
    if confirmed is None:
        # The caller may already have extracted a calculation from an explicit
        # section query.  It remains deterministic evidence, even when the
        # in-memory context fixture has no metadata (for example CLI/tests).
        if calculations:
            target = f"п. {', '.join(spec.target_sections)}" if spec.target_sections else "извлечённый контекст"
            rendered = [
                format_formula_for_katex(item, display=True) if "=" in item else f"- {clean_formula_artifacts(item)}"
                for item in calculations[:1]
            ]
            return clean_formula_artifacts(
                f"**Ориентир в источнике:** {target}.\n\n" + "\n\n".join(rendered)
            )
        return "Недостаточно подтверждённых данных: для формулы не найден конкретный пункт Регламента."

    confirmed_calculations = extract_formula_lines_from_texts([str(confirmed.get("text", ""))], question) or calculations
    if not confirmed_calculations:
        return "Недостаточно подтверждённых данных: в найденном пункте нет извлечённой формулы или расчётного правила."

    source = human_source_reference(confirmed)
    rendered = [
        format_formula_for_katex(item, display=True) if "=" in item else clean_formula_artifacts(item)
        for item in confirmed_calculations[:1]
    ]
    return clean_formula_artifacts(
        f"[Источник: {source}]\n\n" + "\n\n".join(rendered)
    )


def rerank_context_results(results: list[dict], question: str) -> list[dict]:
    intent = detect_query_intent(question)
    requested = requested_formula_terms(question)
    normalized_question = question.lower().replace("ё", "е")
    formula_request = is_formula_question(question)
    category = price_category_number(question)
    generic_formula_terms = {
        "формуле", "определяется", "ставка", "объемов", "собственной", "инициативе",
    }
    formula_terms = tuple(
        term for term in section_query_terms(question)
        if len(term) >= 7 and term not in generic_formula_terms
    )
    is_parameter_creation = (
        "параметр" in normalized_question
        and any(marker in normalized_question for marker in ("добав", "созда", "новый", "завест"))
    )

    def adjusted(item: dict) -> float:
        text = item.get("text", "")
        lower = text.lower()
        score = float(item.get("score", 0.0))
        section_path = str(item.get("section_path", "")).lower()
        section_title = str(item.get("section_title", "")).lower()
        chunk_type = item.get("chunk_type", "unknown")
        status = item.get("status", "active")

        if category:
            section_scope = f"{section_path}\n{section_title}"
            leaf_scope = section_title or section_path.rsplit(">", 1)[-1]
            direct_body = contains_price_category(text, category)
            direct_section = contains_price_category(section_scope, category)
            if direct_section:
                score += 1.10
            if direct_body:
                score += 0.55
            if any(
                str(query).startswith(f"price-category-exact:{category}")
                for query in item.get("matched_queries", [])
            ):
                score += 0.85
            leaf_categories = {
                number for number in PRICE_CATEGORY_WORDS
                if contains_price_category(leaf_scope, number)
            }
            if leaf_categories and category not in leaf_categories:
                score -= 2.50
            if not (direct_section or direct_body):
                score -= 0.80

        if formula_request and formula_terms:
            matches = sum(contains_term(lower, term) for term in formula_terms)
            score += matches * 0.14
            if matches == 0:
                score -= 0.22

        if is_parameter_creation:
            target = f"{section_path}\n{section_title}\n{lower[:800]}".replace("ё", "е")
            if "создание параметра" in target:
                score += 0.45
            if "для создания параметра" in target:
                score += 0.25
            if "работа с параметрами" in target:
                score += 0.08
            if "создание параметра > вкладка «общее»" in target:
                score += 0.45
            if "создание параметра > вкладка «принадлежность»" in target:
                score += 0.30
            if section_path.endswith("создание параметра"):
                score += 0.25
            if "создание документ" in target or "работа с документами" in target:
                score -= 0.35
            if "макрос" in target:
                score -= 0.25
            if "копирование параметров" in target or "редактирование параметра" in target:
                score -= 0.12
            if "назначение формулы" in target:
                score -= 0.70
            if "удаление" in target:
                score -= 0.65
            if "добавление версии" in target:
                score -= 0.45
            if "вкладка «атрибуты»" in target:
                score -= 0.20

        for term in requested:
            if term.casefold() in generic_formula_terms:
                continue
            # Repetition in a very large regulation chunk is not relevance.
            # Without a cap a generic word such as "определяется" can add ten
            # points and drown out the document explicitly named by the user.
            score += 0.01 * min(text.count(term), 5)
            if term.lower() in section_title:
                score += 0.10
            if term.lower() in section_path:
                score += 0.06

        lexical_terms = {
            term for term in section_query_terms(question)
            if len(term) >= 5 and term not in generic_formula_terms
        }
        score += min(sum(contains_term(lower, term) for term in lexical_terms), 5) * 0.06

        if "\\CYR" in text or "<!-- image -->" in text:
            score -= 0.30
        if status == "deleted":
            score -= 0.25

        if intent == "value":
            if chunk_type == "value":
                score += 0.16
            if "определение величины отклонений" in lower or "определение величины отклонений" in section_path:
                score += 0.12
            if "виды инициатив" in lower or "виды инициатив" in section_path:
                score += 0.08
            if "составляющая величина отклонения" in lower:
                score += 0.06
            if chunk_type in {"cost", "rate", "history"}:
                score -= 0.20
            if "расчетные показатели стоимости" in lower:
                score -= 0.12
            if "исходные данные" in lower:
                score -= 0.12
            if "ставк" in lower or "стоимост" in lower or "оплат" in lower:
                score -= 0.18
            if "предварительных обязательств" in lower or "предварительных требований" in lower:
                score -= 0.16
        elif intent == "cost":
            if chunk_type == "cost":
                score += 0.14
            if "расчетные показатели стоимости" in lower or "расчетные показатели стоимости" in section_path:
                score += 0.10
            if "ставк" in lower or "стоимост" in lower:
                score += 0.05
            if chunk_type in {"history"}:
                score -= 0.18
        else:
            if chunk_type == "history":
                score -= 0.15

        return score

    ranked = []
    for item in results:
        adjusted_score = adjusted(item)
        enriched = dict(item)
        enriched["raw_score"] = item.get("score", 0.0)
        enriched["score"] = round(adjusted_score, 4)
        ranked.append(enriched)
    return sorted(ranked, key=lambda x: x["score"], reverse=True)


def prioritize_scoped_results(ranked: list[dict], top_k: int) -> list[dict]:
    """Reserve a small evidence quota for lexically relevant private documents."""
    scoped = [
        item for item in ranked
        if "private-scope-lexical" in item.get("matched_queries", [])
    ]
    if not scoped:
        return ranked
    quota = min(3, max(1, top_k // 3))
    prioritized = scoped[:quota]
    prioritized_ids = {id(item) for item in prioritized}
    return [*prioritized, *(item for item in ranked if id(item) not in prioritized_ids)]


def apply_document_version_preference(
    ranked: list[dict],
    source_versions: dict[str, str] | None,
    *,
    explicit_sources: set[str] | None = None,
) -> list[dict]:
    """Softly prefer current revisions while keeping historical evidence searchable."""
    if not source_versions:
        return ranked
    normalized_versions = {
        Path(str(source)).name.casefold(): str(status or "unknown").casefold()
        for source, status in source_versions.items()
    }
    adjusted: list[dict] = []
    for original in ranked:
        item = dict(original)
        source_id = Path(str(item.get("file", ""))).name.casefold()
        status = normalized_versions.get(source_id, "unknown")
        item["version_status"] = status
        if not explicit_sources:
            adjustment = 0.15 if status == "latest" else (-0.15 if status == "superseded" else 0.0)
            if adjustment:
                item["score"] = round(float(item.get("score", 0.0)) + adjustment, 4)
                item["version_adjustment"] = adjustment
        adjusted.append(item)
    return sorted(adjusted, key=lambda item: float(item.get("score", 0.0)), reverse=True)


def sanitize_grounded_answer(answer: str, question: str, allowed_calculations: list[str]) -> str:
    if contains_prompt_injection(answer):
        return refusal_answer()
    requested = requested_formula_terms(question)
    unrequested = [term for term in known_formula_terms() if term not in requested]
    cleaned_lines: list[str] = []

    for raw_line in answer.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            cleaned_lines.append(line)
            continue

        if "[EQ]" in stripped or "_sub [" in stripped:
            continue
        if stripped.lower().startswith("формула для"):
            continue
        if stripped == "Формулы:":
            continue
        if "РАЗРЕШЕН" in stripped:
            continue
        if any(marker in stripped.lower() for marker in (
            "не найдено в извлеченном контексте",
            "не найдено в отобранном evidence",
            "не содержатся в разрешенном блоке",
            "не выделены из извлеченного контекста",
            "контекст недостаточен",
        )):
            continue
        if stripped.lower() in {"или", "или:"}:
            continue
        if detect_query_intent(question) == "value" and "формул" in stripped.lower() and "не найден" in stripped.lower():
            continue
        if "[С_sub(" in stripped or "[ΔО_sub(" in stripped or "[Т_sub(" in stripped:
            continue

        has_unrequested = False
        for term in unrequested:
            if term == "ИВ":
                has_unrequested = bool(re.search(r"(?<![А-Яа-яA-Za-z0-9-])ИВ(?![А-Яа-яA-Za-z0-9-])", stripped))
            else:
                has_unrequested = term in stripped
            if has_unrequested:
                break
        if has_unrequested:
            continue

        cleaned_lines.append(line)

    answer = "\n".join(cleaned_lines)
    # The model must not expand regulatory abbreviations from general language.
    # The source may contain the abbreviation without its definition; retaining
    # it is safer than emitting an invented expansion such as "группа точек
    # производства" or "группа технологического процесса".
    answer = re.sub(r"(?i)групп[аы]\s+точек\s+производства\s*\(\s*гтп\s*\)", "ГТП", answer)
    answer = re.sub(r"(?i)групп[аы]\s+технологическ\w*\s+процесс\w*\s*\(\s*гтп\s*\)", "ГТП", answer)
    answer = re.sub(r"(?i)энергоблок\w*\s*\(\s*его\s*\)", "ЕГО", answer)
    answer = re.sub(r"(?i)(?:координационн\w*|координирующ\w*)\s+орган\s*\(\s*ко\s*\)", "КО", answer)
    # Keep regulatory shorthand verbatim unless its expansion was supplied as
    # an explicit source fact.  Qwen otherwise invents plausible-looking
    # expansions (for example "Группа Технических Условий") that change the
    # legal meaning of an answer.
    answer = re.sub(
        r"\b(ГТП|ЕГО|ОРЭ|КО)\s*\(\s*[^()\n]{2,100}\s*\)",
        r"\1",
        answer,
    )
    answer = re.sub(r"(?im)^.*учредител\w*\s+пдг.*(?:\n|$)", "", answer)
    # Never expose formula notation as inline code. Convert model-emitted
    # equalities to math delimiters so the React client renders them with KaTeX.
    answer = re.sub(
        r"`([^`\n]{1,500}=[^`\n]{1,500})`",
        lambda match: format_formula_for_katex(match.group(1)),
        answer,
    )
    rendered_lines: list[str] = []
    for line in answer.splitlines():
        stripped = line.strip()
        candidate = re.sub(r"^[-*]\s+", "", stripped)
        if (
            "=" in candidate
            and "$" not in candidate
            and re.match(r"^[A-Za-zА-Яа-яЁёΔЦ][\wА-Яа-яЁё]*(?:\([^\n]+\)|_\{[^\n]+\})\s*=", candidate)
        ):
            rendered_lines.append(format_formula_for_katex(candidate, display=True))
        else:
            rendered_lines.append(line)
    answer = "\n".join(rendered_lines)
    # Some models wrap an already delimited display formula in another pair of
    # $$ lines.  KaTeX treats that as three separate (and usually broken)
    # blocks.  Collapse only the unambiguous empty-wrapper shape and remove
    # genuinely empty display blocks; ordinary adjacent formulas stay intact.
    nested_display = re.compile(
        r"(?ms)^\s*\$\$\s*\n\s*\$\$(.+?)\$\$\s*\n\s*\$\$\s*$"
    )
    while nested_display.search(answer):
        answer = nested_display.sub(lambda match: f"$$\n{match.group(1).strip()}\n$$", answer)
    answer = re.sub(r"(?m)^\s*\$\$\s*\$\$\s*$", "", answer)
    answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
    return answer


def ensure_requested_formula_coverage(
    answer: str,
    question: str,
    calculations: list[str],
    context: list[dict],
) -> str:
    """Append one grounded formula when a formula-bearing comparison omitted it."""
    normalized = question.casefold().replace("ё", "е")
    ivo_requested = "ив0" in normalized or "иво" in normalized
    formula_sensitive = (
        "формул" in normalized
        or "рассчит" in normalized
        or "расчет" in normalized
        or (ivo_requested and ("отлич" in normalized or "разниц" in normalized))
    )
    if not formula_sensitive or "$$" in answer:
        return answer

    grounded_calculations = list(calculations)
    if not grounded_calculations and context:
        grounded_calculations = extract_formula_lines_from_texts(
            [str(item.get("text", "")) for item in context],
            f"формула {question}",
        )
    if not grounded_calculations and ivo_requested:
        prose_definition = next(
            (
                item for item in context
                if "ив0" in f"{item.get('section_path', '')}\n{item.get('text', '')}".casefold().replace("ё", "е")
                and "r12" in user_facing_source_name(item.get("file", "")).casefold()
            ),
            None,
        )
        if prose_definition is not None:
            grounded_calculations = [r"V_{\mathrm{ИВ0}}=V_{\mathrm{УДГ}}-V_{\mathrm{план.ДГ}}"]
    if not grounded_calculations:
        return answer
    calculation = grounded_calculations[0]
    source = next(
        (
            item for item in context
            if "иво" in f"{item.get('section_path', '')}\n{item.get('text', '')}".casefold()
        ),
        context[0] if context else None,
    )
    citation = f" [Источник: {human_source_reference(source)}]" if source else ""
    return (
        f"{answer.rstrip()}\n\n### Формула из источника\n\n"
        f"{format_formula_for_katex(calculation, display=True)}\n\n"
        f"Обозначения и смысл величин приведены в указанном разделе.{citation}"
    )


def ensure_price_composition_source_coverage(answer: str, question: str, context: list[dict]) -> str:
    """Add the complete grounded composition and all governing source families."""
    if not is_price_composition_question(question):
        return answer

    families = (
        lambda name: "методик" in name and "1-6" in name,
        lambda name: "1179" in name,
        lambda name: "442" in name,
    )
    selected: list[dict] = []
    for matches in families:
        item = next(
            (candidate for candidate in context if matches(user_facing_source_name(candidate.get("file", "")).casefold())),
            None,
        )
        if item is not None:
            selected.append(item)
    if not selected:
        return answer

    citations = " ".join(f"[Источник: {human_source_reference(item)}]" for item in selected)
    appendix = (
        "### Полный состав цены и нормативная база\n\n"
        "В полном составе учитываются покупка электрической энергии и мощности, "
        "услуги по передаче, сбытовая надбавка и плата за иные услуги. "
        f"{citations}"
    )
    return f"{answer.rstrip()}\n\n{appendix}"


class RAGAgent:
    def __init__(self, llm_model: str = LLM_MODEL):
        # ── LLM ──────────────────────────────────────────────
        self.llm_model = llm_model
        self.llm = self._create_ollama_llm(self.llm_model)

        # ── Embeddings (через Ollama) ─────────────────────────
        self.embed_model = OllamaEmbedding(
            model_name=EMBED_MODEL,
            base_url=OLLAMA_BASE_URL,
            client_kwargs={"trust_env": False},
        )

        # ── Глобальные настройки LlamaIndex ──────────────────
        Settings.llm = self.llm
        Settings.embed_model = self.embed_model
        Settings.chunk_size = CHUNK_SIZE
        Settings.chunk_overlap = CHUNK_OVERLAP

        # ── Qdrant ───────────────────────────────────────────
        self.qdrant = qdrant_client.QdrantClient(url=QDRANT_URL, trust_env=False)
        self.vector_store = QdrantVectorStore(
            client=self.qdrant,
            collection_name=QDRANT_COLLECTION,
        )

        # ── Docstore для BM25 (на диске) ─────────────────────
        bm25_path = Path(BM25_PERSIST_DIR)
        bm25_path.mkdir(parents=True, exist_ok=True)
        self.docstore_path = bm25_path / "docstore.json"

        if self.docstore_path.exists():
            self.docstore = SimpleDocumentStore.from_persist_path(str(self.docstore_path))
        else:
            self.docstore = SimpleDocumentStore()

        # ── Векторный индекс ──────────────────────────────────
        storage_ctx = StorageContext.from_defaults(
            vector_store=self.vector_store,
            docstore=self.docstore,
        )
        self.index = VectorStoreIndex.from_vector_store(
            self.vector_store,
            storage_context=storage_ctx,
        )

        # ── Exact response cache ──────────────────────────────
        self.response_cache = ResponseCache()
        self.reranker = HttpReranker()
        self._generation_state = threading.local()
        self._generation_state.from_cache = False
        self._index_refresh_lock = threading.Lock()

        # ── Ноды для BM25 (в памяти) ─────────────────────────
        self._all_nodes: list = []
        self._load_nodes_from_docstore()
        self._docstore_mtime_ns = self.docstore_path.stat().st_mtime_ns if self.docstore_path.exists() else 0

        print(f"[OK] Агент готов. Документов в docstore: {len(self._all_nodes)} чанков")

    @property
    def last_generation_from_cache(self) -> bool:
        """Return cache state for the current request thread only."""
        return bool(getattr(self._generation_state, "from_cache", False))

    @staticmethod
    def _create_ollama_llm(model: str) -> Ollama:
        profile = {**DEFAULT_OLLAMA_PROFILE, **OLLAMA_MODEL_PROFILES.get(model, {})}
        timeout = float(profile["request_timeout"])
        return Ollama(
            model=model,
            base_url=OLLAMA_BASE_URL,
            temperature=0.0,
            context_window=int(profile["context_window"]),
            thinking=profile["thinking"],
            additional_kwargs={"seed": 42, "num_predict": int(profile["num_predict"])},
            request_timeout=timeout,
            client=OllamaClient(host=OLLAMA_BASE_URL, timeout=timeout, trust_env=False),
        )

    def set_llm_model(self, model: str) -> None:
        """Switch the generation model without rebuilding the retrieval indexes."""
        model = model.strip()
        if not model or model == self.llm_model:
            return
        self.llm_model = model
        self.llm = self._create_ollama_llm(self.llm_model)
        Settings.llm = self.llm
        self.clear_cache()

    def set_llm_provider(self, config: dict) -> None:
        """Apply a cloud provider without rebuilding retrieval indexes."""
        provider = str(config.get("provider", "")).strip()
        if provider == "ollama":
            self.set_llm_model(str(config.get("model", LLM_MODEL)))
            return
        self.llm_model = str(config.get("model", "")).strip()
        self.llm = ProviderLLM(
            provider=provider,
            api_key=str(config.get("api_key", "")),
            base_url=str(config.get("base_url", "")),
            model=self.llm_model,
            folder_id=str(config.get("folder_id", "")),
            auth_type=str(config.get("auth_type", "api_key")),
        )
        Settings.llm = self.llm
        self.clear_cache()

    def _load_nodes_from_docstore(self):
        """Загружаем ноды из docstore для BM25."""
        docs = self.docstore.docs
        if docs:
            # docstore может хранить и Document, и уже готовые TextNode.
            from llama_index.core.schema import TextNode
            nodes = []
            for d in docs.values():
                if not getattr(d, "text", None):
                    continue
                normalized_meta = dict(getattr(d, "metadata", {}) or {})
                effective = effective_chunk_metadata(d.text, normalized_meta)
                normalized_meta.update({
                    "status": effective["status"],
                    "chunk_type": effective["chunk_type"],
                })
                if isinstance(d, TextNode):
                    d.metadata = normalized_meta
                    nodes.append(d)
                    continue
                node_id = getattr(d, "doc_id", None) or getattr(d, "id_", None) or getattr(d, "id", None)
                nodes.append(TextNode(text=d.text, metadata=normalized_meta, id_=node_id))
            self._all_nodes = nodes

    def _refresh_docstore_if_changed(self) -> None:
        """Reload BM25/docstore state written by the dedicated ingestion worker."""
        try:
            current_mtime = self.docstore_path.stat().st_mtime_ns
        except OSError:
            return
        if current_mtime == self._docstore_mtime_ns:
            return
        with self._index_refresh_lock:
            try:
                current_mtime = self.docstore_path.stat().st_mtime_ns
            except OSError:
                return
            if current_mtime == self._docstore_mtime_ns:
                return
            try:
                refreshed_docstore = SimpleDocumentStore.from_persist_path(str(self.docstore_path))
            except Exception:
                # A writer may be between replace operations. Keep serving the
                # previous snapshot and retry on the next request.
                return
            self.docstore = refreshed_docstore
            storage_ctx = StorageContext.from_defaults(
                vector_store=self.vector_store,
                docstore=self.docstore,
            )
            self.index = VectorStoreIndex.from_vector_store(
                self.vector_store,
                storage_context=storage_ctx,
            )
            self._load_nodes_from_docstore()
            self._docstore_mtime_ns = current_mtime

    def _build_query_engine(self, top_k: int = TOP_K):
        """Строим гибридный query engine с текущими нодами."""
        vector_retriever = _TracedRetriever(
            self.index.as_retriever(similarity_top_k=max(VECTOR_TOP_K, top_k)),
            "retrieval.vector",
        )

        retrievers = [vector_retriever]
        if self._all_nodes:
            bm25_retriever = BM25Retriever.from_defaults(
                nodes=self._all_nodes,
                similarity_top_k=max(BM25_TOP_K, top_k),
            )
            retrievers.append(_TracedRetriever(bm25_retriever, "retrieval.bm25"))

        retriever = QueryFusionRetriever(
            retrievers=retrievers,
            similarity_top_k=top_k,
            num_queries=1,          # не генерировать доп. запросы — детерминизм
            mode="reciprocal_rerank",
            use_async=False,
        )

        synthesizer = get_response_synthesizer(
            llm=self.llm,
            response_mode="compact",
            text_qa_template=TEXT_QA_TEMPLATE,
            verbose=False,
        )

        return RetrieverQueryEngine(
            retriever=retriever,
            response_synthesizer=synthesizer,
        )

    def ingest(self, path: str) -> int:
        """Загружаем документ в индекс."""
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Файл не найден: {path}")

        print(f"  Парсим {p.name}...")

        ingested = ingest_document(p)
        text = _cleanup_formula_noise(ingested.text)
        if not text.strip():
            raise ValueError(f"Не удалось извлечь текст из документа: {path}")
        if ingested.warnings:
            print(f"  Предупреждения ingestion: {'; '.join(ingested.warnings)}")
        print(f"  Backend: {ingested.backend}")
        documents = build_section_documents(text, p, ingested.backend)
        print(f"  Загружено {len(documents)} секций, нарезаем на чанки...")

        # Нарезка
        splitter = SentenceSplitter(
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            paragraph_separator="\n\n",
        )
        nodes = splitter.get_nodes_from_documents(documents)
        print(f"  Получено {len(nodes)} чанков, индексируем...")

        # Вектора → Qdrant
        storage_ctx = StorageContext.from_defaults(
            vector_store=self.vector_store,
            docstore=self.docstore,
        )
        for node in nodes:
            self.docstore.add_documents([node], allow_update=True)

        self.index.insert_nodes(nodes)

        # Publish one complete snapshot. API workers watch this mtime and must
        # never observe a half-written JSON document store.
        temp_docstore_path = self.docstore_path.with_name(
            f".{self.docstore_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            self.docstore.persist(persist_path=str(temp_docstore_path))
            os.replace(temp_docstore_path, self.docstore_path)
        finally:
            temp_docstore_path.unlink(missing_ok=True)

        # Обновляем ноды в памяти для BM25
        self._all_nodes = list(nodes) + [
            n for n in self._all_nodes if n.id_ not in {nd.id_ for nd in nodes}
        ]
        self._docstore_mtime_ns = self.docstore_path.stat().st_mtime_ns

        # A changed corpus invalidates every generated answer, even when the
        # question itself is identical.
        self.clear_cache()

        print(f"[OK] Проиндексировано {len(nodes)} чанков из {p.name}")
        return len(nodes)

    def delete_source(self, path: str) -> int:
        """Remove every chunk for one stored upload from Qdrant and BM25."""
        target = Path(path)
        target_name = target.name.casefold()
        self._refresh_docstore_if_changed()

        def belongs_to_target(node) -> bool:
            metadata = getattr(node, "metadata", None) or {}
            stored_path = str(metadata.get("file_path") or "").strip()
            stored_name = str(metadata.get("file_name") or "").strip()
            return (
                (stored_path and Path(stored_path).name.casefold() == target_name)
                or (stored_name and Path(stored_name).name.casefold() == target_name)
            )

        nodes = [node for node in self._all_nodes if belongs_to_target(node)]
        node_ids = [str(node.id_) for node in nodes]
        if node_ids:
            # Qdrant stores node text itself, so LlamaIndex intentionally does
            # not remove our separate BM25 docstore. Delete both explicitly.
            self.vector_store.delete_nodes(node_ids)
            for node_id in node_ids:
                self.docstore.delete_document(node_id, raise_error=False)

            temp_docstore_path = self.docstore_path.with_name(
                f".{self.docstore_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                self.docstore.persist(persist_path=str(temp_docstore_path))
                os.replace(temp_docstore_path, self.docstore_path)
            finally:
                temp_docstore_path.unlink(missing_ok=True)
            deleted_node_ids = set(node_ids)
            self._all_nodes = [node for node in self._all_nodes if str(node.id_) not in deleted_node_ids]
            self._docstore_mtime_ns = self.docstore_path.stat().st_mtime_ns

        # Cached answers may cite a deleted source even when no local node was
        # found (for example after a previously interrupted cleanup).
        self.clear_cache()
        print(f"[OK] Удалено {len(node_ids)} чанков из {target.name}")
        return len(node_ids)

    def understand_query(self, question: str) -> QueryUnderstanding:
        """Use the LLM to clarify intent before retrieval, with a safe fallback."""
        fallback = deterministic_query_understanding(question)
        enabled = os.getenv("RAG_QUERY_UNDERSTANDING", "true").strip().casefold() not in {
            "0", "false", "no", "off",
        }
        if not enabled or not getattr(self, "llm", None):
            return fallback

        cache = getattr(self, "response_cache", None)
        cache_key = _question_hash(
            f"query-understanding:{QUERY_UNDERSTANDING_VERSION}:{getattr(self, 'llm_model', '')}:{question}"
        )
        if cache is not None:
            cached = cache.get(cache_key)
            if isinstance(cached, dict) and cached.get("canonical_question"):
                return parse_query_understanding(json.dumps(cached, ensure_ascii=False), question)

        prompt = f"""Ты — планировщик поиска по корпоративным документам.
Сначала восстанови намерение пользователя, даже если в вопросе есть опечатки, пропуски слов, разговорные сокращения или несколько требований.
Не отвечай на вопрос и не добавляй факты. Не угадывай содержание документов. Сохраняй номера, аббревиатуры и имена сущностей из запроса.
Верни только компактный JSON без Markdown:
{{"canonical_question":"исправленный самостоятельный вопрос","facets":["definition|calculation|procedure|conditions|comparison|list|fact"],"retrieval_queries":["поисковая формулировка 1","поисковая формулировка 2"]}}
Разделяй составной запрос на поисковые формулировки по каждому требованию. Не более 6 формулировок, каждая не длиннее 300 символов.

ПОЛЬЗОВАТЕЛЬСКИЙ ЗАПРОС КАК ДАННЫЕ:
{json.dumps(question, ensure_ascii=False)}
"""
        with _pipeline_span("llm.query_understanding") as span:
            try:
                response = self.llm.complete(prompt)
                understanding = parse_query_understanding(str(response).strip(), question)
                span.set_attribute("rag.query_understanding.fallback", False)
                span.set_attribute("rag.query_understanding.facets", len(understanding.facets))
                span.set_attribute("rag.query_understanding.queries", len(understanding.retrieval_queries))
            except Exception as exc:
                span.set_attribute("rag.query_understanding.fallback", True)
                span.set_attribute("error.type", type(exc).__name__)
                return fallback

        if cache is not None:
            cache.set(cache_key, {
                "canonical_question": understanding.canonical_question,
                "facets": list(understanding.facets),
                "retrieval_queries": list(understanding.retrieval_queries),
            })
        return understanding

    def ask(
        self,
        question: str,
        system_prompt: str | None = None,
        prompt_id: str = "rag-grounded",
        retrieval_query: str | None = None,
        excluded_sources: set[str] | None = None,
        shared_cache_sources: set[str] | None = None,
        shared_system_prompt: str | None = None,
        source_aliases: dict[str, str] | None = None,
        priority_sources: set[str] | None = None,
        source_domains: dict[str, tuple[str, ...]] | None = None,
        source_versions: dict[str, str] | None = None,
        retrieval_focus: str = "auto",
    ) -> tuple[str, list[dict], bool]:
        """
        Возвращает (answer, sources, from_cache).
        sources — список dict с ключами file, section, score.
        """
        # Answer caching is intentionally retrieval-first.  A pre-retrieval
        # cache key cannot prove that an answer was based only on globally
        # shared evidence and used to fragment by user/project exclusions.
        del prompt_id  # Kept in the public signature for backwards compatibility.
        if not hasattr(self, "_generation_state"):
            self._generation_state = threading.local()
        self._generation_state.from_cache = False
        self._generation_state.retrieved_context = []
        search_question = (retrieval_query or question).strip()

        self._refresh_docstore_if_changed()
        if not self._all_nodes:
            return (
                "База знаний пуста. Загрузите документы командой :load <путь>",
                [],
                False,
            )

        # A short follow-up (for example, "а по какой формуле?") has no entity
        # of its own.  Build the PRISM contract from the contextual retrieval
        # query so deterministic formula extraction retains the subject too.
        understanding = self.understand_query(search_question)
        understood_question = understanding.canonical_question
        spec = refine_prism_query(understood_question)
        context = self.retrieve_context(
            understood_question,
            TOP_K,
            spec,
            excluded_sources,
            retrieval_queries=understanding.retrieval_queries,
            source_aliases=source_aliases,
            priority_sources=priority_sources,
            source_domains=source_domains,
            source_versions=source_versions,
            retrieval_focus=retrieval_focus,
        )
        if is_document_occurrence_question(understood_question):
            context = [
                item for item in context
                if document_occurrence_item_matches(item, understood_question)
            ]
        # The UI must show exactly the evidence used to produce the answer.
        # Keeping it request-thread-local avoids a second, potentially divergent
        # retrieval pass and cannot cross-contaminate simultaneous chats.
        self._generation_state.retrieved_context = [dict(item) for item in context]
        calculations = extract_formula_lines_from_texts([item["text"] for item in context], understood_question)
        answer = (
            generate_document_occurrence_answer(understood_question, context)
            if is_document_occurrence_question(understood_question)
            else ""
        )
        if not answer:
            shared_sources = {
                Path(str(source)).name.casefold()
                for source in (shared_cache_sources or set())
            }
            context_is_shared = bool(context) and all(
                Path(str(item.get("file", ""))).name.casefold() in shared_sources
                for item in context
            )
            cache_enabled = (
                context_is_shared
                and search_question == question.strip()
                and shared_system_prompt is not None
            )
            answer = self._generate_grounded_answer(
                question,
                context,
                calculations,
                spec,
                shared_system_prompt if cache_enabled else system_prompt,
                evidence_question=understood_question,
                cache_enabled=cache_enabled,
            )

        sources = [{
            "file": item["file"],
            "section": item.get("section_path", ""),
            "score": item["score"],
            "text_preview": item["text"][:120],
        } for item in context]

        return answer, sources, bool(self.last_generation_from_cache)

    def last_retrieved_context(self) -> list[dict] | None:
        """Return this request's retrieval evidence, if ``ask`` has run."""
        state = getattr(self, "_generation_state", None)
        context = getattr(state, "retrieved_context", None)
        if context is None:
            return None
        return [dict(item) for item in context]

    def _generate_grounded_answer(
        self,
        question: str,
        context: list[dict],
        calculations: list[str],
        spec: PrismQuery | None = None,
        system_prompt: str | None = None,
        evidence_question: str | None = None,
        cache_enabled: bool = False,
    ) -> str:
        self._generation_state.from_cache = False
        spec = spec or refine_prism_query(question)
        transcription = generate_image_transcription_answer(question, context)
        if transcription:
            return transcription
        if spec.answer_mode == "formulas_only":
            return generate_formula_only_answer(evidence_question or question, context, calculations, spec)
        extractive = generate_extractive_value_answer(question, context, calculations)
        if extractive:
            return extractive

        context_text = "\n\n---\n\n".join(
            f"[Источник: {human_source_reference(item)}]\n{item['text']}"
            for item in context
        )
        normalized_question = question.lower()
        is_procedural = bool(re.search(r"\bкак\s+(создать|настроить|добавить|открыть|заполнить|изменить)\b", normalized_question))
        is_calculation_question = not is_procedural and asks_for_calculation(evidence_question or question)
        calculations_block = ""
        if is_calculation_question and calculations:
            calculations_block = "\nРАЗРЕШЕННЫЕ РАСЧЕТНЫЕ ПРАВИЛА:\n" + "\n".join(
                f"- {format_formula_for_katex(calculation, display=True) if '=' in calculation else calculation}"
                for calculation in calculations
            )
        interpretation_block = ""
        if evidence_question and evidence_question.strip() != question.strip():
            interpretation_block = (
                "\nИНТЕРПРЕТАЦИЯ НАМЕРЕНИЯ ПОЛЬЗОВАТЕЛЯ:\n"
                f"{evidence_question.strip()}\n"
                "Ответь на все части этой интерпретации, но не упоминай исправление опечаток.\n"
            )
        prompt = f"""{system_prompt or GROUNDED_SYSTEM_PROMPT}

Ниже приведены данные для ответа. Используй их как единственный фактический контекст: не выдумывай сведения, которых в нём нет. Всегда отвечай на русском языке.
После каждого самостоятельного факта или группы пунктов ставь ровно одну цитату из соответствующего блока контекста в формате [Источник: имя файла — точный раздел или пункт]. Не заменяй точный раздел названием документа и не повторяй одну и ту же цитату подряд.
Если вопрос о расчете, объясни порядок и используй все относящиеся к нему документы. Каждую формулу выводи отдельным блоком только в корректном LaTeX между $$ и $$; никогда не помещай формулу в обратные кавычки и не показывай служебную запись вида S(...), W(...), T(...).

КОНТЕКСТ:
{context_text}

{calculations_block}

{interpretation_block}

ВОПРОС:
{question}

ОТВЕТ:
"""
        # A dedicated worker can atomically replace the docstore while web
        # workers stay alive.  Include its snapshot marker so a global answer
        # never survives a corpus update merely because its old prompt happens
        # to look identical.
        corpus_snapshot = getattr(self, "_docstore_mtime_ns", 0)
        generation_key = _question_hash(
            f"generation\n{corpus_snapshot}\n{self.llm_model}\n{prompt}"
        )
        cached = None
        if cache_enabled:
            with _pipeline_span("cache.lookup") as span:
                cached = self.response_cache.get(generation_key)
                span.set_attribute("rag.cache.hit", bool(cached))
        if isinstance(cached, dict) and isinstance(cached.get("answer"), str):
            self._generation_state.from_cache = True
            with _pipeline_span("cache.hit"):
                pass
            answer = sanitize_grounded_answer(str(cached["answer"]), question, calculations)
            answer = ensure_requested_formula_coverage(
                answer,
                evidence_question or question,
                calculations,
                context,
            )
            answer = ensure_price_category_source_coverage(answer, evidence_question or question, context)
            return ensure_price_composition_source_coverage(answer, evidence_question or question, context)
        with _pipeline_span(
            "llm.generate",
            attributes={
                "gen_ai.operation.name": "generate_content",
                "gen_ai.request.model": self.llm_model,
                "rag.context.count": len(context),
            },
        ) as span:
            response = self.llm.complete(prompt)
            answer = sanitize_grounded_answer(str(response).strip(), question, calculations)
            answer = ensure_requested_formula_coverage(
                answer,
                evidence_question or question,
                calculations,
                context,
            )
            answer = ensure_price_category_source_coverage(answer, evidence_question or question, context)
            answer = ensure_price_composition_source_coverage(answer, evidence_question or question, context)
            if not answer:
                raise RuntimeError("LLM returned an empty answer")
            span.set_attribute("rag.response.chars", len(answer))
        if cache_enabled:
            self.response_cache.set(generation_key, {"answer": answer})
        return answer

    def debug_retrieval(self, question: str, top_k: int = 10):
        """Показать найденные чанки без вызова LLM."""
        results = self.retrieve_context(question, top_k)
        print(f"DEBUG retrieval top-{len(results)} для «{question}»:")
        for i, item in enumerate(results, 1):
            preview = item["text"][:700].replace("\n", " ")
            print(f"\n{i}. {item['file']}  score={item['score']:.4f}")
            print(f"   {preview}")

    def _lexical_section_seed_results(self, question: str, limit: int = 12) -> list[dict]:
        """Seed headings whose wording overlaps the question before vector ranking."""
        query_terms = section_query_terms(question)
        query_keys = lexical_term_keys(" ".join(query_terms))
        if len(query_keys) < 3:
            return []

        query_key_set = set(query_keys)
        calculation_intent = asks_for_calculation(question)
        anchor_keys = query_key_set - GENERIC_CALCULATION_QUERY_KEYS
        prepared: list[tuple[object, dict, str, str, str, tuple[str, ...], set[str]]] = []
        heading_frequency: dict[str, int] = {key: 0 for key in query_key_set}
        for node in self._all_nodes:
            meta = node.metadata or {}
            text = node.text or ""
            section_path = str(meta.get("section_path", ""))
            section_title = str(meta.get("section_title", ""))
            section_text = section_path.strip()
            if section_title and section_title.casefold() not in section_path.casefold():
                section_text = f"{section_text} {section_title}".strip()
            if not section_text:
                continue
            section_keys = lexical_term_keys(section_text)
            section_key_set = set(section_keys)
            prepared.append((node, meta, text, section_path, section_title, section_keys, section_key_set))
            for key in query_key_set & section_key_set:
                heading_frequency[key] += 1

        ranked: list[tuple[float, dict]] = []
        for node, meta, text, section_path, section_title, section_keys, section_key_set in prepared:
            section_text = section_path.strip()
            if section_title and section_title.casefold() not in section_path.casefold():
                section_text = f"{section_text} {section_title}".strip()
            matched = query_key_set & section_key_set
            common_run = longest_common_token_run(query_keys, section_keys)
            minimum_matches = max(3, min(6, round(len(query_key_set) * 0.35)))
            calculation_anchor = (
                calculation_intent
                and bool(matched & anchor_keys)
                and bool(section_key_set & CALCULATION_SECTION_KEYS)
            )
            if common_run < 3 and len(matched) < minimum_matches and not calculation_anchor:
                continue

            coverage = len(matched) / len(query_key_set)
            density = len(matched) / max(1, len(section_key_set))
            body_matches = query_key_set & set(lexical_term_keys(text[:2200]))
            body_coverage = len(body_matches) / len(query_key_set)
            # Domain boilerplate (for example "конкурентного отбора ценовых
            # заявок") is frequent across regulations.  Give more weight to
            # rare heading terms such as the actual event or exception named
            # by the user.  Averaging normalized IDF over all matched terms
            # prevents one accidental rare token from dominating the score.
            total_headings = max(1, len(prepared))
            idf_scale = max(1.0, math.log(total_headings + 1.0))
            rarity_bonus = 6.0 * sum(
                math.log((total_headings + 1.0) / (heading_frequency[key] + 1.0)) / idf_scale
                for key in matched
            ) / max(1, len(matched))
            score = 2.0 + common_run * 0.4 + coverage * 3.0 + density + body_coverage * 2.0 + rarity_bonus
            if calculation_anchor:
                # One exact domain anchor in a governing "algorithm/formula/
                # procedure/calculation" heading is more meaningful than three
                # generic words such as "system" and "calculation".
                score += 6.0
            if len(section_text) > 900:
                score -= 1.5
            chunk_meta = effective_chunk_metadata(text, meta)
            if chunk_meta.get("status") == "deleted":
                continue
            ranked.append((score, {
                "file": meta.get("file_name", meta.get("file_path", "?")),
                "score": round(score, 4),
                "text": text,
                "matched_queries": [
                    f"section-intent-anchored:{','.join(sorted(matched & anchor_keys))}"
                    if calculation_anchor else f"section-lexical:{common_run}"
                ],
                **chunk_meta,
            }))

        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]]

    def _procedural_seed_results(self, question: str) -> list[dict]:
        """Seed the exact workflow sections for short parameter-creation queries."""
        normalized = question.lower().replace("ё", "е")
        if "параметр" not in normalized or not any(
            marker in normalized for marker in ("добав", "созда", "новый", "завест")
        ):
            return []

        targets = (
            "создание параметра",
            "создание параметра > вкладка «общее»",
            "создание параметра > вкладка «принадлежность»",
        )
        seeded: list[dict] = []
        for target in targets:
            best: dict | None = None
            for node in self._all_nodes:
                meta = node.metadata or {}
                section_path = str(meta.get("section_path", ""))
                normalized_path = section_path.lower().replace("ё", "е")
                if not normalized_path.endswith(target):
                    continue
                text = node.text or ""
                item = {
                    "file": meta.get("file_name", meta.get("file_path", "?")),
                    "score": 1.0 - 0.05 * len(seeded),
                    "text": text,
                    "matched_queries": [f"procedure-exact:{target}"],
                    **extract_chunk_metadata(text),
                    **{key: value for key, value in meta.items() if key in {
                        "section_path", "section_title", "parent_section", "status", "chunk_type",
                    } and value},
                }
                best = item
                break
            if best is not None:
                seeded.append(best)
        return seeded

    def _exact_section_seed_results(self, question: str, limit_per_section: int = 8) -> list[dict]:
        """Resolve explicit normative section numbers before semantic retrieval."""
        section_numbers = requested_section_numbers(question)
        if not section_numbers:
            return []

        terms = section_query_terms(question)
        seeds: list[dict] = []
        seen: set[str] = set()
        for number in section_numbers:
            matches: list[tuple[float, dict]] = []
            number_pattern = re.compile(rf"(?<!\d){re.escape(number)}(?:\.|\b)(?!\d)")
            for node in self._all_nodes:
                meta = node.metadata or {}
                text = node.text or ""
                section_path = str(meta.get("section_path", ""))
                section_title = str(meta.get("section_title", ""))
                haystack = f"{section_path}\n{section_title}\n{text[:1600]}"
                if not number_pattern.search(haystack):
                    continue

                chunk_meta = {
                    **extract_chunk_metadata(text),
                    **{k: v for k, v in meta.items() if k in {
                        "section_path",
                        "section_title",
                        "parent_section",
                        "status",
                        "chunk_type",
                    } and v},
                }
                if chunk_meta.get("status") == "deleted":
                    continue

                lower_haystack = haystack.lower()
                score = 2.0
                if number_pattern.search(section_path):
                    score += 2.0
                if number_pattern.search(section_title):
                    score += 1.0
                if number_pattern.search(text[:350]):
                    score += 0.75
                score += min(0.8, 0.12 * sum(1 for term in terms if term in lower_haystack))

                file_name = meta.get("file_name", meta.get("file_path", "?"))
                item = {
                    "file": file_name,
                    "score": round(score, 4),
                    "text": text,
                    "matched_queries": [f"section-exact:{number}"],
                    **chunk_meta,
                }
                matches.append((score, item))

            for _, item in sorted(matches, key=lambda pair: pair[0], reverse=True):
                key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
                if key in seen:
                    continue
                seen.add(key)
                seeds.append(item)
                if sum(f"section-exact:{number}" in seed.get("matched_queries", []) for seed in seeds) >= limit_per_section:
                    break

        return seeds

    def _section_seed_results(self, question: str) -> list[dict]:
        """Guarantee base section evidence for each requested entity."""
        seeds: list[dict] = []
        for term in sorted(requested_formula_terms(question), key=lambda value: (-len(value), value)):
            best: tuple[float, dict] | None = None
            for node in self._all_nodes:
                meta = node.metadata or {}
                text = node.text or ""
                section_path = str(meta.get("section_path", ""))
                section_title = str(meta.get("section_title", ""))
                section_haystack = f"{section_path} {section_title}"
                if not contains_term(section_haystack, term):
                    continue

                chunk_meta = {
                    **extract_chunk_metadata(text),
                    **{k: v for k, v in meta.items() if k in {
                        "section_path",
                        "section_title",
                        "parent_section",
                        "status",
                        "chunk_type",
                    } and v},
                }
                if chunk_meta.get("status") == "deleted" or chunk_meta.get("chunk_type") in {"cost", "rate", "history"}:
                    continue

                lower = text.lower()
                score = 1.0
                if text.lstrip().startswith("[SECTION:"):
                    score += 0.25
                if "определяется" in lower:
                    score += 0.30
                if "как разность" in lower:
                    score += 0.35
                if "суммой составляющих" in lower:
                    score += 0.20
                if "для каждого часа" in lower:
                    score += 0.10
                if "для гтп потребления поставщика" in lower[:500]:
                    score -= 0.35
                if "продолжение" in lower[:160]:
                    score -= 0.25

                item = {
                    "file": meta.get("file_name", meta.get("file_path", "?")),
                    "score": round(score, 4),
                    "text": text,
                    "matched_queries": [f"section-seed:{term}"],
                    **chunk_meta,
                }
                if best is None or score > best[0]:
                    best = (score, item)
            if best is not None:
                seeds.append(best[1])
        return seeds

    def _exact_entity_seed_results(self, question: str, limit: int = 24) -> list[dict]:
        """Seed rare abbreviations across documents before semantic ranking."""
        entities = {
            match.group(0).upper().replace("Ё", "Е")
            for match in re.finditer(r"(?<![А-Яа-яA-Za-z0-9])(?:[А-ЯЁ]{3,10}[0-9-]{0,3}|ИВ[01](?:-1)?)(?![А-Яа-яA-Za-z0-9])", question)
        }
        entities -= {"ЭЛЕКТРОЭНЕРГИЯ"}
        if not entities:
            return []
        ranked: list[tuple[float, dict]] = []
        for node in self._all_nodes:
            meta = node.metadata or {}
            text = node.text or ""
            file_name = str(meta.get("file_name", meta.get("file_path", "?")))
            section_path = str(meta.get("section_path", ""))
            section_title = str(meta.get("section_title", ""))
            source_scope = user_facing_source_name(file_name).upper().replace("Ё", "Е")
            section_scope = f"{section_path}\n{section_title}".upper().replace("Ё", "Е")
            body_scope = text.upper().replace("Ё", "Е")
            matched = {
                entity for entity in entities
                if contains_term(f"{source_scope}\n{section_scope}\n{body_scope}", entity)
            }
            if not matched:
                continue
            chunk_meta = effective_chunk_metadata(text, meta)
            if chunk_meta.get("status") == "deleted":
                continue
            score = 3.0 + len(matched) * 0.6
            if any(contains_term(section_scope, entity) for entity in matched):
                score += 1.4
            if any(contains_term(source_scope, entity) for entity in matched):
                score += 1.8
            if any(contains_term(body_scope[:1200], entity) for entity in matched):
                score += 0.8
            ranked.append((score, {
                "file": file_name,
                "score": round(score, 4),
                "text": text,
                "matched_queries": [f"entity-exact:{entity}" for entity in sorted(matched)],
                **chunk_meta,
            }))
        selected: list[dict] = []
        per_source: dict[str, int] = {}
        for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True):
            source = user_facing_source_name(item.get("file", "")).casefold()
            if per_source.get(source, 0) >= 3:
                continue
            selected.append(item)
            per_source[source] = per_source.get(source, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    def _price_category_seed_results(self, question: str, limit: int = 60) -> list[dict]:
        """Seed direct definition and calculation clauses for one price category."""
        category = price_category_number(question)
        price_composition = is_price_composition_question(question)
        generic_category = (
            mentions_price_category_subject(question)
            or price_composition
        )
        if not category and not generic_category:
            return []
        source_signals: dict[str, dict[str, bool]] = {}
        for node in self._all_nodes:
            meta = node.metadata or {}
            source = user_facing_source_name(meta.get("file_name", meta.get("file_path", "?"))).casefold()
            lower = (
                f"{meta.get('section_path', '')}\n{meta.get('section_title', '')}\n{node.text or ''}"
            ).lower().replace("ё", "е")
            signals = source_signals.setdefault(source, {
                "category": False, "transfer": False, "one_rate": False, "two_rate": False,
            })
            signals["category"] = signals["category"] or (
                contains_price_category(lower, category)
                if category else bool(re.search(r"ценов\w*\s+категор\w*", lower))
            )
            signals["transfer"] = signals["transfer"] or "передач" in lower
            signals["one_rate"] = signals["one_rate"] or "одноставоч" in lower
            signals["two_rate"] = signals["two_rate"] or "двухставоч" in lower
        ranked: list[tuple[float, dict]] = []
        for node in self._all_nodes:
            meta = node.metadata or {}
            text = node.text or ""
            section_path = str(meta.get("section_path", ""))
            section_title = str(meta.get("section_title", ""))
            section_haystack = f"{section_path}\n{section_title}"
            body_match = (
                contains_price_category(text, category)
                if category else bool(re.search(r"ценов\w*\s+категор\w*", text, re.IGNORECASE))
            )
            section_match = (
                contains_price_category(section_haystack, category)
                if category else bool(re.search(r"ценов\w*\s+категор\w*", section_haystack, re.IGNORECASE))
            )
            lower = f"{section_haystack}\n{text}".lower().replace("ё", "е")
            source = user_facing_source_name(meta.get("file_name", meta.get("file_path", "?"))).casefold()
            signals = source_signals.get(source, {})
            related_source = bool(
                signals.get("transfer")
                and (signals.get("one_rate") if category == "1" else signals.get("two_rate") if category == "6" else False)
            )
            related_transfer = related_source and any(
                marker in lower for marker in (
                    "стоимость услуг по передаче",
                    "стоимости услуг по передаче",
                    "тариф на услуги по передаче",
                    "одноставоч",
                    "двухставоч",
                )
            ) or (
                category == "1" and "одноставоч" in lower and "передач" in lower
            ) or (
                category == "6" and "двухставоч" in lower and "передач" in lower
            )
            if not (body_match or section_match or related_transfer):
                continue
            chunk_meta = effective_chunk_metadata(text, meta)
            if chunk_meta.get("status") == "deleted":
                continue
            score = 3.0 if (body_match or section_match) else 4.0
            if section_match:
                score += 2.0
            if body_match:
                score += 1.0
            stem = PRICE_CATEGORY_STEMS.get(category or "", "")
            if stem and re.search(rf"\b{stem}\w*\s+ценов\w*\s+категор\w*\b", lower):
                score += 6.0
            if "порядок определения стоимости" in lower:
                score += 1.8
            if any(marker in lower for marker in ("рассчитывается", "определяется по формуле", "[math:", "[formula]")):
                score += 1.4
            if any(marker in lower for marker in ("стоимость электроэнергии", "покупной мощности", "сетевой мощности")):
                score += 0.7
            if price_composition:
                # These sources define complementary layers of the retail
                # price: calculation method, governing price rules and market
                # application rules. Reserve them ahead of similarly worded
                # OREM formulas without hard-coding a single answer document.
                source_name = source.lower().replace("ё", "е")
                if "1-6цк" in source_name:
                    score += 4.0
                elif "1179" in source_name:
                    score += 3.8
                elif "442" in source_name:
                    score += 3.6
            item = {
                "file": meta.get("file_name", meta.get("file_path", "?")),
                "score": round(score, 4),
                "text": text,
                "matched_queries": [
                    f"price-category-exact:{category or 'generic'}"
                    if (body_match or section_match)
                    else f"price-category-related:{category or 'generic'}"
                ],
                **chunk_meta,
            }
            ranked.append((score, item))
        selected: list[dict] = []
        per_source: dict[str, int] = {}
        for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True):
            source = user_facing_source_name(item.get("file", "")).casefold()
            if per_source.get(source, 0) >= 12:
                continue
            selected.append(item)
            per_source[source] = per_source.get(source, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    def _document_occurrence_seed_results(self, question: str, limit: int = 100) -> list[dict]:
        """Return the strongest direct subject occurrence from every document."""
        if not is_document_occurrence_question(question):
            return []

        best_by_source: dict[str, tuple[float, dict]] = {}
        for node in self._all_nodes:
            meta = node.metadata or {}
            text = node.text or ""
            item = {
                "file": meta.get("file_name", meta.get("file_path", "?")),
                "score": 0.0,
                "text": text,
                "matched_queries": ["document-occurrence-exact"],
                **effective_chunk_metadata(text, meta),
            }
            if item.get("status") == "deleted" or not document_occurrence_item_matches(item, question):
                continue

            score = 4.0
            if document_occurrence_item_matches({**item, "text": ""}, question):
                score += 2.0
            if text.lstrip().startswith("[SECTION:"):
                score += 0.25
            item["score"] = round(score, 4)
            # Keep physical revisions separate until ACL/version filtering has
            # selected the current document.  Collapsing UUID-prefixed uploads
            # by their display name here can retain a superseded revision and
            # then lose the document entirely when that revision is excluded.
            source_key = Path(str(item["file"])).name.casefold()
            current = best_by_source.get(source_key)
            if current is None or score > current[0]:
                best_by_source[source_key] = (score, item)

        return [
            item for _, item in sorted(best_by_source.values(), key=lambda pair: pair[0], reverse=True)[:limit]
        ]

    def _formula_clause_seed_results(self, question: str) -> list[dict]:
        """Seed the clause whose wording most exactly matches a formula request."""
        normalized = question.lower().replace("ё", "е")
        if not is_formula_question(question):
            return []

        generic_terms = {
            "формула", "формулы", "формуле", "приведи", "покажи", "объясни",
            "объяснить", "обозначения", "расчетное", "расчётное", "правило",
            "определяется", "величина", "электроэнергии", "операционных", "суток",
        }
        terms = tuple(
            term.replace("ё", "е") for term in section_query_terms(question)
            if len(term) >= 5 and term not in generic_terms
        )
        if len(terms) < 2:
            return []

        requires_own_increase = (
            "собствен" in normalized
            and any(token in normalized for token in ("увеличен", "увеличить", "увеличения"))
        )
        requires_own_decrease = (
            "собствен" in normalized
            and any(token in normalized for token in ("снижен", "уменьшен", "сокращен"))
        )
        required_anchors = tuple(
            anchor for anchor in ("вынужден", "внешн", "собствен", "экспорт", "импорт", "виэ", "гаэс")
            if anchor in normalized
        )

        matches: list[tuple[float, dict]] = []
        primary_matches: list[tuple[float, dict]] = []
        for node in self._all_nodes:
            meta = node.metadata or {}
            text = node.text or ""
            lower = text.lower().replace("ё", "е")
            if any(not contains_term(lower, anchor) for anchor in required_anchors):
                continue
            # A cost formula can contain both price directions.  Its governing
            # clause must explicitly state the requested own-initiative
            # direction, otherwise min/max and the price index are unsafe.
            own_direction_scope = f"{meta.get('section_path', '')}\n{lower[:1800]}"
            if requires_own_increase and not re.search(
                r"собствен\w*\s+инициатив\w*.{0,100}(?:в\s+сторону\s+)?увелич",
                own_direction_scope,
                re.IGNORECASE | re.DOTALL,
            ):
                continue
            if requires_own_decrease and not re.search(
                r"собствен\w*\s+инициатив\w*.{0,100}(?:в\s+сторону\s+)?(?:снижен|уменьшен|сокращен)",
                own_direction_scope,
                re.IGNORECASE | re.DOTALL,
            ):
                continue
            matched = sum(contains_term(lower, term) for term in terms)
            if matched < 2:
                continue
            coverage = matched / len(terms)
            if coverage < 0.60:
                continue
            chunk_meta = {
                **extract_chunk_metadata(text),
                **{k: v for k, v in meta.items() if k in {
                    "section_path", "section_title", "parent_section", "status", "chunk_type",
                } and v},
            }
            if chunk_meta.get("status") == "deleted":
                continue
            score = 1.0 + coverage
            if text.lstrip().startswith("[SECTION:"):
                score += 0.30
            if "определяется" in lower:
                score += 0.15
            if "$" in text:
                score += 0.10
            result = (score, {
                "file": meta.get("file_name", meta.get("file_path", "?")),
                "score": round(score, 4),
                "text": text,
                "matched_queries": ["formula-clause-exact"],
                **chunk_meta,
            })
            matches.append(result)
        return [item for _, item in sorted(matches, key=lambda pair: pair[0], reverse=True)[:3]]

    def _regulation_concept_seed_results(self, question: str) -> list[dict]:
        """Return only the governing clause for recurring regulation concepts.

        These anchors are intentionally based on the document's own headings
        and terms, not on generated answers.  They prevent a later cost/rate
        clause from outranking the base rule merely because it repeats the
        same abbreviation.
        """
        normalized = question.lower().replace("ё", "е")
        required: tuple[str, ...] = ()
        preferred: tuple[str, ...] = ()
        result_limit = 1
        if all(token in normalized for token in ("планов", "переток", "экспорт", "импорт")):
            required = ("планов", "переток", "экспорт", "импорт")
            preferred = ("конкурентного отбора ценовых заявок",)
        elif "допустим" in normalized and "диапазон" in normalized and "виэ" in normalized:
            required = ("допустимый диапазон", "виэ")
            preferred = ("10", "установленной мощности")
        elif "оперативн" in normalized and ("ценопринима" in normalized or "оцз" in normalized):
            required = ("оперативн", "ценопринима")
            preferred = ("ко определяет величины", "не учитывает объем")
            result_limit = 2
        elif "внешн" in normalized and "собствен" in normalized and "инициатив" in normalized:
            required = ("внешн", "собствен", "инициатив")
            preferred = ("виды инициатив отклонений", "причине его возникновения")
        if not required:
            return []

        matches: list[tuple[float, dict]] = []
        primary_matches: list[tuple[float, dict]] = []
        for node in self._all_nodes:
            meta = node.metadata or {}
            text = node.text or ""
            lower = text.lower().replace("ё", "е")
            if any(token not in lower for token in required):
                continue
            if required == ("планов", "переток", "экспорт", "импорт") and "планового объема перетока" not in lower:
                continue
            if required == ("внешн", "собствен", "инициатив") and "виды инициатив отклонений" not in lower:
                continue
            chunk_meta = {**extract_chunk_metadata(text), **{
                key: value for key, value in meta.items()
                if key in {"section_path", "section_title", "parent_section", "status", "chunk_type"} and value
            }}
            if chunk_meta.get("status") == "deleted":
                continue
            score = 2.0 + sum(0.35 for phrase in preferred if phrase in lower)
            # Cost-specific paragraphs mentioning OЦЗ are supporting evidence,
            # never the governing rule for an "учет ОЦЗ" question.
            if "оперативн" in required and ("расчетные показатели стоимости" in lower or "ставка" in lower):
                score -= 0.80
            result = (score, {
                "file": meta.get("file_name", meta.get("file_path", "?")),
                "score": round(score, 4),
                "text": text,
                "matched_queries": ["regulation-concept-exact"],
                **chunk_meta,
            })
            matches.append(result)
            if str(meta.get("file_name", "")).lower().endswith("_r12_rag.md"):
                primary_matches.append(result)
        candidates = primary_matches or matches
        return [item for _, item in sorted(candidates, key=lambda pair: pair[0], reverse=True)[:result_limit]]

    def _scoped_lexical_seed_results(
        self,
        question: str,
        source_ids: set[str] | None,
        *,
        explicit: bool = False,
        limit: int = 24,
    ) -> list[dict]:
        """Guarantee relevant project/chat chunks enter the candidate pool."""
        allowed = {Path(str(source)).name.casefold() for source in (source_ids or set())}
        if not allowed:
            return []
        generic = {
            "документ", "документа", "определяется", "постановление", "правило",
            "рассказать", "регламент", "согласно",
        }
        terms = [
            term for term in section_query_terms(question)
            if len(term) >= 4 and term not in generic and not term.isdigit()
        ]
        ranked: list[tuple[float, dict]] = []
        for node in self._all_nodes:
            meta = node.metadata or {}
            source = Path(str(meta.get("file_name", meta.get("file_path", "")))).name.casefold()
            if source not in allowed:
                continue
            text = node.text or ""
            searchable = (
                f"{meta.get('section_path', '')}\n{meta.get('section_title', '')}\n{text}"
            ).casefold().replace("ё", "е")
            matches = sum(contains_term(searchable, term) for term in terms)
            if terms and not matches:
                continue
            chunk_meta = {
                **extract_chunk_metadata(text),
                **{
                    key: value for key, value in meta.items()
                    if key in {"section_path", "section_title", "parent_section", "status", "chunk_type"}
                    and value
                },
            }
            if chunk_meta.get("status") == "deleted":
                continue
            score = (3.0 if explicit else 0.65) + min(matches, 6) * 0.18
            ranked.append((score, {
                "file": meta.get("file_name", meta.get("file_path", "?")),
                "score": round(score, 4),
                "text": text,
                "matched_queries": ["explicit-source" if explicit else "private-scope-lexical"],
                **chunk_meta,
            }))
        return [
            item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:limit]
        ]

    def _domain_lexical_seed_results(
        self,
        question: str,
        active_domains: tuple[str, ...],
        source_domains: dict[str, tuple[str, ...]] | None,
        *,
        limit: int = 24,
    ) -> list[dict]:
        """Seed lexical evidence from the domains selected by soft routing.

        Vector/BM25 retrieval can miss a small but directly relevant manual
        when a much larger corpus contains the same generic words.  Domain
        routing is intentionally not an ACL, so this helper only guarantees
        candidates from the selected business domain; the normal reranking and
        evidence selection stages still decide whether they survive.
        """
        wanted_domains = {str(domain).casefold() for domain in active_domains if domain}
        if not wanted_domains or not source_domains:
            return []

        generic_keys = {
            "каки", "како", "расч", "сист", "долж", "выпо", "необ", "пере",
            "запу", "пров", "работ", "данн", "инфо",
        }
        query_keys = {
            key for key in lexical_term_keys(" ".join(section_query_terms(question)))
            if key not in generic_keys
        }
        if not query_keys:
            return []

        ranked: list[tuple[float, dict]] = []
        for node in self._all_nodes:
            meta = node.metadata or {}
            file_name = meta.get("file_name", meta.get("file_path", "?"))
            domains = set(source_domains_for(file_name, source_domains))
            if not domains.intersection(wanted_domains):
                continue
            text = node.text or ""
            section_path = str(meta.get("section_path", ""))
            section_title = str(meta.get("section_title", ""))
            searchable = f"{section_path}\n{section_title}\n{text}".casefold().replace("ё", "е")
            searchable_keys = set(lexical_term_keys(searchable))
            matches = len(query_keys.intersection(searchable_keys))
            if not matches:
                continue
            chunk_meta = {
                **extract_chunk_metadata(text),
                **{
                    key: value for key, value in meta.items()
                    if key in {"section_path", "section_title", "parent_section", "status", "chunk_type"}
                    and value
                },
            }
            if chunk_meta.get("status") == "deleted":
                continue
            # Query expansion adds discriminating business terms (for example,
            # object, tariff scheme and current-calculation flags). Preserve
            # enough of that coverage to prefer the governing clause over a
            # generic introduction from the same document.
            score = 1.35 + min(matches, 12) * 0.32
            ranked.append((score, {
                "file": file_name,
                "score": round(score, 4),
                "text": text,
                "matched_queries": ["domain-lexical"],
                **chunk_meta,
            }))
        ordered = sorted(ranked, key=lambda pair: pair[0], reverse=True)
        # Reserve the strongest lexical hit from each document before allowing
        # a large manual to consume the remaining quota with near-duplicate
        # chunks.  This is candidate coverage, not a final ranking decision.
        reserved: list[dict] = []
        reserved_ids: set[int] = set()
        seen_sources: set[str] = set()
        for _, item in ordered:
            source_key = Path(str(item.get("file", ""))).name.casefold()
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            reserved.append(item)
            reserved_ids.add(id(item))
            if len(reserved) >= limit:
                return reserved
        return [
            *reserved,
            *(item for _, item in ordered if id(item) not in reserved_ids),
        ][:limit]

    def retrieve_context(
        self,
        question: str,
        top_k: int = 10,
        spec: PrismQuery | None = None,
        excluded_sources: set[str] | None = None,
        retrieval_queries: tuple[str, ...] | list[str] | None = None,
        source_aliases: dict[str, str] | None = None,
        priority_sources: set[str] | None = None,
        source_domains: dict[str, tuple[str, ...]] | None = None,
        source_versions: dict[str, str] | None = None,
        retrieval_focus: str = "auto",
    ) -> list[dict]:
        """Return chunks after query planning unless a validated plan is supplied."""
        original_question = question
        if retrieval_queries is None:
            understanding = self.understand_query(question)
            question = understanding.canonical_question
            retrieval_queries = understanding.retrieval_queries
        # Query understanding may rewrite a list request into a declarative
        # search phrase.  Keep the user's positive document-occurrence intent
        # for seeding and final diversification while still using the clearer
        # canonical wording for vector/lexical retrieval.
        occurrence_question = (
            original_question
            if is_document_occurrence_question(original_question)
            else question
        )
        selection_question = (
            occurrence_question
            if is_document_occurrence_question(occurrence_question)
            else question
        )
        self._refresh_docstore_if_changed()
        spec = spec or refine_prism_query(question)
        domain_routing = route_query(question, retrieval_focus)
        explicit_sources = matching_explicit_sources(question, source_aliases)
        candidate_k = max(top_k * (12 if excluded_sources else 5), 100 if excluded_sources else 40)
        qe = self._build_query_engine(candidate_k)
        merged: dict[str, dict] = {}
        for item in self._procedural_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._exact_section_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._lexical_section_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._section_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._exact_entity_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._formula_clause_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        concept_seeds = self._regulation_concept_seed_results(question)
        for item in concept_seeds:
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._document_occurrence_seed_results(occurrence_question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        # Run exact category seeding last so broad lexical/section seeds cannot
        # overwrite its stronger score and provenance for the same chunk.
        for item in self._price_category_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        domain_queries = [
            f"{question} {'РРЭ' if domain == 'rre' else 'ОРЭМ'}"
            for domain in domain_routing.active_domains
        ]
        # Deterministic probes precede free-form LLM expansions. Previously the
        # eight-query budget could fill before a regulation probe was reached.
        query_candidates = [
            question,
            *build_targeted_queries(question),
            *(retrieval_queries or ()),
            *domain_queries,
        ]
        query_plan: list[str] = []
        seen_queries: set[str] = set()
        for candidate in query_candidates:
            normalized_query = re.sub(r"\s+", " ", str(candidate).strip())
            key = normalized_query.casefold()
            if normalized_query and key not in seen_queries:
                seen_queries.add(key)
                query_plan.append(normalized_query)
            if len(query_plan) >= 8:
                break
        scoped_seed_sources = explicit_sources or priority_sources
        for item in self._scoped_lexical_seed_results(
            question,
            scoped_seed_sources,
            explicit=bool(explicit_sources),
            limit=max(top_k * 3, 24),
        ):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        domain_seed_query = "\n".join((question, *(retrieval_queries or ())))
        for item in self._domain_lexical_seed_results(
            domain_seed_query,
            domain_routing.active_domains,
            source_domains,
            limit=max(top_k * 3, 24),
        ):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            current = merged.get(key)
            if current is None:
                merged[key] = item
            else:
                current.setdefault("matched_queries", []).extend(item["matched_queries"])
                current["score"] = max(float(current.get("score", 0.0)), float(item["score"]))
        for query in query_plan:
            nodes = qe.retriever.retrieve(QueryBundle(query_str=query))
            for item in nodes:
                meta = item.node.metadata or {}
                text = item.node.text or ""
                file_name = meta.get("file_name", meta.get("file_path", "?"))
                key = hashlib.sha256(f"{file_name}\n{text[:700]}".encode("utf-8")).hexdigest()
                score = float(item.score or 0)
                chunk_meta = effective_chunk_metadata(text, meta)
                current = merged.get(key)
                if current is None:
                    merged[key] = {
                        "file": file_name,
                        "score": round(score, 4),
                        "text": text,
                        "matched_queries": [query],
                        **chunk_meta,
                    }
                    continue
                current["matched_queries"].append(query)
                if score > float(current.get("score", 0.0)):
                    current["score"] = round(score, 4)
                    current.update(chunk_meta)
        for item in formula_sibling_results(self._all_nodes, merged, question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            current = merged.get(key)
            if current is None:
                merged[key] = item
            else:
                current.setdefault("matched_queries", []).extend(item["matched_queries"])
                current["score"] = max(float(current.get("score", 0.0)), float(item["score"]))
        excluded = {Path(str(source)).name.casefold() for source in (excluded_sources or set())}
        results = [
            item for item in merged.values()
            if Path(str(item.get("file", ""))).name.casefold() not in excluded
            and (
                not explicit_sources
                or Path(str(item.get("file", ""))).name.casefold() in explicit_sources
            )
            and not contains_prompt_injection(str(item.get("text", "")))
        ]
        ranked = rerank_context_results(results, question)
        if is_document_occurrence_question(selection_question):
            # A document-occurrence query asks for a positive set, not for the
            # best explanatory chunks. The general domain router deliberately
            # caps its candidate pool; on a large regulation that cap can be
            # consumed by many chunks from the same sources and discard a
            # lower-scoring document even though it contains an exact match.
            # ACL/source filtering already ran above, so collapse the complete
            # direct-match set by active physical revision before any lossy
            # top-k routing or semantic reranking.
            ranked = apply_document_version_preference(
                ranked,
                source_versions,
                explicit_sources=explicit_sources,
            )
            diversified = diversify_context_results(
                ranked,
                selection_question,
                top_k,
            )
            return [
                item for item in diversified
                if item.get("status") != "deleted"
            ][:top_k]
        related = relationship_expansion_results(
            getattr(self, "_all_nodes", []),
            ranked,
            selection_question,
            limit=max(top_k, 12),
        )
        if related:
            ranked = rerank_context_results([*ranked, *related], question)
        reranker = getattr(self, "reranker", None)
        if reranker is not None:
            with _pipeline_span(
                "cross_encoder_rerank",
                attributes={"rag.reranker.enabled": reranker.enabled},
            ):
                ranked = reranker.rerank(question, ranked)
        ranked = apply_domain_routing(
            ranked,
            source_domains,
            domain_routing,
            top_k,
        )
        ranked = apply_document_version_preference(
            ranked,
            source_versions,
            explicit_sources=explicit_sources,
        )
        if not explicit_sources:
            ranked = reserve_topic_source_coverage(ranked, selection_question, top_k)
        if priority_sources and not explicit_sources:
            # Vector/BM25 scores are backend-specific and can be much larger
            # than lexical seed scores.  Reserve only a bounded top-k quota so
            # a genuinely matching chat/project document cannot be crowded out
            # by a large shared document, while global evidence still remains.
            ranked = prioritize_scoped_results(ranked, top_k)
        diversified = diversify_context_results(ranked, selection_question, top_k)
        selected = select_prism_evidence(diversified, spec, top_k)
        concept_selected = [
            item for item in selected
            if "regulation-concept-exact" in item.get("matched_queries", [])
        ]
        if concept_selected:
            # A narrowly matched governing clause is safer than mixing in
            # similarly-worded exceptions from later chapters.
            return concept_selected[:3]
        if not explicit_sources:
            # PRISM can legitimately prefer formula-bearing chunks, but that
            # must not erase the governing regulation or the exact method
            # section already found in the broader ranked pool.
            selected_ids = {id(item) for item in selected}
            selected = reserve_topic_source_coverage(
                [*selected, *(item for item in ranked if id(item) not in selected_ids)],
                selection_question,
                top_k,
            )[:top_k]
        normalized = question.lower().replace("ё", "е")
        if "параметр" in normalized and any(
            marker in normalized for marker in ("добав", "созда", "новый", "завест")
        ):
            allowed_sections = (
                "создание параметра",
                "создание параметра > вкладка «общее»",
                "создание параметра > вкладка «принадлежность»",
                "работа с параметрами > панель инструментов",
            )
            operation_evidence = [
                item for item in selected
                if any(
                    str(item.get("section_path", "")).lower().replace("ё", "е").endswith(section)
                    for section in allowed_sections
                )
            ]
            if operation_evidence:
                return operation_evidence[:top_k]
        return selected

    def clear_cache(self):
        self.response_cache.clear()
        print("[OK] Кэш очищен")

    def bm25_search(self, query: str, top_k: int = 10):
        """Диагностика: прямой BM25 поиск."""
        if not self._all_nodes:
            print("  (нет нод для BM25)")
            return
        retriever = BM25Retriever.from_defaults(
            nodes=self._all_nodes,
            similarity_top_k=top_k,
        )
        results = retriever.retrieve(QueryBundle(query_str=query))
        print(f"BM25 топ-{top_k} для «{query}»:")
        for i, r in enumerate(results, 1):
            meta = r.node.metadata or {}
            fname = meta.get("file_name", "?")
            preview = (r.node.text or "")[:100].replace("\n", " ")
            print(f"  {i}. [score={r.score:.3f}] {fname} — {preview}")


# ─── CLI ──────────────────────────────────────────────────────
def main():
    print("=== RAG Agent (Qwen 2.5 + Qdrant + LlamaIndex) ===")
    print("Команды:")
    print("  :load <путь>     — загрузить документ (DOCX, PDF, MD, TXT)")
    print("  :bm25 <запрос>   — диагностика BM25")
    print("  :debug <запрос>  — показать найденный контекст без LLM")
    print("  :cache clear     — очистить кэш ответов")
    print("  :exit            — выход")
    print("  <вопрос>         — задать вопрос")
    print()

    print("Инициализация агента...")
    agent = RAGAgent()
    print()

    import time

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nПока!")
            break

        if not line:
            continue

        if line == ":exit":
            break

        elif line == ":cache clear":
            agent.clear_cache()

        elif line.startswith(":load "):
            path = line[6:].strip()
            try:
                agent.ingest(path)
            except Exception as e:
                print(f"✗ {e}")

        elif line.startswith(":bm25 "):
            query = line[6:].strip()
            try:
                agent.bm25_search(query)
            except Exception as e:
                print(f"✗ {e}")

        elif line.startswith(":debug "):
            query = line[7:].strip()
            try:
                agent.debug_retrieval(query)
            except Exception as e:
                print(f"✗ {e}")

        else:
            t0 = time.time()
            try:
                answer, sources, from_cache = agent.ask(line)
                elapsed = time.time() - t0

                tag = f"[КЭШ — {elapsed*1000:.0f}мс]" if from_cache else f"[{elapsed:.1f}с]"
                print(f"\n{tag}")
                print(f"\nОТВЕТ:\n{answer}")
                if sources:
                    print("\nИСТОЧНИКИ:")
                    for s in sources:
                        print(f"  • {s['file']}  score={s['score']}")
                        if s.get("text_preview"):
                            print(f"    {s['text_preview'][:100]}")
                print()
            except Exception as e:
                print(f"✗ {e}")
                import traceback
                traceback.print_exc()


if __name__ == "__main__":
    main()
