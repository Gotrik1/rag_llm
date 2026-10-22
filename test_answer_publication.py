"""Publication/heading linkage invariants, with synthetic, non-oracle verdicts."""
from dataclasses import FrozenInstanceError
import json
import tracemalloc

import pytest

from grounding_verifier import (
    TaskPublication, build_grounding_judge_prompt, extract_answer_claims,
    safe_partial_answer, task_publication_candidate, verify_grounding,
)
from user_task import UserTask


TASK = UserTask("Describe inspection.", "Describe inspection.")
SOURCES = [{"evidence_id": "E7", "file": "inspection.txt",
            "text": "In emergency mode inspect every hour. The catalogue is available."}]


def judgments(answer, *, acknowledge=True):
    rows = []
    for claim in extract_answer_claims(answer):
        row = {"claim_id": claim["claim_id"], "support": "supported",
               "passage_ids": ["E7:P1"], "task_relevance": "matched"}
        if acknowledge:
            row["context_ids"] = claim["context_ids"]
        rows.append(row)
    return rows


def checked(answer, rows=None):
    return verify_grounding(answer, SOURCES, claims=judgments(answer) if rows is None else rows, user_task=TASK)


def payload(answer):
    prompt = build_grounding_judge_prompt(answer, SOURCES, TASK.question, user_task=TASK)
    return prompt, json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]


def test_publication_has_structured_count_and_is_immutable():
    answer = "Inspect every hour [E7]."
    candidate = task_publication_candidate(answer, checked(answer), user_task=TASK)
    assert candidate == TaskPublication(answer, 1)
    with pytest.raises(FrozenInstanceError):
        candidate.verified_claim_count = 0


def test_safe_partial_delegates_without_neutral_message_heuristics():
    answer = "Inspect every hour [E7]."
    report = checked(answer)
    assert safe_partial_answer(answer, report, user_task=TASK) == task_publication_candidate(answer, report, user_task=TASK).answer
    missing = task_publication_candidate(answer, verify_grounding(answer, SOURCES), user_task=TASK)
    assert missing.verified_claim_count == 0
    assert answer not in missing.answer


def test_judge_examples_use_only_real_current_evidence_aliases():
    prompt, data = payload("Inspect every hour [E7].")
    assert '"passage_ids":["E7:P1"]' in prompt
    assert "E1" not in prompt
    assert data["answer_claims"][0]["cited_evidence_ids"] == ["E7"]
    empty = build_grounding_judge_prompt("No data.", [], user_task=TASK)
    assert '"passage_ids":[]' in empty and "E1" not in empty


def test_judge_example_respects_the_sample_claims_actual_citation():
    other = {"evidence_id": "E92", "file": "other.txt", "text": "Another fact."}
    prompt = build_grounding_judge_prompt("Inspect every hour [E7].", [other, *SOURCES], user_task=TASK)
    example = json.JSONDecoder().raw_decode(prompt.split("Только JSON: ", 1)[1])[0]
    assert example["claims"][0]["passage_ids"] == ["E7:P1"]
    no_citation = build_grounding_judge_prompt("Inspect every hour.", SOURCES, user_task=TASK)
    row = json.JSONDecoder().raw_decode(no_citation.split("Только JSON: ", 1)[1])[0]["claims"][0]
    assert row["support"] == "uncertain" and row["passage_ids"] == []


def test_empty_answer_does_not_invent_a_claim_in_judge_example():
    prompt = build_grounding_judge_prompt("", SOURCES, user_task=TASK, excluded_topics=("Another topic",))
    example = json.JSONDecoder().raw_decode(prompt.split("Только JSON: ", 1)[1])[0]
    assert example == {"claims": []}


def test_headings_are_exact_catalogued_ancestors_once_not_copied_into_claim_text():
    answer = "# Equipment\n\n## Only in emergency mode\n\nInspect every hour [E7]."
    prompt, data = payload(answer)
    assert [item["context_id"] for item in data["heading_contexts"]] == ["H1", "H2"]
    assert data["heading_contexts"][1]["parent_id"] == "H1"
    assert data["answer_claims"][0]["context_ids"] == ["H1", "H2"]
    assert data["answer_claims"][0]["text"] == "Inspect every hour [E7]."
    for heading in data["heading_contexts"]:
        assert answer[heading["start"]:heading["end"]] == heading["text"]
    assert prompt.count('"text": "## Only in emergency mode"') == 1
    assert "СОВМЕСТНЫЙ смысл" in prompt


