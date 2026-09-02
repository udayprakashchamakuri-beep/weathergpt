"""NWP facade: primary provider with immediate failover to a second source.

Call sites ask this module for current conditions and forecasts and do not
care which upstream answered. The Provenance record attached to the data says
who did, so the answer stays attributable and the source chip in the UI
changes visibly on failover.

Two rules encoded here, both learned from the deployed instance:

1. **429 is not transient, so it is never retried.** Open-Meteo rate-limits by
   source IP and a free managed host shares one egress IP across tenants; the
   quota was exhausted by somebody else and will still be exhausted a second
   later. Retrying a 429 buys nothing and costs the user the whole retry
   ladder. The primary's own retry loop is therefore skipped for 429 and we
   move straight to the fallback.

2. **A request has a total upstream budget.** Before this, one 429'd call cost
   about 25 s (three attempts x 8 s plus backoff). Under a handful of
   concurrent users those requests pile up and starve the single free
   instance, which then fails health checks and returns errors on every
   route, including /api/health. Capping the budget means a bad upstream
   degrades one answer instead of taking the service down.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from ..config import get_settings
from . import metno, openmeteo

log = logging.getLogger(__name__)

# Ordered: first entry is primary. Each must expose current()/forecast() with
# identical signatures and dict keys.
PROVIDERS = (("open-meteo", openmeteo), ("met.no", metno))


# Which provider actually served the most recent NWP call, so /api/health can
# report reality instead of the configured primary. A deployment silently
# running on its fallback is exactly the thing an operator needs to see.
LAST: dict = {"provider": None, "role": None, "at": None, "note": None}


def _record(name: str, index: int, note: str | None = None) -> None:
    LAST.update(provider=name,
                role="primary" if index == 0 else "fallback",
                at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                note=note)


def status() -> dict:
    """Snapshot for /api/health."""
    if not LAST["provider"]:
        return {"provider": f"{PROVIDERS[0][0]} (configured primary, not yet called)",
                "role": "unknown", "last_success": None, "note": None}
    return {"provider": LAST["provider"], "role": LAST["role"],
            "last_success": LAST["at"], "note": LAST["note"]}


def is_rate_limited(exc: BaseException) -> bool:
    return (isinstance(exc, httpx.HTTPStatusError)
            and exc.response is not None
            and exc.response.status_code == 429)


async def _failover(method: str, *args, **kwargs) -> dict:
    """Try each provider in order within one shared time budget."""
    budget = get_settings().upstream_budget_s
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    errors: list[str] = []

    for index, (name, provider) in enumerate(PROVIDERS):
        remaining = deadline - loop.time()
        if remaining <= 0:
            errors.append(f"{name}: skipped, request budget of {budget}s spent")
            break
        try:
            data = await asyncio.wait_for(
                getattr(provider, method)(*args, **kwargs), timeout=remaining)
            _record(name, index, "; ".join(errors) if errors else None)
            if errors:
                log.warning("NWP failover: %s served %s after %s",
                            name, method, "; ".join(errors))
            return data
        except asyncio.TimeoutError:
            errors.append(f"{name}: exceeded the remaining request budget")
        except Exception as exc:                       # noqa: BLE001
            why = "rate-limited (429)" if is_rate_limited(exc) else repr(exc)[:120]
            errors.append(f"{name}: {why}")

    # Every source failed. Raise rather than invent a value -- the caller
    # turns this into an honest degraded answer naming the failure.
    raise RuntimeError("all NWP providers failed -> " + "; ".join(errors))


async def current(lat: float, lon: float) -> dict:
    return await _failover("current", lat, lon)


async def forecast(lat: float, lon: float, days: int = 7) -> dict:
    return await _failover("forecast", lat, lon, days=days)
