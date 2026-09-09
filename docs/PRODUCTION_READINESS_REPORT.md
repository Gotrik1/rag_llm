# Production readiness report

Date: 2026-09-09. Canonical branch: `dev`. Active application release: `d3f29da69`.

The platform is publicly available without VPN at
`https://5.35.120.112:8443/`. The former browser Basic Auth challenge has been
removed. The application now presents its own registration and login page and
keeps an authenticated session in a secure HttpOnly cookie.

## Delivered capabilities

| Area | Production behavior | Evidence |
| --- | --- | --- |
| Local accounts | self-service registration, login, logout, normalized unique email, Argon2id password hashes, generic authentication errors and lockout after repeated failures | authentication/security unit tests and live browser/API acceptance |
| Durable sessions | opaque server-side session tokens survive refresh and browser navigation; only a SHA-256 token digest is stored; default lifetime is seven days | browser reload and logout/login acceptance |
| CSRF protection | every authenticated state-changing API request requires a session-bound HMAC token | unit tests and live missing-token rejection (`403`) |
| Per-user isolation | projects, chats, messages, workspace settings and private documents are scoped to the authenticated owner | PostgreSQL integration tests and two-user live API acceptance |
| Shared library | all six documents that existed before account migration are global and readable/retrievable by every authenticated user | migration check, DB invariant and two-user acceptance |
| Project documents | every ordinary upload is private to the selected project/chat; another user receives `404` for the owning project/chat/document | storage/API contracts |
| Knowledge scope | each project selects `project_only` or `project_and_global`; retrieval exclusions enforce the choice before vector search | unit/integration tests and live UI/API toggle |
| Persistent conversations | chat history and assistant terminal state live in PostgreSQL; a generation task keeps its owner scope after page refresh/disconnect | restart/restore and background-generation contracts |
| Concurrent chats | answers remain attached to their original `chat_id`; different projects and chats do not share context | integration tests and production multi-chat acceptance |
| Queues | ingestion and generation use separate Redis/ARQ queues; uploads return quickly and the UI follows the authoritative registry state | worker/job tests and live production stack |
| Cache correctness | cache keys include corpus revision, project/chat scope and flow; document or flow changes invalidate stale results | cache/invalidation regression tests |
| Bounded RAG context | retrieval, reranking and neighbor expansion select a bounded evidence set; complete documents are not copied into the system prompt | PRISM/prompt-injection tests and live token telemetry |
| Document registry | UI shows separate `Общие документы` and `Документы проекта`, original filename, upload time, status, detected version and freshness statement | browser acceptance and deterministic inventory answer |
| Human sources | answer citations and the `Sources` panel link to the original document and show a human locator; internal chunk IDs/scores are not user-facing | source-contract tests and live production answer |
| Formulas | Markdown/KaTeX renders validated LaTeX; formula payloads contain equations and variable definitions | formula tests and live `ИВ0` acceptance |
| Technical panel | `Evidence`, `Sources` and `Debug` show metadata for the selected answer and restore it with the chat | UI tests and live tab-by-tab acceptance |
| Prompt-injection resistance | uploaded text is treated as evidence, never as a replacement for system/user instructions | policy and injection tests |

## Automated verification

- Full release-image Python suite: `126` tests, `124 passed`, `2 skipped`, one
  upstream Starlette/httpx deprecation warning; isolated temporary PostgreSQL
  database and role were removed after the run.
- Branch-aware coverage over production Python modules: `55.7%` across `4,694`
  statements and `1,624` branches. Focused modules include document catalog
  `90.5%`, persistence `85.0%`, generation worker `80.3%`, document ingestion
  `77.9%`, scoped DB store `75.7%`, cache `69.5%`, ASGI layer `63.6%`, DOCX
  parser `62.2%`, web layer `54.0%`, and core RAG agent `42.8%`.