@pytest.mark.parametrize("ack", [None, [], ["H2"], ["H1", "H1"], ["H2", "H1"], "H1"])
def test_missing_or_malformed_heading_acknowledgement_cannot_verify_bare_body(ack):
    answer = "# Only in emergency mode\n\nInspect every hour [E7]."
    rows = judgments(answer, acknowledge=False)
    if ack is not None:
        rows[0]["context_ids"] = ack
    report = checked(answer, rows)
    assert report.assessment_complete is False
    assert report.needs_repair is True
    assert report.task_relevance["claim_counts"]["unassessed"] == 1
    assert not any(check.status == "unsupported" for check in report.checks)
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0


def test_valid_partial_preserves_conditional_heading_and_only_supported_body():
    answer = "## Only in emergency mode\n\nInspect every hour [E7].\n\nUnknown procedure [E7]."
    rows = judgments(answer)
    rows[1].update(support="unsupported", passage_ids=[], task_relevance="mismatch")
    result = task_publication_candidate(answer, checked(answer, rows), user_task=TASK)
    assert result.verified_claim_count == 1
    assert result.answer.startswith("## Only in emergency mode\n\nInspect every hour [E7].")
    assert "Unknown procedure" not in result.answer


def test_neutral_heading_is_not_required_to_match_source_words_literally():
    answer = "# Operating instructions\n\nInspect every hour [E7]."
    report = checked(answer)
    assert report.assessment_complete is True
    assert not report.needs_repair
    assert task_publication_candidate(answer, report, user_task=TASK) == TaskPublication(answer, 1)


def test_factual_heading_is_jointly_assessed_not_silently_skipped():
    answer = "# The price is 999 rubles.\n\nThe catalogue is available [E7]."
    _, data = payload(answer)
    assert data["heading_contexts"][0]["text"] == "# The price is 999 rubles."
    rows = judgments(answer)
    # The fake judge assesses the joint meaning as uncertain, not the true
    # catalogue sentence alone. These are contract tests, not semantic proof.
    rows[0].update(support="uncertain", passage_ids=[])
    report = checked(answer, rows)
    assert report.assessment_complete is False
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0


def test_orphan_factual_heading_gets_its_own_claim_and_cannot_escape_by_omission():
    answer = "# Catalogue\n\nThe catalogue is available [E7].\n\n# The price is 999 rubles."
    claims = extract_answer_claims(answer)
    assert len(claims) == 2
    assert claims[1]["text"] == "# The price is 999 rubles."
    report = checked(answer, judgments(answer)[:1])
    assert report.assessment_complete is False
    partial = task_publication_candidate(answer, report, user_task=TASK)
    assert partial.verified_claim_count == 1
    assert "# Catalogue" in partial.answer
    assert "999" not in partial.answer


def test_entire_orphan_answer_is_not_automatically_certified():
    answer = "# The price is 999 rubles."
    assert len(extract_answer_claims(answer)) == 1
    report = checked(answer, [])
    assert not report.assessment_complete
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0


def test_nested_partial_preserves_all_ancestors_and_does_not_merge_siblings():
    answer = "# Equipment\n\n## Emergency\n\nInspect every hour [E7].\n\n## Ordinary\n\nUnknown [E7]."
    rows = judgments(answer)
    rows[1].update(support="unsupported", passage_ids=[], task_relevance="mismatch")
    partial = task_publication_candidate(answer, checked(answer, rows), user_task=TASK)
    assert partial.verified_claim_count == 1
    assert partial.answer.startswith("# Equipment\n\n## Emergency")
    assert "## Ordinary" not in partial.answer


def test_context_deduplication_uses_id_not_equal_heading_text():
    answer = "# Equipment\n\n## Procedure\n\nInspect every hour [E7].\n\n## Procedure\n\nThe catalogue is available [E7]."
    partial = task_publication_candidate(answer, checked(answer), user_task=TASK)
    assert partial.verified_claim_count == 2
    assert partial.answer.count("# Equipment") == 1
    assert partial.answer.count("## Procedure") == 2


def test_bold_label_condition_is_preserved_and_bound_like_a_heading():
    answer = "**Only in emergency mode**\n\nInspect every hour [E7].\n\nUnknown [E7]."
    _, data = payload(answer)
    assert data["heading_contexts"][0]["kind"] == "bold"
    rows = judgments(answer)
    rows[1].update(support="unsupported", passage_ids=[])
    partial = task_publication_candidate(answer, checked(answer, rows), user_task=TASK)
    assert partial.verified_claim_count == 1
    assert partial.answer.startswith("**Only in emergency mode**\n\nInspect every hour [E7].")


