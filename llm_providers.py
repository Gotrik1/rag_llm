"""LLM adapters for cloud providers used by the local RAG application."""

from __future__ import annotations

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


DEFAULTS = {
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4.1-mini"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash"},
    "gigachat": {"base_url": "https://gigachat.devices.sberbank.ru/api", "model": "GigaChat"},
    "yandex": {"base_url": "https://llm.api.cloud.yandex.net/foundationModels/v1", "model": "yandexgpt-lite"},
}

GIGACHAT_CA_ARCHIVES = (
    Path.home() / "Downloads" / "russian_trusted_sub_ca.zip",
    Path.home() / "Downloads" / "windows_russian_trusted_root_ca.zip",
)
GIGACHAT_CA_BUNDLE = CERTIFICATES_DIR / "gigachat-russian-ca.pem"
MAX_OUTPUT_TOKENS = 4056


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
    api_key: str
    base_url: str
    model: str
    folder_id: str = ""
    auth_type: str = "api_key"
    temperature: float = 0.0
    timeout: float = 120.0
    _giga_token: str = ""
    _giga_token_expires: float = 0.0

    @property
    def metadata(self) -> LLMMetadata:
        return LLMMetadata(context_window=32768, num_output=MAX_OUTPUT_TOKENS, is_chat_model=True, model_name=self.model)

    def _post(self, path: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url.rstrip('/')}{path}", headers=headers, json=payload, timeout=self.timeout,
            verify=gigachat_ca_bundle() if self.provider == "gigachat" else True,
        )
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(f"{self.provider}: HTTP {response.status_code}: {detail}")
        return response.json()

    def _openai_compatible(self, prompt: str) -> str:
        data = self._post(
            "/chat/completions",
            {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": self.temperature, "max_tokens": MAX_OUTPUT_TOKENS},
        )
        return str(data["choices"][0]["message"]["content"])

    def _gigachat_token(self) -> str:
        if self._giga_token and time.time() < self._giga_token_expires - 30:
            return self._giga_token
        # GigaChat Studio issues an Authorization key ready for the Basic scheme.
        # It must be passed verbatim, without an extra Base64 encoding step.
        credentials = self.api_key
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
        if not response.ok:
            raise RuntimeError(f"gigachat: OAuth HTTP {response.status_code}: {response.text}")
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
            {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": self.temperature, "max_tokens": MAX_OUTPUT_TOKENS},
        )
        return str(data["choices"][0]["message"]["content"])

    def _yandex(self, prompt: str) -> str:
        if not self.folder_id:
            raise RuntimeError("yandex: укажите идентификатор каталога (folder ID).")
        scheme = "Bearer" if self.auth_type == "iam" else "Api-Key"
        model_uri = self.model if self.model.startswith("gpt://") else f"gpt://{self.folder_id}/{self.model}"
        data = self._post(
            "/completion",
            {"Authorization": f"{scheme} {self.api_key}", "Content-Type": "application/json"},
            {"modelUri": model_uri, "completionOptions": {"stream": False, "temperature": self.temperature, "maxTokens": str(MAX_OUTPUT_TOKENS)}, "messages": [{"role": "user", "text": prompt}]},
        )
        return str(data["result"]["alternatives"][0]["message"]["text"])

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
        return CompletionResponse(text=self._request(prompt))

    @llm_completion_callback()
    def stream_complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> Generator[CompletionResponse, None, None]:
        text = self._request(prompt)
        yield CompletionResponse(text=text, delta=text)
