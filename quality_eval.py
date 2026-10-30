"""Heuristic regression checks plus raw evidence for human semantic assessment.

Word coverage and citation counts are not a measure of factual correctness.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
from http.client import RemoteDisconnected
from http.cookiejar import CookieJar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener


REFUSAL_MARKERS = (
    "недостаточно данных",
    "нет подтверждения",
    "не найдено в документах",
    "не могу подтвердить",
)


@dataclass(frozen=True)
class QualityCase:
    """Immutable regression expectations for one question: literal term/source/locator matches, refusal markers and a latency threshold.

    Expected strings are supplied by the dataset author; they do not constitute
    a semantic gold answer or establish the corpus's complete relevant sources."""
    id: str
    question: str
    expected_terms: tuple[str, ...] = ()
    expected_sources: tuple[str, ...] = ()
    expected_locators: tuple[str, ...] = ()
    expected_formula_terms: tuple[str, ...] = ()
    forbidden_terms: tuple[str, ...] = ()
    expect_refusal: bool = False
    max_latency_s: float = 60.0
    retrieval_focus: str = "auto"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class QualityResult:
    """Weighted lexical/link/latency checks on a nominal 0-10 scale, with diagnostic failures; not a factual correctness grade."""
    id: str
    score: float
    latency_s: float
    checks: dict[str, float]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalResult:
    """Candidate-level expected-source/locator coverage and mean reciprocal expected-source positions, plus transport diagnostics.

    These metrics describe the returned context list, not whether the final answer
    uses the sources correctly, prioritizes the main rule or avoids hallucination."""
    source_recall: float
    locator_recall: float
    mean_reciprocal_rank: float
    latency_s: float
    candidate_count: int
    missing_sources: tuple[str, ...]
    missing_locators: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class CaseResult:
    """Raw answer, source records, heuristic checks and a separately measured retrieval result for human review.

    Failure labels may flag incomplete generation independently of the numeric
    score; consumers must not treat that score alone as semantic acceptance."""
    id: str
    score: float
    latency_s: float
    checks: dict[str, float]
    failures: tuple[str, ...]
    retrieval: RetrievalResult
    answer: str = ""
    sources: tuple[dict, ...] = ()
    status: str = ""


