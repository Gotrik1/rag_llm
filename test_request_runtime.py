"""Real Handler/agent/ARQ seams, with fake clients and no services."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, local
from types import SimpleNamespace
import asyncio
import json
from unittest.mock import Mock

import pytest

import rag_agent
import web_ui

REAL_LOAD_CONFIG = web_ui.load_provider_config


ITEM = {"file": "global.md", "section_path": "Rabel", "score": 1.0,
        "text": "The Rabel node stores a named value.", "status": "active"}
QUESTION = "Describe the Rabel node."


class MemoryCache:
    def __init__(self):
        self.data = {}
        self.reads = []
        self.writes = []

    def get(self, key):
        self.reads.append(key)
        return deepcopy(self.data.get(key))

    def set(self, key, value):
        self.writes.append(key)
        self.data[key] = deepcopy(value)

    def clear(self):
        self.data.clear()


class FakeClient:
    def __init__(self, provider, model, callback=None):
        self.provider, self.model = provider, model
        self.callback = callback
        self.metadata = SimpleNamespace(context_window=16384)
        self.calls = []

    def complete(self, prompt, formatted=False):
        self.calls.append(prompt)
        if self.callback:
            self.callback(self)
        return f"Request produced by {self.provider} {self.model}."


@pytest.fixture
def service(monkeypatch):
    state = {"provider": "openai", "model": "model-A", "base_url": "https://a.invalid/v1",
             "api_key": "synthetic-credential-A"}
    clients, reads = [], []

    def read_config():
        reads.append(dict(state))
        return dict(state)

    def factory(spec):
        client = FakeClient(spec.provider, spec.model)
        clients.append(client)
        return client

    agent = object.__new__(rag_agent.RAGAgent)
    agent.llm_model = "model-A"
    agent.llm = FakeClient("openai", "model-A")
    agent.response_cache = MemoryCache()
    agent._generation_state = local()
    agent._all_nodes = [object()]
    agent.index = object()
    agent._docstore_mtime_ns = 7
    agent._runtime_config_reader = read_config
    agent._runtime_client_factory = factory
    agent._refresh_docstore_if_changed = lambda: None
    agent.retrieve_context = lambda *args, **kwargs: [dict(ITEM)]

    def legacy_set(config):
        agent.llm = FakeClient(config["provider"], config["model"])
        agent.llm_model = config["model"]

    agent.set_llm_provider = legacy_set
    monkeypatch.setattr(web_ui, "_agent", None)
    monkeypatch.setattr(web_ui, "RAGAgent", lambda: agent)
    monkeypatch.setattr(web_ui, "load_provider_config", read_config)
    monkeypatch.setattr(web_ui, "load_flow_mode", lambda: "python")
    monkeypatch.setattr(web_ui, "load_system_prompt_id", lambda: "test")
    monkeypatch.setattr(web_ui, "get_profile", lambda *args: {"id": "test", "prompt": "Use only sources."})
    monkeypatch.setattr(web_ui, "get_project_conversation", lambda chat: {
        "project_name": "Synthetic", "memory": {}, "messages": [], "active_user_messages": []})
    monkeypatch.setattr(web_ui, "retrieval_document_scope", lambda *args: (set(), {"global.md": "global.md"}))
    monkeypatch.setattr(web_ui, "shared_global_document_sources", lambda: {"global.md"})
    monkeypatch.setattr(web_ui, "validate_retrieval", lambda *args: [])
    monkeypatch.setenv("RAG_QUERY_UNDERSTANDING", "off")
    monkeypatch.setenv("RAG_CORRECTIVE_PIPELINE", "off")
    monkeypatch.setenv("RAG_SECRET_KEY", "synthetic-server-key-for-tests-only-32-bytes")

    def ask(chat="chat-1"):
        handler = object.__new__(web_ui.Handler)
        result = {}
        handler.send_json = lambda value, status=200: result.update(value=value, status=status)
        handler.handle_ask({"question": QUESTION, "chat_id": chat})
        assert result["status"] == 200
        return result["value"]

    return SimpleNamespace(state=state, agent=agent, clients=clients, reads=reads, ask=ask,
                           factory=factory, read_config=read_config)


def test_actual_warmed_get_agent_rereads_config_and_labels_request_producer(service):
    warmed = web_ui.get_agent()
    index = warmed.index
    first = service.ask()
    service.state.update(provider="deepseek", model="model-B", base_url="https://b.invalid/v1",
                         api_key="synthetic-credential-B")
    second = service.ask("chat-2")
    assert web_ui.get_agent() is warmed and warmed.index is index
    assert "openai model-A" in first["answer"]
    assert first["llm"]["model"] == "model-A"
    assert "deepseek model-B" in second["answer"]
    assert second["llm"]["provider"] == "deepseek" and second["llm"]["model"] == "model-B"
    assert len(service.reads) == 2


def test_actual_handler_label_does_not_reread_saved_config_after_generation(service):
    web_ui.get_agent()

    def changed_midflight(client):
        service.state.update(provider="deepseek", model="model-B", base_url="https://b.invalid/v1")

    service.agent.llm.callback = changed_midflight
    original_factory = service.agent._runtime_client_factory

    def factory(spec):
        client = original_factory(spec)
        client.callback = changed_midflight
        return client

    service.agent._runtime_client_factory = factory
    result = service.ask()
    assert "openai model-A" in result["answer"]
    assert result["llm"]["provider"] == "openai" and result["llm"]["model"] == "model-A"


def test_concurrent_actual_handlers_pin_a_b_without_a_generation_lock(service):
    entered, release = Event(), Event()
    original_factory = service.agent._runtime_client_factory

    def factory(spec):
        client = original_factory(spec)
        if spec.model == "model-A":
            def block(_client):
                entered.set()
                assert release.wait(5), "A was never released"
            client.callback = block
        return client

    service.agent._runtime_client_factory = factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.ask)
        try:
            assert entered.wait(3)
            service.state.update(provider="deepseek", model="model-B", base_url="https://b.invalid/v1")
            second = pool.submit(service.ask, "chat-2").result(timeout=3)
            assert "deepseek model-B" in second["answer"]
            assert second["llm"]["model"] == "model-B"
            assert not first.done()
        finally:
            release.set()
        original = first.result(timeout=3)
    assert "openai model-A" in original["answer"] and original["llm"]["model"] == "model-A"
    assert len(service.reads) == 2


@pytest.mark.parametrize("change", [
    {"provider": "deepseek"}, {"model": "model-B"}, {"base_url": "https://b.invalid/v1"},
    {"temperature": 0.25}, {"top_p": 0.8}, {"api_key": "synthetic-key-rotated"},
    {"folder_id": "different-account-scope"}, {"auth_type": "iam"},
])
def test_actual_answer_cache_partitions_effective_runtime(service, change):
    first = service.ask()
    assert not first["from_cache"]
    service.state.update(change)
    second = service.ask("chat-2")
    assert not second["from_cache"]
    assert len(service.agent.response_cache.data) == 2
    assert len(service.agent.response_cache.writes) == 2


def test_actual_cache_hit_carries_original_producer_and_rejects_legacy_or_foreign_values(service):
    first = service.ask()
    cache = service.agent.response_cache
    key = cache.writes[-1]
    original = deepcopy(cache.data[key])
    second = service.ask("chat-2")
    assert second["from_cache"] and second["llm"] == first["llm"]
    assert service.clients[-1].calls == []
    for invalid in [
        {"answer": "legacy answer-only"},
        {**original, "runtime_identity": "foreign-snapshot"},
        {**original, "producer": {**original["producer"], "model": "wrong-model"}},
    ]:
        cache.data[key] = invalid
        result = service.ask()
        assert not result["from_cache"] and "model-A" in result["answer"]
        assert result["llm"]["model"] == "model-A"


def test_actual_cache_still_contains_full_prompt_and_corpus_revision(service, monkeypatch):
    service.ask()
    service.agent._docstore_mtime_ns = 8
    assert not service.ask()["from_cache"]
    monkeypatch.setattr(web_ui, "get_profile", lambda *args: {"id": "test", "prompt": "Different exact policy."})
    assert not service.ask()["from_cache"]
    assert len(service.agent.response_cache.data) == 3


@pytest.mark.parametrize("private,followup", [(True, False), (False, True)])
def test_actual_handler_preserves_private_and_contextual_answer_cache_bypass(service, monkeypatch, private, followup):
    if private:
        service.agent.retrieve_context = lambda *args, **kwargs: [{**ITEM, "file": "private.md"}]
    if followup:
        monkeypatch.setattr(web_ui, "contextualize_retrieval_question", lambda *args: "Earlier user context. " + QUESTION)
    result = service.ask()
    assert not result["from_cache"]
    assert service.agent.response_cache.reads == [] and service.agent.response_cache.writes == []


def test_acl_denial_does_not_capture_or_generate(service, monkeypatch):
    monkeypatch.setattr(web_ui, "get_project_conversation", lambda chat: None)
    handler = object.__new__(web_ui.Handler)
    result = {}
    handler.send_json = lambda value, status=200: result.update(value=value, status=status)
    handler.handle_ask({"question": QUESTION, "chat_id": "not-visible"})
    assert result["status"] == 404 and service.clients == [] and service.reads == []


def test_corrective_planner_retriever_writer_judge_use_one_snapshot(service, monkeypatch):
    import corrective_rag
    seen_retrieval = []
    original_factory = service.agent._runtime_client_factory

    def factory(spec):
        client = original_factory(spec)
        client.callback = lambda _client: service.state.update(
            provider="deepseek", model="model-B", base_url="https://b.invalid/v1")
        return client

    def retrieve(*args, **kwargs):
        seen_retrieval.append(kwargs["runtime"])
        return [{**ITEM, "evidence_id": "E1"}]

    def pipeline(question, search_question, *, complete, retrieve, **kwargs):
        complete("planner prompt", "planning")
        retrieve(question, [question], [], {}, search_focus=None, constraint_question=question)
        complete("writer prompt", "answer")
        complete("judge prompt", "evidence-review")
        return SimpleNamespace(context=[{**ITEM, "evidence_id": "E1"}], trace={},
                               answer="The Rabel node stores a value. [E1]")

    service.agent._runtime_client_factory = factory
    service.agent.retrieve_context = retrieve
    monkeypatch.setenv("RAG_CORRECTIVE_PIPELINE", "on")
    monkeypatch.setenv("RAG_PLANNER_THINKING", "true")  # Ollama-only option must not break a cloud snapshot.
    monkeypatch.setattr(corrective_rag, "run_corrective", pipeline)
    result = service.ask()
    assert result["llm"]["provider"] == "openai" and result["llm"]["model"] == "model-A"
    assert len(service.clients) == 3
    assert all(client.model == "model-A" and client.provider == "openai" for client in service.clients)
    assert seen_retrieval[0].spec.model == "model-A"
    assert len(service.reads) == 1


@pytest.mark.parametrize("mode", ["rust", "hybrid"])
def test_real_rust_selected_evidence_uses_same_pinned_python_adapter(service, monkeypatch, mode):
    monkeypatch.setattr(web_ui, "load_flow_mode", lambda: mode)
    monkeypatch.setattr(web_ui, "rust_context", lambda question: [])
    native = Mock(side_effect=AssertionError("native generator lacks full runtime snapshot"))
    monkeypatch.setattr(web_ui, "_run_rust_json", native)
    service.state.update(provider="ollama", model="same-alias", base_url="http://custom.invalid:11434")
    first = service.ask()
    second = service.ask("chat-2")
    assert "ollama same-alias" in first["answer"]
    assert first["llm"]["generation_adapter"] == "python"
    assert second["from_cache"] and second["llm"] == first["llm"]
    assert len(service.reads) == 2
    native.assert_not_called()


def test_arq_executes_current_snapshot_and_persists_original_producer(service, monkeypatch):
    import asgi_app
    import generation_worker
    store = Mock()
    store.get_job.return_value = {"cancel_requested": False}
    persisted = []
    monkeypatch.setattr(generation_worker, "get_store", lambda: store)
    monkeypatch.setattr(generation_worker, "_persist_message", lambda mid, data: persisted.append(deepcopy(data)))
    monkeypatch.setattr(asgi_app, "legacy_operation", lambda method, path, body: (service.ask(body["chat_id"]), 200))
    payload = {"assistant_message_id": "message-1", "request": {"question": QUESTION, "chat_id": "chat-1"}}
    web_ui.get_agent()  # A worker process was already warmed with A before execution.
    service.state.update(provider="deepseek", model="model-B", base_url="https://b.invalid/v1")
    result = asyncio.run(generation_worker.generate_answer_job({}, "job-1", payload))
    assert "deepseek model-B" in result["answer"]
    assert persisted[-1]["metadata"]["llm"] == result["llm"]
    assert persisted[-1]["metadata"]["model_label"] == result["llm"]["label"]
    assert len(service.reads) == 1


def test_arq_pre_cancel_preserves_no_capture_or_generation(service, monkeypatch):
    import generation_worker
    store = Mock()
    store.get_job.return_value = {"cancel_requested": True}
    monkeypatch.setattr(generation_worker, "get_store", lambda: store)
    monkeypatch.setattr(generation_worker, "_persist_message", lambda *args: None)
    result = asyncio.run(generation_worker.generate_answer_job({}, "job-cancelled", {
        "assistant_message_id": "message-1", "request": {"question": QUESTION}}))
    assert result["cancelled"] and service.clients == [] and service.reads == []


def test_request_result_and_cache_do_not_expose_auth_endpoint_or_fake_digest(service):
    service.state["model_digest"] = "operator-supplied-unverified-digest"
    result = service.ask()
    public = json.dumps(result)
    private_cache = json.dumps(service.agent.response_cache.data)
    for secret in [service.state["api_key"], service.state["base_url"], service.state["model_digest"]]:
        assert secret not in public and secret not in private_cache
    assert result["llm"]["identity_kind"] == "request_client_snapshot"


def test_actual_persisted_config_is_reread_in_two_warmed_agent_instances(service, monkeypatch, tmp_path):
    from copy import copy
    path = tmp_path / "llm_providers.json"
    monkeypatch.setattr(web_ui, "PROVIDERS_CONFIG_PATH", path)
    reads = []

    def reader():
        value = REAL_LOAD_CONFIG()
        reads.append(value)
        return value

    web_ui.save_provider_config(dict(service.state))
    monkeypatch.setattr(web_ui, "load_provider_config", reader)
    first_agent = web_ui.get_agent()
    second_agent = copy(first_agent)  # independent API/ARQ process-local agent state
    second_agent._generation_state = local()
    second_agent._runtime_config_reader = reader
    web_ui.save_provider_config({**service.state, "provider": "deepseek", "model": "model-B"})
    first = service.ask()
    second, _, _ = second_agent.ask(QUESTION, "Use sources", pipeline_mode="off")
    assert "deepseek model-B" in first["answer"] and "deepseek model-B" in second
    assert len(reads) == 2
    assert first_agent.index is second_agent.index
    assert list(tmp_path.iterdir()) == [path]


def test_missing_chat_uses_one_config_snapshot(service, monkeypatch):
    monkeypatch.setattr(web_ui, "get_workspace", lambda *args: {"active_chat_id": "chat-1"})
    handler = object.__new__(web_ui.Handler)
    result = {}
    handler.send_json = lambda value, status=200: result.update(value=value, status=status)
    handler.handle_ask({"question": QUESTION})
    assert result["status"] == 200 and result["value"]["llm"]["model"] == "model-A"
    assert len(service.reads) == 1


def test_model_and_provider_save_persist_without_mutating_warmed_agent(service, monkeypatch):
    captured, saved, endpoints = {}, [], []
    handler = object.__new__(web_ui.Handler)
    handler.send_json = lambda value, status=200: captured.update(value=value, status=status)
    monkeypatch.setattr(web_ui, "save_provider_config", lambda value: saved.append(deepcopy(value)))
    monkeypatch.setattr(web_ui, "ollama_models", lambda base_url=None: endpoints.append(base_url) or ["model-A"])
    monkeypatch.setattr(web_ui, "get_agent", Mock(side_effect=AssertionError("save must not mutate any agent")))
    service.state.update(provider="ollama", base_url="http://custom.invalid:11434")
    handler.handle_model_select({"model": "model-A"})
    assert saved[-1]["base_url"] == "http://custom.invalid:11434"
    handler.handle_provider_save({"provider": "ollama", "model": "model-A",
                                  "base_url": "http://second.invalid:11434"})
    assert saved[-1]["base_url"] == "http://second.invalid:11434"
    assert endpoints == ["http://custom.invalid:11434", "http://second.invalid:11434"]


def test_model_catalog_selection_reads_saved_config_not_warmed_default(service, monkeypatch):
    web_ui.get_agent()
    service.state.update(provider="ollama", model="model-B", base_url="http://custom.invalid:11434")
    handler = object.__new__(web_ui.Handler)
    captured = {}
    handler.send_json = lambda value, status=200: captured.update(value=value)
    models = Mock(return_value=["model-A", "model-B"])
    monkeypatch.setattr(web_ui, "ollama_models", models)
    handler.handle_models()
    assert captured["value"]["selected"] == "model-B"
    models.assert_called_once_with(base_url="http://custom.invalid:11434")


def test_real_same_name_cloud_to_ollama_setter_has_no_early_return_or_settings_swap(service, monkeypatch):
    original = service.agent.llm
    local_client = FakeClient("ollama", "model-A")
    monkeypatch.setattr(rag_agent.RAGAgent, "_create_ollama_llm", staticmethod(lambda model: local_client))
    monkeypatch.setattr(rag_agent, "Settings", object())
    service.agent.set_llm_model("model-A")
    assert service.agent.llm is local_client and service.agent.llm is not original


def test_query_understanding_runtime_cache_and_identity_are_not_answer_only(service, monkeypatch):
    monkeypatch.setenv("RAG_QUERY_UNDERSTANDING", "on")
    original_factory = service.agent._runtime_client_factory

    def factory(spec):
        client = original_factory(spec)
        client.complete = lambda prompt, formatted=False: json.dumps({
            "canonical_question": QUESTION, "facets": ["fact"], "retrieval_queries": [QUESTION]})
        return client

    service.agent._runtime_client_factory = factory
    first_runtime = service.agent.capture_runtime(dict(service.state))
    service.agent.understand_query(QUESTION, runtime=first_runtime)
    first_key = service.agent.response_cache.writes[-1]
    service.state["api_key"] = "rotated-private-credential"
    second_runtime = service.agent.capture_runtime(dict(service.state))
    service.agent.understand_query(QUESTION, runtime=second_runtime)
    assert service.agent.response_cache.writes[-1] != first_key
    assert all("producer" in item and "runtime_identity" in item
               for item in service.agent.response_cache.data.values())


def test_actual_handler_corrective_native_adapters_preserve_bridge_purpose_and_unknown_termination(service, monkeypatch):
    import corrective_rag
    import ollama
    import requests

    model, endpoint = "synthetic-native-model", "http://custom.invalid:11434"
    service.state.update(provider="ollama", model=model, base_url=endpoint)
    service.agent._runtime_client_factory = None  # use genuine core factory/adapters, fake only transport
    sent, native = [], []

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert self.trust_env is False
            sent.append((url, kwargs))
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {
                "model": model, "response": "complete internal JSON result", "done": True,
                "thinking_enabled": False})

    def chat(_client, **kwargs):
        native.append(kwargs)
        return {"model": model, "message": {"role": "assistant", "content": "complete writer result"},
                "done": True, "done_reason": "stop"}

    def pipeline(question, search_question, *, complete, **kwargs):
        for purpose in ("planning", "source-navigation", "evidence-review", "answer"):
            assert "complete" in complete("prompt for " + purpose, purpose)
        return SimpleNamespace(context=[{**ITEM, "evidence_id": "E1"}], trace={},
                               answer="The Rabel node stores a value. [E1]")

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr(ollama.Client, "chat", chat)
    monkeypatch.setattr(corrective_rag, "run_corrective", pipeline)
    monkeypatch.setenv("RAG_CORRECTIVE_PIPELINE", "on")
    monkeypatch.setenv("RAG_PLANNER_THINKING", "false")
    monkeypatch.setenv("RAG_PLANNER_TOKEN_BUDGET", "321")
    monkeypatch.setenv("RAG_PLANNER_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("RAG_PLANNER_BASE_URL", "http://must-not-be-used.invalid")
    result = service.ask()
    assert [payload[1]["json"]["purpose"] for payload in sent] == ["planning", "planning", "evidence-review"]
    assert all(url == endpoint + "/api/generate" and options["allow_redirects"] is False
               and options["timeout"] == 7 and options["json"]["options"]["num_predict"] == 321
               and options["json"]["model"] == model for url, options in sent)
    events = result["retrieval_trace"]["completions"]
    assert [event["purpose"] for event in events] == ["planning", "source-navigation", "evidence-review", "answer"]
    assert all(event["termination_observed"] is False and event["termination_status"] == "unknown"
               and event["thinking_used"] is False for event in events[:3])
    assert len(native) == 1 and native[0]["model"] == model
    assert result["llm"]["model"] == model and len(service.reads) == 1


def test_mock_only_diagnostic_metadata_is_unknown_and_answer_only_cache_is_not_accepted():
    agent = object.__new__(rag_agent.RAGAgent)
    agent._generation_state = local()
    agent.response_cache = SimpleNamespace(get=lambda key: {"answer": "legacy answer only"})
    assert agent._producer(None) == {"provider": "unknown", "model": "unknown",
        "label": "unknown · unknown", "identity_kind": "injected_diagnostic_client"}
    with pytest.raises(RuntimeError, match="Generation client is unavailable"):
        agent._generate_grounded_answer(QUESTION, [dict(ITEM)], [], cache_enabled=True)
    assert not agent.last_generation_from_cache


@pytest.mark.parametrize("content", [None, "", " \t\r\n ", ["not textual content"], {"text": "not textual content"}])
def test_actual_handler_invalid_cloud_final_never_writes_answer_cache(service, monkeypatch, content):
    import llm_providers
    service.agent._runtime_client_factory = None  # genuine ProviderLLM extraction, fake HTTP only
    transport = Mock(return_value=SimpleNamespace(ok=True, status_code=200, json=lambda: {
        "choices": [{"finish_reason": "stop", "message": {"content": content}}]}))
    monkeypatch.setattr(llm_providers.requests, "post", transport)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            service.ask()
        assert not service.agent.last_generation_from_cache
    assert transport.call_count == 2
    assert service.agent.response_cache.writes == []
    assert service.agent.response_cache.data == {}


def test_rejected_legacy_entry_is_not_reported_as_a_cache_hit(service, monkeypatch):
    from contextlib import contextmanager
    service.ask()
    cache = service.agent.response_cache
    cache.data[cache.writes[-1]] = {"answer": "legacy answer only"}
    outcomes = {}

    @contextmanager
    def span(name, **kwargs):
        values = outcomes.setdefault(name, {})
        yield SimpleNamespace(set_attribute=lambda key, value: values.update({key: value}))

    monkeypatch.setattr(rag_agent, "_pipeline_span", span)
    assert not service.ask()["from_cache"]
    assert outcomes["cache.lookup"]["rag.cache.hit"] is False
