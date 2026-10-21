"""Offline paired retrieval comparison; recall never certifies answer quality.

Only aggregate metrics and allowlisted runtime settings are exported. Answers,
evidence, raw exceptions, prompts and credentials are not copied to the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


_METRICS = ("source_recall", "locator_recall")
_PUBLIC_SETTINGS = frozenset((
    "model", "provider", "temperature", "seed", "top_k", "max_tokens", "max_completion_tokens",
    "QDRANT_COLLECTION", "EMBED_MODEL", "RAG_RERANKER_MODE", "RAG_EVIDENCE_MAX_CHARS",
    "RAG_PLANNER_TOKEN_BUDGET", "RAG_PLANNER_THINKING", "RAG_PLANNER_TIMEOUT_SECONDS",
    "RAG_MAX_ADDITIONAL_SEARCH_ROUNDS", "RAG_CORRECTIVE_PIPELINE", "RAG_MAX_OUTPUT_TOKENS",
    "RAG_REFERENCE_EXPANSION",
))
_HASH_FIELDS = ("dataset_sha256", "source_manifest_sha256", "corpus_sha256", "corpus_hash")
_SEMANTIC_NOTICE = "not assessed: source/locator recall measures retrieval coverage, not factual correctness, completeness or answer quality"


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _case_map(report: dict, label: str, errors: list[str]) -> dict[str, dict]:
    rows = report.get("cases")
    if not isinstance(rows, list):
        errors.append(label + ": cases must be a list")
        return {}
    result = {}
    for number, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            errors.append(f"{label}: case at position {number} has no valid string ID")
            continue
        identifier = row["id"].strip()
        if identifier in result:
            errors.append(label + ": duplicate case IDs")
        result[identifier] = row
    return result


def _error(row: dict) -> bool:
    return bool(row.get("error") or _retrieval(row).get("error"))


def _retrieval(row: dict) -> dict:
    value = row.get("retrieval")
    return value if isinstance(value, dict) else {}


def _number(value: Any, *, recall: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or (recall and value > 1):
        return None
    return float(value)


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    left, right = math.floor(position), math.ceil(position)
    return round(ordered[left] + (ordered[right] - ordered[left]) * (position - left), 6)


def _latency(row: dict) -> float | None:
    return _number(row.get("latency_s", _retrieval(row).get("latency_s")))


def _manifest(report: dict) -> str | None:
    value = report.get("source_manifest")
    if not isinstance(value, list):
        return None
    # Compare full records, including IDs/revisions/content hashes, ignoring only
    # list order. Source details themselves never appear in comparison output.
    return _digest(sorted((_digest(row) for row in value)))


def _runtime(report: dict) -> dict:
    values = report.get("runtime") or {}
    return {key: value for key, value in values.items() if key in _PUBLIC_SETTINGS
            and (value is None or isinstance(value, (str, bool, int)) or isinstance(value, float) and math.isfinite(value))} if isinstance(values, dict) else {}


def _cohort_state(report: dict, observed: set[str]) -> dict:
    selected = report.get("selected_case_ids")
    dataset = report.get("dataset_case_ids")
    selected_ids = {str(value) for value in selected} if isinstance(selected, list) else None
    dataset_ids = {str(value) for value in dataset} if isinstance(dataset, list) else None
    dataset_count = report.get("dataset_case_count")
    if dataset_ids is not None:
        dataset_count = len(dataset_ids)
    planned = selected_ids if selected_ids is not None else dataset_ids
    missing = sorted(planned - observed) if planned is not None else []
    unexpected = sorted(observed - planned) if planned is not None else []
    if missing:
        state = "partial-run"
    elif unexpected:
        state = "unexpected-case-ids"
    elif dataset_ids is not None:
        state = "full-dataset" if observed == dataset_ids else "completed-subset"
    elif isinstance(dataset_count, int) and not isinstance(dataset_count, bool):
        state = "full-dataset" if len(observed) == dataset_count else "completed-subset" if planned is not None else "partial-or-subset"
    else:
        state = "same-observed-ids-but-fullness-unverified" if planned is None else "selected-run-complete-dataset-size-unknown"
    return {"state": state, "dataset_case_count": dataset_count if isinstance(dataset_count, int) else None,
            "selected_count": len(planned) if planned is not None else None,
            "missing_selected_ids": missing, "unexpected_ids": unexpected}


def _run_summary(report: dict, rows: dict[str, dict]) -> dict:
    failed = sum(_error(row) for row in rows.values())
    mode = report.get("mode") if report.get("mode") in ("legacy", "coverage", "corrective") else None
    count = report.get("source_count")
    times = [value for row in rows.values() if (value := _latency(row)) is not None]
    return {"mode": mode, "answers_enabled": bool(report.get("answers_enabled")),
            "effective_answers_enabled": report.get("mode") == "corrective" or bool(report.get("answers_enabled")),
            "observed": len(rows), "completed": len(rows) - failed, "errors": failed,
            "all_observed_latency_s": {"count": len(times), "p50": _percentile(times, 0.5), "p95": _percentile(times, 0.95)},
            "source_count": count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else None,
            "runtime": _runtime(report)}


def compare_reports(before: dict, after: dict) -> dict:
    """Compare matched completed IDs, rejecting different scopes/corpora/data.

    A valid comparison may still be partial or experimentally confounded; these
    properties are reported separately rather than hidden behind mean recall.
    """
    errors, warnings = [], []
    before_rows, after_rows = _case_map(before, "before", errors), _case_map(after, "after", errors)
    if not before.get("scope") or not after.get("scope"):
        errors.append("Both reports must declare a retrieval scope")
    elif before["scope"] != after["scope"]:
        errors.append("Retrieval scopes differ")
    for key in _HASH_FIELDS:
        first, second = before.get(key), after.get(key)
        if key.endswith("sha256") and any(value is not None and not (
            isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value)) for value in (first, second)):
            errors.append(key + " must be a SHA-256 hex digest when supplied")
        if first is not None and second is not None and first != second:
            errors.append(key + " mismatch")
    before_manifest, after_manifest = _manifest(before), _manifest(after)
    if any(report.get("source_manifest") is not None and not isinstance(report.get("source_manifest"), list) for report in (before, after)):
        errors.append("source_manifest must be a list when supplied")
    if before_manifest is not None and after_manifest is not None and before_manifest != after_manifest:
        errors.append("source_manifest mismatch: registered sources, versions or content hashes differ")
    dataset_verified = bool(before.get("dataset_sha256") and before.get("dataset_sha256") == after.get("dataset_sha256"))
    corpus_verified = before_manifest is not None and before_manifest == after_manifest
    if not dataset_verified:
        warnings.append("Dataset identity is unverified: both matching dataset hashes were not supplied")
    if not corpus_verified:
        warnings.append("Corpus identity is unverified: both matching source manifests were not supplied")
    counts_match = before.get("source_count") == after.get("source_count")
    if not counts_match:
        warnings.append("Source counts differ; counts alone do not establish equal document contents")

    first_ids, second_ids = set(before_rows), set(after_rows)
    shared = sorted(first_ids & second_ids)
    missing_before, missing_after = sorted(second_ids - first_ids), sorted(first_ids - second_ids)
    if missing_before or missing_after:
        warnings.append("Partial ID overlap: aggregate changes use only matched completed cases")
    for identifier in shared:
        a, b = before_rows[identifier], after_rows[identifier]
        if a.get("question") is not None and b.get("question") is not None and a["question"] != b["question"]:
            errors.append("Matched case questions differ")
            break

    summaries = {"before": _run_summary(before, before_rows), "after": _run_summary(after, after_rows)}
    first_runtime, second_runtime = summaries["before"]["runtime"], summaries["after"]["runtime"]
    configuration_differences = [{"setting": key, "before": first_runtime.get(key), "after": second_runtime.get(key)}
                                 for key in sorted(set(first_runtime) | set(second_runtime))
                                 if first_runtime.get(key) != second_runtime.get(key)]
    if configuration_differences:
        warnings.append("Model/runtime parameters differ: do not attribute metric changes solely to the pipeline")
    models_declared = all(value.get("model") not in (None, "", "unknown") for value in (first_runtime, second_runtime))
    if not models_declared:
        warnings.append("Model identity is missing or unknown; the comparison is not experimentally controlled")
    answer_modes_differ = summaries["before"]["effective_answers_enabled"] != summaries["after"]["effective_answers_enabled"]
    if answer_modes_differ:
        warnings.append("Answer generation differs: end-to-end latencies are not an apples-to-apples timing comparison")
    first_code, second_code = before.get("implementation_sha256") or {}, after.get("implementation_sha256") or {}
    if not isinstance(first_code, dict) or not isinstance(second_code, dict):
        errors.append("implementation_sha256 must be a mapping when supplied")
        first_code, second_code = {}, {}
    implementation_differences = sorted(key for key in set(first_code) | set(second_code) if first_code.get(key) != second_code.get(key))

    cohort = {"aligned": len(shared), "missing_from_before": missing_before, "missing_from_after": missing_after,
              "before": _cohort_state(before, first_ids), "after": _cohort_state(after, second_ids)}
    if any(cohort[label]["unexpected_ids"] for label in ("before", "after")):
        errors.append("Observed case IDs are outside the declared selected/dataset IDs")
    if any(value["state"] != "full-dataset" for value in (cohort["before"], cohort["after"])):
        warnings.append("This pair does not establish completed coverage of the full dataset; inspect cohort states")
    result = {"schema_version": 1, "valid": not errors, "validation_errors": errors, "warnings": warnings,
              "semantic_quality": _SEMANTIC_NOTICE, "classification": "Pareto direction of source/locator recall; mixed means a trade-off, not unchanged",
              "controlled_comparison": not errors and dataset_verified and corpus_verified and models_declared and counts_match and not configuration_differences and not answer_modes_differ,
              "comparison_limits": "Recorded metadata only; absence of recorded confounders does not prove a controlled experiment",
              "configuration_differences": configuration_differences,
              "implementation_changed_files": implementation_differences,
              "cohort": cohort, "runs": summaries, "comparison_eligible": 0, "cases": [], "paired_metrics": {}}
    if errors:
        return result

    eligible, per_case = [], []
    for identifier in shared:
        first, second = before_rows[identifier], after_rows[identifier]
        row = {"id": identifier, "status": "error", "comparison_eligible": False,
               "before_error": _error(first), "after_error": _error(second)}
        if row["before_error"] or row["after_error"]:
            row["reason"] = "execution-error"
            row["error_transition"] = "both" if row["before_error"] and row["after_error"] else "introduced" if row["after_error"] else "resolved"
        else:
            a = {key: _number(_retrieval(first).get(key), recall=True) for key in _METRICS}
            b = {key: _number(_retrieval(second).get(key), recall=True) for key in _METRICS}
            if any(value is None for value in (*a.values(), *b.values())):
                row["reason"] = "missing-or-invalid-retrieval-metric"
            else:
                deltas = {key: round(b[key] - a[key], 8) for key in _METRICS}
                directions = {key: "better" if value > 0 else "worse" if value < 0 else "unchanged" for key, value in deltas.items()}
                signs = set(directions.values()) - {"unchanged"}
                row.update(status=next(iter(signs)) if len(signs) == 1 else "mixed" if signs else "unchanged",
                           comparison_eligible=True, before=a, after=b, delta_pp={key: round(value * 100, 6) for key, value in deltas.items()},
                           metric_directions=directions, before_latency_s=_latency(first), after_latency_s=_latency(second))
                eligible.append(row)
        per_case.append(row)
    paired = {}
    for key in _METRICS:
        a = mean(row["before"][key] for row in eligible) if eligible else None
        b = mean(row["after"][key] for row in eligible) if eligible else None
        paired[key] = {"before": round(a, 6) if a is not None else None,
                       "after": round(b, 6) if b is not None else None,
                       "delta_pp": round((b - a) * 100, 6) if a is not None else None}
    latency_rows = [row for row in eligible if row["before_latency_s"] is not None and row["after_latency_s"] is not None]
    paired["latency_s"] = {"eligible": len(latency_rows), "percentile_method": "linear interpolation at (n-1)*q",
                           **{label: {"p50": _percentile([row[label + "_latency_s"] for row in latency_rows], 0.5),
                                      "p95": _percentile([row[label + "_latency_s"] for row in latency_rows], 0.95)}
                              for label in ("before", "after")}}
    result.update(comparison_eligible=len(eligible), cases=per_case, paired_metrics=paired,
                  outcomes=dict(Counter(row["status"] for row in per_case)))
    if not eligible:
        result["controlled_comparison"] = False
        warnings.append("No matched completed cases with valid retrieval metrics are eligible")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.resolve() in (args.before.resolve(), args.after.resolve()):
        parser.error("--output must differ from both input reports")
    try:
        before, after = (json.loads(path.read_text(encoding="utf-8-sig")) for path in (args.before, args.after))
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("Reports must be JSON objects")
        report = compare_reports(before, after)
    except (OSError, ValueError, TypeError) as exc:
        report = {"valid": False, "validation_errors": ["Cannot read valid report objects: " + type(exc).__name__], "comparison_eligible": 0}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"valid": report["valid"], "comparison_eligible": report["comparison_eligible"]}))
    return 0 if report["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
