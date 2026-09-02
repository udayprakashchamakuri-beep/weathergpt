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

# The provenance record must not invent an issue time. Open-Meteo publishes a
# valid time for the current analysis and none for the daily forecast, so the
# forecast source must carry issued_at = null rather than "now".
fc_src = [s_ for s_ in d["sources"] if "forecast" in s_["product"]]
check("forecast provenance does not fabricate an issue time",
      bool(fc_src) and fc_src[0]["issued_at"] is None,
      f"got {fc_src[0]['issued_at'] if fc_src else 'no forecast source'}")
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

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
