"""Request-local budgets apply to internal calls, never shared final outputs."""
import threading
import time
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

import llm_providers
from llm_providers import ProviderLLM


PROVIDERS = ("openai", "deepseek", "gemini", "gigachat", "yandex")


def make_llm(provider="openai", **kwargs):
    llm = ProviderLLM(
        provider=provider, api_key="synthetic-test-key", base_url="https://example.invalid",
        model="synthetic-test-model", folder_id="synthetic-folder", timeout=900, **kwargs,
    )
    # All tests are offline. Exercise the completion payload, not real OAuth.
    llm._giga_token = "synthetic-cached-token"
    llm._giga_token_expires = time.time() + 3600
    return llm


@pytest.fixture
def requests(monkeypatch):
    records = []
    monkeypatch.setattr(llm_providers, "MAX_OUTPUT_TOKENS", -1)
    monkeypatch.setattr(llm_providers, "gigachat_ca_bundle", lambda: True)

    class Response:
        ok = True

        def json(self):
            return {"choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}],
                    "result": {"alternatives": [{"message": {"text": "synthetic answer"},
                                                  "status": "ALTERNATIVE_STATUS_FINAL"}]}}

    def post(url, **kwargs):
        records.append((url, kwargs))
        return Response()

    monkeypatch.setattr(llm_providers.requests, "post", post)
    return records


def payload_budget(provider, payload):
    return payload.get("completionOptions", {}).get("maxTokens") if provider == "yandex" else payload.get("max_tokens")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_internal_stage_is_bounded_but_following_final_answer_remains_uncapped(provider, requests):
    llm = make_llm(provider)
    result = llm.complete_stage("Построй внутренний план", max_tokens=512, timeout=17.5)
    assert str(result) == "synthetic answer"
    stage = requests[-1][1]
    assert payload_budget(provider, stage["json"]) == 512
    assert stage["timeout"] == 17.5
    assert llm.output_token_limit == -1
    assert llm.timeout == 900

    assert str(llm.complete("Полный итоговый ответ")) == "synthetic answer"
    final = requests[-1][1]
    assert payload_budget(provider, final["json"]) is None
    assert final["timeout"] == 900
    assert llm.output_token_limit == -1


@pytest.mark.parametrize("provider", PROVIDERS)
def test_operator_final_limit_and_internal_budget_are_independent(provider, requests):
    llm = make_llm(provider, output_token_limit=10000)
    llm.complete_stage("Review", max_tokens=700, timeout=21)
    assert payload_budget(provider, requests[-1][1]["json"]) == 700
    llm.complete("Answer")
    assert payload_budget(provider, requests[-1][1]["json"]) == 10000
    assert llm.output_token_limit == 10000


def test_per_instance_default_captures_current_operator_configuration(monkeypatch, requests):
    first = make_llm()
    monkeypatch.setattr(llm_providers, "MAX_OUTPUT_TOKENS", 2300)
    second = make_llm()
    assert first.output_token_limit == -1
    assert second.output_token_limit == 2300
    first.complete("Answer")
    assert payload_budget("openai", requests[-1][1]["json"]) is None
    second.complete("Answer")
    assert payload_budget("openai", requests[-1][1]["json"]) == 2300


def test_concurrent_stage_clones_do_not_change_each_other_or_shared_adapter(monkeypatch, requests):
    llm = make_llm()
    barrier = threading.Barrier(2)
    captured = []

    def post(adapter, path, headers, payload):
        if payload.get("max_tokens"):
            barrier.wait(timeout=5)
        captured.append((id(adapter), adapter.timeout, payload.get("max_tokens"), payload["messages"][0]["content"]))
        return {"choices": [{"message": {"content": "synthetic answer"}}]}

    monkeypatch.setattr(ProviderLLM, "_post", post)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda settings: llm.complete_stage(
            str(settings[0]), max_tokens=settings[0], timeout=settings[1]), [(256, 11), (768, 23)]))
    assert all(str(result) == "synthetic answer" for result in results)
    assert {(row[1], row[2]) for row in captured} == {(11, 256), (23, 768)}
    assert len({row[0] for row in captured}) == 2
    assert all(row[0] != id(llm) for row in captured)
    assert llm.timeout == 900
    assert llm.output_token_limit == -1
    llm.complete("final")
    assert captured[-1] == (id(llm), 900, None, "final")


