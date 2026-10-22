"""Synthetic task-fit contracts; fake verdicts do not measure model quality."""
import json

import pytest

from grounding_verifier import (
    build_grounding_judge_prompt,
    extract_answer_claims,
    safe_partial_answer,
    verify_grounding,
)
from user_task import UserTask


STORAGE = "The storage fee is seven tokens per box."
VELORA = "The Velora surcharge is two tokens per crate."
SOURCES = [
    {"evidence_id": "E1", "file": "toy-storage.txt", "text": STORAGE},
    {"evidence_id": "E2", "file": "toy-velora.txt", "text": VELORA},
]
TASK = UserTask("How is the Velora surcharge calculated?", "How is the Velora surcharge calculated?")
ANSWER = "The storage fee is seven tokens per box [E1]."


def row(identifier="C1", relevance="matched", passage="E1:P1"):
    value = {"claim_id": identifier, "support": "supported", "passage_ids": [passage]}
    if relevance is not None:
        value["task_relevance"] = relevance
    return value


def report(answer=ANSWER, rows=None, task=TASK):
    return verify_grounding(answer, SOURCES, claims=[row()] if rows is None else rows, user_task=task)


def test_prompt_has_exact_authority_and_independent_relevance_without_changing_claim_ids():
    task = UserTask("How is it calculated?", "Previous USER topic: Velora surcharge. Current USER question: How is it calculated?")
    prompt = build_grounding_judge_prompt(ANSWER, SOURCES, task.question, user_task=task)
    assert json.dumps(task.to_payload(), ensure_ascii=False) in prompt
    assert "task_relevance" in prompt
    assert "support=supported" in prompt and "task_relevance=mismatch" in prompt
    assert '"claim_id": "C1"' in prompt
    assert "Velora" in prompt


def test_legacy_prompt_does_not_require_task_relevance():
    assert "task_relevance" not in build_grounding_judge_prompt(ANSWER, SOURCES, TASK.question)


def test_exclusions_and_task_relevance_have_separate_prompt_fields():
    prompt = build_grounding_judge_prompt(ANSWER, SOURCES, TASK.question, user_task=TASK,
                                         excluded_topics=("storage",), canonical_question="A model hypothesis")
    assert '"task_scope"' in prompt and '"user_task"' in prompt
    assert '"excluded_topic_ids"' in prompt and '"task_relevance"' in prompt


def test_supported_wrong_topic_preserves_source_support_but_requires_repair():
    result = report(rows=[row(relevance="mismatch")])
    assert [check.status for check in result.checks if check.kind == "claim"] == ["judge_supported"]
    assert [check.status for check in result.checks if check.kind == "relevance"] == ["mismatch"]
    assert result.assessment_complete is True  # Assessed is not correct.
    assert result.needs_repair is True
    assert result.task_relevance["all_claims_matched"] is False
    assert result.unsupported_spans == []  # Irrelevance is not factual falsity.
    assert "storage fee" not in safe_partial_answer(ANSWER, result, user_task=TASK)


@pytest.mark.parametrize("value", [None, "uncertain", "unassessed", "SUPPORTED", True, {}, [], 0])
def test_missing_or_uncertain_relevance_does_not_become_unsupported(value):
    result = report(rows=[row(relevance=value)])
    assert [check.status for check in result.checks if check.kind == "claim"] == ["judge_supported"]
    assert result.assessment_complete is False
    assert result.needs_repair is True
    assert result.unsupported_spans == []
    partial = safe_partial_answer(ANSWER, result, user_task=TASK)
    assert "storage fee" not in partial
    assert "сведений нет" not in partial


@pytest.mark.parametrize("rows", [[row("C999")], [row(), row()], [row(), row("C999")]])
def test_unknown_and_duplicate_claim_ids_cannot_complete_task_assessment(rows):
    result = report(rows=rows)
    assert result.assessment_complete is False
    assert result.needs_repair is True
    assert result.task_relevance["all_claims_matched"] is False


def test_legacy_exact_text_claim_is_not_an_authority_for_strict_relevance():
    legacy = {"claim": ANSWER, "support": "supported", "evidence_ids": ["E1"],
              "quotes": [{"evidence_id": "E1", "quote": STORAGE}], "task_relevance": "matched"}
    strict = report(rows=[legacy])
    assert any(check.status == "judge_supported" for check in strict.checks)
    assert strict.task_relevance["claim_counts"]["unassessed"] == 1
    assert strict.assessment_complete is False
    old = report(rows=[legacy], task=None)
    assert old.assessment_complete is True
    assert old.needs_repair is False


def test_matched_verdict_cannot_compensate_for_invalid_source_passage():
    result = report(rows=[row(passage="E1:P999")])
    assert any(check.status == "invalid_judge_evidence" for check in result.checks)
    assert result.assessment_complete is False
    assert result.needs_repair is True
    assert "storage fee" not in safe_partial_answer(ANSWER, result, user_task=TASK)


def test_matched_relevance_does_not_publish_an_invalid_verbatim_quote():
    bad_source_link = {"claim_id": "C1", "support": "supported", "evidence_ids": ["E1"],
                       "quotes": [{"evidence_id": "E1", "quote": "An invented supporting quotation."}],
                       "task_relevance": "matched"}
    result = report(rows=[bad_source_link])
    assert result.task_relevance["all_claims_matched"] is True
    assert any(check.status == "invalid_judge_evidence" for check in result.checks)
    assert not any(check.status == "unsupported" for check in result.checks)
    assert result.assessment_complete is False
    assert result.needs_repair is True
    assert "storage fee" not in safe_partial_answer(ANSWER, result, user_task=TASK)


