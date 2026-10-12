from __future__ import annotations

import hashlib
import html
import io
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from runtime_paths import MATHML_CACHE_DIR


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}


@dataclass
class DocxBlock:
    kind: str
    text: str
    style: str = ""
    level: int = 0


def read_docx_text(path: Path | str) -> str:
    """Extract DOCX content from Word XML while preserving structure and formulas."""
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
        styles = _read_styles(archive)
        image_text = _read_embedded_images(archive)

    root = ET.fromstring(document_xml)
    body = root.find("w:body", NS)
    if body is None:
        return ""

    blocks: list[DocxBlock] = []
    legal_document = bool(re.search(r"постановлен|регламент|правила", path.name, re.I))
    def body_blocks(container: ET.Element):
        for child in list(container):
            if _local_name(child.tag) in {"p", "tbl"}:
                yield child
            elif _local_name(child.tag) in {"sdt", "sdtContent", "customXml"}:
                yield from body_blocks(child)

    for child in body_blocks(body):
        tag = _local_name(child.tag)
        if tag == "p":
            block = _parse_paragraph(child, styles, image_text)
            if legal_document and re.match(r"^\d+(?:\.\d+)*\.\s+", block.text):
                block.kind = "clause"
            elif legal_document and re.match(r"^(?:ПРАВИЛА|ИЗМЕНЕНИЯ|ПРИЛОЖЕНИЕ|ОСНОВНЫЕ ПОЛОЖЕНИЯ)\b", block.text) and block.text == block.text.upper():
                block.kind, block.level = "legal-part", 1
            if block.text:
                blocks.append(block)
        elif tag == "tbl":
            block = _parse_table(child, styles, image_text)
            if block.text:
                blocks.append(block)

    return _render_blocks(blocks)


