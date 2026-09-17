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

# Advice is for the day asked about, matched by date. "Safe to go fishing
# tomorrow?" used to be answered from today's gusts, and at 22:00 "today" was
# built from one remaining 3-hourly slot.
from datetime import datetime as _dtt               # noqa: E402
from app import tools as _t                         # noqa: E402
_night = _dtt(2026, 9, 17, 22, 0, tzinfo=_t.IST)
_week = [day(date=f"2026-09-{n}") for n in (17, 18, 19, 20)]
check("evening advisory rolls over to tomorrow",
      _t._days_from(_week, 0, True, _night)[0]["date"] == "2026-09-18")
check("morning advisory stays on today",
      _t._days_from(_week, 0, True, _night.replace(hour=9))[0]["date"] == "2026-09-17")
check("'tomorrow' found by date when days[0] is already tomorrow",
      _t._days_from(_week[1:], 1, False, _night)[0]["date"] == "2026-09-18")
_calm_today_gale_tomorrow = [day(date="2026-09-17", gust_max_kmh=10),
                             day(date="2026-09-18", gust_max_kmh=95)]
check("fisherman asking about tomorrow gets tomorrow's gale",
      build(Persona.FISHERMAN, _t._days_from(_calm_today_gale_tomorrow, 1, True,
                                             _night)).severity == Severity.RED)
check("day named in the answer's language, date beyond tomorrow",
      _t._day_name("2026-09-18", "te", _night) == "రేపు"
      and _t._day_name("2026-09-20", "en", _night) == "on 2026-09-20")
check("farmer action names the day it is for",
      any("tomorrow" in a for a in build(Persona.FARMER, [day(rain_mm=20)],
                                         when="tomorrow").actions))

# Spray windows by the hour. A daily total cannot tell a dry morning from a
# wet afternoon, and the spray needs ~6 dry hours after application.
from app.advisory import spray_window                # noqa: E402
def _st(start, rain=0.0, wind=5.0, hours=3):
    return {"start": start, "hours": hours, "rain_mm": rain, "wind_kmh": wind}
_steps = [_st("2026-09-18T05:30"),                   # before daylight
          _st("2026-09-18T08:30", wind=20),          # drift
          _st("2026-09-18T11:30"),                   # rain lands 14:30-17:30
          _st("2026-09-18T14:30", rain=3.0),
          _st("2026-09-18T17:30"), _st("2026-09-18T20:30"), _st("2026-09-18T23:30"),
          _st("2026-09-19T02:30"), _st("2026-09-19T05:30"),
          _st("2026-09-19T08:30", wind=6), _st("2026-09-19T11:30"),
          _st("2026-09-19T14:30", rain=0.3), _st("2026-09-19T17:30")]
_win = spray_window(_steps, "2026-09-18T00:00")
check("spray window skips drift, and a dry slot with rain within 6 h after",
      _win and _win["start"] == "2026-09-19T08:30", f"got {_win}")
_hourly = [_st(f"2026-09-20T{h:02d}:00", hours=1) for h in range(24)] +           [_st(f"2026-09-21T{h:02d}:00", hours=1) for h in range(6)]
_hw = spray_window(_hourly, "2026-09-20T00:00")
check("hourly steps report the whole daylight run, not its first hour",
      _hw and (_hw["start"], _hw["end"]) == ("2026-09-20T06:00", "2026-09-20T18:00"),
      f"got {_hw}")
check("no window where the forecast ends before the dry span does",
      spray_window(_steps[:-3], "2026-09-19T00:00") is None)
check("window search starts no earlier than not_before",
      spray_window(_steps, "2026-09-19T12:00") is None)
_d19 = [day(date="2026-09-19")]
check("farmer: best spray window named with its hours",
      any("Best spray window tomorrow: 08:30-14:30" in a
          for a in build(Persona.FARMER, _d19, when="tomorrow", steps=_steps,
                         not_before="2026-09-19T00:00").actions))
