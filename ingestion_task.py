"""Cancellable subprocess boundary for one ingestion job."""
from __future__ import annotations

import json
import os
import sys


def main() -> int:
    """Ingest one CLI-supplied path and emit the worker's final JSON result or serialized failure."""
    if len(sys.argv) != 2:
        print(json.dumps({"error": "path argument is required"})); return 2
    try:
        from rag_agent import RAGAgent
        agent = RAGAgent(embedding_base_url=os.getenv('RAG_INGESTION_EMBED_URL') or None)
        chunks = agent.ingest(sys.argv[1])
        report = getattr(agent, 'last_ingestion_report', {})
        print(json.dumps({"chunks": chunks, **(report if isinstance(report, dict) else {})}, ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False)); return 1


if __name__ == "__main__": raise SystemExit(main())
