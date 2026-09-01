"""WeatherGPT API -- SIH26068 (MoES / India Meteorological Department).

Run:  uvicorn app.main:app --reload --port 8000
Docs: http://localhost:8000/docs
UI:   http://localhost:8000/
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (Depends, FastAPI, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from . import alerts, i18n, nlu, security, tools
from .cache import response_cache
from .config import get_settings
from .providers import geocode, imd, openmeteo
from .schemas import (AlertEvent, ChatRequest, ChatResponse, Intent, Persona,
                      Place, Severity)

settings = get_settings()
STARTED = time.time()

app = FastAPI(
    title="WeatherGPT",
    version="0.4.0",
    description=("Conversational weather intelligence for India. "
                 "SIH26068 — Ministry of Earth Sciences / IMD."),
)
# CORS is read from CORS_ALLOW_ORIGINS (comma-separated). It defaults to "*"
# so a fresh clone runs with no configuration, which is what the README
# promises -- but on the deployment it is pinned to the deployed origin, so a
# third-party page cannot drive this API from a visitor's browser.
_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    # X-Demo-Token must be allowed through preflight or the gated endpoints
    # become uncallable from the browser once an explicit origin list is set.
    allow_headers=["*", "X-Demo-Token"],
)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


# ------------------------------------------------------------------ health
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ps_id": settings.ps_id,
        "uptime_s": round(time.time() - STARTED, 1),
        "sources": {
            "imd_api": "configured" if imd.available() else "no key — NWP fallback",
            "nwp": "open-meteo (GFS/ECMWF/ICON)",
            "reanalysis": "ERA5 archive",
            "language": ("bhashini" if settings.bhashini_api_key
                         else "bundled templates + on-device speech"),
            "llm_router": settings.llm_provider,
        },
        "cache": {"response": response_cache.stats()},
        "subscriptions": len(alerts.SUBSCRIPTIONS),
        "languages": list(i18n.LANGUAGES),
        "templated_languages": list(i18n.TEMPLATED),
    }


@app.get("/api/imd/endpoints")
async def imd_endpoints():
    """The IMD endpoints this service is wired to. Useful for the demo:
    it shows the integration surface even before a key is issued."""
    return {
        "base": settings.imd_api_base,
        "key_configured": imd.available(),
        "register": "https://api.imd.gov.in/public/register.php",
        "reference": "https://api.imd.gov.in/public/api_reference.html",
        "endpoints": imd.ENDPOINTS,
    }


# -------------------------------------------------------------------- chat
async def _resolve_place(req: ChatRequest, parsed) -> Place | None:
    if parsed.place_text:
        if place := await geocode.resolve(parsed.place_text):
            return place
    if req.lat is not None and req.lon is not None:
        return await geocode.reverse(req.lat, req.lon)
    return None


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    # Guards the shared upstream NWP quota, not the CPU: every request from
    # this deployment reaches the provider from one IP.
    security.enforce(security.chat_limiter, request, "/api/chat")
    t0 = time.perf_counter()

    parsed = await nlu.parse(req.message, req.lang, req.persona)
    place = await _resolve_place(req, parsed)

    if place is None:
        return ChatResponse(
            answer=i18n.t("no_place", parsed.lang),
            answer_en=i18n.t("no_place", "en"),
            intent=parsed.intent, persona=parsed.persona, lang=parsed.lang,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            followups=["Weather in Hyderabad", "Forecast for Guntur",
                       "Cyclone warning for Puri"],
        )

    ck = response_cache.key("chat", parsed.intent, parsed.persona, parsed.lang,
                            round(place.lat, 2), round(place.lon, 2),
                            parsed.day_offset, parsed.horizon_days,
                            parsed.years_back, parsed.month)
    if (hit := response_cache.get(ck)) is not None:
        hit = dict(hit)
        hit["cached"] = True
        hit["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        return ChatResponse(**hit)

    try:
        result = await tools.execute(parsed, place)
    except Exception as exc:                       # upstream failure
        # Never fake an answer when the data layer is down. Say so, name the
        # source, and keep the conversation usable.
        detail = str(exc)
        friendly = ("The upstream data source is rate-limiting or unreachable "
                    "right now, so I will not guess a value. Try again in a "
                    "moment.")
        if "429" in detail:
            friendly = ("The public archive is rate-limiting this shared IP. "
                        "In deployment this query is served from the local "
                        "IMD gridded / ERA5 store with no such limit.")
        return ChatResponse(
            answer=friendly, answer_en=friendly, intent=parsed.intent,
            persona=parsed.persona, lang=parsed.lang, place=place,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            degraded=[f"upstream error: {detail[:180]}"],
            followups=["Try the current weather instead", "Show the 7-day forecast"],
        )

    # Long-tail localisation: only for languages with no bundled template.
    answer_loc = result["loc"]
    degraded = list(result.get("degraded", []))
    if parsed.lang != "en" and not i18n.has_templates(parsed.lang):
        answer_loc = await i18n.localize(result["en"], parsed.lang)
        if answer_loc == result["en"]:
            degraded.append(f"no bundled template for '{parsed.lang}' and Bhashini "
                            "not configured — answered in English")

    resp = ChatResponse(
        answer=answer_loc,
        answer_en=result["en"],
        intent=parsed.intent,
        persona=parsed.persona,
        lang=parsed.lang,
        place=place,
        facts=result.get("facts", []),
        advisory=result.get("advisory"),
        severity=result.get("severity", Severity.NONE),
        sources=result.get("sources", []),
        latency_ms=int((time.perf_counter() - t0) * 1000),
        cached=False,
        degraded=degraded,
        chart=result.get("chart"),
        followups=tools.FOLLOWUPS.get(parsed.intent, []),
    )
    response_cache.set(ck, resp.model_dump(mode="json"), ttl=settings.cache_ttl_seconds)
    return resp


@app.get("/api/parse")
async def parse_only(q: str, lang: str = "en"):
    """Expose the router on its own — useful for showing judges that the LLM
    only ever emits a typed intent, never a weather value."""
    parsed = await nlu.parse(q, lang)
    return parsed.model_dump(mode="json")


# ------------------------------------------------------------- direct data
@app.get("/api/weather/current")
async def api_current(place: str | None = None, lat: float | None = None,
                      lon: float | None = None):
    p = await _place_or_400(place, lat, lon)
    data = await openmeteo.current(p.lat, p.lon)
    return {"place": p.model_dump(), **{k: v for k, v in data.items()
                                        if k != "provenance"},
            "provenance": data["provenance"].model_dump(mode="json")}


@app.get("/api/weather/forecast")
async def api_forecast(place: str | None = None, lat: float | None = None,
                       lon: float | None = None, days: int = Query(7, ge=1, le=16)):
    p = await _place_or_400(place, lat, lon)
    data = await openmeteo.forecast(p.lat, p.lon, days=days)
    return {"place": p.model_dump(), "days": data["days"],
            "provenance": data["provenance"].model_dump(mode="json")}


@app.get("/api/climate/trend")
async def api_climate(place: str | None = None, lat: float | None = None,
                      lon: float | None = None,
                      years: int = Query(30, ge=5, le=60),
                      month: int | None = Query(None, ge=1, le=12)):
    p = await _place_or_400(place, lat, lon)
    data = await openmeteo.climate_series(p.lat, p.lon, years_back=years, month=month)
    series = data["series"]
    if len(series) < 5:
        raise HTTPException(422, "insufficient archive data for a trend")
    years_x = [float(s["year"]) for s in series]
    t_slope, _ = openmeteo.linear_trend(years_x, [s["mean_tmax_c"] for s in series])
    r_slope, _ = openmeteo.linear_trend(years_x, [float(s["total_rain_mm"])
                                                  for s in series])
    return {
        "place": p.model_dump(),
        "period": f"{series[0]['year']}-{series[-1]['year']}",
        "month": month,
        "temp_trend_c_per_decade": round(t_slope * 10, 3),
        "rain_trend_mm_per_decade": round(r_slope * 10, 2),
        "series": series,
        "provenance": data["provenance"].model_dump(mode="json"),
    }


async def _place_or_400(place: str | None, lat: float | None,
                        lon: float | None) -> Place:
    if place:
        if p := await geocode.resolve(place):
            return p
        raise HTTPException(404, f"could not resolve place '{place}'")
    if lat is not None and lon is not None:
        return await geocode.reverse(lat, lon)
    raise HTTPException(400, "supply ?place= or ?lat=&lon=")


# ------------------------------------------------------------------ alerts
@app.post("/api/alerts/subscribe",
          dependencies=[Depends(security.require_demo_token)])
async def api_subscribe(request: Request, address: str, place: str | None = None,
                        lat: float | None = None, lon: float | None = None,
                        channel: str = "push", lang: str = "en",
                        persona: Persona = Persona.GENERAL,
                        radius_km: float = 25.0,
                        min_severity: Severity = Severity.YELLOW):
    security.enforce(security.subscribe_limiter, request,
                     "/api/alerts/subscribe")
    p = await _place_or_400(place, lat, lon)
    sub = alerts.subscribe(address, p.lat, p.lon, channel=channel,
                           radius_km=radius_km, lang=lang, persona=persona,
                           min_severity=min_severity)
    return {"subscription": sub.model_dump(mode="json"), "place": p.model_dump()}


@app.delete("/api/alerts/subscribe/{sub_id}")
async def api_unsubscribe(sub_id: str):
    if not alerts.unsubscribe(sub_id):
        raise HTTPException(404, "no such subscription")
    return {"deleted": sub_id}


@app.get("/api/alerts/subscriptions")
async def api_subscriptions():
    return {"count": len(alerts.SUBSCRIPTIONS),
            "subscriptions": [s.model_dump(mode="json")
                              for s in alerts.SUBSCRIPTIONS.values()]}


@app.post("/api/alerts/scan",
          dependencies=[Depends(security.require_demo_token)])
async def api_scan(place: str | None = None, lat: float | None = None,
                   lon: float | None = None):
    """Run the threshold monitor for one location and disseminate if it fires."""
    p = await _place_or_400(place, lat, lon)
    event = await alerts.scan_location(p.lat, p.lon, p.name)
    if not event:
        return {"fired": False, "place": p.model_dump(),
                "message": "no threshold exceeded in the next 3 days"}
    return {"fired": True, **alerts.dispatch(event)}


@app.post("/api/alerts/simulate",
           dependencies=[Depends(security.require_demo_token)])
async def api_simulate(place: str = "Puri", severity: Severity = Severity.RED,
                       headline: str = ("Cyclonic storm expected to cross the coast; "
                                        "extremely heavy rainfall and gale-force winds")):
    """DEMO ONLY. Broadcasts a synthetic CAP alert to matching subscribers.

    Disabled unless ENABLE_DEMO_ENDPOINTS is true, because an open endpoint
    that fans out a fabricated red warning is a public-safety hazard, not a
    convenience. Every event it produces is stamped "Simulated CAP feed
    (demo)" in its provenance and "simulated": true in the response, so a
    demo alert can never be mistaken for a real one downstream.
    """
    if not settings.enable_demo_endpoints:
        raise HTTPException(403, "demo endpoints are disabled on this deployment")
    p = await _place_or_400(place, None, None)
    event = alerts.simulate(p.name, p.lat, p.lon, severity, headline)
    return {"simulated": True, **alerts.dispatch(event)}


@app.get("/api/alerts/log")
async def api_log(limit: int = 50):
    return {"count": len(alerts.DELIVERY_LOG), "entries": alerts.DELIVERY_LOG[-limit:]}


# --------------------------------------------------------------- websocket
@app.websocket("/ws/alerts")
async def ws_alerts(ws: WebSocket):
    """Live push channel. In production this is one of several fan-out
    adapters; the browser client uses it to render alerts as they arrive."""
    await ws.accept()
    await ws.send_json({"type": "hello", "ts": datetime.now(timezone.utc).isoformat(),
                        "subscriptions": len(alerts.SUBSCRIPTIONS)})
    try:
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "scan":
                p = await geocode.resolve(msg.get("place", "Hyderabad"))
                if not p:
                    await ws.send_json({"type": "error", "message": "unknown place"})
                    continue
                event = await alerts.scan_location(p.lat, p.lon, p.name)
                if event:
                    await ws.send_json({"type": "alert", **alerts.dispatch(event)})
                else:
                    await ws.send_json({"type": "clear", "place": p.name})
            elif msg.get("action") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        return


# ---------------------------------------------------------------- frontend
@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-file UI with the demo token injected at page load.

    The gated endpoints (subscribe / scan / simulate) need X-Demo-Token, and
    the UI has to obtain it from somewhere. Injecting it here keeps the secret
    out of the repo and out of the image -- it is read from the environment at
    request time.

    Be clear about what this does and does not buy: anyone who can load this
    page can read the token out of the page source. It stops drive-by scripted
    abuse of the dissemination endpoints by callers who never fetch the page;
    it is NOT access control, and it would not survive an adversary who wanted
    in. That is the right trade for a demo whose delivery channel is a log
    write. Before a real SMS/IVR channel is wired up, this must be replaced by
    a server-side session or a signed, short-lived token.
    """
    idx = FRONTEND / "index.html"
    if not idx.exists():
        return JSONResponse({"message": "WeatherGPT API", "docs": "/docs"})
    html = idx.read_text(encoding="utf-8")
    injected = (
        "<script>window.__DEMO_TOKEN__ = "
        + json.dumps(settings.demo_token or "")
        + ";</script></head>"
    )
    return HTMLResponse(html.replace("</head>", injected, 1))
