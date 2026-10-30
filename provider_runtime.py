"""Request-owned LLM clients resolved from one immutable effective configuration.

Capture performs no config-file, database, model-discovery or network I/O. The
caller supplies its saved config and defaults. Only the three documented env
defaults below are read, once, unless an environment mapping is injected.
An identity describes the requested client, not the remote model's weights.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
import hashlib
import hmac
import json
import math
import os
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit


# Also exported as llm_providers.DEFAULTS. Keeping data here lets injected-factory
# captures stay stdlib-only, without importing adapters and their env defaults.
PROVIDER_DEFAULTS = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash"},
    "gigachat": {"base_url": "https://gigachat.devices.sberbank.ru/api", "model": "GigaChat"},
    "yandex": {"base_url": "https://llm.api.cloud.yandex.net/foundationModels/v1", "model": "yandexgpt-lite"},
}


class RuntimeConfigError(ValueError):
    """Value-free configuration errors are safe for user-facing logs."""


class RuntimeInvocationError(RuntimeError):
    """Transport text may contain credentials/endpoints and is not propagated."""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise RuntimeConfigError("invalid_runtime_settings") from None


class _PrivateText:
    """Immutable opaque material, excluded even from generic dataclass dumps."""
    __slots__ = ("_value",)

    def __init__(self, value: str):
        object.__setattr__(self, "_value", value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Private runtime material is immutable")

    def __repr__(self) -> str:
        return "<private runtime material redacted>"

    __str__ = __repr__

    def __deepcopy__(self, memo: dict) -> _PrivateText:
        return self

    def __reduce_ex__(self, protocol: int) -> Any:
        raise TypeError("Private runtime material is not serializable")

    def reveal(self) -> str:
        """Expose private configuration only at an explicit transport boundary; never use this in diagnostics."""
        return self._value


@dataclass(frozen=True, repr=False)
class RuntimeSpec:
    """All values used by the client factory; mutable input objects are detached."""
    provider: str
    model: str
    settings_json: str = field(repr=False)
    _endpoint: _PrivateText = field(repr=False)
    _credential: _PrivateText = field(repr=False)
    _folder_id: _PrivateText = field(repr=False)
    _auth_type: _PrivateText = field(repr=False)
    _auth_scope: _PrivateText = field(repr=False)
    identity: str = field(repr=False)

    def __repr__(self) -> str:
        return f"<RuntimeSpec provider={self.provider!r} model={self.model!r} private_config=redacted>"

    @property
    def endpoint(self) -> str:
        """Reveal the validated private transport endpoint for client construction."""
        return self._endpoint.reveal()

    @property
    def api_key(self) -> str:
        """Reveal the detached credential for transport use; it is omitted from public model dumps."""
        return self._credential.reveal()

    @property
    def folder_id(self) -> str:
        """Reveal the provider-specific private folder binding for client construction."""
        return self._folder_id.reveal()

    @property
    def auth_type(self) -> str:
        """Return the pinned provider authentication mode without rereading saved configuration."""
        return self._auth_type.reveal()

    @property
    def settings(self) -> dict[str, Any]:
        """Deserialize a fresh settings copy so callers cannot mutate this immutable specification."""
        return json.loads(self.settings_json)

    def model_dump(self) -> dict[str, Any]:
        """Export provider, model and effective settings while withholding endpoint and authentication material."""
        return {"provider": self.provider, "model": self.model, "settings": self.settings}

    def with_settings(self, **updates: Any) -> RuntimeSpec:
        """Derive a new immutable specification and cache identity, retaining the original private auth binding."""
        settings_json = _json({**self.settings, **updates})
        return replace(self, settings_json=settings_json, identity=_identity(
            self.provider, self.model, self.endpoint, settings_json, self._auth_scope.reveal()))


def _text(value: Any, code: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise RuntimeConfigError(code)
    return value.strip()


def _endpoint(value: Any) -> str:
    value = _text(value, "missing_runtime_endpoint").rstrip("/")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and
                 parsed.username is None and parsed.password is None and not parsed.query and
                 not parsed.fragment and (parsed.port is None or 0 < parsed.port <= 65535))
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeConfigError("invalid_runtime_endpoint")
    return value


def _finite(value: Any, code: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise RuntimeConfigError(code)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RuntimeConfigError(code) from None
    if not math.isfinite(number) or (positive and number <= 0):
        raise RuntimeConfigError(code)
    return number


def _integer(value: Any, code: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeConfigError(code)
    try:
        number = int(value)
    except (ValueError, OverflowError):
        raise RuntimeConfigError(code) from None
    if positive and number <= 0:
        raise RuntimeConfigError(code)
    return number


def _identity(provider: str, model: str, endpoint: str, settings_json: str, auth_scope: str) -> str:
    material = {"schema": "provider-runtime/v1", "provider": provider, "model": model,
                "endpoint": endpoint, "settings": json.loads(settings_json), "auth_scope": auth_scope}
    return hashlib.sha256(_json(material).encode("utf-8")).hexdigest()


def _resolve_settings(config: Mapping[str, Any], *, provider: str, model: str,
                      profiles: Mapping[str, Mapping[str, Any]], default_profile: Mapping[str, Any],
                      environ: Mapping[str, str]) -> dict[str, Any]:
    profile = {**default_profile, **profiles.get(model, {})} if provider == "ollama" else {}
    # Preserve the existing agent's effective timeout: its env/default 900
    # overrides the historical per-model request_timeout entry (120/180).
    timeout = config.get("timeout", config.get("request_timeout", environ.get("LLM_REQUEST_TIMEOUT", "900")))
    output = config.get("output_token_limit", config.get("num_predict", environ.get(
        "RAG_MAX_OUTPUT_TOKENS", profile.get("num_predict", -1))))
    thinking = config.get("thinking", profile.get("thinking"))
    if thinking is not None and type(thinking) is not bool and (
            not isinstance(thinking, str) or thinking not in {"low", "medium", "high"}):
        raise RuntimeConfigError("invalid_runtime_thinking")
    seed = config.get("seed", profile.get("seed", 42 if provider == "ollama" else None))
    top_p = config.get("top_p", profile.get("top_p"))
    top_k = config.get("top_k", profile.get("top_k"))
    settings = {"temperature": _finite(config.get("temperature", profile.get("temperature", 0.0)),
                                        "invalid_runtime_temperature"),
                "timeout": _finite(timeout, "invalid_runtime_timeout", positive=True),
                "context_window": _integer(config.get("context_window", profile.get("context_window", 32768)),
                                           "invalid_runtime_context_window", positive=True),
                "thinking": thinking,
                "output_token_limit": _integer(output, "invalid_runtime_output_limit"),
                "seed": _integer(seed, "invalid_runtime_seed") if seed is not None else None,
                "top_p": _finite(top_p, "invalid_runtime_top_p") if top_p is not None else None,
                "top_k": _integer(top_k, "invalid_runtime_top_k", positive=True) if top_k is not None else None,
                "require_complete_output": False}
    if settings["output_token_limit"] == 0 or settings["output_token_limit"] < -1:
        raise RuntimeConfigError("invalid_runtime_output_limit")
    if settings["top_p"] is not None and not 0 < settings["top_p"] <= 1:
        raise RuntimeConfigError("invalid_runtime_top_p")
    # Cloud adapters do not implement Ollama-only options; reject rather than
    # claim that a requested setting was effective while dropping it on wire.
    if provider != "ollama" and any(settings[name] is not None for name in ("seed", "thinking", "top_k")):
        raise RuntimeConfigError("unsupported_runtime_provider_setting")
    if provider == "yandex" and settings["top_p"] is not None:
        raise RuntimeConfigError("unsupported_runtime_provider_setting")
    return settings


@lru_cache(maxsize=1)
def _ollama_type() -> type:
    """A real BaseLLM whose native observability is safe, imported lazily.

    LlamaIndex requires its concrete BaseLLM rather than the public facade.
    Exclusions therefore belong on the native model too, including nested
    Pydantic serialization. The actual endpoint remains available to transport.
    """
    from llama_index.llms.ollama import Ollama
    from pydantic import Field

    class RequestOllama(Ollama):
        base_url: str = Field(default="http://localhost:11434", exclude=True, repr=False)
        headers: dict[str, str] | None = Field(default=None, exclude=True, repr=False)

        def to_payload(self) -> dict[str, Any]:
            return {"class_name": self.class_name(), **self.metadata.model_dump()}

    return RequestOllama


@dataclass(frozen=True, repr=False)
class _OllamaStageClient:
    """Owned purpose-bearing generate transport for native Ollama and bridge.

    The deployed bridge drops finish metadata, but activates its historical
    truncation rejection only for declared internal purposes. This transport
    preserves that contract; it never fabricates a native stop observation.
    """
    spec: RuntimeSpec = field(repr=False)

    def __repr__(self) -> str:
        return f"<OllamaStageClient model={self.spec.model!r} private_config=redacted>"

    def model_dump(self) -> dict[str, Any]:
        """Expose only the redacted effective specification of this internal stage transport."""
        return self.spec.model_dump()

    to_payload = model_dump

    def complete(self, prompt: str, formatted: bool = False) -> Any:
        """Call the pinned generate endpoint with an internal purpose and preserve observed termination metadata."""
        import requests
        from llama_index.core.llms import CompletionResponse
        settings = self.spec.settings
        purpose = settings["stage_purpose"]
        # The bridge predates source-navigation. It is navigation planning,
        # so use its protected planning purpose on wire, retaining the original
        # local purpose for audit. No unprotected purpose can disable its guard.
        wire_purpose = "planning" if purpose == "source-navigation" else purpose
        options = {"temperature": settings["temperature"], "seed": settings["seed"],
                   "num_predict": settings["output_token_limit"], "num_ctx": settings["context_window"],
                   "reasoning_effort": settings["reasoning_effort"]}
        options.update({name: settings[name] for name in ("top_p", "top_k") if settings[name] is not None})
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(self.spec.endpoint + "/api/generate",
                json={"model": self.spec.model, "prompt": prompt, "stream": False,
                      "think": settings["thinking"], "purpose": wire_purpose, "options": options},
                timeout=settings["timeout"], allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise RuntimeInvocationError("stage_http_error")
            response.raise_for_status()
            body = response.json()
        if not isinstance(body, dict):
            raise RuntimeInvocationError("invalid_stage_response")
        text = body.get("response", "")
        # Do not turn an object/None into plausible nonempty generated text.
        text = text if isinstance(text, str) else ""
        return CompletionResponse(text=text, raw=body, additional_kwargs={
            "stage_protocol": "purpose_generate", "stage_purpose": purpose,
            "termination_observed": "done_reason" in body})


def _default_client_factory(spec: RuntimeSpec) -> Any:
    settings = spec.settings
    if spec.provider == "ollama":
        if settings["require_complete_output"]:
            return _OllamaStageClient(spec)
        from ollama import Client as OllamaClient
        options = {"seed": settings["seed"], "num_predict": settings["output_token_limit"]}
        options.update({name: settings[name] for name in ("top_p", "top_k") if settings[name] is not None})
        return _ollama_type()(model=spec.model, base_url=spec.endpoint, temperature=settings["temperature"],
                              context_window=settings["context_window"], thinking=settings["thinking"],
                              additional_kwargs=options, request_timeout=settings["timeout"],
                              client=OllamaClient(host=spec.endpoint, timeout=settings["timeout"], trust_env=False))
    from llm_providers import ProviderLLM
    return ProviderLLM(provider=spec.provider, model=spec.model, base_url=spec.endpoint,
                       api_key=spec.api_key, folder_id=spec.folder_id, auth_type=spec.auth_type,
                       temperature=settings["temperature"], timeout=settings["timeout"],
                       context_window=settings["context_window"], top_p=settings["top_p"],
                       output_token_limit=settings["output_token_limit"],
                       require_complete_output=settings["require_complete_output"])


class _SafeClient:
    """Small request-owned facade; callbacks and exceptions cannot dump its config."""
    __slots__ = ("_client", "_spec")

    def __init__(self, client: Any, spec: RuntimeSpec):
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_spec", spec)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Request client configuration is immutable")

    def __repr__(self) -> str:
        return f"<RequestClient provider={self._spec.provider!r} model={self._spec.model!r} private_config=redacted>"

    @property
    def metadata(self) -> Any:
        """Expose LlamaIndex planning metadata; the fallback output size is not a new transport token limit."""
        from llama_index.core.llms import LLMMetadata
        settings = self._spec.settings
        return LLMMetadata(context_window=settings["context_window"], model_name=self._spec.model,
                           num_output=settings["output_token_limit"] if settings["output_token_limit"] > 0 else 2048,
                           is_chat_model=True)

    def model_dump(self) -> dict[str, Any]:
        """Return the redacted runtime specification instead of serializing the underlying provider client."""
        return self._spec.model_dump()

    to_payload = model_dump

    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> Any:
        """Run completion with pinned options and replace potentially secret-bearing transport errors."""
        if kwargs:
            raise RuntimeConfigError("unpinned_runtime_completion_options")
        try:
            return self._client.complete(prompt, formatted=formatted)
        except Exception:
            raise RuntimeInvocationError("model_request_failed") from None

    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> Any:
        """Stream with pinned options and sanitize errors raised during iteration as well as startup."""
        if kwargs:
            raise RuntimeConfigError("unpinned_runtime_completion_options")
        try:
            yield from self._client.stream_complete(prompt, formatted=formatted)
        except Exception:
            raise RuntimeInvocationError("model_request_failed") from None


def _client(factory: Callable[[RuntimeSpec], Any], spec: RuntimeSpec) -> _SafeClient:
    try:
        created = factory(spec)
        if not callable(getattr(created, "complete", None)):
            raise TypeError("invalid client")
        return _SafeClient(created, spec)
    except Exception:
        raise RuntimeConfigError("runtime_client_factory_failed") from None


@dataclass(frozen=True, repr=False)
class RuntimeSnapshot:
    """Request-owned immutable client/configuration binding reused across retrieval and generation stages."""
    spec: RuntimeSpec
    client: _SafeClient = field(repr=False)
    _factory: Callable[[RuntimeSpec], Any] = field(repr=False)
    # Explicit operator/diagnostic binding only; ordinary captures never infer
    # a tokenizer or remote weights from their configured model name.
    _prompt_counter: Any = field(default=None, repr=False, compare=False)

    def __repr__(self) -> str:
        return f"<RuntimeSnapshot provider={self.spec.provider!r} model={self.spec.model!r} client=redacted>"

    @property
    def identity(self) -> str:
        """Return the opaque configuration identity used to partition request-bound caches."""
        return self.spec.identity

    @property
    def llama_index_client(self) -> Any:
        """Internal request-owned BaseLLM for LlamaIndex resolve_llm boundaries.

        The safe facade remains the generation/error boundary. This native
        object is not public producer metadata and must not be serialized.
        """
        return self.client._client

    def producer(self) -> dict[str, str]:
        # Configuration is not remote attestation. No guessed weights/digest,
        # endpoint, credential, private auth scope or operator key is published.
        """Describe the requested client without claiming verification of remote weights or exposing credentials."""
        return {"provider": self.spec.provider, "model": self.spec.model,
                "label": f"{self.spec.provider} · {self.spec.model}", "identity_kind": "request_client_snapshot"}

    def model_dump(self) -> dict[str, Any]:
        """Export public producer information and effective settings for diagnostics."""
        return {"producer": self.producer(), "settings": self.spec.settings}

    def complete_stage(self, prompt: str, max_tokens: int, timeout: float, thinking: bool = False,
                       purpose: str = "evidence-review") -> Any:
        """Derive bounded internal-stage options and require a complete nonempty provider response."""
        if type(max_tokens) is not int or max_tokens <= 0:
            raise RuntimeConfigError("invalid_internal_stage_budget")
        timeout = _finite(timeout, "invalid_internal_stage_timeout", positive=True)
        if type(thinking) is not bool:
            raise RuntimeConfigError("invalid_internal_stage_thinking")
        if type(purpose) is not str or purpose not in {
                "planning", "evidence-review", "source-reading", "source-navigation"}:
            raise RuntimeConfigError("invalid_internal_stage_purpose")
        updates = {"output_token_limit": max_tokens, "timeout": timeout, "require_complete_output": True}
        if self.spec.provider == "ollama":
            updates.update(thinking=thinking, stage_purpose=purpose, reasoning_effort="low")
        elif thinking:
            raise RuntimeConfigError("unsupported_internal_stage_thinking")
        stage_spec = self.spec.with_settings(**updates)
        stage_client = _client(self._factory, stage_spec)
        result = stage_client.complete(prompt)
        if stage_spec.provider == "ollama":
            raw = getattr(result, "raw", None)
            content = getattr(result, "text", None)
            # Only our local purpose-bearing legacy transport may accept an
            # unobserved finish reason. A remote flag cannot grant this exception.
            legacy_contract = (isinstance(stage_client._client, _OllamaStageClient) and
                               isinstance(raw, dict) and raw.get("model") == stage_spec.model and
                               type(raw.get("thinking_enabled")) is bool and
                               isinstance(raw.get("response"), str) and bool(raw["response"].strip()))
            reason_ok = (isinstance(raw, dict) and (raw.get("done_reason") == "stop" or
                         (legacy_contract and "done_reason" not in raw)))
            if (not isinstance(raw, dict) or raw.get("done") is not True or not reason_ok or
                    not isinstance(content, str) or not content.strip()):
                raise RuntimeInvocationError("Internal stage response did not finish completely")
        return result


def capture_runtime(config: Mapping[str, Any], *, default_model: str, ollama_base_url: str,
                    ollama_profiles: Mapping[str, Mapping[str, Any]], default_ollama_profile: Mapping[str, Any],
                    client_factory: Callable[[RuntimeSpec], Any] | None = None,
                    environ: Mapping[str, str] | None = None) -> RuntimeSnapshot:
    """Capture once per API/worker request, then never consult mutable selection.

    RAG_SECRET_KEY is reused when supplied: no secret is created or persisted.
    Without it, authenticated clients use a domain-separated HMAC keyed by their
    credential as a stable opaque cache partition. This is a fingerprint, not
    encryption or a substitute for protecting credentials/cache storage. An
    unauthenticated Ollama client requires no deployment secret at all.
    """
    if not isinstance(config, Mapping):
        raise RuntimeConfigError("invalid_runtime_config")
    config = dict(config)
    environment = os.environ if environ is None else environ
    provider = _text(config.get("provider") or "ollama", "missing_runtime_provider")
    if provider != "ollama" and provider not in PROVIDER_DEFAULTS:
        raise RuntimeConfigError("unsupported_runtime_provider")
    defaults = ({"model": default_model, "base_url": ollama_base_url} if provider == "ollama"
                else PROVIDER_DEFAULTS[provider])
    model = _text(config.get("model") or defaults["model"], "missing_runtime_model")
    endpoint = _endpoint(config.get("base_url") or defaults["base_url"])
    credential = _text(config.get("api_key", ""), "invalid_runtime_auth", empty=True)
    auth_type = _text(config.get("auth_type") or "api_key", "invalid_runtime_auth_type")
    folder_id = _text(config.get("folder_id", ""), "invalid_runtime_folder", empty=True)
    # An obsolete cloud key remaining in saved config must not make an
    # unauthenticated Ollama request's identity depend on a key it never sends.
    if provider == "ollama":
        credential, auth_type, folder_id = "", "none", ""
    settings_json = _json(_resolve_settings(config, provider=provider, model=model,
        profiles=ollama_profiles, default_profile=default_ollama_profile, environ=environment))
    scope_key = _text(environment.get("RAG_SECRET_KEY", ""), "invalid_runtime_scope_key", empty=True)
    auth_payload = _json({"schema": "provider-auth-scope/v1", "auth_type": auth_type, "folder_id": folder_id,
                          "credential": credential if scope_key else None}).encode("utf-8")
    key = (scope_key or credential or "provider-runtime-unauthenticated/v1").encode("utf-8")
    auth_scope = hmac.new(key, auth_payload, hashlib.sha256).hexdigest()
    spec = RuntimeSpec(provider, model, settings_json, _PrivateText(endpoint), _PrivateText(credential),
                       _PrivateText(folder_id), _PrivateText(auth_type), _PrivateText(auth_scope),
                       _identity(provider, model, endpoint, settings_json, auth_scope))
    factory = client_factory or _default_client_factory
    return RuntimeSnapshot(spec, _client(factory, spec), factory)
