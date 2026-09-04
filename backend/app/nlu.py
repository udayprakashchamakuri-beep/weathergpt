"""Query understanding.

THE GROUNDING CONTRACT
----------------------
The LLM is a *router*, never a source. It converts free text into a typed
`ParsedQuery` and, optionally, rephrases an already-grounded sentence. It is
never shown raw data and asked "what's the weather" -- so it has nothing to
hallucinate with. Every number in every answer comes from `tools.py`, carries
a `Provenance`, and is rendered by a deterministic template.

Concretely:
    user text --> [LLM or rules] --> ParsedQuery  (typed, validated)
                                        |
                                        v
                              tools.py (deterministic)
                                        |
                                        v
                              Fact[] + Provenance[]
                                        |
                                        v
                          template renderer (deterministic)
                                        |
                                        v
                   [optional] LLM rewrite, constrained: may not add,
                   remove or alter any numeral present in the input

A deterministic rule parser runs first. It resolves the large majority of
real traffic ("kal barish hogi kya", "weather in Guntur", "cyclone alert")
with zero LLM cost and ~1 ms latency; the LLM is only invoked when rules are
low-confidence. With no LLM configured the system still works end to end --
which is also what makes it deployable inside an IMD network with no
outbound internet.
"""
from __future__ import annotations

import logging

import json
import re

import httpx

from .config import get_settings
from .schemas import Intent, ParsedQuery, Persona

log = logging.getLogger(__name__)

# Why the last LLM routing attempt failed, surfaced through /api/health.
# A decommissioned model and "the rules were confident" look identical from
# outside otherwise -- which is how a dead model name sat unnoticed behind a
# provider the health endpoint reported as configured.
LLM_LAST_ERROR: str | None = None

# --------------------------------------------------------------- lexicons
# Romanised Hindi/Telugu/Tamil/Bengali/Marathi included on purpose: that is
# how people actually type on Indian keyboards.
INTENT_LEXICON: dict[Intent, list[str]] = {
    Intent.WARNING: [
        "warning", "alert", "cyclone", "storm", "flood", "heatwave", "heat wave",
        "cold wave", "thunderstorm", "lightning", "danger", "red alert",
        "chetavani", "chetavni", "toofan", "aandhi", "baadh", "tufan",
        "எச்சரிக்கை", "সতর্কতা", "चेतावनी", "हवामान इशारा", "హెచ్చరిక",
    ],
    Intent.ADVISORY: [
        "advisory", "should i", "can i", "is it safe", "safe to", "spray",
        "sow", "sowing", "harvest", "irrigate", "irrigation", "fertilizer",
        "pesticide", "go fishing", "sail", "put to sea", "fly", "flight",
        "takeoff", "landing", "match", "picnic", "travel", "commute",
        "kya karu", "salah", "sallah", "vyavasayam",
    ],
    Intent.CLIMATE: [
        "trend", "climate", "average", "historical", "history", "over the years",
        "last 30 years", "decade", "normal", "anomaly", "changed", "changing",
        "monsoon onset", "compared to", "since 19", "since 20", "warming",
    ],
    Intent.AIR_QUALITY: [
        "air quality", "aqi", "pollution", "pm2.5", "pm 2.5", "pm10", "smog",
        "pradushan", "hawa", "vayu",
    ],
    Intent.FORECAST: [
        "forecast", "tomorrow", "next", "week", "will it", "going to rain",
        "coming days", "weekend", "day after", "kal", "parso", "barish",
        "baarish", "varsha", "vaana", "mazhai", "brishti", "paus", "aage",
        "predict", "outlook", "5 day", "7 day", "10 day", "repu", "naale",
        "udya", "parso",
        # Native script. Without these a question typed in Telugu or Hindi
        # matched no forecast term and fell through to current conditions --
        # "రేపు వర్షం పడుతుందా" (will it rain tomorrow) returned today's
        # temperature. Romanised input was already covered; script input was
        # not, which is the half of our own users who type in their language.
        "రేపు", "వర్షం", "వాన", "ఎల్లుండి", "సూచన", "పడుతుందా", "వారం",
        "कल", "परसों", "बारिश", "होगी", "अगले", "सप्ताह", "पूर्वानुमान",
        "நாளை", "மழை", "முன்னறிவிப்பு", "அடுத்த",
        "আগামীকাল", "বৃষ্টি", "পরশু", "সপ্তাহ",
        "उद्या", "पाऊस", "परवा", "पुढील",
    ],
    Intent.CURRENT: [
        "now", "right now", "current", "currently", "today", "outside",
        "temperature", "temp", "humidity", "wind", "hot", "cold", "raining",
        "abhi", "aaj", "ippudu", "ippo", "ekhon", "aata", "mausam", "weather",
        "ఇప్పుడు", "ప్రస్తుతం", "ఈరోజు", "వాతావరణం",
        "अभी", "आज", "मौसम",
        "இப்போது", "இன்று", "வானிலை",
        "এখন", "আজ", "আবহাওয়া",
        "आता", "हवामान",
    ],
    Intent.SUBSCRIBE: [
        "subscribe", "notify me", "alert me", "remind me", "send me alerts",
        "sign me up", "register for alerts", "keep me posted",
    ],
}

