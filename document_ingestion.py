from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from docx_parser import read_docx_text as read_docx_xml_text
from runtime_paths import INGESTION_CACHE_DIR


CACHE_DIR = INGESTION_CACHE_DIR
INGESTION_CACHE_VERSION = "v8-visual-extraction"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


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
            text = reader(path)
            text = normalize_ingested_text(text)
            if has_extracted_content(text, minimum_text_length):
                unread_images = len(re.findall(r"\[IMAGE_UNREAD:", text))
                if unread_images:
                    warnings.append(f"{unread_images} embedded images were not recognized; image equations are not text formulas")
                warnings.extend(dict.fromkeys(re.findall(r'\[IMAGE_WARNING: ([^\]]+)\]', text)))
                result = IngestedDocument(text=text, backend=name, warnings=warnings)
                _write_cache(path, result)
                return result
            warnings.append(f"{name}: no readable content extracted")
        except Exception as exc:
            warnings.append(f"{name}: {type(exc).__name__}: {exc}")

    raise RuntimeError("No ingestion backend could parse document: " + "; ".join(warnings))


def _read_image_with_tesseract(path: Path) -> str:
    """Extract Russian and English text from a raster image using local OCR."""
    import pytesseract
    from PIL import Image, ImageOps

    languages = os.getenv("RAG_OCR_LANGUAGES", "rus+eng").strip() or "rus+eng"
    page_segmentation = os.getenv("RAG_OCR_PSM", "3").strip() or "3"
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source)
        grayscale = ImageOps.grayscale(image)
        prepared = ImageOps.autocontrast(grayscale)
        text = pytesseract.image_to_string(
            prepared,
            lang=languages,
            config=f"--oem 1 --psm {page_segmentation}",
        )
        if len(text.strip()) < 3 and page_segmentation != "11":
            text = pytesseract.image_to_string(
                prepared,
                lang=languages,
                config="--oem 1 --psm 11",
            )
    return text


def normalize_ingested_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


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


def _read_with_xml(path: Path) -> str:
    if path.suffix.lower() == ".docx":
        return read_docx_xml_text(path)

    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    raise ValueError(f"unsupported fallback format: {path.suffix}")


def _cache_key(path: Path) -> str:
    stat = path.stat()
    from image_parsing import parser_signature
    payload = f"{INGESTION_CACHE_VERSION}|{parser_signature()}|{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _cache_path(path: Path) -> Path:
    return CACHE_DIR / f"{_cache_key(path)}.json"


def _read_cache(path: Path) -> IngestedDocument | None:
    cache_path = _cache_path(path)
    if not cache_path.exists():
        return None
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    if any(any(marker in warning.lower() for marker in ('unavailable', 'not recognized', 'failed', 'fallback', 'incomplete')) for warning in data.get('warnings', [])):
        return None
    if not has_extracted_content(data["text"], 3 if path.suffix.lower() in IMAGE_SUFFIXES else 1):
        return None
    return IngestedDocument(
        text=data["text"],
        backend=data.get("backend", "cache"),
        warnings=data.get("warnings", []),
    )


def _write_cache(path: Path, result: IngestedDocument) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = _cache_path(path)
    cache_path.write_text(
        json.dumps({
            "text": result.text,
            "backend": result.backend,
            "warnings": result.warnings,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