_d18 = [day(date="2026-09-18")]
check("farmer: no window today -> do not spray, next window given",
      any("Do not spray today" in a and "2026-09-19 08:30-14:30" in a
          for a in build(Persona.FARMER, _d18, steps=_steps,
                         not_before="2026-09-18T00:00").actions))

# Heat stress for people and cattle. Wet-bulb is checked against Open-Meteo's
# own hourly wet_bulb_temperature_2m for the same inputs (Nagpur, 17 Sep 2026).
from app.advisory import thi, wet_bulb_c            # noqa: E402
check("Stull wet-bulb matches Open-Meteo (26.3 C, 84% -> 24.1 C)",
      abs(wet_bulb_c(26.3, 84) - 24.1) < 0.1, f"got {wet_bulb_c(26.3, 84):.2f}")
check("Stull wet-bulb matches Open-Meteo (31.4 C, 61% -> 25.4 C)",
      abs(wet_bulb_c(31.4, 61) - 25.4) < 0.1, f"got {wet_bulb_c(31.4, 61):.2f}")
check("THI rises with humidity at the same temperature", thi(35, 80) > thi(35, 40))

hot_humid = build(Persona.WORKER, [day(wetbulb_max_c=30.6)])
check("worker: wet-bulb above 30 C -> red, stop 11:00-16:00",
      hot_humid.severity == Severity.RED
      and any("11:00 to 16:00" in a for a in hot_humid.actions))
check("worker: no humidity data is declared, not silently ignored",
      any("No humidity data" in a
          for a in build(Persona.WORKER, [day(wetbulb_max_c=None)]).actions))
windy_site = build(Persona.WORKER, [day(wetbulb_max_c=24, gust_max_kmh=62)])
check("worker: gusts 62 km/h stop crane lifts and pours",
      any("crane lifts" in a for a in windy_site.actions)
      and any("concrete pours" in a for a in windy_site.actions))
check("farmer: THI 78 triggers the dairy heat-stress action",
      any("THI" in a for a in build(Persona.FARMER, [day(thi_max=78)]).actions))
check("farmer: THI 70 does not",
      not any("THI" in a for a in build(Persona.FARMER, [day(thi_max=70)]).actions))

_trip = [day(date="2026-09-18", gust_max_kmh=20), day(date="2026-09-19", gust_max_kmh=25),
         day(date="2026-09-20", gust_max_kmh=70)]
check("fisher: 3-day trip judged on its worst day, not its first",
      build(Persona.FISHERMAN, _trip, trip_days=3).severity == Severity.ORANGE
      and "2026-09-20" in build(Persona.FISHERMAN, _trip, trip_days=3).actions[0])
check("fisher: a one-day question is still judged on that day",
      build(Persona.FISHERMAN, _trip, trip_days=1).severity == Severity.GREEN)
check("fisher: trip longer than the forecast says so",
      any("only covers 3 of the 5 days" in a
          for a in build(Persona.FISHERMAN, _trip, trip_days=5).actions))

from datetime import timezone as _utc                # noqa: E402
from app import alerts as _alerts_mod                # noqa: E402
from app.schemas import AlertEvent, Provenance, Subscription  # noqa: E402
def _event(headline):
    return AlertEvent(id="t", headline=headline, severity=Severity.ORANGE,
                      area="Guntur", lat=16.3, lon=80.4, radius_km=25,
                      effective=_dtt(2026, 9, 18, tzinfo=_utc.utc),
                      provenance=Provenance(source="test", product="test"))
def _sub(persona):
    return Subscription(id="s", address="x", lat=16.3, lon=80.4, persona=persona,
                        created_at=_dtt(2026, 9, 17, tzinfo=_utc.utc))
check("farmer alert for hail carries the PMFBY 72-hour deadline",
      "72 hours" in _alerts_mod.render_for(_sub(Persona.FARMER), _event("Hailstorm")))
check("farmer alert for heat does not",
      "72 hours" not in _alerts_mod.render_for(_sub(Persona.FARMER), _event("Heat wave")))