PERSONA_LEXICON: dict[Persona, list[str]] = {
    Persona.FARMER: ["farm", "farmer", "crop", "sow", "sowing", "harvest",
                     "irrigate", "irrigation", "spray", "pesticide", "paddy",
                     "cotton", "wheat", "kisan", "kheti", "fasal", "rythu",
                     "pantalu", "vyavasayam", "field", "agri", "agriculture"],
    Persona.FISHERMAN: ["fish", "fisherman", "fishermen", "boat", "sea", "sail",
                        "trawler", "catamaran", "coast", "harbour", "harbor",
                        "matsya", "machhuara", "jaladhi", "put to sea"],
    Persona.AVIATION: ["flight", "fly", "aviation", "pilot", "airport", "runway",
                       "takeoff", "take off", "landing", "metar", "taf",
                       "crosswind", "visibility", "ceiling", "briefing"],
    Persona.URBAN: ["city", "traffic", "waterlogging", "water logging", "drain",
                    "commute", "municipal", "civic", "ward", "smart city",
                    "power cut", "outage"],
    Persona.RESEARCHER: ["dataset", "anomaly", "reanalysis", "era5", "gridded",
                         "correlation", "time series", "statistics", "significance"],
}

DAY_WORDS = {
    "today": 0, "aaj": 0, "now": 0, "tonight": 0,
    "tomorrow": 1, "kal": 1, "repu": 1, "naale": 1, "udya": 1, "kaal": 1,
    "రేపు": 1, "कल": 1, "நாளை": 1, "আগামীকাল": 1, "उद्या": 1,
    "ఎల్లుండి": 2, "परसों": 2, "পরশু": 2, "परवा": 2,
    "day after tomorrow": 2, "parso": 2, "ellundi": 2,
}

# "in|at|for|near <Place>" and "<Place> ka|ki|me|mein weather"
PLACE_PATTERNS = [
    re.compile(r"\b(?:in|at|for|near|around|over)\s+([A-Za-z][A-Za-z\s\.\-']{1,32}?)"
               r"(?=\s+(?:tomorrow|today|now|next|this|for|over|during|kal|aaj)\b|[?,.!]|$)",
               re.I),
    re.compile(r"^([A-Za-z][A-Za-z\s\.\-']{1,32}?)\s+(?:weather|forecast|mausam|"
               r"temperature|rain|barish|aqi)\b", re.I),
    re.compile(r"\b([A-Za-z][A-Za-z\s\.\-']{1,32}?)\s+(?:ka|ki|me|mein|mai)\s+"
               r"(?:mausam|weather|barish)\b", re.I),
]

STOPWORD_PLACES = {
    "the", "a", "an", "me", "my", "us", "you", "here", "there", "it", "this",
    "that", "weather", "forecast", "rain", "temperature", "today", "tomorrow",
    "now", "next", "week", "days", "day", "alert", "warning", "please",
}

# Kept in step with the native-script terms in INTENT_TERMS. They drifted
# once: script words were added for intent matching but not here, so
# "ఈరోజు వర్షం పడుతుందా" was understood as a forecast question and answered
# in English -- and, because the response cache is keyed on the parsed
# meaning including language, it then collided with an earlier English
# forecast and returned that answer verbatim. Add script words to both.
LANG_HINTS = {
    "hi": ["kya", "hai", "kal", "aaj", "barish", "baarish", "mausam", "kaisa",
           "hoga", "chetavani", "mujhe", "बारिश", "मौसम", "कल", "आज", "अभी",
           "परसों", "होगी", "सप्ताह", "पूर्वानुमान", "चेतावनी", "तापमान"],
    "te": ["repu", "ippudu", "vaana", "vaanam", "ela", "undi", "rythu",
           "వాన", "వాతావరణం", "రేపు", "వర్షం", "ఈరోజు", "ఇప్పుడు",
           "ప్రస్తుతం", "ఎల్లుండి", "పడుతుందా", "సూచన", "వారం",
           "హెచ్చరిక", "ఉష్ణోగ్రత", "గాలి", "తేమ"],
    "ta": ["mazhai", "innaiku", "naale", "eppadi", "மழை", "வானிலை", "நாளை",
           "இன்று", "இப்போது", "அடுத்த", "முன்னறிவிப்பு", "எச்சரிக்கை"],
    "bn": ["brishti", "aaj ke", "kemon", "ekhon", "বৃষ্টি", "আবহাওয়া", "আজ",
           "এখন", "আগামীকাল", "পরশু", "সপ্তাহ", "সতর্কতা"],
    "mr": ["paus", "udya", "kasa", "aahe", "पाऊस", "हवामान", "उद्या", "आज",
           "आता", "परवा", "पुढील", "इशारा"],
}