@pytest.mark.parametrize("next_heading", ["**Inspection**", "### Inspection", "# Inspection"])
def test_consecutive_prebody_heading_prefix_cannot_drop_a_bold_condition(next_heading):
    answer = "**Only in emergency mode**\n" + next_heading + "\n\nInspect every hour [E7]."
    claims = extract_answer_claims(answer)
    assert len(claims) == 1
    assert claims[0]["context_ids"] == ["H1", "H2"]
    rows = judgments(answer)
    complete = task_publication_candidate(answer, checked(answer, rows), user_task=TASK)
    assert complete.verified_claim_count == 1
    assert "**Only in emergency mode**" in complete.answer
    rows[0]["context_ids"] = ["H2"]
    assert task_publication_candidate(answer, checked(answer, rows), user_task=TASK).verified_claim_count == 0


def test_prebody_context_bundle_survives_children_but_not_closed_sibling_scope():
    answer = ("**Only in emergency mode**\n## Inspection\n\nInspect every hour [E7].\n\n"
              "### Detailed\n\nThe catalogue is available [E7].\n\n## Ordinary\n\nInspect every hour [E7].")
    claims = extract_answer_claims(answer)
    assert [claim["context_ids"] for claim in claims] == [["H1", "H2"], ["H1", "H2", "H3"], ["H4"]]


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_heading_markers_inside_fenced_code_are_not_context(fence):
    answer = fence + "text\n# This is code\n\nvalue = 1\n" + fence + "\n\nInspect every hour [E7]."
    _, data = payload(answer)
    assert "heading_contexts" not in data
    assert len(data["answer_claims"]) == 2
    assert data["answer_claims"][0]["text"].startswith(fence)
    assert data["answer_claims"][0]["text"].endswith(fence)


def test_heading_markers_inside_math_are_not_context_and_math_stays_atomic():
    answer = "$$\n# x = y\n\nz = q\n$$\n\nInspect every hour [E7]."
    _, data = payload(answer)
    assert "heading_contexts" not in data
    assert len(data["answer_claims"]) == 2
    assert data["answer_claims"][0]["text"].startswith("$$")
    assert data["answer_claims"][0]["text"].endswith("$$")


def test_unverified_formula_in_ancestor_cannot_be_published_with_supported_body():
    answer = "# Price $p = 999$\n\nThe catalogue is available [E7]."
    report = checked(answer)
    assert any(check.kind == "formula" for check in report.checks)
    assert task_publication_candidate(answer, report, user_task=TASK).verified_claim_count == 0


def test_max_claim_cap_does_not_silently_certify_a_factual_heading_tail():
    answer = "\n\n".join("Inspect every hour [E7]." for _ in range(100)) + "\n\n# The price is 999 rubles."
    assert len(extract_answer_claims(answer)) == 100
    report = checked(answer)
    assert report.unassessed_claim_count > 0
    assert report.assessment_complete is False
    partial = task_publication_candidate(answer, report, user_task=TASK)
    assert partial.verified_claim_count == 100
    assert "999" not in partial.answer


def test_task_or_answer_rebinding_returns_count_zero():
    answer = "# Equipment\n\nInspect every hour [E7]."
    report = checked(answer)
    assert task_publication_candidate(answer, report, user_task=UserTask("Other", "Other")).verified_claim_count == 0
    assert task_publication_candidate(answer + " More.", report, user_task=TASK).verified_claim_count == 0


def test_long_prebody_prefix_retains_every_condition_without_truncation():
    count = 2500
    answer = "\n\n".join(f"# Condition {index}" for index in range(count)) + "\n\nInspect every hour [E7]."
    _, data = payload(answer)
    assert len(data["answer_claims"]) == 1
    assert data["answer_claims"][0]["context_ids"] == [f"H{index}" for index in range(1, count + 1)]
    assert len(data["heading_contexts"]) == count
    assert data["heading_contexts"][-1]["parent_id"] == f"H{count-1}"
    assert data["heading_contexts"][-1]["text"] == f"# Condition {count-1}"


def test_long_orphan_prefix_is_claim_bounded_without_quadratic_internal_copies():
    answer = "\n\n".join(f"## Heading {index}" for index in range(4000))
    tracemalloc.start()
    try:
        claims = extract_answer_claims(answer)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(claims) == 100
    assert claims[-1]["context_ids"] == [f"H{index}" for index in range(1, 100)]
    # Generous allocation bound, not a wall-clock assertion. The former eager
    # prefix copies used ~65 MiB for this 67k-character input; persistent links
    # require only a few MiB while retaining all selected claims' ancestry.
    assert peak < 20 * 1024 * 1024
