"""Deterministic risk engine: Signals -> score, confidence, action.

Design rules:
- Every point in the score is traceable to a Signal (see `drivers`).
- The worst finding sets the base; independent sources agreeing raise it.
  One model forecasting storms is a concern; the TAF, Open-Meteo and an NWS
  warning all saying so is near-certain.
- Confidence is reported separately from risk: "low risk" because we
  checked is not the same as "low risk" because a source was down.
- The LLM never touches any of this; it only narrates the result.
"""
import re
from collections import defaultdict
from typing import Dict, List, Tuple

from app.models import Action, Driver, Mode, Severity, Signal, SourceStatus, Trip
from app.timewin import local_today

POINTS = {Severity.none: 0, Severity.low: 10, Severity.moderate: 25,
          Severity.high: 50, Severity.severe: 80}
CORROBORATION_POINTS = 10        # per extra independent source seeing >= moderate
MAX_CORROBORATING_SOURCES = 2
BREADTH_POINTS = 3               # per further finding, capped
MAX_BREADTH_POINTS = 10

# (upper bound exclusive, label, severity)
LEVELS = [(10, "Minimal", Severity.none), (25, "Low", Severity.low),
          (50, "Moderate", Severity.moderate), (75, "High", Severity.high),
          (101, "Severe", Severity.severe)]


# --- score -----------------------------------------------------------------

def score(signals: List[Signal]) -> Tuple[int, List[Driver]]:
    scored = sorted(
        (s for s in signals if s.valid_for_travel_date and s.severity > Severity.none),
        key=lambda s: -POINTS[s.severity],
    )
    if not scored:
        return 0, []

    top = scored[0]
    drivers = [_driver(POINTS[top.severity], f"Worst finding: {top.headline}", top)]
    used = {id(top)}

    # Corroboration: the strongest finding from each *other* source that sees
    # moderate-or-worse conditions in the *same part of the trip*. Winds at
    # the origin are not confirmed by a storm at the destination.
    best_by_source: Dict[str, Signal] = {}
    for s in scored:
        if (s.source != top.source and same_place(s, top)
                and s.severity >= Severity.moderate and s.source not in best_by_source):
            best_by_source[s.source] = s
    for s in list(best_by_source.values())[:MAX_CORROBORATING_SOURCES]:
        drivers.append(_driver(CORROBORATION_POINTS, f"Corroborated by {s.source}: {s.headline}", s))
        used.add(id(s))

    rest = [s for s in scored if id(s) not in used]
    if rest:
        pts = min(len(rest) * BREADTH_POINTS, MAX_BREADTH_POINTS)
        drivers.append(Driver(points=pts, reason=f"{len(rest)} further lower-level finding(s)",
                              source="multiple", location=", ".join(sorted({s.location for s in rest}))[:120]))

    return min(100, sum(d.points for d in drivers)), drivers


def same_place(a: Signal, b: Signal) -> bool:
    """Origin/destination: same end of the trip. Route: a long drive is one
    "area" but hundreds of km wide, so findings must share a sample point
    ("km 150 …"); a warning at km 600 isn't confirmed by fog at km 0."""
    if a.area != b.area:
        return False
    if a.area != "route":
        return True
    return bool(set(re.findall(r"km \d+", a.location)) & set(re.findall(r"km \d+", b.location)))


def level_for(score_: int) -> Tuple[str, Severity]:
    for bound, label, sev in LEVELS:
        if score_ < bound:
            return label, sev
    return LEVELS[-1][1], LEVELS[-1][2]


def _driver(points: int, reason: str, s: Signal) -> Driver:
    return Driver(points=points, reason=reason, source=s.source, location=s.location)


# --- confidence ------------------------------------------------------------

def confidence(trip: Trip, signals: List[Signal], sources: List[SourceStatus]) -> Tuple[str, List[str]]:
    penalty, reasons = 0, []

    for st in sources:
        if not st.ok:
            penalty += 1
            reasons.append(f"{st.source} was unavailable, so its evidence is missing")

    days_out = (trip.travel_date - local_today(trip.origin.timezone)).days
    if days_out > 7:
        penalty += 2
        reasons.append(f"Trip is {days_out} days out; weather forecasts lose most of their skill beyond a week")
    elif days_out > 3:
        penalty += 1
        reasons.append(f"Trip is {days_out} days out; forecast details will still change")

    if trip.mode == Mode.flight:
        taf = [s for s in signals if s.source.startswith("AviationWeather")]
        uncovered = sorted(s.location for s in taf if not s.valid_for_travel_date)
        if taf and len(uncovered) == len(taf):
            penalty += 1
            reasons.append("Airport forecasts (TAF) don't reach the travel date yet; "
                           "relying on general weather models")
        elif uncovered:
            penalty += 1
            reasons.append(f"Airport forecasts (TAF) for {', '.join(uncovered)} don't reach the travel "
                           "date yet; those airports rely on general weather models")
        if days_out == 0:
            reasons.append("Travelling today, so live FAA ground stops and delays apply directly")
        else:
            reasons.append("Live FAA delays can't be predicted ahead; re-check on the day of travel")

    if any(s.source.startswith("Open-Meteo") and not s.valid_for_travel_date for s in signals):
        penalty += 2
        reasons.append("Part of the trip falls beyond the weather forecast range")

    # Sources that answered but only cover part of the trip.
    notes = list(dict.fromkeys(s.coverage_note for s in signals if s.coverage_note))
    if notes:
        penalty += 1
        reasons.extend(notes[:3])

    if penalty == 0 and not any("unavailable" in r for r in reasons):
        reasons.insert(0, "All sources responded and forecasts cover the travel window")
    label = "high" if penalty == 0 else "medium" if penalty <= 2 else "low"
    return label, reasons


