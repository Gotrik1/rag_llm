"""Run a golden question set against the current RAG agent and write JSONL logs."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_QUESTIONS = Path("golden_questions.txt")
DEFAULT_LOG = Path("logs/evaluation.jsonl")
REFUSAL_MARKERS = (
    "не знаю",
    "нет информации",
    "не найден",
    "недостаточно данных",
    "не могу ответить",
    "отсутствует в контексте",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_questions(path: Path) -> list[dict]:
    questions: list[dict] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|", 3)
        if len(parts) != 4:
            raise ValueError(f"{path}:{line_number}: ожидается expected|question|keywords|id")
        expected, question, keywords, case_id = (part.strip() for part in parts)
        if expected not in {"known", "unknown"}:
            raise ValueError(f"{path}:{line_number}: expected должен быть known или unknown")
        questions.append({
            "id": case_id or f"case-{line_number}",
            "expected": expected,
            "question": question,
            "keywords": [item.strip().lower() for item in keywords.split(",") if item.strip()],
        })
    return questions


def answer_is_refusal(answer: str) -> bool:
    normalized = re.sub(r"\s+", " ", answer.lower())
    return any(marker in normalized for marker in REFUSAL_MARKERS)


def assess(item: dict, answer: str, sources: list[dict]) -> tuple[str, str]:
    has_sources = bool(sources)
    has_keywords = not item["keywords"] or any(keyword in answer.lower() for keyword in item["keywords"])
    refusal = answer_is_refusal(answer)
    if item["expected"] == "known":
        return ("pass" if has_sources and len(answer.strip()) >= 20 and has_keywords else "fail",
                "sources_and_answer" if has_sources and len(answer.strip()) >= 20 and has_keywords else "missing_evidence_or_keyword")
    return ("pass" if refusal or not has_sources else "fail",
            "refusal_or_empty_context" if refusal or not has_sources else "answered_without_refusal")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    parser.add_argument("--output", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--model", default=None)
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = parse_questions(args.questions)
    if args.limit > 0:
        cases = cases[: args.limit]
    if not cases:
        raise SystemExit("Golden set пуст")

    from rag_agent import RAGAgent

    agent = RAGAgent(llm_model=args.model) if args.model else RAGAgent()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    passed = 0
    with args.output.open("a", encoding="utf-8") as stream:
        for item in cases:
            started = time.perf_counter()
            error = None
            answer = ""
            sources: list[dict] = []
            try:
                answer, sources, from_cache = agent.ask(item["question"])
            except Exception as exc:
                from_cache = False
                error = f"{type(exc).__name__}: {exc}"
            status, reason = assess(item, answer, sources) if error is None else ("error", "exception")
            passed += status == "pass"
            record = {
                "timestamp": utc_now(),
                "case_id": item["id"],
                "question": item["question"],
                "expected": item["expected"],
                "status": status,
                "reason": reason,
                "answer_length": len(answer),
                "answer": answer,
                "sources_found": bool(sources),
                "sources": sources,
                "from_cache": from_cache,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "error": error,
            }
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"[{status}] {item['id']}: {item['question']}")

    print(f"Итог: {passed}/{len(cases)} успешно; лог: {args.output}")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())

