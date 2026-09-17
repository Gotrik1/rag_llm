import argparse
import unittest

from evals.compare_rerankers import parse_target


class CompareRerankersTests(unittest.TestCase):
    def test_parse_target_keeps_model_slashes(self):
        target = parse_target("bge|http://localhost:11434/api/rerank|BAAI/bge-reranker-base")
        self.assertEqual(target.name, "bge")
        self.assertEqual(target.model, "BAAI/bge-reranker-base")

    def test_parse_target_requires_three_parts(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_target("bge|http://localhost")


if __name__ == "__main__":
    unittest.main()
