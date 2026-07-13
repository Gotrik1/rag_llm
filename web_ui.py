from __future__ import annotations

import base64
import cgi
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib.request import urlopen
from urllib.error import URLError

from db_store import (
    create_chat,
    create_message,
    create_project,
    delete_chat,
    delete_message,
    delete_project,
    get_chat,
    get_project_conversation,
    get_message,
    get_project,
    get_workspace,
    list_chats,
    list_messages,
    list_projects,
    rollback_transaction,
    run_migrations,
    update_chat,
    update_message,
    update_project,
    update_workspace,
)
from llm_providers import DEFAULTS, ProviderLLM
from rag_agent import CACHE_VERSION, DATA_DIR, GROUNDED_SYSTEM_PROMPT, LLM_MODEL, OLLAMA_BASE_URL, QDRANT_URL, RAGAgent, TOP_K, _cleanup_formula_noise, clean_formula_artifacts, extract_formula_lines_from_texts, refine_prism_query, validate_retrieval
from system_prompts import get_profile, profiles
from opentelemetry.trace import Status, StatusCode
from telemetry import PIPELINE_SECONDS, tracer


HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
PORT = int(os.environ.get("BACKEND_PORT", "8080"))
STATE_DIR = DATA_DIR / "state"
MATHML_CACHE_DIR = DATA_DIR / "cache" / "mathml"
UPLOAD_DIR = DATA_DIR / "uploads"
PROVIDERS_CONFIG_PATH = STATE_DIR / "llm_providers.json"
SYSTEM_PROMPT_CONFIG_PATH = STATE_DIR / "system_prompt.json"
FLOW_CONFIG_PATH = STATE_DIR / "flow_mode.json"
FLOW_MODES = {"python", "rust", "hybrid"}
OPENAPI_SPEC_PATH = Path("openapi.json")


