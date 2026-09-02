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

    If DEMO_TOKEN is unset while the demo endpoints are enabled, this REFUSES
    (503) rather than allowing the call. An open-by-default gate is a gate
    that is open precisely when someone forgot to configure it, which is the
    case where it matters most.

    Compared with hmac.compare_digest so a wrong guess cannot be narrowed down
    by timing.
    """
    expected = settings.demo_token
    if not expected:
        # FAIL CLOSED. An unset token used to mean "open", which is fine on a
        # laptop and indefensible on a public URL -- it is exactly how the
        # deployed instance ended up accepting /simulate from the internet.
        # If the demo endpoints are switched on, the operator must supply a
        # token; misconfiguration now costs a 503, not a broadcast channel.
        raise HTTPException(
            503,
            "demo endpoints are enabled but DEMO_TOKEN is not configured, so "
            "this endpoint refuses to serve. Set DEMO_TOKEN, or set "
            "ENABLE_DEMO_ENDPOINTS=false to disable these routes entirely.",
        )
    if not x_demo_token or not hmac.compare_digest(x_demo_token, expected):
        raise HTTPException(
            401,
            "missing or invalid X-Demo-Token. This endpoint is gated on public "
            "deployments because it writes to the dissemination path.",
        )


def websocket_authorized(ws) -> bool:
    """Same gate as require_demo_token, for the /ws/alerts upgrade.

    This exists because an auth check that covers one transport is not an auth
    check. The socket's "scan" action reaches alerts.dispatch() -- the exact
    capability guarded on POST /api/alerts/scan -- so leaving the socket open
    while gating the HTTP route just moves the door, it does not lock it.

    Browsers cannot set custom headers on a WebSocket handshake (the JS
    WebSocket constructor takes a URL and subprotocols, nothing else), so the
    token is accepted from the `token` query parameter as well as from an
    X-Demo-Token header for non-browser clients. The query parameter is the
    pragmatic option, with one real cost: URLs are the part of a request most
    likely to end up in proxy and access logs, so this token should be treated
    as log-exposed. It already is page-source-exposed by design, so this does
    not widen the exposure -- but it is another reason a real channel needs a
    signed, short-lived token instead.

    Unset DEMO_TOKEN refuses the upgrade, matching the HTTP gate: a missing
    token and a wrong token are both rejected.
    """
    expected = settings.demo_token
    if not expected:
        # Fail closed, same as the HTTP gate: no token configured means the
        # socket is refused rather than opened to the world.
        return False
    supplied = ws.query_params.get("token") or ws.headers.get("x-demo-token") or ""
    return bool(supplied) and hmac.compare_digest(supplied, expected)


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


def startup_check() -> list[str]:
    """Shout at startup if the deployment is in an unsafe combination.

    Returns the problems found so the caller can log them; an operator reading
    boot logs should not have to discover this from a defaced demo.
    """
    problems: list[str] = []
    if settings.enable_demo_endpoints and not settings.demo_token:
        problems.append(
            "ENABLE_DEMO_ENDPOINTS is true but DEMO_TOKEN is unset. "
            "/api/alerts/simulate, /subscribe and /scan and the /ws/alerts "
            "upgrade are REFUSING all requests (503) rather than serving them "
            "unauthenticated. Set DEMO_TOKEN in the platform environment.")
    if not settings.metno_contact.strip():
        problems.append(
            "METNO_CONTACT is unset. MET Norway requires a contact address in "
            "the User-Agent and may throttle or block requests without one, "
            "which would take out the NWP fallback exactly when the primary "
            "is rate-limited. Set METNO_CONTACT to an address you monitor.")
    if settings.allowed_origins() == ["*"]:
        problems.append(
            "CORS is wide open (allow_origins=*). Set CORS_ALLOW_ORIGINS, or "
            "deploy on Render where RENDER_EXTERNAL_URL pins it automatically.")
    return problems