check("fisherman alert for hail does not",
      "72 hours" not in _alerts_mod.render_for(_sub(Persona.FISHERMAN), _event("Hailstorm")))

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
# MET Norway aggregation had no test at all, which is how a heat-stress change
# that shadowed the loop's timestamp with a temperature reached main: the next
# `t >= covered_until` raised on every MET Norway forecast.
from app.providers import metno as _mn               # noqa: E402
def _mn_entry(iso, temp, rh, rain_1h):
    return {"time": iso, "data": {
        "instant": {"details": {"air_temperature": temp, "relative_humidity": rh,
                                "wind_speed": 3.0}},
        "next_1_hours": {"summary": {"symbol_code": "rain"},
                         "details": {"precipitation_amount": rain_1h}}}}
async def _mn_daily(entries):
    async def fake_get(lat, lon):
        return {"properties": {"meta": {"updated_at": "2026-09-17T00:00:00Z"},
                               "timeseries": entries}}
    real, _mn._get = _mn._get, fake_get
    try:
        return await _mn.forecast(13.0, 80.2, days=3)
    finally:
        _mn._get = real
_mn_out = asyncio.run(_mn_daily([_mn_entry("2026-09-18T01:00:00Z", 28.0, 80, 0.4),
                                 _mn_entry("2026-09-18T02:00:00Z", 30.0, 70, 1.1)]))
check("MET Norway aggregates rain across hourly entries",
      _mn_out["days"][0]["rain_mm"] == 1.5, f"got {_mn_out['days'][0]['rain_mm']}")
check("MET Norway wet-bulb max from coincident instant values",
      _mn_out["days"][0]["wetbulb_max_c"]
      == round(max(_adv.wet_bulb_c(28.0, 80), _adv.wet_bulb_c(30.0, 70)), 1),
      f"got {_mn_out['days'][0]['wetbulb_max_c']}")

check("OpenWeather step starts 3 h before dt, in IST (rain.3h ends at dt)",
      mixed["steps"][1]["start"] == "2026-09-02T05:30"
      and mixed["steps"][1]["rain_mm"] == 1.2, f"got {mixed['steps'][1]}")
check("MET Norway step starts at the entry time, in IST",
      _mn_out["steps"][0]["start"] == "2026-09-18T06:30"
      and _mn_out["steps"][0]["hours"] == 1, f"got {_mn_out['steps'][0]}")
check("daily wet-bulb max comes from coincident slot temperature and humidity",
      mixed["days"][0]["wetbulb_max_c"] == round(_adv.wet_bulb_c(31.0, 70), 1),
      f"got {mixed['days'][0]['wetbulb_max_c']}")

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

# ------------------------------------- 8. NDMA SACHET alert normalisation
# In-process against a captured row from the live feed: no key, no network,
# no running server. SACHET is the authoritative alert path while the IMD key
# is pending, so a mistake here mislabels a real warning or drops it. The
# centroid check matters most -- the feed publishes "lon,lat" while the rest
# of this service uses lat/lon, and swapping them puts an Assam thunderstorm
# in the Indian Ocean.
print("")
print("[8] NDMA SACHET alert normalisation")

from app.providers import sachet as _sx                   # noqa: E402

_ROW = {
    "severity": "WATCH",
    "identifier": 1788465350602010,
    "effective_start_time": "Fri Sep 04 01:22:00 IST 2026",
    "effective_end_time": "Fri Sep 04 04:22:00 IST 2026",
    "disaster_type": "Thunderstorm with Lightning",
    "area_description": "4 districts of Assam",
    "warning_message": "IMD Guwahati has issued forecast for Thunderstorm "
                       "with Lightning. Issued in Public Interest by ASDMA.",
    "severity_color": "yellow",
    "centroid": "95.05437771692465,27.06182950319295",
    "alert_source": "IMD Shillong",
    "area_covered": 10851.776608053555,
    "actual_lang": "en",
}

