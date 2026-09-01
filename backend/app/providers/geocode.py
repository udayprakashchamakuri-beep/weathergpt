"""Location resolution.

Order: bundled Indian gazetteer (0 ms, offline, covers ~120 places incl. all
state capitals and major agri/coastal districts) -> Open-Meteo geocoder ->
device GPS supplied by the client.

In production this is replaced by the IMD station/district master list joined
against a PostGIS boundary layer, so a query resolves to the *administrative
unit IMD issues warnings for* (district object id), not just a lat/lon.
"""
from __future__ import annotations

import re
import unicodedata

import httpx

from ..cache import TTLCache, upstream_cache
from ..config import get_settings
from ..schemas import Place

# name -> (lat, lon, state)  |  bundled offline gazetteer
GAZETTEER: dict[str, tuple[float, float, str]] = {
    "hyderabad": (17.3850, 78.4867, "Telangana"),
    "secunderabad": (17.4399, 78.4983, "Telangana"),
    "warangal": (17.9689, 79.5941, "Telangana"),
    "nizamabad": (18.6725, 78.0941, "Telangana"),
    "karimnagar": (18.4386, 79.1288, "Telangana"),
    "khammam": (17.2473, 80.1514, "Telangana"),
    "adilabad": (19.6640, 78.5320, "Telangana"),
    "mahbubnagar": (16.7488, 77.9854, "Telangana"),
    "nalgonda": (17.0575, 79.2684, "Telangana"),
    "vijayawada": (16.5062, 80.6480, "Andhra Pradesh"),
    "visakhapatnam": (17.6868, 83.2185, "Andhra Pradesh"),
    "vizag": (17.6868, 83.2185, "Andhra Pradesh"),
    "guntur": (16.3067, 80.4365, "Andhra Pradesh"),
    "tirupati": (13.6288, 79.4192, "Andhra Pradesh"),
    "kakinada": (16.9891, 82.2475, "Andhra Pradesh"),
    "nellore": (14.4426, 79.9865, "Andhra Pradesh"),
    "kurnool": (15.8281, 78.0373, "Andhra Pradesh"),
    "rajahmundry": (17.0005, 81.8040, "Andhra Pradesh"),
    "delhi": (28.6139, 77.2090, "Delhi"),
    "new delhi": (28.6139, 77.2090, "Delhi"),
    "gurugram": (28.4595, 77.0266, "Haryana"),
    "noida": (28.5355, 77.3910, "Uttar Pradesh"),
    "mumbai": (19.0760, 72.8777, "Maharashtra"),
    "pune": (18.5204, 73.8567, "Maharashtra"),
    "nagpur": (21.1458, 79.0882, "Maharashtra"),
    "nashik": (19.9975, 73.7898, "Maharashtra"),
    "aurangabad": (19.8762, 75.3433, "Maharashtra"),
    "solapur": (17.6599, 75.9064, "Maharashtra"),
    "ratnagiri": (16.9902, 73.3120, "Maharashtra"),
    "bengaluru": (12.9716, 77.5946, "Karnataka"),
    "bangalore": (12.9716, 77.5946, "Karnataka"),
    "mysuru": (12.2958, 76.6394, "Karnataka"),
    "mangaluru": (12.9141, 74.8560, "Karnataka"),
    "hubballi": (15.3647, 75.1240, "Karnataka"),
    "belagavi": (15.8497, 74.4977, "Karnataka"),
    "chennai": (13.0827, 80.2707, "Tamil Nadu"),
    "coimbatore": (11.0168, 76.9558, "Tamil Nadu"),
    "madurai": (9.9252, 78.1198, "Tamil Nadu"),
    "tiruchirappalli": (10.7905, 78.7047, "Tamil Nadu"),
    "thoothukudi": (8.7642, 78.1348, "Tamil Nadu"),
    "nagapattinam": (10.7660, 79.8424, "Tamil Nadu"),
    "rameswaram": (9.2876, 79.3129, "Tamil Nadu"),
    "kanyakumari": (8.0883, 77.5385, "Tamil Nadu"),
    "kochi": (9.9312, 76.2673, "Kerala"),
    "cochin": (9.9312, 76.2673, "Kerala"),
    "thiruvananthapuram": (8.5241, 76.9366, "Kerala"),
    "kozhikode": (11.2588, 75.7804, "Kerala"),
    "thrissur": (10.5276, 76.2144, "Kerala"),
    "wayanad": (11.6854, 76.1320, "Kerala"),
    "alappuzha": (9.4981, 76.3388, "Kerala"),
    "kolkata": (22.5726, 88.3639, "West Bengal"),
    "howrah": (22.5958, 88.2636, "West Bengal"),
    "digha": (21.6270, 87.5090, "West Bengal"),
    "siliguri": (26.7271, 88.3953, "West Bengal"),
    "darjeeling": (27.0360, 88.2627, "West Bengal"),
    "bhubaneswar": (20.2961, 85.8245, "Odisha"),
    "puri": (19.8135, 85.8312, "Odisha"),
    "cuttack": (20.4625, 85.8830, "Odisha"),
    "paradip": (20.3160, 86.6110, "Odisha"),
    "gopalpur": (19.2667, 84.9167, "Odisha"),
    "balasore": (21.4942, 86.9336, "Odisha"),
    "patna": (25.5941, 85.1376, "Bihar"),
    "gaya": (24.7955, 85.0002, "Bihar"),
    "muzaffarpur": (26.1209, 85.3647, "Bihar"),
    "ranchi": (23.3441, 85.3096, "Jharkhand"),
    "jamshedpur": (22.8046, 86.2029, "Jharkhand"),
    "lucknow": (26.8467, 80.9462, "Uttar Pradesh"),
    "kanpur": (26.4499, 80.3319, "Uttar Pradesh"),
    "varanasi": (25.3176, 82.9739, "Uttar Pradesh"),
    "prayagraj": (25.4358, 81.8463, "Uttar Pradesh"),
    "agra": (27.1767, 78.0081, "Uttar Pradesh"),
    "gorakhpur": (26.7606, 83.3732, "Uttar Pradesh"),
    "meerut": (28.9845, 77.7064, "Uttar Pradesh"),
    "jaipur": (26.9124, 75.7873, "Rajasthan"),
    "jodhpur": (26.2389, 73.0243, "Rajasthan"),
    "udaipur": (24.5854, 73.7125, "Rajasthan"),
    "bikaner": (28.0229, 73.3119, "Rajasthan"),
    "jaisalmer": (26.9157, 70.9083, "Rajasthan"),
    "kota": (25.2138, 75.8648, "Rajasthan"),
    "ahmedabad": (23.0225, 72.5714, "Gujarat"),
    "surat": (21.1702, 72.8311, "Gujarat"),
    "rajkot": (22.3039, 70.8022, "Gujarat"),
    "vadodara": (22.3072, 73.1812, "Gujarat"),
    "bhuj": (23.2419, 69.6669, "Gujarat"),
    "porbandar": (21.6417, 69.6293, "Gujarat"),
    "dwarka": (22.2394, 68.9678, "Gujarat"),
    "veraval": (20.9077, 70.3670, "Gujarat"),
    "bhopal": (23.2599, 77.4126, "Madhya Pradesh"),
    "indore": (22.7196, 75.8577, "Madhya Pradesh"),
    "gwalior": (26.2183, 78.1828, "Madhya Pradesh"),
    "jabalpur": (23.1815, 79.9864, "Madhya Pradesh"),
    "raipur": (21.2514, 81.6296, "Chhattisgarh"),
    "bilaspur": (22.0797, 82.1409, "Chhattisgarh"),
    "chandigarh": (30.7333, 76.7794, "Chandigarh"),
    "ludhiana": (30.9010, 75.8573, "Punjab"),
    "amritsar": (31.6340, 74.8723, "Punjab"),
    "jalandhar": (31.3260, 75.5762, "Punjab"),
    "bathinda": (30.2110, 74.9455, "Punjab"),
    "hisar": (29.1492, 75.7217, "Haryana"),
    "karnal": (29.6857, 76.9905, "Haryana"),
    "shimla": (31.1048, 77.1734, "Himachal Pradesh"),
    "manali": (32.2432, 77.1892, "Himachal Pradesh"),
    "dharamshala": (32.2190, 76.3234, "Himachal Pradesh"),
    "dehradun": (30.3165, 78.0322, "Uttarakhand"),
    "nainital": (29.3803, 79.4636, "Uttarakhand"),
    "joshimath": (30.5550, 79.5646, "Uttarakhand"),
    "srinagar": (34.0837, 74.7973, "Jammu & Kashmir"),
    "jammu": (32.7266, 74.8570, "Jammu & Kashmir"),
    "leh": (34.1526, 77.5771, "Ladakh"),
    "guwahati": (26.1445, 91.7362, "Assam"),
    "dibrugarh": (27.4728, 94.9120, "Assam"),
    "silchar": (24.8333, 92.7789, "Assam"),
    "shillong": (25.5788, 91.8933, "Meghalaya"),
    "cherrapunji": (25.2702, 91.7323, "Meghalaya"),
    "imphal": (24.8170, 93.9368, "Manipur"),
    "aizawl": (23.7271, 92.7176, "Mizoram"),
    "agartala": (23.8315, 91.2868, "Tripura"),
    "kohima": (25.6751, 94.1086, "Nagaland"),
    "itanagar": (27.0844, 93.6053, "Arunachal Pradesh"),
    "gangtok": (27.3389, 88.6065, "Sikkim"),
    "panaji": (15.4909, 73.8278, "Goa"),
    "goa": (15.2993, 74.1240, "Goa"),
    "port blair": (11.6234, 92.7265, "Andaman & Nicobar"),
    "kavaratti": (10.5669, 72.6420, "Lakshadweep"),
    "puducherry": (11.9416, 79.8083, "Puducherry"),
}

