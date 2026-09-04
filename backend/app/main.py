"""WeatherGPT API -- SIH26068 (MoES / India Meteorological Department).

Run:  uvicorn app.main:app --reload --port 8000
Docs: http://localhost:8000/docs
UI:   http://localhost:8000/
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (Depends, FastAPI, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import alerts, i18n, nlu, security, tools
from .cache import response_cache
from .config import get_settings
from .providers import geocode, imd, nwp, sachet
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
# CORS_ALLOW_ORIGINS wins; else Render's injected RENDER_EXTERNAL_URL pins the
# deployment to its own origin; else "*" so a fresh clone runs unconfigured.
# See Settings.allowed_origins().
_origins = settings.allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_methods=["*"],
    # X-Demo-Token must be allowed through preflight or the gated endpoints
    # become uncallable from the browser once an explicit origin list is set.
    allow_headers=["*", "X-Demo-Token"],
)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


@app.on_event("startup")
async def _warn_on_unsafe_config() -> None:
    for problem in security.startup_check():
        logging.getLogger("weathergpt.startup").error("CONFIG: %s", problem)


# SACHET is a pull feed, so ingest is a timer rather than a callback. The task
# handle is kept so shutdown can cancel it instead of leaving a poll in flight.
_SACHET_TASK: asyncio.Task | None = None


@app.on_event("startup")
async def _start_sachet_poll() -> None:
    global _SACHET_TASK
    if not settings.enable_sachet_poll:
        logging.getLogger("weathergpt.startup").info(
            "SACHET polling disabled (ENABLE_SACHET_POLL=false)")
        return
    _SACHET_TASK = asyncio.create_task(alerts.sachet_loop())


@app.on_event("shutdown")
async def _stop_sachet_poll() -> None:
    if _SACHET_TASK is None:
        return
    _SACHET_TASK.cancel()
    try:
        await _SACHET_TASK
    except asyncio.CancelledError:
        pass


# ------------------------------------------------------------------ health
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "ps_id": settings.ps_id,
        "uptime_s": round(time.time() - STARTED, 1),
        "sources": {
            "imd_api": "configured" if imd.available() else "no key — NWP fallback",
            # The provider that actually answered the most recent call, and
            # whether it was the primary or a fallback -- not the configured
            # ideal. Reporting the primary while the fallback is serving hides
            # a live degradation from whoever is watching this endpoint.
            "nwp": nwp.status(),
            "reanalysis": "ERA5 archive",
            # Authoritative alert ingest. This is what lets an answer carry an
            # authoritative provenance chip while the IMD key is pending.
            "alerts_feed": await sachet.status(),
            "language": ("bhashini" if settings.bhashini_api_key
                         else "bundled templates + on-device speech"),
            "llm_router": {
                "provider": settings.llm_provider,
                "model": settings.llm_model,
                # Non-null means routing fell back to rules and why.
                "last_error": nlu.LLM_LAST_ERROR,
            },
        },
        "cache": {"response": response_cache.stats()},
        "subscriptions": len(alerts.SUBSCRIPTIONS),
        "alert_ingest": alerts.sachet_state(),
        # Open WebSocket clients currently in the alert fan-out set.
        "live_connections": len(alerts.CONNECTIONS),
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
        if "429" in detail or "rate-limited" in detail:
            # Name the host that actually refused. The forecast API and the
            # ERA5 archive are different services on different limits, and
            # blaming the archive for a forecast failure sends anyone
            # debugging this to the wrong place.
            friendly = ("Every upstream forecast source refused this request "
                        "(rate limit on the shared deployment IP). No value "
                        "will be guessed. Air quality and the climate archive "
                        "are unaffected; try again shortly.")
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
    data = await nwp.current(p.lat, p.lon)
    return {"place": p.model_dump(), **{k: v for k, v in data.items()
                                        if k != "provenance"},
            "provenance": data["provenance"].model_dump(mode="json")}


@app.get("/api/weather/forecast")
async def api_forecast(place: str | None = None, lat: float | None = None,
                       lon: float | None = None, days: int = Query(7, ge=1, le=16)):
    p = await _place_or_400(place, lat, lon)
    data = await nwp.forecast(p.lat, p.lon, days=days)
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


@app.post("/api/alerts/poll",
          dependencies=[Depends(security.require_demo_token)])
async def api_poll_sachet(replay: bool = False):
    """Pull the NDMA SACHET feed now and disseminate anything new.

    The background loop already does this on a timer; this is the manual
    trigger for a demo, where waiting five minutes for the next tick is not an
    option. `replay=true` re-dispatches every alert currently in force rather
    than only the unseen ones, so the fan-out can be shown against real
    warnings on a day when nothing new has been issued.
    """
    return await alerts.poll_sachet(replay=replay)


@app.get("/api/alerts/live")
async def api_live_alerts(limit: int = 25, place: str | None = None,
                          lat: float | None = None, lon: float | None = None,
                          radius_km: float = 100.0):
    """Read-through view of the authoritative feed. Ungated and read-only --
    this publishes nothing and fans out nothing.

    With a location it returns only the alerts affecting that point, matched
    by NDMA against the alert's real CAP polygon and carrying that polygon
    back. Without one it returns the national feed.

    The location form is what a user-facing client wants: showing a Vijayawada
    reader a warning for a district 1,500 km away is worse than showing none,
    because it teaches them the banner is noise.
    """
    if place and lat is None:
        p = await geocode.resolve(place)
        if p:
            lat, lon = p.lat, p.lon
    if lat is not None and lon is not None:
        events = await sachet.alerts_for_point(lat, lon, radius_km)
        scope = f"within {radius_km:g} km"
    else:
        events = await sachet.recent_events()
        scope = "national"
    return {
        "count": len(events),
        "source": "NDMA SACHET (CAP)",
        "scope": scope,
        "alerts": [e.model_dump(mode="json") for e in events[:limit]],
    }


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
    adapters; the browser client uses it to render alerts as they arrive.

    Gated by the same DEMO_TOKEN as the HTTP dissemination endpoints, because
    the "scan" action below calls alerts.dispatch() -- writing to the delivery
    log and fanning out to every matching subscriber. Rejecting at the
    handshake means an unauthorised client never gets a session at all, rather
    than being refused per message.
    """
    if not security.websocket_authorized(ws):
        # Close before accept: this fails the upgrade itself. 1008 is the
        # WebSocket "policy violation" close code.
        await ws.close(code=1008, reason="missing or invalid demo token")
        return
    await ws.accept()
    # Join the fan-out set: from here on this client receives every dispatched
    # alert, including ones triggered by curl, another tab, or the scheduler --
    # not just answers to its own requests.
    alerts.register(ws)
    try:
        await ws.send_json({"type": "hello",
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "subscriptions": len(alerts.SUBSCRIPTIONS)})
        while True:
            msg = await ws.receive_json()
            if msg.get("action") == "scan":
                p = await geocode.resolve(msg.get("place", "Hyderabad"))
                if not p:
                    await ws.send_json({"type": "error", "message": "unknown place"})
                    continue
                event = await alerts.scan_location(p.lat, p.lon, p.name)
                if event:
                    # No direct send: dispatch() broadcasts to every registered
                    # connection and this socket is one of them. Sending here
                    # too would deliver the alert twice to the requester.
                    alerts.dispatch(event)
                else:
                    # "clear" is an answer to this client's question, not an
                    # alert, so it stays point-to-point.
                    await ws.send_json({"type": "clear", "place": p.name})
            elif msg.get("action") == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    finally:
        # Must run on every exit path -- a client that disconnects mid-send
        # would otherwise sit in the registry as a permanently failing target.
        alerts.unregister(ws)


