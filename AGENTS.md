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

## MCP knowledge graph

The latest verified server MCP project is `rag-assistant-canonical-20261029-handoff-v17`
(verified on 2026-10-29 against `build_answer_sources` and
`QuestionService.answer`). The older MCP record `rag-assistant` is stale; the
server denied its deletion, so it remains available but must not be treated as
the current source graph.

For later sessions, read `.codebase-memory/artifact.json` when present and
verify its project before using it: the local artifact last retained an older
`C-Users-gotri-Documents-LLM` graph without the new service helpers. Prefer the
verified server project above when the artifact fails that check. After
indexing, verify a current function signature or newly
added helper against the working tree. A matching Git HEAD or an `indexed`
status alone does not prove that uncommitted changes were indexed.