def _read_styles(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        styles_xml = archive.read("word/styles.xml")
    except KeyError:
        return {}

    styles: dict[str, str] = {}
    root = ET.fromstring(styles_xml)
    for style in root.findall("w:style", NS):
        style_id = style.attrib.get(_qn("w:styleId"), "")
        name = style.find("w:name", NS)
        if style_id and name is not None:
            styles[style_id] = name.attrib.get(_qn("w:val"), "")
    return styles


def _parse_paragraph(paragraph: ET.Element, styles: dict[str, str], image_text: dict[str, str] | None = None) -> DocxBlock:
    style_id = _paragraph_style_id(paragraph)
    style_name = styles.get(style_id, "")
    text = _normalize_text(_extract_inline_text(paragraph, image_text))
    if not text:
        return DocxBlock("paragraph", "")

    level = _heading_level(style_id, style_name, text)
    if level:
        return DocxBlock("heading", text, style=style_name or style_id, level=level)
    return DocxBlock("paragraph", text, style=style_name or style_id)


def _parse_table(table: ET.Element, styles: dict[str, str], image_text: dict[str, str] | None = None) -> DocxBlock:
    rows: list[list[str]] = []
    for row in table.findall(".//w:tr", NS):
        cells: list[str] = []
        for cell in row.findall("w:tc", NS):
            parts = []
            for paragraph in cell.findall("w:p", NS):
                text = _normalize_text(_extract_inline_text(paragraph, image_text))
                if text:
                    parts.append(text)
            cells.append(" ".join(parts))
        if any(cells):
            rows.append(cells)

    if not rows:
        return DocxBlock("table", "")

    rendered = ["[TABLE]"]
    for row in rows:
        rendered.append(" | ".join(row))
    return DocxBlock("table", "\n".join(rendered))


def _extract_inline_text(element: ET.Element, image_text: dict[str, str] | None = None) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        local = _local_name(node.tag)
        if local in {"drawing", "pict"}:
            references = []
            for descendant in node.iter():
                for attribute, value in descendant.attrib.items():
                    if attribute in {
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed",
                        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                    } and value in (image_text or {}):
                        references.append(value)
            for reference in dict.fromkeys(references):
                parts.append(f"\n{image_text[reference]}\n")
            return
        if local in {"oMath", "oMathPara"}:
            math_text = _omml_plain_text(node)
            mathml = _omml_to_mathml(node)
            if math_text:
                parts.append(f" [MATH: {math_text}] ")
            if mathml:
                mathml_ref = _store_mathml(mathml)
                parts.append(f" [[MATHML_REF:{mathml_ref}]] ")
            return
        if local == "t":
            if node.text:
                parts.append(html.unescape(node.text))
            return
        if local == "instrText":
            field = _normalize_field(node.text or "")
            if field:
                parts.append(field)
            return
        if local == "tab":
            parts.append(" ")
            return
        if local in {"br", "cr"}:
            parts.append("\n")
            return
        for child in list(node):
            visit(child)

    visit(element)
    return "".join(parts)


def _read_embedded_images(archive: zipfile.ZipFile) -> dict[str, str]:
    """Keep drawing content and explicitly mark images that could not be read.

    OCR is text evidence, never a claim that a mathematical image has been
    converted into an exact editable equation.
    """
    try:
        root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    result, by_target = {}, {}
    ocr_enabled = os.getenv("RAG_DOCX_IMAGE_OCR", "true").lower() == "true" and bool(shutil.which("tesseract"))
    for relationship in root:
        if not relationship.attrib.get("Type", "").endswith("/image") or relationship.attrib.get("TargetMode") == "External":
            continue
        target = relationship.attrib.get("Target", "")
        archive_target = "word/" + target.lstrip("/") if not target.startswith("/") else target.lstrip("/")
        if archive_target not in by_target:
            value = f"[IMAGE_UNREAD: {target}]"
            from image_parsing import parse_image_bytes, visual_parser_enabled
            if visual_parser_enabled():
                try:
                    recognized = parse_image_bytes(archive.read(archive_target), target)
                    # Illustration headings must not replace Word's native
                    # chapter hierarchy or turn a formula into a new chapter.
                    image_body = re.sub(r'(?m)^#{1,6}\s+', '', recognized.text)
                    value = f"[IMAGE_OCR: {target}; engine={recognized.engine}]\n{image_body}\n[/IMAGE_OCR]"
                    if recognized.warnings:
                        value += '\n[IMAGE_WARNING: ' + '; '.join(recognized.warnings) + ']'
                except Exception as exc:
                    value += f" [IMAGE_WARNING: visual extraction failed ({type(exc).__name__})]"
            elif ocr_enabled:
                try:
                    from PIL import Image
                    import pytesseract
                    with Image.open(io.BytesIO(archive.read(archive_target))) as image:
                        # Tiny icons and individual formula glyphs are not
                        # whole equations; retain the unread marker for them.
                        if image.width >= 80 and image.height >= 20:
                            recognized = pytesseract.image_to_string(image, lang=os.getenv("RAG_OCR_LANGUAGES", "rus+eng"), config="--psm 6", timeout=20).strip()
                            if len(recognized) >= 3:
                                value = f"[IMAGE_OCR: {target}]\n{recognized}\n[/IMAGE_OCR]"
                except Exception:
                    pass
            by_target[archive_target] = value
        result[relationship.attrib.get("Id", "")] = by_target[archive_target]
    return result


def _omml_to_mathml(node: ET.Element) -> str:
    body = "".join(_omml_child_to_mathml(child) for child in list(node))
    body = body or _escape_math_text(_omml_plain_text(node))
    return f'<math xmlns="http://www.w3.org/1998/Math/MathML"><mrow>{body}</mrow></math>'


def _store_mathml(mathml: str) -> str:
    digest = hashlib.sha256(mathml.encode("utf-8")).hexdigest()[:24]
    MATHML_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = MATHML_CACHE_DIR / f"{digest}.mathml"
    if not path.exists():
        path.write_text(mathml, encoding="utf-8")
    return digest


def _omml_child_to_mathml(node: ET.Element) -> str:
    local = _local_name(node.tag)
    if local in {"oMath", "oMathPara", "e"}:
        return "".join(_omml_child_to_mathml(child) for child in list(node))
    if local == "r":
        return "".join(_omml_child_to_mathml(child) for child in list(node))
    if local == "t":
        return _mathml_token(node.text or "")
    if local == "f":
        num = _first_child_mathml(node, "num")
        den = _first_child_mathml(node, "den")
        return f"<mfrac>{num}{den}</mfrac>"
    if local == "sSub":
        base = _first_child_mathml(node, "e")
        sub = _first_child_mathml(node, "sub")
        return f"<msub>{base}{sub}</msub>"
    if local == "sSup":
        base = _first_child_mathml(node, "e")
        sup = _first_child_mathml(node, "sup")
        return f"<msup>{base}{sup}</msup>"
    if local == "sSubSup":
        base = _first_child_mathml(node, "e")
        sub = _first_child_mathml(node, "sub")
        sup = _first_child_mathml(node, "sup")
        return f"<msubsup>{base}{sub}{sup}</msubsup>"
    if local == "rad":
        deg = _first_child_mathml(node, "deg")
        expr = _first_child_mathml(node, "e")
        return f"<mroot>{expr}{deg}</mroot>" if deg else f"<msqrt>{expr}</msqrt>"
    if local == "d":
        expr = _first_child_mathml(node, "e")
        beg = _math_prop_val(node, "begChr", "(")
        end = _math_prop_val(node, "endChr", ")")
        return f"<mrow><mo>{html.escape(beg)}</mo>{expr}<mo>{html.escape(end)}</mo></mrow>"
    if local == "nary":
        op = _math_prop_val(node, "chr", "∑")
        base = f"<mo>{html.escape(op)}</mo>"
        sub = _first_child_mathml(node, "sub")
        sup = _first_child_mathml(node, "sup")
        expr = _first_child_mathml(node, "e")
        if sub and sup:
            base = f"<munderover>{base}{sub}{sup}</munderover>"
        elif sub:
            base = f"<munder>{base}{sub}</munder>"
        elif sup:
            base = f"<mover>{base}{sup}</mover>"
        return f"<mrow>{base}{expr}</mrow>"
    if local == "bar":
        expr = _first_child_mathml(node, "e")
        return f'<mover>{expr}<mo accent="true">¯</mo></mover>'
    return "".join(_omml_child_to_mathml(child) for child in list(node))


def _first_child_mathml(node: ET.Element, local_name: str) -> str:
    child = _first_child(node, local_name)
    if child is None:
        return ""
    body = "".join(_omml_child_to_mathml(part) for part in list(child))
    return f"<mrow>{body}</mrow>" if body else ""


def _first_child(node: ET.Element, local_name: str) -> ET.Element | None:
    for child in list(node):
        if _local_name(child.tag) == local_name:
            return child
    return None


def _math_prop_val(node: ET.Element, prop_name: str, default: str) -> str:
    prop = node.find(f".//m:{prop_name}", NS)
    if prop is None:
        return default
    return prop.attrib.get(_qn("m:val"), default)


def _mathml_token(value: str) -> str:
    value = html.unescape(value)
    if not value:
        return ""
    escaped = html.escape(value)
    if re.fullmatch(r"[+\-−=*/=<>()\[\]{}|,.;:]", value):
        return f"<mo>{escaped}</mo>"
    if re.fullmatch(r"\d+(?:[,.]\d+)?", value):
        return f"<mn>{escaped}</mn>"
    return f"<mi>{escaped}</mi>"


def _escape_math_text(value: str) -> str:
    return f"<mi>{html.escape(value)}</mi>" if value else ""


def _omml_plain_text(node: ET.Element) -> str:
    local = _local_name(node.tag)
    if local in {"oMath", "oMathPara", "e", "r"}:
        return "".join(_omml_plain_text(child) for child in list(node))
    if local == "t":
        return html.unescape(node.text or "")
    if local == "f":
        return f"({_omml_plain_part(node, 'num')})/({_omml_plain_part(node, 'den')})"
    if local == "sSub":
        return f"{_omml_plain_part(node, 'e')}_sub({_omml_plain_part(node, 'sub')})"
    if local == "sSup":
        return f"{_omml_plain_part(node, 'e')}^({_omml_plain_part(node, 'sup')})"
    if local == "sSubSup":
        return f"{_omml_plain_part(node, 'e')}_sub({_omml_plain_part(node, 'sub')})^({_omml_plain_part(node, 'sup')})"
    if local == "rad":
        deg = _omml_plain_part(node, "deg")
        expr = _omml_plain_part(node, "e")
        return f"root({deg};{expr})" if deg else f"sqrt({expr})"
    if local == "d":
        return f"{_math_prop_val(node, 'begChr', '(')}{_omml_plain_part(node, 'e')}{_math_prop_val(node, 'endChr', ')')}"
    if local == "nary":
        op = _math_prop_val(node, "chr", "∑")
        sub = _omml_plain_part(node, "sub")
        sup = _omml_plain_part(node, "sup")
        expr = _omml_plain_part(node, "e")
        limits = ""
        if sub:
            limits += f"_sub({sub})"
        if sup:
            limits += f"^({sup})"
        return f"{op}{limits}{expr}"
    return "".join(_omml_plain_text(child) for child in list(node))


def _omml_plain_part(node: ET.Element, local_name: str) -> str:
    child = _first_child(node, local_name)
    return _omml_plain_text(child) if child is not None else ""


def _normalize_field(value: str) -> str:
    value = html.unescape(value).strip()
    if not value:
        return ""

    upper = value.upper()
    if upper.startswith(("PAGEREF ", "REF ", "SEQ ", "TOC ")):
        return ""
    if not upper.startswith("EQ "):
        return ""

    value = value[3:].strip()
    value = re.sub(r"\\\*\s*\w+", "", value)
    value = value.replace("\\s", "_sub")
    value = value.replace("\\f", "_frac")
    value = value.replace("\\i", "_sum")
    value = value.replace("\\su", "_sum")
    value = value.replace("\\", "")
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        return ""
    return f" [{value}] "


def _render_blocks(blocks: list[DocxBlock]) -> str:
    rendered: list[str] = []
    section_stack: list[str] = []
    legal_part = ""

    for block in blocks:
        if block.kind == "legal-part":
            legal_part = block.text
            section_stack = [legal_part]
            rendered.append(f"[SECTION: {legal_part}]\n# {legal_part}")
            continue
        if block.kind == "clause":
            number = re.match(r"^(\d+(?:\.\d+)*)\.", block.text).group(1)
            section_stack = ([legal_part] if legal_part else []) + [f"Пункт {number}"]
            rendered.append(f"[SECTION: {' > '.join(section_stack)}]\n{block.text}")
            continue
        if block.kind == "heading":
            level = max(1, min(block.level, 6))
            section_stack = section_stack[: level - 1]
            section_stack.append(block.text)
            rendered.append(f"[SECTION: {' > '.join(section_stack)}]\n{'#' * level} {block.text}")
            continue

        if section_stack and block.kind == "paragraph":
            rendered.append(f"[SECTION: {' > '.join(section_stack)}]\n{block.text}")
        else:
            rendered.append(block.text)

    return "\n\n".join(rendered)


def _paragraph_style_id(paragraph: ET.Element) -> str:
    style = paragraph.find("w:pPr/w:pStyle", NS)
    if style is None:
        return ""
    return style.attrib.get(_qn("w:val"), "")


def _heading_level(style_id: str, style_name: str, text: str) -> int:
    style = f"{style_id} {style_name}".lower()
    match = re.search(r"(heading|заголовок)\s*([1-6])?", style)
    number = re.match(r"^(\d+(?:\.\d+)*)(?:[.)]\s*|\s+)(?=\S)", text)
    structured_number = number and ('.' in number.group(0) or ')' in number.group(0))
    if match and structured_number:
        # Authors frequently reuse the wrong Word Heading style. A visible
        # 5.3 section belongs to chapter 5, not to the preceding regional case.
        return min(number.group(1).count('.') + 1, 6)
    if match:
        return int(match.group(2) or 1)

    if structured_number and len(text) <= 160:
        return min(number.group(1).count(".") + 1, 6)

    return 0


def _normalize_text(value: str) -> str:
    value = value.replace("\xa0", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = value.strip()
    return value


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _qn(name: str) -> str:
    prefix, local = name.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"
