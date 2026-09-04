"""Push dissemination: the half a chatbot alone cannot do.

A conversational interface is pull-only -- it helps the person who thought to
ask. Early warning has to reach the person who did not. This module is the
subscribe + geofence-match + fan-out engine that turns WeatherGPT from an
assistant into a dissemination channel.

Flow in production:
    WIS2.0 MQTT (wis2/.../a/wis2/in-imd/...) or IMD CAP feed
        -> normalise to AlertEvent (CAP 1.2 fields preserved)
        -> PostGIS ST_DWithin against the subscription table (spatial index)
        -> per-subscriber language + persona rendering (i18n templates)
        -> channel fan-out: FCM push | SMS gateway | IVR outdial | WhatsApp
        -> delivery receipts written back for audit

This demo keeps subscriptions in memory and evaluates the geofence with a
haversine, so the whole loop is observable without any infrastructure.
"""
from __future__ import annotations

import asyncio
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone

from . import advisory as adv
from .config import get_settings
from . import i18n
from .providers import nwp, sachet
from .schemas import (AlertEvent, Persona, Provenance, Severity, Subscription)

log = logging.getLogger(__name__)

SUBSCRIPTIONS: dict[str, Subscription] = {}
DELIVERY_LOG: list[dict] = []

# Live WebSocket clients. This is what makes /ws/alerts a push channel rather
# than a request/response socket: without it, dispatch() could only ever answer
# the client that asked, so an alert fired from curl or a second tab reached
# nobody.
#
# Duck-typed on purpose -- anything with an async send_json() works, so this
# module stays free of a FastAPI import and remains unit-testable without a
# transport. Set, not list, so a double-register is harmless.
#
# In-process, like SUBSCRIPTIONS and DELIVERY_LOG, and for the same reason it
# is safe: the service runs a single worker. With more than one, a client
# connected to worker A would miss an alert dispatched on worker B, which is
# the same partitioning bug --workers 1 already guards against.
CONNECTIONS: set = set()


def register(ws) -> None:
    CONNECTIONS.add(ws)


def unregister(ws) -> None:
    CONNECTIONS.discard(ws)


async def broadcast(payload: dict) -> int:
    """Push one payload to every live connection. Returns the number reached.

    A send can fail because a client vanished between the registry check and
    the write -- a closed tab, a dropped mobile connection, a proxy timeout.
    One such failure must not abort the fan-out, so every send is awaited
    concurrently with return_exceptions=True and the losers are evicted rather
    than raised. Iterating a snapshot keeps the eviction from mutating the set
    mid-iteration.
    """
    targets = list(CONNECTIONS)
    if not targets:
        return 0
    results = await asyncio.gather(
        *(ws.send_json(payload) for ws in targets), return_exceptions=True)
    delivered = 0
    for ws, outcome in zip(targets, results):
        if isinstance(outcome, BaseException):
            log.info("dropping dead websocket: %r", outcome)
            unregister(ws)
        else:
            delivered += 1
    return delivered


def _schedule_broadcast(payload: dict) -> None:
    """Fire-and-forget the broadcast from synchronous dispatch().

    dispatch() is sync and is called from async request handlers, so the loop
    is already running and create_task is the right bridge. Outside a loop --
    a direct unit-test call -- there is nothing to push to and nothing to do,
    so this is a no-op rather than an error. The task reference is held until
    completion because asyncio only keeps a weak reference to tasks.
    """
    if not CONNECTIONS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(broadcast(payload))
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


_PENDING: set = set()

SEV_ORDER = [Severity.NONE, Severity.GREEN, Severity.YELLOW,
             Severity.ORANGE, Severity.RED]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def subscribe(address: str, lat: float, lon: float, *, channel: str = "push",
              radius_km: float = 25.0, lang: str = "en",
              persona: Persona = Persona.GENERAL,
              min_severity: Severity = Severity.YELLOW) -> Subscription:
    sub = Subscription(
        id=uuid.uuid4().hex[:10], channel=channel, address=address, lat=lat, lon=lon,
        radius_km=radius_km, lang=lang, persona=persona, min_severity=min_severity,
        created_at=datetime.now(timezone.utc),
    )
    SUBSCRIPTIONS[sub.id] = sub
    return sub


def unsubscribe(sub_id: str) -> bool:
    return SUBSCRIPTIONS.pop(sub_id, None) is not None


