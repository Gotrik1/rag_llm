# Production readiness report

Date: 2026-09-08. Branch: `dev`. The application release described below is
deployed on VM `iprg001`; the remaining authentication gate is called out
explicitly and prevents treating the Internet-facing endpoint as security-ready.

## Implemented in this hardening pass

| Area | Result | Verification |
| --- | --- | --- |
| Refresh/restart-safe generation | generation is an ARQ job with PostgreSQL state; a dedicated worker survives browser loss and API replacement; restored UI polls `running` records and restores sources/formulas | queue/worker contracts + ASGI/SSE + UI state tests |
| Concurrent chats | request captures original `chat_id`; UI running state is per chat; global model settings remain locked during any generation | TypeScript build and review of state transitions |
| PostgreSQL concurrency | removed process-global psycopg connection; connections are isolated per worker thread and closed after each request | clean PostgreSQL 18 integration suite with `ResourceWarning` promoted to error |
| Separate queues | bounded upload uses a single-writer ingestion worker; answers use a dedicated concurrent generation worker | job routing, worker terminal-state and Compose tests |
| BM25 publication | worker uses atomic snapshot replacement; API workers hot-reload changed snapshot | two docstore refresh tests |
| Document registry | project/chat listing, status, progress, chunks and source download links exposed in UI/API | ASGI registry tests + UI build |
| Project isolation | documents attached to other project/chats are filtered before reranking; exclusion set is part of cache key | scope unit tests |
| Cache | exact Redis namespace/TTL retained; corpus and flow changes invalidate; scope affects key | response-cache and flow tests |
| Grounding | prompt remains bounded to selected chunks; generalized beyond regulatory documents; textual source policy added | PRISM/prompt-injection contracts |
| Formulas | KaTeX rendering, short Markdown/LaTeX equality extraction, and same-document formula sibling retrieval | formula/PRISM tests + real production browser |
| Live API contract | `/api/openapi.json` now serves `app.openapi()` rather than the stale compatibility file | route contract/import test |
| Deployment | local Compose contract is `rag-assistant`; the existing VM project remains `rag-platform` to preserve named volumes; worker single-writer default and secret templates corrected | rendered Compose validation and live VM services |
| Documentation | root README, current architecture/runbook, deploy inventory, detailed Russian user guide | link/path review |

## Test evidence

Latest completed local runs:

- Python Pytest suite against a clean PostgreSQL 18 cluster: `86 passed, 2 skipped`; both skips are opt-in live model tests. The equivalent CI `unittest discover` run executes 88 tests with 2 skips.
- Python branch coverage: `52.2%` overall with a CI regression floor of 50%; document ingestion is `77.9%`, deterministic DOCX parsing `62.2%`, durable generation worker `79.7%`, persistence `83.8%`, the legacy adapter `87.1%`, and ASGI routes `63.7%`. CI publishes `coverage.xml`.
- UI Vitest: `11 passed`.
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

## VM deployment evidence

| Check | Result | Evidence |
| --- | --- | --- |
| Target/release | passed | `iprg001`; `/opt/rag_llm/current` points to an immutable release; the exact revision and archive SHA-256 are recorded in root-only `/opt/rag_llm/shared/deployed-release.txt`; accepted application-code baseline is `941c499da` |
| Release artifact | passed | release archive was built from tracked files with runtime data, generated artifacts and secrets explicitly excluded; hash was verified before extraction |
| Backup/rollback | passed | latest backup `/opt/rag_llm/shared/backups/pre-941c499da-20260908T200340Z`; PostgreSQL dump, Qdrant snapshot, app-data archive and `SHA256SUMS`; two deliberately triggered gates proved automatic rollback before final activation |
| Migrations/services | passed | migration container exited successfully; app/proxy/LLM bridge healthy; PostgreSQL, Redis, Qdrant, ingestion worker, generation worker, vLLM and telemetry containers running |
| Health | passed | internal HTTP and external TLS `/healthz` both return `{"status":"ok"}` |
| Dependencies/tests | passed | final image `pip check`: no broken requirements; final image `93` unit/contract tests passed, `7` environment-dependent integration/live-model tests skipped |
| Durable generation | passed | a queued answer survived browser refresh and an API/worker replacement, then completed in its original chat |
| Concurrent chats | passed with automated contract evidence | server-owned two-chat test passes; UI keeps independent chat state and lists several project chats concurrently |
| Upload/registry/source | passed | Markdown acceptance file became `ready` with 4 chunks; UI shows it under “Документы в памяти”; the original is available through its document-content link |
| Retrieval/formulas | passed | clean production chat returned `NEBULA-742`, `I = U/R`, and `I = 3 A`; answer cites both `Unique fact` and `Formula`; browser accessibility tree contains real KaTeX `math` nodes |
| Queues | passed | default, ingestion and generation ARQ queue lengths are all zero after acceptance; ingestion and generation use distinct queue names |
| Cache | passed at contract level | namespace/TTL, scope key, flow invalidation and corpus invalidation tests pass; a separate live repeat/mutation benchmark was not run because chat history intentionally changes the grounded cache key |
| Project isolation | passed at integration-contract level | cross-project exclusion test passes; a destructive live two-project corpus mutation was not performed on the retained VM data |
| Observability | partially verified | Prometheus, Grafana, Loki, Tempo, Alloy and OpenTelemetry containers are running; dashboard contents and alert delivery were not manually certified in this pass |
| Authentication | **blocked** | VM currently has `RAG_AUTH_MODE=development` and no OIDC issuer; requests receive the development bootstrap superadmin identity. This is unacceptable for an open production endpoint. |

The technical rollout is complete and rollback-ready, but security acceptance is
not complete until the operator supplies the corporate OIDC issuer, audience,
JWKS URL and bootstrap subject, switches `RAG_AUTH_MODE=oidc`, and verifies RBAC
with both an allowed and a denied user. No credential from the VM access document
was reused as an application password.

## Residual risks and scaling triggers

1. In-flight generation survives browser and API-process replacement through
   Redis/ARQ plus PostgreSQL state. A simultaneous loss of Redis AOF and
   PostgreSQL still requires infrastructure restore from a consistent backup.
2. BM25 is one shared immutable snapshot with a single writer. Shard or move to
   a service before increasing ingestion workers or deploying writers on
   multiple hosts.
3. The frontend bundle should be code-split when cold-load measurements exceed
   the product target; current gzip size is about 318 KB.
4. Answer quality is corpus- and model-dependent. `quality_eval.py` now enforces
   a per-question 9/10 threshold for facts, citations, formulas, refusal, and
   latency. A representative production JSONL set and VM run are still required
   before claiming the score for the real corpus.
5. Rust currently has compilation coverage but no unit tests; production should
   add retrieval/cache protocol tests before making Rust the default flow.
6. The Qdrant Python client is `1.19.0` while the server is `1.17.1`; the client
   warns that the minor-version gap exceeds its compatibility policy. Pin or
   upgrade the pair together in the next maintenance release.
