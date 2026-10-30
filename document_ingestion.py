"""Extract and normalize uploaded document content before indexing.

The extraction cache binds source bytes and parser settings. Table coordinates
are retained only when they still map to the normalized text; OCR remains
unverified transcription rather than a trusted reconstructed formula."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from docx_parser import read_docx_text as read_docx_xml_text
from docx_parser import DocxReadResult, read_docx_structured
from runtime_paths import INGESTION_CACHE_DIR


CACHE_DIR = INGESTION_CACHE_DIR
INGESTION_CACHE_VERSION = "v10-image-occurrences"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


@dataclass
class IngestedDocument:
    """Extraction result with parser provenance, warnings and optional native table/image-occurrence bindings."""
    text: str
    backend: str
    warnings: list[str]
    tables: list[dict] = field(default_factory=list)
    image_occurrences: list[dict] = field(default_factory=list)


def ingest_document(path: str | Path) -> IngestedDocument:
    """Read a validated cache or try the format's readers; reject files without usable extracted content."""
    path = Path(path)
    cached = _read_cache(path)
    if cached is not None:
        return cached

    warnings: list[str] = []
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        from image_parsing import parse_image_file, visual_parser_enabled
        if visual_parser_enabled():
            parsed = parse_image_file(path)
            result = IngestedDocument(parsed.text, parsed.engine, parsed.warnings)
            if not has_extracted_content(result.text, 3):
                raise RuntimeError('Image contains no readable content')
            if not result.warnings:
                _write_cache(path, result)
            return result
        backends = [("tesseract-ocr", _read_image_with_tesseract)]
        minimum_text_length = 3
    elif suffix == ".pdf":
        from image_parsing import read_pdf
        parsed = read_pdf(path)
        result = IngestedDocument(parsed.text, parsed.engine, parsed.warnings)
        if not has_extracted_content(result.text):
            raise RuntimeError('PDF contains no readable content')
        if not result.warnings:
            _write_cache(path, result)
        return result
    elif suffix == ".docx":
        backends = [
            ("xml", _read_with_xml),
            ("docling", _read_with_docling),
            ("unstructured", _read_with_unstructured),
            ("mammoth", _read_with_mammoth),
        ]
        minimum_text_length = 1
    else:
        backends = [
            ("docling", _read_with_docling),
            ("unstructured", _read_with_unstructured),
            ("xml", _read_with_xml),
        ]
        minimum_text_length = 1

    for name, reader in backends:
        try:
            parsed = reader(path)
            raw_text = parsed.text if isinstance(parsed, DocxReadResult) else parsed
            text = normalize_ingested_text(raw_text)
            tables: list[dict] = []
            if isinstance(parsed, DocxReadResult):
                tables = _realign_tables(raw_text, text, parsed.tables, warnings)
            if has_extracted_content(text, minimum_text_length):
                unread_images = len(re.findall(r"\[IMAGE_UNREAD:", text))
                if unread_images:
                    warnings.append(f"{unread_images} embedded images were not recognized; image equations are not text formulas")
                warnings.extend(dict.fromkeys(re.findall(r'\[IMAGE_WARNING: ([^\]]+)\]', text)))
                if suffix in IMAGE_SUFFIXES:
                    from image_parsing import TRANSCRIPTION_WARNING
                    warnings.append(TRANSCRIPTION_WARNING)
                result = IngestedDocument(text=text, backend=name, warnings=warnings, tables=tables,
                    image_occurrences=parsed.image_occurrences if isinstance(parsed, DocxReadResult) else [])
                _write_cache(path, result)
                return result
            warnings.append(f"{name}: no readable content extracted")
        except Exception as exc:
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")

    raise RuntimeError("No ingestion backend could parse document: " + "; ".join(warnings))


def _read_image_with_tesseract(path: Path) -> str:
    """Extract Russian and English text from a raster image using local OCR."""
    from image_parsing import parse_image_file
    return parse_image_file(path).text


def normalize_ingested_text(text: str) -> str:
    """Normalize extracted text before indexing; table bindings are realigned and validated separately."""
    return _normalize_layout(text).strip()


def _normalize_layout(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text


def _validated_tables(text: str, tables: object) -> list[dict] | None:
    """Reject corrupt provenance rather than silently associating wrong cells.

    Metadata is cache/input data, not a trusted source of character positions.
    Unknown or unsupported Word constructs are retained through explicit
    warnings; malformed record shapes or nonmatching text are not applied.
    """
    if not isinstance(tables, list):
        return None
    previous_end = 0
    identifiers: set[int] = set()
    for table in tables:
        if not isinstance(table, dict):
            return None
        table_id, start, end = (table.get(key) for key in ("table_id", "start_char", "end_char"))
        if any(type(value) is not int for value in (table_id, start, end)):
            return None
        if table_id < 0 or table_id in identifiers or start < previous_end or end <= start or end > len(text):
            return None
        if not isinstance(table.get("text"), str) or text[start:end] != table["text"]:
            return None
        if not isinstance(table.get("warnings"), list) or any(not isinstance(warning, str) for warning in table["warnings"]):
            return None
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            return None
        rendered_rows: list[str] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict) or type(row.get("row_index")) is not int or row["row_index"] != row_index:
                return None
            if type(row.get("is_header")) is not bool or not isinstance(row.get("cells"), list):
                return None
            before, after = row.get("grid_before", 0), row.get("grid_after", 0)
            if any(type(value) is not int or value < 0 for value in (before, after)):
                return None
            column = before
            for cell in row["cells"]:
                if not isinstance(cell, dict):
                    return None
                cell_column, span = cell.get("column_index"), cell.get("column_span")
                if type(cell_column) is not int or type(span) is not int or cell_column != column or span < 1:
                    return None
                vertical_merge = cell.get("vertical_merge")
                if not isinstance(cell.get("text"), str) or (vertical_merge is not None and
                        (not isinstance(vertical_merge, str) or vertical_merge not in {"restart", "continue"})):
                    return None
                column += span
            rendered_rows.append(" | ".join(cell["text"] for cell in row["cells"]))
        rendered = "[TABLE]\n" + "\n".join(rendered_rows)
        if normalize_ingested_text(rendered) != normalize_ingested_text(table["text"]):
            return None
        identifiers.add(table_id)
        previous_end = end
    return tables


