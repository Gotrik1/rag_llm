"""LLM adapters for cloud providers used by the local RAG application."""

from __future__ import annotations

import math
import os
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Generator

import requests
from pydantic import Field

from llama_index.core.llms import CompletionResponse, LLMMetadata
from llama_index.core.llms.callbacks import llm_completion_callback
from llama_index.core.llms.custom import CustomLLM
from runtime_paths import CERTIFICATES_DIR
from provider_runtime import PROVIDER_DEFAULTS as DEFAULTS


GIGACHAT_CA_ARCHIVES = (
    Path.home() / "Downloads" / "russian_trusted_sub_ca.zip",
    Path.home() / "Downloads" / "windows_russian_trusted_root_ca.zip",
)
GIGACHAT_CA_BUNDLE = CERTIFICATES_DIR / "gigachat-russian-ca.pem"
MAX_OUTPUT_TOKENS = int(os.getenv("RAG_MAX_OUTPUT_TOKENS", "-1"))


def output_limit(field: str = "max_tokens", *, limit: int | None = None) -> dict[str, Any]:
    """Omit the application cap by default; the provider's model window remains."""
    value = MAX_OUTPUT_TOKENS if limit is None else limit
    return {field: value} if value > 0 else {}


def _require_completion_text(value: Any) -> str:
    """Reject absent/nontext content instead of turning it into a cached answer."""
    if type(value) is not str or not value.strip():
        raise RuntimeError("LLM response has no usable text content") from None
    return value  # Validate whitespace without altering valid provider text.


def gigachat_ca_bundle() -> str | bool:
    """Build a local CA bundle from the official Russian CA archives, if supplied."""
    configured_bundle = os.getenv("GIGACHAT_CA_BUNDLE", "").strip()
    if configured_bundle and Path(configured_bundle).exists():
        return configured_bundle
    if GIGACHAT_CA_BUNDLE.exists():
        return str(GIGACHAT_CA_BUNDLE)
    certificates: list[str] = []
    for archive in GIGACHAT_CA_ARCHIVES:
        if not archive.exists():
            continue
        with zipfile.ZipFile(archive) as contents:
            for member in contents.namelist():
                # Python/OpenSSL can validate the RSA chain; skip the separate
                # GOST certificates, which are not supported by stock OpenSSL.
                if not member.endswith(".cer") or "gost" in member.lower():
                    continue
                certificate = contents.read(member).decode("ascii")
                if "-----BEGIN CERTIFICATE-----" in certificate:
                    certificates.append(certificate.strip())
    if not certificates:
        return True  # Fall back to certifi when the optional archives are absent.
    GIGACHAT_CA_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    GIGACHAT_CA_BUNDLE.write_text("\n".join(certificates) + "\n", encoding="ascii")
    return str(GIGACHAT_CA_BUNDLE)


