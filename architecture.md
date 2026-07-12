# Фактическая архитектура RAG-проекта

> Состояние исходного кода после добавления переключателя режимов. Web backend объединяет Python- и Rust-реализации и предоставляет три маршрута обработки вопроса: `python`, `rust`, `hybrid`.

## ASGI, безопасность и телеметрия (инкремент миграции)

Новый входной контур — `asgi_app.py`, запускаемый командой:

```powershell
python -m uvicorn asgi_app:app --host 127.0.0.1 --port 8080
```

`python web_ui.py` запускает тот же ASGI-контур. `Handler` больше не является
внешним сервером: он сохранён только как временный compatibility adapter, пока
его операции не будут вынесены из `web_ui.py` в отдельные сервисы.

На первом инкременте в ASGI перенесён `POST /api/ask`; его синхронный RAG
pipeline исполняется через worker thread, поэтому event loop не блокируется.
Остальные существующие `/api/*` endpoint'ы проходят через защищённый ASGI
facade `legacy_adapter.py`: контракт UI не меняется, но auth, trace и метрики
уже не обходятся. Долгая индексация доступна как job через
`POST /api/jobs/ingestion` и опрашивается `GET /api/jobs/{id}`. Реестр jobs
пока in-memory — при рестарте задача не сохраняется; для production следующим
шагом нужна внешняя очередь и БД.

Контур identity изолирован в `security.py`: route получает `Principal`, а
проверка доступа происходит через `authorize(principal, "rag.access")`.
Режим по умолчанию — `RAG_AUTH_MODE=development`, создающий bootstrap
superadmin с subject из `BOOTSTRAP_SUPERADMIN_SUBJECT` (по умолчанию
`rag-superadmin`). Не задавайте постоянные секреты или пароли в коде. Для
production предназначен `RAG_AUTH_MODE=oidc`; `OidcIdentityProvider` —
выделенная точка для будущей проверки JWT/JWKS, issuer и audience.

`context` в `authorize()` намеренно является частью контракта: текущая
проверка RBAC использует только permission `rag.access`, а будущий ABAC сможет
оценивать атрибуты пользователя и ресурса без изменения route handlers.

Телеметрия задаётся в `telemetry.py`. Доступны `/healthz`, `/readyz` и
`/metrics`; middleware выдаёт `X-Request-ID` и `X-Trace-ID`. Prometheus
метрики покрывают HTTP, решения авторизации и длительность RAG pipeline.
При настройке `OTEL_EXPORTER_OTLP_ENDPOINT` spans экспортируются в OTLP
Collector. В labels не передаются вопрос, subject, токен, document ID или
содержимое ответа — это не допускает high-cardinality и утечки данных.

## Runtime-схема: три режима рядом

