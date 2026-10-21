"""Offline paired report contracts; no RAG runtime, network or database imports."""
from copy import deepcopy
import json

import pytest

from evals.compare_pipeline_reports import compare_reports, main


def row(identifier, source=0.5, locator=0.5, latency=3.0, **kwargs):
    return {"id": identifier, "question": "Вопрос " + identifier, "latency_s": latency,
            "retrieval": {"source_recall": source, "locator_recall": locator}, **kwargs}


def report(rows=None, **kwargs):
    result = {"scope": "ready-global-only", "mode": "legacy", "answers_enabled": False,
              "dataset_sha256": "a" * 64, "source_count": 1,
              "source_manifest": [{"id": "doc-1", "file_name": "Методика.md", "sha256": "b" * 64, "version": "1"}],
              "runtime": {"model": "synthetic-model", "EMBED_MODEL": "synthetic-embedding"},
              "cases": rows if rows is not None else [row("A")]}
    result.update(kwargs)
    return result


def test_alignment_uses_ids_not_row_order_and_means_use_completed_pairs_only():
    before = report([row("A", 0.2, 0.4, 1), row("B", 0.8, 0.6, 3), row("C", 0.5, 0.5, 5)])
    after = report([row("C", 0.5, 0.5, 6), row("A", 0.4, 0.6, 2), row("B", 0.8, 0.6, 4)])
    result = compare_reports(before, after)
    assert result["valid"]
    assert result["comparison_eligible"] == 3
    assert result["paired_metrics"]["source_recall"] == {"before": 0.5, "after": 0.566667, "delta_pp": 6.666667}
    assert result["paired_metrics"]["latency_s"]["before"] == {"p50": 3, "p95": 4.8}
    assert result["paired_metrics"]["latency_s"]["after"] == {"p50": 4, "p95": 5.8}
    assert [(item["id"], item["status"]) for item in result["cases"]] == [("A", "better"), ("B", "unchanged"), ("C", "unchanged")]
    assert result["semantic_quality"].startswith("not assessed")


@pytest.mark.parametrize("change", [
    {"scope": "project-only"}, {"scope": None}, {"dataset_sha256": "c" * 64},
    {"corpus_sha256": "d" * 64}, {"source_manifest_sha256": "e" * 64},
    {"source_manifest": [{"id": "doc-1", "sha256": "f" * 64}]},
])
def test_scope_dataset_or_corpus_difference_blocks_comparison(change):
    before = report(corpus_sha256="b" * 64, source_manifest_sha256="b" * 64)
    after = deepcopy(before)
    after.update(change)
    result = compare_reports(before, after)
    assert result["valid"] is False
    assert result["comparison_eligible"] == 0
    assert result["validation_errors"]
    assert result["paired_metrics"] == {}


def test_manifest_list_order_does_not_matter_but_version_and_content_do():
    before = report()
    before["source_manifest"].append({"id": "doc-2", "sha256": "d" * 64, "version": "3"})
    after = deepcopy(before)
    after["source_manifest"].reverse()
    assert compare_reports(before, after)["valid"]
    after["source_manifest"][0]["version"] = "4"
    assert compare_reports(before, after)["valid"] is False


def test_source_count_difference_warns_when_both_manifests_not_available():
    before, after = report(), report(source_count=2)
    del before["source_manifest"]
    del after["source_manifest"]
    result = compare_reports(before, after)
    assert result["valid"]
    assert any("Source counts differ" in warning for warning in result["warnings"])
    assert result["controlled_comparison"] is False