# --- action ----------------------------------------------------------------

def recommend(trip: Trip, level: str, conf: str, signals: List[Signal],
              sources: List[SourceStatus]) -> Action:
    hazards = [s for s in signals if s.valid_for_travel_date and s.severity >= Severity.moderate]
    failed = [st.source for st in sources if not st.ok]
    is_today = trip.travel_date == local_today(trip.origin.timezone)
    flight = trip.mode == Mode.flight

    if level == "Severe":
        action = Action(level="rebook", headline="Rebook or postpone this trip", steps=[
            "Contact the traveller now; don't wait for cancellations to be announced.",
            ("Move to a different day, or check the airline's travel waiver: carriers usually allow free "
             "changes ahead of major weather." if flight else
             "Postpone the drive, or fly instead if the airports at both ends are clear."),
        ])
    elif level == "High":
        action = Action(level="mitigate", headline="Take action to reduce disruption", steps=[
            ("Move to an earlier flight, ahead of the worst conditions, and avoid the last flight of the day."
             if flight else "Shift the departure time to avoid the affected stretch of road (see below)."),
            ("Check for an airline travel waiver allowing free changes." if flight
             else "Check the state DOT / 511 road conditions before leaving and carry a winter/emergency kit."),
        ])
    elif level == "Moderate":
        action = Action(level="monitor", headline="Monitor and build in buffer time", steps=[
            "Re-check 24 hours before departure; forecast details firm up.",
            ("Avoid tight connections (under 90 min) and enable airline app notifications."
             if flight else "Allow extra driving time and check road conditions before leaving."),
        ])
    elif conf == "low":
        action = Action(level="monitor", headline="No significant risk found yet; re-check closer to the date",
                        steps=["Evidence is limited (see confidence), so this is not an all-clear."])
    else:
        action = Action(level="none", headline="No action needed", steps=[
            "No significant disruption risk found across all sources."])

    if flight and action.level != "none":
        action.steps.extend(_alternate_airports(trip, signals))
    if not flight:
        action.steps.extend(f"Hazard on route: {s.location}: {s.headline.split(': ', 1)[-1]}"
                            for s in hazards[:4])
    if flight and not is_today:
        action.steps.append("Re-check on the morning of travel: live ground stops and delays only show up on the day.")
    for src in failed:
        action.steps.append(f"Verify manually: {src} couldn't be reached.")
    return action


def _alternate_airports(trip: Trip, signals: List[Signal]) -> List[str]:
    """If one airport in a multi-airport city is the problem, name the others."""
    # A signal's location can name several airports ("JFK, LGA" from a merged
    # NWS alert, "LGA/EWR/JFK" from the city-level forecast); it applies to each.
    worst: Dict[str, Severity] = defaultdict(lambda: Severity.none)
    for s in signals:
        if s.valid_for_travel_date:
            for code in re.findall(r"\b[A-Z]{3}\b", s.location):
                worst[code] = max(worst[code], s.severity, key=int)
    out = []
    for place in (trip.origin, trip.destination):
        codes = [a.iata for a in place.airports]
        if len(codes) < 2:
            continue
        bad = [c for c in codes if worst[c] >= Severity.high]
        good = [c for c in codes if worst[c] <= Severity.low]
        if bad and good:
            out.append(f"At {place.name}, the issue is specific to {'/'.join(bad)}; "
                       f"{'/'.join(good)} look clear, so consider flying via {good[0]}.")
    return out


# --- rule-based summary (used when the LLM summary isn't available) -------

def rule_summary(trip: Trip, score_: int, level: str, conf: str, drivers: List[Driver], action: Action) -> str:
    mode = "flight" if trip.mode == Mode.flight else "drive"
    s = (f"{level} disruption risk ({score_}/100) for the {trip.origin.name} → "
         f"{trip.destination.name} {mode} on {trip.travel_date:%a %b %d}. ")
    s += f"Main factor: {drivers[0].reason.split(': ', 1)[-1]}. " if drivers else "No significant hazards found. "
    s += f"Recommended: {action.headline.lower()}. Confidence: {conf}."
    return s
