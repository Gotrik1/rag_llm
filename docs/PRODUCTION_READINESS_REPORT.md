# Production readiness report

Date: 2026-09-10. Canonical branch: `dev`. Active application and frontend
release: `6286e2b89`. Rollback release: `8d4d80711`.

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
| Scoped uploads | each entry point has one fixed destination: the question-field `+` uploads only to the current chat, `Память и документы` in the project menu uploads only to that project, and the right technical panel uploads only to the global library | storage/API contracts, UI tests and production browser acceptance |
| Knowledge scope | each project selects `project_only` or `project_and_global`; exact indexed source IDs enforce owner, project and chat visibility before evidence enters the prompt or answer | unit/integration tests, duplicate-name isolation and live UI/API toggle |
| Persistent conversations | chat history and assistant terminal state live in PostgreSQL; a generation task keeps its owner scope after page refresh/disconnect | restart/restore and background-generation contracts |
| Concurrent chats | answers remain attached to their original `chat_id`; different projects and chats do not share context | integration tests and production multi-chat acceptance |
| Queues | ingestion and generation use separate Redis/ARQ queues; uploads return quickly and the UI follows the authoritative registry state | worker/job tests and live production stack |
| Cache correctness | retrieval runs before answer-cache lookup; exact answers based only on active common documents can be reused across users, while any project/chat/history evidence disables cache read and write; the cache has a dedicated bounded Redis and cannot evict queue jobs | cache/invalidation regression tests and ADR-0001 |
| Bounded RAG context | retrieval, reranking and neighbor expansion select a bounded evidence set; complete documents are not copied into the system prompt | PRISM/prompt-injection tests and live token telemetry |
| Query understanding | an LLM planning pass plus deterministic validation interprets misspellings and compound intent before retrieval while preserving identifiers such as `1 ЦК`, `6ЦК` and section numbers | planner regression tests and live malformed-query acceptance |
| Multi-document evidence | price-category calculation questions retrieve definition, calculation, parameters and transmission rules across sources; lexically relevant project/chat evidence receives a bounded top-k quota so a large common document cannot crowd it out; explicit document names route evidence to that visible document | 46 focused PRISM tests, full release suite and live 1/6 ЦК plus ПП 442 acceptance |
| Positive occurrence lists | “where is X mentioned” returns only documents with a confirmed direct occurrence and never pads the answer with documents where the term is absent | occurrence/diversification tests and live `1ЦК` acceptance |
| Document registry | common and current-chat files remain visible in navigation; the project menu opens a dedicated registry with project memory, source scope, original filename, upload time, status, detected version and freshness statement | browser acceptance and deterministic inventory answer |
| Image OCR | PNG, JPEG, WebP, TIFF and BMP are processed locally by Tesseract with Russian and English language packs; the original image remains the cited source | real release-image OCR acceptance and ingestion tests |
| Extractive image answers | explicit transcription requests are answered directly from OCR evidence, including first-line/card requests, without losing the recognized text in a generative round trip | focused PRISM tests and live PNG acceptance |
| Human sources | answer citations and the `Sources` panel link to the original document and show a human locator; internal chunk IDs/scores are not user-facing | source-contract tests and live production answer |
| Formulas | Markdown/KaTeX renders validated LaTeX, including persisted display blocks with duplicated inner delimiters; formula payloads contain equations and variable definitions | formula tests plus live `ИВ0`, `1 ЦК` and malformed `6 ЦК` acceptance |
| Technical panel | `Evidence`, `Sources` and `Debug` show metadata for the selected answer; `Система` exposes the global upload control only to administrators and never accepts project/chat files | UI tests, release-image authorization test and live non-admin acceptance |
| Deletion lifecycle | deleting a document immediately removes it from the visible registry, then a serialized worker removes the source file, Qdrant nodes and BM25/docstore nodes and clears answer cache | queue/API/unit tests and full live delete audit |
| Prompt-injection resistance | uploaded text is treated as evidence, never as a replacement for system/user instructions | policy and injection tests |

## Automated verification

- Full release-image Python suite: `171` tests, `163 passed`, `8 skipped`.
- PostgreSQL integration suite: `6/6 passed` against an isolated temporary
  PostgreSQL 17 database.
- Current branch-aware coverage: `54.8%` across `5,494` statements and `1,948`
  branches. Focused modules include deletion/ingestion tasks `100%`, document
  catalog `90.5%`, legacy adapter `87.1%`, persistence `84.6%`, generation
  worker `80.3%`, policy `77.3%`, document ingestion `70.8%`, cache `69.5%`,
  ASGI layer `63.8%`, DOCX parser `62.2%`, core RAG agent `59.8%`, security
  `59.6%` and web layer `46.1%`.
- UI Vitest: `29/29 passed`.
- UI production build: successful, `2,551` modules transformed.
- Focused local backend gate for authentication, scoping, gateway, jobs and
  cache: `42 passed`.
- Full production image health gates: `compileall`, dependency consistency,
  migrations `0001`-`0004`, Rust release build and container readiness passed.

Six skipped tests are PostgreSQL integration cases that were executed separately
and passed. The remaining two require optional live Qdrant/Ollama fixtures.
They are not silently treated as passed; equivalent public production RAG
behavior was exercised separately. Coverage excludes test and generated
artifact files.

## Live production acceptance

