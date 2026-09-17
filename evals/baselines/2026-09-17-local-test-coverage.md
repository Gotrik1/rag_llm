# Local automated test and coverage snapshot

- Date: 2026-09-17
- Python: 3.12 virtual environment
- Command: `python -m pytest -q`
- Result: **215 passed, 10 skipped, 0 failed**
- Compose validation: `docker compose config --quiet` passed
- Python compilation: all changed runtime and evaluation modules passed `py_compile`

Coverage was measured for the RAG modules with the complete Python test suite:

| Module | Line/branch coverage |
| --- | ---: |
| `document_catalog.py` | 90.8% |
| `reranker.py` | 89.4% |
| `rag_agent.py` | 63.1% |
| `quality_eval.py` | 52.0% |
| `web_ui.py` | 46.4% |
| `db_store.py` | 16.8% |
| **Selected modules total** | **54.9%** |

The low `db_store.py` result is expected in this local snapshot: PostgreSQL
integration tests are skipped while the local Docker engine is unavailable.
Production migration and persistence behaviour still require deployment-time
verification against the real PostgreSQL service; unit tests cover version
normalisation, scope isolation, out-of-order chains, and retrieval preference.
