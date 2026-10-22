"""Offline preparation and serial source-reading/primary-writer A/B replay.

The baseline is the exact actual request of a fresh oracle_ablation preparation.
The candidate adds one bounded source-selection call and a validated guide,
retaining all original evidence. No retrieval, gold facts, review or repair.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals import oracle_ablation as oracle
from corrective_rag import generation_prompt
from source_reader import build_source_reading_prompt, read_sources, validated_writer_payload

AblationError = oracle.AblationError
SCHEMA_VERSION = 1
KIND = "same-actual-sources-reader-primary-ablation"
READER_TOKENS = 4096


def _need(condition, message):
    oracle._need(condition, message)


def implementation_hashes():
    return {**oracle.implementation_hashes(), **{
        name: oracle.digest((ROOT / name).read_bytes())
        for name in ("source_reader.py", "evals/source_reading_ablation.py")}}


def _fingerprint(value, field):
    return oracle.digest({key: item for key, item in value.items() if key != field})


def _request_info(body, evidence):
    prompt = body["messages"][0]["content"]
    return {"request": deepcopy(body), "request_sha256": oracle.digest(body),
            "prompt_sha256": oracle.text_digest(prompt), "prompt_chars": len(prompt),
            "request_json_chars": len(oracle.canonical(body)), "context_sha256": oracle.digest(evidence),
            "context_chars": sum(len(item["text"]) for item in evidence), "evidence_count": len(evidence)}


def _case_inputs(case):
    """Recover only prompt inputs, then require exact current prompt equality."""
    plan = oracle._plan(case["saved_plan"])
    request = case["baseline"]["request"]
    messages = request["messages"]
    _need(isinstance(messages, list) and messages and all(
        isinstance(row, dict) and set(row) == {"role", "content"}
        and row["role"] in {"user", "assistant", "system"} and isinstance(row["content"], str)
        for row in messages), "Unsupported frozen messages")
    prompt = messages[0]["content"]
    marker = prompt.rfind('{"question":')
    _need(marker >= 0, "Cannot recover frozen generation inputs")
    data, _ = json.JSONDecoder().raw_decode(prompt[marker:])
    _need(isinstance(data, dict) and isinstance(data.get("question"), str)
          and isinstance(data.get("evidence"), list) and data["evidence"], "Invalid frozen generation inputs")
    evidence = data["evidence"]
    for item in evidence:
        oracle._scope(item)
    limits = case.get("selection_limits", {})
    bare = generation_prompt(data["question"], plan, evidence, deepcopy(oracle.NEUTRAL_COVERAGE),
                             selection_limits=limits)
    _need(prompt.endswith(bare), "Frozen baseline differs from current generation prompt")
    prefix = prompt[:-len(bare)]
    _need(oracle.text_digest(prefix) == case["system_prompt_sha256"], "System prompt fingerprint mismatch")
    _need(oracle.digest(evidence) == case["context_sha256"], "Shared context fingerprint mismatch")
    return data["question"], plan, evidence, limits, prefix


def _candidate(case, reader_output, *, finish_reason="stop"):
    question, plan, evidence, limits, prefix = _case_inputs(case)
    # A syntactically complete object at the token boundary still cannot
    # establish that the reader finished selecting conditions and list items.
    usable_output = reader_output if finish_reason == "stop" else ""
    reading = read_sources(question, asdict(plan), evidence, lambda *_: usable_output, selection_limits=limits)
    accepted = validated_writer_payload(reading, question, asdict(plan), evidence, selection_limits=limits)
    prompt = generation_prompt(question, plan, evidence, deepcopy(oracle.NEUTRAL_COVERAGE), prefix,
                               selection_limits=limits, source_reading=reading)
    request = deepcopy(case["baseline"]["request"])
    request["messages"][0]["content"] = prompt
    _need("max_tokens" not in request and "max_completion_tokens" not in request, "Writer output must remain uncapped")
    info = _request_info(request, evidence)
    info["guide"] = {"applied": accepted is not None,
                     "fallback_reason": None if accepted is not None else (
                         "reader-finish-not-stop" if finish_reason != "stop" else
                         "reader-unassessed" if not reading.assessed else "writer-guide-budget-or-validation"),
                     "reader_finish_reason": finish_reason,
                     "reading_sha256": oracle.digest(reading.to_dict()),
                     "writer_guide_sha256": oracle.digest(accepted) if accepted is not None else None,
                     "reader_warnings": list(reading.warnings)}
    return info, reading.to_dict()


def prepare(oracle_prepared, *, ids=None):
    """Freeze actual-only A/B inputs from a fresh oracle preparation, offline."""
    if isinstance(oracle_prepared, (str, Path)):
        source_path = Path(oracle_prepared).resolve()
        raw = source_path.read_bytes()
        original = json.loads(raw.decode("utf-8-sig"))
    else:
        # oracle.prepare returns dataclass tuple fields in memory; a saved
        # preparation is JSON lists. Normalize both APIs to that same form.
        source_path, raw, original = None, None, json.loads(oracle.canonical(oracle_prepared))
    oracle._validate_prepared(original)
    runtime = original["runtime"]
    _need(runtime["temperature"] == 0 and runtime["seed"] == 42 and runtime["thinking"] is False
          and runtime["max_output_tokens"] == 0, "Require temperature 0, seed 42, thinking false and uncapped writer")
    requested = set(ids) if ids is not None else {case["id"] for case in original["cases"]}
    _need(requested and requested <= {case["id"] for case in original["cases"]}, "Unknown or empty selected case IDs")
    result = {"schema_version": SCHEMA_VERSION, "kind": KIND,
              "source_preparation_sha256": original["preparation_sha256"],
              "scope": original["scope"], "normalization_policy": original["normalization_policy"],
              "input_paths": deepcopy(original["input_paths"]), "input_sha256": deepcopy(original["input_sha256"]),
              "implementation_sha256": implementation_hashes(), "runtime": deepcopy(runtime),
              "runtime_sha256": original["runtime_sha256"], "model_sha256": original["model_sha256"],
              "source_manifest_sha256": original["source_manifest_sha256"],
              "identity_verification": original["identity_verification"],
              "coverage": deepcopy(oracle.NEUTRAL_COVERAGE),
              "calls_per_case": {"baseline_writer": 1, "candidate_reader": 1, "candidate_writer": 1,
                                 "planning": 0, "review": 0, "repair": 0},
              "reader_settings": {"max_tokens": READER_TOKENS, "temperature": 0, "seed": 42, "thinking": False},
              "budgets": deepcopy(original["budgets"]),
              "history_policy": "All frozen request messages and exact question retained; no history reconstructed",
              "oracle_facts_used": False, "archived_answers_used": False, "cases": []}
    if source_path:
        result["input_paths"]["oracle_prepared"] = str(source_path)
        result["input_sha256"]["oracle_prepared"] = oracle.digest(raw)
    for source_case in original["cases"]:
        if source_case["id"] not in requested:
            continue
        actual = source_case["arms"]["actual"]
        prompt = actual["request"]["messages"][0]["content"]
        data, _ = json.JSONDecoder().raw_decode(prompt[prompt.rfind('{"question":'):])
        evidence = data["evidence"]
        case = {"id": source_case["id"], "saved_plan": deepcopy(source_case["saved_plan"]),
                "selection_limits": deepcopy(actual.get("selection_limits", {})),
                "system_prompt_sha256": original["system_prompt_sha256"],
                "context_sha256": oracle.digest(evidence),
                "original_actual_context_sha256": actual["context_sha256"],
                "baseline": _request_info(actual["request"], evidence)}
        question, plan, evidence, limits, _ = _case_inputs(case)
        reader_prompt = build_source_reading_prompt(question, asdict(plan), evidence, limits)
        reader_request = deepcopy(actual["request"])
        reader_request["messages"][0]["content"] = reader_prompt
        reader_request["max_tokens"] = READER_TOKENS
        case["reader"] = _request_info(reader_request, evidence)
        case["question_sha256"] = oracle.text_digest(question)
        case["plan_sha256"] = oracle.digest(asdict(plan))
        case["execution_order"] = (["baseline", "candidate"] if int(oracle.text_digest(case["id"])[0], 16) % 2 == 0
                                   else ["candidate", "baseline"])
        case["pair_sha256"] = _fingerprint(case, "pair_sha256")
        result["cases"].append(case)
    result["preparation_sha256"] = _fingerprint(result, "preparation_sha256")
    _validate_prepared(result)
    return result


def _validate_prepared(prepared):
    _need(isinstance(prepared, dict) and prepared.get("schema_version") == SCHEMA_VERSION
          and prepared.get("kind") == KIND, "Invalid source-reading preparation")
    _need(prepared.get("preparation_sha256") == _fingerprint(prepared, "preparation_sha256"), "Preparation fingerprint mismatch")
    _need(prepared["implementation_sha256"] == implementation_hashes(), "Implementation changed since preparation")
    _need(set(prepared["input_paths"]) == set(prepared["input_sha256"]), "Input fingerprint keys differ")
    for name, path in prepared["input_paths"].items():
        _need(oracle.digest(Path(path).read_bytes()) == prepared["input_sha256"][name], "Frozen input data changed")
    _need(prepared.get("cases") and len({case["id"] for case in prepared["cases"]}) == len(prepared["cases"]), "Invalid case IDs")
    for case in prepared["cases"]:
        _need(case["pair_sha256"] == _fingerprint(case, "pair_sha256"), "Case fingerprint mismatch")
        _need(sorted(case["execution_order"]) == ["baseline", "candidate"], "Invalid arm order")
        question, plan, evidence, limits, _ = _case_inputs(case)
        _need(case["baseline"] == _request_info(case["baseline"]["request"], evidence), "Baseline request fingerprint mismatch")
        request = deepcopy(case["baseline"]["request"])
        _need(request["model"] == prepared["runtime"]["model"] and request.get("temperature") == 0
              and request.get("seed") == 42 and request.get("stream") is False
              and request.get("chat_template_kwargs") == {"enable_thinking": False}
              and "max_tokens" not in request and "max_completion_tokens" not in request, "Invalid frozen writer settings")
        reader_request = deepcopy(case["baseline"]["request"])
        reader_request["messages"][0]["content"] = build_source_reading_prompt(question, asdict(plan), evidence, limits)
        reader_request["max_tokens"] = READER_TOKENS
        _need(case["reader"] == _request_info(reader_request, evidence), "Reader request fingerprint mismatch")
        _need(case["question_sha256"] == oracle.text_digest(question)
              and case["plan_sha256"] == oracle.digest(asdict(plan)), "Task fingerprint mismatch")


def _checkpoint(output, report, *, exclusive=False):
    report["report_sha256"] = _fingerprint(report, "report_sha256")
    oracle._write_json(output, report, exclusive=exclusive)


def _entry_info(case, arm, stage, info):
    return {"id": case["id"], "arm": arm, "stage": stage,
            "pair_sha256": case["pair_sha256"], "question_sha256": case["question_sha256"],
            "plan_sha256": case["plan_sha256"],
            **{key: deepcopy(value) for key, value in info.items() if key != "request"}}


def _validate_report(report, prepared, fingerprint):
    _need(report.get("run_sha256") == fingerprint and report.get("preparation_sha256") == prepared["preparation_sha256"],
          "Resume fingerprints differ")
    _need(report.get("report_sha256") == _fingerprint(report, "report_sha256"), "Result checkpoint fingerprint mismatch")
    by_case = {case["id"]: case for case in prepared["cases"]}
    completed = {}
    for entry in report.get("results", []):
        key = (entry.get("id"), entry.get("arm"), entry.get("stage"))
        _need(key[0] in by_case and key[1:] in {("baseline", "writer"), ("candidate", "reader"), ("candidate", "writer")}
              and key not in completed, "Unexpected or duplicate resumed stage")
        _need(entry.get("status") == "completed", "Unfinished/failed attempt cannot be retried by resume")
        _need(entry.get("output_sha256") == oracle.text_digest(entry.get("output", "")), "Resumed output fingerprint mismatch")
        completed[key] = entry
    for key, entry in completed.items():
        case = by_case[key[0]]
        if key[1:] == ("baseline", "writer"):
            info = case["baseline"]
        elif key[2] == "reader":
            info = case["reader"]
        else:
            reader = completed.get((key[0], "candidate", "reader"))
            _need(reader is not None, "Candidate writer has no completed reader stage")
            info, reading = _candidate(case, reader["output"], finish_reason=reader["finish_reason"])
            _need("reading" not in reader or reader["reading"] == reading, "Saved reading differs from current resolution")
        expected = _entry_info(case, key[1], key[2], info)
        _need(all(entry.get(field) == value for field, value in expected.items()), "Resumed stage request fingerprint mismatch")
    return completed


def run(prepared, output, *, endpoint, api_key="", timeout=900, resume=False):
    """Run three serial stages per case; an ambiguous attempt is never retried."""
    _validate_prepared(prepared)
    _need(type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0, "Invalid timeout")
    endpoint = oracle._endpoint(endpoint)
    output = oracle._output_guard(output, prepared["input_paths"].values())
    transport = {"endpoint_sha256": oracle.text_digest(endpoint), "timeout_s": timeout,
                 "auth_present": bool(api_key), "retry_count": 0, "concurrency": 1}
    fingerprint = oracle.digest({"preparation": prepared["preparation_sha256"], "transport": transport})
    with oracle._run_lock(output):
        if output.exists():
            _need(resume, "Output exists; explicit --resume is required")
            report = json.loads(output.read_text(encoding="utf-8"))
            completed = _validate_report(report, prepared, fingerprint)
        else:
            _need(not resume, "Resume output does not exist")
            report = {"schema_version": SCHEMA_VERSION, "kind": KIND,
                      "preparation_sha256": prepared["preparation_sha256"], "run_sha256": fingerprint,
                      "transport": transport, "started_at": datetime.now(timezone.utc).isoformat(),
                      "comparison": "same complete actual evidence; source-selection plus primary writer; no semantic scores",
                      "provenance": {key: deepcopy(value) for key, value in prepared.items()
                                     if key not in {"cases", "preparation_sha256", "input_paths"}}, "results": []}
            completed = {}
            _checkpoint(output, report, exclusive=True)

        def dispatch(case, arm, stage, info):
            key = case["id"], arm, stage
            if key in completed:
                return completed[key]
            _validate_prepared(prepared)  # source/code drift must stop before dispatch
            budget = prepared["budgets"]["prompt_and_request_json_chars"]
            _need(info["prompt_chars"] <= budget and info["request_json_chars"] <= budget,
                  "Full stage prompt/request budget exceeded; no truncation permitted")
            entry = {**_entry_info(case, arm, stage, info), "status": "started",
                     "started_at": datetime.now(timezone.utc).isoformat(),
                     "requested_model": info["request"]["model"],
                     "requested_max_tokens": info["request"].get("max_tokens"), "timeout_s": timeout}
            report["results"].append(entry)
            _checkpoint(output, report)
            started = time.perf_counter()
            try:
                result = oracle.chat_completion(endpoint, info["request"], api_key, timeout)
                _need(not api_key or api_key not in oracle.canonical(result), "Credential appeared in completion response")
                entry.update(result, status="completed")
                if stage == "writer":
                    entry["writer_output_truncated"] = result["finish_reason"] == "length"
                else:
                    entry["reader_output_unavailable"] = result["finish_reason"] != "stop"
            except Exception as error:
                entry.update(status="failed", error_type=type(error).__name__,
                             elapsed_s=round(time.perf_counter() - started, 6))
                _checkpoint(output, report)
                raise AblationError("Stage attempt failed; saved without sensitive diagnostics; no retry") from None
            entry["elapsed_s"] = round(time.perf_counter() - started, 6)
            _checkpoint(output, report)
            completed[key] = entry
            return entry

        for case in prepared["cases"]:
            for arm in case["execution_order"]:
                if arm == "baseline":
                    dispatch(case, arm, "writer", case["baseline"])
                    continue
                reader = dispatch(case, arm, "reader", case["reader"])
                candidate, reading = _candidate(case, reader["output"], finish_reason=reader["finish_reason"])
                reader["reading"] = reading
                _checkpoint(output, report)
                dispatch(case, arm, "writer", candidate)
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        _checkpoint(output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Offline actual-only preparation from a fresh oracle manifest")
    prep.add_argument("--oracle-prepared", type=Path, required=True)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--ids", nargs="+")
    execute = commands.add_parser("run", help="Opt-in sequential reader/writer requests; no automatic retries")
    execute.add_argument("--prepared", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--endpoint", default=os.getenv("SOURCE_READING_ABLATION_ENDPOINT"))
    execute.add_argument("--api-key-env", default="SOURCE_READING_ABLATION_API_KEY")
    execute.add_argument("--timeout", type=float, default=900)
    execute.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            output = oracle._output_guard(args.output, [args.oracle_prepared])
            _need(not output.exists(), "Preparation output already exists")
            result = prepare(args.oracle_prepared, ids=args.ids)
            output = oracle._output_guard(output, result["input_paths"].values())
            oracle._write_json(output, result, exclusive=True)
            print(json.dumps({"status": "prepared", "cases": len(result["cases"]), "model_calls": 0}))
        else:
            _need(bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env)), "Invalid API key environment variable name")
            output = oracle._output_guard(args.output, [args.prepared])
            result = run(json.loads(args.prepared.read_text(encoding="utf-8")), output, endpoint=args.endpoint,
                         api_key=os.getenv(args.api_key_env, ""), timeout=args.timeout, resume=args.resume)
            print(json.dumps({"status": "completed", "stages": len(result["results"])}))
        return 0
    except AblationError as error:
        print("Source-reading ablation failed closed: " + str(error), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        print("Source-reading ablation failed closed; inspect schemas, budgets and fingerprints.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
