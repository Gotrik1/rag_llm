"""Task compliance is separate from provenance; no model/network calls here."""
import json

import pytest

from grounding_verifier import (build_grounding_judge_prompt, extract_answer_claims,
                                safe_partial_answer, verify_grounding)


EVIDENCE = [{"evidence_id":"E1", "file":"Регламент.md",
             "text":"Контроль выполняют перед запуском. Оборудование настраивают при монтаже."}]
ANSWER = "Контроль выполняют перед запуском. [E1]\n\nОборудование настраивают при монтаже. [E1]"


def judgments(answer=ANSWER):
    return [{"claim_id":claim["claim_id"], "support":"supported",
             "passage_ids":["E1:P1"], "scope_disposition":"in_scope", "excluded_topic_ids":[]}
            for claim in extract_answer_claims(answer)]


def test_scope_prompt_carries_canonical_task_and_stable_constraint_ids():
    prompt = build_grounding_judge_prompt(ANSWER,EVIDENCE,question="Только контроль",
        canonical_question="Как выполняют контроль перед запуском?", excluded_topics=("Настройка оборудования",))
    payload = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]
    assert payload["question"] == "Только контроль"
    assert payload["task_scope"] == {"canonical_question":"Как выполняют контроль перед запуском?",
                                      "excluded_topics":[{"id":"X1","text":"Настройка оборудования"}]}
    assert "excluded_topic_ids" in prompt
    assert payload["evidence"][0]["passages"][0]["text"] == EVIDENCE[0]["text"]


def test_real_source_support_does_not_override_explicitly_excluded_subject():
    rows = judgments()
    rows[-1]["excluded_topic_ids"] = ["X1"]
    rows[-1]["scope_disposition"] = "excluded"
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert all(check.status == "judge_supported" for check in report.checks if check.kind == "claim")
    violation = next(check for check in report.checks if check.kind == "scope")
    assert ANSWER[violation.start:violation.end] == violation.text
    assert "Оборудование" in violation.text
    assert report.needs_repair
    assert report.task_scope["assessment_complete"]
    assert "Настройка оборудования" in report.repair_prompt()
    partial = safe_partial_answer(ANSWER,report)
    assert "Контроль выполняют перед запуском" in partial
    assert "Оборудование настраивают" not in partial
    assert "Не удалось подтвердить" not in partial  # relevance is not factual falsehood


@pytest.mark.parametrize("bad_ids", [None,"X1",["X2"],["X1","X1"],[None],[{"id":"X1"}]])
def test_malformed_scope_judgment_does_not_authorize_deletion(bad_ids):
    rows = judgments()
    rows[-1]["excluded_topic_ids"] = bad_ids
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.needs_repair
    assert not report.task_scope["assessment_complete"]
    assert not report.assessment_complete
    assert safe_partial_answer(ANSWER,report) == ANSWER


def test_unknown_claim_and_duplicate_conflicting_judgments_are_not_scope_proof():
    rows = judgments()
    rows.append({**rows[-1],"excluded_topic_ids":["X1"]})
    rows.append({"claim_id":"C999","excluded_topic_ids":["X1"]})
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.needs_repair
    assert not report.task_scope["assessment_complete"]


def test_omitted_task_verdict_is_unassessed_not_an_error_or_success():
    rows = judgments()
    for row in rows:
        row.pop("excluded_topic_ids")
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.needs_repair
    assert report.task_scope["assessed_claim_count"] == 0
    assert not report.assessment_complete


def test_scope_is_off_without_explicit_constraints_even_if_model_sends_x_ids():
    rows = judgments()
    rows[-1]["excluded_topic_ids"] = ["X1"]
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows)
    assert not report.needs_repair
    assert report.task_scope == {}
    assert safe_partial_answer(ANSWER,report) == ANSWER


