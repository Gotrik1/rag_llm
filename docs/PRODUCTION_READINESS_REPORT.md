# Production readiness report

Date: 2026-09-10. Canonical branch: `dev`. Active application release: `8cff19c34`.

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
| Scoped uploads | the `+` panel accepts documents and images and makes the destination explicit: only the current chat or every chat in the current project; another user receives `404` for the owning project/chat/document | storage/API contracts and desktop/mobile browser acceptance |
| Knowledge scope | each project selects `project_only` or `project_and_global`; retrieval exclusions enforce the choice before vector search | unit/integration tests and live UI/API toggle |
| Persistent conversations | chat history and assistant terminal state live in PostgreSQL; a generation task keeps its owner scope after page refresh/disconnect | restart/restore and background-generation contracts |
| Concurrent chats | answers remain attached to their original `chat_id`; different projects and chats do not share context | integration tests and production multi-chat acceptance |
| Queues | ingestion and generation use separate Redis/ARQ queues; uploads return quickly and the UI follows the authoritative registry state | worker/job tests and live production stack |
| Cache correctness | cache keys include corpus revision, project/chat scope and flow; document or flow changes invalidate stale results | cache/invalidation regression tests |
| Bounded RAG context | retrieval, reranking and neighbor expansion select a bounded evidence set; complete documents are not copied into the system prompt | PRISM/prompt-injection tests and live token telemetry |
| Query understanding | an LLM planning pass plus deterministic validation interprets misspellings and compound intent before retrieval while preserving identifiers such as `1 ЦК`, `6ЦК` and section numbers | planner regression tests and live malformed-query acceptance |
| Multi-document evidence | price-category calculation questions retrieve definition, calculation, parameters and transmission rules across sources; a deterministic coverage gate adds a missing relevant transmission citation but never an unrelated document | 39 focused PRISM tests, full release suite and live 1/6 ЦК acceptance |
| Positive occurrence lists | “where is X mentioned” returns only documents with a confirmed direct occurrence and never pads the answer with documents where the term is absent | occurrence/diversification tests and live `1ЦК` acceptance |
| Document registry | UI shows separate `Общие документы`, `Документы проекта` and `Документы чата`, original filename, upload time, status, detected version and freshness statement | browser acceptance and deterministic inventory answer |
| Image OCR | PNG, JPEG, WebP, TIFF and BMP are processed locally by Tesseract with Russian and English language packs; the original image remains the cited source | real release-image OCR acceptance and ingestion tests |
| Extractive image answers | explicit transcription requests are answered directly from OCR evidence, including first-line/card requests, without losing the recognized text in a generative round trip | focused PRISM tests and live PNG acceptance |
| Human sources | answer citations and the `Sources` panel link to the original document and show a human locator; internal chunk IDs/scores are not user-facing | source-contract tests and live production answer |
| Formulas | Markdown/KaTeX renders validated LaTeX; formula payloads contain equations and variable definitions | formula tests and live `ИВ0` acceptance |
| Technical panel | `Evidence`, `Sources` and `Debug` show metadata for the selected answer and restore it with the chat | UI tests and live tab-by-tab acceptance |
| Deletion lifecycle | deleting a document immediately removes it from the visible registry, then a serialized worker removes the source file, Qdrant nodes and BM25/docstore nodes and clears answer cache | queue/API/unit tests and full live delete audit |
| Prompt-injection resistance | uploaded text is treated as evidence, never as a replacement for system/user instructions | policy and injection tests |

## Automated verification

- Full release-image Python suite: `158` tests, `156 passed`, `2 skipped`;
  PostgreSQL integration ran against an isolated temporary database and passed.
- Most recent branch-aware baseline before the final upload/OCR delta: `55.7%`
  across `4,694` statements and `1,624` branches. The current release adds
  targeted upload and OCR tests, but a new whole-repository coverage percentage
  was not recomputed. Focused modules in that baseline include document catalog
  `90.5%`, persistence `85.0%`, generation worker `80.3%`, document ingestion
  `77.9%`, scoped DB store `75.7%`, cache `69.5%`, ASGI layer `63.6%`, DOCX
  parser `62.2%`, web layer `54.0%`, and core RAG agent `42.8%`.
