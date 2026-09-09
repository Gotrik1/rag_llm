# Production readiness report

Date: 2026-09-09. Branch: `dev`. Production release: `b1049e1dd`.

The application is publicly available without VPN at
`https://5.35.120.112:8443/`. The TLS gateway requires Basic Auth once per
browser session. Anonymous access is rejected. The gateway consumes the Basic
credentials and removes both authentication headers before forwarding requests
to the application, so navigation and API calls do not cause recurring browser
login dialogs.

## Delivered capabilities

| Area | Production behavior | Evidence |
| --- | --- | --- |
| Durable sessions | chats and messages are stored in PostgreSQL; an in-progress answer survives refresh, tab closure and API replacement | restart/restore contracts and live VM acceptance |
| Concurrent chats | generation is bound to its original `chat_id`; switching chats does not move or overwrite the result | two simultaneous live production questions plus UI state tests |
| Queues | uploads use a single-writer ingestion queue; answers use a separate concurrent generation queue | ARQ routing/worker tests; all three production queues drained to zero |
| Cache | Redis keys include project/chat scope, corpus revision and flow; corpus or flow changes invalidate stale results | cache and invalidation contracts |
| Bounded RAG context | the system prompt does not contain the whole document; retrieval, reranking and neighbor expansion select a bounded evidence set | PRISM and prompt-injection tests; live token telemetry |
| Project isolation | retrieval excludes documents outside the active project/chat scope | cross-project integration contracts |
| Document memory | UI/API show file name, upload time, status, chunk count, detected version and freshness status | live inventory query and UI/API tests |
| Version awareness | versions are compared only when metadata is supported; upload time is never presented as document edition; unknown freshness is stated explicitly | inventory/version tests and live production answer |
| Sources | answers cite the original document with a clickable download link and a human locator such as section, paragraph or page; chunk UUIDs, scores and previews are hidden | source-contract tests and live answers |
| Formulas | Markdown/KaTeX renders validated LaTeX formulas; formula payload contains equations rather than prose | formula contracts and live `ИВ0` answer |
| Grounded answers | deterministic inventory and evidence-based extractive paths reduce unnecessary LLM use; out-of-scope questions are refused | live acceptance on `r5.docx`, `r7.docx`, `r12.docx` and refusal case |
| Prompt-injection resistance | document text is treated as evidence, not as executable user/system instructions | policy and injection contracts |
| UX | compact chat list, visible project document registry, upload progress synchronized with the authoritative document status, restored running states, and a technical panel that restores the matching Evidence, Sources and Debug question after refresh/chat switching | 19 UI tests, production build and live persisted-metadata acceptance |

## Automated verification

- Backend: `112 passed`, `7 skipped`, one upstream Starlette/httpx deprecation
  warning; duration 7.56 s.
- Branch-aware Python coverage: `49.2%` overall across 4,166 statements and
  1,460 branches. High-risk focused modules include persistence `84.6%`,
  generation worker `80.3%`, document ingestion `77.9%`, response cache `69.5%`,
  ASGI routes `63.8%`, deterministic DOCX parser `62.2%`, and core RAG agent
  `42.8%`. Coverage is reported honestly; live acceptance and integration tests
  do not replace missing unit branches.
- UI Vitest: `19 passed` (`7` API and `12` application tests).
- UI production build: successful, `2522` modules.
- Focused source/formula/PRISM regression: `28 passed`.
- PostgreSQL 18 integration gate from the hardening pass: `86 passed`,
  `2 skipped`, with connection warnings promoted to errors.

The seven skipped backend tests require optional live infrastructure or a live
model and are covered separately by production acceptance. Docker Desktop on
the workstation was unavailable, so the final runtime gate was performed on the
target VM rather than claimed from a local Compose launch.

## Live production acceptance

| Check | Result |
| --- | --- |
| Release | `/opt/rag_llm/current` resolves to `/opt/rag_llm/releases/b1049e1dd`; rollback is `/opt/rag_llm/releases/456dc3194` |
| Artifact | 29,084,618 bytes; SHA-256 `B6F78EE2472104FFBFAD600CCC1A9369E4C1F802CA7E24FF7D2700A8BE2CF674` |
| Runtime | 19 services running; application, proxy, LLM bridge, Qdrant, Redis, PostgreSQL and TLS proxy healthy |
| Health | internal `/healthz` returns `{"status":"ok"}` and readiness returns `{"status":"ready"}` |
| Public access | authenticated external workspace request returns `200`; anonymous request returns `401`; no VPN is required |
| Repeated authentication | five sequential authenticated UI/API requests, including repeated `/api/me`, returned `200`; no downstream Basic challenge was emitted |
| Queue drain | default, ingestion and generation queues are `0 / 0 / 0` after acceptance |
| Document registry | four ready documents are visible: `r5.docx` (196 chunks), `r7.docx` (466), `r12.docx` (1,242), `production_acceptance.md` (4) |
| Inventory answer | returns all four documents, clickable links, timestamps, processing status and explicit `version unknown / freshness not confirmed`; uses no retrieval or LLM tokens |
| Procedural answer | `r7.docx` question returned the required sections, event scope and calculation algorithm with one clean human-readable source locator |
| Definition/formula | `r12.docx` question returned a concise definition of `ИВ0`, one valid displayed equation, variable definitions and the locator `Виды инициатив отклонений > Внешняя инициатива ИВ0-1` |
| Context budget | live `ИВ0` answer used about 2,895 context tokens and about 3,118 total tokens, rather than injecting the complete document |
| Parallelism | simultaneous `ИВ0` and `r7` requests completed in their original chats without cross-chat result leakage |
| Source contract | returned sources are original documents with stable content URLs and useful locators; storage UUIDs and internal chunk labels are absent |
| Upload status consistency | the right-side upload notice derives from the current document registry instead of retaining the initial queue response; pending, mixed, ready and failed states are distinguished |
| Technical panel restore | active production chat exposes one persisted source, one formula, usage and LLM metadata; deployed asset `assets/index-DzH4VeGm.js` restores those values and the matching question after page refresh |

The representative production acceptance cases score at least 9/10 under the
project rubric. This is not a universal guarantee for every future document:
quality depends on parsing, corpus completeness and model behavior. A
version-controlled corpus-specific evaluation set remains the release gate for
new document classes and model/retrieval changes.

## Deployment and rollback

Deployment was atomic and retained existing PostgreSQL, Redis, Qdrant and vLLM
data. The tracked release archive was verified before extraction. Runtime data,
generated artifacts and secrets were excluded. A failed candidate is stopped
before the `current` symlink is switched; the previous release remains directly
available for rollback. Final operational documentation is stored separately
under `/opt/rag_llm/shared/docs` so it survives release rotation.

## Remaining enterprise hardening

The current Basic gateway is appropriate for restricted single-account use.
Corporate OIDC is still required for named users, individual RBAC and
user-attributed audit logs. Configure issuer, audience, JWKS URL and bootstrap
subject before claiming enterprise multi-user identity. Also keep the Qdrant
client/server minor versions aligned at the next maintenance window and expand
branch coverage in `rag_agent.py`, `web_ui.py`, `db_store.py` and provider error
paths.
