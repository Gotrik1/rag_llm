# Production readiness report

Date: 2026-09-09. Branch: `dev`. Application release `3c9e780e6` is deployed on
VM `iprg001` and is available at `https://5.35.120.112:8443/`. The external TLS
gateway requires Basic Auth; anonymous and invalid credentials are rejected.
O2 Cloud Edge Firewall permits public ingress only to TCP/8443 for this service;
the existing DNAT forwards that port to `192.168.30.2`. The endpoint therefore
does not require a corporate VPN, while the application remains protected by
the gateway credentials.
Corporate OIDC remains the required next step for named users, per-user RBAC and
audit attribution.

## Implemented in this hardening pass

| Area | Result | Verification |
| --- | --- | --- |
| Refresh/restart-safe generation | generation is an ARQ job with PostgreSQL state; a dedicated worker survives browser loss and API replacement; restored UI polls `running` records and restores sources/formulas | queue/worker contracts + ASGI/SSE + UI state tests |
| Concurrent chats | request captures original `chat_id`; UI running state is per chat; global model settings remain locked during any generation | TypeScript build and review of state transitions |
| PostgreSQL concurrency | removed process-global psycopg connection; connections are isolated per worker thread and closed after each request | clean PostgreSQL 18 integration suite with `ResourceWarning` promoted to error |
| Separate queues | bounded upload uses a single-writer ingestion worker; answers use a dedicated concurrent generation worker | job routing, worker terminal-state and Compose tests |
| BM25 publication | worker uses atomic snapshot replacement; API workers hot-reload changed snapshot | two docstore refresh tests |
| Document registry | project/chat listing, status, progress, chunks and source download links exposed in UI/API | ASGI registry tests + UI build |
| Workspace/version awareness | inventory questions are answered deterministically from the scoped PostgreSQL registry without retrieval or LLM tokens; the answer includes document link, upload time, processing state, explicit version and a newer-loaded-version warning | intent/version/serialization tests + live durable-queue acceptance |
| Project isolation | documents attached to other project/chats are filtered before reranking; exclusion set is part of cache key | scope unit tests |
| Cache | exact Redis namespace/TTL retained; corpus and flow changes invalidate; scope affects key | response-cache and flow tests |
| Grounding | prompt remains bounded to selected chunks; generalized beyond regulatory documents; textual source policy added | PRISM/prompt-injection contracts |
| Formulas | KaTeX rendering, short Markdown/LaTeX equality extraction, and same-document formula sibling retrieval | formula/PRISM tests + real production browser |
| User-facing sources | citations are the original document plus a human locator and stable content link; UUID filename prefixes, chunk previews and retrieval scores are removed; only actually cited linked locators are returned | 6 source-contract tests + live VM fact/formula acceptance |
| Live API contract | `/api/openapi.json` now serves `app.openapi()` rather than the stale compatibility file | route contract/import test |
| Deployment | local Compose contract is `rag-assistant`; the existing VM project remains `rag-platform` to preserve named volumes; worker single-writer default and secret templates corrected | rendered Compose validation and live VM services |
| Documentation | root README, current architecture/runbook, deploy inventory, detailed Russian user guide | link/path review |

## Test evidence

Latest completed release-gate runs:

