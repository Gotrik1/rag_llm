"""Exact source binding, complete text coverage and math-safe stable anchors."""
from copy import deepcopy
import json

import pytest

from evidence_anchors import build_evidence_anchors, evidence_anchor_payload, resolve_passages


def item(text, identifier="E1", **kwargs):
    return {"evidence_id": identifier, "text": text, "file": "Методика.md", "section_path": "Общий порядок", **kwargs}


def passages(text):
    return [row["text"] for row in build_evidence_anchors([item(text)]).values()]


def test_reference_provenance_is_retained_without_claiming_general_applicability():
    origin = {'file':'Методика.md','section_path':'Основной порядок','quote':'Смотри пункт 4.'}
    payload = evidence_anchor_payload([item('Целевой порядок.',applicability='referenced',
        reference_from=origin,reference_target='4')])[0]
    assert payload['reference_from'] == origin
    assert payload['reference_target'] == '4'
    assert payload['applicability'] == 'referenced'


def test_source_passages_are_verbatim_and_cover_entire_text_including_separators():
    source = "  Первый абзац с условием.\r\n\r\nВторой абзац.\n\n\nПоследний абзац без завершающего переноса.  "
    values = passages(source)
    assert "".join(values) == source
    assert all(value in source for value in values)
    assert "Последний абзац" in values[-1]


def test_large_text_is_fully_addressable_not_cropped_to_context_or_quote_limit():
    source = "\n\n".join(f"Параграф {number}: " + "Подтверждённое правило. " * 40 for number in range(80))
    values = passages(source)
    assert len(values) > 32
    assert "".join(values) == source
    assert "Параграф 79" in values[-1]
    assert all(len(value) <= 2400 for value in values)


def test_plain_paragraph_with_conditions_is_not_split_just_to_hit_target():
    source = "Условие применяется, если " + "показатель соответствует договору, " * 50 + "кроме специально оговоренного случая."
    assert 1200 < len(source) < 2400
    assert passages(source) == [source]


def test_huge_paragraph_splits_at_sentence_boundaries_without_losing_characters():
    source = "Условие применимости проверяется перед расчётом. " * 160
    values = passages(source)
    assert len(values) > 2
    assert "".join(values) == source
    assert all(value.rstrip().endswith(".") for value in values)


@pytest.mark.parametrize("formula", [
    "$$" + "A_{i}=B_{i}+C_{i}\\\\\n\n" * 160 + "$$",
    "\\[" + "A=B+C\\\\\n\n" * 250 + "\\]",
    "[MATH: " + "A_{[i]}=B_{[i]}+C_{[i]}\n\n" * 160 + "]",
    "[MATH]" + "A=B+C\n\n" * 400 + "[/MATH]",
])
def test_display_and_word_equations_are_atomic_even_when_oversized(formula):
    source = "Вводное правило.\n\n" + formula + "\n\nУсловия применения."
    values = passages(source)
    assert "".join(values) == source
    assert sum(formula in value for value in values) == 1


def test_equation_inside_huge_paragraph_cannot_be_split_by_sentence_or_space_breaks():
    formula = "$$" + "x_{i}=a_{i}+b_{i}; " * 200 + "$$"
    source = "До формулы. " * 150 + formula + " После формулы. " * 150
    values = passages(source)
    assert "".join(values) == source
    assert sum(formula in value for value in values) == 1


def test_unclosed_display_equation_is_preserved_without_inventing_a_closing_token():
    source = "Условие.\n\n$$" + "X=Y+Z\n\n" * 600
    values = passages(source)
    assert "".join(values) == source
    assert sum(value.count("$$") for value in values) == 1
    assert source[source.index("$$"):] in values[-1]


def test_unbroken_long_token_is_retained_whole_not_arbitrarily_truncated():
    source = "a" * 10000
    assert passages(source) == [source]


