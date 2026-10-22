"""Network-free orchestration checks; source selection is not semantic proof."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import socket
from threading import Barrier

import pytest

from corrective_rag import SearchPlan, assign_evidence, generation_prompt, run_corrective
from source_reader import read_sources, validated_writer_payload


QUESTION = "Как выполняют контроль? Не описывай настройку оборудования."
FACT = "Контроль выполняют перед каждым запуском."
PLAN = {
    "canonical_question": "Как выполняют контроль?",
    "requirements": ["Основное правило контроля"],
    "queries": ["Основное правило контроля"],
    "exclusions": ["Настройка оборудования"],
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Integration fixtures must not access the network")
    monkeypatch.setattr(socket, "create_connection", fail)
    monkeypatch.setattr(socket.socket, "connect", fail)


def fragment(text=FACT, *, name="Контроль.md", **extra):
    return {"file": name, "source": name, "section_path": "Основной порядок",
            "text": text, **extra}


def reader_data(prompt):
    """Parse the public reader prompt contract, as in test_source_reader.py."""
    return json.loads(prompt.split("ДАННЫЕ ЧТЕНИЯ:\n", 1)[1])


def writer_data(prompt):
    value, _ = json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])
    return value


def source_selection(data):
    """Select existing passage IDs only; never supply model-authored quotes."""
    passages = [row["passage_id"] for row in data["evidence"][0]["passages"]]
    return {"snapshot_id": data["snapshot_id"], "cards": [{
        "id": "C1", "kind": "rule", "passage_ids": passages,
        "related_card_ids": [], "requirement_ids": ["R1"],
    }], "unresolved_requirement_ids": []}


class PipelineFixture:
    def __init__(self, *, sources=None, plan=None, reading=None, covered=True,
                 answers=None, repair=False, barrier=None):
        self.sources = sources if sources is not None else [fragment()]
        self.plan = deepcopy(plan if plan is not None else PLAN)
        self.reading = reading
        self.covered = covered
        self.answers = answers or [FACT + " [E1]"]
        self.repair = repair
        self.barrier = barrier
        self.calls = []
        self.reader_inputs = []
        self.answers_seen = []
        self.retrieval_calls = []
        self.judge_calls = 0

    def retrieve(self, question, queries, requirements, trace):
        self.retrieval_calls.append((question, list(queries), list(requirements)))
        return deepcopy(self.sources)

    def complete(self, prompt, purpose):
        self.calls.append((purpose, prompt))
        if purpose == "planning":
            return json.dumps(self.plan, ensure_ascii=False)
        if purpose == "source-reading":
            data = reader_data(prompt)
            self.reader_inputs.append(data)
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            if isinstance(self.reading, Exception):
                raise self.reading
            response = self.reading(data) if callable(self.reading) else self.reading
            response = source_selection(data) if response is None else response
            return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        if purpose == "answer":
            self.answers_seen.append(writer_data(prompt))
            return self.answers[min(len(self.answers_seen) - 1, len(self.answers) - 1)]
        assert purpose == "evidence-review"
        if "Проверь доказательность ответа" in prompt:
            self.judge_calls += 1
            # The first unsupported verdict drives bounded repair. Other prose
            # deliberately remains unassessed; this fixture proves no facts.
            claims = ([{"claim_id": "C1", "support": "unsupported", "passage_ids": []}]
                      if self.repair and self.judge_calls == 1 else [])
            return json.dumps({"claims": claims})
        rows = ([{"requirement_id": "R1", "passage_ids": ["E1:P1"], "applicable": True}]
                if self.covered else [])
        return json.dumps({"coverage": rows, "missing_queries": [], "conflicts": []})

    def run(self, question=QUESTION, **options):
        return run_corrective(question, question, retrieve=self.retrieve, complete=self.complete,
                              max_additional_rounds=0, **options)


def test_valid_reader_runs_once_before_primary_with_original_sources_and_constraints():
    model = PipelineFixture()
    original = deepcopy(model.sources)
    result = model.run(read_before_answer=True)
    stages = [purpose for purpose, _ in model.calls]
    assert stages == ["planning", "evidence-review", "source-reading", "answer", "evidence-review"]
    guide = model.answers_seen[0]["source_reading"]
    # Task constraints stay in the original writer payload, not duplicated in
    # the compact guide; its snapshot is still bound to the complete task.
    assert model.answers_seen[0]["question"] == QUESTION
    assert model.answers_seen[0]["canonical_question"] == PLAN["canonical_question"]
    assert model.answers_seen[0]["exclusions"] == PLAN["exclusions"]
    assert model.answers_seen[0]["requirements"] == PLAN["requirements"]
    assert guide["snapshot_id"] == model.reader_inputs[0]["snapshot_id"]
    assert guide["quoted_passages"]["E1:P1"]["quote"] == FACT
    assert guide["coverage_certified"] is False
    assert result.trace["source_reading"]["applied"] is True
    assert model.sources == original
    assert result.context == model.answers_seen[0]["evidence"]


def test_repair_reuses_same_reading_without_another_reader_call_and_tracks_primary_hash():
    primary = "Неподтвержденное утверждение. [E1]"
    repaired = FACT + " [E1]"
    model = PipelineFixture(answers=[primary, repaired], repair=True)
    result = model.run(read_before_answer=True)
    assert [purpose for purpose, _ in model.calls].count("source-reading") == 1
    assert len(model.answers_seen) == 2
    assert model.answers_seen[0]["source_reading"] == model.answers_seen[1]["source_reading"]
    assert model.answers_seen[0]["evidence"] == model.answers_seen[1]["evidence"]
    assert result.trace["verification"]["initial"]["needs_repair"] is True
    assert "primary" not in result.trace["answer_versions"]
    assert result.trace["answer_versions"]["primary_chars"] == len(primary)
    assert result.trace["answer_versions"]["primary_sha256"] == hashlib.sha256(primary.encode()).hexdigest()
    assert result.trace["answer_versions"]["final_sha256"] == hashlib.sha256(repaired.encode()).hexdigest()
    assert result.answer == repaired


def test_public_reader_diagnostics_do_not_repeat_source_quotes_or_task_payload():
    model = PipelineFixture()
    result = model.run(read_before_answer=True)
    diagnostic = result.trace["source_reading"]
    assert diagnostic["applied"] is True
    assert diagnostic["card_count"] == 1
    assert diagnostic["selected_passage_count"] == 1
    assert diagnostic["coverage_certified"] is False
    assert diagnostic["guide_chars"] > 0
    assert FACT not in json.dumps(diagnostic, ensure_ascii=False)
    assert QUESTION not in json.dumps(diagnostic, ensure_ascii=False)
    assert "result" not in diagnostic


@pytest.mark.parametrize("enabled", [True, False])
def test_agent_snapshots_reader_switch_before_a_different_chat_changes_settings(monkeypatch, enabled):
    from test_corrective_integration import FakeModel, agent_with_fake_dependencies

    class ChangingSettingsModel(FakeModel):
        reader_calls = 0

        def complete(self, prompt, **kwargs):
            if prompt.startswith("Планируй поиск"):
                monkeypatch.setenv("RAG_SOURCE_READING", "off" if enabled else "on")
            if prompt.startswith("Прочитай источники"):
                self.reader_calls += 1
                return json.dumps(source_selection(reader_data(prompt)), ensure_ascii=False)
            return super().complete(prompt, **kwargs)

    monkeypatch.setenv("RAG_SOURCE_READING", "on" if enabled else "off")
    monkeypatch.setenv("RAG_PLANNER_THINKING", "false")
    model = ChangingSettingsModel()
    agent = agent_with_fake_dependencies(model)
    agent.ask(QUESTION, pipeline_mode="on")
    assert model.reader_calls == int(enabled)
    assert agent.last_retrieval_trace()["source_reading"]["enabled"] == enabled


@pytest.mark.parametrize("failure", [
    "{malformed", TimeoutError("private transport detail"), RuntimeError("private transport detail"),
    lambda data: {**source_selection(data), "snapshot_id": "sha256:stale-chat"},
    lambda data: {**source_selection(data), "quotes": ["invented text"]},
])
def test_reader_failures_fall_back_to_identical_unguided_answer_without_source_loss(failure):
    control = PipelineFixture()
    baseline = control.run()
    candidate = PipelineFixture(reading=failure)
    result = candidate.run(read_before_answer=True)
    assert result.trace["source_reading"]["applied"] is False
    assert "source_reading" not in candidate.answers_seen[0]
    assert candidate.answers_seen == control.answers_seen
    assert result.answer == baseline.answer
    assert result.context == baseline.context
    assert "private transport detail" not in json.dumps(result.trace, ensure_ascii=False)


def test_writer_budget_rejects_entire_valid_reading_without_pruning_sources():
    text = "Длинное обязательное условие. " * 330
    sources = [fragment(text)]
    model = PipelineFixture(sources=sources)
    context = assign_evidence(sources)
    plan = SearchPlan(tuple(PLAN["requirements"]), tuple(PLAN["queries"]),
                      tuple(PLAN["exclusions"]), canonical_question=PLAN["canonical_question"])
    reading = read_sources(QUESTION, asdict(plan), context,
                           lambda prompt, purpose: json.dumps(source_selection(reader_data(prompt))))
    assert reading.assessed
    assert validated_writer_payload(reading, QUESTION, asdict(plan), context, max_chars=20000)
    assert validated_writer_payload(reading, QUESTION, asdict(plan), context, max_chars=8192) is None
    result = model.run(read_before_answer=True)
    assert result.trace["source_reading"]["applied"] is False
    assert "source_reading" not in model.answers_seen[0]
    assert model.answers_seen[0]["evidence"][0]["text"] == text
    assert result.context[0]["text"] == text
    assert sources[0]["text"] == text


def test_reading_does_not_promote_incomplete_coverage_or_unassessed_answer():
    model = PipelineFixture(covered=False)
    result = model.run(read_before_answer=True)
    data = model.answers_seen[0]
    assert result.trace["source_reading"]["applied"] is True
    assert data["coverage"]["complete"] is False
    assert result.trace["coverage_complete"] is False
    assert data["source_reading"]["coverage_certified"] is False
    assert result.trace["verification"]["initial"]["assessment_complete"] is False


@pytest.mark.parametrize("options", [{}, {"read_before_answer": False}])
def test_disabled_default_makes_no_reader_call_or_additional_writer_instruction(options):
    model = PipelineFixture()
    result = model.run(**options)
    assert [purpose for purpose, _ in model.calls] == [
        "planning", "evidence-review", "answer", "evidence-review"]
    assert "source_reading" not in model.answers_seen[0]
    assert "source_reading —" not in next(prompt for purpose, prompt in model.calls if purpose == "answer")
    assert result.trace["source_reading"]["enabled"] is False
    assert result.trace["source_reading"]["applied"] is False


def test_parallel_requests_keep_same_local_evidence_ids_bound_to_distinct_snapshots():
    barrier = Barrier(2)
    models = [PipelineFixture(sources=[fragment("Только источник ALPHA.", name="alpha.md")], barrier=barrier),
              PipelineFixture(sources=[fragment("Только источник BETA.", name="beta.md")], barrier=barrier)]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda model: model.run(read_before_answer=True), models))
    guides = [model.answers_seen[0]["source_reading"] for model in models]
    assert guides[0]["snapshot_id"] != guides[1]["snapshot_id"]
    assert list(guides[0]["quoted_passages"]) == list(guides[1]["quoted_passages"]) == ["E1:P1"]
    assert "BETA" not in json.dumps(guides[0], ensure_ascii=False)
    assert "ALPHA" not in json.dumps(guides[1], ensure_ascii=False)
    assert all(result.trace["from_cache"] is False for result in results)


@pytest.mark.parametrize("change", ["question", "exclusions", "source", "selection_limits"])
def test_generation_rebinds_reading_and_rejects_same_ids_from_another_task_or_snapshot(change):
    context = assign_evidence([fragment(version_id="v1")])
    plan = SearchPlan(tuple(PLAN["requirements"]), tuple(PLAN["queries"]),
                      tuple(PLAN["exclusions"]), canonical_question=PLAN["canonical_question"])
    reading = read_sources(QUESTION, asdict(plan), context,
                           lambda prompt, purpose: json.dumps(source_selection(reader_data(prompt))))
    assert reading.assessed
    question, current_plan, current_context, limits = QUESTION, plan, deepcopy(context), None
    if change == "question":
        question = "Другой текущий вопрос"
    elif change == "exclusions":
        current_plan = SearchPlan(plan.requirements, plan.queries, (), canonical_question=plan.canonical_question)
    elif change == "source":
        current_context[0]["version_id"] = "v2"
    else:
        limits = {"inventory_enumeration_incomplete": True}
    prompt = generation_prompt(question, current_plan, current_context, {"complete": False},
                               source_reading=reading, selection_limits=limits)
    data = writer_data(prompt)
    assert "source_reading" not in data
    assert data["evidence"] == current_context
    assert data["question"] == question


def test_no_evidence_never_calls_reader_or_writer():
    model = PipelineFixture(sources=[])
    result = model.run(read_before_answer=True)
    assert [purpose for purpose, _ in model.calls] == ["planning", "evidence-review"]
    assert result.context == []
    assert result.trace["verification"] == {"skipped": "no_evidence"}
