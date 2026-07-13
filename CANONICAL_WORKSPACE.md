# Canonical workspace

The single source of truth for this RAG service is:

`C:\Users\gotri\Documents\LLM`

It is the checkout used by the running `rag-assistant` Docker Compose stack.
Use its current branch (`dev`) for code, tests, documentation, and runtime
operations. Do not use `.codex\worktrees\e7c6\LLM` or other isolated worktrees
as a second active copy.

Current runtime endpoints:

- frontend: `http://127.0.0.1:5173`
- backend: `http://127.0.0.1:8080`
- Qdrant: `http://127.0.0.1:6333`
- Ollama: `http://127.0.0.1:11434`

Before editing, inspect `git status --short`. The checkout currently contains
staged/uncommitted work from prior tasks; preserve it and coordinate before
merging another branch.

## Safety snapshot

The staged state present before the RBAC integration assessment is preserved
locally and on GitHub in `backup/dev-pre-rbac-integration-20260712`, commit
`95ce5a5096f72c39602ca7eb3962836dfed874f4`. Do not delete this branch until
the RBAC/async/telemetry integration has been completed and accepted.

## RBAC integration preview

The in-progress integration branch is `codex/rbac-integration`. Its local
preview is deliberately isolated from the current stack:

- preview UI: `http://127.0.0.1:5174`
- preview API: `http://127.0.0.1:8081`
- preview PostgreSQL: `127.0.0.1:5434`
- preview Redis: `127.0.0.1:6380`

The stable UI remains on ports `5173/8080`. The preview uses the same UI
contract through an ASGI compatibility facade, while adding SSO-ready RBAC,
ABAC policies, audit events, persistent jobs, SSE and telemetry.

## GigaChat certificates

The public Russian trust-chain certificates are kept in `certs/` and are
installed into both backend images during Docker build. DER `.cer` files are
converted to PEM `.crt` files and registered with Debian's CA store. The
RSA chain is installed; GOST-only certificates are retained as source files
but skipped because stock OpenSSL cannot use them. The images set
`GIGACHAT_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`, so the
GigaChat OAuth and API requests use the packaged trust store. API keys remain
runtime secrets and are not part of the image or repository.
