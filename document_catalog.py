"""Workspace document inventory and conservative version awareness."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


_SEMVER_RE = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё])(?:v|ver(?:sion)?|версия|ред(?:акция)?|rev|r)\s*[-_.]?\s*(\d+(?:[._-]\d+){0,3})",
    re.IGNORECASE,
)
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_.](0?[1-9]|1[0-2])[-_.](0?[1-9]|[12]\d|3[01])(?!\d)")
_RU_DATE_RE = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[._-](0?[1-9]|1[0-2])[._-](20\d{2})(?!\d)")
_YEAR_RE = re.compile(r"(?i)(?:ред(?:акция)?|версия|от)\s*(20\d{2})(?!\d)")
_BARE_SEMVER_RE = re.compile(r"^\s*(\d+(?:[._-]\d+){0,3})\s*$")
_CATALOG_INTENT_PATTERNS = (
    re.compile(r"\bкакие\b.{0,60}\b(регламент|документ|инструкц|файл)", re.IGNORECASE),
    re.compile(r"\b(список|перечень)\b.{0,60}\b(регламент|документ|инструкц|файл)", re.IGNORECASE),
    re.compile(r"\bчто\b.{0,30}\b(загружено|в памяти|есть из документов|есть из регламентов)", re.IGNORECASE),
    re.compile(r"\bс чем\s+(?:мы\s+)?работаем\b", re.IGNORECASE),
    re.compile(r"\bкогда\b.{0,50}\bзагруж", re.IGNORECASE),
    re.compile(r"\b(актуальн|новее|новая|последн|свеж)(?:\w*)\b.{0,60}\b(верси|редакц|документ|регламент)", re.IGNORECASE),
    re.compile(r"\b(верси|редакц)(?:\w*)\b.{0,60}\b(актуальн|новее|новая|последн|свеж)", re.IGNORECASE),
    re.compile(r"\b(какая|какие|что за)\b.{0,40}\b(верси|редакц)(?:\w*)\b", re.IGNORECASE),
)


def is_document_catalog_question(question: str) -> bool:
    """Return true only for workspace/document-inventory questions."""
    normalized = " ".join(str(question).split())
    return any(pattern.search(normalized) for pattern in _CATALOG_INTENT_PATTERNS)


def _metadata_version(document: dict[str, Any]) -> str:
    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    for key in ("document_version", "version", "revision", "edition", "effective_date", "document_date"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    return ""


def detect_document_version(document: dict[str, Any]) -> dict[str, Any]:
    """Detect an explicit comparable version without guessing from upload time."""
    file_name = str(document.get("file_name") or "")
    stem = Path(file_name).stem
    metadata_value = _metadata_version(document)
    candidates = [(metadata_value, "metadata")] if metadata_value else []
    candidates.append((stem, "file_name"))
    for value, source in candidates:
        semantic = _SEMVER_RE.search(value)
        if semantic is None and source == "metadata":
            semantic = _BARE_SEMVER_RE.fullmatch(value)
        if semantic:
            parts = tuple(int(part) for part in re.split(r"[._-]", semantic.group(1)))
            return {
                "version_label": semantic.group(1).replace("_", ".").replace("-", "."),
                "version_kind": "semantic",
                "version_key": parts,
                "version_source": source,
                "version_span": semantic.span() if source == "file_name" else None,
            }
        date_match = _DATE_RE.search(value)
        if date_match:
            year, month, day = (int(part) for part in date_match.groups())
            return {
                "version_label": f"{year:04d}-{month:02d}-{day:02d}",
                "version_kind": "date",
                "version_key": (year, month, day),
                "version_source": source,
                "version_span": date_match.span() if source == "file_name" else None,
            }
        ru_date = _RU_DATE_RE.search(value)
        if ru_date:
            day, month, year = (int(part) for part in ru_date.groups())
            return {
                "version_label": f"{year:04d}-{month:02d}-{day:02d}",
                "version_kind": "date",
                "version_key": (year, month, day),
                "version_source": source,
                "version_span": ru_date.span() if source == "file_name" else None,
            }
        year_match = _YEAR_RE.search(value)
        if year_match:
            year = int(year_match.group(1))
            return {
                "version_label": str(year),
                "version_kind": "date",
                "version_key": (year, 1, 1),
                "version_source": source,
                "version_span": year_match.span() if source == "file_name" else None,
            }
    return {
        "version_label": None,
        "version_kind": None,
        "version_key": None,
        "version_source": None,
        "version_span": None,
    }


def _document_family(file_name: str, version: dict[str, Any]) -> str:
    stem = Path(file_name).stem.casefold().replace("ё", "е")
    span = version.get("version_span")
    if span:
        stem = stem[: span[0]] + " " + stem[span[1] :]
    stem = _DATE_RE.sub(" ", stem)
    stem = _RU_DATE_RE.sub(" ", stem)
    stem = re.sub(r"[^0-9a-zа-я]+", " ", stem, flags=re.IGNORECASE)
    return " ".join(stem.split()) or Path(file_name).stem.casefold()


def annotate_document_versions(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add JSON-safe version/family status to document registry records."""
    annotated: list[dict[str, Any]] = []
    private_versions: list[dict[str, Any]] = []
    for document in documents:
        item = dict(document)
        version = detect_document_version(item)
        family = _document_family(str(item.get("file_name") or ""), version)
        item.update({
            "document_family": family,
            "version_label": version["version_label"],
            "version_source": version["version_source"],
            "version_status": "unknown" if version["version_key"] is None else "latest",
            "newer_document_id": None,
            "newer_file_name": None,
        })
        annotated.append(item)
        private_versions.append(version)

    for index, item in enumerate(annotated):
        version = private_versions[index]
        if version["version_key"] is None:
            continue
        newer_candidates = [
            (other_version["version_key"], other)
            for other, other_version in zip(annotated, private_versions)
            if other is not item
            and other["document_family"] == item["document_family"]
            and other_version["version_kind"] == version["version_kind"]
            and other_version["version_key"] is not None
            and other_version["version_key"] > version["version_key"]
        ]
        if newer_candidates:
            _, newest = max(newer_candidates, key=lambda candidate: candidate[0])
            item["version_status"] = "superseded"
            item["newer_document_id"] = str(newest.get("id") or "") or None
            item["newer_file_name"] = str(newest.get("file_name") or "") or None
    return annotated


