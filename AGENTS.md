# Canonical workspace policy

This repository checkout is the canonical working copy for the RAG service:

`C:\Users\gotri\Documents\LLM`

All future code changes, tests, documentation, Docker Compose operations, and
runtime checks for this project must be performed in this checkout on the
`dev` branch. Do not edit or run the service from Codex-managed worktrees.

The active local stack is the Compose project `rag-assistant` from this
directory: backend `:8080`, frontend `:5173`, Qdrant `:6333`, and PostgreSQL
published on `:5433`. Before changing source, inspect `git status` and preserve
all existing staged or uncommitted user changes. Do not reset, clean, or delete
those changes without explicit approval.

Codex-managed worktrees are temporary task checkouts; their changes are not
canonical until intentionally ported and reviewed here.