class ProviderLLM(CustomLLM):
    """Minimal LlamaIndex completion adapter for supported HTTP APIs."""

    provider: str
    api_key: str = Field(exclude=True, repr=False)
    base_url: str = Field(exclude=True, repr=False)
    model: str
    folder_id: str = Field(default="", exclude=True, repr=False)
    auth_type: str = Field(default="api_key", exclude=True, repr=False)
    temperature: float = 0.0
    timeout: float = Field(default_factory=lambda: float(os.getenv("LLM_REQUEST_TIMEOUT", "900")))
    output_token_limit: int = Field(default_factory=lambda: MAX_OUTPUT_TOKENS)
    context_window: int = 32768
    top_p: float | None = None
    require_complete_output: bool = Field(default=False, exclude=True, repr=False)
    _giga_token: str = ""
    _giga_token_expires: float = 0.0

    @property
    def metadata(self) -> LLMMetadata:
        """Expose LlamaIndex planning metadata; the 2048 fallback does not impose a provider output cap."""
        return LLMMetadata(context_window=self.context_window, num_output=self.output_token_limit if self.output_token_limit > 0 else 2048,
                           is_chat_model=True, model_name=self.model)

    def _post(self, path: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}{path}", headers=headers, json=payload, timeout=self.timeout,
                verify=gigachat_ca_bundle() if self.provider == "gigachat" else True,
            )
        except requests.RequestException:
            raise RuntimeError(f"{self.provider}: request failed") from None
        if not response.ok:
            # Provider bodies may echo Authorization/configuration; never put
            # them into public exception text, callback events or chat history.
            raise RuntimeError(f"{self.provider}: HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            if self.require_complete_output:
                raise RuntimeError("Internal stage response did not finish completely") from None
            raise ValueError(f"{self.provider}: invalid response") from None
        if self.require_complete_output:
            self._validate_stage_completion(data)
        return data

    def _validate_stage_completion(self, data: Any) -> None:
        """A parseable JSON fragment is usable only after normal termination.

        Gemini uses its OpenAI-compatible route here, not native finishReason.
        Yandex Foundation Models uses the alternative's native status instead.
        Unknown/missing metadata cannot certify an internal structured result.
        """
        try:
            if self.provider == "yandex":
                complete = data["result"]["alternatives"][0]["status"] == "ALTERNATIVE_STATUS_FINAL"
            else:
                complete = data["choices"][0]["finish_reason"] == "stop"
        except (KeyError, TypeError, IndexError):
            complete = False
        if not complete:
            # Never interpolate provider response bodies, text or credentials.
            raise RuntimeError("Internal stage response did not finish completely")

    def _openai_compatible(self, prompt: str) -> str:
        data = self._post(
            "/chat/completions",
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": self.temperature,
             **({"top_p": self.top_p} if self.top_p is not None else {}),
             **output_limit(limit=self.output_token_limit)},
        )
        return _require_completion_text(data["choices"][0]["message"]["content"])

    def _gigachat_token(self) -> str:
        if self._giga_token and time.time() < self._giga_token_expires - 30:
            return self._giga_token
        # GigaChat Studio issues an Authorization key ready for the Basic scheme.
        # It must be passed verbatim, without an extra Base64 encoding step.
        credentials = self.api_key
        try:
            response = requests.post(
                "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
                headers={
                    "Authorization": f"Basic {credentials}",
                    "RqUID": str(uuid.uuid4()),
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"scope": "GIGACHAT_API_PERS"}, timeout=self.timeout,
                verify=gigachat_ca_bundle(),
            )
        except requests.RequestException:
            raise RuntimeError("gigachat: OAuth request failed") from None
        if not response.ok:
            raise RuntimeError(f"gigachat: OAuth HTTP {response.status_code}")
        data = response.json()
        self._giga_token = str(data["access_token"])
        expires_at = float(data.get("expires_at", time.time() + 1500))
        # The API may return Unix milliseconds; normalize it before comparing
        # against time.time(), which is Unix seconds.
        self._giga_token_expires = expires_at / 1000 if expires_at > 10**11 else expires_at
        return self._giga_token

    def _gigachat(self, prompt: str) -> str:
        token = self._gigachat_token()
        data = self._post(
            "/v1/chat/completions",
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": self.temperature,
             **({"top_p": self.top_p} if self.top_p is not None else {}),
             **output_limit(limit=self.output_token_limit)},
        )
        return _require_completion_text(data["choices"][0]["message"]["content"])

    def _yandex(self, prompt: str) -> str:
        if not self.folder_id:
            raise RuntimeError("yandex: укажите идентификатор каталога (folder ID).")
        scheme = "Bearer" if self.auth_type == "iam" else "Api-Key"
        model_uri = self.model if self.model.startswith("gpt://") else f"gpt://{self.folder_id}/{self.model}"
        data = self._post(
            "/completion",
            {"Authorization": f"{scheme} {self.api_key}", "Content-Type": "application/json"},
            {"modelUri": model_uri, "completionOptions": {"stream": False, "temperature": self.temperature,
             **output_limit("maxTokens", limit=self.output_token_limit)}, "messages": [{"role": "user", "text": prompt}]},
        )
        return _require_completion_text(data["result"]["alternatives"][0]["message"]["text"])

    def _request(self, prompt: str) -> str:
        if not self.api_key:
            raise RuntimeError(f"{self.provider}: укажите API-ключ в настройках провайдера.")
        if self.provider in {"openai", "deepseek", "gemini"}:
            return self._openai_compatible(prompt)
        if self.provider == "gigachat":
            return self._gigachat(prompt)
        if self.provider == "yandex":
            return self._yandex(prompt)
        raise RuntimeError(f"Неподдерживаемый провайдер: {self.provider}")

    @llm_completion_callback()
    def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> CompletionResponse:
        """Execute one complete provider request using the adapter's captured configuration and callback contract."""
        return CompletionResponse(text=self._request(prompt))

    def complete_stage(self, prompt: str, max_tokens: int, timeout: float) -> CompletionResponse:
        """Bound an internal planning/review call without capping final answers.

        Per-request copies avoid racing shared adapter settings between chats.
        Authentication/configuration are inherited, while refreshed private
        token state and temporary budgets remain confined to the copy.
        """
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("Internal stage max_tokens must be a positive integer")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Internal stage timeout must be positive and finite")
        request_llm = self.model_copy(update={"output_token_limit": max_tokens, "timeout": float(timeout),
                                              "require_complete_output": True})
        return request_llm.complete(prompt)

    @llm_completion_callback()
    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> Generator[CompletionResponse, None, None]:
        """Yield one complete response as one delta; this adapter does not stream incremental network tokens."""
        text = self._request(prompt)
        yield CompletionResponse(text=text, delta=text)