# ---------------------------------------------------------------- frontend
# ------------------------------------------------------------------- PWA
# Served explicitly rather than via StaticFiles so each file gets the exact
# content type and cache policy it needs. Getting these wrong is the usual
# reason an installable app silently stops being installable.
@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest():
    f = FRONTEND / "manifest.webmanifest"
    if not f.exists():
        raise HTTPException(404, "manifest not built")
    return FileResponse(f, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    """The service worker must be served from the ORIGIN ROOT.

    A worker can only control pages at or below its own path, so serving this
    from /static/sw.js would scope it to /static and it would control nothing.
    It is also sent no-cache: a browser holding an old copy of the worker is
    the classic way a PWA gets stuck on a stale build.
    """
    f = FRONTEND / "sw.js"
    if not f.exists():
        raise HTTPException(404, "service worker not built")
    return FileResponse(f, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})


@app.get("/icons/{name}", include_in_schema=False)
async def icon(name: str):
    # Explicit allow-list, not a path join on user input: this route would
    # otherwise be a directory traversal into the container filesystem.
    if name not in {"icon-192.png", "icon-512.png", "icon-maskable-512.png",
                    "apple-touch-icon.png", "favicon-32.png"}:
        raise HTTPException(404, "no such icon")
    f = FRONTEND / "icons" / name
    if not f.exists():
        raise HTTPException(404, "icon not built")
    return FileResponse(f, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    """The WeatherNow dashboard UI.

    Served alongside the chat UI at "/" rather than replacing it: the smoke
    suite and the demo flow both drive the chat surface, and a dashboard that
    took over the root would break them.
    """
    f = FRONTEND / "app.html"
    if not f.exists():
        raise HTTPException(404, "dashboard not built")
    # Same token injection as "/" -- the dashboard opens /ws/alerts and reads
    # /api/alerts/log, both of which are gated. Same caveat too: anyone who can
    # load the page can read the token out of the source.
    html = f.read_text(encoding="utf-8")
    injected = ("<script>window.__DEMO_TOKEN__ = "
                + json.dumps(settings.demo_token or "")
                + ";</script></head>")
    return HTMLResponse(html.replace("</head>", injected, 1))


@app.get("/chat", response_class=HTMLResponse, include_in_schema=False)
async def chat_ui():
    """Full-width conversational view.

    The dashboard carries one question in a side card, which is the wrong
    shape for a platform whose problem statement is conversational. This is
    the same surface with room for a thread, and it shares the dashboard's
    language detection and provenance chips.
    """
    f = FRONTEND / "chat.html"
    if not f.exists():
        raise HTTPException(404, "chat UI not built")
    html = f.read_text(encoding="utf-8")
    injected = ("<script>window.__DEMO_TOKEN__ = "
                + json.dumps(settings.demo_token or "")
                + ";</script></head>")
    return HTMLResponse(html.replace("</head>", injected, 1))


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
