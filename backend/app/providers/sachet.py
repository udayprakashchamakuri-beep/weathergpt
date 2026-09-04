"""NDMA SACHET -- authoritative Indian alerts without an IMD key.

SACHET is the National Disaster Management Authority's public CAP
dissemination portal (https://sachet.ndma.gov.in). It carries the warnings
that IMD, the Central Water Commission and the state SDMAs issue, already
geo-tagged and already colour-coded on the standard yellow/orange/red
impact ladder. No key, no registration, no IP whitelisting.

That matters because `providers/imd.py` -- the authoritative adapter -- needs
a registered key AND a whitelisted source IP, and every IMD endpoint returns
"Your IP/Domain needs to be whitelisted" without one. SACHET reaches the same
warnings by the channel they are *published* on rather than the one they are
*produced* on, so an alert here is genuinely authoritative:

    "IMD Guwahati has issued forecast for Thunderstorm with Lightning ...
     Issued in Public Interest by ASDMA."

Provenance therefore names the originator and the carrier separately --
"IMD Shillong via NDMA SACHET" -- and sets authoritative=True. A model-derived
forecast never gets that flag; see providers/nwp.py.

This adapter is read-only, polled, and cached. The endpoint backs NDMA's own
public alert map, so it is used politely: one request per poll interval, a
real User-Agent, and the response cached for the whole interval.
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from ..cache import upstream_cache
from ..config import get_settings
from ..schemas import AlertEvent, Provenance, Severity

log = logging.getLogger(__name__)

# Why the last fetch failed, surfaced through /api/health. An empty feed and
# a blocked feed look identical from the outside otherwise, which is how a
# deployment ends up quietly serving no warnings at all.
LAST_ERROR: str | None = None

IST = timezone(timedelta(hours=5, minutes=30))

# SACHET publishes the same green/yellow/orange/red ladder advisory.py already
# encodes, so this is a direct mapping rather than a reinterpretation.
COLOUR_SEVERITY = {
    "green": Severity.GREEN,
    "yellow": Severity.YELLOW,
    "orange": Severity.ORANGE,
    "red": Severity.RED,
}

# Fallback when severity_color is absent or unrecognised. These are the words
# the feed uses in its `severity` field.
WORD_SEVERITY = {
    "watch": Severity.YELLOW,
    "alert": Severity.ORANGE,
    "warning": Severity.RED,
    "yellow": Severity.YELLOW,
    "orange": Severity.ORANGE,
    "red": Severity.RED,
}

# "Fri Sep 04 01:22:00 IST 2026" -- java.util.Date.toString(). %Z will not
# parse "IST" portably, so the zone token is matched out and applied by hand.
_JAVA_DATE = re.compile(
    r"^[A-Za-z]{3}\s+([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+"
    r"([A-Za-z]{2,5})\s+(\d{4})$")
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}


def parse_ts(raw: str | None) -> datetime | None:
    """Parse SACHET's Java-style timestamp. Returns None rather than raising:
    a malformed date must not drop an otherwise valid warning."""
    if not raw or not isinstance(raw, str):
        return None
    m = _JAVA_DATE.match(raw.strip())
    if not m:
        return None
    mon, day, hh, mm, ss, zone, year = m.groups()
    if mon not in _MONTHS:
        return None
    tz = IST if zone.upper() in ("IST", "UTC+0530") else timezone.utc
    try:
        return datetime(int(year), _MONTHS[mon], int(day),
                        int(hh), int(mm), int(ss), tzinfo=tz)
    except ValueError:
        return None


def parse_centroid(raw: str | None) -> tuple[float, float] | None:
    """SACHET's centroid is "lon,lat" -- the GeoJSON order, not the
    lat/lon order the rest of this service uses. Getting this backwards puts
    an Assam thunderstorm in the Indian Ocean, so it is asserted in the tests.

    Returns (lat, lon), or None if the pair is unusable or out of range.
    """
    if not raw or not isinstance(raw, str) or "," not in raw:
        return None
    lon_s, _, lat_s = raw.partition(",")
    try:
        lon, lat = float(lon_s), float(lat_s)
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return lat, lon


def radius_km(area_covered) -> float:
    """`area_covered` is the affected area in square kilometres. The geofence
    matcher wants a radius, so treat the area as a disc: r = sqrt(A / pi).

    Clamped to [10, 300] km. An uncapped value would let one state-wide
    advisory match every subscriber in the country, which is how a
    dissemination system trains people to ignore it.
    """
    try:
        a = float(area_covered)
    except (TypeError, ValueError):
        return 50.0
    if a <= 0:
        return 50.0
    return max(10.0, min(300.0, round(math.sqrt(a / math.pi), 1)))


def severity_of(row: dict) -> Severity:
    colour = str(row.get("severity_color") or "").strip().lower()
    if colour in COLOUR_SEVERITY:
        return COLOUR_SEVERITY[colour]
    word = str(row.get("severity") or "").strip().lower()
    return WORD_SEVERITY.get(word, Severity.YELLOW)


def headline_of(row: dict) -> str:
    """The hazard, and only the hazard.

    Deliberately excludes the area. AlertEvent carries `area` as its own
    field and i18n's warning template already injects it, so folding the area
    in here renders the district list twice in every delivered message -- a
    twelve-district Meghalaya warning printed the whole list twice before this
    was fixed. Compare alerts.scan_location(), which sets headline to the
    reason alone for the same reason.

    Falls back to the originator's free-text message, trimmed on a word
    boundary, when the feed omits a hazard type.
    """
    kind = str(row.get("disaster_type") or "").strip()
    if kind:
        return kind
    msg = " ".join(str(row.get("warning_message") or "").split())
    if not msg:
        return "Weather warning"
    return msg if len(msg) <= 160 else msg[:157].rsplit(" ", 1)[0] + "..."


def parse_area_json(raw) -> dict | None:
    """SACHET's `area_json` is the alert's real CAP footprint as GeoJSON.

    Only FetchLocationWiseAlerts returns it; the global FetchAllAlertDetails
    feed does not, which is why the disc approximation still exists as the
    fallback. Coordinates are GeoJSON order -- [lon, lat].
    """
    if not raw:
        return None
    geom = raw
    if isinstance(raw, str):
        try:
            geom = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(geom, dict):
        return None
    if geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None
    if not isinstance(geom.get("coordinates"), list) or not geom["coordinates"]:
        return None
    return geom


def _ring_contains(lat: float, lon: float, ring: list) -> bool:
    """Ray casting against one linear ring. Ring points are [lon, lat]."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        try:
            xi, yi = float(ring[i][0]), float(ring[i][1])
            xj, yj = float(ring[j][0]), float(ring[j][1])
        except (TypeError, ValueError, IndexError):
            j = i
            continue
        if (yi > lat) != (yj > lat):
            denom = (yj - yi) or 1e-12
            if lon < (xj - xi) * (lat - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def point_in_geometry(lat: float, lon: float, geom: dict | None) -> bool:
    """Exact point-in-polygon against a CAP footprint.

    Honours interior rings: a point inside a hole is outside the polygon.
    Returns False for anything unusable, so a malformed footprint falls back
    to the caller's coarse test rather than silently matching everyone.
    """
    if not geom:
        return False
    polys = (geom.get("coordinates") or []) if geom.get("type") == "MultiPolygon"         else [geom.get("coordinates") or []]
    for poly in polys:
        if not poly:
            continue
        if not _ring_contains(lat, lon, poly[0]):
            continue
        in_hole = any(_ring_contains(lat, lon, hole) for hole in poly[1:])
        if not in_hole:
            return True
    return False


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def distance_to_geometry_km(lat: float, lon: float, geom: dict | None) -> float | None:
    """How far a point sits outside a CAP footprint. 0.0 when inside.

    Exact containment alone is too strict for dissemination: a subscriber who
    asked to hear about hazards within 60 km of them still wants a warning
    whose polygon stops 3 km short of their village. Containment answers
    "where is the hazard"; the subscriber's radius answers "how far away do I
    still care", and both belong in the decision.

    Distance is approximated as the smallest great-circle distance to any
    vertex of the footprint. CAP polygons here are dense -- hundreds of points
    along a district boundary -- so vertex distance tracks true edge distance
    closely, and it never under-reports in a way that would suppress a
    warning: a nearer edge midpoint only makes the real distance smaller,
    which can only add recipients, not drop them.

    Returns None when there is no usable footprint, so the caller falls back
    to the disc test rather than treating "unknown" as "far away".
    """
    if not geom:
        return None
    if point_in_geometry(lat, lon, geom):
        return 0.0
    polys = (geom.get("coordinates") or []) if geom.get("type") == "MultiPolygon"         else [geom.get("coordinates") or []]
    best: float | None = None
    for poly in polys:
        for ring in poly or []:
            for pt in ring or []:
                try:
                    plon, plat = float(pt[0]), float(pt[1])
                except (TypeError, ValueError, IndexError):
                    continue
                d = _haversine_km(lat, lon, plat, plon)
                if best is None or d < best:
                    best = d
    return best


async def alerts_for_point(lat: float, lon: float,
                           radius_km: float = 25.0) -> list[AlertEvent]:
    """Alerts affecting one point, carrying their exact CAP footprint.

    This is the precise path. NDMA filters server-side against the real
    polygon, and returns `area_json` so the match can also be verified here.
    Cached per rounded point + radius for the poll interval, so a hundred
    subscribers in one town cost one upstream call.
    """
    s = get_settings()
    r = max(1, min(1000, int(round(radius_km))))
    key_pt = (round(lat, 2), round(lon, 2), r)
    ck = upstream_cache.key("sachet:point", *key_pt)
    rows = upstream_cache.get(ck)
    if rows is None:
        url = s.sachet_location_alerts_url
        headers = {"User-Agent": getattr(s, "sachet_user_agent", "WeatherGPT/1.0"),
                   "content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=s.sachet_timeout_s) as client:
                resp = await client.post(
                    url, headers=headers,
                    params={"lat": lat, "long": lon, "radius": str(r)})
                resp.raise_for_status()
                payload = resp.json()
            rows = payload.get("alerts") or [] if isinstance(payload, dict) else []
        except Exception as exc:                   # noqa: BLE001
            log.warning("SACHET point query failed (%s,%s r=%s): %r",
                        lat, lon, r, exc)
            return []
        upstream_cache.set(ck, rows, ttl=max(60, s.sachet_poll_seconds))

    out: list[AlertEvent] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ev = to_event(row)
        if ev is not None:
            out.append(ev)
    return out


def to_event(row: dict) -> AlertEvent | None:
    """Normalise one SACHET row into the internal AlertEvent contract.

    Returns None for a row that cannot be placed on the map -- without a
    centroid the geofence matcher has nothing to match, and dispatching an
    unplaceable alert to every subscriber is worse than dropping it.
    """
    coords = parse_centroid(row.get("centroid"))
    if coords is None:
        return None
    lat, lon = coords

    effective = parse_ts(row.get("effective_start_time")) or datetime.now(timezone.utc)
    expires = parse_ts(row.get("effective_end_time"))

    originator = str(row.get("alert_source") or "").strip()
    source = f"{originator} via NDMA SACHET" if originator else "NDMA SACHET"
    kind = str(row.get("disaster_type") or "alert").strip() or "alert"

    ident = row.get("identifier")
    alert_id = str(ident) if ident not in (None, "") else uuid.uuid4().hex[:8]

    return AlertEvent(
        id=alert_id,
        headline=headline_of(row),
        severity=severity_of(row),
        area=str(row.get("area_description") or "India").strip() or "India",
        lat=lat,
        lon=lon,
        radius_km=radius_km(row.get("area_covered")),
        geometry=parse_area_json(row.get("area_json")),
        effective=effective,
        expires=expires,
        provenance=Provenance(
            source=source,
            product=f"CAP alert: {kind}",
            issued_at=effective,
            valid_until=expires,
            url="https://sachet.ndma.gov.in/",
            # The originating agencies are national and state authorities.
            # This is the flag providers/nwp.py is never allowed to set.
            authoritative=True,
        ),
    )


async def fetch_raw() -> list[dict]:
    """One polite GET, cached for the poll interval. Never raises: a feed
    outage must degrade dissemination, not take down the service."""
    s = get_settings()
    url = s.sachet_alerts_url
    ck = upstream_cache.key(url)
    if (hit := upstream_cache.get(ck)) is not None:
        return hit
    headers = {"User-Agent": getattr(s, "sachet_user_agent", "WeatherGPT/1.0"),
               "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=s.sachet_timeout_s) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:                       # noqa: BLE001 - see docstring
        global LAST_ERROR
        LAST_ERROR = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("SACHET fetch failed: %r", exc)
        stale = upstream_cache.get(ck + ":stale")
        return stale if isinstance(stale, list) else []
    if not isinstance(data, list):
        log.warning("SACHET returned %s, expected a list", type(data).__name__)
        return []
    LAST_ERROR = None
    upstream_cache.set(ck, data, ttl=max(60, s.sachet_poll_seconds))
    upstream_cache.set(ck + ":stale", data, ttl=21600)
    return data


async def recent_events(min_severity: Severity = Severity.YELLOW) -> list[AlertEvent]:
    """Fetch and normalise. Rows that cannot be placed are dropped, and
    anything below `min_severity` is filtered out here rather than in the
    matcher, so the poller's counts mean what they say."""
    order = [Severity.NONE, Severity.GREEN, Severity.YELLOW,
             Severity.ORANGE, Severity.RED]
    floor = order.index(min_severity)
    out: list[AlertEvent] = []
    for row in await fetch_raw():
        if not isinstance(row, dict):
            continue
        ev = to_event(row)
        if ev is None or order.index(ev.severity) < floor:
            continue
        out.append(ev)
    return out


async def status() -> dict:
    """Cheap summary for /api/health -- reads the cache, never the network."""
    s = get_settings()
    ck = upstream_cache.key(s.sachet_alerts_url)
    cached = upstream_cache.get(ck)
    return {
        "feed": "NDMA SACHET (CAP)",
        "keyless": True,
        "cached_alerts": len(cached) if isinstance(cached, list) else 0,
        "poll_seconds": s.sachet_poll_seconds,
        "enabled": s.enable_sachet_poll,
        "last_error": LAST_ERROR,
    }
