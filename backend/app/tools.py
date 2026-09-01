"""Deterministic answer construction.

One function per intent. Each returns (english_text, localised_text, facts,
advisory, severity, chart). No function here calls an LLM and no function
here invents a value: every number it emits came out of a provider and is
wrapped in a Fact carrying its Provenance.
"""
from __future__ import annotations

import asyncio

from . import advisory as adv
from . import i18n
from .providers import imd, openmeteo
from .schemas import (Advisory, Fact, Intent, ParsedQuery, Persona, Place,
                      Provenance, Severity)


def _fact(key, value, prov: Provenance, unit=None, label=None) -> Fact:
    return Fact(key=key, value=value, unit=unit, label=label, provenance=prov)


def _fmt(v, nd=0):
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


# ------------------------------------------------------------------ current
async def answer_current(q: ParsedQuery, place: Place) -> dict:
    # Concurrent, not sequential: the two upstream calls are independent, so
    # cold latency is one round trip rather than two. This is the single
    # biggest lever on p95 after the cache.
    cur, fc = await asyncio.gather(
        openmeteo.current(place.lat, place.lon),
        openmeteo.forecast(place.lat, place.lon, days=7),
    )
    prov = cur["provenance"]

    cond_en = cur["condition"]
    slots = {
        "place": place.name,
        "temp": _fmt(cur["temp_c"], 1),
        "condition": i18n.condition(cond_en, q.lang),
        "feels": _fmt(cur["feels_like_c"], 1),
        "humidity": _fmt(cur["humidity_pct"]),
        "wind": _fmt(cur["wind_kmh"]),
    }
    en_slots = dict(slots, condition=cond_en)

    text_en = i18n.t("current", "en", **en_slots)
    text_loc = i18n.t("current", q.lang, **slots)

    if (cur.get("precip_mm") or 0) > 0:
        text_en += f" Rain in the last hour: {cur['precip_mm']} mm."

    a = adv.build(q.persona, fc["days"], cur)

    facts = [
        _fact("temp_c", cur["temp_c"], prov, "°C", "Temperature"),
        _fact("feels_like_c", cur["feels_like_c"], prov, "°C", "Feels like"),
        _fact("humidity_pct", cur["humidity_pct"], prov, "%", "Humidity"),
        _fact("wind_kmh", cur["wind_kmh"], prov, "km/h", "Wind"),
        _fact("wind_gust_kmh", cur["wind_gust_kmh"], prov, "km/h", "Gusts"),
        _fact("pressure_hpa", cur["pressure_hpa"], prov, "hPa", "Pressure"),
        _fact("cloud_pct", cur["cloud_pct"], prov, "%", "Cloud cover"),
        _fact("condition", cond_en, prov, None, "Condition"),
    ]
    return {
        "en": text_en, "loc": text_loc, "facts": facts, "advisory": a,
        "severity": a.severity, "sources": [prov, fc["provenance"]],
        "chart": _hourly_chart(fc),
    }


def _hourly_chart(fc: dict) -> dict | None:
    h = fc.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return None
    n = min(24, len(times))
    return {
        "type": "hourly",
        "labels": [t[11:16] for t in times[:n]],
        "temp": (h.get("temperature_2m") or [])[:n],
        "precip": (h.get("precipitation") or [])[:n],
        "prob": (h.get("precipitation_probability") or [])[:n],
    }


