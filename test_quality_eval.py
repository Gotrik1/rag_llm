import json
import tempfile
import unittest
from pathlib import Path

from quality_eval import QualityCase, load_cases, score_response


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
            with self.assertRaisesRegex(ValueError, "expected_terms or expect_refusal"):
                load_cases(path)


if __name__ == "__main__":
    unittest.main()
