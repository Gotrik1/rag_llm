"""Capture isolated production evidence and original sources for reproducible QA."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quality_eval import ApiClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--answers", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    client = ApiClient("https://5.35.120.112:8443", insecure=True)
    client.login(os.environ["RAG_EVAL_EMAIL"], os.environ["RAG_EVAL_PASSWORD"])
    documents = client.request("/api/documents").get("items", [])
    (args.output / "catalog.json").write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
    for document in documents:
        name = document["file_name"]
        if not any(term in name.casefold() for term in ("1179", "рук", "ruk", "методика")):
            continue
        target = args.output / name
        if not target.exists():
            with client.opener.open(client.base_url + f"/api/documents/{document['id']}/content", timeout=60) as response:
                target.write_bytes(response.read())
        print("source", name, target.stat().st_size, flush=True)
    questions = [
        "поиск графика", "какие бывают модели?", "ХОП", "работа с графиками",
        "Что ведется в договорных условиях?", "Как рассчитывается 2 ЦК по постановлениям?",
        "Чем отличаются расчеты по 3 ЦК и по 6 ЦК?", "Как рассчитывается цена на электроэнергию по 6 ЦК?",
    ]
    if args.download_only:
        return
    records = []
    for question in questions:
        response = client.request("/api/debug", "POST", {"question": question})
        record = {"question": question, "retrieval": response}
        if args.answers:
            record["generation"] = client.request("/api/ask", "POST", {"question": question})
        records.append(record)
        (args.output / "baseline.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
        context = response.get("context", [])
        print(question, [(item.get("file"), item.get("section_path")) for item in context[:3]], flush=True)


if __name__ == "__main__":
    main()