_geo_cache = TTLCache(ttl=86400)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z ]", "", text.lower()).strip()


def lookup_local(name: str) -> Place | None:
    key = _norm(name)
    if key in GAZETTEER:
        lat, lon, state = GAZETTEER[key]
        return Place(name=name.title(), admin1=state, lat=lat, lon=lon,
                     source="bundled-gazetteer")
    # loose contains match, longest first so "new delhi" beats "delhi"
    for cand in sorted(GAZETTEER, key=len, reverse=True):
        if cand in key or key in cand:
            lat, lon, state = GAZETTEER[cand]
            return Place(name=cand.title(), admin1=state, lat=lat, lon=lon,
                         source="bundled-gazetteer")
    return None


async def resolve(name: str) -> Place | None:
    """Gazetteer first, then the network geocoder."""
    if not name:
        return None
    local = lookup_local(name)
    if local:
        return local

    ck = _geo_cache.key("geo", _norm(name))
    if (hit := _geo_cache.get(ck)) is not None:
        return Place(**hit) if hit else None

    s = get_settings()
    try:
        async with httpx.AsyncClient(timeout=s.http_timeout) as client:
            r = await client.get(
                s.openmeteo_geocode_base,
                params={"name": name, "count": 1, "language": "en", "format": "json"},
            )
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
    except Exception:
        _geo_cache.set(ck, None, ttl=120)
        return None

    if not results:
        _geo_cache.set(ck, None, ttl=3600)
        return None

    top = results[0]
    place = Place(
        name=top.get("name", name),
        admin1=top.get("admin1"),
        country=top.get("country", "India"),
        lat=top["latitude"],
        lon=top["longitude"],
        source="open-meteo-geocoder",
    )
    _geo_cache.set(ck, place.model_dump())
    return place


async def reverse(lat: float, lon: float) -> Place:
    """Nearest gazetteer entry -- good enough to name a GPS fix in the demo."""
    best, best_d = None, 1e9
    for name, (glat, glon, state) in GAZETTEER.items():
        d = (glat - lat) ** 2 + (glon - lon) ** 2
        if d < best_d:
            best, best_d = (name, glat, glon, state), d
    if best and best_d < 4.0:      # ~2 degrees
        name, glat, glon, state = best
        return Place(name=f"near {name.title()}", admin1=state, lat=lat, lon=lon,
                     source="reverse-gazetteer")
    return Place(name=f"{lat:.2f}, {lon:.2f}", lat=lat, lon=lon, source="gps")
