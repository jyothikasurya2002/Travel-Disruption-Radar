"""National Weather Service active alerts (watches, warnings, advisories).

Queried by point (each airport, or each sample along a drive) so we only see
alerts for zones the traveller actually passes through. ~70% of U.S. alerts
at any time are marine; those and agricultural ones are filtered out.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from app.geo import nearest_town_label
from app.http import get_json
from app.models import Mode, Severity, Signal, Trip
from app.timewin import (Window, area_of, drive_window, flight_airports, fmt_local,
                         operating_window, overlap_hours)

ALERTS_URL = "https://api.weather.gov/alerts/active"
SOURCE = "NWS Alerts"

IRRELEVANT_KEYWORDS = (
    "marine", "small craft", "gale", "surf", "rip current", "beach", "seas",
    "freezing spray", "frost", "freeze", "low water", "brisk wind", "lake wind",
    "hurricane force wind", "tsunami",
)
IRRELEVANT_EXACT = {"Storm Warning", "Storm Watch"}  # marine products despite the name

NWS_SEVERITY = {"Extreme": Severity.severe, "Severe": Severity.high,
                "Moderate": Severity.moderate, "Minor": Severity.low}

# Events that disrupt travel more than their generic NWS severity implies.
TRAVEL_FLOOR = {
    "Blizzard Warning": Severity.severe, "Ice Storm Warning": Severity.severe,
    "Winter Storm Warning": Severity.high, "Tornado Warning": Severity.severe,
    "Hurricane Warning": Severity.severe, "Tropical Storm Warning": Severity.high,
    "Flash Flood Warning": Severity.high, "Dust Storm Warning": Severity.high,
    "High Wind Warning": Severity.high, "Dense Fog Advisory": Severity.moderate,
    "Winter Weather Advisory": Severity.moderate, "Freezing Fog Advisory": Severity.moderate,
    "Severe Thunderstorm Warning": Severity.high,
}

Check = Tuple[str, float, float, Window, str, str]  # label, lat, lon, exposure window, tz, area


async def fetch(trip: Trip) -> List[Signal]:
    checks = _checks(trip)
    results = await asyncio.gather(*(
        get_json(ALERTS_URL, {"point": f"{c[1]:.4f},{c[2]:.4f}", "status": "actual"})
        for c in checks
    ), return_exceptions=True)

    # One point failing shouldn't throw away the others. NWS only covers the
    # U.S., so a drive through Canada (Anchorage -> Seattle) gets a 400 for
    # those points. If every point fails, let the source fail as a whole.
    ok = [(c, r) for c, r in zip(checks, results) if not isinstance(r, Exception)]
    if not ok:
        raise next(r for r in results if isinstance(r, Exception))
    signals = parse([r for _, r in ok], [c for c, _ in ok])
    missed: Dict[str, List[str]] = {}  # area -> labels NWS couldn't answer for
    for c, r in zip(checks, results):
        if isinstance(r, Exception):
            missed.setdefault(c[5], []).append(c[0])
    for area, labels in missed.items():
        signals.append(Signal(
            source=SOURCE, area=area, location=", ".join(labels), category="alert", severity=Severity.none,
            headline=f"No NWS data for {len(labels)} location(s) (outside NWS coverage or no response)",
            url="https://alerts.weather.gov/", observed_at=datetime.now(timezone.utc),
            coverage_note=f"NWS alerts couldn't be checked for {', '.join(labels)}",
        ))
    return signals


def _checks(trip: Trip) -> List[Check]:
    if trip.mode == Mode.flight:
        return [(a.iata, a.lat, a.lon, operating_window(trip.travel_date, p.timezone), p.timezone,
                 area_of(trip, p)) for a, p in flight_airports(trip)]
    out = []
    for pt in trip.route.samples:
        label = f"km {pt.km_from_start:.0f} {nearest_town_label(pt.lat, pt.lon)}"
        out.append((label, pt.lat, pt.lon, drive_window(trip, pt), trip.origin.timezone, "route"))
    return out


def parse(results: List[dict], checks: List[Check]) -> List[Signal]:
    # The same alert often covers several of our points: report it once,
    # listing every place it affects.
    seen: Dict[str, Signal] = {}
    clear: Dict[str, List[str]] = {}  # area -> labels with nothing found
    for (label, _, _, window, tz, area), data in zip(checks, results):
        hit = False
        for feat in data.get("features", []):
            p = feat["properties"]
            if not _relevant(p["event"]):
                continue
            span = _alert_window(p)
            if span is None or overlap_hours(span, window) == 0:
                continue
            hit = True
            if p["id"] in seen:
                seen[p["id"]].location += f", {label}"
                continue
            seen[p["id"]] = Signal(
                source=SOURCE, area=area, location=label, category="alert", severity=_severity(p),
                headline=f"{p['event']} until {fmt_local(span[1], tz)}",
                detail=(p.get("headline") or "") + ("\n\n" + p["description"] if p.get("description") else ""),
                url=p.get("@id") or feat.get("id"),
                observed_at=_dt(p.get("sent")) or datetime.now(timezone.utc),
            )
        if not hit:
            clear.setdefault(area, []).append(label)

    signals = list(seen.values())
    for area, labels in clear.items():
        signals.append(Signal(
            source=SOURCE, area=area, location=", ".join(labels), category="alert", severity=Severity.none,
            headline=f"No travel-relevant NWS alerts for {len(labels)} checked location(s)",
            url="https://alerts.weather.gov/", observed_at=datetime.now(timezone.utc),
        ))
    return signals


def _relevant(event: str) -> bool:
    e = event.lower()
    return event not in IRRELEVANT_EXACT and not any(k in e for k in IRRELEVANT_KEYWORDS)


def _severity(p: dict) -> Severity:
    base = NWS_SEVERITY.get(p.get("severity"), Severity.low)
    floor = TRAVEL_FLOOR.get(p["event"], Severity.none)
    sev = max(base, floor, key=int)
    # A watch means "conditions are possible", not expected: cap it.
    if p["event"].endswith("Watch"):
        sev = min(sev, Severity.moderate, key=int)
    return sev


def _alert_window(p: dict) -> Optional[Window]:
    start = _dt(p.get("onset")) or _dt(p.get("effective"))
    end = _dt(p.get("ends")) or _dt(p.get("expires"))
    if start is None:
        return None
    return start, end or start + timedelta(hours=12)


def _dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None