# ----------------------------------------------------------------- forecast
async def answer_forecast(q: ParsedQuery, place: Place) -> dict:
    days_n = max(q.horizon_days, q.day_offset + 1)
    # Always pull a full week: the answer is trimmed to what was asked, but the
    # chart and the "next suitable window" search need the wider horizon, and
    # the wider pull caches once for every narrower question about this place.
    fc = await openmeteo.forecast(place.lat, place.lon, days=max(7, days_n))
    prov = fc["provenance"]
    days = fc["days"]

    if q.day_offset and q.day_offset < len(days):
        shown = [days[q.day_offset]]
    else:
        shown = days[: min(days_n, 7)]

    lead_en = i18n.t("forecast_lead", "en", place=place.name, n=len(shown))
    lead_loc = i18n.t("forecast_lead", q.lang, place=place.name, n=len(shown))

    lines_en, lines_loc = [], []
    for d in shown:
        s = {
            "date": d["date"], "tmin": _fmt(d["tmin_c"]), "tmax": _fmt(d["tmax_c"]),
            "rain": _fmt(d["rain_mm"], 1), "prob": _fmt(d["rain_prob_pct"]),
            "wind": _fmt(d["wind_max_kmh"]),
        }
        lines_en.append(i18n.t("forecast_day", "en", condition=d["condition"], **s))
        lines_loc.append(i18n.t("forecast_day", q.lang,
                                condition=i18n.condition(d["condition"], q.lang), **s))

    # Advisory is built on the days actually shown, so headline severity and
    # the reported severity badge can never disagree.
    a = adv.build(q.persona, shown or days)
    sev, _ = adv.classify(shown[0]) if shown else (Severity.GREEN, [])
    for d in shown[1:]:
        s2, _ = adv.classify(d)
        sev = adv._max_sev(sev, s2)

    facts = []
    for d in shown:
        facts += [
            _fact(f"tmax_{d['date']}", d["tmax_c"], prov, "°C", f"Max {d['date']}"),
            _fact(f"tmin_{d['date']}", d["tmin_c"], prov, "°C", f"Min {d['date']}"),
            _fact(f"rain_{d['date']}", d["rain_mm"], prov, "mm", f"Rain {d['date']}"),
        ]

    return {
        "en": lead_en + "\n" + "\n".join(lines_en),
        "loc": lead_loc + "\n" + "\n".join(lines_loc),
        "facts": facts, "advisory": a, "severity": sev,
        "sources": [prov],
        "chart": {
            "type": "daily",
            "labels": [d["date"][5:] for d in days[:7]],
            "tmax": [d["tmax_c"] for d in days[:7]],
            "tmin": [d["tmin_c"] for d in days[:7]],
            "rain": [d["rain_mm"] for d in days[:7]],
        },
    }


# ----------------------------------------------------------------- warnings
async def answer_warnings(q: ParsedQuery, place: Place) -> dict:
    """IMD district warnings when a key is configured; NWP-threshold
    screening otherwise. The answer always states which one it used."""
    sources: list[Provenance] = []
    degraded: list[str] = []

    imd_block = None
    if imd.available():
        district_id = imd.resolve_district_id(place.name)
        if district_id is None:
            degraded.append("IMD district-id mapping not loaded — cannot address "
                            f"the warning endpoint for '{place.name}', so no "
                            "official IMD warning was read")
        else:
            imd_block = await imd.district_warnings(district_id)
            if imd_block:
                sources.append(imd_block["provenance"])
            else:
                degraded.append("IMD warning endpoint returned nothing for this "
                                "district")
    else:
        degraded.append("IMD_API_KEY not set — screening NWP output against IMD "
                        "impact thresholds instead of reading official warnings")

    fc = await openmeteo.forecast(place.lat, place.lon, days=5)
    sources.append(fc["provenance"])

    hits = []
    worst = Severity.GREEN
    for d in fc["days"]:
        sev, why = adv.classify(d)
        if sev in (Severity.YELLOW, Severity.ORANGE, Severity.RED) and why:
            hits.append((d["date"], sev, "; ".join(why)))
            worst = adv._max_sev(worst, sev)

    if not hits:
        text_en = i18n.t("warning_none", "en", place=place.name)
        text_loc = i18n.t("warning_none", q.lang, place=place.name)
        a = adv.build(q.persona, fc["days"])
        return {"en": text_en, "loc": text_loc, "facts": [], "advisory": a,
                "severity": Severity.GREEN, "sources": sources, "degraded": degraded}

    lines_en, lines_loc = [], []
    for date, sev, reason in hits:
        lines_en.append(i18n.t("warning_active", "en", place=place.name, date=date,
                               severity_word=i18n.severity_word(sev, "en"),
                               reason=reason))
        lines_loc.append(i18n.t("warning_active", q.lang, place=place.name, date=date,
                                severity_word=i18n.severity_word(sev, q.lang),
                                reason=reason))

    a = adv.build(q.persona, fc["days"])
    facts = [_fact(f"warning_{d}", s.value, fc["provenance"], None, d)
             for d, s, _ in hits]
    return {"en": "\n".join(lines_en), "loc": "\n".join(lines_loc), "facts": facts,
            "advisory": a, "severity": worst, "sources": sources,
            "degraded": degraded}