# --- 1. centroid is lon,lat and must not be swapped ------------------------
_c = _sx.parse_centroid(_ROW["centroid"])
check("centroid parses to (lat, lon), not (lon, lat)",
      _c is not None and abs(_c[0] - 27.0618) < 0.001 and abs(_c[1] - 95.0544) < 0.001,
      f"got {_c}")
check("an Assam alert lands in Assam, not the Indian Ocean",
      _c is not None and 22 < _c[0] < 30 and 89 < _c[1] < 97, f"got {_c}")
check("malformed centroid is rejected, not guessed",
      _sx.parse_centroid("not-a-point") is None)
check("out-of-range centroid is rejected", _sx.parse_centroid("400,900") is None)
check("missing centroid is rejected", _sx.parse_centroid(None) is None)

# --- 2. severity comes off IMD's own colour ladder -------------------------
check("severity_color yellow -> YELLOW", _sx.severity_of(_ROW) is _Sev.YELLOW)
check("severity_color orange -> ORANGE",
      _sx.severity_of({"severity_color": "orange"}) is _Sev.ORANGE)
check("severity_color red -> RED", _sx.severity_of({"severity_color": "red"}) is _Sev.RED)
check("word 'WARNING' falls back to RED",
      _sx.severity_of({"severity": "WARNING"}) is _Sev.RED)
check("an unknown severity degrades to YELLOW, never to none",
      _sx.severity_of({"severity": "???"}) is _Sev.YELLOW)

# --- 3. Java-style timestamps ----------------------------------------------
_t = _sx.parse_ts("Fri Sep 04 01:22:00 IST 2026")
check("IST timestamp parses to the right instant",
      _t is not None and _t.year == 2026 and _t.month == 9 and _t.day == 4
      and _t.hour == 1 and _t.utcoffset().total_seconds() == 19800, f"got {_t}")
check("a malformed timestamp returns None rather than raising",
      _sx.parse_ts("not a date") is None)

# --- 4. area -> geofence radius, clamped -----------------------------------
check("10851 km2 becomes a ~58.8 km radius",
      abs(_sx.radius_km(10851.776608053555) - 58.8) < 0.2,
      f"got {_sx.radius_km(10851.776608053555)}")
check("a state-sized area is clamped to 300 km", _sx.radius_km(9_000_000) == 300.0)
check("a tiny area is floored at 10 km", _sx.radius_km(1.0) == 10.0)
check("a missing area falls back to 50 km", _sx.radius_km(None) == 50.0)

# --- 5. the whole row -> AlertEvent ----------------------------------------
_ev = _sx.to_event(_ROW)
check("a well-formed row becomes an AlertEvent", _ev is not None)
check("alert id is the feed's own identifier",
      _ev is not None and _ev.id == "1788465350602010", f"got {_ev.id if _ev else None}")
check("provenance names originator and carrier",
      _ev is not None and _ev.provenance.source == "IMD Shillong via NDMA SACHET",
      f"got {_ev.provenance.source if _ev else None}")
check("a SACHET alert is marked authoritative",
      _ev is not None and _ev.provenance.authoritative is True)
check("headline is the hazard alone", _ev is not None and _ev.headline == "Thunderstorm with Lightning",
      f"got {_ev.headline if _ev else None}")
check("the area lives in its own field, not folded into the headline",
      _ev is not None and "Assam" in _ev.area and "Assam" not in _ev.headline,
      f"got area={_ev.area if _ev else None}")
check("a row with no centroid is dropped, not broadcast to everyone",
      _sx.to_event({**_ROW, "centroid": None}) is None)

# --- 6. it matches the geofence it should ----------------------------------
from app import alerts as _al                             # noqa: E402

_sub_near = _al.subscribe("near@example.org", 27.0, 95.0, radius_km=25,
                          min_severity=_Sev.YELLOW)
_sub_far = _al.subscribe("far@example.org", 13.08, 80.27, radius_km=25,
                         min_severity=_Sev.YELLOW)
