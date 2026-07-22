"""
services/semantic_cache.py
---------------------------
In-process LRU cache for embedding vectors with TTL expiry.

Avoids re-embedding the same search query strings against the Gemini API,
which costs tokens and adds 200-500 ms of latency per repeated query.

Cache key
  SHA-256 hash of the normalised query string (lowercased, whitespace-collapsed).
  This makes the key model-agnostic — if the embedding model changes, clear_all().

TTL
  Defaults to 30 minutes.  Embeddings are stable within a session; a 30-minute
  TTL ensures stale entries don't persist across roster updates.

Thread/async safety
  Python's GIL ensures dict operations are atomic enough for single-process use.
  This is not safe for multi-process deployments — use Redis or Cosmos in that case.

Usage::

    from app.services.semantic_cache import embedding_cache

    vec = embedding_cache.get("Java Architect Mexico")
    if vec is None:
        vec = await _compute_embedding("Java Architect Mexico")
        embedding_cache.set("Java Architect Mexico", vec)
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import Optional

_logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 30 * 60   # 30 minutes
_DEFAULT_MAX_SIZE = 512           # maximum number of cached vectors


def _cache_key(text: str) -> str:
    """Return a stable, short cache key for *text*."""
    normalised = re.sub(r"\s+", " ", text.strip().lower())
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


class _CacheEntry:
    __slots__ = ("vector", "expires_at")

    def __init__(self, vector: list[float], ttl: float) -> None:
        self.vector = vector
        self.expires_at = time.monotonic() + ttl


class SemanticCache:
    """LRU cache for embedding vectors with per-entry TTL."""

    def __init__(self, max_size: int = _DEFAULT_MAX_SIZE, ttl: float = _DEFAULT_TTL_SECONDS) -> None:
        self._max_size = max_size
        self._ttl = ttl
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, text: str) -> Optional[list[float]]:
        """Return the cached embedding for *text*, or None on miss/expiry."""
        key = _cache_key(text)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        if time.monotonic() > entry.expires_at:
            del self._store[key]
            self._misses += 1
            return None
        # LRU: move to end on access
        self._store.move_to_end(key)
        self._hits += 1
        return entry.vector

    def set(self, text: str, vector: list[float]) -> None:
        """Store *vector* for *text*, evicting the LRU entry if at capacity."""
        key = _cache_key(text)
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = _CacheEntry(vector, self._ttl)
        if len(self._store) > self._max_size:
            evicted = self._store.popitem(last=False)
            _logger.debug("[SemanticCache] Evicted LRU entry: %s", evicted[0])

    def clear_all(self) -> None:
        """Evict all entries (call after embedding model change or roster refresh)."""
        self._store.clear()
        _logger.info("[SemanticCache] Cache cleared.")

    def stats(self) -> dict:
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / max(self._hits + self._misses, 1),
            "ttl_seconds": self._ttl,
        }


# Singleton — imported by hybrid_retriever and bench_search
embedding_cache = SemanticCache()
