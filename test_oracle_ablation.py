"""Offline causal replay contracts. No retrieval, database, model or HTTP calls."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from evals import oracle_ablation as replay


@pytest.fixture
def frozen(tmp_path):
    aliases = {"a_doc.md": "Doc.md", "b_other.md": "Other.md"}
    native = "Native fact A.\n\nNative fact B."
    node = {"__data__": {"id_": "node-a", "metadata": {"file_name": "A_Doc.md",
            "section_path": "Rule", "status": "active", "file_path": "/private/server/path"}, "text": native}}
    docstore = {"docstore/data": {"node-a": node}}
    context = {"file": "A_Doc.md", "file_path": "/private/archive/path", "text": "Native fact A.",
               "section_path": "Rule", "status": "active", "applicability": "referenced",
               "section_role": "operative", "selection_signals": {"definition_closure_incomplete": True,
               "inventory_enumeration_reserved": True,
               "missing_definition_symbols": ["C"]}, "reference_from": [{"file": "B_Other.md",
               "section_path": "Other rule", "quote": "Refer to rule."}],
               "reference_target": {"kind": "section", "hint": "Rule", "locator": "5.3"},
               "metadata": {"unexpected_private_key": "METADATA_MUST_NOT_REACH_PROMPT"}}
    plan = {"requirements": ["Explain rule"], "queries": ["rule"], "exclusions": ["other subject"],
            "style": "short", "fallback": False, "canonical_question": "Explain the rule?"}
    archive = {"scope": replay.SCOPE, "runtime": {"model": "fixture-model"},
               "source_manifest": [{"file_name": name, "content_sha256": str(index) * 64}
                                   for index, name in enumerate(aliases.values(), 1)],
               "cases": [{"id": case_id, "question": "Explain the rule?", "trace": {"plan": plan},
                          "context": [context], "answer": "ARCHIVED_REPAIRED_ANSWER_IS_NOT_CONTROL"}
                         for case_id in ("case-b", "case-a")]}
    paths = {name: tmp_path / (name + ".json") for name in ("archive", "oracle", "docstore", "aliases")}
    def save(name, value):
        paths[name].write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    save("aliases", aliases)
    save("docstore", docstore)
    oracle = {"schema_version": 1, "scope": replay.SCOPE,
              "aliases_sha256": replay.digest(paths["aliases"].read_bytes()),
              "docstore_sha256": replay.digest(paths["docstore"].read_bytes()),
              "cases": [{"id": case_id, "evidence": [{"node_id": "node-a", "source": "a_doc.md"}],
                         "required_facts": ["RUBRIC_MUST_NOT_REACH_PROMPT"],
                         "forbidden_claims": ["FORBIDDEN_CLAIM_MUST_NOT_REACH_PROMPT"],
                         "scoring_rubric": {"pass": "SCORING_RUBRIC_MUST_NOT_REACH_PROMPT"}}
                        for case_id in ("case-a", "case-b")]}
    save("archive", archive)
    save("oracle", oracle)
    def prepare(**kwargs):
        return replay.prepare(*(paths[name] for name in ("archive", "oracle", "docstore", "aliases")),
                              runtime_id="fixture-image-and-start", model_revision="fixture-weight-revision", **kwargs)
    return {"paths": paths, "archive": archive, "oracle": oracle, "docstore": docstore,
            "aliases": aliases, "save": save, "prepare": prepare, "native": native}


def payload(arm):
    prompt = arm["request"]["messages"][0]["content"]
    start = prompt.index('{"question":')
    return json.JSONDecoder().raw_decode(prompt[start:])[0]


def completion(body, output="Primary answer [E1]"):
    return {"output": output, "output_sha256": replay.text_digest(output), "finish_reason": "stop",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}, "server_model": body["model"]}


def test_prepare_is_offline_and_both_arms_replay_same_saved_plan_and_coverage(frozen, monkeypatch):
    monkeypatch.setattr(replay, "chat_completion", lambda *args: pytest.fail("prepare contacted model"))
    prepared = frozen["prepare"]()
    assert [row["id"] for row in prepared["cases"]] == ["case-a", "case-b"]
    assert prepared["calls_per_arm"] == {"generation": 1, "planning": 0, "review": 0, "repair": 0}
    assert prepared["archived_answers_used"] is False
    actual, oracle = (payload(prepared["cases"][0]["arms"][name]) for name in ("actual", "oracle"))
    for key in ("question", "canonical_question", "requirements", "exclusions", "coverage"):
        assert actual[key] == oracle[key]
    assert actual["coverage"] == replay.NEUTRAL_COVERAGE
    assert actual["evidence"][0]["selection_signals"]["definition_closure_incomplete"] is True
    assert actual["evidence"][0]["selection_signals"]["inventory_enumeration_reserved"] is True
    assert actual["evidence"][0]["applicability"] == "referenced"
    assert actual["evidence"][0]["reference_from"][0]["file"] == "b_other.md"
    assert actual["evidence"][0]["section_role"] == "operative"
    assert actual["evidence"][0]["reference_target"]["locator"] == "5.3"
    for arm in prepared["cases"][0]["arms"].values():
        request = arm["request"]
        prompt = request["messages"][0]["content"]
        assert "max_tokens" not in request
        assert request["chat_template_kwargs"] == {"enable_thinking": False}
        assert not request["stream"] and request["seed"] == 42
        for forbidden in ("RUBRIC_MUST_NOT", "METADATA_MUST_NOT", "/private/", "ARCHIVED_REPAIRED_ANSWER",
                          "FORBIDDEN_CLAIM_MUST_NOT", "SCORING_RUBRIC_MUST_NOT"):
            assert forbidden not in prompt
        assert arm["request_json_chars"] == len(replay.canonical(request))
        assert arm["prompt_chars"] == len(prompt)
    assert prepared["cases"][0]["oracle_provenance"][0]["text_sha256"] == replay.text_digest(frozen["native"])


def test_pairing_is_stable_across_archive_and_manifest_order(frozen):
    before = frozen["prepare"]()
    frozen["archive"]["cases"].reverse()
    frozen["oracle"]["cases"].reverse()
    frozen["save"]("archive", frozen["archive"])
    frozen["save"]("oracle", frozen["oracle"])
    after = frozen["prepare"]()
    assert [(row["id"], row["pair_sha256"], row["execution_order"]) for row in before["cases"]] == [
        (row["id"], row["pair_sha256"], row["execution_order"]) for row in after["cases"]]


@pytest.mark.parametrize("source", ["A_Doc.md", "unknown.md", "../a_doc.md", "Doc.md"])
def test_oracle_physical_source_must_match_alias_key_exactly(frozen, source):
    frozen["oracle"]["cases"][0]["evidence"][0]["source"] = source
    frozen["save"]("oracle", frozen["oracle"])
    with pytest.raises(replay.AblationError, match="source"):
        frozen["prepare"]()


@pytest.mark.parametrize("mutation", ["node-source", "deleted", "project", "node-id", "authored-text"])
def test_oracle_rejects_source_mismatch_private_deleted_unknown_or_authored_evidence(frozen, mutation):
    node = frozen["docstore"]["docstore/data"]["node-a"]["__data__"]
    if mutation == "node-source":
        node["metadata"]["file_name"] = "b_other.md"
    elif mutation == "deleted":
        node["metadata"]["status"] = "deleted"
    elif mutation == "project":
        node["metadata"]["project_id"] = "private-project"
    elif mutation == "node-id":
        frozen["oracle"]["cases"][0]["evidence"][0]["node_id"] = "missing"
    else:
        frozen["oracle"]["cases"][0]["evidence"][0]["text"] = "Authored summary"
    frozen["save"]("docstore", frozen["docstore"])
    frozen["oracle"]["docstore_sha256"] = replay.digest(frozen["paths"]["docstore"].read_bytes())
    frozen["save"]("oracle", frozen["oracle"])
    with pytest.raises(replay.AblationError):
        frozen["prepare"]()


def test_exact_span_offsets_and_hash_are_preserved(frozen):
    ref = frozen["oracle"]["cases"][0]["evidence"][0]
    start = frozen["native"].index("Native fact B")
    ref.update(span_start=start, span_end=len(frozen["native"]),
               text_sha256=replay.text_digest(frozen["native"][start:]))
    frozen["save"]("oracle", frozen["oracle"])
    prepared = frozen["prepare"]()
    assert payload(prepared["cases"][0]["arms"]["oracle"])["evidence"][0]["text"] == "Native fact B."
    ref["text_sha256"] = "0" * 64
    frozen["save"]("oracle", frozen["oracle"])
    with pytest.raises(replay.AblationError, match="hash"):
        frozen["prepare"]()


@pytest.mark.parametrize("offsets", [{"span_start": 1}, {"span_start": -1, "span_end": 9},
                                    {"span_start": True, "span_end": 9}, {"span_start": 3, "span_end": 3000},
                                    {"span_start": 4, "span_end": 4}])
def test_invalid_spans_fail_closed(frozen, offsets):
    frozen["oracle"]["cases"][0]["evidence"][0].update(offsets)
    frozen["save"]("oracle", frozen["oracle"])
    with pytest.raises(replay.AblationError, match="span"):
        frozen["prepare"]()


@pytest.mark.parametrize("budget", [{"context_budget": 4}, {"prompt_budget": 100}])
def test_budget_failures_never_slice_or_silently_drop(frozen, budget):
    with pytest.raises(replay.AblationError, match="budget"):
        frozen["prepare"](**budget)


def test_duplicate_evidence_cannot_be_silently_removed(frozen):
    refs = frozen["oracle"]["cases"][0]["evidence"]
    refs.append(deepcopy(refs[0]))
    frozen["save"]("oracle", frozen["oracle"])
    with pytest.raises(replay.AblationError, match="dropped or deduplicated"):
        frozen["prepare"]()


@pytest.mark.parametrize("mutation", ["manifest-sources", "actual-source", "private-reference", "hashes", "case-missing", "case-duplicate"])
def test_exact_acl_case_and_snapshot_validation(frozen, mutation):
    if mutation == "manifest-sources":
        frozen["archive"]["source_manifest"][0]["file_name"] = "unknown.md"
    elif mutation == "actual-source":
        frozen["archive"]["cases"][0]["context"][0]["file"] = "private.md"
    elif mutation == "private-reference":
        frozen["archive"]["cases"][0]["context"][0]["reference_from"][0]["file"] = "private.md"
    elif mutation == "hashes":
        frozen["oracle"]["aliases_sha256"] = "0" * 64
    elif mutation == "case-missing":
        frozen["oracle"]["cases"].pop()
    else:
        frozen["oracle"]["cases"].append(deepcopy(frozen["oracle"]["cases"][0]))
    frozen["save"]("archive", frozen["archive"])
    frozen["save"]("oracle", frozen["oracle"])
    with pytest.raises(replay.AblationError):
        frozen["prepare"]()


def test_serial_run_and_resume_issue_each_request_exactly_once(frozen, tmp_path, monkeypatch):
    prepared = frozen["prepare"]()
    calls = []
    def complete(endpoint, body, api_key, timeout):
        calls.append(replay.digest(body))
        return completion(body)
    monkeypatch.setattr(replay, "chat_completion", complete)
    output = tmp_path / "results.json"
    report = replay.run(prepared, output, endpoint="http://127.0.0.1:18084/v1")
    assert len(calls) == 4 and len(report["results"]) == 4
    assert calls == [case["arms"][name]["request_sha256"] for case in prepared["cases"] for name in case["execution_order"]]
    assert all(row["elapsed_s"] >= 0 and row["usage"]["total_tokens"] == 20 for row in report["results"])
    assert report["case_audit"][0]["forbidden_claims"] == ["FORBIDDEN_CLAIM_MUST_NOT_REACH_PROMPT"]
    assert report["provenance"]["input_sha256"] == prepared["input_sha256"]
    replay.run(prepared, output, endpoint="http://127.0.0.1:18084/v1", resume=True)
    assert len(calls) == 4
    with pytest.raises(replay.AblationError, match="fingerprints"):
        replay.run(prepared, output, endpoint="http://127.0.0.1:18085/v1", resume=True)


def test_failed_request_does_not_retry_or_leak_secrets(frozen, tmp_path, monkeypatch):
    prepared, calls = frozen["prepare"](), []
    secret = "sk-private-credential"
    def fail(*args):
        calls.append(1)
        raise RuntimeError(secret + " HTTP-body")
    monkeypatch.setattr(replay, "chat_completion", fail)
    output = tmp_path / "failed.json"
    with pytest.raises(replay.AblationError, match="no retry") as error:
        replay.run(prepared, output, endpoint="http://localhost:123/v1", api_key=secret)
    assert len(calls) == 1
    assert secret not in str(error.value) and secret not in output.read_text()
    with pytest.raises(replay.AblationError, match="cannot be retried"):
        replay.run(prepared, output, endpoint="http://localhost:123/v1", api_key=secret, resume=True)
    assert len(calls) == 1


def test_interrupted_unknown_request_is_not_automatically_repeated(frozen, tmp_path, monkeypatch):
    def interrupt(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(replay, "chat_completion", interrupt)
    output = tmp_path / "interrupted.json"
    prepared = frozen["prepare"]()
    with pytest.raises(KeyboardInterrupt):
        replay.run(prepared, output, endpoint="http://localhost:123/v1")
    assert json.loads(output.read_text())["results"][0]["status"] == "started"
    with pytest.raises(replay.AblationError, match="cannot be retried"):
        replay.run(prepared, output, endpoint="http://localhost:123/v1", resume=True)


def test_preparation_tampering_and_concurrent_output_lock_refused(frozen, tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "chat_completion", lambda *args: pytest.fail("unexpected completion"))
    prepared = frozen["prepare"]()
    changed = deepcopy(prepared)
    changed["cases"][0]["arms"]["actual"]["request"]["seed"] += 1
    with pytest.raises(replay.AblationError, match="fingerprint"):
        replay.run(changed, tmp_path / "changed.json", endpoint="http://localhost:123/v1")
    output = tmp_path / "locked.json"
    output.with_suffix(".json.lock").touch()
    with pytest.raises(replay.AblationError, match="lock"):
        replay.run(prepared, output, endpoint="http://localhost:123/v1")


def test_resume_rejects_altered_output_or_preparation_identity(frozen, tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "chat_completion", lambda endpoint, body, key, timeout: completion(body))
    prepared = frozen["prepare"]()
    output = tmp_path / "resumed.json"
    report = replay.run(prepared, output, endpoint="http://localhost/v1")
    report["results"][0]["output"] = "Changed after run"
    output.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(replay.AblationError, match="fingerprint"):
        replay.run(prepared, output, endpoint="http://localhost/v1", resume=True)
    report["preparation_sha256"] = "0" * 64
    output.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(replay.AblationError, match="fingerprints"):
        replay.run(prepared, output, endpoint="http://localhost/v1", resume=True)


def test_prepare_and_run_outputs_never_overwrite_inputs(frozen, monkeypatch):
    monkeypatch.setattr(replay, "chat_completion", lambda *args: pytest.fail("unexpected completion"))
    before = frozen["paths"]["archive"].read_bytes()
    with pytest.raises(replay.AblationError, match="overwrite"):
        replay.run(frozen["prepare"](), frozen["paths"]["archive"], endpoint="http://localhost:123/v1")
    args = ["prepare", "--runtime-id", "r", "--model-revision", "m"]
    for name, path in frozen["paths"].items():
        args.extend(["--" + name, str(path)])
    assert replay.main([*args, "--output", str(frozen["paths"]["archive"])]) == 2
    assert frozen["paths"]["archive"].read_bytes() == before


@pytest.mark.parametrize("endpoint", ["http://user:secret@localhost/v1", "http://localhost/v1?token=secret",
                                      "http://external.example/v1", "http://localhost/v1#secret"])
def test_unsafe_endpoint_refused_without_echo(frozen, tmp_path, endpoint):
    with pytest.raises(replay.AblationError) as error:
        replay.run(frozen["prepare"](), tmp_path / "no.json", endpoint=endpoint)
    assert "secret" not in str(error.value)


def test_http_error_has_no_body_or_credential_and_no_retry(monkeypatch):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            raise HTTPError(request.full_url, 401, "sk-private server error", {}, None)
    def opener(*handlers):
        proxy_handlers = [handler for handler in handlers if isinstance(handler, replay.ProxyHandler)]
        assert len(proxy_handlers) == 1 and proxy_handlers[0].proxies == {}
        assert replay._NoRedirect in handlers
        return Opener()
    monkeypatch.setattr(replay, "build_opener", opener)
    with pytest.raises(replay.AblationError) as error:
        replay.chat_completion("http://localhost/v1/chat/completions", {"model": "fixture-model"}, "sk-private", 5)
    assert len(calls) == 1 and "sk-private" not in str(error.value)


def test_completion_response_credential_echo_is_not_saved(frozen, tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "chat_completion", lambda endpoint, body, key, timeout: completion(body, key))
    output = tmp_path / "echo.json"
    with pytest.raises(replay.AblationError):
        replay.run(frozen["prepare"](), output, endpoint="http://localhost/v1", api_key="sk-private")
    assert "sk-private" not in output.read_text()
