"""Deployment guards: shared-secret gate + in-process rate limiting.

Neither of these exists to make WeatherGPT a multi-tenant service. They exist
because the demo build is about to become reachable from the public internet,
where two things that are harmless on a laptop stop being harmless:

  * /api/alerts/simulate fans out a synthetic RED cyclone warning, and
    /api/alerts/subscribe accepts any address with no verification. Nothing in
    this build actually sends SMS -- delivery is a log write -- so today the
    exposure is defacement and log-flooding rather than a public-safety one.
    It becomes a safety one the moment a real channel is wired in, so the gate
    goes in now, while it is cheap.

  * The upstream NWP provider rate-limits by source IP, and every request from
    the deployment shares ONE IP. A single scraper hitting /api/chat can
    therefore burn the quota for the whole demo. The limiter protects the
    upstream budget, not the CPU.

Deliberately in-process: no Redis, no extra moving part before a demo. This is
sound only because the service runs a single worker (see the Dockerfile) --
the same constraint that makes the alert state work.
"""
from __future__ import annotations

import hmac
import time
from collections import deque

from fastapi import Header, HTTPException, Request

from .config import get_settings

settings = get_settings()


# ------------------------------------------------------------ shared secret
def require_demo_token(x_demo_token: str | None = Header(default=None)) -> None:
    """Gate for the dissemination endpoints.

    If DEMO_TOKEN is unset the gate is OPEN, so a laptop clone with no
    configuration still runs exactly as the README promises. It is set on the
    deployment; that is what closes it.

    Compared with hmac.compare_digest so a wrong guess cannot be narrowed down
    by timing.
    """
    expected = settings.demo_token
    if not expected:
        return
    if not x_demo_token or not hmac.compare_digest(x_demo_token, expected):
        raise HTTPException(
            401,
            "missing or invalid X-Demo-Token. This endpoint is gated on public "
            "deployments because it writes to the dissemination path.",
        )


# ------------------------------------------------------------ rate limiting
class SlidingWindowLimiter:
    """Fixed-capacity sliding window, keyed by client IP.

    A deque of hit timestamps per key; entries older than the window are
    discarded on read. Memory is bounded by pruning idle keys whenever the
    table grows past _MAX_KEYS, which keeps a spray of forged IPs from being
    a memory-exhaustion vector in its own right.
    """

    _MAX_KEYS = 4096

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._hits.items()
                    if not v or now - v[-1] > self.window_s]:
            del self._hits[key]

    def check(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds)."""
        # limit <= 0 disables the guard entirely. This is the escape hatch the
        # smoke suite uses: tests/test_smoke.py drives /api/chat far faster
        # than any human, so a limit tuned for public traffic would fail the
        # build rather than protect anything. Run the suite against a server
        # started with RATE_LIMIT_CHAT_PER_MIN=0.
        if self.limit <= 0:
            return True, 0
        now = time.monotonic()
        if len(self._hits) > self._MAX_KEYS:
            self._prune(now)
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False, max(1, int(self.window_s - (now - q[0])) + 1)
        q.append(now)
        return True, 0


def client_ip(request: Request) -> str:
    """Best-effort client identity.

    Behind the Hugging Face Spaces proxy the socket peer is the proxy, so the
    left-most X-Forwarded-For hop is the real client. That header is
    caller-supplied and therefore spoofable -- which is acceptable here,
    because this limiter is a courtesy guard on a shared upstream quota, not
    an access control. Anything that must not be bypassed goes behind
    require_demo_token instead.
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


chat_limiter = SlidingWindowLimiter(
    settings.rate_limit_chat_per_min, 60.0)
subscribe_limiter = SlidingWindowLimiter(
    settings.rate_limit_subscribe_per_min, 60.0)


def enforce(limiter: SlidingWindowLimiter, request: Request, what: str) -> None:
    allowed, retry_after = limiter.check(client_ip(request))
    if not allowed:
        raise HTTPException(
            429,
            f"rate limit exceeded for {what}: max {limiter.limit} requests per "
            f"minute from one address. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