@contextmanager
def _pipeline_span(
    stage: str,
    *,
    flow: str,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Measure a stage without recording prompts, evidence, answers, or identities."""
    started = time.perf_counter()
    with tracer().start_as_current_span(
        f"rag.{stage}", record_exception=False, set_status_on_exception=False
    ) as span:
        span.set_attribute("rag.pipeline.stage", stage)
        span.set_attribute("rag.flow", flow)
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        try:
            yield span
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR))
            span.set_attribute("error.type", type(exc).__name__)
            raise
        finally:
            PIPELINE_SECONDS.labels(stage=stage, flow=flow).observe(time.perf_counter() - started)

_agent: RAGAgent | None = None
_agent_lock = threading.Lock()
_request_lock = threading.Lock()


SWAGGER_UI_HTML = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RAG Assistant API</title>
  <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css">
  <style>body { margin: 0; background: #fafafa; } .swagger-ui .topbar { display: none; }</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    window.ui = SwaggerUIBundle({
      url: "/api/openapi.json",
      dom_id: "#swagger-ui",
      deepLinking: true,
      displayRequestDuration: true,
      persistAuthorization: true
    });
  </script>
</body>
</html>
"""


def load_openapi_spec() -> dict:
    try:
        spec = json.loads(OPENAPI_SPEC_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Не удалось прочитать OpenAPI-спецификацию: {exc}") from exc
    if not isinstance(spec, dict):
        raise RuntimeError("OpenAPI-спецификация должна быть JSON-объектом.")
    return spec


def load_provider_config() -> dict:
    try:
        config = json.loads(PROVIDERS_CONFIG_PATH.read_text(encoding="utf-8"))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {"provider": "ollama", "model": LLM_MODEL}


def save_provider_config(config: dict) -> None:
    PROVIDERS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROVIDERS_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


def load_system_prompt_id() -> str:
    try:
        payload = json.loads(SYSTEM_PROMPT_CONFIG_PATH.read_text(encoding="utf-8"))
        return str(payload.get("selected", "rag-grounded"))
    except (OSError, ValueError):
        return "rag-grounded"


def save_system_prompt_id(prompt_id: str) -> None:
    SYSTEM_PROMPT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSTEM_PROMPT_CONFIG_PATH.write_text(json.dumps({"selected": prompt_id}), encoding="utf-8")


def load_flow_mode() -> str:
    try:
        mode = str(json.loads(FLOW_CONFIG_PATH.read_text(encoding="utf-8")).get("mode", "python"))
        return mode if mode in FLOW_MODES else "python"
    except (OSError, ValueError):
        return "python"


def storage_defaults() -> tuple[str, str, str]:
    config = load_provider_config()
    return (
        load_flow_mode(),
        str(config.get("provider", "ollama")),
        str(config.get("model", LLM_MODEL)),
    )


def project_context_prompt(scope: dict) -> str:
    """Format only the current project's persisted conversation for generation."""
    memory = scope.get("memory") if isinstance(scope.get("memory"), dict) else {}
    memory_text = str(memory.get("text", "")).strip()
    entries: list[str] = []
    used = 0
    for message in reversed(scope.get("messages", [])):
        content = str(message.get("content", "")).strip()
        if not content:
            continue
        title = str(message.get("chat_title", "Чат"))[:120]
        role = str(message.get("role", "user"))
        entry = f"[{title} | {role}] {content[:1800]}"
        if used + len(entry) > 12_000:
            break
        entries.append(entry)
        used += len(entry)
    entries.reverse()
    parts = [
        f"КОНТЕКСТ ПРОЕКТА: {scope.get('project_name', 'Проект')}",
        "Используй этот контекст только для продолжения работы в проекте. "
        "Он не заменяет доказательства из регламентов и не содержит данных других проектов.",
    ]
    if memory_text:
        parts.append(f"ПАМЯТЬ ПРОЕКТА:\n{memory_text[:4000]}")
    if entries:
        parts.append("ИСТОРИЯ ЧАТОВ ПРОЕКТА:\n" + "\n".join(entries))
    return "\n\n".join(parts)


def save_flow_mode(mode: str) -> None:
    FLOW_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    FLOW_CONFIG_PATH.write_text(json.dumps({"mode": mode}), encoding="utf-8")


def _rust_command() -> list[str]:
    configured = os.environ.get("RUST_CLI_PATH", "").strip()
    if configured:
        return [configured]
    binary = Path("target") / "debug" / ("llm-rust.exe" if os.name == "nt" else "llm-rust")
    return [str(binary)] if binary.exists() else ["cargo", "run", "--quiet"]


def _run_rust_json(command: str, question: str, *, flow: str = "rust") -> dict | list[dict]:
    """Call the Rust CLI and record only operation metadata, never its payload."""
    operation = command.removesuffix("-json")
    with _pipeline_span(
        "rust.subprocess",
        flow=flow,
        attributes={"rag.rust.operation": operation},
    ) as span:
        try:
            result = subprocess.run(
                _rust_command(), input=f":{command} {question}\n:exit\n".encode("utf-8"),
                capture_output=True, cwd=Path.cwd(), timeout=180, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Rust flow недоступен: {exc}") from exc
        span.set_attribute("process.exit.code", result.returncode)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        for line in reversed(stdout.splitlines()):
            line = line.strip().removeprefix("> ").strip()
            if line.startswith("{") or line.startswith("["):
                try:
                    return json.loads(line)
                except ValueError:
                    pass
        raise RuntimeError("Rust flow завершился с ошибкой")


def rust_context(question: str, *, flow: str = "rust") -> list[dict]:
    payload = _run_rust_json("retrieve-json", question, flow=flow)
    if not isinstance(payload, list):
        raise RuntimeError("Некорректный retrieval-ответ Rust flow")
    return [{
        "file": str(item.get("file", "?")), "score": float(item.get("score", 0)),
        "text": str(item.get("text", "")), "section_path": str(item.get("section_path", "")),
        "section_title": str(item.get("section_title", "")), "status": str(item.get("status", "active")),
        "chunk_type": str(item.get("chunk_type", "unknown")),
    } for item in payload if isinstance(item, dict)]


def fuse_contexts(python_items: list[dict], rust_items: list[dict], top_k: int = TOP_K, *, flow: str = "hybrid") -> list[dict]:
    with _pipeline_span(
        "retrieval.cross_runtime_fusion",
        flow=flow,
        attributes={
            "rag.results.python_count": len(python_items),
            "rag.results.rust_count": len(rust_items),
        },
    ):
        return _fuse_contexts(python_items, rust_items, top_k)


def _fuse_contexts(python_items: list[dict], rust_items: list[dict], top_k: int = TOP_K) -> list[dict]:
    """Fuse evidence while treating the current Python index as authoritative.

    The Rust index may be older or contain only part of the corpus.  Keep most
    slots for the current UI knowledge base and use Rust evidence to complement
    it.  Exact duplicate chunks are removed and deleted evidence is rejected.
    """
    selected: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(items: list[dict], origin: str, limit: int) -> None:
        for raw in items:
            if len(selected) >= limit or raw.get("status") == "deleted":
                continue
            item = dict(raw)
            item["flow_origin"] = origin
            key = (
                Path(str(item.get("file", ""))).name.lower(),
                str(item.get("section_path", "")).strip().lower(),
                re.sub(r"\s+", " ", str(item.get("text", "")))[:500].lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            selected.append(item)

    if python_items:
        python_slots = min(len(python_items), max(1, top_k - 2))
        add(python_items, "python", python_slots)
        add(rust_items, "rust", top_k)
        add(python_items[python_slots:], "python", top_k)
    else:
        add(rust_items, "rust", top_k)
    return selected[:top_k]


def filter_rust_context(question: str, items: list[dict]) -> list[dict]:
    """Reject Rust evidence that matches a word but not the requested operation."""
    normalized = question.lower().replace("ё", "е")
    asks_parameter = "параметр" in normalized
    asks_creation = any(marker in normalized for marker in ("добав", "созда", "новый", "завест"))
    filtered: list[dict] = []
    for item in items:
        haystack = f"{item.get('section_path', '')}\n{item.get('text', '')}".lower().replace("ё", "е")
        if asks_parameter and "параметр" not in haystack:
            continue
        if asks_creation and not any(marker in haystack for marker in ("добав", "созда", "создание")):
            continue
        filtered.append(item)
    return filtered


def rust_generate(question: str, context: list[dict], project_context: str = "") -> tuple[str, bool]:
    with _pipeline_span(
        "rust.generate",
        flow="rust",
        attributes={"rag.context.count": len(context)},
    ) as span:
        answer, from_cache = _rust_generate(question, context, project_context)
        span.set_attribute("rag.cache.hit", from_cache)
        span.set_attribute("rag.response.chars", len(answer))
        return answer, from_cache


def _rust_generate(question: str, context: list[dict], project_context: str = "") -> tuple[str, bool]:
    context_text = "\n\n---\n\n".join(
        f"[Источник: {item.get('file', '?')}, Раздел: {item.get('section_path', '')}]\n{item.get('text', '')}"
        for item in context
    )
    if project_context:
        context_text = f"{context_text}\n\n---\n\n{project_context}"
    config = load_provider_config()
    model = str(config.get("model", "")).strip() if config.get("provider", "ollama") == "ollama" else ""
    cache_key = hashlib.sha256(
        f"{CACHE_VERSION}\nrust-generation\n{model}\n{question}\n{context_text}".encode("utf-8")
    ).hexdigest()
    response_cache = get_agent().response_cache
    cached = response_cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("answer"), str):
        return str(cached["answer"]), True
    request = json.dumps(
        {"question": question, "context": context_text, "model": model or None},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = _run_rust_json("generate-json", request)
    if not isinstance(payload, dict) or not str(payload.get("answer", "")).strip():
        raise RuntimeError("Rust flow не вернул ответ")
    answer = str(payload["answer"]).strip()
    response_cache.set(cache_key, {"answer": answer})
    return answer, False


def public_provider_config(config: dict) -> dict:
    result = {key: value for key, value in config.items() if key != "api_key"}
    key = str(config.get("api_key", ""))
    result["has_api_key"] = bool(key)
    result["api_key_hint"] = f"••••{key[-4:]}" if key else ""
    return result


def llm_label() -> dict:
    config = load_provider_config()
    provider = str(config.get("provider", "ollama"))
    provider_names = {
        "ollama": "Ollama",
        "openai": "ChatGPT / OpenAI",
        "deepseek": "DeepSeek",
        "gemini": "Google Gemini",
        "gigachat": "GigaChat",
        "yandex": "YandexGPT",
    }
    model = str(config.get("model", LLM_MODEL))
    return {"provider": provider, "model": model, "label": f"{provider_names.get(provider, provider)} · {model}"}


def ollama_models() -> list[str]:
    """Return locally installed Ollama model names."""
    try:
        with urlopen(f"{OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise RuntimeError(f"Ollama недоступен: {OLLAMA_BASE_URL}") from exc
    # Embedding-only models (for example nomic-embed-text) cannot answer a chat
    # request, so keep the selector limited to completion-capable models.
    models = []
    for item in payload.get("models", []):
        name = str(item.get("name", "")).strip()
        capabilities = item.get("capabilities")
        if name and (not isinstance(capabilities, list) or "completion" in capabilities):
            models.append(name)
    return sorted(set(models))


def get_agent() -> RAGAgent:
    global _agent
    with _agent_lock:
        if _agent is None:
            _agent = RAGAgent()
            config = load_provider_config()
            if config.get("provider") and (config.get("provider") != "ollama" or config.get("model") != LLM_MODEL):
                _agent.set_llm_provider(config)
        return _agent


def render_text(value: str) -> str:
    value = _cleanup_formula_noise(value or "")
    value = clean_formula_artifacts(value)
    value = cleanup_llm_markup(value)
    mathml_blocks: list[str] = []

    def stash_mathml(match: re.Match[str]) -> str:
        try:
            mathml = base64.b64decode(match.group(1)).decode("utf-8")
        except Exception:
            return ""
        token = f"@@MATHML_{len(mathml_blocks)}@@"
        mathml_blocks.append(f'<span class="mathml">{mathml}</span>')
        return token

    value = re.sub(r"\[\[MATHML:([A-Za-z0-9+/=]+)\]\]", stash_mathml, value)

    def stash_mathml_ref(match: re.Match[str]) -> str:
        ref = match.group(1)
        if not re.fullmatch(r"[a-f0-9]{24}", ref):
            return ""
        path = MATHML_CACHE_DIR / f"{ref}.mathml"
        try:
            mathml = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        token = f"@@MATHML_{len(mathml_blocks)}@@"
        mathml_blocks.append(f'<span class="mathml">{mathml}</span>')
        return token

    value = re.sub(r"\[\[MATHML_REF:([a-f0-9]{24})\]\]", stash_mathml_ref, value)
    value = escape(value)
    for index, mathml in enumerate(mathml_blocks):
        value = value.replace(escape(f"@@MATHML_{index}@@"), mathml)
    value = re.sub(r"\[MATH:\s*([^\]\n]+)\]\s*(?=<span class=\"mathml\">)", "", value)
    value = re.sub(r"\[MATH:\s*([^\]\n]+)\]", r"[\1]", value)
    value = re.sub(r"\bИВО\b", "ИВ0", value)

    def formula(match: re.Match[str]) -> str:
        body = match.group(1)
        body = body.replace("\\", "")
        if "_sub(" in body:
            prefix, suffix = body.split("_sub(", 1)
            suffix = suffix[:-1] if suffix.endswith(")") else suffix
            suffix = suffix.strip()
            if suffix.startswith(";"):
                suffix = suffix[1:].strip()
            body = f"{prefix}<sub>{suffix}</sub>"
        return f'<span class="formula">{body}</span>'

    value = re.sub(r"\[([^\[\]\n]*(?:_sub|Δ|С|Т)[^\[\]\n]*)\]", formula, value)
    value = re.sub(r"(?<!\w)(ΔО|С|Т)_sub\(([^)]+)\)", r'<span class="formula">\1<sub>\2</sub></span>', value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = value.replace("\n", "<br>")
    return value


def cleanup_llm_markup(value: str) -> str:
    """Remove Markdown/LaTeX wrappers the model adds around already-normalized formulas."""
    value = re.sub(r"\\?<(?:/)?br\s*/?>|&lt;/?br\s*/?&gt;", "\n", value, flags=re.IGNORECASE)
    value = value.replace("\\[", "").replace("\\]", "")
    value = value.replace("\\(", "").replace("\\)", "")
    value = value.replace("**", "")
    value = re.sub(r"(?m)^\s*#{1,6}\s*", "", value)
    value = re.sub(r"(?m)^\s*[-*]\s+", "", value)
    value = re.sub(r"[ \t]+\n", "\n", value)
    return value


def extract_formula_lines(chunks: list[dict], question: str = "") -> list[str]:
    return extract_formula_lines_from_texts([chunk.get("text", "") for chunk in chunks], question)


def estimate_tokens(value: str) -> int:
    return max(1, round(len(value or "") / 4))


def usage_stats(answer: str = "", context: list[dict] | None = None) -> dict:
    context_text = "\n".join(item.get("text", "") for item in (context or []))
    return {
        "answer_chars": len(answer or ""),
        "answer_tokens_est": estimate_tokens(answer),
        "context_chars": len(context_text),
        "context_tokens_est": estimate_tokens(context_text) if context_text else 0,
        "total_tokens_est": estimate_tokens((answer or "") + context_text),
    }


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RAG Agent</title>
  <style>
    :root { color-scheme: light; --bg:#f7f7f8; --panel:#fff; --line:#d9d9df; --text:#1f2328; --muted:#606873; --accent:#1456cc; }
    * { box-sizing: border-box; }
    body { margin:0; font:14px/1.45 system-ui, -apple-system, Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--text); }
    header { padding:14px 20px; border-bottom:1px solid var(--line); background:var(--panel); display:flex; align-items:center; justify-content:space-between; }
    h1 { margin:0; font-size:18px; font-weight:650; }
    main { display:grid; grid-template-columns:minmax(360px, 1fr) minmax(360px, 0.9fr); gap:16px; padding:16px; max-width:1500px; margin:0 auto; }
    section { background:var(--panel); border:1px solid var(--line); border-radius:8px; min-width:0; }
    .pane { padding:14px; }
    textarea, input { width:100%; border:1px solid var(--line); border-radius:6px; padding:10px; font:inherit; background:#fff; }
    textarea { min-height:130px; resize:vertical; }
    .row { display:flex; gap:8px; margin-top:10px; align-items:center; }
    button { border:1px solid #b8c7e8; background:#eef4ff; color:#123f91; border-radius:6px; padding:9px 12px; cursor:pointer; font-weight:600; }
    button.primary { background:var(--accent); color:white; border-color:var(--accent); }
    button.danger { border-color:#e4b5b5; background:#fff0f0; color:#9d1c1c; }
    button:disabled { opacity:.55; cursor:wait; }
    .status { color:var(--muted); font-size:13px; }
    .metrics { display:flex; gap:10px; flex-wrap:wrap; margin-top:10px; color:var(--muted); font-size:13px; }
    .metric { padding:4px 7px; border:1px solid var(--line); border-radius:999px; background:#fafafa; }
    .warning { color:#8a4b00; background:#fff8e8; border:1px solid #f1d39a; border-radius:6px; padding:7px 9px; margin:6px 0; }
    .answer { white-space:normal; }
    .block { padding:12px 14px; border-top:1px solid var(--line); }
    .block:first-child { border-top:0; }
    .title { font-weight:650; margin-bottom:8px; }
    .source { color:var(--muted); margin-bottom:6px; font-size:13px; }
    .badges { display:flex; gap:6px; flex-wrap:wrap; margin:4px 0 6px; }
    .badge { color:var(--muted); border:1px solid var(--line); background:#fafafa; border-radius:999px; padding:2px 6px; font-size:12px; }
    .context { max-height:72vh; overflow:auto; }
    .chunk { border-top:1px solid var(--line); padding:12px 0; }
    .chunk:first-child { border-top:0; padding-top:0; }
    .text { overflow-wrap:anywhere; }
    .formula { display:inline-block; padding:1px 4px; margin:0 1px; border-radius:4px; background:#f2f4f8; font-family:"Cambria Math","Times New Roman",serif; font-size:1.05em; }
    .mathml { display:inline-block; margin:0 2px; vertical-align:middle; }
    sub { font-size:.72em; vertical-align:sub; }
    @media (max-width: 900px) { main { grid-template-columns:1fr; } }
  </style>
  <script>
    window.MathJax = {
      startup: { typeset: false },
      svg: { fontCache: "global" }
    };
  </script>
  <script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/mml-svg.js"></script>
</head>
<body>
  <header>
    <h1>RAG Agent</h1>
    <div class="status" id="status">готов</div>
  </header>
  <main>
    <section class="pane">
      <div class="title">Вопрос</div>
      <textarea id="question" placeholder="Например: как рассчитываются ИВ0-1 ИВ0 ИВ1 ИС"></textarea>
      <div class="row">
        <button class="primary" id="askBtn">Спросить</button>
        <button id="debugBtn">Debug context</button>
        <button id="clearBtn">Очистить</button>
        <button class="danger" id="cacheBtn">Сброс кэша</button>
      </div>
      <div class="row">
        <input id="loadPath" placeholder="r12.docx">
        <button id="loadBtn">Load</button>
      </div>
    </section>
    <section>
      <div class="block">
        <div class="title">Ответ</div>
        <div class="metrics" id="metrics">
          <span class="metric">время: 0.0с</span>
          <span class="metric">токены: 0</span>
        </div>
        <div class="answer text" id="answer">Пока нет ответа.</div>
      </div>
      <div class="block">
        <div class="title">Расчетные правила</div>
        <div id="formulas" class="text status">Пока нет расчетных правил.</div>
      </div>
      <div class="block">
        <div class="title">Проверка</div>
        <div id="warnings" class="text status">Пока нет проверки.</div>
      </div>
      <div class="block">
        <div class="title">Источники</div>
        <div id="sources" class="text status">Пока нет источников.</div>
      </div>
    </section>
    <section style="grid-column:1 / -1">
      <div class="block context">
        <div class="title">Найденный контекст</div>
        <div id="context" class="text status">Нажми Debug context, чтобы увидеть фрагменты без вызова модели.</div>
      </div>
    </section>
  </main>
<script>
const $ = (id) => document.getElementById(id);
let timerId = null;
let startedAt = 0;

async function post(path, body) {
  setBusy(true);
  try {
    const resp = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    return data;
  } finally {
    setBusy(false);
  }
}

function setBusy(busy) {
  $("askBtn").disabled = busy;
  $("debugBtn").disabled = busy;
  $("loadBtn").disabled = busy;
  $("cacheBtn").disabled = busy;
  if (busy) {
    startedAt = performance.now();
    $("status").textContent = "ИИ думает... 0.0с";
    timerId = setInterval(() => {
      const elapsed = (performance.now() - startedAt) / 1000;
      $("status").textContent = `ИИ думает... ${elapsed.toFixed(1)}с`;
      updateMetrics({elapsed_s: elapsed});
    }, 200);
  } else {
    if (timerId) clearInterval(timerId);
    timerId = null;
    $("status").textContent = "готов";
  }
}

function updateMetrics(data) {
  const elapsed = Number(data?.elapsed_s ?? ((performance.now() - startedAt) / 1000));
  const usage = data?.usage || {};
  const total = usage.total_tokens_est ?? 0;
  const answer = usage.answer_tokens_est ?? 0;
  const context = usage.context_tokens_est ?? 0;
  $("metrics").innerHTML = [
    `<span class="metric">время: ${elapsed.toFixed(1)}с</span>`,
    `<span class="metric">токены ~${total}</span>`,
    `<span class="metric">ответ ~${answer}</span>`,
    `<span class="metric">контекст ~${context}</span>`
  ].join("");
}

function renderSources(items) {
  if (!items || !items.length) return "<span class='status'>Нет источников.</span>";
  return items.map((s, i) => {
    const section = s.section ? `<span class="badge">${escapeHtml(s.section)}</span>` : "";
    return `<div class="source">${i+1}. ${escapeHtml(s.file)} score=${s.score}</div><div class="badges">${section}</div><div>${s.html_preview || ""}</div>`;
  }).join("");
}

function renderContext(items) {
  if (!items || !items.length) return "<span class='status'>Ничего не найдено.</span>";
  return items.map((c, i) => {
    const badges = [
      c.section_path,
      c.chunk_type ? `type: ${c.chunk_type}` : "",
      c.status && c.status !== "active" ? `status: ${c.status}` : ""
    ].filter(Boolean).map(v => `<span class="badge">${escapeHtml(v)}</span>`).join("");
    return `<div class="chunk"><div class="source">${i+1}. ${escapeHtml(c.file)} score=${c.score}</div><div class="badges">${badges}</div><div>${c.html_text}</div></div>`;
  }).join("");
}

function renderFormulas(items) {
  if (!items || !items.length) return "<span class='status'>Расчетные правила не выделены.</span>";
  return items.map((f, i) => `<div class="chunk"><div class="source">${i+1}.</div><div>${f.html}</div></div>`).join("");
}

function renderWarnings(items) {
  if (!items || !items.length) return "<span class='status'>Критичных предупреждений нет.</span>";
  return items.map(w => `<div class="warning">${escapeHtml(w)}</div>`).join("");
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}

function typesetMath() {
  if (window.MathJax?.typesetPromise) {
    window.MathJax.typesetPromise().catch(() => {});
  }
}

$("askBtn").onclick = async () => {
  const question = $("question").value.trim();
  if (!question) return;
  try {
    const workspaceResponse = await fetch("/api/workspace", {cache: "no-store"});
    const workspace = await workspaceResponse.json();
    if (!workspaceResponse.ok || !workspace.active_chat_id) throw new Error(workspace.error || "Нет активного чата");
    const data = await post("/api/ask", {question, chat_id: workspace.active_chat_id});
    $("answer").innerHTML = data.html_answer;
    $("formulas").innerHTML = renderFormulas(data.formulas);
    $("warnings").innerHTML = renderWarnings(data.warnings);
    $("sources").innerHTML = renderSources(data.sources);
    $("context").innerHTML = renderContext(data.context);
    $("status").textContent = data.from_cache ? "ответ из кэша" : `готов за ${data.elapsed_s.toFixed(1)}с`;
    updateMetrics(data);
    typesetMath();
  } catch (e) {
    $("answer").textContent = e.message;
  }
};

$("debugBtn").onclick = async () => {
  const question = $("question").value.trim();
  if (!question) return;
  try {
    const data = await post("/api/debug", {question});
    $("formulas").innerHTML = renderFormulas(data.formulas);
    $("warnings").innerHTML = renderWarnings(data.warnings);
    $("context").innerHTML = renderContext(data.context);
    updateMetrics(data);
    typesetMath();
  } catch (e) {
    $("context").textContent = e.message;
  }
};

$("loadBtn").onclick = async () => {
  const path = $("loadPath").value.trim();
  if (!path) return;
  try {
    const data = await post("/api/load", {path});
    $("status").textContent = `загружено ${data.chunks} чанков`;
  } catch (e) {
    $("status").textContent = e.message;
  }
};

$("clearBtn").onclick = () => {
  $("question").value = "";
  $("answer").textContent = "Пока нет ответа.";
  $("formulas").textContent = "Пока нет расчетных правил.";
  $("warnings").textContent = "Пока нет проверки.";
  $("sources").textContent = "Пока нет источников.";
  $("context").textContent = "Нажми Debug context, чтобы увидеть фрагменты без вызова модели.";
  updateMetrics({elapsed_s: 0, usage: {total_tokens_est: 0, answer_tokens_est: 0, context_tokens_est: 0}});
};

$("cacheBtn").onclick = async () => {
  try {
    const data = await post("/api/cache/clear", {});
    $("status").textContent = data.message || "кэш очищен";
  } catch (e) {
    $("status").textContent = e.message;
  }
};
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/docs":
            self.send_html(SWAGGER_UI_HTML)
            return
        if path == "/api/openapi.json":
            self.send_json(load_openapi_spec())
            return
        if path == "/":
            self.send_json({"service": "RAG Assistant API", "docs": "/docs", "openapi": "/api/openapi.json"})
            return
        if path == "/api/workspace":
            self.send_json(get_workspace(*storage_defaults()))
            return
        if path == "/api/projects":
            self.send_json({"items": list_projects()})
            return
        if path.startswith("/api/projects/") and path.count("/") == 3:
            project_id = path.rsplit("/", 1)[-1]
            project = get_project(project_id)
            if project is None:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(project)
            return
        if path.startswith("/api/projects/") and path.endswith("/chats"):
            project_id = path.split("/")[3]
            self.send_json({"items": list_chats(project_id)})
            return
        if path == "/api/chats":
            self.send_json({"items": list_chats()})
            return
        if path.startswith("/api/chats/") and path.count("/") == 3:
            chat_id = path.rsplit("/", 1)[-1]
            chat = get_chat(chat_id)
            if chat is None:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(chat)
            return
        if path.startswith("/api/chats/") and path.endswith("/messages"):
            chat_id = path.split("/")[3]
            self.send_json({"items": list_messages(chat_id)})
            return
        if path.startswith("/api/messages/") and path.count("/") == 3:
            message_id = path.rsplit("/", 1)[-1]
            message = get_message(message_id)
            if message is None:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(message)
            return
        if path == "/api/models":
            self.handle_models()
            return
        if path == "/api/providers":
            self.handle_provider_get()
            return
        if path == "/api/system-prompts":
            self.handle_system_prompts()
            return
        if path == "/api/flow-mode":
            self.send_json({"mode": load_flow_mode()})
            return
        if path == "/api/settings":
            self.send_json(get_workspace(*storage_defaults()))
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/upload":
                self.handle_upload()
                return

            body = self.read_json()
            if path == "/api/ask":
                self.handle_ask(body)
            elif path == "/api/debug":
                self.handle_debug(body)
            elif path == "/api/models/select":
                self.handle_model_select(body)
            elif path == "/api/providers":
                self.handle_provider_save(body)
            elif path == "/api/providers/test":
                self.handle_provider_test(body)
            elif path == "/api/system-prompts/select":
                self.handle_system_prompt_select(body)
            elif path == "/api/flow-mode":
                self.handle_flow_mode(body)
            elif path == "/api/load":
                self.handle_load(body)
            elif path == "/api/cache/clear":
                self.handle_cache_clear()
            elif path == "/api/settings":
                self.send_json(update_workspace(body, *storage_defaults()))
            elif path == "/api/projects":
                self.send_json(create_project(body), 201)
            elif path.startswith("/api/projects/") and path.count("/") == 3:
                self.send_json({"error": "use PATCH or DELETE for this resource"}, 405)
            elif path.startswith("/api/projects/") and path.endswith("/chats"):
                project_id = path.split("/")[3]
                self.send_json(create_chat(project_id, body, *storage_defaults()), 201)
            elif path == "/api/chats":
                self.send_json(create_chat(body.get("project_id"), body, *storage_defaults()), 201)
            elif path.startswith("/api/chats/") and path.count("/") == 3:
                self.send_json({"error": "use PATCH or DELETE for this resource"}, 405)
            elif path.startswith("/api/chats/") and path.endswith("/messages"):
                chat_id = path.split("/")[3]
                self.send_json(create_message(chat_id, body), 201)
            elif path.startswith("/api/messages/") and path.count("/") == 3:
                self.send_json({"error": "use PATCH or DELETE for this resource"}, 405)
            else:
                self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            rollback_transaction()
            self.send_json({"error": str(exc)}, 500)

    def do_PATCH(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self.read_json()
            if path.startswith("/api/projects/") and path.count("/") == 3:
                updated = update_project(path.rsplit("/", 1)[-1], body)
            elif path.startswith("/api/chats/") and path.count("/") == 3:
                updated = update_chat(path.rsplit("/", 1)[-1], body)
            elif path.startswith("/api/messages/") and path.count("/") == 3:
                updated = update_message(path.rsplit("/", 1)[-1], body)
            else:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json(updated or {"error": "not found"}, 200 if updated else 404)
        except Exception as exc:
            rollback_transaction()
            self.send_json({"error": str(exc)}, 500)

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/projects/") and path.count("/") == 3:
                deleted = delete_project(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/chats/") and path.count("/") == 3:
                deleted = delete_chat(path.rsplit("/", 1)[-1])
            elif path.startswith("/api/messages/") and path.count("/") == 3:
                deleted = delete_message(path.rsplit("/", 1)[-1])
            else:
                self.send_json({"error": "not found"}, 404)
                return
            self.send_json({"deleted": deleted}, 200 if deleted else 404)
        except Exception as exc:
            rollback_transaction()
            self.send_json({"error": str(exc)}, 500)

    def handle_models(self) -> None:
        models = ollama_models()
        saved = load_provider_config()
        selected = _agent.llm_model if _agent is not None else (saved.get("model") if saved.get("provider") == "ollama" else LLM_MODEL)
        self.send_json({"models": models, "selected": selected})

    def handle_model_select(self, body: dict) -> None:
        model = str(body.get("model", "")).strip()
        models = ollama_models()
        if model not in models:
            self.send_json({"error": "Выбранная модель не установлена в Ollama."}, 400)
            return
        with _request_lock:
            agent = get_agent()
            agent.set_llm_model(model)
            config = load_provider_config()
            config.update({"provider": "ollama", "model": model, "base_url": OLLAMA_BASE_URL})
            save_provider_config(config)
        self.send_json({"model": model})

    def handle_provider_get(self) -> None:
        self.send_json({"config": public_provider_config(load_provider_config()), "defaults": DEFAULTS})

    def handle_system_prompts(self) -> None:
        available = profiles(GROUNDED_SYSTEM_PROMPT)
        selected = load_system_prompt_id()
        if not any(item["id"] == selected for item in available):
            selected = "rag-grounded"
        self.send_json({
            "selected": selected,
            "prompts": [{key: item[key] for key in ("id", "name", "description")} for item in available],
        })

    def handle_system_prompt_select(self, body: dict) -> None:
        prompt_id = str(body.get("prompt_id", "")).strip()
        available = profiles(GROUNDED_SYSTEM_PROMPT)
        if not any(item["id"] == prompt_id for item in available):
            self.send_json({"error": "Неизвестный системный промт."}, 400)
            return
        save_system_prompt_id(prompt_id)
        self.send_json({"selected": prompt_id})

    def handle_flow_mode(self, body: dict) -> None:
        mode = str(body.get("mode", "")).strip()
        if mode not in FLOW_MODES:
            self.send_json({"error": "Неизвестный режим flow."}, 400)
            return
        previous_mode = load_flow_mode()
        # Python and Rust share cache.db.  Clear it before saving a new mode so
        # an answer produced by the previous flow can never be reused.
        if mode != previous_mode:
            with _request_lock:
                get_agent().clear_cache()
        save_flow_mode(mode)
        self.send_json({"mode": mode, "cache_cleared": mode != previous_mode})

    def handle_provider_save(self, body: dict) -> None:
        provider = str(body.get("provider", "")).strip()
        if provider not in {"ollama", *DEFAULTS}:
            self.send_json({"error": "Неподдерживаемый провайдер."}, 400)
            return
        previous = load_provider_config()
        config = {
            "provider": provider,
            "model": str(body.get("model", "")).strip(),
            "base_url": str(body.get("base_url", "")).strip(),
            "folder_id": str(body.get("folder_id", "")).strip(),
            "auth_type": str(body.get("auth_type", "api_key")).strip(),
        }
        api_key = str(body.get("api_key", "")).strip()
        config["api_key"] = api_key or (str(previous.get("api_key", "")) if previous.get("provider") == provider else "")
        if provider == "ollama":
            models = ollama_models()
            if config["model"] not in models:
                self.send_json({"error": "Выбранная модель не установлена в Ollama."}, 400)
                return
        elif not config["model"] or not config["api_key"]:
            self.send_json({"error": "Укажите модель и API-ключ."}, 400)
            return
        with _request_lock:
            get_agent().set_llm_provider(config)
            save_provider_config(config)
        self.send_json({"config": public_provider_config(config)})

    def handle_provider_test(self, body: dict) -> None:
        config = load_provider_config()
        for key in ("provider", "model", "base_url", "folder_id", "auth_type"):
            if key in body:
                config[key] = str(body[key]).strip()
        if str(body.get("api_key", "")).strip():
            config["api_key"] = str(body["api_key"]).strip()
        if config.get("provider") == "ollama":
            self.send_json({"message": f"Ollama: модель {config.get('model')} доступна."})
            return
        llm = ProviderLLM(**config)
        response = llm.complete("Ответь одним словом: OK")
        self.send_json({"message": f"Соединение успешно: {str(response).strip()[:120]}"})

    def handle_ask(self, body: dict) -> None:
        question = str(body.get("question", "")).strip()
        if not question:
            self.send_json({"error": "empty question"}, 400)
            return
        chat_id = str(body.get("chat_id", "")).strip()
        scope = get_project_conversation(chat_id)
        if scope is None:
            self.send_json({"error": "chat not found"}, 404)
            return

        mode = load_flow_mode()
        agent = get_agent()
        t0 = time.time()
        with _request_lock:
            profile = get_profile(load_system_prompt_id(), GROUNDED_SYSTEM_PROMPT)
            scoped_prompt = f"{profile['prompt']}\n\n{project_context_prompt(scope)}"
            if mode == "python":
                answer, sources, from_cache = agent.ask(question, scoped_prompt, profile["id"])
                context = agent.retrieve_context(question, TOP_K)
            elif mode == "rust":
                python_items = agent.retrieve_context(question, TOP_K)
                rust_items = filter_rust_context(question, rust_context(question))
                context = fuse_contexts(python_items, rust_items, TOP_K)
                rust_result = rust_generate(question, context, project_context_prompt(scope))
                if isinstance(rust_result, tuple):
                    answer, from_cache = rust_result
                else:  # Keeps the callable easy to mock in contract tests.
                    answer, from_cache = str(rust_result), False
                sources = [{
                    "file": item["file"], "section": item.get("section_path", ""), "score": item["score"],
                    "text_preview": item["text"][:120],
                } for item in context]
            else:
                python_items = agent.retrieve_context(question, TOP_K)
                rust_items = filter_rust_context(question, rust_context(question))
                context = fuse_contexts(python_items, rust_items, TOP_K)
                spec = refine_prism_query(question)
                calculations = extract_formula_lines(context, question)
                answer = agent._generate_grounded_answer(question, context, calculations, spec, scoped_prompt)
                from_cache = bool(getattr(agent, "_last_generation_from_cache", False))
                sources = [{"file": item["file"], "section": item.get("section_path", ""), "score": item["score"], "text_preview": item["text"][:120]} for item in context]
        formulas = extract_formula_lines(context, question)
        warnings = validate_retrieval(question, context, answer)
        self.send_json({
            "answer": answer,
            "html_answer": render_text(answer),
            "formulas": [{"text": f, "html": render_text(f)} for f in formulas],
            "warnings": warnings,
            "sources": [
                {**s, "html_preview": render_text(str(s.get("text_preview", "")))}
                for s in sources
            ],
            "context": [
                {**c, "html_text": render_text(c["text"])}
                for c in context
            ],
            "from_cache": from_cache,
            "llm": {**llm_label(), "label": f"{mode.title()} flow · {llm_label()['label']}"},
            "flow_mode": mode,
            "elapsed_s": time.time() - t0,
            "usage": usage_stats(answer, context),
        })

    def handle_debug(self, body: dict) -> None:
        question = str(body.get("question", "")).strip()
        if not question:
            self.send_json({"error": "empty question"}, 400)
            return
        with _request_lock:
            mode = load_flow_mode()
            python_items = get_agent().retrieve_context(question, TOP_K)
            rust_items = filter_rust_context(question, rust_context(question)) if mode != "python" else []
            context = python_items if mode == "python" else fuse_contexts(python_items, rust_items, TOP_K)
        formulas = extract_formula_lines(context, question)
        warnings = validate_retrieval(question, context)
        self.send_json({
            "formulas": [{"text": f, "html": render_text(f)} for f in formulas],
            "warnings": warnings,
            "context": [
                {**c, "html_text": render_text(c["text"])}
                for c in context
            ],
            "elapsed_s": 0,
            "usage": usage_stats("", context),
        })

    def handle_cache_clear(self) -> None:
        with _request_lock:
            get_agent().clear_cache()
        self.send_json({"message": "кэш ответов очищен"})

    def handle_load(self, body: dict) -> None:
        raw_path = str(body.get("path", "")).strip()
        if not raw_path:
            self.send_json({"error": "empty path"}, 400)
            return
        qdrant_error = self.qdrant_error()
        if qdrant_error:
            self.send_json({"error": qdrant_error}, 503)
            return
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        with _request_lock:
            chunks = get_agent().ingest(str(path))
        self.send_json({"chunks": chunks})

    def handle_upload(self) -> None:
        files = self.read_multipart_files()
        if not files:
            self.send_json({"error": "no files uploaded"}, 400)
            return

        qdrant_error = self.qdrant_error()
        if qdrant_error:
            self.send_json({"error": qdrant_error}, 503)
            return

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        loaded = []
        total_chunks = 0

        with _request_lock:
            agent = get_agent()
            for item in files:
                safe_name = self.safe_upload_name(item["filename"])
                target = UPLOAD_DIR / f"{uuid.uuid4().hex}_{safe_name}"
                target.write_bytes(item["content"])
                chunks = agent.ingest(str(target))
                total_chunks += chunks
                loaded.append({
                    "file": safe_name,
                    "stored_path": str(target),
                    "chunks": chunks,
                })

        self.send_json({"files": loaded, "chunks": total_chunks})

    @staticmethod
    def qdrant_error() -> str | None:
        try:
            with urlopen(f"{QDRANT_URL.rstrip('/')}/readyz", timeout=3) as response:
                if response.status < 500:
                    return None
        except URLError:
            pass
        except TimeoutError:
            pass
        return f"Qdrant не запущен или недоступен: {QDRANT_URL}. Запусти Qdrant на порту 6333 и повтори загрузку."

    def read_multipart_files(self) -> list[dict]:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return []

        length = int(self.headers.get("Content-Length", "0"))
        environ = {
            "REQUEST_METHOD": "POST",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(length),
        }
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ=environ,
            keep_blank_values=True,
        )
        files = []

        for field_name in ("files", "file"):
            if field_name not in form:
                continue
            items = form[field_name]
            if not isinstance(items, list):
                items = [items]
            for item in items:
                filename = getattr(item, "filename", "") or ""
                if not filename:
                    continue
                content = item.file.read()
                if content:
                    files.append({"filename": filename, "content": content})

        return files

    @staticmethod
    def safe_upload_name(filename: str) -> str:
        name = Path(filename).name.strip() or "upload.bin"
        name = re.sub(r"[^A-Za-zА-Яа-я0-9._ -]+", "_", name)
        return name[:180] or "upload.bin"

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        return json.loads(data or "{}")

    def send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: dict, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


def main() -> None:
    try:
        run_migrations()
        get_workspace(*storage_defaults())
    except Exception as exc:
        # Keep API documentation and stateless RAG routes available while a
        # local PostgreSQL container is being repaired or started.
        print("PostgreSQL unavailable at startup; check DATABASE_URL and the container logs.")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Web UI: http://{HOST}:{PORT}")
    print(f"Swagger UI: http://{HOST}:{PORT}/docs")
    server.serve_forever()


if __name__ == "__main__":
    main()
