# Canonical workspace

The single source of truth for this RAG service is:

`C:\Users\gotri\Documents\LLM`

It is the checkout used by the running `rag-assistant` Docker Compose stack.
Use branch `dev` for code, tests, documentation, and runtime operations. The
only long-lived branches are `dev`, `test`, `prod`, and `main`. Do not use
Codex worktrees or safety copies as a second active checkout.

Local legacy-development endpoints (only when `docker-compose.yml` is chosen explicitly):

- frontend: `http://127.0.0.1:5173`
- backend: `http://127.0.0.1:8080`
- Qdrant: `http://127.0.0.1:6333`
- Ollama: `http://127.0.0.1:11434`

The authoritative production file is `compose.yaml`, project
`rag-assistant`: loopback HTTP proxy `:8080`, external TLS proxy `:8443`, and
loopback Grafana `:3000`. Databases, Qdrant, API workers, and vLLM are internal.

Before editing, inspect `git status --short`. Runtime files live only under
`.data/`; code resolves them through `RAG_DATA_DIR` (locally `.data`, in the
backend container `/app/.data`).

The legacy `docker-compose.yml` bind-mounts `./.data` and must be named
explicitly when used. Production `compose.yaml` bind-mounts
`${RAG_DATA_ROOT}/app` at `/app/.data`, applies migrations through a one-shot
service, and starts a separate single-writer ARQ ingestion worker. One Redis
instance uses AOF/noeviction for durable queue transport and a separate key
namespace/TTL for disposable exact answer-cache entries.

Because both Compose files exist, never run an implicit `docker compose ...`
command in automation. Always pass `-f compose.yaml` for production or
`-f docker-compose.yml` for the legacy local stack.

## GigaChat certificates

The public Russian trust-chain certificates are kept in `certs/` and are
installed into both backend images during Docker build. DER `.cer` files are
converted to PEM `.crt` files and registered with Debian's CA store. The
RSA chain is installed; GOST-only certificates are retained as source files
but skipped because stock OpenSSL cannot use them. The images set
`GIGACHAT_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`, so the
GigaChat OAuth and API requests use the packaged trust store. API keys remain
runtime secrets and are not part of the image or repository.
