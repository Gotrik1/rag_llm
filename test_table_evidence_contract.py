"""Table units keep identical header/cell bindings across evidence consumers.

These are structural trust-boundary tests, not a claim that a model correctly
interprets the cells or that a header establishes an actor/action relationship.
"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from table_context import parse_table_unit, serialize_table_unit
from evidence_anchors import (
    _large_paragraph_units,
    build_evidence_anchors,
    evidence_anchor_payload,
)
from evidence_selection import SectionEvidenceIndex, _content_blocks, _focused_excerpt, _legend_rows


def cell(text, column=0, *, row=1):
    return {"row": row, "column": column, "span": 1, "text": text,
            "start": 0, "end": len(text), "total_chars": len(text)}


def table_unit(*, headings=("Quartz origin", "Amber destination"),
               values=("Blue observatory", "Green station"), table_id="a" * 64):
    payload = {
        "schema": "table-grid-v1", "table_id": table_id, "row": 1,
        "row_kind": "body", "header_status": "explicit",
        "headers": [{"row": 0, "column": column, "span": 1, "text": text}
                    for column, text in enumerate(headings)],
        "cells": [cell(text, column) for column, text in enumerate(values)],
        "context": [], "warnings": [],
    }
    unit = serialize_table_unit(payload)
    assert parse_table_unit(unit) == payload
    return unit


def evidence(text):
    return {"evidence_id": "E1", "file": "synthetic-grid.docx",
            "section_path": "Grid definitions", "content_hash": "revision-A", "text": text}


def anchors(text):
    return [row["text"] for row in build_evidence_anchors([evidence(text)]).values()]


def excerpt(text, budget):
    return _focused_excerpt(text, budget, (frozenset({"quartz"}),), {}, question="Quartz")


def test_column_order_and_arbitrary_headers_are_preserved_without_semantic_rebinding():
    unit = table_unit(headings=("Amber destination", "Quartz origin"),
                      values=("Green station", "Blue observatory"))
    assert _content_blocks(unit) == [unit]
    assert _legend_rows(unit) == [unit]
    assert parse_table_unit(anchors(unit)[0])["headers"][1]["text"] == "Quartz origin"
    assert parse_table_unit(anchors(unit)[0])["cells"][1]["text"] == "Blue observatory"


def test_single_line_unit_isolated_from_prose_without_rewriting_unit_bytes():
    unit = table_unit()
    text = "Opening condition.\n" + unit + "\nClosing condition."
    blocks = _content_blocks(text)
    assert unit in blocks
    assert blocks == ["Opening condition.\n", unit, "Closing condition."]
    assert "".join(anchors(text)) == text
    assert any(unit == row.strip() for row in anchors(text))


def test_multiline_cells_pipes_and_nested_marker_strings_remain_one_unit():
    value = "First line | not a new column\n\n[TABLE_ROW_V1] not another record. [MATH: x | y]"
    unit = table_unit(values=(value, "Second cell. Further sentence; ending."))
    assert "\n" not in unit
    assert _content_blocks(unit) == [unit]
    assert _legend_rows(unit) == [unit]
    assert anchors(unit) == [unit]
    assert parse_table_unit(unit)["cells"][0]["text"] == value


@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_text_separators_inside_json_strings_are_not_record_boundaries(separator):
    unit = table_unit(values=("Quartz" + separator + "origin", "Amber ending"))
    assert _content_blocks(unit) == [unit]
    assert _legend_rows(unit) == [unit]
    assert anchors(unit) == [unit]


def test_exact_budget_retains_complete_unit():
    unit = table_unit()
    assert excerpt(unit, len(unit)) == (unit, [])


@pytest.mark.parametrize("budget", [0, 1, 120, 250])
def test_insufficient_budget_rejects_whole_unit_without_exposing_values(budget):
    unit = table_unit()
    assert len(unit) > budget
    assert excerpt(unit, budget) == ("", [])


def test_one_character_short_cannot_turn_header_and_cell_into_separate_evidence():
    unit = table_unit()
    assert excerpt(unit, len(unit) - 1) == ("", [])


def test_mixed_excerpt_keeps_any_retained_table_packet_whole_and_stays_in_budget():
    unit = table_unit()
    text = ("Unrelated opening. " * 250) + "\n\n" + unit + "\n\n" + ("Unrelated ending. " * 250)
    budget = len(unit) + 100
    selected, _ = excerpt(text, budget)
    assert len(selected) <= budget
    assert unit in selected
    assert _content_blocks(selected).count(unit) == 1
    assert selected.count("[TABLE_ROW_V1]") == 1


def test_foreign_oversized_valid_unit_is_never_split_by_passage_targets():
    unit = table_unit(values=("Quartz conditions. " * 500, "Amber ending."))
    assert len(unit) > 2400
    assert _large_paragraph_units(unit) == [unit]
    assert anchors(unit) == [unit]
    assert excerpt(unit, 2400) == ("", [])


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\n\n", "\r\n\r\n"])
def test_passage_segmentation_preserves_all_original_whitespace(separator):
    first = table_unit()
    second = table_unit(table_id="b" * 64, headings=("Cobalt heading",), values=("Violet value",))
    text = separator + "Prelude." + separator + first + separator + separator + second + separator
    result = anchors(text)
    assert "".join(result) == text
    assert sum(first in row for row in result) == 1
    assert sum(second in row for row in result) == 1
    assert not any(first in row and second in row for row in result)


@pytest.mark.parametrize("consumer", [
    _content_blocks, _legend_rows, _large_paragraph_units, anchors,
    lambda text: excerpt(text, len(text) + 100),
])
def test_tampered_own_record_fails_closed_instead_of_falling_back_to_bare_cells(consumer):
    unit = table_unit().replace("Blue observatory", "False operator")
    assert parse_table_unit(unit) is None
    with pytest.raises(ValueError, match="invalid-table-unit"):
        consumer(unit)


def test_embedded_nonstandalone_marker_is_plain_source_not_a_structural_record():
    text = "The literal [TABLE_ROW_V1] appears in this prose example."
    assert _content_blocks(text) == [text]
    assert _legend_rows(text) == [text]
    assert anchors(text) == [text]


@pytest.mark.parametrize("selector", ["select", "select_v2"])
def test_short_invalid_unit_cannot_bypass_validation_when_selected_section_fits(selector):
    unit = table_unit().replace("Blue observatory", "False operator")
    # Rejection during native section assembly is even earlier than packing;
    # the invariant is that no short corrupt record reaches either writer.
    with pytest.raises(ValueError, match="invalid-table-unit"):
        index = SectionEvidenceIndex([SimpleNamespace(
            text=unit,
            metadata={"file_name": "synthetic-grid.docx", "section_path": "Quartz",
                      "section_title": "Quartz", "status": "active"},
        )])
        getattr(index, selector)("Quartz", [], max_chars=36000)


def test_reader_writer_and_judge_receive_the_same_complete_table_bytes():
    from corrective_rag import SearchPlan, generation_prompt
    from grounding_verifier import build_grounding_judge_prompt
    from source_reader import build_source_reading_prompt

    unit = table_unit()
    records = [evidence(unit)]
    before = deepcopy(records)
    question = "What is the Quartz origin?"
    plan = SearchPlan(requirements=(question,), queries=(question,), canonical_question=question)
    reader = build_source_reading_prompt(question, {
        "canonical_question": question, "requirements": [question], "exclusions": [],
    }, records)
    reader_data = json.loads(reader.split("ДАННЫЕ ЧТЕНИЯ:\n", 1)[1])
    writer = generation_prompt(question, plan, records, {"complete": True})
    writer_data = json.JSONDecoder().raw_decode(writer[writer.index('{"question"'):])[0]
    judge = build_grounding_judge_prompt("The origin is Blue observatory [E1].", records, question)
    judge_data = json.JSONDecoder().raw_decode(judge[judge.index('{"question"'):])[0]
    assert writer_data["evidence"][0]["text"] == unit
    assert reader_data["evidence"][0]["passages"][0]["text"] == unit
    assert judge_data["evidence"][0]["passages"][0]["text"] == unit
    assert evidence_anchor_payload(records)[0]["passages"][0]["text"] == unit
    assert records == before
