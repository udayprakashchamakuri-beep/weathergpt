"""Decision support: turn meteorology into an action.

A forecast is not an answer. "32 C, 18 mm rain, gusts 45 km/h" is data;
"do not spray today, the rain will wash it off -- spray Thursday morning"
is the product. This module encodes sector thresholds as auditable rules
rather than asking an LLM to improvise, because these outputs drive money
and safety decisions and must be explainable to a domain regulator.

Thresholds follow IMD's own impact-based colour convention and published
sector criteria:
  * heavy rain      64.5-115.5 mm/24h ; very heavy 115.6-204.4 ; extremely >204.4
  * small craft     gusts >=62 km/h (34 kt) is the fishing no-go signal
  * gale            gusts >=88 km/h
  * thunderstorm    WMO codes 95-99

ONE DELIBERATE DEVIATION, stated so nobody mistakes it for the real thing:
IMD defines a heat wave by DEPARTURE FROM NORMAL (>=4.5 C above the station
normal on the plains, >=6.4 C for a severe heat wave), not by an absolute
temperature. Computing that needs the 1991-2020 climatological normal per
station, which this build does not yet load. So the heat rules below are
absolute-temperature SCREENING thresholds (40 C / 45 C) and say so in their
reason text. Wiring the normals in -- the ERA5 archive the climate module
already reads can supply them -- turns this into the real criterion; until
then the system must not claim to implement it.

Every rule cites the variable and threshold that fired, so a user can ask
"why?" and get the actual reason.
"""
from __future__ import annotations

from .schemas import Advisory, Persona, Severity

# IMD 24-h rainfall classification (mm)
RAIN_LIGHT, RAIN_MODERATE = 2.5, 15.6
RAIN_HEAVY, RAIN_VERY_HEAVY, RAIN_EXTREME = 64.5, 115.6, 204.5

# Wind (km/h). 34 kt = 62.9 km/h is IMD's small-craft / fishermen warning
# threshold. (A "squall" in IMD terminology is a separate, stricter
# phenomenon; these names describe the warning, not the phenomenon.)
WIND_STRONG, WIND_SMALL_CRAFT, WIND_GALE = 40.0, 62.0, 88.0

# Absolute screening thresholds -- NOT IMD's departure-from-normal heat-wave
# criterion. See the module docstring.
HEAT_SCREEN, HEAT_SCREEN_SEVERE = 40.0, 45.0
COLD_WAVE = 10.0

THUNDER_CODES = {95, 96, 99}


def _max_sev(*sev: Severity) -> Severity:
    order = [Severity.NONE, Severity.GREEN, Severity.YELLOW,
             Severity.ORANGE, Severity.RED]
    return max(sev, key=order.index)


def classify(day: dict) -> tuple[Severity, list[str]]:
    """Base meteorological severity for one forecast day, with reasons."""
    sev = Severity.GREEN
    why: list[str] = []

    rain = day.get("rain_mm") or 0.0
    gust = day.get("gust_max_kmh") or day.get("wind_max_kmh") or 0.0
    tmax = day.get("tmax_c")
    tmin = day.get("tmin_c")
    code = day.get("weather_code")

    if rain >= RAIN_EXTREME:
        sev = _max_sev(sev, Severity.RED)
        why.append(f"extremely heavy rainfall {rain:.0f} mm "
                   "(IMD threshold 204.5 mm)")
    elif rain >= RAIN_VERY_HEAVY:
        sev = _max_sev(sev, Severity.RED)
        why.append(f"very heavy rainfall {rain:.0f} mm (threshold 115.6 mm)")
    elif rain >= RAIN_HEAVY:
        sev = _max_sev(sev, Severity.ORANGE)
        why.append(f"heavy rainfall {rain:.0f} mm (threshold 64.5 mm)")
    elif rain >= RAIN_MODERATE:
        sev = _max_sev(sev, Severity.YELLOW)
        why.append(f"moderate rainfall {rain:.0f} mm")

    if gust >= WIND_GALE:
        sev = _max_sev(sev, Severity.RED)
        why.append(f"gale-force gusts {gust:.0f} km/h")
    elif gust >= WIND_SMALL_CRAFT:
        sev = _max_sev(sev, Severity.ORANGE)
        why.append(f"gusts {gust:.0f} km/h, above the 34 kt small-craft threshold")
    elif gust >= WIND_STRONG:
        sev = _max_sev(sev, Severity.YELLOW)
        why.append(f"strong winds, gusts {gust:.0f} km/h")

    if code in THUNDER_CODES:
        sev = _max_sev(sev, Severity.ORANGE)
        why.append("thunderstorm with lightning expected")

    if tmax is not None:
        if tmax >= HEAT_SCREEN_SEVERE:
            sev = _max_sev(sev, Severity.RED)
            why.append(f"max {tmax:.0f} C, above the 45 C severe-heat screening "
                       "threshold (not IMD's departure-from-normal heat-wave test)")
        elif tmax >= HEAT_SCREEN:
            sev = _max_sev(sev, Severity.ORANGE)
            why.append(f"max {tmax:.0f} C, above the 40 C heat screening threshold")

    if tmin is not None and tmin <= COLD_WAVE:
        sev = _max_sev(sev, Severity.YELLOW)
        why.append(f"cold conditions, min {tmin:.0f} C")

    return sev, why