# ----------------------------------------------------------------- advisory
async def answer_advisory(q: ParsedQuery, place: Place) -> dict:
    cur, fc = await asyncio.gather(
        openmeteo.current(place.lat, place.lon),
        openmeteo.forecast(place.lat, place.lon, days=7),
    )
    a = adv.build(q.persona, fc["days"], cur)

    # The bubble carries the headline only; the client renders the action list
    # from `advisory.actions` so the same payload drives SMS, IVR and push
    # without the text being duplicated on screen.
    head_en = i18n.t("advisory_lead", "en", headline=a.headline)
    head_loc = i18n.t("advisory_lead", q.lang, headline=a.headline)
    body = ""

    facts = [
        _fact("advisory_severity", a.severity.value, fc["provenance"], None, "Severity"),
        _fact("advisory_reason", a.reason, fc["provenance"], None, "Triggering condition"),
    ]
    return {"en": f"{head_en}{body}", "loc": f"{head_loc}{body}",
            "facts": facts, "advisory": a, "severity": a.severity,
            "sources": [cur["provenance"], fc["provenance"]],
            "chart": {"type": "daily",
                      "labels": [d["date"][5:] for d in fc["days"][:7]],
                      "tmax": [d["tmax_c"] for d in fc["days"][:7]],
                      "tmin": [d["tmin_c"] for d in fc["days"][:7]],
                      "rain": [d["rain_mm"] for d in fc["days"][:7]]}}


# -------------------------------------------------------------- air quality
async def answer_air_quality(q: ParsedQuery, place: Place) -> dict:
    aq = await openmeteo.air_quality(place.lat, place.lon)
    prov = aq["provenance"]
    slots = {"place": place.name, "pm25": _fmt(aq["pm2_5"], 1),
             "pm10": _fmt(aq["pm10"], 1), "band": aq["cpcb_band"]}
    facts = [
        _fact("pm2_5", aq["pm2_5"], prov, "µg/m³", "PM2.5"),
        _fact("pm10", aq["pm10"], prov, "µg/m³", "PM10"),
        _fact("no2", aq["no2"], prov, "µg/m³", "NO₂"),
        _fact("o3", aq["o3"], prov, "µg/m³", "Ozone"),
        _fact("cpcb_band", aq["cpcb_band"], prov, None, "CPCB band"),
    ]
    a = Advisory(persona=q.persona, severity=Severity.YELLOW
                 if aq["cpcb_band"] in ("Poor", "Very Poor", "Severe") else Severity.GREEN,
                 headline=f"Air quality is {aq['cpcb_band']}",
                 actions=["Limit outdoor exertion and use an N95 mask if you are "
                          "asthmatic or elderly."]
                 if aq["cpcb_band"] in ("Poor", "Very Poor", "Severe")
                 else ["No air-quality precautions needed."])
    return {"en": i18n.t("aqi", "en", **slots), "loc": i18n.t("aqi", q.lang, **slots),
            "facts": facts, "advisory": a, "severity": a.severity, "sources": [prov]}


