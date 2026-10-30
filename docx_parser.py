"""Read DOCX XML in document order, preserving native math and explicit table geometry.

Word header flags and source coordinates establish table roles; visual guesses
do not. Embedded-image recognition is separately labeled unverified, and an
undecoded OLE object is never presented as a native formula."""
from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET
from urllib.parse import unquote, urlsplit

from runtime_paths import MATHML_CACHE_DIR


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}


@dataclass
class DocxBlock:
    """One ordered paragraph, section or table block with its original style and structural metadata."""
    kind: str
    text: str
    style: str = ""
    level: int = 0
    table: dict | None = None


@dataclass
class DocxReadResult:
    """Combined text, table-coordinate bindings and image occurrences from one original DOCX."""
    text: str
    tables: list[dict] = field(default_factory=list)
    image_occurrences: list[dict] = field(default_factory=list)


def read_docx_text(path: Path | str) -> str:
    """Extract DOCX content from Word XML while preserving structure and formulas."""
    return read_docx_structured(path).text


def read_docx_structured(path: Path | str) -> DocxReadResult:
    """Extract text and explicit Word table coordinates in one XML/OCR pass.

    Header roles come only from ``w:tblHeader``. A visually plausible first
    row is not enough evidence to assign a header or a semantic column role.
    """
    path = Path(path)
    with path.open('rb') as source:
        source_sha256 = hashlib.file_digest(source, 'sha256').hexdigest()
    with zipfile.ZipFile(path) as archive:
        document_xml = archive.read("word/document.xml")
        root = ET.fromstring(document_xml)
        styles = _read_styles(archive)
        image_occurrences = _read_image_occurrences(archive, root, source_sha256)
        image_text = _read_embedded_images(archive, image_occurrences=image_occurrences)

    body = root.find("w:body", NS)
    if body is None:
        return DocxReadResult("")

    blocks: list[DocxBlock] = []
    legal_document = bool(re.search(r"постановлен|регламент|правила", path.name, re.I))
    def body_blocks(container: ET.Element):
        for child in list(container):
            if _local_name(child.tag) in {"p", "tbl"}:
                yield child
            elif _local_name(child.tag) in {"sdt", "sdtContent", "customXml"}:
                yield from body_blocks(child)

    table_id = 0
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
            block = _parse_table(child, styles, image_text, table_id=table_id)
            table_id += 1
            if block.text:
                blocks.append(block)

    tables: list[dict] = []
    text = _render_blocks(blocks, tables=tables)
    return DocxReadResult(text, tables, image_occurrences)


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


def _table_children(container: ET.Element, child_name: str, warnings: list[str]):
    """Walk Word content wrappers, never cross a nested table/cell boundary."""
    for child in container:
        local = _local_name(child.tag)
        if local == child_name:
            yield child
        elif local in {"sdt", "sdtContent", "customXml"}:
            yield from _table_children(child, child_name, warnings)
        elif local not in {"tbl", "tc", "tr", "p"} and child.find(f".//w:{child_name}", NS) is not None:
            warnings.append(f"unsupported-table-wrapper:{local}")
            yield from _table_children(child, child_name, warnings)


def _table_integer(properties: ET.Element | None, name: str, default: int,
                   minimum: int, warnings: list[str], location: str) -> int:
    element = properties.find(f"w:{name}", NS) if properties is not None else None
    if element is None:
        return default
    value = element.attrib.get(_qn("w:val"), "")
    if not re.fullmatch(r"\d+", value) or int(value) < minimum:
        label = {"gridSpan": "grid-span", "gridBefore": "grid-before", "gridAfter": "grid-after"}[name]
        warnings.append(f"invalid-{label}:{location}")
        return default
    return int(value)


def _table_cell_text(cell: ET.Element, styles: dict[str, str], image_text: dict[str, str] | None,
                     warnings: list[str], location: str, depth: int) -> str:
    parts: list[str] = []

    def visit(container: ET.Element) -> None:
        for child in container:
            local = _local_name(child.tag)
            if local == "p":
                value = _normalize_text(_extract_inline_text(child, image_text))
                if value:
                    parts.append(value)
            elif local == "tbl":
                # Keep nested content within its containing cell; pooling its
                # rows with the outer grid silently associates unrelated cells.
                warnings.append(f"nested-table:{location}")
                if depth >= 16:
                    warnings.append(f"nested-table-depth-limit:{location}")
                    value = " ".join(_normalize_text(_extract_inline_text(p, image_text))
                                     for p in child.findall(".//w:p", NS))
                    parts.append("[NESTED_TABLE_UNSTRUCTURED] " + value)
                else:
                    value = _parse_table(child, styles, image_text, depth=depth + 1).text
                    if value:
                        parts.append(value.replace("[TABLE]", "[NESTED_TABLE]", 1))
            elif local in {"sdt", "sdtContent", "customXml"}:
                visit(child)
            elif child.find(".//w:p", NS) is not None or child.find(".//w:tbl", NS) is not None:
                warnings.append(f"unsupported-cell-wrapper:{local}:{location}")
                visit(child)

    visit(cell)
    return " ".join(parts)


