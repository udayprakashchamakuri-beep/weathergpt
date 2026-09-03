"""Smoke + grounding tests.

Run:  python3 tests/test_smoke.py          (server must be on :8000)

These are the checks that matter for a met-service deployment:
  1. every intent routes correctly
  2. no numeric claim is ever emitted without a provenance record
  3. the LLM-rewrite guard rejects any numeral change
  4. sector thresholds fire where IMD says they should
  5. a geofenced alert reaches exactly the right subscribers
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

BASE = os.getenv("WEATHERGPT_BASE", "http://localhost:8000")
PASS, FAIL = 0, 0

# The dissemination endpoints are gated by X-Demo-Token whenever the server
# has DEMO_TOKEN set (see backend/app/security.py). Locally the gate is open
# and this is an empty header set. Export the same DEMO_TOKEN as the server to
# run this suite against a gated deployment.
# Note the suite also drives /api/chat far faster than a human, so start the
# server with RATE_LIMIT_CHAT_PER_MIN=0.
_TOKEN = os.getenv("DEMO_TOKEN", "")
_AUTH = {"X-Demo-Token": _TOKEN} if _TOKEN else {}


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def post(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else b""
    req = urllib.request.Request(BASE + path, data=data, method="POST",
                                 headers={"content-type": "application/json",
                                          **_AUTH})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


def get(path: str):
    req = urllib.request.Request(BASE + path, headers=_AUTH)
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)


# ------------------------------------------------------------- 1. routing
print("\n[1] intent routing")
ROUTING = [
    ("Weather in Hyderabad right now", "current_weather", "Hyderabad"),
    ("kal Guntur me barish hogi kya?", "forecast", "Guntur"),
    ("repu Warangal lo vaana padutunda?", "forecast", "Warangal"),
    ("Any warning for Puri?", "warnings", "Puri"),
    ("Should I spray pesticide on my field in Nizamabad?", "advisory", "Nizamabad"),
    ("Air quality in Delhi", "air_quality", "Delhi"),
    ("How has rainfall in Pune changed over 30 years?", "climate_trend", "Pune"),
]
for q, want_intent, want_place in ROUTING:
    d = post("/api/chat", {"message": q})
    got_place = (d.get("place") or {}).get("name", "")
    check(f"{q[:44]!r:48} -> {want_intent}",
          d["intent"] == want_intent and want_place.lower() in got_place.lower(),
          f"got {d['intent']} / {got_place}")

# ------------------------------------------------- 2. grounding contract
print("\n[2] grounding: every fact carries provenance")
d = post("/api/chat", {"message": "Weather in Chennai right now"})
check("facts present", len(d["facts"]) > 0)
check("all facts have a source",
      all(f["provenance"]["source"] for f in d["facts"]))
check("all facts have a product",
      all(f["provenance"]["product"] for f in d["facts"]))
check("response lists its sources", len(d["sources"]) > 0)

# The provenance record must not INVENT an issue time -- that is the property
# under test, and it is unchanged. What changed is that it is now enforced per
# source, because the sources genuinely differ:
#
#   Open-Meteo   publishes no model run time for the daily forecast, so the
#                only honest value is null. Anything else is fabricated.
#   MET Norway   publishes a real run time in properties.meta.updated_at, so
#                carrying it is MORE provenance, not less.
#   OpenWeather  /data/2.5/forecast publishes no run time -> null.
#
# The original assertion was "issued_at is always null", which was correct
# while Open-Meteo was the only forecast source and became wrong the moment a
# source that publishes a run time was added. Asserting null unconditionally
# would now punish a provider for being more transparent, so the check tests
# the actual invariant: an issue time is never request time.
fc_src = [s_ for s_ in d["sources"] if "forecast" in s_["product"]]
check("a forecast source is present", bool(fc_src))
_fsrc = fc_src[0]["source"] if fc_src else ""
_issued = fc_src[0]["issued_at"] if fc_src else "no forecast source"

if "Open-Meteo" in _fsrc or "OpenWeather" in _fsrc:
    check("forecast provenance does not fabricate an issue time",
          _issued is None, f"{_fsrc} got {_issued}")
else:
    # A source that does publish one may carry it, but it must be the
    # source's own timestamp -- never "now" stamped on at request time.
    from datetime import datetime as _d, timezone as _tz
    _ok = _issued is None
    if _issued is not None:
        _t = _d.fromisoformat(str(_issued).replace("Z", "+00:00"))
        if _t.tzinfo is None:
            _t = _t.replace(tzinfo=_tz.utc)
        _age = abs((_d.now(_tz.utc) - _t).total_seconds())
        # Stamped within 30s of the request is indistinguishable from now().
        _ok = _age > 30
    check("forecast issue time is the source's own, not request time",
          _ok, f"{_fsrc} got {_issued}")

cur_src = [s_ for s_ in d["sources"] if "analysis" in s_["product"]]
check("analysis provenance carries the source's own valid time",
      bool(cur_src) and cur_src[0]["issued_at"] is not None)

nums_in_answer = {t for t in d["answer_en"].replace("°C", " ").replace("%", " ")
                  .replace(",", " ").split() if t.replace(".", "").isdigit()}


def rendered_forms(v):
    """Every string the renderer could legitimately print for a fact value.

    tools._fmt() formats to 0 or 1 decimal places, so a wind of 93.9 km/h is
    rendered "94" in the answer. The previous check compared the rendered
    string against the raw value with a substring test, so "94" vs "93.9"
    looked like an ungrounded numeral. That is a false positive on the check
    that guards the project's headline claim -- the most expensive kind,
    because it trains everyone to ignore the one test that must be believed.
    """
    forms = {str(v)}
    if isinstance(v, bool):
        return forms
    if isinstance(v, (int, float)):
        forms |= {f"{v:.0f}", f"{v:.1f}"}
    return forms


fact_forms = set()
for f in d["facts"]:
    fact_forms |= rendered_forms(f["value"])
# Exact match against the rendered forms, not a substring test: substring
# matching also passes "1" against a fact of "1004.7", which is the opposite
# error -- an orphan that slips through.
orphans = {n for n in nums_in_answer if n not in fact_forms}
check("no orphan numerals in the answer", not orphans,
      f"orphans={orphans} facts={sorted(fact_forms)}")

# --------------------------------------------- 3. LLM rewrite guardrail
print("\n[3] numeral-preservation guard")
from app.nlu import numerals_preserved            # noqa: E402

check("identical numerals accepted",
      numerals_preserved("Rain 64.5 mm, wind 40 km/h",
                         "Wind is 40 km/h with 64.5 mm of rain"))
check("altered numeral rejected",
      not numerals_preserved("Rain 64.5 mm", "Rain 6.45 mm"))
check("invented numeral rejected",
      not numerals_preserved("Rain 64.5 mm", "Rain 64.5 mm, gusts 90 km/h"))
check("dropped numeral rejected",
      not numerals_preserved("Rain 64.5 mm, gusts 90 km/h", "Rain 64.5 mm"))

# -------------------------------------------------- 4. advisory thresholds
print("\n[4] IMD impact thresholds")
from app.advisory import build, classify           # noqa: E402
from app.schemas import Persona, Severity          # noqa: E402

def day(**kw):
    base = {"date": "2026-09-01", "rain_mm": 0, "gust_max_kmh": 10,
            "wind_max_kmh": 8, "tmax_c": 30, "tmin_c": 24, "weather_code": 1,
            "humidity_max_pct": 60, "rain_prob_pct": 0}
    base.update(kw)
    return base

check("heavy rain 65 mm -> orange", classify(day(rain_mm=65))[0] == Severity.ORANGE)
check("very heavy 120 mm -> red", classify(day(rain_mm=120))[0] == Severity.RED)
check("64 mm stays yellow", classify(day(rain_mm=64))[0] == Severity.YELLOW)
check("gust 90 km/h -> red", classify(day(gust_max_kmh=90))[0] == Severity.RED)
check("gust 63 km/h -> orange (34 kt)", classify(day(gust_max_kmh=63))[0] == Severity.ORANGE)
check("tmax 46 C -> red", classify(day(tmax_c=46))[0] == Severity.RED)
check("calm day -> green", classify(day())[0] == Severity.GREEN)

gale = build(Persona.FISHERMAN, [day(gust_max_kmh=95)])
check("fisherman gale -> no-go", gale.severity == Severity.RED
      and "DO NOT PUT TO SEA" in gale.actions[0])

wet = build(Persona.FARMER, [day(rain_mm=20), day(rain_mm=0, wind_max_kmh=8),
                             day(rain_mm=0, wind_max_kmh=6)])
check("farmer wet day -> do not spray",
      any("Do not spray" in a for a in wet.actions))
check("farmer offered a next spray window",
      any("Next suitable spray window" in a for a in wet.actions))

# --------------------------------------------------- 5. alert dissemination
print("\n[5] geofenced dissemination")
s1 = post("/api/alerts/subscribe?address=test-near&place=Puri&channel=sms"
          "&lang=te&persona=farmer&min_severity=yellow")
s2 = post("/api/alerts/subscribe?address=test-far&place=Jaisalmer&channel=sms"
          "&lang=hi&persona=farmer&min_severity=yellow")
s3 = post("/api/alerts/subscribe?address=test-redonly&place=Puri&channel=ivr"
          "&lang=ta&persona=fisherman&min_severity=red")

res = post("/api/alerts/simulate?place=Puri&severity=orange")
check("simulated alerts are labelled as simulated", res.get("simulated") is True)
check("simulated alert provenance names the demo feed",
      "Simulated" in res["alert"]["provenance"]["source"])
addrs = {r["address"] for r in res["delivered"]}
check("nearby subscriber matched", "test-near" in addrs)
check("distant subscriber not matched", "test-far" not in addrs)
check("red-only subscriber skipped on orange", "test-redonly" not in addrs)

res_red = post("/api/alerts/simulate?place=Puri&severity=red")
addrs_red = {r["address"] for r in res_red["delivered"]}
check("red-only subscriber matched on red", "test-redonly" in addrs_red)
telugu = next((r for r in res_red["delivered"] if r["address"] == "test-near"), None)
check("message rendered in subscriber's language",
      bool(telugu) and any("ఀ" <= c <= "౿" for c in telugu["message"]),
      "expected Telugu script")

for s in (s1, s2, s3):
    urllib.request.urlopen(urllib.request.Request(
        BASE + "/api/alerts/subscribe/" + s["subscription"]["id"], method="DELETE"))

# ------------------------------------------------------------ 6. latency
print("\n[6] cache and latency")
q = {"message": "Weather in Bengaluru right now"}
first = post("/api/chat", q)
second = post("/api/chat", q)
check("second identical query served from cache", second["cached"] is True)
check("cached response under 60 ms", second["latency_ms"] < 60,
      f"{second['latency_ms']} ms")
check("cold response under 8 s", first["latency_ms"] < 8000,
      f"{first['latency_ms']} ms")

# ------------------------------------- 7. OpenWeather unit conversions
# These run in-process against synthetic payloads: no key, no network, no
# running server. They assert on the CONVERSION, not the plumbing, because
# every one of them is a silent under-warning if it is wrong. advisory.py
# compares against km/h thresholds and will accept a smaller number without
# complaint -- a raw m/s gust simply never trips anything.
print("")
print("[7] OpenWeather unit conversions")

import asyncio                                          # noqa: E402
from datetime import datetime as _dt, timezone as _tz   # noqa: E402

from app import advisory as _adv                        # noqa: E402
from app.providers import openweather as ow             # noqa: E402
from app.schemas import Severity as _Sev                # noqa: E402

# --- 1. wind: metres per second -> km/h ------------------------------------
check("10 m/s renders as 36 km/h", ow._to_kmh(10) == 36.0, f"got {ow._to_kmh(10)}")
check("0 m/s stays 0 km/h", ow._to_kmh(0) == 0.0)
check("absent wind stays None, not 0", ow._to_kmh(None) is None)

# --- 2. pop: 0-1 fraction -> percentage ------------------------------------
check("pop 0.35 renders as 35%", ow._pop_to_pct(0.35) == 35,
      f"got {ow._pop_to_pct(0.35)}")
check("pop 1.0 renders as 100%", ow._pop_to_pct(1.0) == 100)
check("pop 0 renders as 0%, not None", ow._pop_to_pct(0) == 0)
check("absent pop stays None", ow._pop_to_pct(None) is None)

# --- 3. absent rain means zero, not missing --------------------------------
check("absent rain block counts as 0.0 mm", ow._rain_mm(None, "3h") == 0.0)
check("rain 3h of 1.2 reads as 1.2 mm", ow._rain_mm({"3h": 1.2}, "3h") == 1.2)
check("rain block without the window key is 0.0",
      ow._rain_mm({"1h": 9.9}, "3h") == 0.0)


def _slot(dt_utc, temp, gust_ms, pop, rain_3h=None, cid=800):
    """One 3-hourly OpenWeather forecast entry, in their exact shape."""
    epoch = int(_dt.fromisoformat(dt_utc).replace(tzinfo=_tz.utc).timestamp())
    slot = {"dt": epoch, "main": {"temp": temp, "humidity": 70},
            "wind": {"speed": gust_ms / 2, "gust": gust_ms},
            "pop": pop, "weather": [{"id": cid, "description": "x"}]}
    if rain_3h is not None:                # omitted entirely when dry
        slot["rain"] = {"3h": rain_3h}
    return slot


async def _daily(slots):
    """Run the real aggregation over a synthetic payload."""
    async def fake_get(url, lat, lon, ttl):
        return {"list": slots, "city": {"timezone": 19800}}
    real, ow._get = ow._get, fake_get
    try:
        return await ow.forecast(13.0, 80.2, days=5)
    finally:
        ow._get = real


# --- the assertion that matters most: a real gust trips a real threshold ---
# WIND_SMALL_CRAFT is 34 kt = 62.968 km/h, i.e. 17.4911 m/s -- which is 34 kt
# expressed in m/s. So 17.4 m/s (62.64 km/h) is genuinely BELOW it and 17.5
# m/s (63.0 km/h) is above. Both sides are asserted so the boundary is pinned.
below = asyncio.run(_daily([_slot("2026-09-02T06:00:00", 30.0, 17.4, 0.35)]))
above = asyncio.run(_daily([_slot("2026-09-02T06:00:00", 30.0, 17.5, 0.35)]))
check("17.4 m/s gust converts to 62.6 km/h",
      below["days"][0]["gust_max_kmh"] == 62.6,
      f"got {below['days'][0]['gust_max_kmh']}")
check("17.5 m/s gust converts to 63.0 km/h",
      above["days"][0]["gust_max_kmh"] == 63.0,
      f"got {above['days'][0]['gust_max_kmh']}")
check("62.6 km/h stays below the 34 kt small-craft threshold",
      below["days"][0]["gust_max_kmh"] < _adv.WIND_SMALL_CRAFT)
check("63.0 km/h trips WIND_SMALL_CRAFT (34 kt)",
      above["days"][0]["gust_max_kmh"] >= _adv.WIND_SMALL_CRAFT)
check("a tripping gust drives the fisherman advisory to ORANGE or worse",
      _adv.fisherman(above["days"]).severity in (_Sev.ORANGE, _Sev.RED),
      f"got {_adv.fisherman(above['days']).severity}")
# The exact regression this guards against: forgetting the 3.6.
check("unconverted 17.5 m/s would NOT trip the threshold (why 3.6 matters)",
      17.5 < _adv.WIND_SMALL_CRAFT)

check("pop 0.35 survives aggregation as 35%",
      above["days"][0]["rain_prob_pct"] == 35,
      f"got {above['days'][0]['rain_prob_pct']}")

# --- the gust degradation notice must switch OFF for OpenWeather -----------
# tools._gust_degradation() warns that wind thresholds are being evaluated on
# sustained wind because the source published no gusts. OpenWeather does
# publish them, so that warning must disappear -- otherwise the demo cries
# wolf on every answer and the notice stops meaning anything. Asserted in both
# directions so neither state can silently flip.
from app import tools as _tools                         # noqa: E402

_ow_days = above["days"]                                # has gust_max_kmh
_ow_cur = {"wind_gust_kmh": 40.0}
_prov_ow = ow._prov("5-day forecast (3-hourly, aggregated)")
check("no gust caveat when the source publishes gusts (OpenWeather)",
      _tools._gust_degradation(_ow_cur, _ow_days, _prov_ow) == [],
      f"got {_tools._gust_degradation(_ow_cur, _ow_days, _prov_ow)}")

_no_gust_days = [dict(d, gust_max_kmh=None) for d in _ow_days]
check("gust caveat still fires when the source publishes none (MET Norway)",
      len(_tools._gust_degradation({"wind_gust_kmh": None},
                                   _no_gust_days, _prov_ow)) == 1)

# --- rain summed across 3-hourly slots, with dry slots absent --------------
mixed = asyncio.run(_daily([
    _slot("2026-09-02T00:00:00", 26.0, 5.0, 0.1),                # dry: no key
    _slot("2026-09-02T03:00:00", 27.0, 5.0, 0.6, rain_3h=1.2),
    _slot("2026-09-02T06:00:00", 31.0, 5.0, 0.6, rain_3h=2.3),
]))
check("3-hourly rain sums across the day, absent treated as 0.0",
      mixed["days"][0]["rain_mm"] == 3.5, f"got {mixed['days'][0]['rain_mm']}")
check("daily max/min come from the 3-hourly slots",
      (mixed["days"][0]["tmax_c"], mixed["days"][0]["tmin_c"]) == (31.0, 26.0),
      f"got {mixed['days'][0]['tmax_c']}/{mixed['days'][0]['tmin_c']}")

# --- bucketing is Asia/Kolkata, not UTC ------------------------------------
# 19:00Z on 2 Sep is 00:30 IST on 3 Sep. Bucketed in UTC it lands on the 2nd
# and corrupts that day's maximum; bucketed in IST it correctly starts the 3rd.
crossing = asyncio.run(_daily([
    _slot("2026-09-02T06:00:00", 30.0, 5.0, 0.1),
    _slot("2026-09-02T19:00:00", 40.0, 5.0, 0.1),
]))
_dates = [d["date"] for d in crossing["days"]]
check("19:00Z buckets into the next IST day, not the same UTC day",
      _dates == ["2026-09-02", "2026-09-03"], f"got {_dates}")
check("the late-evening slot does not corrupt the earlier day's maximum",
      crossing["days"][0]["tmax_c"] == 30.0,
      f"got {crossing['days'][0]['tmax_c']}")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
