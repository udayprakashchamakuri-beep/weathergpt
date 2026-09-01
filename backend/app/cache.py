"""Two-tier cache.

Tier 1 -- exact response cache keyed on (normalised query, place, lang, persona).
Tier 2 -- upstream payload cache keyed on the provider URL.

In production both tiers live in Redis with the TTL pinned to the *data's*
validity (a nowcast valid 3h is cached 3h, a 7-day forecast until the next
model run), not to a fixed wall-clock number. That is what keeps p95 latency
under ~300 ms for the ~80 % of traffic that is "what's the weather here".
"""
from __future__ import annotations

import hashlib
import time
from typing import Any


class TTLCache:
    def __init__(self, ttl: int = 600, maxsize: int = 4096):
        self.ttl = ttl
        self.maxsize = maxsize
        self._store: dict[str, tuple[float, Any]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(*parts: Any) -> str:
        raw = "|".join(str(p) for p in parts)
        return hashlib.sha1(raw.encode()).hexdigest()

    def get(self, key: str) -> Any | None:
        row = self._store.get(key)
        if not row:
            self.misses += 1
            return None
        expiry, value = row
        if expiry < time.time():
            self._store.pop(key, None)
            self.misses += 1
            return None
        self.hits += 1
        return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if len(self._store) >= self.maxsize:
            # cheap eviction: drop the soonest-to-expire quarter
            doomed = sorted(self._store.items(), key=lambda kv: kv[1][0])
            for k, _ in doomed[: self.maxsize // 4]:
                self._store.pop(k, None)
        self._store[key] = (time.time() + (ttl or self.ttl), value)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "entries": len(self._store),
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / total, 3) if total else 0.0,
        }


response_cache = TTLCache(ttl=300)
upstream_cache = TTLCache(ttl=600)