_matched = {s.id for s in _al.match(_ev)}
check("a subscriber inside the SACHET footprint matches", _sub_near.id in _matched)
check("a subscriber 2,000 km away does not", _sub_far.id not in _matched)
_al.unsubscribe(_sub_near.id)
_al.unsubscribe(_sub_far.id)

# --- IMD nowcasts, and the chat warnings answer reading SACHET ---------------
# "Any warning for Puri?" used to answer from NWP screening alone and say the
# IMD key was missing, while the same service fanned out SACHET's official
# warnings to subscribers. Row captured from FetchIMDNowcastAlerts, 17 Sep 2026.
from app.cache import upstream_cache as _uc              # noqa: E402
from app.config import get_settings as _gs               # noqa: E402
from app.schemas import ParsedQuery as _PQ, Place as _Place, Intent as _Int  # noqa: E402
_nc_row = {"severity": "Watch", "severity_color": "yellow", "source": "IMD",
           "effective_start_time": "Thu Sep 17 21:30:00 IST 2026",
           "effective_end_time": "Thu Sep 17 23:30:00 IST 2026",
           "area_description": "Wardha    ", "event_category": "Rain",
           "events": "Moderate Rain, Light Rain",
           "location": {"coordinates": [78.600000, 20.740000], "type": "Point"}}
_nc = _sx.to_nowcast(_nc_row)
check("nowcast location read as [lon, lat]",
      _nc and (_nc["lat"], _nc["lon"]) == (20.74, 78.6), f"got {_nc}")
check("nowcast is authoritative IMD via SACHET, area trimmed",
      _nc["provenance"].authoritative and _nc["area"] == "Wardha"
      and "IMD" in _nc["provenance"].source)

_uc.set(_uc.key(_gs().sachet_nowcast_url), [_nc_row], ttl=600)
_during = _dtt(2026, 9, 17, 17, 0, tzinfo=_utc.utc)      # 22:30 IST
_after = _dtt(2026, 9, 17, 18, 30, tzinfo=_utc.utc)      # 00:00 IST
check("nowcast within 40 km of Wardha town is returned, with its distance",
      len(asyncio.run(_sx.nowcasts_near(20.75, 78.62, now=_during))) == 1)
check("nowcast 150 km away is not",
      asyncio.run(_sx.nowcasts_near(21.15, 79.09 + 1.0, now=_during)) == [])
check("expired nowcast is not",
      asyncio.run(_sx.nowcasts_near(20.75, 78.62, now=_after)) == [])


async def _warn(official, ncs, point_error=None, lang="en"):
    async def fake_point(lat, lon, r):
        _sx.POINT_LAST_ERROR = point_error
        return official
    async def fake_nc(lat, lon):
        return ncs
    async def fake_fc(lat, lon, days=5):
        return {"days": [day(date="2026-09-18")], "provenance":
                Provenance(source="test model", product="test")}
    saved = (_sx.alerts_for_point, _sx.nowcasts_near, _tools.nwp.forecast)
    _sx.alerts_for_point, _sx.nowcasts_near, _tools.nwp.forecast = fake_point, fake_nc, fake_fc
    try:
        return await _tools.answer_warnings(
            _PQ(intent=_Int.WARNING, lang=lang),
            _Place(name="Wardha", lat=20.75, lon=78.62))
    finally:
        _sx.alerts_for_point, _sx.nowcasts_near, _tools.nwp.forecast = saved
        _sx.POINT_LAST_ERROR = None

_nc_live = dict(_nc, distance_km=2.3)
_w = asyncio.run(_warn([], [_nc_live]))
check("chat warnings lead with the official IMD nowcast",
      _w["en"].startswith("Official warnings") and "IMD nowcast for Wardha" in _w["en"]
      and "21:30–23:30" in _w["en"], _w["en"])
check("chat warnings carry the nowcast's authoritative provenance",
      any(s.authoritative for s in _w["sources"]))
check("nowcast line rendered in the asker's language",
      "నౌకాస్ట్" in asyncio.run(_warn([], [_nc_live], lang="te"))["loc"])
