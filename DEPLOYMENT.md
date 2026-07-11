# Docker Runtime

The default stack starts PostgreSQL, Qdrant, the Python backend, and the React frontend:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
```

Endpoints:

| Service | URL |
| --- | --- |
| Frontend | `http://127.0.0.1:5173` |
| Swagger UI | `http://127.0.0.1:8080/docs` |
| OpenAPI | `http://127.0.0.1:8080/api/openapi.json` |
| Backend API | `http://127.0.0.1:8080/api/...` |
| Qdrant dashboard | `http://127.0.0.1:6333/dashboard` |

The backend waits for healthy PostgreSQL and Qdrant, then applies `migrations/*.sql` and creates the default workspace. Runtime state is kept in named Docker volumes. Inspect service logs with `docker compose logs -f backend`.

## Ollama

By default the containers use an Ollama instance on the host through `host.docker.internal:11434`. Set `OLLAMA_BASE_URL` in `.env` if it is elsewhere.

To run Ollama in Compose instead, set `OLLAMA_BASE_URL=http://ollama:11434` in `.env` and start the optional profile:

```powershell
docker compose --profile ollama up --build -d
docker compose exec ollama ollama pull qwen2.5:14b
docker compose exec ollama ollama pull nomic-embed-text
```

## Rust CLI

The backend image already includes the Rust CLI for `rust` and `hybrid` flows. To open the CLI manually, use its optional profile:

```powershell
docker compose --profile cli run --rm rust-cli
```

Files from the repository are mounted read-only at `/workspace`; for example, run `:load /workspace/r12.docx` from the CLI.

## Stop And Reset

```powershell
docker compose down
docker compose down -v  # deletes PostgreSQL, Qdrant, cache, and CLI data
```
