import io
import json
import unittest
from unittest.mock import patch

from reranker import HttpReranker, RerankerSettings, candidate_text


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class RerankerTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "mode": "http",
            "url": "http://reranker/api/rerank",
            "model": "test-model",
            "timeout_seconds": 1.0,
            "candidate_limit": 3,
            "model_weight": 0.5,
            "text_limit": 100,
        }
        values.update(overrides)
        return RerankerSettings(**values)

    def test_disabled_mode_preserves_input_identity(self):
        ranked = [{"text": "a", "score": 1.0}, {"text": "b", "score": 0.5}]
        self.assertIs(HttpReranker(self.settings(mode="off")).rerank("q", ranked), ranked)

    def test_http_scores_promote_better_passage(self):
        ranked = [{"text": "a", "score": 1.0}, {"text": "b", "score": 0.9}]
        with patch("urllib.request.urlopen", return_value=_Response({"scores": [0.0, 1.0]})) as call:
            result = HttpReranker(self.settings()).rerank("question", ranked)

        self.assertEqual(result[0]["text"], "b")
        self.assertEqual(result[0]["heuristic_score"], 0.9)
        self.assertEqual(result[0]["reranker_score"], 1.0)
        sent = json.loads(call.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(sent["model"], "test-model")

    def test_invalid_response_fails_open(self):
        ranked = [{"text": "a", "score": 1.0}, {"text": "b", "score": 0.9}]
        with patch("urllib.request.urlopen", return_value=_Response({"scores": [1.0]})):
            result = HttpReranker(self.settings()).rerank("question", ranked)
        self.assertIs(result, ranked)

    def test_unbounded_logit_cannot_erase_hard_heuristic_penalty(self):
        ranked = [{"text": "right", "score": 2.0}, {"text": "wrong", "score": -2.0}]
        with patch("urllib.request.urlopen", return_value=_Response({"scores": [-100.0, 100.0]})):
            result = HttpReranker(self.settings(model_weight=0.75)).rerank("question", ranked)
        self.assertEqual(result[0]["text"], "right")
        self.assertLessEqual(result[1]["score"], -1.25)

    def test_only_candidate_limit_is_reranked(self):
        ranked = [
            {"text": "a", "score": 1.0},
            {"text": "b", "score": 0.9},
            {"text": "c", "score": 0.8},
        ]
        with patch("urllib.request.urlopen", return_value=_Response({"scores": [0.0, 1.0]})):
            result = HttpReranker(self.settings(candidate_limit=2)).rerank("question", ranked)
        self.assertEqual(result[-1], ranked[-1])

    def test_candidate_text_contains_locator_and_is_bounded(self):
        text = candidate_text({"section_path": "A > B", "section_title": "B", "text": "x" * 20}, 12)
        self.assertEqual(text, "A > B\nB\nxxxx")


if __name__ == "__main__":
    unittest.main()
