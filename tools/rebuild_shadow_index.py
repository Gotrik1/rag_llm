"""Rebuild registered sources into a NEW index; never mutate the live one.

Run with ingestion writers stopped. This creates no registry records and does
not switch production: an operator verifies manifest and changes configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", required=True)
    parser.add_argument("--bm25-dir", type=Path, required=True)
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Resume only a verified incomplete checkpoint; writers must be stopped")
    args = parser.parse_args()
    from db_store import list_documents
    records = list_documents()
    ready = [record for record in records if record.get("status") == "ready"]
    sources = {}
    for record in ready:
        path = Path(record["source_path"])
        if not path.is_file():
            raise FileNotFoundError(f"Registered ready source missing: {path}")
        sources[str(path)] = {
            "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "document_ids": sources.get(str(path), {}).get("document_ids", []) + [str(record["id"])],
        }
    if not sources:
        raise RuntimeError("No registered ready sources")
    print(json.dumps({"records": len(records), "ready": len(ready), "files": len(sources),
                      "sources": list(sources.values())}, ensure_ascii=False), flush=True)
    if args.inventory_only:
        return
    if args.collection == os.environ.get("QDRANT_COLLECTION"):
        raise RuntimeError("Refusing live collection")
    if args.bm25_dir.exists() and not args.resume:
        raise RuntimeError("Target directory must not exist: no overwrite/resume")
    import qdrant_client
    client = qdrant_client.QdrantClient(url=os.environ["QDRANT_URL"], trust_env=False)
    exists = client.collection_exists(args.collection)
    if exists and not args.resume:
        raise RuntimeError("Target collection already exists: no overwrite/resume")
    checkpoint = None
    if args.resume:
        checkpoint = json.loads((args.bm25_dir / 'manifest.json').read_text(encoding='utf-8'))
        if not exists or checkpoint.get('complete') or checkpoint.get('collection') != args.collection or checkpoint.get('bm25_dir') != str(args.bm25_dir):
            raise RuntimeError('Not a matching incomplete checkpoint')
        for saved in checkpoint['sources']:
            source = sources.get(saved['path'])
            if not source or source['sha256'] != saved['sha256'] or sorted(source['document_ids']) != sorted(saved['document_ids']):
                raise RuntimeError('Checkpoint source changed or lost registration')
        expected = sum(source['chunks'] for source in checkpoint['sources'])
        if client.count(args.collection, exact=True).count != expected:
            raise RuntimeError('Vector checkpoint has partial writes; refusing duplicate ingestion')
    os.environ["QDRANT_COLLECTION"] = args.collection
    os.environ["BM25_PERSIST_DIR"] = str(args.bm25_dir)
    from rag_agent import RAGAgent
    agent = RAGAgent()
    # A shadow build must not invalidate live user response caches per source.
    agent.clear_cache = lambda: None
    manifest = checkpoint or {"collection": args.collection, "bm25_dir": str(args.bm25_dir),
                              "sources": [], "complete": False}
    if checkpoint and len(agent.docstore.docs) != expected:
        raise RuntimeError('Docstore and vector checkpoints disagree')
    completed = {source['path'] for source in manifest['sources']}
    manifest_path = args.bm25_dir / "manifest.json"
    for index, source in enumerate(sources.values(), 1):
        if source['path'] in completed:
            print(f'CHECKPOINT {index}/{len(sources)} already complete', flush=True)
            continue
        print(f"SOURCE {index}/{len(sources)} {source['path']}", flush=True)
        source["chunks"] = agent.ingest(source["path"])
        manifest["sources"].append(source)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    # Detect an unexpected active writer or deleted/replaced registry source.
    current = sorted((str(r["id"]), r["source_path"]) for r in list_documents() if r.get("status") == "ready")
    initial = sorted((str(r["id"]), r["source_path"]) for r in ready)
    if current != initial:
        raise RuntimeError("Registry changed during rebuild; do not switch this index")
    for source in sources.values():
        if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
            raise RuntimeError("Source changed during rebuild")
    manifest["complete"] = True
    manifest["total_chunks"] = len(agent.docstore.docs)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("SHADOW_COMPLETE", manifest["total_chunks"], flush=True)


if __name__ == "__main__":
    main()
