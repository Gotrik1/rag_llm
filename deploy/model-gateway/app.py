from __future__ import annotations

import hashlib
import hmac
import logging
import os
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask


LOGGER = logging.getLogger("model_gateway")

UPSTREAM_BASE_URL = os.getenv("MODEL_UPSTREAM_BASE_URL", "http://vllm:8000").rstrip("/")
TOKEN_HASHES_FILE = os.getenv("MODEL_API_TOKEN_HASHES_FILE", "/run/secrets/model_api_token_hashes")
REQUEST_TIMEOUT = float(os.getenv("MODEL_API_REQUEST_TIMEOUT", "900"))
MAX_REQUEST_BYTES = max(1024, int(os.getenv("MODEL_API_MAX_REQUEST_BYTES", str(2 * 1024 * 1024))))

app = FastAPI(
    title="Direct model API gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@lru_cache(maxsize=1)
def _token_hashes() -> tuple[tuple[str, str], ...]:
    path = Path(TOKEN_HASHES_FILE)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"Cannot read model API token hashes from {path}") from exc

    entries: list[tuple[str, str]] = []
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        label, separator, digest = line.partition(":")
        label = label.strip()
        digest = digest.strip().lower()
        if not separator or not label or len(digest) != 64:
            raise RuntimeError(f"Invalid token hash entry at line {line_number}")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise RuntimeError(f"Invalid SHA-256 digest at line {line_number}") from exc
        entries.append((label, digest))

    if not entries:
        raise RuntimeError("No model API token hashes are configured")
    return tuple(entries)


def require_model_token(authorization: str | None = Header(default=None)) -> str:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Expected a Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    candidate = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    matched_label: str | None = None
    for label, expected_digest in _token_hashes():
        if hmac.compare_digest(candidate, expected_digest):
            matched_label = label
    if matched_label is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid model API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched_label


async def _close_upstream(response: httpx.Response, client: httpx.AsyncClient) -> None:
    await response.aclose()
    await client.aclose()


async def _proxy(request: Request, upstream_path: str, token_label: str) -> StreamingResponse:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from None

    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large")

    headers: dict[str, str] = {}
    if content_type := request.headers.get("content-type"):
        headers["content-type"] = content_type
    if request_id := request.headers.get("x-request-id"):
        headers["x-request-id"] = request_id

    client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT, trust_env=False)
    try:
        upstream_request = client.build_request(
            request.method,
            f"{UPSTREAM_BASE_URL}{upstream_path}",
            headers=headers,
            content=body,
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        LOGGER.warning("model upstream failure token=%s path=%s error=%s", token_label, upstream_path, type(exc).__name__)
        raise HTTPException(status_code=503, detail="Model service is unavailable") from exc

    response_headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "cache-control", "x-request-id"}
    }
    response_headers["X-Model-Token-Label"] = token_label
    response_headers["X-Accel-Buffering"] = "no"
    LOGGER.info("model request token=%s method=%s path=%s status=%s", token_label, request.method, upstream_path, response.status_code)
    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=response_headers,
        background=BackgroundTask(_close_upstream, response, client),
    )


@app.get("/health")
async def health() -> dict[str, int | str]:
    return {"status": "ok", "configured_tokens": len(_token_hashes())}


@app.get("/v1/models")
async def models(request: Request, token_label: str = Depends(require_model_token)):
    return await _proxy(request, "/v1/models", token_label)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, token_label: str = Depends(require_model_token)):
    return await _proxy(request, "/v1/chat/completions", token_label)


@app.post("/v1/completions")
async def completions(request: Request, token_label: str = Depends(require_model_token)):
    return await _proxy(request, "/v1/completions", token_label)
