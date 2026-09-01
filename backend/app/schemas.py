"""Typed contracts.

The single most important type here is `Provenance`. Every numeric fact that
leaves this service carries one. If a fact has no provenance it does not get
rendered -- that is the whole anti-hallucination guarantee.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- provenance
class Provenance(BaseModel):
    source: str                       # "IMD", "GFS (NCEP) via Open-Meteo", "ERA5"
    product: str                      # "district nowcast", "0.25deg deterministic"
    issued_at: datetime | None = None  # model run / bulletin issue time
    valid_until: datetime | None = None
    url: str | None = None
    authoritative: bool = False       # True only for IMD / national met service


class Fact(BaseModel):
    """A single grounded value. The LLM may reorder and translate these; it
    may never invent one or alter `value`."""
    key: str
    value: Any
    unit: str | None = None
    label: str | None = None
    provenance: Provenance


# ---------------------------------------------------------------- intents
class Intent(str, Enum):
    CURRENT = "current_weather"
    FORECAST = "forecast"
    WARNING = "warnings"
    ADVISORY = "advisory"
    CLIMATE = "climate_trend"
    AIR_QUALITY = "air_quality"
    SUBSCRIBE = "subscribe_alerts"
    COMPARE = "compare_locations"
    UNKNOWN = "unknown"


class Persona(str, Enum):
    GENERAL = "general"
    FARMER = "farmer"
    FISHERMAN = "fisherman"
    AVIATION = "aviation"
    URBAN = "urban"          # smart-city / civic ops
    RESEARCHER = "researcher"


class Severity(str, Enum):
    NONE = "none"
    GREEN = "green"
    YELLOW = "yellow"      # be aware
    ORANGE = "orange"      # be prepared
    RED = "red"            # take action


class Place(BaseModel):
    name: str
    admin1: str | None = None
    country: str | None = "India"
    lat: float
    lon: float
    source: str = "geocoder"


class ParsedQuery(BaseModel):
    intent: Intent = Intent.UNKNOWN
    place_text: str | None = None
    place: Place | None = None
    persona: Persona = Persona.GENERAL
    lang: str = "en"
    horizon_days: int = 1
    day_offset: int = 0
    variables: list[str] = Field(default_factory=list)
    years_back: int = 30
    month: int | None = None
    raw: str = ""
    parser: Literal["rules", "llm"] = "rules"
    confidence: float = 0.0


# ---------------------------------------------------------------- responses
class Advisory(BaseModel):
    persona: Persona
    headline: str
    severity: Severity
    actions: list[str] = Field(default_factory=list)
    reason: str = ""


class ChatRequest(BaseModel):
    message: str
    lang: str = "en"
    persona: Persona = Persona.GENERAL
    lat: float | None = None
    lon: float | None = None
    session_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    answer_en: str
    intent: Intent
    persona: Persona
    lang: str
    place: Place | None = None
    facts: list[Fact] = Field(default_factory=list)
    advisory: Advisory | None = None
    severity: Severity = Severity.NONE
    sources: list[Provenance] = Field(default_factory=list)
    latency_ms: int = 0
    cached: bool = False
    degraded: list[str] = Field(default_factory=list)
    chart: dict[str, Any] | None = None
    followups: list[str] = Field(default_factory=list)


class Subscription(BaseModel):
    id: str
    channel: Literal["push", "sms", "ivr", "whatsapp"] = "push"
    address: str
    lat: float
    lon: float
    radius_km: float = 25.0
    lang: str = "en"
    persona: Persona = Persona.GENERAL
    min_severity: Severity = Severity.YELLOW
    created_at: datetime


class AlertEvent(BaseModel):
    id: str
    headline: str
    severity: Severity
    area: str
    lat: float
    lon: float
    radius_km: float
    effective: datetime
    expires: datetime | None = None
    provenance: Provenance
    matched_subscribers: int = 0