- Final application image: `109` Python unit/contract tests executed, `102` passed and `7` environment-dependent tests skipped; `pip check` reports no broken requirements.
- Earlier clean PostgreSQL 18 integration run: `86 passed, 2 skipped`; both skips are opt-in live model tests.
- Python branch coverage: `52.2%` overall with a CI regression floor of 50%; document ingestion is `77.9%`, deterministic DOCX parsing `62.2%`, durable generation worker `79.7%`, persistence `83.8%`, the legacy adapter `87.1%`, and ASGI routes `63.7%`. CI publishes `coverage.xml`.
- UI Vitest: `14 passed`.
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
| Target/release | passed | `iprg001`; `/opt/rag_llm/current` resolves to `/opt/rag_llm/releases/3c9e780e6`; archive SHA-256 `76fa17674745ccb85a8a642a2e86d6159b67270da14bc54f13836dcc5e3e11fe` is recorded in root-only `/opt/rag_llm/shared/deployed-release.txt` |
| Release artifact | passed | archive was built from tracked files only; runtime data, generated artifacts and secrets were not included; SHA-256 was verified before extraction |
| Backup/rollback | passed | latest backup `/opt/rag_llm/shared/backups/pre-3c9e780e6-20260909T092335Z`; PostgreSQL dump, Qdrant snapshot, app-data archive and `SHA256SUMS`; a failing release test stopped candidate `d870003fa` before activation, proving the test gate and rollback path |
| Migrations/services | passed | migration container exited successfully; app/proxy/LLM bridge healthy; PostgreSQL, Redis, Qdrant, ingestion worker, generation worker, vLLM and telemetry containers running |
| Health | passed | internal HTTP and external TLS `/healthz` both return `{"status":"ok"}` |
| Dependencies/tests | passed | final image `pip check`: no broken requirements; `109` tests executed, `102` passed and `7` environment-dependent integration/live-model tests skipped; UI has `14` passing tests and a successful 2522-module production build |
| Durable generation | passed | a queued answer survived browser refresh and an API/worker replacement, then completed in its original chat |
| Concurrent chats | passed with automated contract evidence | server-owned two-chat test passes; UI keeps independent chat state and lists several project chats concurrently |
| Upload/registry/source | passed | Markdown acceptance file is `ready` with 4 indexed chunks; UI shows it under “Документы в памяти”; the original is available through its document-content link |
| Workspace inventory/version query | passed | production chat `6c99742f-fa79-404a-b472-7d7ea7b4daf2` answered “Какие регламенты у нас есть, когда они загружены и есть ли более актуальные версии?” from the PostgreSQL registry (`flow_mode=workspace`), returned the original document URL and upload time, used zero retrieval chunks, and explicitly did not confuse upload time with document edition or claim an external actuality check |
| Retrieval/formulas | passed | production chat `0ad3e456-1a67-4f25-89d2-58b5139ae110` returned `NEBULA-742`, `I = U/R`, and `I = 3 A`; the fact links to `production_acceptance.md — раздел «Unique fact»`, the formula links to `production_acceptance.md — раздел «Formula»`; the API returns exactly these cited document locators without score, preview or storage filename |
| Queues | passed | default, ingestion and generation ARQ queue lengths are all zero after acceptance; ingestion and generation use distinct queue names |
| Cache | passed at contract level | namespace/TTL, scope key, flow invalidation and corpus invalidation tests pass; a separate live repeat/mutation benchmark was not run because chat history intentionally changes the grounded cache key |
| Project isolation | passed at integration-contract level | cross-project exclusion test passes; a destructive live two-project corpus mutation was not performed on the retained VM data |
| Observability | partially verified | Prometheus, Grafana, Loki, Tempo, Alloy and OpenTelemetry containers are running; dashboard contents and alert delivery were not manually certified in this pass |
| External authentication | passed for restricted single-admin operation | `/nginx-health` returns `200`; anonymous UI access and an invalid password return `401`; valid gateway credentials return `200`; plaintext recovery credentials are mode `0600`, the salted htpasswd hash is `0644` inside a root-only directory |
| Public internet ingress | passed | O2 Cloud Edge rule `RAG_8443_public` allows source `ANY` only through application profile `RAG_HTTPS_8443` (`TCP/8443`) to `192.168.30.2`; independent TCP probes succeeded from five internet nodes (Hungary, Indonesia, Netherlands, Russia and Turkey); an external anonymous HTTPS request returned `401`, an authenticated request returned `200 text/html`, and the VM DNAT counter recorded 8 packets on port 8443 |
| Per-user authentication | remaining enterprise hardening | application auth remains `development` behind the mandatory gateway. Corporate OIDC is still required before issuing individual accounts or claiming per-user RBAC/audit attribution |
| Final runtime audit | passed | `19` services running, internal health `{"status":"ok"}`, default/ingestion/generation queue lengths `0,0,0`; PostgreSQL contains 1 project, 13 acceptance chats, 49 messages and 1 registered document |

The technical rollout is complete and rollback-ready for restricted use through
the mandatory TLS Basic Auth gateway. Before multi-user rollout, the operator
must supply the corporate OIDC issuer, audience, JWKS URL and bootstrap subject,
switch `RAG_AUTH_MODE=oidc`, and verify RBAC with both an allowed and a denied
user. The gateway password was generated independently; no credential from the
VM access document was reused.

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