def test_empty_scope_verdict_does_not_invent_an_exclusion_violation():
    answer = "Настройку оборудования не рассматриваю."
    rows = [{"claim_id":claim["claim_id"],"support":"uncertain","passage_ids":[],
             "scope_disposition":"in_scope","excluded_topic_ids":[]}
            for claim in extract_answer_claims(answer)]
    report = verify_grounding(answer,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.needs_repair
    assert safe_partial_answer(answer,report) == answer


def test_scope_judge_outage_preserves_answer_without_certification():
    def offline(_):
        raise TimeoutError("not a factual contradiction")
    report = verify_grounding(ANSWER,EVIDENCE,judge=offline,excluded_topics=("Настройка оборудования",))
    assert not report.needs_repair
    assert not report.task_scope["assessment_complete"]
    assert safe_partial_answer(ANSWER,report) == ANSWER


def test_mixed_scope_clause_requests_rewrite_but_does_not_delete_the_useful_fact():
    answer = "Контроль выполняют перед запуском; оборудование настраивают при монтаже. [E1]"
    rows = judgments(answer)
    assert len(rows) == 1
    rows[0].update(scope_disposition="mixed",excluded_topic_ids=["X1"])
    report = verify_grounding(answer,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert report.needs_repair
    assert any(check.status == "mixed_scope" for check in report.checks)
    assert report.unsupported_spans == []
    assert safe_partial_answer(answer,report) == answer


def test_unknown_extra_claim_prevents_scope_completeness_even_when_known_claims_are_assessed():
    rows = judgments()
    rows.append({**rows[0],"claim_id":"C999"})
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.task_scope["assessment_complete"]
    assert report.task_scope["invalid_claim_links"] == 1
    assert not report.needs_repair


def test_unlisted_answer_tail_after_claim_budget_cannot_be_certified_for_scope():
    answer = "\n\n".join(f"Контроль {index} выполняют перед запуском. [E1]" for index in range(101))
    rows = [{"claim_id":claim["claim_id"],"support":"uncertain","passage_ids":[],
             "scope_disposition":"in_scope","excluded_topic_ids":[]}
            for claim in extract_answer_claims(answer)]
    assert len(rows) == 100
    report = verify_grounding(answer,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.task_scope["assessment_complete"]
    assert report.task_scope["unlisted_answer_tail"]


def test_judge_overflow_cannot_disappear_from_scope_diagnostics():
    rows = judgments()
    rows.extend([{**rows[0],"claim_id":f"C{number}"} for number in range(3,105)])
    report = verify_grounding(ANSWER,EVIDENCE,claims=rows,excluded_topics=("Настройка оборудования",))
    assert not report.task_scope["assessment_complete"]
    assert report.task_scope["judge_rows_overflow"]


def test_scope_check_runs_in_existing_judge_call_without_extra_model_request():
    prompts = []
    def judge(prompt):
        prompts.append(prompt)
        return {"claims":judgments()}
    report = verify_grounding(ANSWER,EVIDENCE,judge=judge,excluded_topics=("Настройка оборудования",))
    assert len(prompts) == 1
    assert report.task_scope["assessment_complete"]


@pytest.mark.parametrize("scope_enabled",[False,True])
def test_pipeline_reuses_existing_repair_for_source_supported_but_excluded_text(scope_enabled):
    from test_source_reading_integration import PipelineFixture, FACT

    extra = "Оборудование настраивают при монтаже."
    bad = FACT + " [E1]\n\n" + extra + " [E1]"

    class TaskJudge(PipelineFixture):
        def complete(self,prompt,purpose):
            if purpose == "evidence-review" and prompt.startswith("Проверь доказательность ответа"):
                self.calls.append((purpose,prompt))
                self.judge_calls += 1
                data = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]
                rows = [{"claim_id":item["claim_id"],"support":"supported","passage_ids":["E1:P1"],
                         "scope_disposition":"excluded" if extra in item["text"] else "in_scope",
                         "excluded_topic_ids":["X1"] if extra in item["text"] else []}
                        for item in data["answer_claims"]]
                assert ("task_scope" in data) == scope_enabled
                return json.dumps({"claims":rows})
            return super().complete(prompt,purpose)

    model = TaskJudge(answers=[bad,FACT + " [E1]"])
    model.sources[0]["text"] = FACT + " " + extra
    result = model.run(check_answer_scope=scope_enabled)
    assert model.judge_calls == (2 if scope_enabled else 1)
    assert len(model.answers_seen) == (2 if scope_enabled else 1)
    assert (extra not in result.answer) == scope_enabled
    assert not any(purpose == "source-reading" for purpose,_ in model.calls)
    if scope_enabled:
        assert result.trace["verification"]["initial"]["needs_repair"]
        assert not result.trace["verification"]["after_repair"]["needs_repair"]


@pytest.mark.parametrize("enabled",[False,True])
def test_agent_freezes_scope_check_flag_before_other_settings_change(monkeypatch,enabled):
    from test_corrective_integration import FakeModel, agent_with_fake_dependencies, QUESTION

    class ChangingSettingsModel(FakeModel):
        def complete(self,prompt,**options):
            if prompt.startswith("Планируй поиск"):
                monkeypatch.setenv("RAG_TASK_SCOPE_CHECK","off" if enabled else "on")
                self.calls.append((0,prompt,options))
                return json.dumps({"requirements":["Правило контроля"],"queries":["Контроль"],
                                   "exclusions":["Настройка оборудования"]})
            return super().complete(prompt,**options)

    monkeypatch.setenv("RAG_SOURCE_READING","off")
    monkeypatch.setenv("RAG_PLANNER_THINKING","false")
    monkeypatch.setenv("RAG_TASK_SCOPE_CHECK","on" if enabled else "off")
    model = ChangingSettingsModel()
    agent = agent_with_fake_dependencies(model)
    agent.ask(QUESTION,pipeline_mode="on")
    assert agent.last_retrieval_trace()["experiments"]["task_scope_check"] == enabled
    prompt = next(prompt for _,prompt,_ in model.calls if prompt.startswith("Проверь доказательность ответа"))
    assert ('"task_scope"' in prompt) == enabled


def test_compose_passes_disabled_experiment_flags_to_app_and_generation_worker():
    from pathlib import Path
    import yaml
    configuration = yaml.safe_load(Path(__file__).with_name("compose.yaml").read_text(encoding="utf-8"))
    for service in ("app","generation-worker"):
        environment = configuration["services"][service]["environment"]
        for flag in ("RAG_SOURCE_READING","RAG_TASK_SCOPE_CHECK"):
            assert environment[flag] == "${" + flag + ":-off}"
