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

The Internet-facing TLS proxy requires `gateway_htpasswd`. Generate a strong,
unique password independently from SSH/sudo credentials and store only its
Apache-compatible hash in that Docker secret. Keep the plaintext recovery copy,
if one is operationally required, in a root-only host file outside the release.
For Compose file-backed secrets, keep the host secret directory mode `0700`,
the plaintext recovery file `0600`, and the salted htpasswd hash `0644` so the
unprivileged nginx worker can read its direct bind mount.
The public `/nginx-health` probe is intentionally exempt; every UI and API path
is protected. Corporate OIDC should replace this single shared gateway identity
when per-user RBAC and audit attribution are required.