def test_identifiers_and_passage_text_are_stable_and_per_evidence():
    evidence = [item("Первое правило.\n\nВторое правило."), item("Иной источник.", "E8")]
    assert build_evidence_anchors(evidence) == build_evidence_anchors(deepcopy(evidence))
    assert list(build_evidence_anchors(evidence)) == ["E1:P1", "E8:P1"]
    reordered = build_evidence_anchors(list(reversed(evidence)))
    assert reordered["E1:P1"] == build_evidence_anchors(evidence)["E1:P1"]


def test_payload_retains_all_source_text_and_applicability_version_without_raw_text_duplicate():
    evidence = [item("Документированное правило.", applicability="Только по согласованному графику", version_status="current")]
    payload = evidence_anchor_payload(evidence)
    assert payload == [{"evidence_id": "E1", "file": "Методика.md", "section_path": "Общий порядок",
                        "applicability": "Только по согласованному графику", "version_status": "current",
                        "passages": [{"passage_id": "E1:P1", "text": "Документированное правило."}]}]
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload


def test_resolver_binds_quote_programmatically_in_requested_order():
    evidence = [item("Первый документ."), item("Второй документ.", "E2")]
    resolved = resolve_passages(["E2:P1", "E1:P1"], evidence)
    assert resolved == (["E2", "E1"], [{"evidence_id": "E2", "quote": "Второй документ."},
                                     {"evidence_id": "E1", "quote": "Первый документ."}])


def test_multiple_passages_of_one_evidence_share_one_evidence_id_but_keep_all_quotes():
    source = "Правило. " * 180 + "\n\n" + "Ограничение. " * 120
    evidence = [item(source)]
    anchors = build_evidence_anchors(evidence)
    result = resolve_passages(list(anchors), evidence)
    assert result[0] == ["E1"]
    assert "".join(quote["quote"] for quote in result[1]) == source


@pytest.mark.parametrize("selection", [
    None, "E1:P1", ("E1:P1",), [], [None], [1], [{"passage_id": "E1:P1", "quote": "Выдуманный факт"}],
    ["E1:P1", "E1:P1"], ["E1:P1", "E9:P1"], ["E1:P2"], ["e1:p1"], [" E1:P1"],
    ["E1:P1"] * 33,
])
def test_bad_passage_selections_are_rejected_as_a_whole(selection):
    assert resolve_passages(selection, [item("Правило из реального источника.")]) is None


def test_limit_is_32_unique_passages_not_32_quotes_repeated_or_an_unbounded_list():
    evidence = [item("Правило " + str(number), "E" + str(number)) for number in range(1, 34)]
    keys = list(build_evidence_anchors(evidence))
    assert len(resolve_passages(keys[:32], evidence)[1]) == 32
    assert resolve_passages(keys, evidence) is None


@pytest.mark.parametrize("evidence", [
    [item("Первый"), item("Другой", "E1")], [item("Правило", "e1")],
    [item("Правило", "E0")], [item("Правило", None)], [item(123)], ["not-a-mapping"],
])
def test_invalid_evidence_cannot_redirect_or_manufacture_an_anchor(evidence):
    with pytest.raises(ValueError):
        build_evidence_anchors(evidence)
    assert resolve_passages(["E1:P1"], evidence) is None


def test_empty_evidence_and_whitespace_sources_do_not_produce_supporting_passages():
    assert build_evidence_anchors([]) == {}
    assert build_evidence_anchors([item(" \n\t ")]) == {}
    assert resolve_passages(["E1:P1"], [item("")]) is None
    assert evidence_anchor_payload([item(None)]) == [{"evidence_id": "E1", "file": "Методика.md",
        "section_path": "Общий порядок", "applicability": None, "version_status": None, "passages": []}]


def test_passage_ids_never_resolve_from_another_snapshot_or_cached_chat():
    assert resolve_passages(["E9:P1"], [item("Приватный источник", "E9")]) is not None
    assert resolve_passages(["E9:P1"], [item("Другой чат", "E1")]) is None