# ------------------------------------------------------------------ farmer
def farmer(days: list[dict]) -> Advisory:
    d0 = days[0] if days else {}
    next3 = days[:3]
    rain_today = d0.get("rain_mm") or 0.0
    rain_next3 = sum((d.get("rain_mm") or 0.0) for d in next3)
    wind_today = d0.get("wind_max_kmh") or 0.0
    hum = d0.get("humidity_max_pct") or 0
    sev, why = classify(d0)
    for d in next3[1:]:
        s2, _ = classify(d)
        sev = _max_sev(sev, s2)

    actions: list[str] = []

    # spraying window
    if rain_today >= RAIN_LIGHT:
        actions.append("Do not spray pesticide or foliar fertiliser today -- "
                       f"{rain_today:.0f} mm of rain will wash it off within hours.")
        dry = next((d for d in days[1:5] if (d.get("rain_mm") or 0) < RAIN_LIGHT
                    and (d.get("wind_max_kmh") or 0) < 15), None)
        if dry:
            actions.append(f"Next suitable spray window: {dry['date']} "
                           f"({dry.get('rain_mm', 0):.0f} mm, wind "
                           f"{dry.get('wind_max_kmh', 0):.0f} km/h).")
    elif wind_today > 15:
        actions.append(f"Spray early morning -- afternoon wind of "
                       f"{wind_today:.0f} km/h will cause spray drift.")
    else:
        actions.append("Conditions are suitable for spraying today "
                       "(dry, wind under 15 km/h).")

    # irrigation
    if rain_next3 >= 25:
        actions.append(f"Skip irrigation -- {rain_next3:.0f} mm expected over the "
                       "next 3 days will meet crop water demand.")
    elif rain_next3 < 5 and (d0.get("tmax_c") or 0) > 35:
        actions.append("Irrigate in the evening -- no meaningful rain in 3 days "
                       "and high evaporative demand.")

    # sowing / harvest
    if RAIN_MODERATE <= rain_next3 < RAIN_HEAVY:
        actions.append("Good sowing window: soil moisture will be adequate "
                       "without waterlogging.")
    if rain_next3 >= RAIN_HEAVY:
        actions.append("Advance any standing harvest and move produce to covered "
                       "storage before the heavy spell.")

    # disease pressure
    if hum >= 85 and (d0.get("tmax_c") or 0) >= 25:
        actions.append("High humidity with warm days -- scout for fungal blast / "
                       "blight and keep a prophylactic ready.")

    headline = {
        Severity.RED: "Protect the crop -- severe weather ahead",
        Severity.ORANGE: "Adjust field operations -- disruptive weather likely",
        Severity.YELLOW: "Plan around unsettled weather",
        Severity.GREEN: "Normal field operations",
    }.get(sev, "Normal field operations")

    return Advisory(persona=Persona.FARMER, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "no threshold exceeded")