def detect_lang(text: str, declared: str = "en") -> str:
    if declared and declared != "auto":
        return declared
    low = text.lower()
    for lang, hints in LANG_HINTS.items():
        if any(h in low for h in hints):
            return lang
    return "en"


def _score(text: str, terms: list[str]) -> int:
    return sum(1 for t in terms if t in text)


def gazetteer_sweep(text: str) -> str | None:
    """Find any known Indian place name anywhere in the sentence.

    Word-order-free, so it handles SOV languages and code-mixed input where
    the English preposition patterns do not apply. Longest match wins so
    'New Delhi' beats 'Delhi'.
    """
    from .providers.geocode import GAZETTEER      # local import: avoids a cycle

    low = re.sub(r"[^a-z ]", " ", text.lower())
    padded = f" {low} "
    best = None
    for name in GAZETTEER:
        if f" {name} " in padded and (best is None or len(name) > len(best)):
            best = name
    return best.title() if best else None


def parse_rules(text: str, declared_lang: str = "en",
                declared_persona: Persona = Persona.GENERAL) -> ParsedQuery:
    low = f" {text.lower().strip()} "

    # ---- intent ---------------------------------------------------------
    scores = {i: _score(low, terms) for i, terms in INTENT_LEXICON.items()}
    intent, best = max(scores.items(), key=lambda kv: kv[1])
    if best == 0:
        intent = Intent.CURRENT if "?" in text or len(text.split()) < 8 else Intent.UNKNOWN
        confidence = 0.25
    else:
        runner_up = sorted(scores.values(), reverse=True)[1] if len(scores) > 1 else 0
        confidence = min(0.95, 0.55 + 0.15 * (best - runner_up) + 0.05 * best)

    # ---- persona --------------------------------------------------------
    persona = declared_persona
    if persona == Persona.GENERAL:
        pscores = {p: _score(low, terms) for p, terms in PERSONA_LEXICON.items()}
        pbest = max(pscores.items(), key=lambda kv: kv[1])
        if pbest[1] > 0:
            persona = pbest[0]

    # ---- horizon --------------------------------------------------------
    day_offset = 0
    for word, off in sorted(DAY_WORDS.items(), key=lambda kv: -len(kv[0])):
        if f" {word} " in low:
            day_offset = off
            break
    horizon = 1
    if m := re.search(r"\b(\d{1,2})\s*(?:-|\s)?\s*day", low):
        horizon = max(1, min(int(m.group(1)), 16))
    elif "week" in low:
        horizon = 7
    elif intent == Intent.FORECAST:
        horizon = max(3, day_offset + 1)
    if day_offset:
        horizon = max(horizon, day_offset + 1)

    # ---- climate window -------------------------------------------------
    years_back = 30
    if m := re.search(r"\b(\d{2,3})\s*years?\b", low):
        years_back = max(5, min(int(m.group(1)), 60))
    month = None
    for i, name in enumerate(["january", "february", "march", "april", "may",
                              "june", "july", "august", "september", "october",
                              "november", "december"], start=1):
        if name in low or (name[:3] in low.split()):
            month = i
            break

    # ---- place ----------------------------------------------------------
    # 1) syntactic patterns ("in Guntur", "Guntur ka mausam")
    place_text = None
    for pat in PLACE_PATTERNS:
        if m := pat.search(text):
            cand = m.group(1).strip(" .,'-")
            if cand and cand.lower() not in STOPWORD_PLACES and len(cand) > 2:
                place_text = cand
                break

    # 2) gazetteer sweep -- catches word orders the patterns miss, which is
    #    most transliterated Indian-language input ("repu Warangal lo vaana").
    if not place_text:
        place_text = gazetteer_sweep(text)

    return ParsedQuery(
        intent=intent,
        place_text=place_text,
        persona=persona,
        lang=detect_lang(text, declared_lang),
        horizon_days=horizon,
        day_offset=day_offset,
        years_back=years_back,
        month=month,
        raw=text,
        parser="rules",
        confidence=confidence,
    )


