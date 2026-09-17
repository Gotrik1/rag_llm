"""Compare remote rerankers on the same deployed retrieval candidate lists."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from quality_eval import ApiClient, load_cases, retrieval_summary, score_retrieval
from reranker import HttpReranker, RerankerSettings


@dataclass(frozen=True)
class RerankerTarget:
    name: str
    url: str
    model: str


def parse_target(value: str) -> RerankerTarget:
    parts = value.split("|", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise argparse.ArgumentTypeError("expected NAME|URL|MODEL")
    return RerankerTarget(*(part.strip() for part in parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare cross-encoder endpoints on frozen per-question candidates"
    )
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--base-url", default=os.getenv("RAG_EVAL_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--chat-id", default=os.getenv("RAG_EVAL_CHAT_ID", ""))
    parser.add_argument("--reranker", action="append", required=True, type=parse_target)
    parser.add_argument("--case-id-pattern", default="")
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--model-weight", type=float, default=0.75)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--insecure", action="store_true", default=os.getenv("RAG_EVAL_INSECURE") == "1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.chat_id:
        parser.error("--chat-id or RAG_EVAL_CHAT_ID is required")

    client = ApiClient(args.base_url, os.getenv("RAG_EVAL_TOKEN", ""), insecure=args.insecure)
    email = os.getenv("RAG_EVAL_EMAIL", "")
    password = os.getenv("RAG_EVAL_PASSWORD", "")
    if email or password:
        if not email or not password:
            parser.error("RAG_EVAL_EMAIL and RAG_EVAL_PASSWORD must be set together")
        client.login(email, password)

    cases = [case for case in load_cases(args.dataset) if case.expected_sources or case.expected_locators]
    if args.case_id_pattern:
        selected = re.compile(args.case_id_pattern)
        cases = [case for case in cases if selected.search(case.id)]
    if not cases:
        parser.error("no retrieval cases selected")

    targets = {
        target.name: HttpReranker(RerankerSettings(
            mode="http",
            url=target.url,
            model=target.model,
            timeout_seconds=args.timeout,
            candidate_limit=args.candidate_limit,
            model_weight=args.model_weight,
        ))
        for target in args.reranker
    }
    scored = {"baseline": []} | {name: [] for name in targets}
    latencies = {name: [] for name in targets}
    failures = {name: [] for name in targets}
    rows: list[dict] = []

    for case in cases:
        started = time.perf_counter()
        response = client.request(
            "/api/debug",
            "POST",
            {
                "question": case.question,
                "chat_id": args.chat_id,
                "retrieval_focus": case.retrieval_focus,
            },
        )
        context = [item for item in response.get("context", []) if isinstance(item, dict)]
        baseline = score_retrieval(case, {"context": context}, time.perf_counter() - started)
        scored["baseline"].append(baseline)
        row = {"id": case.id, "baseline": asdict(baseline), "rerankers": {}}
        for name, reranker in targets.items():
            rerank_started = time.perf_counter()
            reranked = reranker.rerank(case.question, context)
            elapsed = time.perf_counter() - rerank_started
            latencies[name].append(elapsed)
            failed_open = len(context) >= 2 and reranked is context
            if failed_open:
                failures[name].append(case.id)
            result = score_retrieval(case, {"context": reranked}, elapsed)
            scored[name].append(result)
            row["rerankers"][name] = {**asdict(result), "failed_open": failed_open}
        rows.append(row)

    baseline_mrr = retrieval_summary(scored["baseline"])["mean_reciprocal_rank"]
    summaries = {"baseline": retrieval_summary(scored["baseline"])}
    for name in targets:
        summary = retrieval_summary(scored[name])
        summary["mrr_delta"] = round(summary["mean_reciprocal_rank"] - baseline_mrr, 4)
        summary["latency_p50_s"] = round(statistics.median(latencies[name]), 4)
        summary["latency_p95_s"] = round(
            sorted(latencies[name])[max(0, int(len(latencies[name]) * 0.95) - 1)], 4
        )
        summary["failed_open_cases"] = len(failures[name])
        summaries[name] = summary

    report = {
        "candidate_source": "deployed /api/debug output",
        "case_count": len(cases),
        "summaries": summaries,
        "failures": failures,
        "results": rows,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 1 if any(failures.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
