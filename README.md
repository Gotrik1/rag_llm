# RAG Assistant

Production-oriented RAG workspace for grounded answers over DOCX, PDF, Markdown,
and text documents. The application combines persistent projects/chats,
asynchronous document ingestion, hybrid Qdrant + BM25 retrieval, exact Redis
caching, source links, formula rendering, RBAC/OIDC, and an observable Compose
deployment.

## What users get

- persistent projects, project memory, chats, and messages in PostgreSQL;
- concurrent work in different chats without mixing late responses;
- server-owned generation that continues when a browser page is refreshed;
- queued, cancellable ingestion with visible document status and chunk count;
- project/chat document isolation plus a visible project-memory registry;
- grounded answers from selected chunks rather than entire documents in prompts;
- clickable source files and section labels;
- Markdown, tables, inline math, and display math rendered in the UI;
- Python, Rust, and hybrid retrieval modes with exact, namespaced Redis caching.

## Start here

- [User guide](docs/USER_GUIDE_RU.md) — detailed Russian-language instructions.
- [Architecture](architecture.md) — current runtime and data-flow boundaries.
- [Deployment](DEPLOYMENT.md) — production deployment, verification, backup, and rollback.
- [Historical production report](docs/PRODUCTION_READINESS_REPORT.md) — release-scoped capabilities and tests, not current answer-quality certification.
- [RAG quality gate](docs/QUALITY_EVALUATION.md) — corpus-level 9/10 acceptance method.
- [Context ablation](docs/ORACLE_CONTEXT_EVALUATION.md) — isolate evidence-selection errors from generation errors.
- [Evidence-first pipeline](docs/CORRECTIVE_RAG.md) — bounded corrective search, traces, evaluation and rollback.
- Live API schema: `/api/openapi.json`; Swagger UI: `/docs`.

## Local verification

The canonical checkout is this directory on branch `dev`. The authoritative
stack is `compose.yaml`, Compose project `rag-assistant`.

```powershell
git status --short --branch
docker compose -f compose.yaml config --quiet
python -m pytest -q
Push-Location ui
npm test -- --run
npm run build
Pop-Location
cargo test
```

Integration tests need a PostgreSQL URL. With a disposable local PostgreSQL,
set `DATABASE_URL` explicitly before running `test_workspace_integration.py`.
Never point destructive tests at production.

## Production quick start

Do not deploy from an arbitrary worktree. Follow [DEPLOYMENT.md](DEPLOYMENT.md),
create `.env`, secret files, and TLS material, then validate the fully rendered
configuration before starting it:

```powershell
docker compose -f compose.yaml --env-file .env config --quiet
docker compose -f compose.yaml --env-file .env build
docker compose -f compose.yaml --env-file .env up -d
docker compose -f compose.yaml --env-file .env ps
```

The HTTP proxy binds to loopback by default. The TLS proxy is the intended
external entry point. PostgreSQL, Redis, Qdrant, the API, and telemetry stores
remain on internal networks.

## Repository map

| Path | Responsibility |
| --- | --- |
| `asgi_app.py` | FastAPI facade, auth, persistent generation, uploads, jobs, metrics |
| `web_ui.py` | RAG request orchestration and compatibility routes |
| `rag_agent.py` | chunking, hybrid retrieval, reranking, grounding, formulas, cache keys |
| `corrective_rag.py` | opt-in bounded planning, repeated search, coverage and answer checks |
| `evidence_selection.py` | applicability-aware section and paragraph packing |
| `grounding_verifier.py` | source-linked claim checks, conservative formula and arithmetic validation |
| `conversation_context.py` | current-chat subject, focus and exclusions from persisted user turns |
| `db_store.py` | PostgreSQL workspace/document CRUD and migrations |
| `ingestion_worker.py` | ARQ single-writer ingestion worker |
| `response_cache.py` | exact Redis cache with TTL and namespace isolation |
| `ui/` | React/TypeScript product UI |
| `src/` | Rust retrieval/generation CLI |
| `migrations/` | additive SQL schema changes |
| `deploy/` | proxy, observability, secrets templates, and container entrypoint |

## Safety invariants

- Never commit `.env`, secret values, certificates, uploaded documents, or runtime data.
- Never use `docker compose down -v` against an environment whose data must survive.
- Keep `RAG_WORKER_CONCURRENCY=1` while BM25 uses one shared snapshot.
- Scale `RAG_GENERATION_CONCURRENCY` independently from ingestion, after
  measuring model memory and tokens-per-second under concurrent prompts.
- Keep production authentication in `oidc` mode with real issuer/audience/JWKS values.
- Treat answer text as a synthesis; use its source links to verify critical facts.
