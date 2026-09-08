"""Deterministic end-to-end quality gate for a deployed RAG corpus."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


REFUSAL_MARKERS = (
    "недостаточно данных",
    "нет подтверждения",
    "не найдено в документах",
    "не могу подтвердить",
)


@dataclass(frozen=True)
class QualityCase:
    id: str
    question: str
    expected_terms: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    expected_formula_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    expect_refusal: bool = False
    max_latency_s: float = 60.0


@dataclass(frozen=True)
class QualityResult:
    id: str
    score: float
    latency_s: float
    checks: dict[str, float]
    failures: tuple[str, ...]


def load_cases(path: Path) -> list[QualityCase]:
    cases: list[QualityCase] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"invalid JSON on line {line_number}: {exc}") from exc
        question = str(payload.get("question", "")).strip()
        case_id = str(payload.get("id", f"line-{line_number}")).strip()
        expect_refusal = bool(payload.get("expect_refusal", False))
        expected_terms = tuple(str(item).strip() for item in payload.get("expected_terms", []) if str(item).strip())
        if not question or (not expected_terms and not expect_refusal):
            raise ValueError(f"case {case_id!r} requires question and expected_terms or expect_refusal")
        cases.append(QualityCase(
            id=case_id,
            question=question,
            expected_terms=expected_terms,
            expected_sources=tuple(str(item).strip() for item in payload.get("expected_sources", []) if str(item).strip()),
            expected_formula_terms=tuple(
                str(item).strip() for item in payload.get("expected_formula_terms", []) if str(item).strip()
            ),
            forbidden_terms=tuple(str(item).strip() for item in payload.get("forbidden_terms", []) if str(item).strip()),
            expect_refusal=expect_refusal,
            max_latency_s=float(payload.get("max_latency_s", 60.0)),
        ))
    if not cases:
        raise ValueError("quality dataset is empty")
    return cases


def _coverage(expected: tuple[str, ...], values: list[str]) -> float:
    if not expected:
        return 1.0
    haystack = "\n".join(values).casefold()
    return sum(term.casefold() in haystack for term in expected) / len(expected)


def score_response(case: QualityCase, response: dict[str, Any], latency_s: float) -> QualityResult:
    answer = str(response.get("answer", ""))
    sources = [item for item in response.get("sources", []) if isinstance(item, dict)]
    formulas = [item for item in response.get("formulas", []) if isinstance(item, dict)]
    source_names = [str(item.get("file", "")) for item in sources]
    formula_texts = [str(item.get("text", "")) for item in formulas]

    checks = {
        "facts": 4.0 * _coverage(case.expected_terms, [answer]),
        "sources": 2.0 * _coverage(case.expected_sources, source_names),
        "links": 1.0 if not case.expected_sources or all(
            any(expected.casefold() in str(source.get("file", "")).casefold() and source.get("url")
                for source in sources)
            for expected in case.expected_sources
        ) else 0.0,
        "formulas": 1.0 * _coverage(case.expected_formula_terms, formula_texts + [answer]),
        "refusal": 1.0 if (
            (case.expect_refusal and any(marker in answer.casefold() for marker in REFUSAL_MARKERS) and not sources)
            or (not case.expect_refusal and not any(marker in answer.casefold() for marker in REFUSAL_MARKERS))
        ) else 0.0,
        "latency": 1.0 if latency_s <= case.max_latency_s else 0.0,
    }
    failures: list[str] = [name for name, score in checks.items() if score == 0]
    forbidden = [term for term in case.forbidden_terms if term.casefold() in answer.casefold()]
    score = sum(checks.values())
    if forbidden:
        score = max(0.0, score - 2.0)
        failures.append("forbidden:" + ",".join(forbidden))
    return QualityResult(case.id, round(score, 2), round(latency_s, 3), checks, tuple(failures))


def _request_json(base_url: str, path: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc


def run_case(base_url: str, token: str, chat_id: str, case: QualityCase) -> QualityResult:
    _request_json(
        base_url,
        f"/api/chats/{chat_id}/messages",
        token,
        "POST",
        {"role": "user", "content": case.question},
    )
    started = time.perf_counter()
    response = _request_json(base_url, "/api/ask", token, "POST", {"question": case.question, "chat_id": chat_id})
    message_id = str(response.get("message_id", ""))
    deadline = time.monotonic() + max(case.max_latency_s * 2, 30)
    while response.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(1)
        messages = _request_json(base_url, f"/api/chats/{chat_id}/messages", token).get("items", [])
        record = next((item for item in messages if str(item.get("id")) == message_id), None)
        if not record or record.get("status") == "running":
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        response = {
            "answer": record.get("content", ""),
            "sources": metadata.get("sources", []),
            "formulas": metadata.get("formulas", []),
            "status": record.get("status"),
        }
    return score_response(case, response, time.perf_counter() - started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deployed RAG quality gate")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--base-url", default=os.getenv("RAG_EVAL_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--chat-id", default=os.getenv("RAG_EVAL_CHAT_ID", ""))
    parser.add_argument("--minimum", type=float, default=9.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.chat_id:
        parser.error("--chat-id or RAG_EVAL_CHAT_ID is required")
    token = os.getenv("RAG_EVAL_TOKEN", "")
    cases = load_cases(args.dataset)
    results = [run_case(args.base_url, token, args.chat_id, case) for case in cases]
    report = {
        "minimum": args.minimum,
        "passed": all(result.score >= args.minimum for result in results),
        "average": round(sum(result.score for result in results) / len(results), 2),
        "results": [asdict(result) for result in results],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
