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
