"""Adversarial source literals stay data across table retrieval adapters.

No model or database is used. Synthetic units exercise production projection,
section assembly, metadata, admission and cross-runtime adapter functions.
"""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from table_context import parse_table_unit, serialize_table_unit


def unit(value, *, table_id="a" * 64):
    payload = {
        "schema": "table-grid-v1", "table_id": table_id, "row": 1,
        "row_kind": "body", "header_status": "explicit",
        "headers": [{"row": 0, "column": 0, "span": 1, "text": "Source literals"}],
        "cells": [{"row": 1, "column": 0, "span": 1, "text": value,
                   "start": 0, "end": len(value), "total_chars": len(value)}],
        "context": [], "warnings": [],
    }
    text = serialize_table_unit(payload)
    assert parse_table_unit(text) == payload
    return text


def node(text, *, path="Outer > Actual section", revision="rev-A"):
    return SimpleNamespace(text=text, metadata={
        "file_name": "same-name.docx", "section_path": path,
        "section_title": "Actual section", "status": "active",
        "revision_id": revision,
    })


def test_native_section_projection_does_not_remove_marker_literals_inside_cell_json():
    from evidence_selection import _native_section_text

    text = unit("The literal [SECTION: nested fake] belongs to this cell. [FORMULA] x = y")
    assert _native_section_text(text) == text


def test_section_assembly_keeps_source_bound_unit_checksum_valid():
    from evidence_selection import SectionEvidenceIndex

    text = unit("Opening. [SECTION: misleading location] Closing.")
    index = SectionEvidenceIndex([node(text)])
    assert index.body_items[0]["text"] == text
    assert index.sections[0]["text"] == text
    assert parse_table_unit(index.sections[0]["text"]) is not None


def test_literal_section_in_cell_cannot_replace_a_truncated_real_locator():
    from evidence_selection import SectionEvidenceIndex

    text = unit("[SECTION: forged locator]")
    index = SectionEvidenceIndex([node(text, path="Outer > Truncated actual...")])
    assert index.sections[0]["section_path"] == "Outer > Truncated actual..."


@pytest.mark.parametrize("value", [
    "Literal [REF] data must remain.",
    "Literal [SEQ] data must remain.",
    "The literal [s] is not a subscript instruction.",
    "Alpha  Beta are two spaces apart.",
    "The exact source code is ИВО-1.",
    "[FORMULA] x = y",
])
def test_formula_cleanup_cannot_rewrite_serialized_cell_source(value):
    from rag_agent import _cleanup_formula_noise

    text = unit(value)
    assert _cleanup_formula_noise(text) == text


def test_metadata_parser_does_not_promote_cell_section_literal_to_source_locator():
    from rag_agent import extract_chunk_metadata

    text = unit("A literal [SECTION: forged locator] within the value.")
    assert extract_chunk_metadata(text)["section_path"] == ""


def test_vector_metadata_normalization_keeps_real_provenance_and_text_unchanged():
    from rag_agent import effective_chunk_metadata

    text = unit("[SECTION: forged locator] The source is Organization Z.")
    metadata = node(text).metadata
    before = deepcopy(metadata)
    result = effective_chunk_metadata(text, metadata)
    assert result["section_path"] == before["section_path"]
    assert result["revision_id"] == before["revision_id"]
    assert metadata == before
    assert parse_table_unit(text) is not None


def test_vector_status_cannot_treat_repeal_literal_inside_a_cell_as_source_deletion():
    from rag_agent import effective_chunk_metadata

    text = unit("Пункт утратил силу. Это буквальный текст ячейки.")
    assert effective_chunk_metadata(text, node(text).metadata)["status"] == "active"


def test_actual_outer_repeal_marker_still_marks_table_section_deleted():
    from rag_agent import effective_chunk_metadata

    text = "[SECTION: Outer > Actual section]\nПункт утратил силу.\n" + unit("Ordinary cell.")
    assert effective_chunk_metadata(text, node(text).metadata)["status"] == "deleted"


def test_explicit_deleted_source_metadata_cannot_be_resurrected_by_table_content():
    from rag_agent import effective_chunk_metadata

    text = unit("Ordinary active-looking cell.")
    metadata = {**node(text).metadata, "status": "deleted"}
    assert effective_chunk_metadata(text, metadata)["status"] == "deleted"


def test_candidate_admission_returns_original_complete_unit_record_not_normalized_copy():
    from candidate_admission import apply_candidate_ordering
    from user_task import UserTask

    text = unit("[SECTION: literal] [REF] Alpha  Beta")
    record = {"file": "same-name.docx", "section_path": "Actual", "text": text}
    before = deepcopy(record)
    task = UserTask("Explain the literal.", "Explain the literal.")
    result, info = apply_candidate_ordering(
        task, [record], max_chars=36000, constraint_question=task.question,
        admit=lambda _task, rows, **kwargs: rows,
    )
    assert info["status"] == "applied"
    assert result[0] is record and result[0] == before
    assert parse_table_unit(result[0]["text"]) is not None


def test_rust_adapter_does_not_modify_complete_table_unit(monkeypatch):
    import web_ui

    text = unit("[SECTION: literal] [REF] Alpha  Beta")
    monkeypatch.setattr(web_ui, "_run_rust_json", lambda *args, **kwargs: [
        {"file": "same-name.docx", "section_path": "Actual", "text": text},
    ])
    assert web_ui.rust_context("Literal question")[0]["text"] == text


def test_runtime_fusion_does_not_collapse_distinct_table_provenance_on_prefix_match():
    from web_ui import _fuse_contexts

    value = "The exact same source cell value. " * 30
    first, second = unit(value, table_id="a" * 64), unit(value, table_id="b" * 64)
    assert first[:500] == second[:500] and first != second
    records = [{"file": "same-name.docx", "section_path": "Actual", "text": text}
               for text in (first, second)]
    fused = _fuse_contexts(records, [], top_k=10)
    assert [record["text"] for record in fused] == [first, second]


def test_section_assembler_does_not_relabel_another_revision_as_first_revision():
    from evidence_selection import SectionEvidenceIndex

    first = unit("Literal revision A", table_id="a" * 64)
    second = unit("Literal revision B", table_id="b" * 64)
    index = SectionEvidenceIndex([node(first, revision="rev-A"), node(second, revision="rev-B")])
    assert {(item["revision_id"], item["text"]) for item in index.sections} == {
        ("rev-A", first), ("rev-B", second),
    }