# ------------------------------------------------------------------- LLM
ROUTER_SYSTEM = """You are the query router for WeatherGPT, an Indian \
meteorological assistant. You do NOT answer weather questions and you never \
state any weather value. Convert the user's message into JSON only:

{"intent": one of ["current_weather","forecast","warnings","advisory",
 "climate_trend","air_quality","subscribe_alerts","compare_locations","unknown"],
 "place_text": the place name transliterated into English/Latin script, with
   any grammatical suffix removed -- "ఖమ్మంలో" -> "Khammam", "విజయవాడలో" ->
   "Vijayawada", "गुंटूर में" -> "Guntur". Never return it in the original
   script: the gazetteer and the geocoder are both Latin-only, so a name in
   Devanagari or Telugu resolves to nothing and the user gets asked which
   district they meant. null if no place is mentioned,
 "persona": one of ["general","farmer","fisherman","aviation","urban","researcher"],
 "lang": ISO-639-1 code of the user's language,
 "day_offset": integer days ahead (0 = today),
 "horizon_days": integer 1-16,
 "years_back": integer for climate questions,
 "month": 1-12 or null,
 "variables": subset of ["rain","temperature","wind","humidity","aqi"] --
   which quantities the question is actually about. Include "rain" whenever
   the user is asking whether it will rain, however they phrase it ("will my
   cotton get soaked", "can I dry the grain", "do I need an umbrella").}

Return JSON and nothing else."""


async def parse_llm(text: str, fallback: ParsedQuery) -> ParsedQuery:
    global LLM_LAST_ERROR
    s = get_settings()
    if s.llm_provider == "none" or not s.llm_api_key:
        return fallback

    base = s.llm_base_url or {
        "groq": "https://api.groq.com/openai/v1",
        "openai": "https://api.openai.com/v1",
        "ollama": "http://localhost:11434/v1",
    }.get(s.llm_provider, "https://api.openai.com/v1")

    try:
        async with httpx.AsyncClient(timeout=s.http_timeout) as client:
            r = await client.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {s.llm_api_key}"},
                json={
                    "model": s.llm_model,
                    "temperature": 0,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": ROUTER_SYSTEM},
                        {"role": "user", "content": text},
                    ],
                },
            )
            r.raise_for_status()
            payload = json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as exc:                       # noqa: BLE001
        LLM_LAST_ERROR = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("LLM routing failed, falling back to rules: %r", exc)
        return fallback

    try:
        return ParsedQuery(
            intent=Intent(payload.get("intent", fallback.intent)),
            place_text=payload.get("place_text") or fallback.place_text,
            persona=Persona(payload.get("persona", fallback.persona)),
            lang=payload.get("lang") or fallback.lang,
            horizon_days=int(payload.get("horizon_days") or fallback.horizon_days),
            day_offset=int(payload.get("day_offset") or fallback.day_offset),
            years_back=int(payload.get("years_back") or fallback.years_back),
            month=payload.get("month") or fallback.month,
            # What the question is about, so downstream answers can lead with
            # a verdict instead of a table. Keyword lists cannot anticipate
            # every phrasing; the router already understood the sentence, so
            # ask it rather than guessing again from the raw text.
            variables=[v for v in (payload.get("variables") or [])
                       if isinstance(v, str)] or fallback.variables,
            raw=text,
            parser="llm",
            confidence=0.9,
        )
    except Exception as exc:                       # noqa: BLE001
        LLM_LAST_ERROR = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("LLM routing failed, falling back to rules: %r", exc)
        return fallback


NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def numerals_preserved(original: str, rewritten: str) -> bool:
    """Guard for any LLM rewrite: the numeral multiset must be identical.

    If the model drops, invents or alters a number, the rewrite is discarded
    and the deterministic template is served instead. Cheap, total, and it
    makes 'the LLM hallucinated a rainfall figure' structurally impossible.
    """
    return sorted(NUM_RE.findall(original)) == sorted(NUM_RE.findall(rewritten))


async def parse(text: str, lang: str = "en",
                persona: Persona = Persona.GENERAL) -> ParsedQuery:
    rules = parse_rules(text, lang, persona)

    # Confidence measures the intent match, not the whole parse. Adding
    # native-script terms made the rules confident about Telugu and Hindi
    # questions, which stopped them escalating -- while the place-name
    # patterns they use are Latin-only, so "ఖమ్మంలో రేపు వర్షం పడుతుందా"
    # came back as a confident forecast with no place at all, and the user
    # was asked which district they meant. Escalate when the intent is clear
    # but the place is missing from a question written in another script:
    # the model reads those names, the regexes cannot.
    needs_place = (rules.place_text is None
                   and any(ord(ch) > 0x0900 for ch in text))
    if rules.confidence >= 0.7 and not needs_place:
        return rules
    return await parse_llm(text, rules)
