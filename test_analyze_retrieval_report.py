import json
import tempfile
import unittest
from pathlib import Path

from evals.analyze_retrieval_report import analyze, load_dataset, render_markdown


class RetrievalReportAnalysisTests(unittest.TestCase):
    def test_groups_failures_by_tag_and_counts_missing_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "golden.jsonl"
            dataset_path.write_text(
                json.dumps({
                    "id": "case-1",
                    "question": "Question",
                    "tags": ["multi-document", "rre"],
                }) + "\n",
                encoding="utf-8",
            )
            report = {
                "retrieval": {
                    "source_recall": 0.5,
                    "locator_recall": 1.0,
                    "mean_reciprocal_rank": 0.5,
                },
                "results": [{
                    "id": "case-1",
                    "retrieval": {
                        "source_recall": 0.5,
                        "locator_recall": 1.0,
                        "mean_reciprocal_rank": 0.5,
                        "latency_s": 2.0,
                        "candidate_count": 10,
                        "missing_sources": ["Document B"],
                        "missing_locators": [],
                        "error": "",
                    },
                }],
            }

            result = analyze(load_dataset(dataset_path), report)

        self.assertEqual(result["tag_summary"]["multi-document"]["source_recall"], 0.5)
        self.assertEqual(result["family_summary"]["case"]["zero_source_cases"], 0)
        self.assertEqual(result["missing_sources"], {"Document B": 1})
        self.assertEqual(result["worst_cases"][0]["question"], "Question")

    def test_markdown_contains_overall_and_worst_case_evidence(self):
        analysis = {
            "case_count": 1,
            "overall": {"source_recall": 0.5, "locator_recall": 1.0, "mean_reciprocal_rank": 0.5},
            "tag_summary": {},
            "family_summary": {
                "case": {
                    "case_count": 1,
                    "source_recall": 0.5,
                    "locator_recall": 1.0,
                    "mean_reciprocal_rank": 0.5,
                    "average_latency_s": 2.0,
                    "zero_source_cases": 0,
                },
            },
            "missing_sources": {"Document B": 1},
            "error_count": 0,
            "unknown_case_ids": [],
            "worst_cases": [{
                "id": "case-1",
                "question": "Question",
                "source_recall": 0.5,
                "locator_recall": 1.0,
                "mean_reciprocal_rank": 0.5,
                "missing_sources": ["Document B"],
                "missing_locators": [],
            }],
        }

        rendered = render_markdown(analysis)

        self.assertIn("Source recall: 0.5000", rendered)
        self.assertIn("Document B: 1", rendered)
        self.assertIn("`case-1`", rendered)
        self.assertIn("| case | 1 | 0.5000", rendered)


if __name__ == "__main__":
    unittest.main()
