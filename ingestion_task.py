"""Cancellable subprocess boundary for one ingestion job."""
from __future__ import annotations

import json
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(json.dumps({"error": "path argument is required"})); return 2
    try:
        from web_ui import get_agent
        chunks = get_agent().ingest(sys.argv[1])
        print(json.dumps({"chunks": chunks}, ensure_ascii=False)); return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False)); return 1


if __name__ == "__main__": raise SystemExit(main())
