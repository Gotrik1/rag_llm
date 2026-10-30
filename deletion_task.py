"""Isolated document-index cleanup executed by the single ingestion worker."""
from __future__ import annotations

import json
import sys


def main() -> int:
    """Remove one document from the agent's indexes and emit a JSON worker result; file removal belongs to the worker."""
    if len(sys.argv) != 2:
        print(json.dumps({"error": "path argument is required"}))
        return 2
    try:
        from web_ui import get_agent

        chunks = get_agent().delete_source(sys.argv[1])
        print(json.dumps({"chunks_deleted": chunks}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
