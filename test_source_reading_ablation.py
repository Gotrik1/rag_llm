"""Mocked causal replay, integrity and at-most-once request contracts."""
from copy import deepcopy
import json

import pytest

from evals import oracle_ablation as oracle
from evals import source_reading_ablation as replay
from test_oracle_ablation import completion, frozen, payload


def reader_answer(body):
    prompt = body["messages"][0]["content"]
    data = json.loads(prompt.split("ДАННЫЕ ЧТЕНИЯ:\n", 1)[1])
    return json.dumps({"snapshot_id": data["snapshot_id"], "cards": [
        {"id": "C1", "kind": "rule", "passage_ids": [data["evidence"][0]["passages"][0]["passage_id"]],
         "related_card_ids": [], "requirement_ids": ["R1"]}], "unresolved_requirement_ids": []})


def transport(calls, output=None):
    def complete(endpoint, body, api_key, timeout):
        calls.append(deepcopy(body))
        response = output(body) if output else (reader_answer(body) if body.get("max_tokens") == 4096 else "Primary [E1]")
        return completion(body, response)
    return complete


def test_prepare_is_offline_and_exact_actual_baseline_has_no_oracle_gold(frozen, monkeypatch):
    monkeypatch.setattr(oracle, "chat_completion", lambda *_: pytest.fail("Preparation must remain offline"))
    original = frozen["prepare"](system_prompt="SYSTEM PREFIX\n")
    before = deepcopy(original)
    result = replay.prepare(original, ids=["case-a"])
    assert len(result["cases"]) == 1 and result["cases"][0]["id"] == "case-a"
    case = result["cases"][0]
    source = next(row for row in original["cases"] if row["id"] == "case-a")
    assert case["baseline"]["request"] == source["arms"]["actual"]["request"]
    assert case["baseline"]["request_sha256"] == source["arms"]["actual"]["request_sha256"]
    assert case["reader"]["request"]["max_tokens"] == 4096
    assert case["reader"]["request"]["temperature"] == 0
    assert case["reader"]["request"]["seed"] == 42
    assert case["reader"]["request"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert result["oracle_facts_used"] is False and result["archived_answers_used"] is False
    serialized = oracle.canonical(result)
    assert all(value not in serialized for value in (
        "RUBRIC_MUST_NOT", "FORBIDDEN_CLAIM_MUST_NOT", "SCORING_RUBRIC_MUST_NOT", "ARCHIVED_REPAIRED_ANSWER"))
    assert original == before
    assert {"source_reader.py", "evals/source_reading_ablation.py", "corrective_rag.py",
            "evals/oracle_ablation.py"} <= set(result["implementation_sha256"])


def test_run_calls_serial_stages_preserving_full_context_and_uncapped_writer(frozen, tmp_path, monkeypatch):
    prepared, calls = replay.prepare(frozen["prepare"]()), []
    output = tmp_path / "results.json"
    active = 0
    complete = transport(calls)
    def tracked(*args):
        nonlocal active
        active += 1
        assert active == 1
        result = complete(*args)
        active -= 1
        saved = json.loads(output.read_text())
        assert saved["results"][-1]["status"] == "started"
        return result
    monkeypatch.setattr(oracle, "chat_completion", tracked)
    report = replay.run(prepared, output, endpoint="http://127.0.0.1:18084/v1")
    assert len(calls) == 6 and len(report["results"]) == 6
    expected_order = [(case["id"], arm, stage) for case in prepared["cases"] for arm in case["execution_order"]
                      for stage in (["writer"] if arm == "baseline" else ["reader", "writer"])]
    assert [(row["id"], row["arm"], row["stage"]) for row in report["results"]] == expected_order
    for case in prepared["cases"]:
        entries = [row for row in report["results"] if row["id"] == case["id"]]
        assert {row["context_sha256"] for row in entries} == {case["context_sha256"]}
        baseline = payload(case["baseline"])
        candidate_index = next(i for i, row in enumerate(report["results"])
                               if (row["id"], row["arm"], row["stage"]) == (case["id"], "candidate", "writer"))
        candidate_body = calls[candidate_index]
        candidate = payload({"request": candidate_body})
        for key in ("question", "canonical_question", "requirements", "exclusions", "evidence", "coverage", "native_formulae"):
            assert candidate[key] == baseline[key]
        assert "source_reading" in candidate
        assert "max_tokens" not in candidate_body and "max_completion_tokens" not in candidate_body
        assert entries[-1]["usage"]["total_tokens"] == 20
    writer_entries = [row for row in report["results"] if row["arm"] == "candidate" and row["stage"] == "writer"]
    assert all(row["guide"]["applied"] for row in writer_entries)
    assert all(row["status"] == "completed" and row["finish_reason"] == "stop" and row["elapsed_s"] >= 0 for row in report["results"])
    assert report["report_sha256"] == replay._fingerprint(report, "report_sha256")
    replay.run(prepared, output, endpoint="http://127.0.0.1:18084/v1", resume=True)
    assert len(calls) == 6


def test_malformed_reader_falls_back_to_exact_baseline_without_retry(frozen, tmp_path, monkeypatch):
    prepared, calls = replay.prepare(frozen["prepare"](), ids=["case-a"]), []
    monkeypatch.setattr(oracle, "chat_completion", transport(calls, lambda body: "malformed JSON" if body.get("max_tokens") else "Primary"))
    report = replay.run(prepared, tmp_path / "fallback.json", endpoint="http://localhost/v1")
    assert len(calls) == 3
    writer = next(row for row in report["results"] if row["arm"] == "candidate" and row["stage"] == "writer")
    assert writer["guide"]["applied"] is False
    assert writer["guide"]["fallback_reason"] == "reader-unassessed"
    assert writer["request_sha256"] == prepared["cases"][0]["baseline"]["request_sha256"]
    assert next(row for row in report["results"] if row["stage"] == "reader")["reading"]["quoted_passages"] == {}


@pytest.mark.parametrize("finish_reason", ["length", "content_filter"])
def test_parseable_reader_json_without_stop_is_unavailable_and_never_promoted(frozen, tmp_path, monkeypatch, finish_reason):
    prepared = replay.prepare(frozen["prepare"](), ids=["case-a"])
    calls = []
    def complete(endpoint, body, key, timeout):
        calls.append(body)
        result = completion(body, reader_answer(body) if body.get("max_tokens") else "Primary")
        if body.get("max_tokens"):
            result["finish_reason"] = finish_reason
        return result
    monkeypatch.setattr(oracle, "chat_completion", complete)
    output = tmp_path / "truncated-reader.json"
    report = replay.run(prepared, output, endpoint="http://localhost/v1", timeout=120)
    reader = next(row for row in report["results"] if row["stage"] == "reader")
    writer = next(row for row in report["results"] if row["arm"] == "candidate" and row["stage"] == "writer")
    assert json.loads(reader["output"])["cards"]
    assert reader["reading"]["assessed"] is False and reader["reader_output_unavailable"] is True
    assert writer["guide"]["applied"] is False and writer["guide"]["fallback_reason"] == "reader-finish-not-stop"
    assert writer["request_sha256"] == prepared["cases"][0]["baseline"]["request_sha256"]
    assert all(row["timeout_s"] == 120 for row in report["results"])
    replay.run(prepared, output, endpoint="http://localhost/v1", timeout=120, resume=True)
    assert len(calls) == 3


def test_writer_length_is_recorded_as_truncated_without_semantic_pass(frozen, tmp_path, monkeypatch):
    prepared = replay.prepare(frozen["prepare"](), ids=["case-a"])
    def complete(endpoint, body, key, timeout):
        result = completion(body, reader_answer(body) if body.get("max_tokens") else "Incomplete primary")
        if not body.get("max_tokens"):
            result["finish_reason"] = "length"
        return result
    monkeypatch.setattr(oracle, "chat_completion", complete)
    report = replay.run(prepared, tmp_path / "writer-length.json", endpoint="http://localhost/v1")
    assert all(row["writer_output_truncated"] for row in report["results"] if row["stage"] == "writer")
    assert "no semantic scores" in report["comparison"]
    assert "PASS" not in oracle.canonical(report)


def test_oversized_valid_guide_falls_back_atomically(frozen, tmp_path, monkeypatch):
    frozen["archive"]["cases"][0]["context"][0]["text"] = "Required condition. " * 520
    frozen["save"]("archive", frozen["archive"])
    prepared = replay.prepare(frozen["prepare"](), ids=["case-b"])
    calls = []
    def output(body):
        if not body.get("max_tokens"):
            return "Primary"
        data = json.loads(body["messages"][0]["content"].split("ДАННЫЕ ЧТЕНИЯ:\n", 1)[1])
        selected = [row["passage_id"] for row in data["evidence"][0]["passages"]]
        return json.dumps({"snapshot_id": data["snapshot_id"], "cards": [{"id": "C1", "kind": "condition",
            "passage_ids": selected, "related_card_ids": [], "requirement_ids": ["R1"]}], "unresolved_requirement_ids": []})
    monkeypatch.setattr(oracle, "chat_completion", transport(calls, output))
    report = replay.run(prepared, tmp_path / "large.json", endpoint="http://localhost/v1")
    reader = next(row for row in report["results"] if row["stage"] == "reader")
    writer = next(row for row in report["results"] if row["arm"] == "candidate" and row["stage"] == "writer")
    assert reader["reading"]["assessed"] is True
    assert writer["guide"]["applied"] is False
    assert writer["guide"]["fallback_reason"] == "writer-guide-budget-or-validation"
    assert writer["request_sha256"] == prepared["cases"][0]["baseline"]["request_sha256"]


@pytest.mark.parametrize("failure_stage", ["reader", "writer"])
def test_ambiguous_interrupt_has_durable_marker_and_cannot_resume(frozen, tmp_path, monkeypatch, failure_stage):
    prepared, calls = replay.prepare(frozen["prepare"](), ids=["case-a"]), []
    def interrupt(endpoint, body, key, timeout):
        calls.append(body)
        if (body.get("max_tokens") == 4096) == (failure_stage == "reader"):
            raise KeyboardInterrupt()
        return completion(body, reader_answer(body))
    monkeypatch.setattr(oracle, "chat_completion", interrupt)
    output = tmp_path / "interrupted.json"
    with pytest.raises(KeyboardInterrupt):
        replay.run(prepared, output, endpoint="http://localhost/v1")
    saved = json.loads(output.read_text())
    assert saved["results"][-1]["status"] == "started"
    count = len(calls)
    with pytest.raises(replay.AblationError, match="cannot be retried"):
        replay.run(prepared, output, endpoint="http://localhost/v1", resume=True)
    assert len(calls) == count


def test_transport_failure_stops_without_secret_echo_or_fallback_network_call(frozen, tmp_path, monkeypatch):
    prepared, calls = replay.prepare(frozen["prepare"](), ids=["case-a"]), []
    secret = "sk-do-not-persist"
    def fail(*_):
        calls.append(1)
        raise RuntimeError(secret + " raw server body")
    monkeypatch.setattr(oracle, "chat_completion", fail)
    output = tmp_path / "failed.json"
    with pytest.raises(replay.AblationError, match="no retry") as error:
        replay.run(prepared, output, endpoint="http://localhost/v1", api_key=secret)
    assert len(calls) == 1
    assert secret not in str(error.value) and secret not in output.read_text()
    assert json.loads(output.read_text())["results"][-1]["status"] == "failed"
    with pytest.raises(replay.AblationError, match="cannot be retried"):
        replay.run(prepared, output, endpoint="http://localhost/v1", api_key=secret, resume=True)
    assert len(calls) == 1


def test_resume_after_completed_reader_reconstructs_without_another_reader_call(frozen, tmp_path, monkeypatch):
    prepared, calls = replay.prepare(frozen["prepare"](), ids=["case-a"]), []
    monkeypatch.setattr(oracle, "chat_completion", transport(calls))
    original = replay._candidate
    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt()
    monkeypatch.setattr(replay, "_candidate", interrupt)
    output = tmp_path / "after-reader.json"
    with pytest.raises(KeyboardInterrupt):
        replay.run(prepared, output, endpoint="http://localhost/v1")
    assert all(row["status"] == "completed" for row in json.loads(output.read_text())["results"])
    monkeypatch.setattr(replay, "_candidate", original)
    report = replay.run(prepared, output, endpoint="http://localhost/v1", resume=True)
    assert len(calls) == 3 and sum(body.get("max_tokens") == 4096 for body in calls) == 1
    assert len(report["results"]) == 3


def test_credential_echo_is_never_saved(frozen, tmp_path, monkeypatch):
    prepared = replay.prepare(frozen["prepare"](), ids=["case-a"])
    monkeypatch.setattr(oracle, "chat_completion", lambda endpoint, body, key, timeout: completion(body, key))
    output = tmp_path / "echo.json"
    with pytest.raises(replay.AblationError):
        replay.run(prepared, output, endpoint="http://localhost/v1", api_key="sk-private")
    assert "sk-private" not in output.read_text()


def test_data_and_implementation_drift_stop_before_any_dispatch(frozen, tmp_path, monkeypatch):
    prepared = replay.prepare(frozen["prepare"](), ids=["case-a"])
    monkeypatch.setattr(oracle, "chat_completion", lambda *_: pytest.fail("No request after drift"))
    frozen["paths"]["aliases"].write_text("{}", encoding="utf-8")
    with pytest.raises(replay.AblationError, match="data changed"):
        replay.run(prepared, tmp_path / "drift.json", endpoint="http://localhost/v1")
    frozen["save"]("aliases", frozen["aliases"])
    monkeypatch.setattr(replay, "implementation_hashes", lambda: {})
    with pytest.raises(replay.AblationError, match="Implementation changed"):
        replay.run(prepared, tmp_path / "code-drift.json", endpoint="http://localhost/v1")


def test_mid_run_code_drift_prevents_next_stage_dispatch(frozen, tmp_path, monkeypatch):
    prepared, calls = replay.prepare(frozen["prepare"](), ids=["case-a"]), []
    def once(endpoint, body, key, timeout):
        calls.append(1)
        monkeypatch.setattr(replay, "implementation_hashes", lambda: {})
        return completion(body, reader_answer(body) if body.get("max_tokens") else "Primary")
    monkeypatch.setattr(oracle, "chat_completion", once)
    with pytest.raises(replay.AblationError, match="Implementation changed"):
        replay.run(prepared, tmp_path / "mid-drift.json", endpoint="http://localhost/v1")
    assert len(calls) == 1


def test_resume_validates_whole_checkpoint_and_completed_stage_fingerprints(frozen, tmp_path, monkeypatch):
    prepared, calls = replay.prepare(frozen["prepare"](), ids=["case-a"]), []
    monkeypatch.setattr(oracle, "chat_completion", transport(calls))
    output = tmp_path / "complete.json"
    report = replay.run(prepared, output, endpoint="http://localhost/v1")
    report["results"][0]["output"] = "Changed after run"
    oracle._write_json(output, report)
    with pytest.raises(replay.AblationError, match="checkpoint fingerprint"):
        replay.run(prepared, output, endpoint="http://localhost/v1", resume=True)
    replay._checkpoint(output, report)
    with pytest.raises(replay.AblationError, match="output fingerprint"):
        replay.run(prepared, output, endpoint="http://localhost/v1", resume=True)
    assert len(calls) == 3


def test_request_identity_and_output_input_guards(frozen, tmp_path, monkeypatch):
    prepared = replay.prepare(frozen["prepare"](), ids=["case-a"])
    monkeypatch.setattr(oracle, "chat_completion", lambda *_: pytest.fail("No unsafe request"))
    changed = deepcopy(prepared)
    changed["cases"][0]["reader"]["request"]["seed"] = 99
    with pytest.raises(replay.AblationError, match="fingerprint"):
        replay.run(changed, tmp_path / "tamper.json", endpoint="http://localhost/v1")
    with pytest.raises(replay.AblationError, match="overwrite"):
        replay.run(prepared, frozen["paths"]["archive"], endpoint="http://localhost/v1")
    lock_output = tmp_path / "locked.json"
    lock_output.with_suffix(".json.lock").touch()
    with pytest.raises(replay.AblationError, match="lock"):
        replay.run(prepared, lock_output, endpoint="http://localhost/v1")


@pytest.mark.parametrize("endpoint", ["http://user:secret@localhost/v1", "http://localhost/v1?token=secret",
                                      "http://external.example/v1", "http://localhost/v1#secret"])
def test_unsafe_endpoint_reuses_secret_safe_guard(frozen, tmp_path, endpoint):
    with pytest.raises(replay.AblationError) as error:
        replay.run(replay.prepare(frozen["prepare"]()), tmp_path / "unsafe.json", endpoint=endpoint)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("settings", [{"temperature": 0.4}, {"seed": 1}, {"thinking": True}, {"max_output_tokens": 900}])
def test_uncontrolled_generation_settings_are_rejected(frozen, settings):
    with pytest.raises(replay.AblationError, match="Require temperature"):
        replay.prepare(frozen["prepare"](**settings))


def test_frozen_messages_and_exact_contextual_question_are_preserved(frozen):
    original = frozen["prepare"]()
    for case in original["cases"]:
        arm = case["arms"]["actual"]
        arm["request"]["messages"].append({"role": "assistant", "content": "FROZEN_HISTORY"})
        arm["request_sha256"] = oracle.digest(arm["request"])
    original["preparation_sha256"] = replay._fingerprint(original, "preparation_sha256")
    prepared = replay.prepare(original, ids=["case-a"])
    case = prepared["cases"][0]
    assert case["baseline"]["request"]["messages"][1]["content"] == "FROZEN_HISTORY"
    assert case["reader"]["request"]["messages"][1]["content"] == "FROZEN_HISTORY"
    info, _ = replay._candidate(case, reader_answer(case["reader"]["request"]))
    assert info["request"]["messages"][1]["content"] == "FROZEN_HISTORY"


def test_prepare_and_run_cli_protect_files_and_use_explicit_opt_in(frozen, tmp_path, monkeypatch, capsys):
    original = frozen["prepare"]()
    original_path = tmp_path / "oracle-prepared.json"
    oracle._write_json(original_path, original, exclusive=True)
    prepared_path, results_path = tmp_path / "prepared.json", tmp_path / "results.json"
    monkeypatch.setattr(oracle, "chat_completion", lambda *_: pytest.fail("prepare contacted HTTP"))
    assert replay.main(["prepare", "--oracle-prepared", str(original_path), "--output", str(prepared_path), "--ids", "case-a"]) == 0
    calls = []
    monkeypatch.setattr(oracle, "chat_completion", transport(calls))
    assert replay.main(["run", "--prepared", str(prepared_path), "--output", str(results_path), "--endpoint", "http://localhost/v1"]) == 0
    assert len(calls) == 3
    assert replay.main(["run", "--prepared", str(prepared_path), "--output", str(prepared_path), "--endpoint", "http://localhost/v1"]) == 2
    assert "overwrite" in capsys.readouterr().err
    assert len(calls) == 3