| Check | Result |
| --- | --- |
| Release/rollback | `current` -> `/opt/rag_llm/releases/6286e2b89`; `rollback` -> `/opt/rag_llm/releases/8d4d80711` |
| Artifact | SHA-256 `E3E8B743BBBFA60117E22165D7BAC1C060DF28B629666B52ACF69F580B5A6A0F`; verified before extraction |
| Images | app `sha256:bd4226aee02d...`; proxy `sha256:38bcec7b45b5...`; bridge remains `sha256:42edd40b424c...` |
| Database safety | PostgreSQL backup `/opt/rag_llm/shared/backups/rag-pre-4c94a3a0b-20260910T110822Z.dump` is present (`787,455` bytes, SHA-256 `052691f115594976bce387af3192278ab6cbe908eebeb9747cf2dfce9fba5991`) and passed restore-list validation at deployment |
| Runtime | 19 Compose services running; application, proxy, TLS proxy, LLM bridge, vLLM, PostgreSQL, Redis and Qdrant report healthy where health checks are defined |
| Health | `/healthz` -> `{"status":"ok"}`; `/readyz` -> `{"status":"ready"}` |
| Public access | root returns `200` without VPN/Basic Auth; anonymous `/api/me` returns JSON `401` without `WWW-Authenticate` |
| Registration/session UI | registration created a workspace and user card; repeated reloads preserved the session; explicit logout showed the application login page; re-login restored the same chat without a Basic Auth prompt |
| Upload entry points | the `+` panel contains only `Добавить в текущий чат` and `Загрузить в чат`; the project menu contains `Память и документы`, a project-only registry and `Загрузить в проект`; the non-admin technical panel contains no file picker and reports six available common documents |
| Scope enforcement | project and chat QA files independently reached `Готов`; a second chat retained the project file but showed zero chat files, and returning to the first chat restored only its chat file; a direct non-admin global upload returned `403` |
| Project state | memory text and `Только документы проекта` were saved through the project panel and remained selected after a full browser reload |
| Mobile acceptance | at `390x844`, authenticated reload preserved the workspace, the `+` attachment action remained available and document width equalled viewport width (`390 px`), with no horizontal overflow |
| OCR runtime | release image contains Tesseract language packs `rus`, `eng` and `osd`; the supplied PNG was parsed through the real ingestion path and the final answer reproduced `дай формулу ИВ0` exactly with a clickable original-image source |
| Two-user isolation | foreign project read, foreign chat read and foreign project update returned `404`; both users independently retained their own default workspace |
| CSRF | authenticated mutation without `X-CSRF-Token` returned `403`; the same workflow with the session token succeeded |
| Shared corpus | both independent users saw the same six global documents and downloaded the same original source; no private documents leaked |
| Knowledge modes | `project_and_global` returned six common documents; after switching to `project_only`, a project with no private files returned an empty corpus; the selection persisted in UI/API |
| Multiple chats | two chats in one project retained distinct histories; two independent browser profiles generated answers concurrently and each result remained attached to its original chat |
| Inventory answer | returned all six document names, links, timestamps, processing states and explicit version/freshness caveats without spending LLM context tokens |
| Technical panel | `Evidence` showed latency/usage and validation; `Sources` showed clickable document cards with dates/versions; `Debug` remains available for diagnostic metadata |
| Formula/source answer | current release returned the requested project formula, all variable definitions and the correct substitution `12 / (0.8 x 3) = 5 m3/h`; KaTeX rendered math nodes and the answer linked `qa_project_c67.md` with its human section locator |
| Malformed 6 ЦК question | `что такое 6ЦК и ак она рассчитывается` was interpreted as definition plus calculation; the persisted answer contains three calculation components, two cited documents, `14` rendered KaTeX nodes and `0` `.katex-error` elements after a production reload |
| Multi-source 1 ЦК answer | `Как рассчитывается 1 ЦК?` returned the 1 ЦК formula and parameters from section 5.1 plus the one-rate transmission rule from `Методика расчета стоимости на передачу в.1.docx`; both original documents are clickable, with human locators, and the answer contains `17` KaTeX nodes with `0` errors |
| Positive document list | `В каких документах фигурирует 1ЦК?` returned only the directly matching methodology and did not mention `r12.docx` or any other negative candidate |
| Scoped ПП 442 retrieval | the existing ready project Markdown stayed in its original project; `Как определяется гарантирующий поставщик?` placed ПП 442 at ranks 1-2, while explicit `по ПП 442` returned only that document across all ten evidence slots; no migration to the common corpus was performed |
| Formula DOM | the final browser answer contained `28` KaTeX nodes, `0` `.katex-error` elements, no raw legacy `S_sub(` notation and no visible raw `\\mathrm`; source download completed from the `Sources` panel |
| Context budget | the representative formula answer used about `1,847` context tokens and `1,970` total estimated tokens instead of injecting the complete document |
| Document deletion | both live DELETE requests returned `202`; both worker jobs completed at `100%`; both source files are absent; BM25 has `0` matching entries and a full Qdrant scroll over `13,229` points found `0` remnants |
| Cleanup and preservation | the four earlier QA accounts (`QA c67 A`, `QA c67 B`, `QA Intent 45d`, `QA Scope 4c94`) and the final `QA MultiDoc 4c94` account are disabled and every live QA session is revoked; active QA documents/projects/chats are `0`. Legitimate users, shared documents and private project data were preserved |
| User documentation | the 12-page DOCX and Markdown guide describe the three fixed upload entry points, registration, project/global scope, OCR limits, queues, source routing, formulas and troubleshooting; every rendered page passed visual inspection |

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
only registration and eventually corporate OIDC/MFA. The five disabled QA user
rows and their tombstoned document registry rows remain for auditability; hard
deletion requires separate explicit authorization. Coverage should next be
raised in provider failures, local-auth database error paths, worker failures,
`rag_agent.py` and the legacy web adapter. Qdrant client/server minor versions
should be aligned at the next maintenance window. Image ingestion currently
extracts printed text; understanding charts, handwriting and spatial diagrams
requires a separate vision-model pipeline and its own evaluation corpus.
