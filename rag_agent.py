"""
RAG Agent — LlamaIndex + Qdrant + Ollama (qwen2.5:14b)
Гибридный поиск: векторный (Qdrant) + BM25
Детерминизм: temperature=0, seed=42, SQLite кэш по хэшу вопроса
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from opentelemetry.trace import Status, StatusCode

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
from llm_providers import ProviderLLM
from telemetry import PIPELINE_SECONDS, tracer

# ─── Настройки ────────────────────────────────────────────────
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
LLM_MODEL       = "qwen2.5:14b"
EMBED_MODEL      = "nomic-embed-text"   # лёгкая, быстрая, хорошая для рус/англ

QDRANT_URL       = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
QDRANT_COLLECTION = "rag_docs_v10"

CHUNK_SIZE       = 1024
CHUNK_OVERLAP    = 128
TOP_K            = 10      # финальных чанков в контексте
BM25_TOP_K       = 12      # кандидатов от BM25
VECTOR_TOP_K     = 12      # кандидатов от вектора

CACHE_DB         = os.getenv("CACHE_DB", "cache.db")
BM25_PERSIST_DIR = "bm25_index_v10"   # сохраняем ноды для BM25
CACHE_VERSION    = "v19-qwen35-profile"

# Model-specific generation budgets. Qwen 3.5 enables long reasoning by default
# and advertises a 262k context window; neither is appropriate for this local RAG
# request path, which supplies a bounded set of document chunks.
OLLAMA_MODEL_PROFILES: dict[str, dict] = {
    "qwen3.5:9b": {
        "context_window": 8192,
        "thinking": False,
        "num_predict": 768,
        "request_timeout": 120.0,
    },
}
DEFAULT_OLLAMA_PROFILE = {
    "context_window": 16384,
    "thinking": None,
    "num_predict": 1024,
    "request_timeout": 180.0,
}


@contextmanager
def _pipeline_span(
    stage: str,
    *,
    flow: str = "python",
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Measure one content-free RAG stage in Prometheus and OpenTelemetry."""
    started = time.perf_counter()
    with tracer().start_as_current_span(
        f"rag.{stage}",
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("rag.pipeline.stage", stage)
        span.set_attribute("rag.flow", flow)
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        try:
            yield span
        except BaseException as exc:
            # Exception messages may contain provider output. Keep only the type.
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(exc).__name__)
            raise
        finally:
            PIPELINE_SECONDS.labels(stage=stage, flow=flow).observe(time.perf_counter() - started)


class _TracedRetriever:
    """Transparent retriever proxy that exposes vector/BM25 child timings."""

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


SYSTEM_PROMPT = """\
Ты — точный ассистент по нормативным документам. Отвечай ТОЛЬКО на основе предоставленного контекста.

Правила:
1. Если информация есть в контексте — дай точный ответ со ссылкой на раздел.
2. Если нужной информации НЕТ в контексте — прямо скажи об этом, не придумывай.
3. Для формул и числовых значений — цитируй точно, без округлений.
4. Аббревиатуры (ИВ0, ИВ1, ИВ0-1, ИС и т.п.) — используй только те определения, которые явно указаны в контексте.
5. Не смешивай определения разных разделов.
6. Если в контексте формулы даны в маркерах вида [С_sub(...)] или [ΔО_sub(...)], сохраняй этот формат и не раскладывай его на отдельные токены вроде [EQ], [s], [].
"""


