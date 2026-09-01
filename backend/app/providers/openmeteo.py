"""NWP + reanalysis provider.

Open-Meteo is used here as a *stand-in for the operational NWP pipeline*.
It serves the same NCEP GFS 0.25 deg and ECMWF IFS output the production
system would ingest directly from NOMADS / MARS as GRIB2, plus the ERA5
reanalysis archive used for climate trends.

Production swap-in (documented in ARCHITECTURE.md):
  forecast   -> in-house GRIB2 ingest of GFS 0.25 + IMD GFS-T1534 / WRF-ARW
                4 km, decoded with cfgrib, tiled into Zarr on object storage
  climate     -> IMD 0.25 deg gridded rainfall (1901-) + ERA5 in TimescaleDB
The adapter interface below does not change when that swap happens.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from ..cache import upstream_cache
from ..config import get_settings
from ..schemas import Provenance

WMO_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "depositing rime fog", 51: "light drizzle",
    53: "moderate drizzle", 55: "dense drizzle", 56: "light freezing drizzle",
    57: "dense freezing drizzle", 61: "light rain", 63: "moderate rain",
    65: "heavy rain", 66: "light freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "light rain showers", 81: "moderate rain showers",
    82: "violent rain showers", 85: "light snow showers",
    86: "heavy snow showers", 95: "thunderstorm",
    96: "thunderstorm with light hail", 99: "thunderstorm with heavy hail",
}


def describe_code(code: int | None) -> str:
    if code is None:
        return "unknown"
    return WMO_CODES.get(int(code), f"code {code}")


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a source-supplied ISO timestamp. Never invents one."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _prov(product: str, valid_time: str | None = None,
          stale: bool = False) -> Provenance:
    """Build a provenance record.

    `issued_at` carries the timestamp the SOURCE reported for this data and is
    left None when the source does not publish one. It is never filled with
    `now()` — a provenance record that claims data was issued at request time
    is exactly the kind of fabricated fact this system exists to prevent.
    """
    if stale:
        product = f"{product} — cached copy, upstream unavailable"
    return Provenance(
        source="NCEP GFS / ECMWF IFS (via Open-Meteo)",
        product=product,
        issued_at=_parse_ts(valid_time),
        url="https://open-meteo.com/en/docs",
        authoritative=False,
    )


async def _get(url: str, params: dict, ttl: int = 600, retries: int = 2) -> dict:
    """GET with cache + bounded retry.

    Operational met feeds are flaky under load (which is exactly when people
    ask). A single transient 5xx must never surface as a failed answer, so
    the fetch retries with backoff and, as a last resort, serves the stale
    cached payload rather than nothing -- clearly marked by its issue time.
    """
    s = get_settings()
    ck = upstream_cache.key(url, sorted(params.items()))
    if (hit := upstream_cache.get(ck)) is not None:
        return hit

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=s.http_timeout) as client:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json()
            upstream_cache.set(ck, data, ttl=ttl)
            # keep a long-lived stale copy for the degraded path
            upstream_cache.set(ck + ":stale", data, ttl=21600)
            return data
        except Exception as exc:            # noqa: BLE001 - retried below
            last = exc
            if attempt < retries:
                await asyncio.sleep(0.35 * (2 ** attempt))

    if (stale := upstream_cache.get(ck + ":stale")) is not None:
        # Serving a stale copy is allowed; serving it silently is not. The
        # marker travels into the provenance record and onto the screen.
        return {**stale, "_stale": True}
    raise last if last else RuntimeError("upstream fetch failed")


# ------------------------------------------------------------------ current
async def current(lat: float, lon: float) -> dict:
    s = get_settings()
    data = await _get(s.openmeteo_forecast_base, {
        "latitude": lat, "longitude": lon,
        "current": ("temperature_2m,relative_humidity_2m,apparent_temperature,"
                    "precipitation,weather_code,wind_speed_10m,wind_direction_10m,"
                    "wind_gusts_10m,surface_pressure,cloud_cover"),
        "timezone": "Asia/Kolkata",
    }, ttl=600)
    cur = data.get("current", {})
    return {
        "temp_c": cur.get("temperature_2m"),
        "feels_like_c": cur.get("apparent_temperature"),
        "humidity_pct": cur.get("relative_humidity_2m"),
        "precip_mm": cur.get("precipitation"),
        "weather_code": cur.get("weather_code"),
        "condition": describe_code(cur.get("weather_code")),
        "wind_kmh": cur.get("wind_speed_10m"),
        "wind_gust_kmh": cur.get("wind_gusts_10m"),
        "wind_dir_deg": cur.get("wind_direction_10m"),
        "pressure_hpa": cur.get("surface_pressure"),
        "cloud_pct": cur.get("cloud_cover"),
        "observed_at": cur.get("time"),
        "provenance": _prov("deterministic analysis, 0.25 deg",
                            valid_time=cur.get("time"),
                            stale=bool(data.get("_stale"))),
    }


# ----------------------------------------------------------------- forecast
async def forecast(lat: float, lon: float, days: int = 7) -> dict:
    s = get_settings()
    days = max(1, min(days, 16))
    data = await _get(s.openmeteo_forecast_base, {
        "latitude": lat, "longitude": lon,
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_sum,precipitation_probability_max,"
                  "wind_speed_10m_max,wind_gusts_10m_max,sunrise,sunset,"
                  "relative_humidity_2m_max"),
        "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m",
        "forecast_days": days,
        "timezone": "Asia/Kolkata",
    }, ttl=1800)

    d = data.get("daily", {})
    out_days = []
    for i, date in enumerate(d.get("time", [])):
        out_days.append({
            "date": date,
            "tmax_c": d["temperature_2m_max"][i],
            "tmin_c": d["temperature_2m_min"][i],
            "rain_mm": d["precipitation_sum"][i],
            "rain_prob_pct": d["precipitation_probability_max"][i],
            "wind_max_kmh": d["wind_speed_10m_max"][i],
            "gust_max_kmh": d["wind_gusts_10m_max"][i],
            "humidity_max_pct": d.get("relative_humidity_2m_max", [None] * 20)[i],
            "weather_code": d["weather_code"][i],
            "condition": describe_code(d["weather_code"][i]),
            "sunrise": d.get("sunrise", [None] * 20)[i],
            "sunset": d.get("sunset", [None] * 20)[i],
        })
    return {
        "days": out_days,
        "hourly": data.get("hourly", {}),
        # Open-Meteo does not publish the underlying model run time, so
        # issued_at stays empty rather than being invented.
        "provenance": _prov(f"{days}-day deterministic forecast, 0.25 deg",
                            stale=bool(data.get("_stale"))),
    }


# -------------------------------------------------------------- air quality
async def air_quality(lat: float, lon: float) -> dict:
    s = get_settings()
    data = await _get(s.openmeteo_aq_base, {
        "latitude": lat, "longitude": lon,
        "current": "pm10,pm2_5,carbon_monoxide,nitrogen_dioxide,ozone,sulphur_dioxide",
        "timezone": "Asia/Kolkata",
    }, ttl=1800)
    cur = data.get("current", {})
    pm25 = cur.get("pm2_5")
    return {
        "pm2_5": pm25,
        "pm10": cur.get("pm10"),
        "no2": cur.get("nitrogen_dioxide"),
        "o3": cur.get("ozone"),
        "so2": cur.get("sulphur_dioxide"),
        "co": cur.get("carbon_monoxide"),
        "cpcb_band": cpcb_band(pm25),
        "observed_at": cur.get("time"),
        "provenance": Provenance(
            source="CAMS global composition (via Open-Meteo)",
            product=("air quality analysis"
                     + (" — cached copy, upstream unavailable"
                        if data.get("_stale") else "")),
            issued_at=_parse_ts(cur.get("time")),
            url="https://open-meteo.com/en/docs/air-quality-api",
        ),
    }


def cpcb_band(pm25: float | None) -> str:
    """CPCB National AQI bands for PM2.5 (24-h, ug/m3)."""
    if pm25 is None:
        return "unknown"
    for limit, label in ((30, "Good"), (60, "Satisfactory"), (90, "Moderate"),
                         (120, "Poor"), (250, "Very Poor")):
        if pm25 <= limit:
            return label
    return "Severe"


# ---------------------------------------------------------------- climate
async def climate_series(lat: float, lon: float, years_back: int = 30,
                         month: int | None = None) -> dict:
    """Annual (or single-month) aggregates from the ERA5 reanalysis archive.

    ERA5 lags real time by ~5 days, so the window ends last completed year.
    """
    s = get_settings()
    end_year = datetime.now().year - 1
    start_year = end_year - years_back + 1

    # Fetched in 10-year chunks. Two reasons: a single 40-year daily pull is
    # an expensive request that public archives rate-limit, and decade chunks
    # cache independently, so "30 years" and "40 years" for the same place
    # share three of their four requests.
    #
    # Production note: this provider is replaced by a local read of the IMD
    # 0.25 deg gridded rainfall series (1901-) and ERA5 in TimescaleDB, where
    # the whole query is one indexed scan and no rate limit applies.
    buckets: dict[int, dict[str, list]] = {}
    chunks = [(y, min(y + 9, end_year)) for y in range(start_year, end_year + 1, 10)]

    for c_start, c_end in chunks:
        data = await _get(s.openmeteo_archive_base, {
            "latitude": lat, "longitude": lon,
            "start_date": f"{c_start}-01-01",
            "end_date": f"{c_end}-12-31",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": "Asia/Kolkata",
        }, ttl=604800)          # archive data never changes: cache a week

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        rain = daily.get("precipitation_sum", [])

        for i, ds in enumerate(dates):
            y, m = int(ds[:4]), int(ds[5:7])
            if month and m != month:
                continue
            b = buckets.setdefault(y, {"tmax": [], "tmin": [], "rain": []})
            if tmax[i] is not None:
                b["tmax"].append(tmax[i])
            if tmin[i] is not None:
                b["tmin"].append(tmin[i])
            if rain[i] is not None:
                b["rain"].append(rain[i])

    series = []
    for y in sorted(buckets):
        b = buckets[y]
        if not b["tmax"]:
            continue
        series.append({
            "year": y,
            "mean_tmax_c": round(sum(b["tmax"]) / len(b["tmax"]), 2),
            "mean_tmin_c": round(sum(b["tmin"]) / len(b["tmin"]), 2) if b["tmin"] else None,
            "total_rain_mm": round(sum(b["rain"]), 1),
            "rain_days": sum(1 for v in b["rain"] if v >= 2.5),
        })

    return {
        "series": series,
        "start_year": start_year,
        "end_year": end_year,
        "month": month,
        "provenance": Provenance(
            source="ECMWF ERA5 reanalysis (via Open-Meteo archive)",
            product=f"daily aggregates {start_year}-{end_year}",
            url="https://open-meteo.com/en/docs/historical-weather-api",
        ),
    }


def linear_trend(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least squares slope + intercept. No numpy dependency."""
    n = len(xs)
    if n < 3:
        return 0.0, (ys[0] if ys else 0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    slope = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / denom
    return slope, my - slope * mx
