# Production deployment and observability

This directory contains the production-oriented Docker Compose deployment for
the RAG API, ingestion worker, PostgreSQL, Redis, Qdrant, reverse proxy, and the
complete metrics/logs/traces stack.

## Topology

- `proxy` serves the built React UI and proxies `/api`, `/healthz`, and
  `/readyz` to the private ASGI service. SSE proxy buffering is disabled.
- `app` and `ingestion-worker` share PostgreSQL, Redis, Qdrant, ingestion data,
  and model caches. `migrate` must complete before either service starts.
- OpenTelemetry Collector sends traces to Tempo and exposes span-derived
  metrics to Prometheus. Alloy discovers this Compose project's containers and
  ships their JSON logs to Loki. Grafana provisions all three data sources, the
  RAG overview dashboard, and alert rules.
- Databases and telemetry backends are not published to the host. Grafana is
  bound to `127.0.0.1` by default. Only the reverse proxy is publicly bound.

The backend Docker network is internal. Application containers also receive a
separate egress network so OIDC JWKS, corporate LLMs, and embedding providers
remain reachable without publishing the API container itself.

## First deployment

1. Copy `.env.example` to `.env` and replace all example OIDC values.
2. Copy every `deploy/secrets/*.example` file to the same name without the
   `.example` suffix. Replace each value with an independent random secret.
   The directory-level `.gitignore` prevents the resulting files from being
   committed.
3. Verify the rendered configuration, build, and start the stack:

   ```console
   docker compose --env-file .env config --quiet
   docker compose --env-file .env build
   docker compose --env-file .env up -d
   docker compose --env-file .env ps
   ```

Generate secrets with a platform secret manager whenever possible. For a local
Compose host, `openssl rand -base64 48` is sufficient for each password and
`openssl rand -hex 32` for `app_secret_key`. Do not reuse the placeholder values.

The one-shot `migrate` service runs `alembic upgrade head`. A failed migration
keeps the API and worker stopped. Inspect it with:

```console
docker compose logs migrate
```

The public checks are:

- `/healthz`: process liveness only; use for a container restart decision.
- `/readyz`: dependency readiness, including Qdrant; use for load-balancer
  admission and rolling-deployment gates.
- `/nginx-health`: reverse-proxy liveness only.

Prometheus scrapes `/metrics` over the internal network; Nginx deliberately
does not publish that endpoint.

## Local Ollama profile

Ollama is optional because a production deployment may use a managed or
corporate LLM endpoint. To run it locally and pull the configured model:

```console
docker compose --env-file .env --profile local-llm up -d ollama
docker compose --env-file .env --profile local-llm run --rm ollama-pull
docker compose --env-file .env --profile local-llm run --rm ollama-pull-embedding
docker compose --env-file .env up -d app ingestion-worker
```

Set `OLLAMA_BASE_URL` to an externally managed endpoint when the profile is not
used. The `ollama` profile publishes its port on loopback only.

## Ports and isolated test data

The default public port is `8080`; Grafana uses loopback port `3000`. Change
`RAG_HTTP_PORT` or `GRAFANA_PORT` when another stack already owns those ports.
This repository is commonly used alongside a Compose project under
`C:\Users\gotri\Documents\LLM`; stop that project or choose non-conflicting
ports before bringing this one up.

PostgreSQL, Redis, and Qdrant intentionally have no host port mapping. This
prevents integration tests from silently reading an unrelated service on
`localhost:6333`. Run tests against this project's service DNS names from a
Compose test container, or publish explicit non-default test ports in a local
override file. Use a distinct Compose project name and fresh named volumes for
every destructive test run.

## Retention and backup policy

The checked-in defaults are:

| Data | Retention | Enforcement |
| --- | ---: | --- |
| PostgreSQL audit events | 365 days | `audit-retention` deletes expired rows daily |
| Prometheus metrics | 30 days | `--storage.tsdb.retention.time` |
| Loki application/audit logs | 30 days | Loki compactor |
| Tempo traces | 7 days | Tempo compactor |

Change PostgreSQL retention with `RAG_AUDIT_RETENTION_DAYS`, its schedule with
`AUDIT_RETENTION_INTERVAL_SECONDS`, and metrics retention with
`METRICS_RETENTION`. Loki and Tempo retention are explicit in
`deploy/loki/loki.yaml` and `deploy/tempo/tempo.yaml`; review them together with
storage capacity before deployment.

Database audit rows are the authoritative security record. Loki is the
short-lived operational copy and must not be treated as the only audit store.
Back up the PostgreSQL and Qdrant volumes with consistent snapshots, encrypt
backups, test restores quarterly, and apply a legal hold outside the deletion
job when required. Pause `audit-retention` during a legal hold.

## Alerts and dashboards

Grafana is available at the configured loopback address. Its password comes
from the `grafana_admin_password` Compose secret. Provisioned alerts cover API
availability, dependency availability, 5xx ratio, p95 latency, authorization
denial spikes, and missing OpenTelemetry metrics. Configure a Grafana contact
point and notification policy for the target environment after first login;
contact credentials are intentionally not stored in this repository.

The `RAG Production Overview` dashboard links metrics with logs and Tempo trace
IDs. Prometheus also evaluates the same core alert rules so an external
Alertmanager can consume them when the organization already operates one.

## Reverse proxy, CORS, and TLS

`CORS_ALLOW_ORIGIN` accepts one exact trusted browser origin. The proxy returns
CORS headers only when the request origin matches. It also applies request
rate limiting, security headers, upload size limits, long streaming timeouts,
and disables buffering for API responses.

The supplied Nginx listener is HTTP so it can sit behind an enterprise ingress
or load balancer. Terminate TLS there, redirect HTTP to HTTPS, and pass the
original scheme. Do not expose this listener directly on an untrusted network
without TLS. If TLS must terminate in this Compose project, add a local Nginx
override that mounts certificate secrets; do not commit certificates or keys.

## Operations

Useful commands:

```console
docker compose ps
docker compose logs --since 15m app ingestion-worker migrate
docker compose exec app python -m alembic current
docker compose exec postgres pg_dump -U rag -Fc rag > rag.dump
docker compose exec redis redis-cli --no-auth-warning -a "$(cat deploy/secrets/redis_password)" ping
docker compose down
```

Use `docker compose down -v` only for an explicitly disposable environment; it
removes persistent databases, vectors, logs, metrics, traces, and model data.
