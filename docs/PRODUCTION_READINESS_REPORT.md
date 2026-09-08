# Production readiness report

Date: 2026-09-08. Branch: `dev`. This report describes the current checkout;
the deployed VM revision must be recorded separately after deployment.

## Implemented in this hardening pass

| Area | Result | Verification |
| --- | --- | --- |
| Refresh-safe chat generation | assistant placeholder/result owned by server; detached task survives client cancellation; restored UI polls `running` records to a terminal state | ASGI cancellation contract + UI state tests |
| Concurrent chats | request captures original `chat_id`; UI running state is per chat; global model settings remain locked during any generation | TypeScript build and review of state transitions |
| PostgreSQL concurrency | removed process-global psycopg connection; connections are isolated per worker thread and closed after each request | clean PostgreSQL 18 integration suite with `ResourceWarning` promoted to error |
| Async ingestion | bounded multipart upload creates document + ARQ job; worker reports progress/terminal state | upload/job API tests |
| BM25 publication | worker uses atomic snapshot replacement; API workers hot-reload changed snapshot | two docstore refresh tests |
| Document registry | project/chat listing, status, progress, chunks and source download links exposed in UI/API | ASGI registry tests + UI build |
| Project isolation | documents attached to other project/chats are filtered before reranking; exclusion set is part of cache key | scope unit tests |
| Cache | exact Redis namespace/TTL retained; corpus and flow changes invalidate; scope affects key | response-cache and flow tests |
| Grounding | prompt remains bounded to selected chunks; generalized beyond regulatory documents; textual source policy added | PRISM/prompt-injection contracts |
| Formulas | KaTeX/MathJax rendering and existing deterministic formula extraction preserved | formula/PRISM tests + production UI build |
| Live API contract | `/api/openapi.json` now serves `app.openapi()` rather than the stale compatibility file | route contract/import test |
| Deployment | Compose project normalized to `rag-assistant`; worker single-writer default; env and secret templates corrected | `docker compose -f compose.yaml config --quiet` |
| Documentation | root README, current architecture/runbook, deploy inventory, detailed Russian user guide | link/path review |

## Test evidence

Latest completed local runs:

- Python Pytest suite against a clean PostgreSQL 18 cluster: `73 passed, 2 skipped`; both skips are opt-in live model tests. The equivalent CI `unittest discover` run executes 75 tests with 2 skips.
- Python branch coverage: `51.7%` overall with a CI regression floor of 50%; document ingestion is `77.9%`, deterministic DOCX parsing `62.2%`, persistence `83.7%`, the legacy adapter `87.1%`, and ASGI routes `64.6%`. CI publishes `coverage.xml`.
- UI Vitest: `9 passed`.
- UI production build: success, 2522 modules; application code is split from the assistant and Markdown/KaTeX dependency chunks.
- Rust: `cargo test` succeeds; the crate currently contains zero Rust unit tests.
- Compose static validation: succeeds for `compose.yaml`.

The PostgreSQL integration suite was verified without Docker by initializing a
clean temporary PostgreSQL 18 cluster, applying `alembic upgrade head` followed
by the workspace SQL migrations, and running the complete test suite with
`ResourceWarning` treated as an error. This exposed and fixed request-thread
connection cleanup. Docker
Desktop 4.71 still crashes in its optional Model Runner before the Engine starts,
so the full Compose runtime remains a required VM acceptance gate.

## Required VM acceptance evidence

Deployment is not complete until the report records:

- target hostname/install directory and deployed Git SHA/image IDs;
- migration completion and healthy Compose service list;
- authenticated `/healthz`, `/readyz`, document upload/job, source download;
- refresh-during-generation result persistence;
- two simultaneous chats with correct placement;
- two-project source-isolation test;
- formula and table rendering in a real browser;
- exact cache hit and corpus-change miss;
- backup artifact/checksum and rollback image/reference;
- final Python/PostgreSQL integration test counts;
- Grafana/Prometheus/Loki/Tempo verification.

## Residual risks and scaling triggers

1. In-flight generation survives browser disconnect but not an API process/host
   crash. Move generation to a durable queue when zero-loss across deployments
   is required.
2. BM25 is one shared immutable snapshot with a single writer. Shard or move to
   a service before increasing ingestion workers or deploying writers on
   multiple hosts.
3. The frontend bundle should be code-split when cold-load measurements exceed
   the product target; current gzip size is about 318 KB.
4. Answer quality is corpus- and model-dependent. Maintain a representative,
   versioned evaluation set with retrieval recall, citation correctness,
   groundedness, formula exactness, refusal, and latency thresholds before
   claiming a universal numeric score.
5. Rust currently has compilation coverage but no unit tests; production should
   add retrieval/cache protocol tests before making Rust the default flow.