def test_failed_stage_cannot_leave_shared_adapter_capped(monkeypatch, requests):
    llm = make_llm()

    def fail(*args, **kwargs):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(ProviderLLM, "_post", fail)
    with pytest.raises(TimeoutError):
        llm.complete_stage("review", max_tokens=900, timeout=3)
    assert llm.timeout == 900
    assert llm.output_token_limit == -1


def test_gigachat_oauth_and_completion_use_stage_deadline_on_private_clone(monkeypatch, requests):
    llm = make_llm("gigachat")
    llm._giga_token = ""
    llm._giga_token_expires = 0
    sent = []

    class Response:
        ok = True

        def __init__(self, oauth):
            self.oauth = oauth

        def json(self):
            if self.oauth:
                return {"access_token": "synthetic-new-token", "expires_at": time.time() + 1800}
            return {"choices": [{"message": {"content": "synthetic answer"}, "finish_reason": "stop"}]}

    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response(url.endswith("/oauth"))

    monkeypatch.setattr(llm_providers.requests, "post", post)
    assert str(llm.complete_stage("review", max_tokens=800, timeout=7)) == "synthetic answer"
    assert len(sent) == 2
    assert all(kwargs["timeout"] == 7 for _, kwargs in sent)
    assert sent[-1][1]["json"]["max_tokens"] == 800
    assert sent[-1][1]["headers"]["Authorization"] == "Bearer synthetic-new-token"
    assert llm._giga_token == ""
    assert llm._giga_token_expires == 0
    assert llm.timeout == 900


@pytest.mark.parametrize("budget,timeout", [
    (-1, 5), (0, 5), (True, 5), (1.5, 5), (500, -1), (500, 0),
    (500, float("inf")), (500, float("nan")), (500, True),
])
def test_invalid_internal_budgets_fail_before_sending_a_request(budget, timeout, requests):
    llm = make_llm()
    with pytest.raises(ValueError):
        llm.complete_stage("review", max_tokens=budget, timeout=timeout)
    assert requests == []


def test_streamed_final_completion_remains_uncapped_after_internal_stage(requests):
    llm = make_llm()
    llm.complete_stage("plan", max_tokens=512, timeout=15)
    assert list(llm.stream_complete("final"))[0].text == "synthetic answer"
    assert "max_tokens" not in requests[-1][1]["json"]
    assert requests[-1][1]["timeout"] == 900


def test_stage_metadata_reports_its_own_budget_without_mutating_final_metadata(monkeypatch, requests):
    llm = make_llm()
    seen = []

    def complete(adapter, prompt, **kwargs):
        seen.append(adapter.metadata.num_output)
        return "synthetic answer"

    monkeypatch.setattr(ProviderLLM, "complete", complete)
    llm.complete_stage("plan", max_tokens=400, timeout=15)
    assert seen == [400]
    assert llm.metadata.num_output == 2048


def completion_body(provider, text, *, finished=True, reason=None):
    if provider == "yandex":
        return {"result": {"alternatives": [{"message": {"text": text}, "status": reason or (
            "ALTERNATIVE_STATUS_FINAL" if finished else "ALTERNATIVE_STATUS_TRUNCATED_FINAL")}]}}
    return {"choices": [{"message": {"content": text}, "finish_reason": reason or ("stop" if finished else "length")}]}


