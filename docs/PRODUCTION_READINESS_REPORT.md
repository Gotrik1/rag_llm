# Production readiness report

Date: 2026-09-08. Branch: `dev`. This report describes the current checkout;
the deployed VM revision must be recorded separately after deployment.

## Implemented in this hardening pass

| Area | Result | Verification |
| --- | --- | --- |
| Refresh-safe chat generation | assistant placeholder/result owned by server; detached task survives client cancellation | ASGI contract + cancellation regression test |
| Concurrent chats | request captures original `chat_id`; UI running state is per chat; global model settings remain locked during any generation | TypeScript build and review of state transitions |
| PostgreSQL concurrency | removed process-global psycopg connection; connections are isolated per worker thread | Python contracts; PostgreSQL integration pending local Engine recovery |
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

- Python unit/contract suite: `61 passed, 6 skipped`; four skips are the PostgreSQL integration cases gated by `DATABASE_URL`, two are opt-in live tests.
- UI Vitest: `7 passed`.
- UI production build: success, 2522 modules; application code is split from the assistant and Markdown/KaTeX dependency chunks.
- Rust: `cargo test` succeeds; the crate currently contains zero Rust unit tests.
- Compose static validation: succeeds for `compose.yaml`.

The full PostgreSQL integration suite could not run on this workstation because
Docker Desktop 4.71 crashes in its optional Model Runner before the Engine
starts. The exact failing AF_UNIX runtime directories were moved aside without
touching volumes; the second launch exposed the same product bug on newly
created Model Runner sockets. Do not misreport these four tests as passing until
they run against a real PostgreSQL instance or the VM.

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