def match(event: AlertEvent) -> list[Subscription]:
    """Spatial + severity match.

    Two tiers. When the event carries its real CAP footprint the test is an
    exact point-in-polygon: a subscriber is affected only if they stand inside
    the area the issuing agency actually drew. Without a footprint it falls
    back to comparing distance against the disc derived from `area_covered`,
    which over-matches -- a 14-district Rajasthan advisory becomes a ~184 km
    circle covering places the advisory never named.

    The fallback is deliberately kept: the global SACHET feed does not publish
    `area_json`, so a coarse hit is what makes a candidate worth confirming.
    """
    out = []
    for sub in SUBSCRIPTIONS.values():
        if SEV_ORDER.index(event.severity) < SEV_ORDER.index(sub.min_severity):
            continue
        if event.geometry:
            # Exact footprint: inside it, or within the distance this
            # subscriber asked to hear about. Containment says where the
            # hazard is; the subscriber's radius says how far away they
            # still care.
            gap = sachet.distance_to_geometry_km(sub.lat, sub.lon, event.geometry)
            if gap is not None:
                if gap <= sub.radius_km:
                    out.append(sub)
                continue
        d = haversine_km(event.lat, event.lon, sub.lat, sub.lon)
        if d <= event.radius_km + sub.radius_km:
            out.append(sub)
    return out


async def confirm_precise(event: AlertEvent) -> AlertEvent:
    """Upgrade a coarse event to its exact footprint before dispatch.

    The global feed gives no geometry, so a disc match is only a candidate.
    This asks SACHET for the alerts affecting each distinct subscriber point
    the disc flagged; if this alert is among them it adopts the real polygon
    returned alongside it. Responses are cached per rounded point, so a town
    full of subscribers costs one upstream call.

    Returns the event unchanged when the feed has nothing to add -- the disc
    result then stands, which is the previous behaviour rather than a silent
    drop.
    """
    s = get_settings()
    if event.geometry or not s.sachet_precise_match:
        return event
    seen_pts: set = set()
    for sub in match(event):
        pt = (round(sub.lat, 2), round(sub.lon, 2))
        if pt in seen_pts:
            continue
        seen_pts.add(pt)
        try:
            nearby = await sachet.alerts_for_point(sub.lat, sub.lon, sub.radius_km)
        except Exception:                          # noqa: BLE001
            continue
        for cand in nearby:
            if cand.id == event.id and cand.geometry:
                event.geometry = cand.geometry
                return event
    return event


def render_for(sub: Subscription, event: AlertEvent) -> str:
    """Per-subscriber rendering: language template + persona action line."""
    word = i18n.severity_word(event.severity, sub.lang)
    base = i18n.t("warning_active", sub.lang, place=event.area,
                  date=event.effective.strftime("%d %b"),
                  severity_word=word, reason=event.headline)
    action = {
        Persona.FARMER: "Move harvested produce under cover and postpone spraying.",
        Persona.FISHERMAN: "Do not put to sea. Return to the nearest harbour.",
        Persona.AVIATION: "Expect operational impact; review alternates.",
        Persona.URBAN: "Pre-position pumps and issue a commuter advisory.",
        Persona.GENERAL: "Stay indoors during the peak and avoid low-lying roads.",
        Persona.RESEARCHER: "Event logged for verification against observations.",
    }[sub.persona]
    return f"{base} {action}"


def dispatch(event: AlertEvent) -> dict:
    """Fan out. Channels are stubbed to the delivery log for the demo; the
    adapter signature is identical to the production FCM/SMS/IVR clients."""
    matched = match(event)
    event.matched_subscribers = len(matched)
    sent = []
    for sub in matched:
        message = render_for(sub, event)
        record = {
            "alert_id": event.id, "subscription_id": sub.id,
            "channel": sub.channel, "address": sub.address, "lang": sub.lang,
            "persona": sub.persona.value, "message": message,
            "severity": event.severity.value,
            "distance_km": round(haversine_km(event.lat, event.lon, sub.lat, sub.lon), 1),
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "status": "queued",
        }
        DELIVERY_LOG.append(record)
        sent.append(record)
    result = {"alert": event.model_dump(mode="json"), "delivered": sent,
              "matched": len(matched)}
    # Push to every open UI. Deliberately after the result is assembled and
    # deliberately not part of the return value: the HTTP callers and the
    # smoke suite depend on this exact shape, so broadcasting is a side effect
    # that adds a channel without changing the contract.
    _schedule_broadcast({"type": "alert", **result})
    return result