- UI Vitest: `24/24 passed`.
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
| Release/rollback | `current` -> `/opt/rag_llm/releases/8cff19c34`; `rollback` -> `/opt/rag_llm/releases/b00d294488` |
| Artifact | `29,128,550` bytes; SHA-256 `9BB9B542FC5B0228CC410D7A21CF0D9541D2B175C3E20779A701BD9D2893A5EA` |
| Images | app `sha256:f68b55d2ca80...`; proxy `sha256:3e00227870e9...`; bridge `sha256:42edd40b424c...` |
| Database safety | PostgreSQL backup `/opt/rag_llm/shared/backups/rag-pre-8cff19c34-20260910T100753Z.dump` is present (`712,926` bytes, SHA-256 `d7bbe1d379ec627300ec6418955b102dd62c67b2a72c596b02c05684a7d5d282`) and passed restore-list validation at deployment |
| Runtime | 19 Compose services running; application, proxy, TLS proxy, LLM bridge, vLLM, PostgreSQL, Redis and Qdrant report healthy where health checks are defined |
| Health | `/healthz` -> `{"status":"ok"}`; `/readyz` -> `{"status":"ready"}` |
| Public access | root returns `200` without VPN/Basic Auth; anonymous `/api/me` returns JSON `401` without `WWW-Authenticate` |
| Registration/session UI | registration created a workspace and user card; repeated reloads preserved the session; explicit logout showed the application login page; re-login restored the same chat without a Basic Auth prompt |
| Attachment UI | mobile and desktop views show the `+` action, explicit chat/project scope, separate document/image selectors, selected filenames and the correct submit label; the obsolete example placeholder is removed |
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
| Malformed 6 ЦК question | `что такое 6ЦК и ак она рассчитывается` was interpreted as definition plus calculation; the answer contained three calculation components, two cited documents and eight rendered KaTeX nodes without raw `S_sub`/`\\mathrm` text |
| Multi-source 1 ЦК answer | `Как рассчитывается 1 ЦК?` returned the 1 ЦК formula and parameters from section 5.1 plus the one-rate transmission rule from `Методика расчета стоимости на передачу в.1.docx`; `Sources` contained both clickable original documents and their human section locators |
| Positive document list | `В каких документах фигурирует 1ЦК?` returned only the directly matching methodology and did not mention `r12.docx` or any other negative candidate |
| Formula DOM | the final browser answer contained `28` KaTeX nodes, `0` `.katex-error` elements, no raw legacy `S_sub(` notation and no visible raw `\\mathrm`; source download completed from the `Sources` panel |
| Context budget | the representative formula answer used about `1,847` context tokens and `1,970` total estimated tokens instead of injecting the complete document |
| Document deletion | both live DELETE requests returned `202`; both worker jobs completed at `100%`; both source files are absent; BM25 has `0` matching entries and a full Qdrant scroll over `13,229` points found `0` remnants |
| Cleanup and preservation | all three QA accounts (`QA c67 A`, `QA c67 B`, `QA Intent 45d`) were deactivated and every live QA session was revoked; active QA documents are `0`. Three real active users, four owned projects, four owned chats, six shared documents and two legitimate private documents were preserved |
| User documentation | the 11-page DOCX and Markdown guide describe registration, project/global scope, chat/project uploads, OCR limits, queues, sources, formulas and troubleshooting; every rendered page passed visual inspection |

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
only registration and eventually corporate OIDC/MFA. The three disabled QA user
rows and their tombstoned document registry rows remain for auditability; hard
deletion requires separate explicit authorization. Coverage should next be
raised in provider failures, local-auth database error paths, worker failures,
`rag_agent.py` and the legacy web adapter. Qdrant client/server minor versions
should be aligned at the next maintenance window. Image ingestion currently
extracts printed text; understanding charts, handwriting and spatial diagrams
requires a separate vision-model pipeline and its own evaluation corpus.
