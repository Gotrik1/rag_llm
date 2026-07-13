from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from docx_parser import read_docx_text as read_docx_xml_text
from runtime_paths import INGESTION_CACHE_DIR


CACHE_DIR = INGESTION_CACHE_DIR
INGESTION_CACHE_VERSION = "v3-omml-mathml-ref"


@dataclass
class IngestedDocument:
    text: str
    backend: str
    warnings: list[str]


def ingest_document(path: str | Path) -> IngestedDocument:
    path = Path(path)
    cached = _read_cache(path)
    if cached is not None:
        return cached

    warnings: list[str] = []
    if path.suffix.lower() == ".docx":
        backends = [
            ("xml", _read_with_xml),
            ("docling", _read_with_docling),
            ("unstructured", _read_with_unstructured),
            ("mammoth", _read_with_mammoth),
        ]
    else:
        backends = [
            ("docling", _read_with_docling),
            ("unstructured", _read_with_unstructured),
            ("xml", _read_with_xml),
        ]

    for name, reader in backends:
        try:
            text = reader(path)
            text = normalize_ingested_text(text)
            if len(text.strip()) > 100:
                result = IngestedDocument(text=text, backend=name, warnings=warnings)
                _write_cache(path, result)
                return result
            warnings.append(f"{name}: extracted too little text")
        except Exception as exc:
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")

    raise RuntimeError("No ingestion backend could parse document: " + "; ".join(warnings))


def normalize_ingested_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


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


def _read_with_xml(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return read_docx_xml_text(path)

    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    raise ValueError(f"unsupported fallback format: {path.suffix}")


def _cache_key(path: Path) -> str:
    stat = path.stat()
    payload = f"{INGESTION_CACHE_VERSION}|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(path: Path) -> Path:
    return CACHE_DIR / f"{_cache_key(path)}.json"


def _read_cache(path: Path) -> IngestedDocument | None:
    cache_path = _cache_path(path)
    if not cache_path.exists():
        return None
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    return IngestedDocument(
        text=data["text"],
        backend=data.get("backend", "cache"),
        warnings=data.get("warnings", []),
    )


def _write_cache(path: Path, result: IngestedDocument) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    cache_path = _cache_path(path)
    cache_path.write_text(
        json.dumps({
            "text": result.text,
            "backend": result.backend,
            "warnings": result.warnings,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
