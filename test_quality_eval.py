import json
import tempfile
import unittest
from pathlib import Path

from quality_eval import (
    ApiClient,
    QualityCase,
    RetrievalResult,
    load_cases,
    retrieval_summary,
    score_response,
    score_retrieval,
)


class QualityEvaluationTests(unittest.TestCase):
    def test_grounded_answer_with_link_formula_and_latency_scores_ten(self) -> None:
        case = QualityCase(
            id="formula",
            question="Как рассчитать Q?",
            expected_terms=("масс", "теплоёмк"),
            expected_sources=("manual.pdf",),
            expected_formula_terms=("Q", "ΔT"),
            max_latency_s=2,
        )
        result = score_response(case, {
            "answer": "Q зависит от массы и теплоёмкости.",
            "sources": [{"file": "manual.pdf", "url": "/api/documents/1/content"}],
            "formulas": [{"text": "Q = m c ΔT"}],
        }, 1.5)
        self.assertEqual(result.score, 10)
        self.assertEqual(result.failures, ())

    def test_missing_evidence_and_forbidden_claim_fail_gate(self) -> None:
        case = QualityCase(
            id="bad",
            question="Факт?",
            expected_terms=("Кедр",),
            expected_sources=("knowledge.md",),
            forbidden_terms=("Сосна",),
        )
        result = score_response(case, {"answer": "Сосна", "sources": []}, 1)
        self.assertLess(result.score, 9)
        self.assertIn("sources", result.failures)

    def test_dataset_requires_an_assertable_expectation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.jsonl"
            path.write_text(json.dumps({"id": "bad", "question": "Что?"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expected term, source, locator, or refusal"):
                load_cases(path)

    def test_retrieval_scores_source_recall_locator_recall_and_mrr(self) -> None:
        case = QualityCase(
            id="multi-document",
            question="Как определяется первая ценовая категория?",
            expected_sources=("442", "1-6ЦК"),
            expected_locators=("порядок определения стоимости 1 ЦК",),
        )
        result = score_retrieval(case, {
            "context": [
                {"file": "Постановление 442.docx", "section_path": "Пункт 4"},
                {
                    "file": "Методика расчета стоимости 1-6ЦК v2.docx",
                    "section_path": "5.1 Порядок определения стоимости 1 ЦК",
                },
            ],
        }, 0.25)
        self.assertEqual(result.source_recall, 1.0)
        self.assertEqual(result.locator_recall, 1.0)
        self.assertEqual(result.mean_reciprocal_rank, 0.75)
        self.assertEqual(result.candidate_count, 2)

    def test_retrieval_reports_missing_required_document(self) -> None:
        case = QualityCase(
            id="missing",
            question="Где описаны ценовые категории?",
            expected_sources=("442", "1179"),
        )
        result = score_retrieval(case, {
            "context": [{"file": "Постановление 442.docx", "section_path": "Пункт 95"}],
        }, 0.1)
        self.assertEqual(result.source_recall, 0.5)
        self.assertEqual(result.missing_sources, ("1179",))

    def test_dataset_accepts_retrieval_only_case_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "retrieval.jsonl"
            path.write_text(json.dumps({
                "id": "retrieval-only",
                "question": "Где описан расчет?",
                "expected_sources": ["Методика.docx"],
                "expected_locators": ["Раздел 5"],
                "retrieval_focus": "rre",
                "tags": ["multi-document", "rre"],
            }, ensure_ascii=False), encoding="utf-8")
            case = load_cases(path)[0]
            self.assertEqual(case.expected_locators, ("Раздел 5",))
            self.assertEqual(case.retrieval_focus, "rre")
            self.assertEqual(case.tags, ("multi-document", "rre"))

    def test_api_client_accepts_local_session_authentication(self) -> None:
        client = ApiClient("https://example.invalid")
        client.request = lambda *args, **kwargs: {  # type: ignore[method-assign]
            "authenticated": True,
            "csrf_token": "csrf",
        }
        client.login("qa@example.invalid", "not-persisted")
        self.assertEqual(client.csrf_token, "csrf")

    def test_retrieval_summary_aggregates_stage_metrics(self) -> None:
        result = RetrievalResult(0.5, 1.0, 0.25, 0.1, 10, ("missing",), ())
        self.assertEqual(retrieval_summary([result]), {
            "source_recall": 0.5,
            "locator_recall": 1.0,
            "mean_reciprocal_rank": 0.25,
        })


if __name__ == "__main__":
    unittest.main()
