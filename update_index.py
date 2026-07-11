"""Incrementally update the RAG index from a local knowledge-base directory.

The script is intentionally independent from cron/Task Scheduler. Both can call
the same command safely because the manifest makes unchanged files idempotent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SOURCE = Path("knowledge_base")
DEFAULT_STATE = Path(".ingestion_cache/index_manifest.json")
DEFAULT_LOG = Path("logs/index_updates.jsonl")
SUPPORTED_SUFFIXES = {".md", ".txt", ".docx", ".pdf"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Не удалось прочитать {path}: {exc}") from exc


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def discover(source: Path) -> dict[str, tuple[Path, str]]:
    if not source.exists():
        raise FileNotFoundError(f"Источник не найден: {source}")
    result: dict[str, tuple[Path, str]] = {}
    for path in sorted(source.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            relative = path.relative_to(source).as_posix()
            result[relative] = (path, fingerprint(path))
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--dry-run", action="store_true", help="показать изменения без запуска RAGAgent")
    parser.add_argument("--force", action="store_true", help="переиндексировать все найденные файлы")
    parser.add_argument("--model", default=None, help="модель Ollama для RAGAgent")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.perf_counter()
    started_at = utc_now()
    try:
        files = discover(args.source)
        previous = read_json(args.state, {}).get("files", {})
        changed = [
            (relative, path, digest)
            for relative, (path, digest) in files.items()
            if args.force or previous.get(relative, {}).get("sha256") != digest
        ]
        deleted = sorted(set(previous) - set(files))

        record = {
            "started_at": started_at,
            "source": str(args.source),
            "dry_run": args.dry_run,
            "files_seen": len(files),
            "files_added_or_changed": len(changed),
            "files_deleted_from_source": len(deleted),
            "files": [relative for relative, _, _ in changed],
            "deleted": deleted,
            "chunks_added": 0,
            "errors": [],
        }

        if not args.dry_run and changed:
            from rag_agent import RAGAgent

            agent = RAGAgent(llm_model=args.model) if args.model else RAGAgent()
            for relative, path, digest in changed:
                try:
                    chunks = agent.ingest(str(path))
                    record["chunks_added"] += chunks
                    previous[relative] = {
                        "sha256": digest,
                        "indexed_at": utc_now(),
                        "chunks": chunks,
                    }
                except Exception as exc:  # keep processing independent files
                    logging.exception("Indexing failed for %s", path)
                    record["errors"].append({"file": relative, "error": f"{type(exc).__name__}: {exc}"})
        else:
            for relative, _, digest in changed:
                previous[relative] = {
                    "sha256": digest,
                    "planned_at": utc_now(),
                }

        record["finished_at"] = utc_now()
        record["duration_s"] = round(time.perf_counter() - started, 3)
        record["status"] = "error" if record["errors"] else "ok"

        if not args.dry_run:
            write_json(args.state, {"version": 1, "updated_at": record["finished_at"], "files": previous})
        append_jsonl(args.log, record)
        print(json.dumps(record, ensure_ascii=False, indent=2))
        return 1 if record["errors"] else 0
    except Exception as exc:
        record = {
            "started_at": started_at,
            "finished_at": utc_now(),
            "duration_s": round(time.perf_counter() - started, 3),
            "status": "error",
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
        }
        append_jsonl(args.log, record)
        print(f"update_index: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