```mermaid
flowchart TB
    USER["Пользователь"] --> UI["Web UI<br/>TypeScript · React · Vite<br/>Выбор Python / Rust / Hybrid"]
    UI -->|"GET / POST /api/flow-mode"| CONFIG[(".ingestion_cache/flow_mode.json<br/>JSON · сохранённый режим")]
    UI -->|"POST /api/ask"| API["web_ui.py<br/>Python · ThreadingHTTPServer<br/>Маршрутизация запроса по flow_mode"]
    CONFIG --> API

    subgraph P["FLOW 1 — PYTHON"]
        direction LR
        P_Q["Вопрос"] --> P_SPEC["PrismQuery + targeted queries<br/>Python · проектные правила"]
        P_SPEC --> P_VEC["Vector retrieval<br/>LlamaIndex + Qdrant<br/>rag_docs_v10"]
        P_SPEC --> P_BM["BM25 retrieval<br/>LlamaIndex BM25Retriever<br/>bm25_index_v10/docstore.json"]
        P_VEC --> P_FUSE["QueryFusionRetriever<br/>reciprocal_rerank"]
        P_BM --> P_FUSE
        P_FUSE --> P_RANK["Rule-based reranking<br/>dedup · diversification · PRISM selection"]
        P_RANK --> P_CTX["Python context<br/>top 10 чанков"]
        P_CTX --> P_GEN["Python generation<br/>LlamaIndex Ollama client<br/>LLM по выбранному provider/profile"]
        P_GEN --> P_VAL["Python validation<br/>формулы · термины · retrieval warnings"]
        P_VAL --> P_OUT["Ответ + context + sources"]
    end

    subgraph R["FLOW 2 — RUST"]
        direction LR
        R_Q["Вопрос"] --> R_PYRET["Python retrieval<br/>актуальный rag_docs_v10"]
        R_Q --> R_BRIDGE["Python → Rust bridge<br/>subprocess.run · timeout 180 s"]
        R_BRIDGE -->|":retrieve-json"| R_RSRET["Rust retrieval<br/>knowledge_base · Tantivy"]
        R_RSRET --> R_FILTER["Rust relevance filter<br/>соответствие сущности и операции"]
        R_PYRET --> R_FUSE["Evidence fusion<br/>Python authoritative: до 8 слотов<br/>Rust complement: до 2 слотов"]
        R_FILTER --> R_FUSE
        R_FUSE --> R_CONTEXT["Объединённый актуальный context<br/>dedup · без deleted · top 10"]
        R_CONTEXT --> R_BRIDGE2["Python → Rust :generate-json<br/>question + context + выбранная Ollama model"]
        R_BRIDGE2 --> R_GEN["Rust fresh generation<br/>reqwest → Ollama<br/>cache bypass"]
        R_GEN --> R_POST["Python presentation validation<br/>формулы · warnings · HTML rendering"]
        R_POST --> R_OUT["Ответ Rust + context + sources"]
    end

    subgraph H["FLOW 3 — HYBRID"]
        direction LR
        H_Q["Вопрос"] --> H_PYRET["Python retrieval<br/>актуальный rag_docs_v10"]
        H_Q --> H_BRIDGE["Python → Rust :retrieve-json"]
        H_BRIDGE --> H_RSRET["Rust retrieval + relevance filter"]
        H_PYRET --> H_FUSE["Evidence fusion<br/>Python-first · Rust complement"]
        H_RSRET --> H_FUSE
        H_FUSE --> H_CTX["Объединённый context<br/>top 10"]
        H_CTX --> H_SPEC["Python preparation<br/>PrismQuery + извлечение расчётных правил"]
        H_SPEC --> H_GEN["Python generation<br/>RAGAgent._generate_grounded_answer<br/>выбранный system prompt/provider/model"]
        H_GEN --> H_VAL["Python validation<br/>validate_retrieval + formula processing"]
        H_VAL --> H_OUT["Гибридный ответ + fused context + sources"]
    end

    API -->|"mode = python"| P_Q
    API -->|"mode = rust"| R_Q
    API -->|"mode = hybrid"| H_Q

    P_OUT --> RESPONSE["JSON response → Web UI"]
    R_OUT --> RESPONSE
    H_OUT --> RESPONSE
```

## Семантика режимов

| Режим | Retrieval | Генерация | Финальная обработка |
|---|---|---|---|
| `python` | Python: LlamaIndex, Qdrant `rag_docs_v10`, BM25Retriever, проектный reranking | Python `RAGAgent` через настроенный LLM provider | Python: формулы, источники, validation warnings, HTML |
| `rust` | Python retrieval + отфильтрованный Rust retrieval; Python evidence приоритетен | Rust `generate_from_context()` по объединённому актуальному context | Python: формулы, источники, validation warnings, HTML |
| `hybrid` | Тот же Python-first fusion двух индексов | Python `_generate_grounded_answer()` по объединённому context | Python: PRISM, расчётные правила, validation warnings, HTML |

## Переключение режима

React-компонент предлагает три значения:

```text
python  — Python retrieval + Python generation
rust    — Python/Rust evidence fusion + Rust generation
hybrid  — Python/Rust evidence fusion + Python generation/validation
```

Web UI читает режим через `GET /api/flow-mode` и изменяет через `POST /api/flow-mode`. Backend принимает только `python`, `rust` и `hybrid`. Выбор сохраняется в `.ingestion_cache/flow_mode.json`, поэтому восстанавливается после перезапуска UI/backend. Если режим действительно изменился, перед сохранением нового значения очищается общая таблица `answer_cache` в `cache.db`.

## Python → Rust bridge

Интеграция реализована не через FFI и не через отдельный сетевой сервис. Python запускает Rust CLI как дочерний процесс:

```text
готовый binary: target/debug/llm-rust[.exe]
fallback:       cargo run --quiet
stdin:          :<command> <question>\n:exit\n
stdout:         одна строка JSON
timeout:        180 секунд
```