def mocked_response(monkeypatch, body):
    """Mock HTTP responses, keeping the adapter's response boundary exercised."""
    sent = []
    class Response:
        ok = True
        def json(self):
            return body
    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()
    monkeypatch.setattr(llm_providers.requests, "post", post)
    monkeypatch.setattr(llm_providers, "gigachat_ca_bundle", lambda: True)
    return sent


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("finished", [False, True])
def test_internal_stage_checks_real_response_termination_even_for_parseable_json(provider, finished, monkeypatch):
    text = json.dumps({"claims": []})
    sent = mocked_response(monkeypatch, completion_body(provider, text, finished=finished))
    llm = make_llm(provider)
    if finished:
        assert str(llm.complete_stage("judge", max_tokens=4096, timeout=120)) == text
    else:
        with pytest.raises(RuntimeError, match="Internal stage response did not finish completely"):
            llm.complete_stage("judge", max_tokens=4096, timeout=120)
    assert len(sent) == 1
    expected_path = "/completion" if provider == "yandex" else "/v1/chat/completions" if provider == "gigachat" else "/chat/completions"
    assert sent[0][0].endswith(expected_path)
    assert payload_budget(provider, sent[0][1]["json"]) == 4096
    assert sent[0][1]["timeout"] == 120
    assert "require_complete_output" not in sent[0][1]["json"]
    assert llm.require_complete_output is False
    assert llm.output_token_limit == -1 and llm.timeout == 900


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("metadata", ["missing", "unknown", "partial", "null"])
def test_internal_missing_unknown_and_nonfinal_metadata_fail_closed_without_sensitive_diagnostics(provider, metadata, monkeypatch):
    secret = "synthetic-test-key RESPONSE_BODY_MUST_NOT_ESCAPE"
    body = completion_body(provider, secret)
    alternative = body["result"]["alternatives"][0] if provider == "yandex" else body["choices"][0]
    key = "status" if provider == "yandex" else "finish_reason"
    if metadata == "missing":
        alternative.pop(key)
    else:
        alternative[key] = (None if metadata == "null" else "ALTERNATIVE_STATUS_PARTIAL" if
                            metadata == "partial" and provider == "yandex" else "content_filter" if
                            metadata == "partial" else "unknown-status-" + secret)
    sent = mocked_response(monkeypatch, body)
    with pytest.raises(RuntimeError) as error:
        make_llm(provider).complete_stage("planning", max_tokens=512, timeout=15)
    assert str(error.value) == "Internal stage response did not finish completely"
    assert secret not in str(error.value) and len(sent) == 1


@pytest.mark.parametrize("provider,body", [
    pytest.param("openai", None, id="response-null"),
    pytest.param("openai", [], id="response-array"),
    pytest.param("openai", {"choices": None}, id="choices-null"),
    pytest.param("openai", {"choices": {}}, id="choices-mapping"),
    pytest.param("openai", {"choices": []}, id="choices-empty"),
    pytest.param("openai", {"choices": [None]}, id="choice-null"),
    pytest.param("openai", {"choices": [{"finish_reason": []}]}, id="finish-reason-array"),
    pytest.param("openai", {"choices": [{"finish_reason": {}}]}, id="finish-reason-mapping"),
    pytest.param("openai", {"choices": [{"finish_reason": True}]}, id="finish-reason-boolean"),
    pytest.param("yandex", {"result": None}, id="yandex-result-null"),
    pytest.param("yandex", {"result": []}, id="yandex-result-array"),
    pytest.param("yandex", {"result": {"alternatives": None}}, id="alternatives-null"),
    pytest.param("yandex", {"result": {"alternatives": {}}}, id="alternatives-mapping"),
    pytest.param("yandex", {"result": {"alternatives": []}}, id="alternatives-empty"),
    pytest.param("yandex", {"result": {"alternatives": [None]}}, id="alternative-null"),
    pytest.param("yandex", {"result": {"alternatives": [{"status": []}]}}, id="yandex-status-array"),
    pytest.param("yandex", {"result": {"alternatives": [{"status": {}}]}}, id="yandex-status-mapping"),
    pytest.param("yandex", {"result": {"alternatives": [{"status": False}]}}, id="yandex-status-boolean"),
])
def test_internal_malformed_response_containers_and_status_types_fail_with_controlled_error(provider, body, monkeypatch):
    sent = mocked_response(monkeypatch, body)
    llm = make_llm(provider)
    with pytest.raises(RuntimeError) as error:
        llm.complete_stage("structured review", max_tokens=512, timeout=15)
    assert str(error.value) == "Internal stage response did not finish completely"
    assert len(sent) == 1
    assert llm.require_complete_output is False
    assert llm.output_token_limit == -1 and llm.timeout == 900


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("final_limit", [-1, 10000])
def test_stage_rejection_preserves_regular_final_and_stream_behavior_with_optional_operator_cap(provider, final_limit, monkeypatch):
    text = "Retain final text even when the provider reports truncation."
    sent = mocked_response(monkeypatch, completion_body(provider, text, finished=False))
    llm = make_llm(provider, output_token_limit=final_limit)
    with pytest.raises(RuntimeError):
        llm.complete_stage("coverage", max_tokens=400, timeout=10)
    assert str(llm.complete("final")) == text
    assert list(llm.stream_complete("final stream"))[0].text == text
    assert [payload_budget(provider, entry[1]["json"]) for entry in sent] == [400,
        final_limit if final_limit > 0 else None, final_limit if final_limit > 0 else None]
    assert [entry[1]["timeout"] for entry in sent] == [10, 900, 900]
    assert llm.require_complete_output is False and llm.output_token_limit == final_limit


