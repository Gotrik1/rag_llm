import unittest
from pathlib import Path

from quality_eval import load_cases


DATASET = Path(__file__).with_name("evals") / "production_golden.jsonl"


class ProductionGoldenDatasetTests(unittest.TestCase):
    def test_dataset_has_reviewable_production_scale_and_unique_cases(self) -> None:
        cases = load_cases(DATASET)
        self.assertGreaterEqual(len(cases), 100)
        self.assertLessEqual(len(cases), 150)
        self.assertEqual(len({case.id for case in cases}), len(cases))
        self.assertEqual(len({case.question.casefold() for case in cases}), len(cases))

    def test_dataset_covers_required_failure_modes(self) -> None:
        cases = load_cases(DATASET)
        tags = {tag for case in cases for tag in case.tags}
        self.assertTrue({
            "multi-document",
            "formula",
            "typo-tolerance",
            "semantic-precision",
            "system-behavior",
            "refusal",
            "rre",
            "orem",
        }.issubset(tags))
        self.assertTrue(any(len(case.expected_sources) >= 3 for case in cases))
        self.assertTrue(any(case.expected_locators for case in cases))
        self.assertTrue(any(case.expect_refusal for case in cases))


if __name__ == "__main__":
    unittest.main()
