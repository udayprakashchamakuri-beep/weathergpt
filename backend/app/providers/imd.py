"""India Meteorological Department -- the authoritative source.

IMD publishes a real public REST API at https://api.imd.gov.in/api/v1/*.
Access needs a free account + IP whitelisting:
    register  https://api.imd.gov.in/public/register.php
    reference https://api.imd.gov.in/public/api_reference.html

Every endpoint below is a real documented IMD endpoint. With `IMD_API_KEY`
set, WeatherGPT prefers IMD for anything IMD is authoritative for -- warnings,
nowcasts, cyclone tracks, marine bulletins -- and falls back to NWP output
only for variables IMD does not expose. Answers are labelled accordingly, so
a user always knows whether they are reading an official IMD warning or a
model-derived estimate. That distinction is a public-safety requirement, not
a UI nicety.

Without a key this module reports `available=False` and the orchestrator
degrades cleanly instead of failing.
"""
from __future__ import annotations

import httpx

from ..cache import upstream_cache
from ..config import get_settings
from ..schemas import Provenance, Severity

ENDPOINTS = {
    # --- observations & nowcast
    "current_wx":            "/current_wx",                 # ?id=<station id>
    "aws_data":              "/aws_data",                   # ?id=NDL | ?sid=<state 1-36>
    "district_nowcast":      "/districtnowcast",            # ?id=<district id>
    "station_nowcast":       "/stationnowcast",             # ?id=<station name>
    # --- forecast
    "city_forecast":         "/cityforecast",               # ?id=<city id>  7 day
    "city_forecast_loc":     "/cityforecastloc",            # with lat/lon
    "subdiv_rain_forecast":  "/subdivision_rainfall_forecast",
    "district_rain_forecast": "/state_district_rainfall_forecast",
    "basin_qpf":             "/basinqpf",                   # river-basin QPF, 5 day
    # --- warnings
    "district_warning":      "/districtwarning",            # ?id=<district obj id>
    "subdivision_warning":   "/subdivisionwarning",
    # --- cyclone (GeoJSON)
    "cyclone_track":         "/cyclone_track",
    "cyclone_wind":          "/cyclone_wind",               # MultiPolygon 27/34/50/64 kt
    "cyclone_cou":           "/cyclone_cou",                # cone of uncertainty
    # --- marine
    "port_warning":          "/portwarning",
    "sea_bulletin":          "/seabulletin",
    "coastal_bulletin":      "/coastalbulletin",
    # --- rainfall accounting
    "district_rainfall":     "/districtrainfall",
    "state_rainfall":        "/staterainfall",
    # --- astronomy
    "sunmoon":               "/sunmoon",                    # ?lat=&lon=
}

# IMD colour code -> internal severity. IMD uses the standard impact-based
# green / yellow / orange / red convention.
COLOUR_SEVERITY = {
    "green": Severity.GREEN,
    "yellow": Severity.YELLOW,
    "orange": Severity.ORANGE,
    "red": Severity.RED,
    "#00ff00": Severity.GREEN,
    "#ffff00": Severity.YELLOW,
    "#ffa500": Severity.ORANGE,
    "#ff0000": Severity.RED,
}


def available() -> bool:
    return bool(get_settings().imd_api_key)


def provenance(product: str, path: str) -> Provenance:
    return Provenance(
        source="India Meteorological Department (IMD)",
        product=product,
        url=f"https://api.imd.gov.in/api/v1{path}",
        authoritative=True,
    )


async def call(name: str, params: dict | None = None, ttl: int = 600):
    """Call a documented IMD endpoint. Returns None when unavailable."""
    s = get_settings()
    if not s.imd_api_key:
        return None
    path = ENDPOINTS.get(name)
    if not path:
        raise ValueError(f"unknown IMD endpoint: {name}")

    url = f"{s.imd_api_base}{path}"
    ck = upstream_cache.key("imd", url, sorted((params or {}).items()))
    if (hit := upstream_cache.get(ck)) is not None:
        return hit

    headers = {"x-api-key": s.imd_api_key, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=s.http_timeout) as client:
            r = await client.get(url, params=params or {}, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception:
        return None

    if isinstance(data, dict) and data.get("error"):
        return None
    upstream_cache.set(ck, data, ttl=ttl)
    return data


# District object ids come from IMD's own district master list. Until that
# list is loaded and joined to a boundary layer, a place name cannot be
# turned into an id -- and guessing is worse than abstaining, so the
# resolver returns None and callers report the gap instead of issuing a
# request that would silently return the wrong district.
def resolve_district_id(place_name: str) -> int | None:
    return None


async def district_warnings(district_id: int | str | None):
    if district_id is None:
        return None
    data = await call("district_warning", {"id": district_id}, ttl=900)
    if not data:
        return None
    return {"raw": data, "provenance": provenance("district warning, 5 day",
                                                  ENDPOINTS["district_warning"])}


async def nowcast(district_id: int | str | None):
    if district_id is None:
        return None
    data = await call("district_nowcast", {"id": district_id}, ttl=300)
    if not data:
        return None
    return {"raw": data, "provenance": provenance("district nowcast, 3 h",
                                                  ENDPOINTS["district_nowcast"])}


async def cyclone_state():
    """Track + wind field + cone of uncertainty, as GeoJSON."""
    track = await call("cyclone_track", ttl=900)
    if not track:
        return None
    return {
        "track": track,
        "wind": await call("cyclone_wind", ttl=900),
        "cone": await call("cyclone_cou", ttl=900),
        "provenance": provenance("cyclone track / wind / cone of uncertainty",
                                 ENDPOINTS["cyclone_track"]),
    }


async def marine(port_id: str | None = None):
    return {
        "port": await call("port_warning", {"id": port_id} if port_id else None, ttl=900),
        "coastal": await call("coastal_bulletin", ttl=900),
        "provenance": provenance("port warning + coastal bulletin",
                                 ENDPOINTS["coastal_bulletin"]),
    }
