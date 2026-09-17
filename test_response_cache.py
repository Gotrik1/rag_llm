"""Unit tests for the exact Redis response-cache contract."""
import unittest

from response_cache import CACHE_NAMESPACE, ResponseCache


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.ttl = {}

    def get(self, key):
        return self.data.get(key)

    def setex(self, key, ttl, value):
        self.data[key] = value
        self.ttl[key] = ttl

    def scan_iter(self, match, count):
        prefix = match.removesuffix("*")
        # Redis SCAN is cursor-based and remains usable while keys are
        # deleted.  Snapshot the in-memory fake so tests model that contract.
        yield from (key for key in tuple(self.data) if key.startswith(prefix))

    def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)
            self.ttl.pop(key, None)


class ResponseCacheTests(unittest.TestCase):
    def test_round_trip_uses_namespace_and_ttl(self):
        client = FakeRedis()
        cache = ResponseCache(ttl_seconds=120, client=client)
        cache.set("abc", {"answer": "готово", "sources": []})

        self.assertEqual(cache.get("abc"), {"answer": "готово", "sources": []})
        self.assertEqual(client.ttl[f"{CACHE_NAMESPACE}abc"], 120)

    def test_clear_does_not_touch_other_redis_data(self):
        client = FakeRedis()
        cache = ResponseCache(client=client)
        cache.set("abc", {"answer": "готово"})
        client.data["other:key"] = "keep"

        cache.clear()

        self.assertIsNone(cache.get("abc"))
        self.assertEqual(client.data["other:key"], "keep")

    def test_clear_deletes_in_bounded_scan_batches(self):
        class TrackingRedis(FakeRedis):
            def __init__(self):
                super().__init__()
                self.delete_batch_sizes = []

            def delete(self, *keys):
                self.delete_batch_sizes.append(len(keys))
                super().delete(*keys)

        client = TrackingRedis()
        cache = ResponseCache(client=client)
        for index in range(401):
            cache.set(f"entry-{index}", {"answer": "готово"})

        cache.clear()

        self.assertEqual(client.data, {})
        self.assertEqual(client.delete_batch_sizes, [200, 200, 1])

    def test_payload_does_not_require_a_question_or_prompt(self):
        client = FakeRedis()
        cache = ResponseCache(client=client)
        cache.set("hash-only", {"answer": "готово", "sources": [{"file": "manual.md", "section": "1", "score": 0.9}]})

        payload = next(iter(client.data.values()))
        self.assertNotIn("question", payload)
        self.assertNotIn("prompt", payload)


if __name__ == "__main__":
    unittest.main()