def test_valid_matched_claim_completes_assessment_without_keyword_gate():
    task = UserTask("Explain the handling charge.", "Explain the handling charge.")
    result = report(task=task)
    assert result.assessment_complete is True
    assert result.needs_repair is False
    assert result.task_relevance["all_claims_matched"] is True
    assert safe_partial_answer(ANSWER, result, user_task=task) == ANSWER


def test_judge_callback_gets_the_same_authoritative_context():
    prompts = []
    def judge(prompt):
        prompts.append(prompt)
        return {"claims": [row(relevance="mismatch")]}
    result = verify_grounding(ANSWER, SOURCES, question=TASK.question, judge=judge, user_task=TASK)
    assert len(prompts) == 1
    assert json.dumps(TASK.to_payload(), ensure_ascii=False) in prompts[0]
    assert result.needs_repair is True


def test_judge_outage_is_unassessed_and_strict_publication_withholds_draft():
    def failed(_):
        raise TimeoutError("synthetic")
    result = verify_grounding(ANSWER, SOURCES, judge=failed, user_task=TASK)
    assert result.assessment_complete is False
    assert result.needs_repair is True
    assert not any(check.status == "unsupported" for check in result.checks)
    assert "storage fee" not in safe_partial_answer(ANSWER, result, user_task=TASK)
    old = verify_grounding(ANSWER, SOURCES, judge=failed)
    assert old.needs_repair is False
    assert safe_partial_answer(ANSWER, old) == ANSWER


def test_repair_binds_to_the_same_task_without_duplicating_context():
    result = report(rows=[row(relevance="mismatch")])
    prompt = result.repair_prompt(user_task=TASK)
    assert "user_task" in prompt and result.task_relevance["task_sha256"] in prompt
    assert TASK.question not in prompt
    assert '"kind": "relevance"' in prompt and '"status": "mismatch"' in prompt
    with pytest.raises(ValueError):
        result.repair_prompt()
    with pytest.raises(ValueError):
        result.repair_prompt(user_task=UserTask("Other task", "Other task"))


def test_task_and_answer_rebinding_do_not_publish_an_old_assessment():
    result = report()
    for task, answer in [(UserTask("Other task", "Other task"), ANSWER), (TASK, ANSWER + " Another topic.")]:
        assert "storage fee" not in safe_partial_answer(answer, result, user_task=task)
    assert "storage fee" not in safe_partial_answer(ANSWER, result)
    assert "storage fee" not in safe_partial_answer(ANSWER, verify_grounding(ANSWER, SOURCES), user_task=TASK)


def test_partial_keeps_only_exact_matched_supported_spans():
    answer = ANSWER + "\n\n" + "The Velora surcharge is two tokens per crate [E2]."
    result = report(answer, [row(relevance="mismatch"), row("C2", passage="E2:P1")])
    partial = safe_partial_answer(answer, result, user_task=TASK)
    assert "storage fee" not in partial
    assert "The Velora surcharge is two tokens per crate [E2]." in partial
    assert "Часть ответа" in partial
    assert result.checks[0].status == "judge_supported"


def test_unassessed_second_claim_is_not_silently_published_with_a_good_first_claim():
    good = "The Velora surcharge is two tokens per crate [E2]."
    answer = good + "\n\n" + ANSWER
    result = report(answer, [row(passage="E2:P1"), row("C2", relevance=None)])
    partial = safe_partial_answer(answer, result, user_task=TASK)
    assert good in partial
    assert "storage fee" not in partial
    assert result.assessment_complete is False


def test_invalid_extra_citation_is_not_retained_in_strict_partial():
    answer = "The storage fee is seven tokens per box [E1, E999]."
    result = report(answer)
    assert "storage fee" not in safe_partial_answer(answer, result, user_task=TASK)


def test_source_unsupported_remains_independent_from_matched_relevance():
    result = report(rows=[{"claim_id": "C1", "support": "unsupported", "passage_ids": [],
                          "task_relevance": "matched"}])
    assert any(check.kind == "claim" and check.status == "unsupported" for check in result.checks)
    assert result.task_relevance["all_claims_matched"] is True
    assert result.needs_repair is True
    assert "storage fee" not in safe_partial_answer(ANSWER, result, user_task=TASK)


def test_unlisted_answer_tail_is_not_certified_or_published():
    answer = "\n\n".join(f"Storage record {i} has seven tokens [E1]." for i in range(101))
    claims = [row(claim["claim_id"]) for claim in extract_answer_claims(answer)]
    result = report(answer, claims)
    assert result.task_relevance["unlisted_answer_tail"] is True
    assert result.assessment_complete is False
    assert result.needs_repair is True
    assert "Storage record 100" not in safe_partial_answer(answer, result, user_task=TASK)


def test_overflow_judge_rows_cannot_complete_relevance():
    result = report(rows=[row() for _ in range(101)])
    assert result.task_relevance["judge_rows_overflow"] is True
    assert result.assessment_complete is False
    assert result.needs_repair is True
