# Deploy directory

Files here support the single production topology in `../compose.yaml`.

| Path | Purpose |
| --- | --- |
| `entrypoint.sh` | reads mounted secrets and starts API, migrations, or ARQ worker |
| `nginx/` | UI/API reverse proxy and optional TLS edge |
| `secrets/*.example` | non-secret shape templates; copy without `.example` and replace placeholders |
| `postgres/` | audit-retention job |
| `otel-collector/` | trace and span-metric pipeline |
| `prometheus/` | scrape configuration and alert rules |
| `grafana/` | provisioned data sources, dashboard, and alerts |
| `loki/`, `tempo/`, `alloy/` | logs and traces |
| `llm-bridge/` | Ollama-compatible bridge to vLLM and FastEmbed |

There are no Compose profiles in the authoritative file. The production model
path is `vllm` → `llm-bridge`; old instructions mentioning `ollama`,
`response-cache`, `job-queue`, `frontend`, or `backend` services are obsolete.

For complete configuration, backup, acceptance, and rollback steps, read
[`../DEPLOYMENT.md`](../DEPLOYMENT.md). Never commit generated secret files,
private keys, certificates, uploaded documents, or runtime volumes.
