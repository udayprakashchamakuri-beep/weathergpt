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

import math
from datetime import datetime, timedelta

from .schemas import Advisory, Persona, Severity

# IMD 24-h rainfall classification (mm)
RAIN_LIGHT, RAIN_MODERATE = 2.5, 15.6
RAIN_HEAVY, RAIN_VERY_HEAVY, RAIN_EXTREME = 64.5, 115.6, 204.5

# Wind (km/h). IMD states its marine warnings in KNOTS, while every rule below
# compares km/h, so these are DERIVED from the knot figures rather than
# transcribed as rounded km/h.
#
# They were previously written as the literals 62.0 and 88.0 while the comment
# alongside them said "34 kt = 62.9 km/h" -- the code contradicted its own
# documentation. Both literals sat about a knot BELOW the criterion they
# claimed to encode, so the small-craft and gale warnings each fired later
# than IMD's own threshold. On a go/no-go for small craft that is an
# under-warning, which is the dangerous direction to be wrong in.
KT_TO_KMH = 1.852

WIND_STRONG = 40.0                       # km/h-native: IMD "strong winds"
WIND_SMALL_CRAFT = 34 * KT_TO_KMH        # 34 kt = 62.968 km/h
WIND_GALE = 48 * KT_TO_KMH               # 48 kt = 88.896 km/h

# Absolute screening thresholds -- NOT IMD's departure-from-normal heat-wave
# criterion. See the module docstring.
HEAT_SCREEN, HEAT_SCREEN_SEVERE = 40.0, 45.0
COLD_WAVE = 10.0

THUNDER_CODES = {95, 96, 99}

# Outdoor work. Maharashtra, Tamil Nadu and Gujarat treat a wet-bulb
# temperature above 30 C as unsafe for work. The 28 C step is this build's
# "approaching the limit" screen, not a notified figure.
WETBULB_UNSAFE, WETBULB_CAUTION = 30.0, 28.0
# Typical site limits, not statutory ones: concrete pours stop around 55 km/h,
# crane lifts around 60 km/h. Each crane's rated limit governs.
WIND_NO_POUR, WIND_NO_LIFT = 55.0, 60.0

# Dairy heat stress: milk yield starts falling at THI 74.
THI_MILK_LOSS = 74.0


