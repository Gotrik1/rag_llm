# Production deployment and rollback

This runbook targets `compose.yaml`, project name `rag-assistant`. Run every
command from the canonical checkout on branch `dev`. Do not use the legacy
`docker-compose.yml` for production.

## 1. Host prerequisites

- Linux x86-64 host with Docker Engine + Compose v2 and NVIDIA Container Toolkit.
- GPU capacity for `Qwen/Qwen3.8-27B` at the configured context/concurrency.
- Persistent filesystem with separate monitored capacity for PostgreSQL,
  Qdrant, uploaded documents/BM25, model cache, Prometheus, Loki, and Tempo.
- DNS/TLS certificate for `RAG_SERVER_NAME`.
- Reachable OIDC issuer/JWKS and correct audience/claims.
- Outbound registry/Hugging Face access for the first image/model pull, or an
  approved offline image/model transfer.

Do not deploy until the exact VM hostname, SSH principal, install directory,
public URL, OIDC values, TLS files, and rollback owner are known.

## 2. Prepare configuration without secrets in Git

```console
cp .env.example .env
cp deploy/secrets/postgres_password.example deploy/secrets/postgres_password
cp deploy/secrets/redis_password.example deploy/secrets/redis_password
cp deploy/secrets/redis_exporter_passwords.json.example deploy/secrets/redis_exporter_passwords.json
cp deploy/secrets/grafana_admin_password.example deploy/secrets/grafana_admin_password
cp deploy/secrets/app_secret_key.example deploy/secrets/app_secret_key
```

Replace every placeholder. `redis_exporter_passwords.json` must contain the
same Redis password as `redis_password`, mapped to `redis://redis:6379`.
Prefer a platform secret manager. If files are used, grant read permission only
to the deployment account. Never paste their contents into logs or reports.

Set real OIDC issuer/audience/JWKS values, one exact trusted CORS origin, server
name, TLS paths, data root, and GPU limits. Production must not use development
authentication.

## 3. Preflight and backup

```console
git status --short --branch
git rev-parse HEAD
docker compose -f compose.yaml --env-file .env config --quiet
docker compose -f compose.yaml --env-file .env config --services
nvidia-smi
df -h
```

Before an upgrade, record the current image IDs/digests and back up:

- PostgreSQL with a consistent `pg_dump -Fc` plus a filesystem snapshot;
- Qdrant using its snapshot API or a stopped consistent volume snapshot;
- `.data/storage/app` (uploads, BM25, state) with checksums;
- `.env`, TLS references, and secret-file metadata without printing values.

Never run `down -v` on a retained environment.

## 4. Build and start

```console
docker compose -f compose.yaml --env-file .env build
docker compose -f compose.yaml --env-file .env up -d
docker compose -f compose.yaml --env-file .env ps
docker compose -f compose.yaml --env-file .env logs --since 15m migrate app ingestion-worker generation-worker proxy tls-proxy
```

`migrate` must finish successfully before `app`, `ingestion-worker`, and
`generation-worker` become
healthy. The first vLLM/model start can take significant time; do not mistake
model download/loading for API readiness.

## 5. Acceptance gates

From the VM:

```console
curl --fail http://127.0.0.1:8080/nginx-health
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
curl --fail --cacert /path/to/ca.pem https://RAG_SERVER_NAME:8443/nginx-health
```

Then verify through the real browser/OIDC path:

1. Create a project and two chats.
2. Upload one small document; observe queued → processing → ready and a nonzero chunk count.
3. Ask a question whose answer is present and verify the section/source download link.
4. Ask a formula question and verify inline/display rendering.
5. Start a slow answer, refresh the page, then restart only `app`; verify the
   generation worker finishes it and the answer, sources, and formulas appear.
6. Start answers in two chats and switch between them; verify no cross-chat message placement.
7. Upload different documents to two projects and verify facts/sources do not cross project boundaries.
8. Repeat an identical question and verify `from_cache=true`; change project memory/document corpus and verify no stale hit.
9. Inspect Grafana `RAG Production Overview`, Prometheus targets, Loki logs, and Tempo traces.
10. Run the versioned corpus gate from `docs/QUALITY_EVALUATION.md`; every case
    must score at least 9/10.

Record response status, latency, source file/section, and the deployed Git SHA.

## 6. Operations

```console
docker compose -f compose.yaml --env-file .env ps
docker compose -f compose.yaml --env-file .env logs --since 15m app ingestion-worker generation-worker vllm llm-bridge
docker compose -f compose.yaml --env-file .env exec app alembic current
docker compose -f compose.yaml --env-file .env restart app
```

Use `/healthz` for process liveness and `/readyz` for dependency admission.
PostgreSQL, Redis, and Qdrant are intentionally not exposed on host ports.
Grafana binds to loopback by default; use a controlled SSH tunnel or enterprise
ingress instead of opening it broadly.

Default retention: audit 365 days, Prometheus 30 days, Loki 30 days, Tempo 7
days. Review disk growth and restore tests before increasing retention.

## 7. Rollback

Application rollback:

1. Stop new uploads or pause the ingestion worker.
2. Preserve logs and the failed image ID.
3. Set `RAG_IMAGE_TAG` to the last verified image and redeploy `app`, worker, and proxies.
4. Do not reverse an applied database migration blindly. Use a tested forward-fix unless the migration has an explicitly verified downgrade.
5. Re-run all health and acceptance gates.

Data rollback is last resort: restore PostgreSQL, Qdrant, and app-data from one
consistent recovery point. Restoring only one store can leave registry rows,
vectors, and source files inconsistent.

## 8. Known local workstation issue

Docker Desktop 4.71 on the canonical Windows workstation can crash while
initializing Docker Model Runner because stale AF_UNIX runtime sockets are
unreadable. This is unrelated to the RAG code. The safe fix is to stop Docker,
disable Model Runner (not used by this stack) or update Docker Desktop, and
recreate only its runtime socket directories. Never reset Docker to factory
defaults unless loss of all local images/volumes is explicitly accepted.