def test_errors_are_reported_separately_not_imputed_as_zero_recall():
    before = report([row("A", 1, 1), row("B", error="NetworkError: private raw message")])
    after = report([row("A", error="TimeoutError: secret"), row("B", 1, 1)])
    result = compare_reports(before, after)
    assert result["comparison_eligible"] == 0
    assert result["runs"]["before"]["completed"] == 1
    assert result["runs"]["before"]["errors"] == 1
    assert result["paired_metrics"]["source_recall"]["before"] is None
    assert [item["error_transition"] for item in result["cases"]] == ["introduced", "resolved"]
    assert "private raw message" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_partial_overlap_does_not_compare_missing_ids_as_failures_or_successes():
    before = report([row("A", 0, 0), row("B", 0.5, 0.5)], selected_case_ids=["A", "B", "C"], dataset_case_ids=["A", "B", "C"])
    after = report([row("C", 1, 1), row("B", 0.75, 0.5)], selected_case_ids=["A", "B", "C"], dataset_case_ids=["A", "B", "C"])
    result = compare_reports(before, after)
    assert result["cohort"]["missing_from_before"] == ["C"]
    assert result["cohort"]["missing_from_after"] == ["A"]
    assert result["cohort"]["before"]["state"] == "partial-run"
    assert result["cohort"]["after"]["state"] == "partial-run"
    assert result["comparison_eligible"] == 1
    assert result["paired_metrics"]["source_recall"]["delta_pp"] == 25
    assert [case["id"] for case in result["cases"]] == ["B"]


def test_matching_observed_subset_is_never_claimed_full_without_dataset_manifest():
    result = compare_reports(report(), report())
    assert result["cohort"]["before"]["state"] == "same-observed-ids-but-fullness-unverified"
    assert any("full dataset" in warning for warning in result["warnings"])


@pytest.mark.parametrize("observed,selected,state", [
    (["A", "B"], ["A", "B"], "full-dataset"),
    (["A"], ["A"], "completed-subset"),
    (["A"], ["A", "B"], "partial-run"),
])
def test_known_selected_ids_distinguish_full_subset_and_interrupted_runs(observed, selected, state):
    before = report([row(identifier) for identifier in observed], selected_case_ids=selected, dataset_case_ids=["A", "B"])
    result = compare_reports(before, deepcopy(before))
    assert result["cohort"]["before"]["state"] == state


def test_mixed_recall_change_is_exposed_as_tradeoff_not_semantic_improvement():
    result = compare_reports(report([row("A", 0.25, 1)]), report([row("A", 0.75, 0.5)]))
    assert result["cases"][0]["status"] == "mixed"
    assert result["cases"][0]["metric_directions"] == {"source_recall": "better", "locator_recall": "worse"}
    assert "not factual correctness" in result["semantic_quality"]


def test_runtime_changes_are_explicit_and_prevent_causal_apples_to_apples_claim():
    before, after = report(), report(mode="corrective", runtime={"model": "another-model", "EMBED_MODEL": "another-embedding"})
    result = compare_reports(before, after)
    assert result["valid"]
    assert result["controlled_comparison"] is False
    assert {item["setting"] for item in result["configuration_differences"]} == {"model", "EMBED_MODEL"}
    assert result["runs"]["after"]["effective_answers_enabled"] is True
    assert any("latencies" in warning for warning in result["warnings"])


def test_implementation_changes_are_recorded_not_treated_as_changed_corpus():
    result = compare_reports(report(implementation_sha256={"rag_agent.py": "a" * 64}), report(implementation_sha256={"rag_agent.py": "b" * 64}))
    assert result["valid"]
    assert result["implementation_changed_files"] == ["rag_agent.py"]


def test_report_never_exports_credentials_answers_evidence_or_raw_exceptions():
    before = report([row("A", answer="DO_NOT_EXPORT_ANSWER", context=[{"text": "DO_NOT_EXPORT_EVIDENCE"}])])
    before["api_key"] = "DO_NOT_EXPORT_KEY"
    before["runtime"]["password"] = "DO_NOT_EXPORT_PASSWORD"
    before["runtime"]["authorization"] = "DO_NOT_EXPORT_AUTH"
    before["runtime"]["RAG_PLANNER_TOKEN_BUDGET"] = 1024
    after = deepcopy(before)
    after["runtime"]["RAG_PLANNER_TOKEN_BUDGET"] = 2048
    result = compare_reports(before, after)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "DO_NOT_EXPORT" not in serialized
    assert result["configuration_differences"] == [{"setting": "RAG_PLANNER_TOKEN_BUDGET", "before": 1024, "after": 2048}]


