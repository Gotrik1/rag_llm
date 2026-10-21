"""Request-local budgets apply to internal calls, never shared final outputs."""
import threading
import time
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
            return {"choices": [{"message": {"content": "synthetic answer"}}],
                    "result": {"alternatives": [{"message": {"text": "synthetic answer"}}]}}

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
            return {"choices": [{"message": {"content": "synthetic answer"}}]}

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