def _parse_table(table: ET.Element, styles: dict[str, str], image_text: dict[str, str] | None = None,
                 *, table_id: int = 0, depth: int = 0) -> DocxBlock:
    warnings: list[str] = []
    rows: list[dict] = []
    grid = table.find("w:tblGrid", NS)
    grid_width = len(grid.findall("w:gridCol", NS)) if grid is not None else 0
    if not grid_width:
        warnings.append("missing-table-grid")
    active_merges: set[tuple[int, int]] = set()
    seen_data = False
    for row_index, row in enumerate(_table_children(table, "tr", warnings)):
        properties = row.find("w:trPr", NS)
        before = _table_integer(properties, "gridBefore", 0, 0, warnings, f"row-{row_index}")
        after = _table_integer(properties, "gridAfter", 0, 0, warnings, f"row-{row_index}")
        marker = properties.find("w:tblHeader", NS) if properties is not None else None
        header_value = marker.attrib.get(_qn("w:val"), "true").lower() if marker is not None else "false"
        is_header = marker is not None and header_value in {"1", "true", "on"}
        if marker is not None and header_value not in {"1", "true", "on", "0", "false", "off"}:
            warnings.append(f"invalid-header-marker:row-{row_index}")
        if is_header and seen_data:
            warnings.append(f"non-leading-header-row:row-{row_index}")
        seen_data = seen_data or not is_header
        cells: list[dict] = []
        column = before
        next_merges: set[tuple[int, int]] = set()
        for cell_index, cell in enumerate(_table_children(row, "tc", warnings)):
            location = f"row-{row_index}-cell-{cell_index}"
            cell_properties = cell.find("w:tcPr", NS)
            span = _table_integer(cell_properties, "gridSpan", 1, 1, warnings, location)
            merge = cell_properties.find("w:vMerge", NS) if cell_properties is not None else None
            vertical = merge.attrib.get(_qn("w:val"), "continue") if merge is not None else None
            if vertical not in {None, "restart", "continue"}:
                warnings.append(f"invalid-vertical-merge:{location}")
                vertical = None
            interval = (column, column + span)
            if vertical == "continue" and interval not in active_merges:
                warnings.append(f"orphan-vertical-merge:{location}")
            if vertical in {"restart", "continue"}:
                next_merges.add(interval)
            if cell_properties is not None and cell_properties.find("w:hMerge", NS) is not None:
                warnings.append(f"unsupported-horizontal-merge:{location}")
            cells.append({"column_index": column, "column_span": span,
                          "text": _table_cell_text(cell, styles, image_text, warnings, location, depth),
                          "vertical_merge": vertical})
            column += span
        if grid_width and column + after != grid_width:
            warnings.append(f"table-grid-width-mismatch:row-{row_index}")
        if not cells:
            warnings.append(f"empty-table-row:row-{row_index}")
        rows.append({"row_index": row_index, "is_header": is_header, "cells": cells,
                     "grid_before": before, "grid_after": after})
        active_merges = next_merges
    if not any(cell["text"] for row in rows for cell in row["cells"]):
        return DocxBlock("table", "")
    if not any(row["is_header"] for row in rows):
        warnings.append("no-explicit-header")
    rendered = ["[TABLE]"] + [" | ".join(cell["text"] for cell in row["cells"]) for row in rows]
    text = "\n".join(rendered)
    record = {"table_id": table_id, "text": text, "rows": rows,
              "warnings": list(dict.fromkeys(warnings))}
    return DocxBlock("table", text, table=record)


def _extract_inline_text(element: ET.Element, image_text: dict[str, str] | None = None) -> str:
    parts: list[str] = []

    def visit(node: ET.Element) -> None:
        local = _local_name(node.tag)
        if local in {"drawing", "pict", "object"}:
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


def _image_member(target: str) -> str | None:
    """Resolve an internal OPC part, never a filesystem or external URL."""
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '\\' in target:
        return None
    decoded = unquote(parsed.path)
    member = posixpath.normpath(decoded.lstrip('/') if decoded.startswith('/') else posixpath.join('word', decoded))
    return member if member and member != '..' and not member.startswith(('../', '/')) else None


