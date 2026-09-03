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
from . import metno, openmeteo, openweather

log = logging.getLogger(__name__)

# Ordered chain, best first. Each must expose current()/forecast() with
# identical signatures and dict keys.
#
#   openweather  keyed, so the quota belongs to this deployment rather than to
#                whatever else shares the host's egress IP -- which is exactly
#                what took Open-Meteo out in production. Supplies wind gusts
#                and probability of precipitation, which met.no does not.
#   met.no       unkeyed second opinion on a different network path. Kept
#                deliberately: a working failover chain is worth more than any
#                single provider.
#   open-meteo   last, because it is the one that is actually rate-limited
#                here. Still useful locally and if the others fail.
#
# NOT in this chain: IMD. providers/imd.py exposes district_warnings(),
# nowcast(), cyclone_state() and marine() -- it is a warnings and nowcast
# source, not a gridded NWP one, and has no current()/forecast() adapter to
# call. It is consumed separately by tools.answer_warnings(). Slotting it in
# front here would mean writing an NWP adapter against an API that has never
# been exercised against a live key (see "Honest limits" in README.md), so the
# hook is documented rather than faked: add ("imd", imd) at the front once
# imd.py grows current()/forecast() and a key exists to test them with.
_CHAIN = (("openweather", openweather), ("met.no", metno), ("open-meteo", openmeteo))


def chain() -> tuple:
    """Providers that are usable right now.

    A provider exposing available() and returning False is dropped rather than
    tried: an unkeyed OpenWeather call would spend budget to earn a 401.
    """
    return tuple((name, p) for name, p in _CHAIN
                 if not hasattr(p, "available") or p.available())


# Kept for callers/tests that want the configured order regardless of keys.
PROVIDERS = _CHAIN


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
    active = chain()
    if not LAST["provider"]:
        return {"provider": (f"{active[0][0]} (configured primary, not yet called)"
                             if active else "none available"),
                "role": "unknown", "last_success": None, "note": None,
                "chain": [n for n, _ in active]}
    return {"provider": LAST["provider"], "role": LAST["role"],
            "last_success": LAST["at"], "note": LAST["note"],
            "chain": [n for n, _ in active]}


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

    active = chain()
    if not active:
        raise RuntimeError("no NWP provider is available")
    for index, (name, provider) in enumerate(active):
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