def wet_bulb_c(t: float, rh: float) -> float:
    """Stull (2011) wet-bulb from air temperature (C) and RH (%).

    Must be fed COINCIDENT values: pairing the day's max temperature with its
    max humidity (which comes at dawn) overstates heat stress by several C.
    """
    return (t * math.atan(0.151977 * math.sqrt(rh + 8.313659))
            + math.atan(t + rh) - math.atan(rh - 1.676331)
            + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


def thi(t: float, rh: float) -> float:
    """Temperature-humidity index for cattle (NRC 1971), from C and %."""
    return (1.8 * t + 32) - (0.55 - 0.0055 * rh) * (1.8 * t - 26)


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


# Spraying. A product needs some dry hours after application to become
# rainfast; 6 h covers most labels, and drizzle under 1 mm over that span is
# tolerated. Wind above 15 km/h drifts the spray.
# ponytail: one rainfastness figure for every product; use label times once
# the crop and product are known.
SPRAY_DRY_HOURS, SPRAY_RAIN_MM, SPRAY_MAX_WIND = 6, 1.0, 15.0
SPRAY_FROM_HOUR, SPRAY_TO_HOUR = 6, 18          # IST daylight


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def spray_window(steps: list[dict], not_before: str, days_ahead: int = 4) -> dict | None:
    """First daylight forecast step, at or after `not_before`, with wind under
    15 km/h and under 1 mm of rain from its start until 6 h after it ends.

    Steps are {start "YYYY-MM-DDTHH:MM" IST, hours, rain_mm, wind_kmh}. A step
    is only judged where the forecast covers the whole dry span: running out
    of forecast is not the same as a dry afternoon.
    """
    steps = sorted(steps, key=lambda s: s["start"])
    stop = (_t(not_before[:10]) + timedelta(days=days_ahead)).strftime("%Y-%m-%dT%H:%M")

    def end_of(s):
        return _t(s["start"]) + timedelta(hours=s["hours"])

    def sprayable(s) -> bool:
        start, end = _t(s["start"]), end_of(s)
        if (start.hour < SPRAY_FROM_HOUR or end.date() != start.date()
                or end.hour > SPRAY_TO_HOUR):
            return False
        if s["wind_kmh"] is None or s["wind_kmh"] >= SPRAY_MAX_WIND:
            return False
        until = end + timedelta(hours=SPRAY_DRY_HOURS)
        span = [x for x in steps if _t(x["start"]) < until and end_of(x) > start]
        return (max(end_of(x) for x in span) >= until
                and sum(x["rain_mm"] for x in span) < SPRAY_RAIN_MM)

    for i, s in enumerate(steps):
        if not (not_before <= s["start"] < stop) or not sprayable(s):
            continue
        # Report the whole run of back-to-back sprayable steps, not just the
        # first hour of it.
        last = s
        for nxt in steps[i + 1:]:
            if _t(nxt["start"]) != end_of(last) or not sprayable(nxt):
                break
            last = nxt
        return {"start": s["start"], "end": end_of(last).strftime("%Y-%m-%dT%H:%M"),
                "wind_kmh": max(x["wind_kmh"] for x in steps[i:steps.index(last) + 1])}
    return None


# ------------------------------------------------------------------ farmer
def farmer(days: list[dict], when: str = "today", steps: list[dict] | None = None,
           not_before: str | None = None) -> Advisory:
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

    # spraying window: by the hour when the provider's steps are available
    if steps and d0.get("date"):
        win = spray_window(steps, not_before or f"{d0['date']}T00:00")
        if win:
            span = (f"{win['start'][11:]}-{win['end'][11:]} (wind up to "
                    f"{win['wind_kmh']:.0f} km/h, under 1 mm of rain for "
                    f"{SPRAY_DRY_HOURS} h after)")
        if win and win["start"][:10] == d0["date"]:
            actions.append(f"Best spray window {when}: {span}.")
        elif win:
            actions.append(f"Do not spray {when} -- every daylight slot has rain "
                           f"within {SPRAY_DRY_HOURS} h or wind above 15 km/h. "
                           f"Next spray window: {win['start'][:10]} {span}.")
        else:
            actions.append("No safe spray window in the next 4 days -- every "
                           f"daylight slot has rain within {SPRAY_DRY_HOURS} h "
                           "or wind above 15 km/h.")
    elif rain_today >= RAIN_LIGHT:
        actions.append(f"Do not spray pesticide or foliar fertiliser {when} -- "
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
        actions.append(f"Conditions are suitable for spraying {when} "
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

    # livestock
    if (t_hi := d0.get("thi_max")) is not None and t_hi >= THI_MILK_LOSS:
        actions.append(f"Dairy animals: heat stress index (THI) {t_hi:.0f} {when}, "
                       "above 74 where milk yield starts to fall. Keep cattle in "
                       "shade with water available, and feed in the cool hours.")

    headline = {
        Severity.RED: "Protect the crop -- severe weather ahead",
        Severity.ORANGE: "Adjust field operations -- disruptive weather likely",
        Severity.YELLOW: "Plan around unsettled weather",
        Severity.GREEN: "Normal field operations",
    }.get(sev, "Normal field operations")

    return Advisory(persona=Persona.FARMER, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "no threshold exceeded")


# --------------------------------------------------------------- fisherman
def fisherman(days: list[dict], when: str = "today", trip_days: int = 1) -> Advisory:
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
        # Wind only: a thunderstorm can still make the headline "not advised".
        actions.append(f"Wind is within limits -- up to {gust:.0f} km/h.")

    if d0.get("weather_code") in THUNDER_CODES:
        actions.append("Thunderstorm risk: lower the mast antenna and avoid open "
                       "deck during squalls.")

    calm = next((d for d in days[1:5]
                 if (d.get("gust_max_kmh") or 0) < WIND_SMALL_CRAFT), None)
    if gust >= WIND_SMALL_CRAFT and calm:
        actions.append(f"Next workable window: {calm['date']} "
                       f"(gusts {calm.get('gust_max_kmh', 0):.0f} km/h).")

    # A multi-day trip is judged on its worst day, not its first: out past
    # ~20 km there is no mobile signal, so a warning issued mid-trip never
    # arrives.
    if trip_days > 1:
        trip = days[:trip_days]
        worst = max(trip, key=lambda d: d.get("gust_max_kmh") or d.get("wind_max_kmh") or 0)
        w_gust = worst.get("gust_max_kmh") or worst.get("wind_max_kmh") or 0.0
        if worst is not d0 and w_gust >= WIND_SMALL_CRAFT:
            sev = _max_sev(sev, Severity.ORANGE if w_gust < WIND_GALE else Severity.RED)
            actions.insert(0, f"NOT safe across the next {trip_days} days: gusts "
                              f"{w_gust:.0f} km/h on {worst['date']}, above the 34 kt "
                              f"small-craft threshold. On a multi-day trip, be back "
                              f"in harbour before {worst['date']} or delay sailing.")
        if len(trip) < trip_days:
            actions.append(f"The forecast only covers {len(trip)} of the "
                           f"{trip_days} days asked about; after {trip[-1]['date']} "
                           "you are sailing without one.")
        actions.append("Note this before you leave: mobile signal ends about 20 km "
                       "offshore, so warnings issued during the trip will not "
                       "reach your phone.")

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
def urban(days: list[dict], when: str = "today") -> Advisory:
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
                       f"{when} and alert the traffic control room.")
    elif rain >= RAIN_MODERATE:
        actions.append(f"Moderate rain {rain:.0f} mm -- expect slower commute and "
                       "localised ponding at underpasses.")
    else:
        actions.append(f"No rain-related disruption expected {when}.")

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
def general(days: list[dict], when: str = "today") -> Advisory:
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
        actions.append(f"No weather precautions needed {when}.")

    headline = {Severity.RED: "Take action -- severe weather",
                Severity.ORANGE: "Be prepared",
                Severity.YELLOW: "Be aware",
                Severity.GREEN: "Nothing of concern"}.get(sev, "Be aware")
    return Advisory(persona=Persona.GENERAL, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "no threshold exceeded")


# ---------------------------------------------------- outdoor work / sites
def worker(days: list[dict], when: str = "today") -> Advisory:
    d0 = days[0] if days else {}
    sev, why = classify(d0)
    actions: list[str] = []
    tw = d0.get("wetbulb_max_c")
    gust = d0.get("gust_max_kmh") or d0.get("wind_max_kmh") or 0.0
    rain = d0.get("rain_mm") or 0.0

    if tw is None:
        actions.append("No humidity data from this forecast source, so heat stress "
                       "is screened on temperature alone and may be understated.")
    elif tw >= WETBULB_UNSAFE:
        sev = _max_sev(sev, Severity.RED)
        why.append(f"wet-bulb {tw:.1f} C, above the 30 C safe-work limit")
        actions.append(f"Wet-bulb temperature reaches {tw:.1f} C {when}, above the "
                       "30 C limit Maharashtra, Tamil Nadu and Gujarat set for safe "
                       "work. Stop heavy outdoor work from 11:00 to 16:00 and move "
                       "it to early morning.")
    elif tw >= WETBULB_CAUTION:
        sev = _max_sev(sev, Severity.ORANGE)
        why.append(f"wet-bulb {tw:.1f} C, close to the 30 C limit")
        actions.append(f"Wet-bulb temperature reaches {tw:.1f} C {when}, close to "
                       "the 30 C limit. Give water every 20 minutes and a shaded "
                       "rest every hour, and do the heaviest work before 11:00.")
    if (tw is None or tw < WETBULB_CAUTION) and (d0.get("tmax_c") or 0) >= HEAT_SCREEN:
        actions.append(f"Max {d0['tmax_c']:.0f} C: keep heavy work out of "
                       "12:00-16:00 and give water and shade breaks.")

    if d0.get("weather_code") in THUNDER_CODES:
        actions.append("Lightning expected: get workers off scaffolding, roofs and "
                       "cranes, and out of open ground, until the storm passes.")
    if gust >= WIND_NO_LIFT:
        actions.append(f"Gusts {gust:.0f} km/h: stop crane lifts (typical limit "
                       "60 km/h; the crane's own rated limit governs).")
    if gust >= WIND_NO_POUR:
        actions.append(f"Gusts {gust:.0f} km/h: postpone concrete pours and "
                       "formwork.")
    if rain >= RAIN_HEAVY:
        actions.append(f"Heavy rain {rain:.0f} mm {when}: postpone concrete pours "
                       "and excavation, and keep dewatering pumps at footings.")
    elif rain >= RAIN_MODERATE:
        actions.append(f"Rain {rain:.0f} mm {when}: cover fresh concrete and keep "
                       "pour timings flexible.")
    if not actions:
        actions.append(f"No weather restrictions on outdoor work {when}.")

    headline = {Severity.RED: "Stop or reschedule outdoor work",
                Severity.ORANGE: "Restrict outdoor work",
                Severity.YELLOW: "Work with precautions",
                Severity.GREEN: "Normal outdoor work"}.get(sev, "Work with precautions")
    return Advisory(persona=Persona.WORKER, headline=headline, severity=sev,
                    actions=actions, reason="; ".join(why) or "no threshold exceeded")


# What each role does when an official warning is in force. Shared by the
# alert fan-out and the chat warnings answer, so both say the same thing.
ALERT_ACTIONS = {
    Persona.FARMER: "Move harvested produce under cover and postpone spraying.",
    Persona.FISHERMAN: "Do not put to sea. Return to the nearest harbour.",
    Persona.AVIATION: "Expect operational impact; review alternates.",
    Persona.URBAN: "Pre-position pumps and issue a commuter advisory.",
    Persona.WORKER: "Stop outdoor work during the warning and move workers off "
                    "scaffolding, cranes and open ground.",
    Persona.GENERAL: "Stay indoors during the peak and avoid low-lying roads.",
    Persona.RESEARCHER: "Event logged for verification against observations.",
}


def under_official(a: Advisory, severity: Severity) -> Advisory:
    """An official warning outranks the model: an ORANGE lightning alert must
    not sit above a card saying "Nothing of concern"."""
    if _max_sev(severity, a.severity) == a.severity:
        return a
    keep = [x for x in a.actions if not x.startswith("No ")]
    return Advisory(persona=a.persona, headline="Follow the official warning",
                    severity=severity, actions=[ALERT_ACTIONS[a.persona]] + keep,
                    reason="official warning in force; " + a.reason)


def build(persona: Persona, days: list[dict], current_wx: dict | None = None,
          when: str = "today", trip_days: int = 1, steps: list[dict] | None = None,
          not_before: str | None = None) -> Advisory:
    """days[0] is the day the advice is for; `when` names it in the actions.

    `steps` are the provider's sub-daily forecast steps, and `not_before` the
    earliest IST time an action can start ("YYYY-MM-DDTHH:MM").
    """
    if persona == Persona.FARMER:
        return farmer(days, when, steps, not_before)
    if persona == Persona.FISHERMAN:
        return fisherman(days, when, trip_days)
    if persona == Persona.WORKER:
        return worker(days, when)
    if persona == Persona.AVIATION:
        return aviation(current_wx or {}, days)
    if persona == Persona.URBAN:
        return urban(days, when)
    return general(days, when)