GROUNDED_SYSTEM_PROMPT = """\
Ты — точный ассистент по нормативным документам. Работай как проверяемый RAG-ответчик.

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


def refine_prism_query(question: str) -> PrismQuery:
    """Turn a user question into a small, inspectable PRISM retrieval contract."""
    normalized = question.lower()
    target_sections = requested_section_numbers(question)
    asks_for_formulas = any(marker in normalized for marker in ("формул", "формула", "уравнен", "расчетное правило"))
    intent = "formula_extraction" if asks_for_formulas else detect_query_intent(question)
    answer_mode = "formulas_only" if asks_for_formulas else "grounded_answer"

    excluded: list[str] = ["history"]
    is_value_question = detect_query_intent(question) == "value"
    explicitly_excludes_cost = any(marker in normalized for marker in ("без стоимости", "не стоимость", "кроме стоимости"))
    if is_value_question or explicitly_excludes_cost:
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
    """Нормализуем и хэшируем вопрос для кэша."""
    normalized = " ".join(
        c for c in question.lower() if c.isalpha() or c.isspace()
    ).split()
    normalized_str = " ".join(normalized)
    return hashlib.sha256(f"{CACHE_VERSION}:{normalized_str}".encode()).hexdigest()


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
    text = re.sub(r"\s{2,}", " ", text)
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
    normalized = question.lower()
    cost_markers = ("стоим", "ставк", "руб", "оплат", "требован", "обязательств", "платеж")
    value_markers = ("величин", "объем", "объём", "отклон", "рассчит", "расчет", "расчёт", "формул", "определ", "порядок", "учет", "учёт")
    if any(marker in normalized for marker in cost_markers) or re.search(r"\bцен(?:а|ы|е|у|ой|ам|ах)\b", normalized):
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
        "что", "как", "это", "есть", "найди", "покажи",
    }
    terms = [
        token
        for token in re.findall(r"[а-яёa-z0-9-]{4,}", value)
        if token not in stop
    ]
    return tuple(dict.fromkeys(terms))


def build_targeted_queries(question: str) -> list[str]:
    """Build deterministic retrieval probes for each requested abbreviation."""
    intent = detect_query_intent(question)
    terms = sorted(requested_formula_terms(question), key=lambda value: (-len(value), value))
    queries = [question]
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
    return deduped[:12]


def known_formula_terms() -> tuple[str, ...]:
    return ("ИВ0-1", "ИВ0", "ИВ1", "ИС", "ИВА", "ИВК", "ИВпр", "ИВон", "ИВ")


def extract_chunk_metadata(text: str) -> dict:
    """Classify a retrieved chunk so ranking can separate values from prices/rates."""
    cleaned = _cleanup_formula_noise(text or "")
    lower = cleaned.lower()
    sections = re.findall(r"\[SECTION:\s*([^\]]+)\]", cleaned)
    section_path = sections[-1].strip() if sections else ""
    section_parts = [part.strip() for part in section_path.split(">") if part.strip()]
    section_title = section_parts[-1] if section_parts else ""
    parent_section = " > ".join(section_parts[:-1])
    status_scope = f"{section_path}\n{cleaned[:900]}".lower()

    status = "deleted" if re.search(
        r"утратил[аио]?\s+силу|пункт\s+удален|исключен[оа]?|не\s+применяется",
        status_scope,
    ) else "active"

    chunk_type = "unknown"
    if "[table]" in lower:
        chunk_type = "table"
    if "[с_sub(" in lower or "[т_sub(" in lower:
        chunk_type = "cost"
    if "расчетные показатели стоимости" in lower or "стоимост" in lower or "ставк" in lower:
        chunk_type = "cost"
    if "исходные данные" in lower or "предварительных обязательств" in lower or "предварительных требований" in lower:
        chunk_type = "rate"
    if "утратил силу" in status_scope:
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
    selected: list[dict] = []
    selected_ids: set[int] = set()

    def add(item: dict) -> None:
        selected.append(item)
        selected_ids.add(id(item))

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
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\[\s*\]", "", text)
    text = re.sub(r"[ \t]+([,.;:])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


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
    parts = [part.strip() for part in (section_path or "").split(">") if part.strip()]
    if len(parts) >= 2:
        return " > ".join(parts[-2:])
    return section_path or "раздел не определен"


def generate_extractive_value_answer(question: str, context: list[dict]) -> str | None:
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

        section = _compact_section_reference(term_items[0].get("section_path", ""))
        if snippets:
            lines.append(f"- {term}: {_short_explanation(term, ' '.join(snippets), 520)} Источник: {section}.")
        else:
            lines.append(f"- {term}: расчетная фраза не выделена из evidence. Источник: {section}.")

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
    matches = list(re.finditer(r"\[SECTION:\s*([^\]]+)\]", cleaned))
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


def extract_formula_lines_from_texts(texts: list[str], question: str = "") -> list[str]:
    """Extract allowed calculation evidence: symbolic formulas and textual calculation rules."""
    calculations: list[str] = []
    seen: set[str] = set()
    requested = requested_formula_terms(question)
    intent = detect_query_intent(question)

    def add(candidate: str, *, requested_term_already_matched: bool = False) -> None:
        candidate = clean_formula_artifacts(_plain_evidence_text(candidate))
        candidate = candidate.strip(" ,;\t")
        if not candidate or len(candidate) < 20:
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
        symbolic_patterns = re.findall(
            r"(\[С_sub\([^\]]+\)\]\s*=\s*\[ΔО_sub\([^\]]+\)\]\s*\*\s*\[Т_sub\([^\]]+\)\](?:\s*[+*]\s*[^.\n]+)?\.?)",
            cleaned,
        )
        for candidate in symbolic_patterns:
            if "+ *" in candidate or candidate.endswith("+ *."):
                continue
            add(candidate)

        if intent == "value":
            text = _plain_evidence_text(cleaned)
            sections = re.findall(r"\[SECTION:\s*([^\]]+)\]", cleaned)
            section_haystack = sections[-1] if sections else ""
            section_terms = [term for term in requested if contains_term(section_haystack, term)]
            sentences = _split_evidence_sentences(text)
            for sentence in sentences:
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

    return calculations[:12]


def generate_formula_only_answer(question: str, context: list[dict], calculations: list[str], spec: PrismQuery) -> str:
    """Answer formula extraction requests without prose synthesis or unrelated rules."""
    if not calculations:
        target = f" пункта {', '.join(spec.target_sections)}" if spec.target_sections else ""
        return f"В отобранном evidence{target} формулы или расчетные правила не найдены."

    lines: list[str] = []
    if spec.target_sections:
        lines.append(f"Расчетные правила из пункта {', '.join(spec.target_sections)}:")
    else:
        lines.append("Расчетные правила из извлеченного контекста:")
    lines.extend(f"- {calculation}" for calculation in calculations)
    return clean_formula_artifacts("\n".join(lines))


def rerank_context_results(results: list[dict], question: str) -> list[dict]:
    intent = detect_query_intent(question)
    requested = requested_formula_terms(question)
    normalized_question = question.lower().replace("ё", "е")
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
            score += 0.01 * text.count(term)
            if term.lower() in section_title:
                score += 0.10
            if term.lower() in section_path:
                score += 0.06

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


def sanitize_grounded_answer(answer: str, question: str, allowed_calculations: list[str]) -> str:
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
    answer = re.sub(r"\n{3,}", "\n\n", answer).strip()
    return answer


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
        bm25_path.mkdir(exist_ok=True)
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

        # ── SQLite кэш ───────────────────────────────────────
        self.cache = sqlite3.connect(CACHE_DB, check_same_thread=False)
        self.cache.execute(
            """CREATE TABLE IF NOT EXISTS answer_cache (
                question_hash TEXT PRIMARY KEY,
                question      TEXT,
                answer        TEXT,
                sources       TEXT,
                created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.cache.commit()

        # ── Ноды для BM25 (в памяти) ─────────────────────────
        self._all_nodes: list = []
        self._load_nodes_from_docstore()

        print(f"✓ Агент готов. Документов в docstore: {len(self._all_nodes)} чанков")

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
                if isinstance(d, TextNode):
                    nodes.append(d)
                    continue
                node_id = getattr(d, "doc_id", None) or getattr(d, "id_", None) or getattr(d, "id", None)
                nodes.append(TextNode(text=d.text, metadata=d.metadata, id_=node_id))
            self._all_nodes = nodes

    def _build_query_engine(self, top_k: int = TOP_K):
        """Строим гибридный query engine с текущими нодами."""
        vector_retriever = _TracedRetriever(
            self.index.as_retriever(similarity_top_k=max(VECTOR_TOP_K, top_k)),
            "retrieval.vector",
        )

        retrievers = [vector_retriever]
        if self._all_nodes:
            bm25_retriever = _TracedRetriever(
                BM25Retriever.from_defaults(
                    nodes=self._all_nodes,
                    similarity_top_k=max(BM25_TOP_K, top_k),
                ),
                "retrieval.bm25",
            )
            retrievers.append(bm25_retriever)

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

        # Сохраняем docstore для BM25
        self.docstore.persist(persist_path=str(self.docstore_path))

        # Обновляем ноды в памяти для BM25
        self._all_nodes = list(nodes) + [
            n for n in self._all_nodes if n.id_ not in {nd.id_ for nd in nodes}
        ]

        print(f"✓ Проиндексировано {len(nodes)} чанков из {p.name}")
        return len(nodes)

    def ask(self, question: str, system_prompt: str | None = None, prompt_id: str = "rag-grounded") -> tuple[str, list[dict], bool]:
        """
        Возвращает (answer, sources, from_cache).
        sources — список dict с ключами file, section, score.
        """
        qhash = _question_hash(f"{prompt_id}\n{system_prompt or GROUNDED_SYSTEM_PROMPT}\n{question}")

        # Проверяем кэш. Хэш, вопрос и ответ намеренно не попадают в telemetry.
        with _pipeline_span("cache.lookup") as span:
            row = self.cache.execute(
                "SELECT answer, sources FROM answer_cache WHERE question_hash = ?",
                (qhash,),
            ).fetchone()
            cache_hit = row is not None
            span.set_attribute("rag.cache.hit", cache_hit)
            # Histogram's _count provides a low-cardinality hit/miss counter.
            PIPELINE_SECONDS.labels(
                stage="cache.hit" if cache_hit else "cache.miss",
                flow="python",
            ).observe(0.0)
        if row:
            answer, sources_json = row
            return answer, json.loads(sources_json), True

        if not self._all_nodes:
            return (
                "База знаний пуста. Загрузите документы командой :load <путь>",
                [],
                False,
            )

        spec = refine_prism_query(question)
        context = self.retrieve_context(question, TOP_K, spec)
        calculations = extract_formula_lines_from_texts([item["text"] for item in context], question)
        answer = self._generate_grounded_answer(question, context, calculations, spec, system_prompt)

        sources = [{
            "file": item["file"],
            "section": item.get("section_path", ""),
            "score": item["score"],
            "text_preview": item["text"][:120],
        } for item in context]

        # Кэшируем
        with _pipeline_span("cache.write"):
            self.cache.execute(
                "INSERT OR REPLACE INTO answer_cache (question_hash, question, answer, sources) VALUES (?,?,?,?)",
                (qhash, question, answer, json.dumps(sources, ensure_ascii=False)),
            )
            self.cache.commit()

        return answer, sources, False

    def _generate_grounded_answer(
        self,
        question: str,
        context: list[dict],
        calculations: list[str],
        spec: PrismQuery | None = None,
        system_prompt: str | None = None,
    ) -> str:
        spec = spec or refine_prism_query(question)
        if spec.answer_mode == "formulas_only":
            with _pipeline_span(
                "generation.formula",
                attributes={"rag.context.count": len(context)},
            ):
                return generate_formula_only_answer(question, context, calculations, spec)
        with _pipeline_span(
            "generation.extractive",
            attributes={"rag.context.count": len(context)},
        ) as extractive_span:
            extractive = generate_extractive_value_answer(question, context)
            extractive_span.set_attribute("rag.generation.matched", bool(extractive))
        if extractive:
            return extractive

        context_text = "\n\n---\n\n".join(
            f"[Источник: {item['file']}, score={item['score']}]\n{item['text']}"
            for item in context
        )
        normalized_question = question.lower()
        is_procedural = bool(re.search(r"\bкак\s+(создать|настроить|добавить|открыть|заполнить|изменить)\b", normalized_question))
        is_calculation_question = not is_procedural and any(
            marker in normalized_question
            for marker in ("как рассчитыва", "формула", "расчетное правило", "расчётное правило")
        )
        calculations_block = ""
        if is_calculation_question and calculations:
            calculations_block = "\nРАЗРЕШЕННЫЕ РАСЧЕТНЫЕ ПРАВИЛА:\n" + "\n".join(
                f"- {calculation}" for calculation in calculations
            )
        prompt = f"""{system_prompt or GROUNDED_SYSTEM_PROMPT}

Ниже приведены данные для ответа. Используй их как единственный фактический контекст: не выдумывай сведения, которых в нём нет. Всегда отвечай на русском языке.

КОНТЕКСТ:
{context_text}

{calculations_block}

ВОПРОС:
{question}

ОТВЕТ:
"""
        provider = str(getattr(self.llm, "provider", "ollama") or "ollama")
        with _pipeline_span(
            "llm.generate",
            attributes={
                "gen_ai.operation.name": "generate_content",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": self.llm_model,
                "rag.context.count": len(context),
            },
        ) as llm_span:
            response = self.llm.complete(prompt)
            llm_span.set_attribute("rag.response.chars", len(str(response)))
        return sanitize_grounded_answer(str(response).strip(), question, calculations)

    def debug_retrieval(self, question: str, top_k: int = 10):
        """Показать найденные чанки без вызова LLM."""
        results = self.retrieve_context(question, top_k)
        print(f"DEBUG retrieval top-{len(results)} для «{question}»:")
        for i, item in enumerate(results, 1):
            preview = item["text"][:700].replace("\n", " ")
            print(f"\n{i}. {item['file']}  score={item['score']:.4f}")
            print(f"   {preview}")

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

    def retrieve_context(self, question: str, top_k: int = 10, spec: PrismQuery | None = None) -> list[dict]:
        """Вернуть найденные чанки без вызова LLM."""
        spec = spec or refine_prism_query(question)
        candidate_k = max(top_k * 5, 40)
        with _pipeline_span(
            "retrieval.setup",
            attributes={"rag.retrieval.candidate_k": candidate_k},
        ):
            qe = self._build_query_engine(candidate_k)
        merged: dict[str, dict] = {}
        for item in self._procedural_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._exact_section_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        for item in self._section_seed_results(question):
            key = hashlib.sha256(f"{item['file']}\n{item['text'][:700]}".encode("utf-8")).hexdigest()
            merged[key] = item
        targeted_queries = build_targeted_queries(question)
        for query_index, query in enumerate(targeted_queries):
            with _pipeline_span(
                "retrieval.fusion",
                attributes={
                    "rag.query.index": query_index,
                    "rag.query.count": len(targeted_queries),
                },
            ) as fusion_span:
                nodes = qe.retriever.retrieve(QueryBundle(query_str=query))
                fusion_span.set_attribute("rag.results.count", len(nodes))
            for item in nodes:
                meta = item.node.metadata or {}
                text = item.node.text or ""
                file_name = meta.get("file_name", meta.get("file_path", "?"))
                key = hashlib.sha256(f"{file_name}\n{text[:700]}".encode("utf-8")).hexdigest()
                score = float(item.score or 0)
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
        results = list(merged.values())
        with _pipeline_span(
            "retrieval.rerank",
            attributes={"rag.results.input_count": len(results)},
        ) as rerank_span:
            ranked = rerank_context_results(results, question)
            rerank_span.set_attribute("rag.results.count", len(ranked))
        with _pipeline_span(
            "retrieval.diversify",
            attributes={"rag.results.input_count": len(ranked)},
        ) as diversify_span:
            diversified = diversify_context_results(ranked, question, top_k)
            diversify_span.set_attribute("rag.results.count", len(diversified))
        with _pipeline_span(
            "retrieval.select",
            attributes={"rag.results.input_count": len(diversified)},
        ) as select_span:
            selected = select_prism_evidence(diversified, spec, top_k)
            select_span.set_attribute("rag.results.count", len(selected))
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
        self.cache.execute("DELETE FROM answer_cache")
        self.cache.commit()
        print("✓ Кэш очищен")

    def bm25_search(self, query: str, top_k: int = 10):
        """Диагностика: прямой BM25 поиск."""
        if not self._all_nodes:
            print("  (нет нод для BM25)")
            return
        retriever = BM25Retriever.from_defaults(
            nodes=self._all_nodes,
            similarity_top_k=top_k,
        )
        with _pipeline_span(
            "retrieval.bm25",
            attributes={"rag.retrieval.top_k": top_k, "rag.diagnostic": True},
        ) as span:
            results = retriever.retrieve(QueryBundle(query_str=query))
            span.set_attribute("rag.results.count", len(results))
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