_w_down = asyncio.run(_warn([], [], point_error="ConnectTimeout"))
check("SACHET unreachable is declared, not answered as an all-clear",
      any("not an all-clear" in x for x in _w_down["degraded"]), f"{_w_down['degraded']}")

# ------------------------------------- 9. exact CAP footprint matching
# The disc derived from `area_covered` over-matches: a 14-district Rajasthan
# advisory becomes a ~184 km circle covering districts the advisory never
# named. FetchLocationWiseAlerts returns the alert's real CAP polygon in
# `area_json`, so when it is present the geofence does point-in-polygon
# instead. These run offline against a hand-built footprint.
print("")
print("[9] exact CAP footprint matching")

# A square around Guwahati, with a hole punched out of the middle.
# GeoJSON order is [lon, lat] -- the reverse of this service's own order.
_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[91.0, 25.5], [92.5, 25.5], [92.5, 26.5], [91.0, 26.5], [91.0, 25.5]],
        [[91.6, 26.0], [91.9, 26.0], [91.9, 26.2], [91.6, 26.2], [91.6, 26.0]],
    ],
}
_MULTI = {"type": "MultiPolygon", "coordinates": [
    [[[91.0, 25.5], [92.5, 25.5], [92.5, 26.5], [91.0, 26.5], [91.0, 25.5]]],
    [[[80.0, 12.8], [80.5, 12.8], [80.5, 13.4], [80.0, 13.4], [80.0, 12.8]]],
]}

check("a point inside the polygon matches",
      _sx.point_in_geometry(26.4, 91.2, _SQUARE) is True)
check("a point outside the polygon does not",
      _sx.point_in_geometry(28.6, 77.2, _SQUARE) is False)
check("a point inside an interior ring is treated as outside",
      _sx.point_in_geometry(26.1, 91.75, _SQUARE) is False)
check("MultiPolygon matches on its first part",
      _sx.point_in_geometry(26.0, 91.2, _MULTI) is True)
check("MultiPolygon matches on its second part (Chennai)",
      _sx.point_in_geometry(13.08, 80.27, _MULTI) is True)
check("MultiPolygon rejects a point in neither part",
      _sx.point_in_geometry(19.07, 72.87, _MULTI) is False)
check("a missing footprint never matches", _sx.point_in_geometry(26.0, 91.2, None) is False)
check("a malformed footprint never matches",
      _sx.point_in_geometry(26.0, 91.2, {"type": "Polygon", "coordinates": []}) is False)

# --- area_json parsing -----------------------------------------------------
check("area_json parses from a JSON string",
      (_sx.parse_area_json(json.dumps(_SQUARE)) or {}).get("type") == "Polygon")
check("area_json accepts an already-decoded dict",
      (_sx.parse_area_json(_SQUARE) or {}).get("type") == "Polygon")
check("a non-geometry area_json is rejected",
      _sx.parse_area_json('{"type": "Point", "coordinates": [1, 2]}') is None)
check("unparseable area_json is rejected, not guessed",
      _sx.parse_area_json("{not json") is None)
check("absent area_json is None", _sx.parse_area_json(None) is None)

# --- the matcher prefers the footprint over the disc -----------------------
_precise = _sx.to_event({**_ROW, "area_json": json.dumps(_SQUARE)})
check("an event built from area_json carries its footprint",
      _precise is not None and _precise.geometry is not None)

# Inside the disc but OUTSIDE the real polygon: the exact test must reject it.
# The disc alone would have matched, which is the bug this removes.
_outside = _al.subscribe("outside@example.org", 27.6, 95.0, radius_km=25,
                         min_severity=_Sev.YELLOW)
_inside = _al.subscribe("inside@example.org", 26.0, 91.2, radius_km=25,
                        min_severity=_Sev.YELLOW)
_ids = {s.id for s in _al.match(_precise)}
check("a subscriber inside the real polygon matches", _inside.id in _ids)
check("a subscriber far outside the polygon is rejected even though the disc covered them",
      _outside.id not in _ids)