async def scan_location(lat: float, lon: float, area: str) -> AlertEvent | None:
    """Threshold monitor: the same classifier the chat path uses, run on a
    schedule instead of on a question. In production this is triggered by the
    arrival of a new model run or a CAP message, not by a timer."""
    fc = await nwp.forecast(lat, lon, days=3)
    worst, worst_day, worst_why = Severity.GREEN, None, []
    for d in fc["days"]:
        sev, why = adv.classify(d)
        if SEV_ORDER.index(sev) > SEV_ORDER.index(worst):
            worst, worst_day, worst_why = sev, d, why
    if worst in (Severity.NONE, Severity.GREEN) or not worst_day:
        return None
    return AlertEvent(
        id=uuid.uuid4().hex[:8],
        headline="; ".join(worst_why),
        severity=worst,
        area=area, lat=lat, lon=lon, radius_km=25.0,
        effective=datetime.fromisoformat(worst_day["date"]),
        expires=datetime.fromisoformat(worst_day["date"]) + timedelta(days=1),
        provenance=fc["provenance"],
    )


# ---------------------------------------------------------------- SACHET poll
# SACHET is HTTP-pull, not push: there is no socket the feed can call back on,
# so "how does the data reach the agent" is answered by a timer. One GET per
# interval, diffed against what we have already fanned out.
#
# In-process like SUBSCRIPTIONS, and for the same single-worker reason -- with
# two workers each would keep its own seen-set and dispatch the same alert
# twice. That is the same partitioning bug --workers 1 already guards against.
SEEN_ALERT_IDS: set[str] = set()
_SACHET_STATE: dict = {"polls": 0, "last_poll": None, "seeded": False,
                       "last_error": None, "dispatched_total": 0}


async def poll_sachet(*, replay: bool = False) -> dict:
    """Fetch the live feed and dispatch anything we have not seen before.

    The first poll seeds the seen-set without dispatching: a live feed always
    carries alerts already in force, and fanning those out on boot would page
    every subscriber on every restart. `replay=True` overrides that for a
    demo, so the dissemination path can be shown against real warnings.
    """
    s = get_settings()
    try:
        events = await sachet.recent_events()
    except Exception as exc:                       # noqa: BLE001
        _SACHET_STATE["last_error"] = repr(exc)
        log.warning("SACHET poll failed: %r", exc)
        return {"polled": 0, "new": 0, "dispatched": 0, "seeded": False,
                "error": repr(exc)}

    _SACHET_STATE["polls"] += 1
    _SACHET_STATE["last_poll"] = datetime.now(timezone.utc).isoformat()
    _SACHET_STATE["last_error"] = None

    fresh = [e for e in events if e.id not in SEEN_ALERT_IDS]
    for e in events:
        SEEN_ALERT_IDS.add(e.id)

    seeding = (not _SACHET_STATE["seeded"]
               and s.sachet_seed_on_first_poll and not replay)
    _SACHET_STATE["seeded"] = True
    if seeding:
        log.info("SACHET seeded with %d alerts already in force", len(events))
        return {"polled": len(events), "new": len(fresh), "dispatched": 0,
                "seeded": True}

    to_send = events if replay else fresh
    # Confirm each candidate against its real polygon before fanning out.
    results = [dispatch(await confirm_precise(e)) for e in to_send]
    delivered = sum(r["matched"] for r in results)
    _SACHET_STATE["dispatched_total"] += len(results)
    return {"polled": len(events), "new": len(fresh),
            "dispatched": len(results), "deliveries": delivered,
            "seeded": False, "replay": replay}


async def sachet_loop() -> None:
    """Background poll loop. Started on app startup; cancelled on shutdown."""
    s = get_settings()
    interval = max(60, s.sachet_poll_seconds)
    log.info("SACHET poll loop starting (every %ds)", interval)
    while True:
        try:
            await poll_sachet()
        except asyncio.CancelledError:
            raise
        except Exception:                          # noqa: BLE001
            log.exception("SACHET poll loop iteration failed")
        await asyncio.sleep(interval)


def sachet_state() -> dict:
    return dict(_SACHET_STATE, seen_ids=len(SEEN_ALERT_IDS))


def simulate(area: str, lat: float, lon: float, severity: Severity,
             headline: str) -> AlertEvent:
    """Inject a synthetic CAP-shaped event -- used to demo the dissemination
    path on a calm day without waiting for real severe weather."""
    now = datetime.now(timezone.utc)
    return AlertEvent(
        id=uuid.uuid4().hex[:8], headline=headline, severity=severity, area=area,
        lat=lat, lon=lon, radius_km=50.0, effective=now,
        expires=now + timedelta(hours=6),
        provenance=Provenance(source="Simulated CAP feed (demo)",
                              product="CAP 1.2 alert", issued_at=now),
    )