def _realign_tables(raw_text: str, normalized_text: str, tables: object, warnings: list[str]) -> list[dict]:
    if _validated_tables(raw_text, tables) is None:
        warnings.append("table-grid: invalid source mapping discarded")
        return []
    layout = _normalize_layout(raw_text)
    leading_trim = len(layout) - len(layout.lstrip())
    result: list[dict] = []
    for table in tables:
        # The table starts with non-whitespace [TABLE], so normalization of
        # its prefix gives an unambiguous position even for identical tables.
        start = len(_normalize_layout(raw_text[:table["start_char"]])) - leading_trim
        table_text = normalize_ingested_text(table["text"])
        rows = [{**row, "cells": [{**cell, "text": normalize_ingested_text(cell["text"])}
                                  for cell in row["cells"]]} for row in table["rows"]]
        result.append({**table, "text": table_text, "rows": rows,
                       "start_char": start, "end_char": start + len(table_text)})
    if _validated_tables(normalized_text, result) is None:
        warnings.append("table-grid: normalization could not preserve exact mapping; bindings discarded")
        return []
    return result


def has_extracted_content(text: str, minimum_chars: int = 1) -> bool:
    """Accept short text while excluding unread-image and location markers."""
    content = re.sub(r"\[(?:SECTION|IMAGE_UNREAD|IMAGE_WARNING):[^\]]*\]", "", text)
    return sum(character.isalnum() for character in content) >= minimum_chars


def _read_with_docling(path: Path) -> str:
    from docling.document_converter import DocumentConverter

    converter = DocumentConverter()
    result = converter.convert(str(path))
    return result.document.export_to_markdown()


def _read_with_mammoth(path: Path) -> str:
    if path.suffix.lower() != ".docx":
        raise ValueError("mammoth supports DOCX only")

    import mammoth

    with path.open("rb") as file:
        result = mammoth.convert_to_markdown(file)
    return result.value


def _read_with_unstructured(path: Path) -> str:
    from unstructured.partition.auto import partition

    elements = partition(filename=str(path))
    return "\n\n".join(str(element) for element in elements if str(element).strip())


def _read_with_xml(path: Path) -> str | DocxReadResult:
    if path.suffix.lower() == ".docx":
        return read_docx_structured(path)

    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    raise ValueError(f"unsupported fallback format: {path.suffix}")


def _cache_key(path: Path) -> str:
    from image_parsing import parser_signature
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    payload = f"{INGESTION_CACHE_VERSION}|{parser_signature()}|{path.resolve()}|{digest}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(path: Path) -> Path:
    return CACHE_DIR / f"{_cache_key(path)}.json"


def _read_cache(path: Path) -> IngestedDocument | None:
    cache_path = _cache_path(path)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("text"), str):
        return None
    if not isinstance(data.get("warnings"), list) or any(not isinstance(warning, str) for warning in data["warnings"]):
        return None
    tables = _validated_tables(data["text"], data.get("tables"))
    if tables is None:
        return None
    images = data.get('image_occurrences')
    if not isinstance(images, list) or any(not isinstance(item, dict) for item in images):
        return None
    if images:
        with path.open('rb') as source:
            source_sha = hashlib.file_digest(source, 'sha256').hexdigest()
        for item in images:
            identity = {key:value for key,value in item.items() if key != 'occurrence_id'}
            if (item.get('schema') != 'docx-image-occurrence-v1' or item.get('source_sha256') != source_sha
                    or item.get('visual_fidelity') != 'not_verified'
                    or item.get('occurrence_id') != hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
                    or (item.get('ole') is not None and (not isinstance(item['ole'], dict)
                        or item['ole'].get('native_formula_status') != 'not_decoded'))):
                return None
    if any(any(marker in warning.lower() for marker in ('unavailable', 'not recognized', 'failed', 'fallback', 'incomplete')) for warning in data.get('warnings', [])):
        return None
    if not has_extracted_content(data["text"], 3 if path.suffix.lower() in IMAGE_SUFFIXES else 1):
        return None
    return IngestedDocument(
        text=data["text"],
        backend=data.get("backend", "cache"),
        warnings=data.get("warnings", []),
        tables=tables,
        image_occurrences=images,
    )


def _write_cache(path: Path, result: IngestedDocument) -> None:
    cache_path = _cache_path(path)
    temporary = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=CACHE_DIR,
                prefix=cache_path.stem+'.', suffix='.tmp', delete=False) as output:
            temporary = Path(output.name)
            json.dump({
            "text": result.text,
            "backend": result.backend,
            "warnings": result.warnings,
            "tables": result.tables,
            "image_occurrences": result.image_occurrences,
            }, output, ensure_ascii=False)
        temporary.replace(cache_path)
    except OSError:
        try:
            matching_winner = _read_cache(path) is not None
        except OSError:
            matching_winner = False
        if not matching_winner:
            result.warnings.append('Document extraction cache unavailable; result was not persisted')
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
