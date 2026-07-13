# Canonical workspace

The single source of truth for this RAG service is:

`C:\Users\gotri\Documents\LLM`

It is the checkout used by the running `rag-assistant` Docker Compose stack.
Use branch `dev` for code, tests, documentation, and runtime operations. The
only long-lived branches are `dev`, `test`, `prod`, and `main`. Do not use
Codex worktrees or safety copies as a second active checkout.

Current runtime endpoints:

- frontend: `http://127.0.0.1:5173`
- backend: `http://127.0.0.1:8080`
- Qdrant: `http://127.0.0.1:6333`
- Ollama: `http://127.0.0.1:11434`

Before editing, inspect `git status --short`. Runtime files live only under
`.data/`; code resolves them through `RAG_DATA_DIR` (locally `.data`, in the
backend container `/app/.data`).

The local `docker-compose.yml` bind-mounts the committed `./.data` snapshot.
The production `compose.yaml` intentionally uses a separate `app_data` Docker
volume at `/app/.data`; production runtime changes do not modify the checkout.

## Isolated preview

An optional local preview can be started without replacing the main stack:

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
