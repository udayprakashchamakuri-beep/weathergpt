"""MET Norway fallback NWP provider (Locationforecast 2.0).

Why this exists: Open-Meteo rate-limits by source IP, and a free managed
platform gives every tenant a shared egress IP. On the deployed instance that
IP is already exhausted by other tenants, so api.open-meteo.com returns 429 to
100% of forecast requests -- current conditions, forecast, warnings and
advisory all die while air quality and the ERA5 archive (different hosts) keep
working. Retrying cannot fix a quota that belongs to somebody else, so the
answer is a second source on a different network path.

MET Norway (the Norwegian Meteorological Institute) publishes a global
deterministic forecast from its own model chain. It is a genuine second
opinion, not a mirror of the same GFS fields.

Terms of service (https://api.met.no/doc/TermsOfService):
  * a User-Agent identifying the application AND a contact address is
    mandatory -- requests without one are refused, and a generic one gets the
    whole platform throttled;
  * responses carry Expires and Last-Modified and clients are required to
    honour them rather than polling. Both are implemented below.

This adapter deliberately exposes the same interface and the same dict keys as
openmeteo.py, so tools.py and advisory.py consume it without knowing which
source answered. What DOES change is the Provenance record, which names MET
Norway -- so the source chip in the UI visibly changes and nobody is misled
about where a number came from.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..cache import upstream_cache
from ..config import get_settings
from ..schemas import Provenance

BASE = "https://api.met.no/weatherapi/locationforecast/2.0/complete"

# India Standard Time. Daily aggregation must bucket by local calendar day, or
# "tomorrow maximum" quietly means something else.
IST = timezone(timedelta(hours=5, minutes=30))

# MET Norway symbol codes -> WMO codes, so advisory.py keeps working on the
# `weather_code` field it already reads. Directional suffixes (_day, _night,
# _polartwilight) are stripped before lookup.
SYMBOL_TO_WMO = {
    "clearsky": 0, "fair": 1, "partlycloudy": 2, "cloudy": 3,
    "fog": 45,
    "lightrain": 61, "rain": 63, "heavyrain": 65,
    "lightrainshowers": 80, "rainshowers": 81, "heavyrainshowers": 82,
    "lightdrizzle": 51, "drizzle": 53,
    "lightsleet": 66, "sleet": 66, "heavysleet": 67,
    "lightsleetshowers": 66, "sleetshowers": 66, "heavysleetshowers": 67,
    "lightsnow": 71, "snow": 73, "heavysnow": 75,
    "lightsnowshowers": 85, "snowshowers": 85, "heavysnowshowers": 86,
    "rainandthunder": 95, "heavyrainandthunder": 95,
    "lightrainandthunder": 95, "rainshowersandthunder": 95,
    "heavyrainshowersandthunder": 95, "lightrainshowersandthunder": 95,
    "sleetandthunder": 96, "snowandthunder": 96,
}


def symbol_to_wmo(symbol: str | None) -> int | None:
    if not symbol:
        return None
    for suffix in ("_day", "_night", "_polartwilight"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
            break
    return SYMBOL_TO_WMO.get(symbol)


def _prov(product: str, valid_time: str | None = None,
          stale: bool = False) -> Provenance:
    """Provenance naming MET Norway.

    `issued_at` carries the model run time MET Norway publishes in
    properties.meta.updated_at -- a real issue time from the source, never
    request time.
    """
    if stale:
        product = f"{product} - cached copy, upstream unavailable"
    issued = None
    if valid_time:
        try:
            issued = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
        except ValueError:
            issued = None
    return Provenance(
        source="MET Norway (Locationforecast 2.0)",
        product=product,
        issued_at=issued,
        url="https://api.met.no/weatherapi/locationforecast/2.0/documentation",
        authoritative=False,
    )


def _ttl_from_expires(r: httpx.Response, default: int = 1800) -> int:
    """Seconds until the Expires header the server sent, floored at 60s."""
    exp = r.headers.get("Expires")
    if not exp:
        return default
    try:
        when = datetime.strptime(exp, "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return default
    return max(60, int((when - datetime.now(timezone.utc)).total_seconds()))


async def _get(lat: float, lon: float) -> dict:
    """Fetch a forecast, honouring the MET Norway caching contract.

    Their terms require clients to respect Expires and to revalidate with
    If-Modified-Since rather than re-polling. Both are done here: the cache
    TTL comes from the Expires header the server sent, not a number we picked,
    and a stored Last-Modified is replayed so an unchanged forecast costs a
    304 instead of a payload.

    Coordinates are truncated to 4 decimals because MET Norway asks for that
    explicitly -- more precision fragments their cache and is treated as abuse.
    """
    s = get_settings()
    lat, lon = round(lat, 4), round(lon, 4)
    ck = upstream_cache.key("metno", lat, lon)

    if (hit := upstream_cache.get(ck)) is not None:
        return hit

    headers = {"User-Agent": s.metno_user_agent}
    meta = upstream_cache.get(ck + ":meta") or {}
    if lm := meta.get("last_modified"):
        headers["If-Modified-Since"] = lm

    async with httpx.AsyncClient(timeout=s.http_timeout) as client:
        r = await client.get(BASE, params={"lat": lat, "lon": lon},
                             headers=headers)

    if r.status_code == 304:
        # Unchanged upstream: the long-lived copy is still correct.
        if (stale := upstream_cache.get(ck + ":stale")) is not None:
            upstream_cache.set(ck, stale, ttl=_ttl_from_expires(r))
            return stale
    r.raise_for_status()
    data = r.json()

    upstream_cache.set(ck, data, ttl=_ttl_from_expires(r))
    upstream_cache.set(ck + ":stale", data, ttl=21600)
    upstream_cache.set(ck + ":meta",
                       {"last_modified": r.headers.get("Last-Modified")},
                       ttl=21600)
    return data


def _ms_to_kmh(v):
    return None if v is None else round(v * 3.6, 1)


def _condition(wmo: int | None, symbol: str | None) -> str:
    from . import openmeteo          # shared WMO vocabulary
    if wmo is not None:
        return openmeteo.describe_code(wmo)
    return (symbol or "unknown").replace("_", " ")


# ------------------------------------------------------------------ current
async def current(lat: float, lon: float) -> dict:
    data = await _get(lat, lon)
    props = data["properties"]
    first = props["timeseries"][0]
    inst = first["data"]["instant"]["details"]
    nxt = first["data"].get("next_1_hours") or first["data"].get("next_6_hours") or {}
    symbol = nxt.get("summary", {}).get("symbol_code")
    wmo = symbol_to_wmo(symbol)

    return {
        "temp_c": inst.get("air_temperature"),
        "feels_like_c": inst.get("apparent_air_temperature"),
        "humidity_pct": inst.get("relative_humidity"),
        "precip_mm": nxt.get("details", {}).get("precipitation_amount"),
        "weather_code": wmo,
        "condition": _condition(wmo, symbol),
        "wind_kmh": _ms_to_kmh(inst.get("wind_speed")),
        # MET Norway does not publish wind gusts for most of India. None is the
        # honest value: advisory.py already falls back to sustained wind, and
        # tools.py renders it as an em dash rather than inventing a figure.
        "wind_gust_kmh": _ms_to_kmh(inst.get("wind_speed_of_gust")),
        "wind_dir_deg": inst.get("wind_from_direction"),
        "pressure_hpa": inst.get("air_pressure_at_sea_level"),
        "cloud_pct": inst.get("cloud_area_fraction"),
        "observed_at": first.get("time"),
        "provenance": _prov("deterministic analysis",
                            valid_time=props["meta"].get("updated_at")),
    }


# ----------------------------------------------------------------- forecast
async def forecast(lat: float, lon: float, days: int = 7) -> dict:
    """Aggregate the MET Norway timeseries into the daily shape advisory.py reads.

    Their feed is hourly for roughly the first two days and six-hourly after,
    so precipitation is accumulated with an explicit coverage cursor: each
    window is counted once and never double-counted where hourly and
    six-hourly entries overlap.
    """
    data = await _get(lat, lon)
    props = data["properties"]

    buckets: dict[str, dict] = {}
    covered_until: datetime | None = None

    for entry in props["timeseries"]:
        t = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
        key = t.astimezone(IST).date().isoformat()
        b = buckets.setdefault(key, {"temps": [], "winds": [], "gusts": [],
                                     "rain": 0.0, "codes": [], "probs": []})

        inst = entry["data"]["instant"]["details"]
        if (v := inst.get("air_temperature")) is not None:
            b["temps"].append(v)
        if (v := inst.get("wind_speed")) is not None:
            b["winds"].append(v)
        if (v := inst.get("wind_speed_of_gust")) is not None:
            b["gusts"].append(v)

        one = entry["data"].get("next_1_hours")
        six = entry["data"].get("next_6_hours")
        window, hours = (one, 1) if one else ((six, 6) if six else (None, 0))
        if window and (covered_until is None or t >= covered_until):
            det = window.get("details", {})
            b["rain"] += det.get("precipitation_amount") or 0.0
            if (p := det.get("probability_of_precipitation")) is not None:
                b["probs"].append(p)
            if sym := window.get("summary", {}).get("symbol_code"):
                b["codes"].append(symbol_to_wmo(sym))
            covered_until = t + timedelta(hours=hours)

    out_days = []
    for key in sorted(buckets)[:days]:
        b = buckets[key]
        if not b["temps"]:
            continue
        codes = [c for c in b["codes"] if c is not None]
        # Worst (highest WMO code) drives the headline condition, matching the
        # convention of the primary provider's own daily field.
        code = max(codes) if codes else None
        out_days.append({
            "date": key,
            "tmax_c": round(max(b["temps"]), 1),
            "tmin_c": round(min(b["temps"]), 1),
            "rain_mm": round(b["rain"], 1),
            # MET Norway omits probability of precipitation for most Indian
            # points; None renders as an em dash rather than a fabricated %.
            "rain_prob_pct": round(max(b["probs"])) if b["probs"] else None,
            "wind_max_kmh": _ms_to_kmh(max(b["winds"])) if b["winds"] else None,
            "gust_max_kmh": _ms_to_kmh(max(b["gusts"])) if b["gusts"] else None,
            "humidity_max_pct": None,
            "weather_code": code,
            "condition": _condition(code, None),
            "sunrise": None,
            "sunset": None,
        })

    return {
        "days": out_days,
        "hourly": {},
        "provenance": _prov(f"{len(out_days)}-day deterministic forecast",
                            valid_time=props["meta"].get("updated_at")),
    }