Машинные команды Rust:

| Команда | Метод Rust | Результат | Использование |
|---|---|---|---|
| `:retrieve-json` | `Agent::retrieve_evidence()` | Массив `RetrievalEvidence` | Hybrid flow и debug context |
| `:ask-json` | `Agent::ask()` | `AgentResponse` | Полный Rust flow |
| `:generate-json` | `Agent::generate_from_context()` | Новый ответ по переданным `question`, `context` и `model` | Текущий Web Rust flow |

Команда `:ask-json` остаётся доступна в CLI, но текущий Web Rust flow её не использует. Backend получает Rust evidence через `:retrieve-json`, объединяет его с актуальным Python evidence и вызывает `:generate-json`. Этот путь намеренно обходит Rust answer cache и всегда генерирует ответ по переданному context.

## Объединение evidence и защита от рассинхронизации индексов

Python- и Rust-индексы физически раздельны и могут содержать разные версии корпуса. Web backend поэтому не считает Rust-индекс единственным источником даже в режиме `rust`:

Зафиксированный дефект возник именно из-за рассинхронизации: Python retrieval работал по актуальному `ruk.docx`, а Rust `knowledge_base` содержал старые чанки из `D:\LLM Rust\r12.docx`. Из-за нерелевантного Rust-контекста Hybrid корректно отказывался формировать неподтверждённый ответ. Исправление выполнено на уровне orchestration и не требует считать два индекса синхронными.

1. Выполняется retrieval в актуальном Python-индексе.
2. Выполняется Rust `:retrieve-json`.
3. Rust evidence фильтруется по сущности и запрошенной операции.
4. `fuse_contexts()` сначала резервирует до `top_k - 2` позиций для Python evidence, затем дополняет Rust evidence.
5. Удаляются точные дубликаты и evidence со статусом `deleted`.
6. Итог ограничивается `TOP_K = 10`.

При наличии десяти Python-кандидатов стандартное распределение — 8 Python + 2 Rust. Поле `flow_origin` показывает происхождение каждого фрагмента.

Для запроса о создании параметра Python reranker и procedural selection приоритетно отбирают разделы:

- `Создание параметра`;
- `Вкладка «Общее»`;
- `Вкладка «Принадлежность»`;
- `Панель инструментов`.

Соседние сценарии получают штрафы или исключаются: создание документов, назначение формул, добавление версии, удаление, макросы, копирование и редактирование параметров.

## Python pipeline

### Индексация

```mermaid
flowchart LR
    DOC["DOCX / PDF / MD / TXT"] --> ING["document_ingestion.py<br/>Python"]
    ING --> XML["DOCX XML parser"]
    ING --> DOCLING["Docling"]
    ING --> UNSTRUCT["Unstructured fallback"]
    ING --> MAMMOTH["Mammoth fallback"]
    XML --> SECTIONS["Section documents + metadata"]
    DOCLING --> SECTIONS
    UNSTRUCT --> SECTIONS
    MAMMOTH --> SECTIONS
    SECTIONS --> SPLIT["LlamaIndex SentenceSplitter<br/>1024 токена · overlap 128"]
    SPLIT --> EMB["OllamaEmbedding<br/>nomic-embed-text"]
    EMB --> QD[("Qdrant HTTP :6333<br/>rag_docs_v10")]
    SPLIT --> DS[("SimpleDocumentStore<br/>bm25_index_v10/docstore.json")]
```

### Запрос

- Vector candidates: `12`.
- BM25 candidates: `12`.
- Fusion: `QueryFusionRetriever(mode="reciprocal_rerank", num_queries=1)`.
- Расширенный candidate pool: `max(top_k × 5, 40)`; при `top_k=10` — 50.
- После fusion применяются проектный reranking, deduplication, diversification и PRISM evidence selection.
- Финальный контекст: `top 10`.
- Answer cache: SQLite `cache.db`, версия ключа задаётся `CACHE_VERSION`.

## Rust pipeline

### Индексация