def test_concurrent_response_integrity_flags_do_not_leak_to_shared_final_adapter(monkeypatch):
    llm = make_llm()
    barrier = threading.Barrier(2)
    sent = []
    class Response:
        ok = True
        def __init__(self, finished):
            self.finished = finished
        def json(self):
            return completion_body("openai", "{}", finished=self.finished)
    def post(url, **kwargs):
        budget = kwargs["json"].get("max_tokens")
        if budget is not None:
            barrier.wait(timeout=5)
        sent.append((budget, kwargs["timeout"]))
        return Response(budget == 768)
    monkeypatch.setattr(llm_providers.requests, "post", post)
    def stage(settings):
        try:
            return str(llm.complete_stage("review", max_tokens=settings[0], timeout=settings[1]))
        except RuntimeError:
            return "rejected"
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(stage, [(256, 11), (768, 23)])) == ["rejected", "{}"]
    assert set(sent) == {(256, 11), (768, 23)}
    assert str(llm.complete("uncapped final")) == "{}"
    assert sent[-1] == (None, 900)
    assert llm.require_complete_output is False and llm.output_token_limit == -1


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("finished", [False, True])
def test_source_reader_requires_normal_provider_termination_before_assessing_parseable_json(provider, finished, monkeypatch):
    from source_reader import build_source_reading_prompt, read_sources
    question = "Как выполняют контроль?"
    plan = {"requirements": ["Основное правило"], "exclusions": [], "canonical_question": question}
    evidence = [{"evidence_id": "E1", "file": "Synthetic.md", "text": "Контроль выполняют перед запуском."}]
    prompt = build_source_reading_prompt(question, plan, evidence)
    snapshot_id = json.loads(prompt.split("ДАННЫЕ ЧТЕНИЯ:\n", 1)[1])["snapshot_id"]
    text = json.dumps({"snapshot_id": snapshot_id, "cards": [{"id": "C1", "kind": "rule",
        "passage_ids": ["E1:P1"], "related_card_ids": [], "requirement_ids": ["R1"]}],
        "unresolved_requirement_ids": []})
    sent = mocked_response(monkeypatch, completion_body(provider, text, finished=finished))
    llm = make_llm(provider)
    reading = read_sources(question, plan, evidence,
                           lambda prompt, _: str(llm.complete_stage(prompt, max_tokens=4096, timeout=120)))
    assert reading.assessed is finished
    if finished:
        assert len(reading.cards) == 1
        assert reading.quoted_passages[0].quote == evidence[0]["text"]
    else:
        assert reading.cards == () and reading.quoted_passages == ()
        assert reading.warnings == ("reader-call-failed",)
    assert len(sent) == 1 and llm.require_complete_output is False


@pytest.mark.parametrize("provider", ["openai", "yandex"])
def test_internal_invalid_json_uses_controlled_error_without_changing_final_decode_behavior(provider, monkeypatch):
    secret = "synthetic-test-key RAW_RESPONSE_BODY"
    class Response:
        ok = True
        def json(self):
            raise ValueError(secret)
    sent = []
    def post(*args, **kwargs):
        sent.append(1)
        return Response()
    monkeypatch.setattr(llm_providers.requests, "post", post)
    llm = make_llm(provider)
    with pytest.raises(RuntimeError) as error:
        llm.complete_stage("planning", max_tokens=512, timeout=15)
    assert str(error.value) == "Internal stage response did not finish completely"
    assert secret not in str(error.value)
    with pytest.raises(ValueError):
        llm.complete("final")
    assert len(sent) == 2 and llm.require_complete_output is False