# --------------------------------------------------------------- fisherman
def fisherman(days: list[dict]) -> Advisory:
    d0 = days[0] if days else {}
    gust = d0.get("gust_max_kmh") or d0.get("wind_max_kmh") or 0.0
    # Whether the number below is a real gust or a stand-in. The thresholds
    # are unchanged; what changes is that a substituted value is declared.
    # Sustained wind is always lower than the gust it replaces, so every
    # threshold here fires LATER than it should -- an under-warning, which on
    # a go/no-go for small craft is the dangerous direction to be wrong in.
    gust_is_substituted = d0.get("gust_max_kmh") is None
    sev, why = classify(d0)
    actions: list[str] = []

    if gust >= WIND_GALE:
        sev = Severity.RED
        actions.append(f"DO NOT PUT TO SEA. Gale-force gusts {gust:.0f} km/h "
                       "(above 48 kt). Return to the nearest harbour.")
    elif gust >= WIND_SMALL_CRAFT:
        sev = _max_sev(sev, Severity.ORANGE)
        actions.append(f"Fishing is not advised -- squally winds {gust:.0f} km/h "
                       "exceed the 34 kt small-craft threshold.")
    elif gust >= WIND_STRONG:
        actions.append(f"Small mechanised boats should stay within sight of the "
                       f"coast -- gusts up to {gust:.0f} km/h.")
    else:
        actions.append(f"Sea conditions are workable -- winds up to {gust:.0f} km/h.")

    if d0.get("weather_code") in THUNDER_CODES:
        actions.append("Thunderstorm risk: lower the mast antenna and avoid open "
                       "deck during squalls.")

    calm = next((d for d in days[1:5]
                 if (d.get("gust_max_kmh") or 0) < WIND_SMALL_CRAFT), None)
    if gust >= WIND_SMALL_CRAFT and calm:
        actions.append(f"Next workable window: {calm['date']} "
                       f"(gusts {calm.get('gust_max_kmh', 0):.0f} km/h).")

    if gust_is_substituted:
        actions.append(
            "CAUTION: no gust data is available from the forecast source for "
            f"this location, so this go/no-go is based on sustained wind "
            f"({gust:.0f} km/h), not gusts. Real gusts will be higher, so "
            "this assessment MAY UNDER-WARN. Treat a borderline call as "
            "no-go and confirm against the IMD port bulletin.")

    actions.append("Cross-check the IMD port warning and fishermen bulletin for "
                   "your landing centre before sailing.")

    headline = {Severity.RED: "No-go: gale warning",
                Severity.ORANGE: "Not advised to sail",
                Severity.YELLOW: "Sail with caution",
                Severity.GREEN: "Safe to sail"}.get(sev, "Sail with caution")
    return Advisory(persona=Persona.FISHERMAN, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "winds below threshold")


# ---------------------------------------------------------------- aviation
def aviation(current_wx: dict, days: list[dict]) -> Advisory:
    wind = current_wx.get("wind_kmh") or 0.0
    gust = current_wx.get("wind_gust_kmh") or wind
    code = current_wx.get("weather_code")
    d0 = days[0] if days else {}
    sev, why = classify(d0)
    actions: list[str] = []

    kt = round(wind / 1.852)
    gkt = round(gust / 1.852)
    actions.append(f"Surface wind {kt} kt, gusting {gkt} kt from "
                   f"{current_wx.get('wind_dir_deg', 0):.0f} deg.")

    if gkt >= 35:
        sev = _max_sev(sev, Severity.RED)
        actions.append("Gusts above 35 kt -- expect crosswind limits to be a factor "
                       "and holding / diversion fuel to be required.")
    elif gkt >= 25:
        sev = _max_sev(sev, Severity.ORANGE)
        actions.append("Gusty conditions -- brief a crosswind approach.")

    if code in (45, 48):
        sev = _max_sev(sev, Severity.ORANGE)
        actions.append("Fog reported -- expect reduced RVR; confirm CAT approach "
                       "minima and alternate.")
    if code in THUNDER_CODES:
        sev = _max_sev(sev, Severity.ORANGE)
        actions.append("CB activity in the terminal area -- plan deviation and "
                       "expect wind shear on approach.")
    if (d0.get("rain_mm") or 0) >= RAIN_HEAVY:
        actions.append("Heavy precipitation forecast -- anticipate contaminated "
                       "runway and reduced braking action.")

    actions.append("Advisory only. File and fly on the current METAR/TAF and the "
                   "IMD aerodrome bulletin.")

    headline = {Severity.RED: "Significant operational impact",
                Severity.ORANGE: "Operationally significant weather",
                Severity.YELLOW: "Minor impact expected",
                Severity.GREEN: "No significant weather"}.get(sev, "Advisory")
    return Advisory(persona=Persona.AVIATION, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "VMC expected")