```mermaid
flowchart LR
    DOC["DOCX / PDF / MD / TXT"] --> PARSE["src/ingest.rs<br/>Rust · ZIP/XML · lopdf · filesystem"]
    PARSE --> CHUNK["Section chunker<br/>1500 символов · overlap 200"]
    CHUNK --> EMB["fastembed<br/>ParaphraseMLMiniLML12V2<br/>384 dimensions"]
    EMB --> QD[("Qdrant gRPC :6334<br/>knowledge_base · cosine")]
    CHUNK --> TAN[("Tantivy 0.22<br/>BM25 fields: text + section")]
```

### Запрос

```mermaid
flowchart LR
    Q["Вопрос"] --> VE["fastembed query embedding"]
    VE --> VS["Qdrant vector search<br/>30 кандидатов · RRF ×1"]
    Q --> BT["Tantivy text + section<br/>15 кандидатов · RRF ×1.5"]
    Q --> AB["Извлечение аббревиатур<br/>2–8 символов"]
    AB --> BS["Tantivy section only<br/>5 на аббревиатуру · RRF ×5"]
    VS --> RRF["Weighted RRF · k=60"]
    BT --> RRF
    BS --> RRF
    RRF --> TOP["Top 10"]
    TOP --> LLM["reqwest → Ollama<br/>model может быть передана из Web config<br/>temperature 0 · seed 42 · top_k 1"]
    LLM --> CACHE[("rusqlite · cache.db")]
```

Параметры Rust Ollama generation для текущего запроса:

```text
think       = false
num_ctx     = 8192
num_predict = 768
temperature = 0.0
seed        = 42
top_k       = 1
top_p       = 1.0
```

`qwen3.5:9b` передаётся из сохранённой Web-конфигурации в `:generate-json`. Thinking отключён на уровне Rust Ollama request. В Python для `qwen3.5:9b` также задан отдельный профиль: `context_window=8192`, `thinking=false`, `num_predict=768`, timeout 120 секунд. Для остальных Ollama-моделей действует Python default profile: окно 16384, `num_predict=1024`, timeout 180 секунд.

## Web-операции, не переключаемые режимом

Текущий selector маршрутизирует обработку вопроса и debug retrieval. Он не маршрутизирует ingestion:

- `/api/load` всегда вызывает Python `RAGAgent.ingest()`;
- `/api/upload` всегда индексирует через Python `RAGAgent`;
- `/api/cache/clear` очищает общую таблицу `answer_cache` через Python connection;
- при фактическом переключении режима эта же таблица очищается автоматически;
- debug в `python` показывает Python retrieval, а в `rust` и `hybrid` — объединённый Python/Rust evidence.

Следствие: документы, загруженные только через Web UI, попадают в Python-индекс. Rust-индекс `knowledge_base` по-прежнему заполняется Rust-командой `:load` отдельно, но его устаревшее или нерелевантное содержимое больше не является единственным context для режимов `rust` и `hybrid`: Web backend объединяет его с актуальным Python evidence.

## Проверки

Повторно выполнено по текущему workspace:

| Проверка | Команда | Результат |
|---|---|---|
| Python routing + PRISM tests | `.venv\\Scripts\\python.exe -m unittest test_flow_modes.py test_prism.py` | 11/11 успешно |
| Rust compile check | `cargo check` | успешно |
| Rust tests | `cargo test` | успешно; в crate сейчас 0 unit tests |
| React production build | `npm run build` в `ui/` | успешно; TypeScript + Vite build |

`test_flow_modes.py` проверяет контракт маршрутизации с mock-объектами: Python flow, fused evidence в Rust/Hybrid, приоритет Python-индекса 8/2, отсечение нерелевантного Rust evidence и очистку кэша при переключении. Это unit/contract-тест, а не запуск реальных Qdrant и Ollama.

## Источники в коде

- Выбор режима и React UI: `ui/src/App.tsx`, `ui/src/styles.css`
- Backend router и subprocess bridge: `web_ui.py`
- Python RAG: `rag_agent.py`
- Python ingestion: `document_ingestion.py`, `docx_parser.py`
- Rust JSON CLI: `src/main.rs`
- Rust orchestration и DTO: `src/agent.rs`
- Rust ingestion: `src/ingest.rs`
- Rust embeddings: `src/embed.rs`
- Rust vector store: `src/store.rs`
- Rust BM25 и retrieval: `src/bm25.rs`, `src/retrieval.rs`
- Rust LLM client: `src/llm.rs`
- Routing tests: `test_flow_modes.py`