@pytest.mark.parametrize("value", [None, -1, 1.5, float("nan"), "0.5", True])
def test_invalid_or_absent_metrics_do_not_become_zero_or_perfect_scores(value):
    result = compare_reports(report([row("A", value)]), report())
    assert result["comparison_eligible"] == 0
    assert result["cases"][0]["status"] == "error"
    assert result["cases"][0]["reason"] == "missing-or-invalid-retrieval-metric"


def test_duplicate_case_ids_and_changed_question_fail_safely():
    assert compare_reports(report([row("A"), row("A")]), report())["valid"] is False
    after = report([row("A", question="Другой вопрос")])
    assert compare_reports(report(), after)["valid"] is False


def test_missing_latency_does_not_destroy_valid_recall_pairs():
    result = compare_reports(report([row("A", latency=None)]), report())
    assert result["comparison_eligible"] == 1
    assert result["paired_metrics"]["latency_s"]["eligible"] == 0
    assert result["paired_metrics"]["latency_s"]["before"]["p50"] is None


def test_all_observed_timings_include_errors_while_paired_recall_timings_do_not():
    before = report([row("A", latency=2), row("B", latency=100, error="synthetic timeout")])
    after = report([row("A", latency=4), row("B", latency=6)])
    result = compare_reports(before, after)
    assert result["runs"]["before"]["all_observed_latency_s"] == {"count": 2, "p50": 51, "p95": 95.1}
    assert result["paired_metrics"]["latency_s"]["before"]["p50"] == 2


@pytest.mark.parametrize("changes", [
    {"implementation_sha256": "not-a-mapping"}, {"source_manifest": "not-a-list"},
    {"dataset_sha256": "not-a-sha256"},
])
def test_malformed_metadata_yields_invalid_comparison_not_a_traceback(changes):
    result = compare_reports(report(), report(**changes))
    assert result["valid"] is False
    assert result["comparison_eligible"] == 0


def test_unknown_model_does_not_establish_controlled_comparison():
    result = compare_reports(report(runtime={}), report(runtime={}))
    assert result["valid"]
    assert result["controlled_comparison"] is False
    assert any("Model identity" in warning for warning in result["warnings"])


def test_rows_outside_declared_selection_are_not_eligible():
    before = report([row("Z")], selected_case_ids=["A"], dataset_case_ids=["A", "B"])
    result = compare_reports(before, deepcopy(before))
    assert result["valid"] is False
    assert result["comparison_eligible"] == 0


def test_cli_writes_comparison_and_returns_nonzero_for_invalid_pair(tmp_path):
    first, second, output = (tmp_path / name for name in ("before.json", "after.json", "comparison.json"))
    first.write_text(json.dumps(report()), encoding="utf-8")
    second.write_text(json.dumps(report()), encoding="utf-8")
    assert main(["--before", str(first), "--after", str(second), "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["comparison_eligible"] == 1
    second.write_text(json.dumps(report(scope="private-project")), encoding="utf-8")
    assert main(["--before", str(first), "--after", str(second), "--output", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8"))["valid"] is False


def test_invalid_json_output_contains_error_type_not_file_contents_or_secret(tmp_path):
    first, second, output = (tmp_path / name for name in ("before.json", "after.json", "comparison.json"))
    first.write_text('{"secret": "DO_NOT_EXPORT', encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    assert main(["--before", str(first), "--after", str(second), "--output", str(output)]) == 2
    assert "DO_NOT_EXPORT" not in output.read_text(encoding="utf-8")


def test_cli_refuses_to_overwrite_an_input_report(tmp_path):
    first, second = tmp_path / "before.json", tmp_path / "after.json"
    content = json.dumps(report())
    first.write_text(content, encoding="utf-8")
    second.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["--before", str(first), "--after", str(second), "--output", str(first)])
    assert first.read_text(encoding="utf-8") == content