# ------------------------------------------------------------------ climate
async def answer_climate(q: ParsedQuery, place: Place) -> dict:
    data = await openmeteo.climate_series(place.lat, place.lon,
                                          years_back=q.years_back, month=q.month)
    series = data["series"]
    prov = data["provenance"]
    if len(series) < 5:
        return {"en": f"Not enough archive data for {place.name} to compute a trend.",
                "loc": f"Not enough archive data for {place.name} to compute a trend.",
                "facts": [], "advisory": None, "severity": Severity.NONE,
                "sources": [prov]}

    years = [float(s["year"]) for s in series]
    tmax = [s["mean_tmax_c"] for s in series]
    rain = [float(s["total_rain_mm"]) for s in series]

    t_slope, _ = openmeteo.linear_trend(years, tmax)
    r_slope, _ = openmeteo.linear_trend(years, rain)
    span = series[-1]["year"] - series[0]["year"]

    first_half = series[: len(series) // 2]
    second_half = series[len(series) // 2:]
    t_early = sum(s["mean_tmax_c"] for s in first_half) / len(first_half)
    t_late = sum(s["mean_tmax_c"] for s in second_half) / len(second_half)
    r_early = sum(s["total_rain_mm"] for s in first_half) / len(first_half)
    r_late = sum(s["total_rain_mm"] for s in second_half) / len(second_half)

    period = f"month {q.month}" if q.month else "the year"
    text = (
        f"{place.name}, {series[0]['year']}–{series[-1]['year']} ({period}):\n"
        f"• Mean daily maximum temperature changed by {t_slope * 10:+.2f} °C per decade "
        f"({t_slope * span:+.2f} °C over {span} years).\n"
        f"• First half of the record averaged {t_early:.2f} °C, second half "
        f"{t_late:.2f} °C — a shift of {t_late - t_early:+.2f} °C.\n"
        f"• Rainfall trend {r_slope * 10:+.1f} mm per decade; "
        f"{r_early:.0f} mm → {r_late:.0f} mm between the two halves.\n"
        f"• Wettest year {max(series, key=lambda s: s['total_rain_mm'])['year']} "
        f"({max(s['total_rain_mm'] for s in series):.0f} mm), driest "
        f"{min(series, key=lambda s: s['total_rain_mm'])['year']} "
        f"({min(s['total_rain_mm'] for s in series):.0f} mm)."
    )

    facts = [
        _fact("temp_trend_per_decade", round(t_slope * 10, 3), prov, "°C/decade",
              "Temperature trend"),
        _fact("rain_trend_per_decade", round(r_slope * 10, 2), prov, "mm/decade",
              "Rainfall trend"),
        _fact("record_span", f"{series[0]['year']}–{series[-1]['year']}", prov,
              None, "Period"),
    ]
    return {"en": text, "loc": text, "facts": facts, "advisory": None,
            "severity": Severity.NONE, "sources": [prov],
            "chart": {"type": "climate",
                      "labels": [str(s["year"]) for s in series],
                      "tmax": tmax, "rain": rain}}


# ------------------------------------------------------------------ router
HANDLERS = {
    Intent.CURRENT: answer_current,
    Intent.FORECAST: answer_forecast,
    Intent.WARNING: answer_warnings,
    Intent.ADVISORY: answer_advisory,
    Intent.AIR_QUALITY: answer_air_quality,
    Intent.CLIMATE: answer_climate,
}


async def execute(q: ParsedQuery, place: Place) -> dict:
    handler = HANDLERS.get(q.intent, answer_current)
    return await handler(q, place)


FOLLOWUPS = {
    Intent.CURRENT: ["Will it rain tomorrow?", "Any warnings for this district?",
                     "Air quality here"],
    Intent.FORECAST: ["Should I spray my crop this week?",
                      "Alert me if a warning is issued", "Show the 7-day rainfall"],
    Intent.WARNING: ["What should I do to prepare?", "Subscribe me to alerts here",
                     "Show the forecast behind this warning"],
    Intent.ADVISORY: ["Show me the raw forecast", "Any warnings for this district?"],
    Intent.CLIMATE: ["Compare with the last 10 years",
                     "How has June rainfall changed?"],
    Intent.AIR_QUALITY: ["Will rain clear the air this week?", "Today's forecast"],
}