# --- distance to the footprint, bounded by the subscriber's own radius ----
check("a point inside the footprint is 0 km from it",
      _sx.distance_to_geometry_km(26.0, 91.2, _SQUARE) == 0.0)
check("a point just outside reports a small positive gap",
      0 < (_sx.distance_to_geometry_km(26.6, 91.2, _SQUARE) or 0) < 30,
      f"got {_sx.distance_to_geometry_km(26.6, 91.2, _SQUARE)}")
check("a distant point reports a large gap",
      (_sx.distance_to_geometry_km(28.6, 77.2, _SQUARE) or 0) > 500)
check("no footprint reports None, so the caller falls back to the disc",
      _sx.distance_to_geometry_km(26.0, 91.2, None) is None)

# A subscriber 65 km outside the polygon: heard only if they asked for it.
_wide = _al.subscribe("wide@example.org", 27.1, 91.7, radius_km=200,
                      min_severity=_Sev.YELLOW)
_narrow = _al.subscribe("narrow@example.org", 27.1, 91.7, radius_km=5,
                        min_severity=_Sev.YELLOW)
_ids2 = {s.id for s in _al.match(_precise)}
check("a subscriber outside the polygon but inside their own radius is reached",
      _wide.id in _ids2)
check("the same point with a tight radius is not",
      _narrow.id not in _ids2)
_al.unsubscribe(_wide.id)
_al.unsubscribe(_narrow.id)

# The same two against the coarse event: the disc over-matches, which is why
# confirm_precise() exists.
_coarse = _sx.to_event(_ROW)
_coarse_ids = {s.id for s in _al.match(_coarse)}
check("the disc fallback still matches the near subscriber", _outside.id in _coarse_ids)
_al.unsubscribe(_outside.id)
_al.unsubscribe(_inside.id)

# ------------------------------------- 10. place resolution must not relocate
# The gazetteer used to fall back to a substring match, so "nagarkurnool"
# matched "kurnool" and a Telangana district resolved to an Andhra Pradesh one
# about 200 km away -- returned as a clean gazetteer hit with normal
# provenance, so nothing about the answer looked wrong. For a service that
# issues warnings, resolving to the wrong district is not a near miss.
print("")
print("[10] place resolution")

from app.providers import geocode as _geo                 # noqa: E402

_nk = _geo.lookup_local("Nagarkurnool")
check("Nagarkurnool resolves to Nagarkurnool, not Kurnool",
      _nk is not None and _nk.admin1 == "Telangana" and 16.0 < _nk.lat < 17.0,
      f"got {_nk.name if _nk else None} / {_nk.admin1 if _nk else None}")
check("the spaced spelling resolves the same way",
      (_geo.lookup_local("Nagar Kurnool") or _nk).admin1 == "Telangana")

_k = _geo.lookup_local("Kurnool")
check("Kurnool itself still resolves to Kurnool",
      _k is not None and _k.admin1 == "Andhra Pradesh",
      f"got {_k.admin1 if _k else None}")
check("Nagarkurnool and Kurnool are not the same point",
      _nk is not None and _k is not None
      and abs(_nk.lat - _k.lat) > 0.4,
      f"{_nk.lat if _nk else None} vs {_k.lat if _k else None}")

# A name the gazetteer does not hold must go to the network geocoder rather
# than being answered with whichever entry happens to be a substring of it.
check("an unlisted place is not silently substituted",
      _geo.lookup_local("Navi Mumbai") is None,
      f"got {(_geo.lookup_local('Navi Mumbai') or {}) and _geo.lookup_local('Navi Mumbai').name}")
check("a qualified name is not collapsed to its parent",
      _geo.lookup_local("North Delhi") is None)
check("an exact multi-word entry still resolves locally",
      (_geo.lookup_local("New Delhi") or {}) and _geo.lookup_local("New Delhi").name == "New Delhi")
check("normalisation still handles case and punctuation",
      (_geo.lookup_local("  hYdErAbAd ") or {}) and
      _geo.lookup_local("  hYdErAbAd ").admin1 == "Telangana")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