def _image_part(archive: zipfile.ZipFile, member: str | None) -> bytes:
    if member is None:
        raise ValueError('Invalid image package part')
    info = archive.getinfo(member)
    if info.is_dir() or info.file_size > 32_000_000:
        raise ValueError('Image exceeds the 32 MB decoding budget')
    return archive.read(member)


def _read_image_occurrences(archive: zipfile.ZipFile, root: ET.Element, source_sha256: str) -> list[dict]:
    """Keep XML/relationship witnesses for future scoped previews, not model markers.

    These are source occurrence descriptors, not an editable formula, an asset
    URL, proof of visual fidelity, or a claim that a crop/transform was applied.
    """
    try:
        relations = {node.get('Id'): node for node in ET.fromstring(archive.read('word/_rels/document.xml.rels'))}
    except KeyError:
        return []
    rel_ns = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    v_ns, o_ns = 'urn:schemas-microsoft-com:vml', 'urn:schemas-microsoft-com:office:office'
    a_ns = 'http://schemas.openxmlformats.org/drawingml/2006/main'
    parents = {child: node for node in root.iter() for child in node}
    paths, pending = {}, [(root, '/document[1]')]
    while pending:
        node, xml_path = pending.pop()
        paths[node] = xml_path
        counts = {}
        for child in node:
            name = _local_name(child.tag)
            counts[name] = counts.get(name, 0) + 1
            pending.append((child, f'{xml_path}/{name}[{counts[name]}]'))
    hashes = {}

    def part_hash(member):
        if member not in hashes:
            try:
                hashes[member] = hashlib.sha256(_image_part(archive, member)).hexdigest()
            except (KeyError, ValueError, OSError, RuntimeError, zipfile.BadZipFile):
                hashes[member] = None
        return hashes[member]

    records = []
    for node in root.iter():
        if node.tag not in {f'{{{v_ns}}}imagedata', f'{{{a_ns}}}blip'}:
            continue
        rid = next((node.get(f'{{{rel_ns}}}{key}') for key in ('embed', 'id', 'link') if node.get(f'{{{rel_ns}}}{key}')), None)
        relation = relations.get(rid)
        if relation is None or not relation.get('Type', '').endswith('/image'):
            continue
        external = relation.get('TargetMode') == 'External'
        member = None if external else _image_member(relation.get('Target', ''))
        media_sha = None if external else part_hash(member)
        ancestors, parent = [], parents.get(node)
        while parent is not None:
            ancestors.append(parent)
            parent = parents.get(parent)
        shape = next((item for item in ancestors if item.tag == f'{{{v_ns}}}shape'), None)
        obj = next((item for item in ancestors if _local_name(item.tag) == 'object'), None)
        ole = None
        if shape is not None and obj is not None and shape.get('id'):
            matched = [item for item in obj.iter(f'{{{o_ns}}}OLEObject') if item.get('ShapeID') == shape.get('id')]
            if len(matched) == 1:
                native = matched[0]
                native_rid = native.get(f'{{{rel_ns}}}id')
                native_relation = relations.get(native_rid)
                native_member = (_image_member(native_relation.get('Target', '')) if native_relation is not None
                    and native_relation.get('TargetMode') != 'External' and native_relation.get('Type', '').endswith('/oleObject') else None)
                ole = {'shape_id': shape.get('id'), 'object_id': native.get('ObjectID'),
                    'progid': native.get('ProgID', ''), 'relationship_id': native_rid,
                    'source_zip_member': native_member, 'media_sha256': part_hash(native_member) if native_member else None,
                    'native_formula_status': 'not_decoded'}
        alternate = any(_local_name(item.tag) == 'AlternateContent' for item in ancestors)
        known_formula = (ole is not None and ole['media_sha256'] is not None and not alternate
                         and re.fullmatch(r'Equation\.(?:3|DSMT\d+)', ole['progid']) is not None)
        record = {'schema': 'docx-image-occurrence-v1', 'document_part': 'word/document.xml',
            'relationship_part': 'word/_rels/document.xml.rels', 'source_sha256': source_sha256,
            'xml_path': paths[node], 'xml_tag': node.tag, 'relationship_id': rid,
            'source_zip_member': member, 'media_sha256': media_sha,
            'status': 'external_relationship_blocked' if external else 'embedded' if media_sha else 'invalid_or_missing_media',
            'purpose_hint': 'formula' if known_formula else 'auto', 'ole': ole,
            'alternate_content_unresolved': alternate, 'visual_fidelity': 'not_verified',
            'presentation': {'crop_attributes': {key: value for key, value in node.attrib.items() if 'crop' in _local_name(key).lower()},
                             'shape_style': shape.get('style', '') if shape is not None else ''}}
        record['occurrence_id'] = hashlib.sha256(json.dumps(record, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
        records.append(record)
    return records


def _read_embedded_images(archive: zipfile.ZipFile, *, image_occurrences: list[dict] | None = None) -> dict[str, str]:
    """Keep drawing content and explicitly mark images that could not be read.

    OCR is text evidence, never a claim that a mathematical image has been
    converted into an exact editable equation.
    """
    try:
        root = ET.fromstring(archive.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    result, by_target, by_content = {}, {}, {}
    from image_parsing import parse_image_bytes, visual_parser_enabled, TRANSCRIPTION_WARNING, image_transcription_block
    ocr_enabled = os.getenv("RAG_DOCX_IMAGE_OCR", "true").lower() == "true"
    engine_available = visual_parser_enabled() or bool(shutil.which('tesseract'))
    formula_rids = {item['relationship_id'] for item in (image_occurrences or []) if item.get('purpose_hint') == 'formula'}
    for relationship in root:
        if not relationship.attrib.get("Type", "").endswith("/image") or relationship.attrib.get("TargetMode") == "External":
            continue
        target = relationship.attrib.get("Target", "")
        archive_target = _image_member(target)
        purpose = 'formula' if relationship.get('Id') in formula_rids else 'auto'
        target_key = (archive_target or target, purpose)
        if target_key not in by_target:
            value = f"[IMAGE_UNREAD: {target}]"
            if ocr_enabled and engine_available:
                try:
                    data = _image_part(archive, archive_target)
                    key = (hashlib.sha256(data).hexdigest(), purpose)
                    if key not in by_content:
                        try:
                            by_content[key] = parse_image_bytes(data, target, purpose)
                        except Exception as exc:
                            by_content[key] = type(exc).__name__
                    recognized = by_content[key]
                    if isinstance(recognized, str):
                        value += f' [IMAGE_WARNING: image extraction failed ({recognized})]'
                    else:
                        # Illustration headings must not replace Word's native
                        # chapter hierarchy or turn a formula into a new chapter.
                        image_body = re.sub(r'(?m)^#{1,6}\s+', '', recognized.text)
                        value = image_transcription_block(image_body,target,recognized.engine)
                        warnings = list(dict.fromkeys([*recognized.warnings, TRANSCRIPTION_WARNING]))
                        value += '\n[IMAGE_WARNING: ' + '; '.join(warnings) + ']'
                except Exception as exc:
                    value += f" [IMAGE_WARNING: image extraction failed ({type(exc).__name__})]"
            else:
                reason = 'image extraction disabled' if not ocr_enabled else 'image recognizer unavailable'
                value += f' [IMAGE_WARNING: {reason}]'
            by_target[target_key] = value
        result[relationship.attrib.get("Id", "")] = by_target[target_key]
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


def _render_blocks(blocks: list[DocxBlock], tables: list[dict] | None = None) -> str:
    rendered: list[str] = []
    section_stack: list[str] = []
    legal_part = ""
    rendered_length = 0

    def append(value: str, table: dict | None = None) -> None:
        nonlocal rendered_length
        start = rendered_length + (2 if rendered else 0)
        rendered.append(value)
        rendered_length = start + len(value)
        if tables is not None and table is not None:
            tables.append({**table, "start_char": start, "end_char": rendered_length})

    for block in blocks:
        if block.kind == "legal-part":
            legal_part = block.text
            section_stack = [legal_part]
            append(f"[SECTION: {legal_part}]\n# {legal_part}")
            continue
        if block.kind == "clause":
            number = re.match(r"^(\d+(?:\.\d+)*)\.", block.text).group(1)
            section_stack = ([legal_part] if legal_part else []) + [f"Пункт {number}"]
            append(f"[SECTION: {' > '.join(section_stack)}]\n{block.text}")
            continue
        if block.kind == "heading":
            level = max(1, min(block.level, 6))
            section_stack = section_stack[: level - 1]
            section_stack.append(block.text)
            append(f"[SECTION: {' > '.join(section_stack)}]\n{'#' * level} {block.text}")
            continue

        if section_stack and block.kind == "paragraph":
            append(f"[SECTION: {' > '.join(section_stack)}]\n{block.text}")
        else:
            append(block.text, block.table)

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