# ------------------------------------------------------------------- urban
def urban(days: list[dict]) -> Advisory:
    d0 = days[0] if days else {}
    rain = d0.get("rain_mm") or 0.0
    rain3 = sum((d.get("rain_mm") or 0.0) for d in days[:3])
    sev, why = classify(d0)
    actions: list[str] = []

    if rain >= RAIN_VERY_HEAVY:
        actions.append(f"Waterlogging highly likely ({rain:.0f} mm). Pre-position "
                       "dewatering pumps at known flood points and issue a "
                       "commuter advisory.")
        actions.append("Consider staggered office timings and school closure "
                       "review with the district administration.")
    elif rain >= RAIN_HEAVY:
        actions.append(f"Heavy rain {rain:.0f} mm -- clear storm-water drain inlets "
                       "today and alert the traffic control room.")
    elif rain >= RAIN_MODERATE:
        actions.append(f"Moderate rain {rain:.0f} mm -- expect slower commute and "
                       "localised ponding at underpasses.")
    else:
        actions.append("No rain-related disruption expected today.")

    if (d0.get("tmax_c") or 0) >= HEAT_SCREEN:
        actions.append(f"Heat action plan: max {d0['tmax_c']:.0f} C. Open cooling "
                       "centres and shift outdoor municipal work out of 12:00-16:00.")
    if rain3 >= 100:
        actions.append(f"{rain3:.0f} mm cumulative over 3 days -- review lake and "
                       "nala levels and downstream release schedule.")

    headline = {Severity.RED: "Activate the emergency operations centre",
                Severity.ORANGE: "Pre-position civic resources",
                Severity.YELLOW: "Monitor and inform commuters",
                Severity.GREEN: "Routine operations"}.get(sev, "Routine operations")
    return Advisory(persona=Persona.URBAN, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "no threshold exceeded")


# ----------------------------------------------------------------- general
def general(days: list[dict]) -> Advisory:
    d0 = days[0] if days else {}
    sev, why = classify(d0)
    actions: list[str] = []
    rain = d0.get("rain_mm") or 0.0
    if rain >= RAIN_MODERATE:
        actions.append("Carry rain protection and allow extra travel time.")
    if (d0.get("gust_max_kmh") or 0) >= WIND_SMALL_CRAFT:
        actions.append("Secure loose objects on balconies and avoid parking under "
                       "trees or hoardings.")
    if d0.get("weather_code") in THUNDER_CODES:
        actions.append("During lightning, move indoors and stay off open fields "
                       "and rooftops.")
    if (d0.get("tmax_c") or 0) >= HEAT_SCREEN:
        actions.append("Avoid direct sun between 12:00 and 16:00 and drink water "
                       "even when not thirsty.")
    if not actions:
        actions.append("No weather precautions needed today.")

    headline = {Severity.RED: "Take action -- severe weather",
                Severity.ORANGE: "Be prepared",
                Severity.YELLOW: "Be aware",
                Severity.GREEN: "Nothing of concern"}.get(sev, "Be aware")
    return Advisory(persona=Persona.GENERAL, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "no threshold exceeded")


def build(persona: Persona, days: list[dict], current_wx: dict | None = None) -> Advisory:
    if persona == Persona.FARMER:
        return farmer(days)
    if persona == Persona.FISHERMAN:
        return fisherman(days)
    if persona == Persona.AVIATION:
        return aviation(current_wx or {}, days)
    if persona == Persona.URBAN:
        return urban(days)
    return general(days)
