"""Offline contracts for request-pinned provider identity and stage clients."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
import json
from types import SimpleNamespace

import pytest

from provider_runtime import (
    RuntimeConfigError, RuntimeInvocationError, capture_runtime,
)


DEFAULT_PROFILE = {"context_window": 16384, "thinking": None,
                   "num_predict": -1, "request_timeout": 180.0}
PROFILES = {"same-model": {"context_window": 8192, "thinking": False,
                            "num_predict": -1, "request_timeout": 120.0}}


class FakeClient:
    def __init__(self, spec):
        self.spec = spec
        self.calls = []

    def complete(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return SimpleNamespace(text="answer", raw={"done": True, "done_reason": "stop"})


def capture(config=None, *, factory=None, environ=None, profiles=None):
    return capture_runtime(config or {"provider": "ollama", "model": "same-model"},
                           default_model="fallback-model", ollama_base_url="http://default.invalid:11434",
                           ollama_profiles=PROFILES if profiles is None else profiles,
                           default_ollama_profile=DEFAULT_PROFILE, client_factory=factory or FakeClient,
                           environ={} if environ is None else environ)


def cloud(**updates):
    return {"provider": "openai", "model": "same-model", "base_url": "https://cloud.invalid/v1",
            "api_key": "synthetic-private-credential", **updates}


def test_cloud_to_ollama_same_model_and_custom_url_create_actual_new_client():
    first = capture(cloud())
    second = capture({"provider": "ollama", "model": "same-model", "base_url": "http://custom.invalid:9999"})
    assert first.spec.provider == "openai" and second.spec.provider == "ollama"
    assert first.spec.model == second.spec.model == "same-model"
    assert first.client is not second.client
    assert second.spec.endpoint == "http://custom.invalid:9999"
    assert first.identity != second.identity


def test_every_request_owns_client_even_when_effective_identity_is_unchanged():
    first, second = capture(), capture()
    assert first.identity == second.identity and first.client is not second.client


@pytest.mark.parametrize("update", [
    {"base_url": "https://other.invalid/v1"}, {"model": "different-model"},
    {"api_key": "second-synthetic-private-key"}, {"auth_type": "iam"},
    {"folder_id": "other-folder"}, {"temperature": 0.7}, {"timeout": 25},
    {"output_token_limit": 900}, {"context_window": 4096}, {"seed": 123},
    {"thinking": True}, {"top_p": 0.8}, {"top_k": 15},
])
def test_effective_identity_partitions_endpoint_auth_and_settings(update):
    original = capture(cloud())
    if set(update) <= {"seed", "thinking", "top_p", "top_k"}:
        base = {"provider": "ollama", "model": "same-model"}
        original = capture(base)
    else:
        base = cloud()
    assert capture({**base, **update}).identity != original.identity


def test_identity_is_stable_with_existing_secret_and_without_secret_provisioning():
    config = cloud()
    assert capture(config).identity == capture(dict(config)).identity
    assert capture(config, environ={"RAG_SECRET_KEY": "synthetic-app-key"}).identity == capture(
        config, environ={"RAG_SECRET_KEY": "synthetic-app-key"}).identity
    assert capture(config, environ={"RAG_SECRET_KEY": "synthetic-app-key"}).identity != capture(config).identity
    assert capture(environ={}).identity == capture(environ={}).identity


def test_config_profile_and_environment_are_detached_once_per_request():
    config = {"provider": "ollama", "model": "same-model"}
    profiles = {"same-model": {**PROFILES["same-model"], "top_p": 0.9}}
    env = {"LLM_REQUEST_TIMEOUT": "77", "RAG_MAX_OUTPUT_TOKENS": "2500"}
    snapshot = capture(config, profiles=profiles, environ=env)
    config["model"], profiles["same-model"]["context_window"], env["LLM_REQUEST_TIMEOUT"] = "later", 100, "3"
    assert snapshot.spec.model == "same-model"
    assert snapshot.spec.settings == {"temperature": 0.0, "timeout": 77.0, "context_window": 8192,
                                      "thinking": False, "output_token_limit": 2500, "seed": 42,
                                      "top_p": 0.9, "top_k": None, "require_complete_output": False}
    settings = snapshot.spec.settings
    settings["context_window"] = 1
    assert snapshot.spec.settings["context_window"] == 8192
    with pytest.raises(FrozenInstanceError):
        snapshot.spec.model = "mutated"
    with pytest.raises(FrozenInstanceError):
        snapshot.client = FakeClient(snapshot.spec)


def test_stage_uses_fresh_explicit_client_and_keeps_final_settings_and_identity():
    created = []
    def factory(spec):
        created.append(FakeClient(spec))
        return created[-1]
    snapshot = capture(factory=factory)
    identity = snapshot.identity
    snapshot.complete_stage("plan", max_tokens=512, timeout=7.5, thinking=True)
    snapshot.client.complete("final")
    assert len(created) == 2 and created[0] is not created[1]
    assert created[1].spec.settings == {**created[0].spec.settings, "output_token_limit": 512,
                                        "timeout": 7.5, "thinking": True, "require_complete_output": True,
                                        "stage_purpose": "evidence-review", "reasoning_effort": "low"}
    assert created[1].spec.settings["context_window"] == 8192
    assert created[1].spec.settings["seed"] == 42
    assert created[1].calls[0][0] == "plan" and created[0].calls[0][0] == "final"
    assert snapshot.spec.settings["output_token_limit"] == -1 and snapshot.identity == identity


def test_concurrent_stage_budgets_and_final_snapshot_do_not_mutate_each_other():
    captured = []
    def factory(spec):
        captured.append(spec)
        return FakeClient(spec)
    snapshot = capture(factory=factory)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda budget: snapshot.complete_stage("plan", budget, budget / 10), (256, 768)))
    assert {(spec.settings["output_token_limit"], spec.settings["timeout"]) for spec in captured[1:]} == {
        (256, 25.6), (768, 76.8)}
    assert captured[0].settings["output_token_limit"] == -1


@pytest.mark.parametrize("budget,timeout", [(0, 1), (True, 1), (1.5, 1), (1, 0), (1, True), (1, float("inf"))])
def test_invalid_stage_settings_never_create_another_client(budget, timeout):
    created = []
    snapshot = capture(factory=lambda spec: created.append(spec) or FakeClient(spec))
    with pytest.raises(RuntimeConfigError):
        snapshot.complete_stage("stage", budget, timeout)
    assert len(created) == 1


@pytest.mark.parametrize("raw", [{}, {"done": False}, {"done": True, "done_reason": "length"},
                                  {"done": True}, {"done": True, "done_reason": "unknown"}])
def test_ollama_stage_requires_real_normal_termination_not_just_parseable_text(raw):
    class Incomplete(FakeClient):
        def complete(self, prompt, **kwargs):
            return SimpleNamespace(text='{"valid":"json"}', raw=raw)
    snapshot = capture(factory=Incomplete)
    with pytest.raises(RuntimeInvocationError, match="did not finish completely"):
        snapshot.complete_stage("stage", 512, 5)
    assert snapshot.client.complete("final").text == '{"valid":"json"}'


def test_producer_repr_and_public_serialization_never_include_private_material():
    config = cloud(folder_id="synthetic-private-folder")
    snapshot = capture(config)
    producer = snapshot.producer()
    assert producer == {"provider": "openai", "model": "same-model", "label": "openai · same-model",
                        "identity_kind": "request_client_snapshot"}
    outputs = [repr(snapshot), repr(snapshot.spec), repr(snapshot.client),
               json.dumps(producer), json.dumps(snapshot.model_dump()),
               json.dumps(snapshot.spec.model_dump()), json.dumps(snapshot.client.model_dump()),
               json.dumps(asdict(snapshot.spec), default=str)]
    for output in outputs:
        assert "synthetic-private" not in output and "cloud.invalid" not in output
        assert "model_digest" not in output


def test_transport_and_factory_errors_are_sanitized_without_chained_private_details():
    private = "synthetic-private-credential https://cloud.invalid/v1"
    def broken_factory(spec):
        raise RuntimeError(private)
    with pytest.raises(RuntimeConfigError) as error:
        capture(cloud(), factory=broken_factory)
    assert private not in str(error.value) and error.value.__suppress_context__
    class Broken(FakeClient):
        def complete(self, prompt, **kwargs):
            raise RuntimeError(private)
    snapshot = capture(cloud(), factory=Broken)
    with pytest.raises(RuntimeInvocationError) as error:
        snapshot.client.complete("prompt")
    assert private not in str(error.value) and error.value.__suppress_context__


@pytest.mark.parametrize("endpoint", ["file:///private", "https://user:password@example.invalid",
                                       "https://example.invalid?api_key=private", "http://host:bad"])
def test_invalid_endpoint_is_rejected_with_value_free_error(endpoint):
    with pytest.raises(RuntimeConfigError) as error:
        capture(cloud(base_url=endpoint))
    assert endpoint not in str(error.value)


def test_default_factory_wires_custom_ollama_host_and_all_effective_settings(monkeypatch):
    import ollama
    import provider_runtime
    seen = {}
    def native_client(**kwargs):
        seen["native"] = kwargs
        return object()
    def llm(**kwargs):
        seen["llm"] = kwargs
        return FakeClient(None)
    monkeypatch.setattr(ollama, "Client", native_client)
    monkeypatch.setattr(provider_runtime, "_ollama_type", lambda: llm)
    snapshot = capture_runtime({"provider": "ollama", "model": "same-model", "base_url": "http://custom.invalid:99",
                                "temperature": 0.5, "top_p": 0.8, "top_k": 12},
                               default_model="fallback", ollama_base_url="http://default.invalid",
                               ollama_profiles=PROFILES, default_ollama_profile=DEFAULT_PROFILE, environ={})
    assert seen["native"] == {"host": "http://custom.invalid:99", "timeout": 900.0, "trust_env": False}
    assert seen["llm"]["base_url"] == snapshot.spec.endpoint == "http://custom.invalid:99"
    assert seen["llm"]["additional_kwargs"] == {"seed": 42, "num_predict": -1, "top_p": 0.8, "top_k": 12}
    assert seen["llm"]["temperature"] == 0.5 and seen["llm"]["context_window"] == 8192
    assert seen["llm"]["thinking"] is False


def test_default_cloud_factory_keeps_supported_defaults_and_stage_guard(monkeypatch):
    import llm_providers
    created = []
    def factory(**kwargs):
        created.append(kwargs)
        return FakeClient(None)
    monkeypatch.setattr(llm_providers, "ProviderLLM", factory)
    for provider, defaults in llm_providers.DEFAULTS.items():
        snapshot = capture_runtime({"provider": provider, "api_key": "synthetic-private-key"},
                                   default_model="local", ollama_base_url="http://local.invalid",
                                   ollama_profiles=PROFILES, default_ollama_profile=DEFAULT_PROFILE, environ={})
        assert created[-1]["model"] == defaults["model"] and created[-1]["base_url"] == defaults["base_url"]
        snapshot.complete_stage("plan", 500, 7)
        assert created[-1]["require_complete_output"] is True
        assert created[-1]["output_token_limit"] == 500 and created[-1]["timeout"] == 7


def test_capture_with_injected_environment_never_reads_process_environment(monkeypatch):
    import provider_runtime
    class NoEnvironment(dict):
        def get(self, *args):
            raise AssertionError("process environment read")
    monkeypatch.setattr(provider_runtime.os, "environ", NoEnvironment())
    assert capture(environ={}).spec.model == "same-model"


def test_llama_index_boundary_uses_request_owned_native_client_not_global_selection():
    created = []
    def factory(spec):
        created.append(FakeClient(spec))
        return created[-1]
    first, second = capture(factory=factory), capture(factory=factory)
    assert first.llama_index_client is created[0] and second.llama_index_client is created[1]
    assert first.llama_index_client is not second.llama_index_client


@pytest.mark.parametrize("value", [{"private": "credential"}, ["low"], 1, float("nan")])
def test_invalid_thinking_is_value_free_configuration_error(value):
    with pytest.raises(RuntimeConfigError, match="invalid_runtime_thinking"):
        capture({"provider": "ollama", "model": "same-model", "thinking": value})


def test_unused_cloud_options_are_rejected_not_reported_as_effective():
    with pytest.raises(RuntimeConfigError, match="unsupported_runtime_provider_setting"):
        capture({"provider": "yandex", "model": "model", "top_p": 0.5})
    with pytest.raises(RuntimeConfigError, match="unsupported_runtime_provider_setting"):
        capture(cloud(seed=42))


def test_cloud_adapter_repr_and_serialization_are_private_even_outside_facade():
    from llm_providers import ProviderLLM
    adapter = ProviderLLM(**cloud(folder_id="synthetic-private-folder"))
    for output in (repr(adapter), str(adapter), adapter.model_dump_json(), json.dumps(adapter.to_payload())):
        assert "synthetic-private" not in output and "cloud.invalid" not in output


@pytest.mark.parametrize("oauth", [False, True])
@pytest.mark.parametrize("transport", [False, True])
def test_provider_http_transport_and_response_failures_do_not_echo_private_bodies(monkeypatch, oauth, transport):
    import llm_providers
    private = "synthetic-private-credential https://cloud.invalid/v1"
    class Response:
        ok = False
        status_code = 401
        text = private
        def json(self):
            return {"echo": private}
    def post(*args, **kwargs):
        if transport:
            raise llm_providers.requests.ConnectionError(private)
        return Response()
    monkeypatch.setattr(llm_providers.requests, "post", post)
    monkeypatch.setattr(llm_providers, "gigachat_ca_bundle", lambda: True)
    adapter = llm_providers.ProviderLLM(**cloud(provider="gigachat" if oauth else "openai"))
    with pytest.raises(RuntimeError) as error:
        adapter.complete("test")
    assert "synthetic-private" not in str(error.value) and "cloud.invalid" not in str(error.value)


def test_real_ollama_adapter_retains_native_termination_and_explicit_stage_options(monkeypatch):
    import ollama
    native_calls = []
    clients = []
    class Native:
        def __init__(self, **kwargs):
            clients.append(kwargs)
        def chat(self, **kwargs):
            native_calls.append(kwargs)
            return {"model": "same-model", "message": {"role": "assistant", "content": '{"ok":true}'},
                    "done": True, "done_reason": "stop"}
    monkeypatch.setattr(ollama, "Client", Native)
    stage_calls = stage_http(monkeypatch, {"model": "same-model", "response": '{"ok":true}',
                                         "done": True, "done_reason": "stop"})
    snapshot = capture_runtime({"provider": "ollama", "model": "same-model", "base_url": "http://custom.invalid:99"},
        default_model="fallback", ollama_base_url="http://default.invalid", ollama_profiles=PROFILES,
        default_ollama_profile=DEFAULT_PROFILE, environ={})
    result = snapshot.complete_stage("plan", 700, 12, thinking=True)
    assert result.raw["done"] is True and result.raw["done_reason"] == "stop"
    assert str(result) == '{"ok":true}'
    snapshot.client.complete("final")
    assert len(clients) == 1 and clients[0] == {"host": "http://custom.invalid:99", "timeout": 900.0, "trust_env": False}
    assert stage_calls[0]["json"]["think"] is True and native_calls[0]["think"] is False
    assert stage_calls[0]["timeout"] == 12.0
    assert stage_calls[0]["json"]["options"] == {"temperature": 0.0, "num_ctx": 8192, "seed": 42,
                                                   "num_predict": 700, "reasoning_effort": "low"}
    assert native_calls[0]["options"] == {"temperature": 0.0, "num_ctx": 8192, "seed": 42, "num_predict": -1}
    assert result.additional_kwargs["termination_observed"] is True
    assert snapshot.client.metadata.context_window == 8192 and snapshot.client.metadata.num_output == 2048
    # This is the genuine LlamaIndex BaseLLM passed through resolve_llm, not
    # just a facade dump. Its serialization cannot expose the transport URL.
    from llama_index.llms.ollama import Ollama
    native = snapshot.llama_index_client
    assert isinstance(native, Ollama)
    for serialized in (repr(native), str(native), native.model_dump_json(), json.dumps(native.to_payload())):
        assert "custom.invalid" not in serialized and "http://" not in serialized
    assert native.base_url == "http://custom.invalid:99"  # Explicit transport access still works.


@pytest.mark.parametrize("provider", ["openai", "deepseek", "gemini", "gigachat", "yandex"])
def test_real_cloud_factory_preserves_final_and_stage_wire_budgets_and_metadata(monkeypatch, provider):
    import llm_providers
    sent = []
    class Response:
        ok = True
        def json(self):
            return {"choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                    "access_token": "synthetic-token", "expires_at": 9999999999000,
                    "result": {"alternatives": [{"message": {"text": "answer"},
                                                   "status": "ALTERNATIVE_STATUS_FINAL"}]}}
    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()
    monkeypatch.setattr(llm_providers.requests, "post", post)
    monkeypatch.setattr(llm_providers, "gigachat_ca_bundle", lambda: True)
    config = cloud(provider=provider, folder_id="synthetic-private-folder", output_token_limit=2300,
                   **({"top_p": 0.85} if provider != "yandex" else {}))
    snapshot = capture_runtime(config, default_model="local", ollama_base_url="http://local.invalid",
                               ollama_profiles=PROFILES, default_ollama_profile=DEFAULT_PROFILE, environ={})
    snapshot.complete_stage("plan", 500, 12)
    stage = sent[-1][1]
    assert snapshot.client.metadata.num_output == snapshot.llama_index_client.metadata.num_output == 2300
    snapshot.client.complete("final")
    final = sent[-1][1]
    if provider == "yandex":
        assert stage["json"]["completionOptions"]["maxTokens"] == 500
        assert final["json"]["completionOptions"]["maxTokens"] == 2300
    else:
        assert stage["json"]["max_tokens"] == 500 and final["json"]["max_tokens"] == 2300
        assert stage["json"]["top_p"] == final["json"]["top_p"] == 0.85
    assert stage["timeout"] == 12 and final["timeout"] == 900
    assert snapshot.llama_index_client.require_complete_output is False


def stage_http(monkeypatch, body, *, error=None, status=200):
    """Fake the entire network boundary; no actual endpoint is opened."""
    import requests
    sent = []
    class Response:
        status_code = status
        def raise_for_status(self):
            if error:
                raise requests.HTTPError(error)
        def json(self):
            return body
    class Session:
        trust_env = True
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return None
        def post(self, url, **kwargs):
            sent.append({"url": url, "trust_env": self.trust_env, **kwargs})
            return Response()
    monkeypatch.setattr(requests, "Session", Session)
    return sent


def production_ollama_capture(config=None):
    return capture_runtime(config or {"provider": "ollama", "model": "same-model", "base_url": "http://custom.invalid:99"},
        default_model="fallback", ollama_base_url="http://default.invalid", ollama_profiles=PROFILES,
        default_ollama_profile=DEFAULT_PROFILE, environ={})


@pytest.mark.parametrize("purpose,wire_purpose", [("planning", "planning"), ("evidence-review", "evidence-review"),
                                                  ("source-reading", "source-reading"),
                                                  ("source-navigation", "planning")])
def test_bridge_stage_purpose_transport_preserves_legacy_contract_without_inventing_stop(monkeypatch, purpose, wire_purpose):
    body = {"model": "same-model", "response": '{"ok":true}', "done": True, "thinking_enabled": False}
    sent = stage_http(monkeypatch, body)
    snapshot = production_ollama_capture()
    identity = snapshot.identity
    result = snapshot.complete_stage("stage", 700, 12, thinking=False, purpose=purpose)
    assert result.raw == body and "done_reason" not in result.raw
    assert result.additional_kwargs == {"stage_protocol": "purpose_generate", "stage_purpose": purpose,
                                         "termination_observed": False}
    assert sent == [{"url": "http://custom.invalid:99/api/generate", "trust_env": False, "timeout": 12.0,
                     "allow_redirects": False,
                     "json": {"model": "same-model", "prompt": "stage", "stream": False, "think": False,
                              "purpose": wire_purpose,
                              "options": {"temperature": 0.0, "seed": 42, "num_predict": 700,
                                          "num_ctx": 8192, "reasoning_effort": "low"}}}]
    assert snapshot.identity == identity and snapshot.spec.settings["output_token_limit"] == -1


@pytest.mark.parametrize("body", [
    {"response": "", "done": True}, {"response": " \n", "done": True},
    {"response": '{"ok":true}', "done": False},
    {"response": '{"ok":true}', "done": True, "done_reason": "length"},
    {"response": '{"ok":true}', "done": True, "done_reason": "unknown"},
    {"response": {"invented": "text"}, "done": True},
])
def test_bridge_empty_truncated_and_native_incomplete_stage_responses_reject(monkeypatch, body):
    sent = stage_http(monkeypatch, body)
    snapshot = production_ollama_capture()
    with pytest.raises(RuntimeInvocationError):
        snapshot.complete_stage("stage", 700, 12, purpose="planning")
    assert len(sent) == 1 and sent[0]["json"]["purpose"] == "planning"


def test_bridge_stage_http_error_is_private_and_never_accepted_as_generation(monkeypatch):
    private = "http://custom.invalid synthetic-private-upstream-body"
    stage_http(monkeypatch, {"response": "not usable", "done": True}, error=private)
    snapshot = production_ollama_capture()
    with pytest.raises(RuntimeInvocationError) as error:
        snapshot.complete_stage("stage", 500, 7, purpose="planning")
    assert "custom.invalid" not in str(error.value) and "synthetic-private" not in str(error.value)


def test_unrecognized_stage_purpose_cannot_disable_bridge_truncation_guard(monkeypatch):
    sent = stage_http(monkeypatch, {"response": "answer", "done": True})
    snapshot = production_ollama_capture()
    with pytest.raises(RuntimeConfigError):
        snapshot.complete_stage("stage", 500, 7, purpose="unprotected-purpose")
    assert not sent


@pytest.mark.parametrize("update,remove", [
    ({}, "model"), ({"model": "wrong-model"}, None), ({"model": None}, None),
    ({}, "thinking_enabled"), ({"thinking_enabled": "false"}, None),
    ({"thinking_enabled": 0}, None), ({"thinking_enabled": None}, None),
])
def test_legacy_missing_reason_requires_exact_known_bridge_shape(monkeypatch, update, remove):
    body = {"model": "same-model", "response": '{"ok":true}', "done": True, "thinking_enabled": False}
    body.update(update)
    if remove:
        body.pop(remove)
    stage_http(monkeypatch, body)
    with pytest.raises(RuntimeInvocationError, match="did not finish completely"):
        production_ollama_capture().complete_stage("stage", 500, 7, purpose="planning")


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 500])
def test_pinned_stage_endpoint_never_follows_or_accepts_redirects(monkeypatch, status):
    body = {"model": "same-model", "response": '{"ok":true}', "done": True, "thinking_enabled": False}
    sent = stage_http(monkeypatch, body, status=status)
    with pytest.raises(RuntimeInvocationError):
        production_ollama_capture().complete_stage("stage", 500, 7, purpose="planning")
    assert len(sent) == 1 and sent[0]["allow_redirects"] is False


def cloud_content_adapter(monkeypatch, provider, content):
    """Exercise real cloud extractors against synthetic HTTP response bodies."""
    import llm_providers
    sent = []
    class Response:
        ok = True
        def json(self):
            return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                    "result": {"alternatives": [{"message": {"text": content},
                                                   "status": "ALTERNATIVE_STATUS_FINAL"}]}}
    def post(url, **kwargs):
        sent.append((url, kwargs))
        return Response()
    monkeypatch.setattr(llm_providers.requests, "post", post)
    monkeypatch.setattr(llm_providers, "gigachat_ca_bundle", lambda: True)
    adapter = llm_providers.ProviderLLM(**cloud(provider=provider, folder_id="synthetic-private-folder",
                                               timeout=5, output_token_limit=-1))
    # Only completion HTTP is relevant here; do not request a real OAuth token.
    adapter._giga_token = "synthetic-private-token"
    adapter._giga_token_expires = 9999999999
    return adapter, sent


@pytest.mark.parametrize("provider", ["openai", "gigachat", "yandex"])
@pytest.mark.parametrize("content", [None, "", " \n\t", ["plausible but not text"], {"text": "not a string"}])
def test_cloud_completion_rejects_invalid_text_before_response_or_cache_can_use_it(monkeypatch, provider, content):
    adapter, sent = cloud_content_adapter(monkeypatch, provider, content)
    with pytest.raises(RuntimeError) as error:
        adapter.complete("final")
    assert str(error.value) == "LLM response has no usable text content"
    assert "cloud.invalid" not in str(error.value) and "synthetic-private" not in str(error.value)
    assert len(sent) == 1


@pytest.mark.parametrize("provider", ["openai", "gigachat", "yandex"])
def test_cloud_completion_preserves_valid_whitespace_padded_string_verbatim(monkeypatch, provider):
    content = " \tПодтверждённый ответ.\n "
    adapter, sent = cloud_content_adapter(monkeypatch, provider, content)
    response = adapter.complete("final")
    assert type(response.text) is str and response.text == content
    assert str(response) == content and len(sent) == 1