- UI Vitest: `21/21 passed` (`7` API tests and `14` application tests).
- UI production build: successful, `2,522` modules transformed.
- Focused local backend gate for authentication, scoping, gateway, jobs and
  cache: `42 passed`.
- Full production image health gates: `compileall`, dependency consistency,
  migrations `0001`-`0004`, Rust release build and container readiness passed.

The two skipped Python tests require optional live Qdrant/Ollama fixtures. They
are not silently treated as passed; equivalent public production RAG behavior
was exercised separately. Coverage is reported without test files inflating
the percentage.

## Live production acceptance

| Check | Result |
| --- | --- |
| Release/rollback | `current` -> `/opt/rag_llm/releases/d3f29da69`; `rollback` -> `/opt/rag_llm/releases/b1049e1dd` |
| Artifact | `29,102,412` bytes; SHA-256 `00207C4EF2CB2BA2190E447229214ED9FA2495FDE4C0A4A903070791DB5CC69B` |
| Database safety | pre-migration PostgreSQL backup `/opt/rag_llm/shared/backups/rag-pre-d3f29da69-20260909T180052Z.dump` passed `pg_restore --list` validation |
| Runtime | 19 Compose services running; application, proxy, TLS proxy, LLM bridge, vLLM, PostgreSQL, Redis and Qdrant report healthy where health checks are defined |
| Health | `/healthz` -> `{"status":"ok"}`; `/readyz` -> `{"status":"ready"}` |
| Public access | root returns `200` without VPN/Basic Auth; anonymous `/api/me` returns JSON `401` without `WWW-Authenticate` |
| Registration UI | registration created a workspace and user card; reload restored the same session without another login |
| Two-user isolation | foreign project read, foreign chat read and foreign project update returned `404`; both users independently retained their own default workspace |
| CSRF | authenticated mutation without `X-CSRF-Token` returned `403`; the same workflow with the session token succeeded |
| Shared corpus | both independent users saw the same six global documents and downloaded the same original source; no private documents leaked |
| Knowledge modes | `project_and_global` returned six common documents; after switching to `project_only`, a project with no private files returned an empty corpus; the selection persisted in UI/API |
| Multiple chats | two chats were created and listed under one project with distinct identifiers |
| Inventory answer | returned all six document names, links, timestamps, processing states and explicit version/freshness caveats without spending LLM context tokens |
| Technical panel | `Evidence` showed latency/usage and validation; `Sources` showed clickable document cards with dates/versions; `Debug` remains available for diagnostic metadata |
| Formula/source answer | current release answered `ИВ0` in `7.324 s`, returned one formula and one clickable `r12.docx` source with a human section locator |
| Context budget | live `ИВ0` answer used about `2,895` context tokens and `3,118` total estimated tokens instead of the complete document |
| Cleanup | all four temporary acceptance accounts and their projects/chats/messages were removed; final DB state is `0` app users, `6` active global documents and `0` private documents |

The representative semantic cases meet the project threshold of at least 9/10.
This is a release acceptance result, not a mathematical guarantee for every
future document: scans, damaged tables, missing editions or new domains require
their own version-controlled evaluation questions.

## Deployment and rollback

Deployment was atomic. Existing PostgreSQL, Redis, Qdrant and vLLM data volumes
were retained. The release archive was hash-checked before extraction, database
backup was taken before migration, and the prior release remains available via
the `rollback` symlink. Operational/user documentation is also copied to
`/opt/rag_llm/shared/docs`, outside rotating release directories.

## Known follow-up hardening

Self-service local authentication is appropriate for the requested public IP
deployment, but production operations still need a deliberate account policy:
email verification/recovery, administrator provisioning, optional invitation-
only registration and eventually corporate OIDC/MFA. Coverage should next be
raised in provider failures, local-auth database error paths, worker failures,
`rag_agent.py` and the legacy web adapter. Qdrant client/server minor versions
should be aligned at the next maintenance window.
