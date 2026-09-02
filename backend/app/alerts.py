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
from . import i18n
from .providers import nwp
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
    """Spatial + severity match. ST_DWithin stands in as a haversine here."""
    out = []
    for sub in SUBSCRIPTIONS.values():
        if SEV_ORDER.index(event.severity) < SEV_ORDER.index(sub.min_severity):
            continue
        d = haversine_km(event.lat, event.lon, sub.lat, sub.lon)
        if d <= event.radius_km + sub.radius_km:
            out.append(sub)
    return out


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