def format_uploaded_at(value: Any) -> str:
    if isinstance(value, datetime):
        moment = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return "время загрузки не зафиксировано"
        try:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%d.%m.%Y %H:%M UTC")


def document_version_summary(document: dict[str, Any]) -> str:
    label = document.get("version_label")
    status = document.get("version_status")
    if not label:
        return "версия не указана; актуальность автоматически не подтверждена"
    if status == "superseded":
        return f"версия {label}; есть более новая загруженная версия: {document.get('newer_file_name')}"
    return f"версия {label}; более новая загруженная версия в этой рабочей области не обнаружена"


def _markdown_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _json_safe(value: Any) -> Any:
    """Normalize registry values before they cross HTTP, SSE, or queue boundaries."""
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def build_document_catalog_answer(project_name: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
    annotated = annotate_document_versions(documents)
    scope_name = project_name.strip() or "свободный чат"
    if not annotated:
        return {
            "answer": f"В рабочей области «{scope_name}» нет загруженных документов.",
            "sources": [],
            "documents": [],
        }
    lines = [f"В рабочей области «{scope_name}» загружено документов: **{len(annotated)}**."]
    sources: list[dict[str, Any]] = []
    for index, document in enumerate(annotated, start=1):
        name = str(document.get("file_name") or "Без названия")
        document_id = str(document.get("id") or "")
        url = f"/api/documents/{document_id}/content" if document_id else ""
        uploaded = format_uploaded_at(document.get("created_at"))
        status = str(document.get("status") or "unknown")
        version_summary = document_version_summary(document)
        safe_name = _markdown_label(name)
        link = f"[{safe_name}]({url})" if url else safe_name
        lines.append(f"{index}. {link} — загружен {uploaded}; статус `{status}`; {version_summary}.")
        locator = f"карточка документа; загружен {uploaded}; {version_summary}"
        source = {"file": name, "locator": locator}
        if url:
            source["url"] = url
        sources.append(source)
    lines.append(
        "Дата загрузки не является датой редакции документа. Сравнение актуальности выполнено только между "
        "явно обозначенными версиями, уже загруженными в эту рабочую область; внешний реестр или СЭД не проверялись."
    )
    return {"answer": "\n".join(lines), "sources": sources, "documents": _json_safe(annotated)}
