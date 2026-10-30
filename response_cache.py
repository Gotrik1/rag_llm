"""Exact, bounded cache for generated RAG responses.

The cache intentionally stores only a hash key plus the already generated answer
and its source metadata.  It is not a semantic cache: in a regulatory RAG
system, similar wording can still require a different answer.
"""

from __future__ import annotations

import json
import os
from typing import Any

try:
    import redis
except ImportError:  # Allows local code inspection before runtime dependencies are installed.
    redis = None  # type: ignore[assignment]


# An answer is only reusable while both the prompt policy and the retrieved
# global corpus snapshot are current.  A day preserves cross-user reuse for
# recurring questions without retaining a long tail of abandoned exact keys.
DEFAULT_TTL_SECONDS = 24 * 60 * 60
CACHE_NAMESPACE = "rag:answer:v3:"


class ResponseCache:
    """Redis-backed exact cache that fails open when Redis is unavailable."""

    def __init__(
        self,
        url: str | None = None,
        ttl_seconds: int | None = None,
        client: Any | None = None,
    ) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds or os.environ.get("RESPONSE_CACHE_TTL_SECONDS", DEFAULT_TTL_SECONDS)))
        self.client = client
        if self.client is None and redis is not None:
            cache_url = url or os.environ.get("RESPONSE_CACHE_URL", "redis://127.0.0.1:6379/0")
            self.client = redis.Redis.from_url(
                cache_url,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )

    @staticmethod
    def _key(key: str) -> str:
        return f"{CACHE_NAMESPACE}{key}"

    def get(self, key: str) -> dict[str, Any] | None:
        """Read one exact JSON object; Redis failures and malformed entries are cache misses."""
        if self.client is None:
            return None
        try:
            raw = self.client.get(self._key(key))
            if not raw:
                return None
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    def set(self, key: str, value: dict[str, Any]) -> None:
        """Write with TTL; the caller supplies an already scope-safe composite key."""
        if self.client is None:
            return
        try:
            self.client.setex(
                self._key(key),
                self.ttl_seconds,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            )
        except Exception:
            pass

    def clear(self) -> None:
        """Invalidate this namespace in bounded batches without touching unrelated Redis keys."""
        if self.client is None:
            return
        try:
            # Keep invalidation bounded even when the cache has accumulated a
            # large number of expired-soon keys.  Materialising every key at
            # once can itself become the next memory spike.
            batch: list[str] = []
            for key in self.client.scan_iter(match=f"{CACHE_NAMESPACE}*", count=200):
                batch.append(key)
                if len(batch) >= 200:
                    self.client.delete(*batch)
                    batch.clear()
            if batch:
                self.client.delete(*batch)
        except Exception:
            pass
