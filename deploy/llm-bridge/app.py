from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI(title="RAG LLM compatibility bridge")

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://vllm:8000").rstrip("/")
LLM_MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen3.8-27B")
EMBED_MODEL = os.getenv("FASTEMBED_MODEL", "nomic-ai/nomic-embed-text-v1.5")
EMBED_CACHE = os.getenv("FASTEMBED_CACHE_PATH", "/data/fastembed")
REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "900"))
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-base")
RERANKER_CACHE = os.getenv("RERANKER_CACHE_PATH", EMBED_CACHE)
RERANKER_MAX_DOCUMENTS = max(1, int(os.getenv("RERANKER_MAX_DOCUMENTS", "64")))
RERANKER_TEXT_LIMIT = max(256, int(os.getenv("RERANKER_TEXT_LIMIT", "6000")))


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        return messages
    prompt = str(payload.get("prompt", ""))
    return [{"role": "user", "content": prompt}]


def _openai_payload(payload: dict[str, Any], stream: bool) -> dict[str, Any]:
    options = payload.get("options") or {}
    result: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": _messages(payload),
        "stream": stream,
        "temperature": float(options.get("temperature", 0.0)),
        "seed": int(options.get("seed", 42)),
    }
    output_tokens = int(options.get("num_predict", -1))
    if output_tokens > 0:
        result["max_tokens"] = output_tokens
    # This bridge serves the latency-sensitive RAG path. Qwen3.8 can otherwise
    # spend the entire output budget on reasoning and return empty content.
    if payload.get("think") is False or LLM_MODEL.startswith("Qwen/Qwen3.8"):
        result["chat_template_kwargs"] = {"enable_thinking": False}
    return result


async def _upstream(payload: dict[str, Any], stream: bool = False) -> httpx.Response:
    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False)
    response = await client.post(f"{VLLM_BASE_URL}/v1/chat/completions", json=_openai_payload(payload, stream))
    response.raise_for_status()
    response.extensions["bridge_client"] = client
    return response


def _content(response: httpx.Response) -> str:
    data = response.json()
    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    return str(message.get("content") or message.get("reasoning_content") or "")


async def _stream_ollama(response: httpx.Response, mode: str) -> AsyncIterator[bytes]:
    client = response.extensions["bridge_client"]
    try:
        for line in response.text.splitlines():
            if not line.startswith("data: "):
                continue
            raw = line[6:].strip()
            if raw == "[DONE]":
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            delta = ((data.get("choices") or [{}])[0].get("delta") or {})
            text = delta.get("content") or delta.get("reasoning_content") or ""
            item: dict[str, Any] = {"model": LLM_MODEL, "done": False}
            if mode == "chat":
                item["message"] = {"role": "assistant", "content": text}
            else:
                item["response"] = text
            yield (json.dumps(item, ensure_ascii=False) + "\n").encode()
        final: dict[str, Any] = {"model": LLM_MODEL, "done": True}
        if mode == "chat":
            final["message"] = {"role": "assistant", "content": ""}
        else:
            final["response"] = ""
        yield (json.dumps(final, ensure_ascii=False) + "\n").encode()
    finally:
        await response.aclose()
        await client.aclose()


@app.get("/health")
async def health() -> dict[str, str]:
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get(f"{VLLM_BASE_URL}/health")
    if response.status_code >= 400:
        raise HTTPException(503, "vLLM is not ready")
    return {"status": "ok"}


@app.get("/api/tags")
async def tags() -> dict[str, list[dict[str, str]]]:
    return {"models": [{"name": LLM_MODEL, "model": LLM_MODEL}]}


@app.post("/api/show")
async def show(payload: dict[str, Any]) -> dict[str, Any]:
    return {"name": payload.get("name", LLM_MODEL), "details": {"family": "qwen3.8"}}


@app.post("/api/generate")
async def generate(payload: dict[str, Any]):
    stream = bool(payload.get("stream", False))
    try:
        response = await _upstream(payload, stream=stream)
    except httpx.HTTPError as exc:
        raise HTTPException(503, str(exc)) from exc
    if stream:
        return StreamingResponse(_stream_ollama(response, "generate"), media_type="application/x-ndjson")
    content = _content(response)
    await response.extensions["bridge_client"].aclose()
    return {"model": LLM_MODEL, "response": content, "done": True}


@app.post("/api/chat")
async def chat(payload: dict[str, Any]):
    stream = bool(payload.get("stream", False))
    try:
        response = await _upstream(payload, stream=stream)
    except httpx.HTTPError as exc:
        raise HTTPException(503, str(exc)) from exc
    if stream:
        return StreamingResponse(_stream_ollama(response, "chat"), media_type="application/x-ndjson")
    content = _content(response)
    await response.extensions["bridge_client"].aclose()
    return {"model": LLM_MODEL, "message": {"role": "assistant", "content": content}, "done": True}


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=EMBED_MODEL, cache_dir=EMBED_CACHE)


def _embed_texts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value or "")]


@app.post("/api/embed")
async def embed(payload: dict[str, Any]) -> dict[str, Any]:
    vectors = list(_embedder().embed(_embed_texts(payload.get("input"))))
    return {"model": EMBED_MODEL, "embeddings": [vector.tolist() for vector in vectors]}


@app.post("/api/embeddings")
async def embeddings(payload: dict[str, Any]) -> dict[str, Any]:
    vectors = list(_embedder().embed(_embed_texts(payload.get("prompt"))))
    return {"model": EMBED_MODEL, "embedding": vectors[0].tolist()}


@lru_cache(maxsize=1)
def _reranker():
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    return TextCrossEncoder(model_name=RERANKER_MODEL, cache_dir=RERANKER_CACHE)


@app.post("/api/rerank")
def rerank(payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query", "")).strip()
    documents = payload.get("documents")
    requested_model = str(payload.get("model", RERANKER_MODEL)).strip()
    if requested_model != RERANKER_MODEL:
        raise HTTPException(400, "requested reranker model is not configured")
    if not query:
        raise HTTPException(422, "query is required")
    if not isinstance(documents, list) or not documents:
        raise HTTPException(422, "documents must be a non-empty list")
    if len(documents) > RERANKER_MAX_DOCUMENTS:
        raise HTTPException(413, "too many documents for one rerank request")
    passages = [str(document)[:RERANKER_TEXT_LIMIT] for document in documents]
    scores = [float(score) for score in _reranker().rerank(query, passages)]
    return {"model": RERANKER_MODEL, "scores": scores}
