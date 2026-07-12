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
