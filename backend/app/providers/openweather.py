"""OpenWeatherMap provider (free tier, 2.5 endpoints).

Preferred over the IP-limited sources because it is *keyed*: the quota belongs
to this deployment rather than to whatever else shares the host's egress IP,
which is what killed Open-Meteo in production. It also supplies the two fields
MET Norway omits for India -- wind gusts and probability of precipitation --
so the 34 kt small-craft threshold is evaluated on real gusts again.

Endpoints (deliberately 2.5, not 3.0):
    /data/2.5/weather    current conditions, incl. wind.gust
    /data/2.5/forecast   5 days x 3-hourly, incl. wind.gust and pop
/data/3.0/onecall is a separately-subscribed product and returns 401 on a
free-tier key. Do not "upgrade" these paths.

THREE UNIT CONVERSIONS LIVE HERE, and nowhere else. Every one of them is a
silent under-warning if it is wrong, because advisory.py compares against
km/h thresholds and will happily accept a smaller number:

  1. units=metric returns wind in METRES PER SECOND. Multiplied by 3.6 here,
     at the adapter boundary. A raw 17 m/s gust (61 km/h) looks like a calm
     day to a threshold expecting km/h.
  2. `pop` is a 0-1 fraction. Multiplied by 100 here. Unconverted, a 90%
     chance of rain renders as "1%".
  3. The forecast is 3-hourly and must be aggregated to local days. Buckets
     use Asia/Kolkata, not UTC -- a UTC bucket boundary at 05:30 IST splits
     an Indian day in the wrong place and reports the wrong daily maximum.

Also: `rain` is ABSENT when there is no rain, rather than present and zero.
Treated as 0.0 in the sum; a KeyError or a None here would poison the daily
total.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..cache import upstream_cache
from ..config import get_settings
from ..schemas import Provenance

CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"
FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

# Daily aggregation must bucket by Indian local day.
IST = timezone(timedelta(hours=5, minutes=30))

MS_TO_KMH = 3.6


def available() -> bool:
    """True when a key is configured. No key means this provider is skipped
    entirely rather than making a request that would 401."""
    return bool(get_settings().openweather_api_key)


def _to_kmh(v) -> float | None:
    """The single m/s -> km/h boundary. Nothing downstream converts again."""
    return None if v is None else round(v * MS_TO_KMH, 1)


def _pop_to_pct(pop) -> float | None:
    """`pop` is a 0-1 fraction; advisory and templates want a percentage."""
    return None if pop is None else round(pop * 100)


def _rain_mm(block: dict | None, key: str) -> float:
    """Rain accumulation for a window.

    OpenWeather omits the `rain` object entirely when there is no rain, so an
    absent key means 0.0 -- not missing data, and not a reason to skip the
    bucket.
    """
    if not block:
        return 0.0
    return float(block.get(key) or 0.0)


# OpenWeather condition ids -> WMO codes, so advisory.py keeps reading the
# `weather_code` field it already understands (including THUNDER_CODES).
def condition_to_wmo(code: int | None) -> int | None:
    if code is None:
        return None
    code = int(code)
    if 200 <= code < 300:                       # thunderstorm group
        return 95
    if 300 <= code < 400:                       # drizzle
        return 51 if code in (300, 310) else 53
    if code in (500, 520):
        return 61
    if code in (501, 521):
        return 63
    if code in (502, 503, 504, 522, 531):
        return 65
    if code == 511:
        return 66
    if 600 <= code < 700:                       # snow
        return 71 if code in (600, 620) else (73 if code in (601, 621) else 75)
    if code in (701, 721, 741):                 # mist / haze / fog
        return 45
    if 700 <= code < 800:
        return 45
    if code == 800:
        return 0
    if code == 801:
        return 1
    if code == 802:
        return 2
    if code in (803, 804):
        return 3
    return None


def _prov(product: str, valid_time: datetime | None = None) -> Provenance:
    return Provenance(
        source="OpenWeatherMap",
        product=product,
        issued_at=valid_time,
        url="https://openweathermap.org/api",
        authoritative=False,
    )


async def _get(url: str, lat: float, lon: float, ttl: int) -> dict:
    """GET with cache. 401/429 propagate so nwp.py can fail over immediately.

    The key is read from settings at call time and never logged; it is
    supplied only through the OPENWEATHER_API_KEY environment variable.
    """
    s = get_settings()
    if not s.openweather_api_key:
        raise RuntimeError("OPENWEATHER_API_KEY is not configured")

    # Cache key excludes the API key -- it must never reach a cache key, a log
    # line, or an error message.
    ck = upstream_cache.key("openweather", url, round(lat, 4), round(lon, 4))
    if (hit := upstream_cache.get(ck)) is not None:
        return hit

    params = {"lat": round(lat, 4), "lon": round(lon, 4),
              "appid": s.openweather_api_key, "units": "metric"}
    async with httpx.AsyncClient(timeout=s.http_timeout) as client:
        r = await client.get(url, params=params)
    r.raise_for_status()
    data = r.json()
    upstream_cache.set(ck, data, ttl=ttl)
    return data


# ------------------------------------------------------------------ current
async def current(lat: float, lon: float) -> dict:
    data = await _get(CURRENT_URL, lat, lon, ttl=600)
    main = data.get("main") or {}
    wind = data.get("wind") or {}
    weather = (data.get("weather") or [{}])[0]
    wmo = condition_to_wmo(weather.get("id"))

    observed = None
    if data.get("dt"):
        observed = datetime.fromtimestamp(data["dt"], tz=timezone.utc)

    from . import openmeteo                     # shared WMO vocabulary
    return {
        "temp_c": main.get("temp"),
        "feels_like_c": main.get("feels_like"),
        "humidity_pct": main.get("humidity"),
        # `rain` is absent when dry; 1h is the last-hour accumulation.
        "precip_mm": _rain_mm(data.get("rain"), "1h"),
        "weather_code": wmo,
        "condition": (openmeteo.describe_code(wmo) if wmo is not None
                      else weather.get("description") or "unknown"),
        "wind_kmh": _to_kmh(wind.get("speed")),          # m/s -> km/h
        "wind_gust_kmh": _to_kmh(wind.get("gust")),      # m/s -> km/h
        "wind_dir_deg": wind.get("deg"),
        "pressure_hpa": main.get("pressure"),
        "cloud_pct": (data.get("clouds") or {}).get("all"),
        "observed_at": observed.isoformat() if observed else None,
        "provenance": _prov("current conditions", observed),
    }


# ----------------------------------------------------------------- forecast
async def forecast(lat: float, lon: float, days: int = 7) -> dict:
    """5-day / 3-hourly forecast aggregated into local days.

    OpenWeather's free tier caps at 5 days; a request for 7 returns the 5 it
    has rather than erroring. advisory.py reads days[0..2], so this is ample
    for every threshold it evaluates.
    """
    data = await _get(FORECAST_URL, lat, lon, ttl=1800)
    buckets: dict[str, dict] = {}

    for slot in data.get("list", []):
        when = datetime.fromtimestamp(slot["dt"], tz=timezone.utc).astimezone(IST)
        key = when.date().isoformat()
        b = buckets.setdefault(key, {"temps": [], "winds": [], "gusts": [],
                                     "rain": 0.0, "codes": [], "pops": [],
                                     "humid": []})
        main = slot.get("main") or {}
        wind = slot.get("wind") or {}

        for field in ("temp", "temp_max", "temp_min"):
            if (v := main.get(field)) is not None:
                b["temps"].append(v)
        if (v := main.get("humidity")) is not None:
            b["humid"].append(v)
        if (v := wind.get("speed")) is not None:
            b["winds"].append(v)
        if (v := wind.get("gust")) is not None:
            b["gusts"].append(v)
        # Absent `rain` means zero, not missing.
        b["rain"] += _rain_mm(slot.get("rain"), "3h")
        if (p := slot.get("pop")) is not None:
            b["pops"].append(p)
        if code := (slot.get("weather") or [{}])[0].get("id"):
            b["codes"].append(condition_to_wmo(code))

    from . import openmeteo
    out_days = []
    for key in sorted(buckets)[:days]:
        b = buckets[key]
        if not b["temps"]:
            continue
        codes = [c for c in b["codes"] if c is not None]
        code = max(codes) if codes else None
        out_days.append({
            "date": key,
            "tmax_c": round(max(b["temps"]), 1),
            "tmin_c": round(min(b["temps"]), 1),
            "rain_mm": round(b["rain"], 1),
            # 0-1 fraction -> percentage.
            "rain_prob_pct": _pop_to_pct(max(b["pops"])) if b["pops"] else None,
            "wind_max_kmh": _to_kmh(max(b["winds"])) if b["winds"] else None,
            "gust_max_kmh": _to_kmh(max(b["gusts"])) if b["gusts"] else None,
            "humidity_max_pct": max(b["humid"]) if b["humid"] else None,
            "weather_code": code,
            "condition": (openmeteo.describe_code(code) if code is not None
                          else "unknown"),
            "sunrise": None,
            "sunset": None,
        })

    return {
        "days": out_days,
        "hourly": {},
        "provenance": _prov(f"{len(out_days)}-day forecast (3-hourly, aggregated)"),
    }