def load_cases(path: Path) -> list[QualityCase]:
    """Read UTF-8 JSONL expectations, ignoring blank/comment lines and rejecting empty or unscorable datasets.

    A case requires a question and an expected term/source/locator or refusal;
    formula/forbidden terms alone do not satisfy that validation requirement."""
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
        expected_sources = tuple(
            str(item).strip() for item in payload.get("expected_sources", []) if str(item).strip()
        )
        expected_locators = tuple(
            str(item).strip() for item in payload.get("expected_locators", []) if str(item).strip()
        )
        if not question or (not expected_terms and not expected_sources and not expected_locators and not expect_refusal):
            raise ValueError(
                f"case {case_id!r} requires question and an expected term, source, locator, or refusal"
            )
        cases.append(QualityCase(
            id=case_id,
            question=question,
            expected_terms=expected_terms,
            expected_sources=expected_sources,
            expected_locators=expected_locators,
            expected_formula_terms=tuple(
                str(item).strip() for item in payload.get("expected_formula_terms", []) if str(item).strip()
            ),
            forbidden_terms=tuple(str(item).strip() for item in payload.get("forbidden_terms", []) if str(item).strip()),
            expect_refusal=expect_refusal,
            max_latency_s=float(payload.get("max_latency_s", 60.0)),
            retrieval_focus=str(payload.get("retrieval_focus", "auto")).strip().lower() or "auto",
            tags=tuple(str(item).strip() for item in payload.get("tags", []) if str(item).strip()),
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
    """Score case-insensitive substring coverage, source links, formula strings, refusal markers and elapsed time.

    Weights are 4/2/1/1/1/1; any forbidden substring subtracts two points once.
    An unspecified expectation receives full coverage credit. A truthy URL is not
    fetched or validated, and matching formula/answer text is not a proof of facts."""
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


def _context_locator(item: dict[str, Any]) -> str:
    return " > ".join(
        part for part in (
            str(item.get("section_path", "")).strip(),
            str(item.get("section_title", "")).strip(),
        )
        if part
    )


def score_retrieval(case: QualityCase, response: dict[str, Any], latency_s: float) -> RetrievalResult:
    """Measure expected name/locator substring coverage in returned context records.

    Ranks are one-based context-row positions, including repeated document rows.
    The rank metric averages the first reciprocal position of each expected
    source, rather than standard single-first-relevant MRR across queries. Missing
    expectations score 1.0 by convention; this is not answer correctness or a
    measurement of all relevant documents outside the supplied expectations."""
    context = [item for item in response.get("context", []) if isinstance(item, dict)]
    source_names = [str(item.get("file", "")) for item in context]
    locators = [_context_locator(item) for item in context]

    missing_sources = tuple(
        expected for expected in case.expected_sources
        if not any(expected.casefold() in source.casefold() for source in source_names)
    )
    missing_locators = tuple(
        expected for expected in case.expected_locators
        if not any(expected.casefold() in locator.casefold() for locator in locators)
    )
    source_recall = (
        1.0 - len(missing_sources) / len(case.expected_sources)
        if case.expected_sources else 1.0
    )
    locator_recall = (
        1.0 - len(missing_locators) / len(case.expected_locators)
        if case.expected_locators else 1.0
    )
    reciprocal_ranks: list[float] = []
    for expected in case.expected_sources:
        rank = next(
            (index for index, source in enumerate(source_names, 1) if expected.casefold() in source.casefold()),
            None,
        )
        reciprocal_ranks.append(1.0 / rank if rank else 0.0)
    mean_reciprocal_rank = (
        sum(reciprocal_ranks) / len(reciprocal_ranks)
        if reciprocal_ranks else 1.0
    )
    return RetrievalResult(
        source_recall=round(source_recall, 4),
        locator_recall=round(locator_recall, 4),
        mean_reciprocal_rank=round(mean_reciprocal_rank, 4),
        latency_s=round(latency_s, 3),
        candidate_count=len(context),
        missing_sources=missing_sources,
        missing_locators=missing_locators,
    )


class ApiClient:
    """Small eval-only API client supporting bearer or local session auth."""

    def __init__(self, base_url: str, token: str = "", *, insecure: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.csrf_token = ""
        handlers: list[Any] = [HTTPCookieProcessor(CookieJar())]
        if insecure:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            handlers.append(HTTPSHandler(context=context))
        self.opener = build_opener(*handlers)

    def request(
        self,
        path: str,
        method: str = "GET",
        payload: dict | None = None,
        *,
        transport_retries: int = 0,
    ) -> dict:
        """Send one JSON API operation with optional bearer/session CSRF and a 180-second attempt timeout.

        Only selected transport failures receive explicitly requested exponential
        retries; HTTP errors fail immediately. Callers decide whether replaying an
        operation is appropriate. Returned JSON is decoded without schema validation."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.csrf_token and method in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["X-CSRF-Token"] = self.csrf_token
        for attempt in range(transport_retries + 1):
            request = Request(self.base_url + path, data=body, headers=headers, method=method)
            try:
                with self.opener.open(request, timeout=180) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
            except (RemoteDisconnected, TimeoutError, ConnectionError, URLError) as exc:
                if attempt >= transport_retries:
                    raise RuntimeError(f"{method} {path} transport failure: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"{method} {path} transport retry loop exhausted")

    def login(self, email: str, password: str) -> None:
        """Establish a local cookie session and retain its CSRF token, rejecting incomplete authentication responses."""
        response = self.request(
            "/api/auth/login",
            "POST",
            {"email": email, "password": password},
        )
        self.csrf_token = str(response.get("csrf_token", "")).strip()
        if not response.get("authenticated") or not self.csrf_token:
            raise RuntimeError("local authentication did not return a valid session and CSRF token")


def run_case(client: ApiClient, chat_id: str, case: QualityCase) -> CaseResult:
    """Probe retrieval separately, persist the user turn, request an answer and poll its message until completion or deadline.

    This mutates the supplied chat. The debug candidate list is a separate probe,
    not an attested snapshot of the answer's actual evidence. Generation latency
    starts after user-message persistence; incomplete/error/empty answers add a
    failure label without replacing the independently computed heuristic score."""
    retrieval = run_retrieval_case(client, chat_id, case)
    client.request(
        f"/api/chats/{chat_id}/messages",
        "POST",
        {"role": "user", "content": case.question},
    )
    started = time.perf_counter()
    response = client.request(
        "/api/ask",
        "POST",
        {
            "question": case.question,
            "chat_id": chat_id,
            "retrieval_focus": case.retrieval_focus,
        },
    )
    message_id = str(response.get("message_id", ""))
    deadline = time.monotonic() + max(case.max_latency_s * 2, 30)
    while response.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(1)
        messages = client.request(f"/api/chats/{chat_id}/messages").get("items", [])
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
    answer = score_response(case, response, time.perf_counter() - started)
    failures = list(answer.failures)
    if response.get("status") in {"running", "error"} or not str(response.get("answer", "")).strip():
        failures.append("incomplete_generation")
    if retrieval.error:
        failures.append("retrieval_error")
    return CaseResult(
        id=answer.id,
        score=answer.score,
        latency_s=answer.latency_s,
        checks=answer.checks,
        failures=tuple(failures),
        retrieval=retrieval,
        answer=str(response.get("answer", "")),
        sources=tuple(response.get("sources", [])),
        status=str(response.get("status", "complete")),
    )


def run_retrieval_case(client: ApiClient, chat_id: str, case: QualityCase) -> RetrievalResult:
    """Run the debug retrieval probe with up to two transport retries and score the returned candidates.

    Runtime transport/API failures become explicit errors and zero expected-item
    coverage; metrics without corresponding expectations retain their 1.0 default.
    This probe does not generate or semantically assess an answer."""
    retrieval_started = time.perf_counter()
    try:
        retrieval_response = client.request(
            "/api/debug",
            "POST",
            {
                "question": case.question,
                "chat_id": chat_id,
                "retrieval_focus": case.retrieval_focus,
            },
            transport_retries=2,
        )
        retrieval = score_retrieval(
            case,
            retrieval_response,
            time.perf_counter() - retrieval_started,
        )
    except RuntimeError as exc:
        retrieval = RetrievalResult(
            source_recall=0.0 if case.expected_sources else 1.0,
            locator_recall=0.0 if case.expected_locators else 1.0,
            mean_reciprocal_rank=0.0 if case.expected_sources else 1.0,
            latency_s=round(time.perf_counter() - retrieval_started, 3),
            candidate_count=0,
            missing_sources=case.expected_sources,
            missing_locators=case.expected_locators,
            error=str(exc),
        )
    return retrieval


def retrieval_summary(results: list[RetrievalResult]) -> dict[str, float]:
    """Macro-average per-case candidate coverage and rank metrics, rounded to four decimals.

    Cases with no relevant expectations still contribute their default 1.0. An
    empty input also returns 1.0 by convention and must not be read as tested success."""
    if not results:
        return {"source_recall": 1.0, "locator_recall": 1.0, "mean_reciprocal_rank": 1.0}
    return {
        "source_recall": round(sum(item.source_recall for item in results) / len(results), 4),
        "locator_recall": round(sum(item.locator_recall for item in results) / len(results), 4),
        "mean_reciprocal_rank": round(
            sum(item.mean_reciprocal_rank for item in results) / len(results), 4
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """Run selected JSONL cases against an existing chat and emit a heuristic regression report with process exit status.

    Full runs mutate the chat; retrieval-only runs skip answer generation. Passing
    uses numeric score/recall thresholds and retrieval error state, not every
    failure label or semantic review. The report explicitly requires human
    semantic assessment; its score is not an objective answer-quality grade."""
    parser = argparse.ArgumentParser(description="Run the deployed RAG quality gate")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--base-url", default=os.getenv("RAG_EVAL_BASE_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--chat-id", default=os.getenv("RAG_EVAL_CHAT_ID", ""))
    parser.add_argument("--minimum", type=float, default=9.0)
    parser.add_argument("--minimum-retrieval-recall", type=float, default=1.0)
    parser.add_argument("--retrieval-only", action="store_true")
    parser.add_argument("--case-id-pattern", default="")
    parser.add_argument("--insecure", action="store_true", default=os.getenv("RAG_EVAL_INSECURE") == "1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.chat_id:
        parser.error("--chat-id or RAG_EVAL_CHAT_ID is required")
    token = os.getenv("RAG_EVAL_TOKEN", "")
    email = os.getenv("RAG_EVAL_EMAIL", "")
    password = os.getenv("RAG_EVAL_PASSWORD", "")
    client = ApiClient(args.base_url, token, insecure=args.insecure)
    if email or password:
        if not email or not password:
            parser.error("RAG_EVAL_EMAIL and RAG_EVAL_PASSWORD must be set together")
        client.login(email, password)
    cases = load_cases(args.dataset)
    if args.case_id_pattern:
        pattern = re.compile(args.case_id_pattern)
        cases = [case for case in cases if pattern.search(case.id)]
        if not cases:
            parser.error("--case-id-pattern did not select any cases")
    if args.retrieval_only:
        cases = [case for case in cases if case.expected_sources or case.expected_locators]
        retrieval_results = [run_retrieval_case(client, args.chat_id, case) for case in cases]
        summary = retrieval_summary(retrieval_results)
        report = {
            "mode": "retrieval-only",
            "minimum_retrieval_recall": args.minimum_retrieval_recall,
            "passed": all(
                item.source_recall >= args.minimum_retrieval_recall
                and item.locator_recall >= args.minimum_retrieval_recall
                and not item.error
                for item in retrieval_results
            ),
            "retrieval": summary,
            "results": [
                {"id": case.id, "retrieval": asdict(result)}
                for case, result in zip(cases, retrieval_results)
            ],
        }
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        return 0 if report["passed"] else 1
    results = [run_case(client, args.chat_id, case) for case in cases]
    summary = retrieval_summary([result.retrieval for result in results])
    report = {
        "score_kind": "heuristic_word_and_link_checks_not_semantic_quality",
        "semantic_review_required": True,
        "minimum": args.minimum,
        "minimum_retrieval_recall": args.minimum_retrieval_recall,
        "passed": all(
            result.score >= args.minimum
            and result.retrieval.source_recall >= args.minimum_retrieval_recall
            and result.retrieval.locator_recall >= args.minimum_retrieval_recall
            and not result.retrieval.error
            for result in results
        ),
        "average": round(sum(result.score for result in results) / len(results), 2),
        "retrieval": summary,
        "results": [asdict(result) for result in results],
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
