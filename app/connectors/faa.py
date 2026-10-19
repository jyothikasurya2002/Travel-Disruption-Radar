"""FAA NAS Status: ground stops, ground delay programs, delays, closures.

Two kinds of evidence with different predictive value:
- Closures carry a NOTAM time window, so we can check them against the
  travel date precisely.
- Ground stops / delay programs / delays are *live* conditions. They usually
  clear within hours, so they only count fully when travelling today; for
  later dates they're shown as context with low severity.
"""
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.http import get_text
from app.models import Airport, Place, Severity, Signal, Trip
from app.timewin import Window, flight_airports, fmt_local, local_today, operating_window, overlap_hours

FAA_URL = "https://nasstatus.faa.gov/api/airport-status-information"
SOURCE = "FAA NAS Status"
LINK = "https://nasstatus.faa.gov/"

# Closures that don't affect airline passengers (private/GA-only restrictions).
IGNORE_CLOSURE_PATTERNS = ("NON SKED", "TRANSIENT GA", "GA ACFT")


async def fetch(trip: Trip) -> List[Signal]:
    return parse(await get_text(FAA_URL), trip)


def parse(xml_text: str, trip: Trip, now: Optional[datetime] = None) -> List[Signal]:
    root = ET.fromstring(xml_text)
    updated = _parse_update_time(root.findtext("Update_Time")) or now or datetime.now(timezone.utc)
    watched: Dict[str, Tuple[Airport, Place]] = {a.iata: (a, p) for a, p in flight_airports(trip)}

    # Dispatch on element tags (fixed by the FAA's AirportStatus.dtd) rather
    # than the free-text <Name>, which the DTD doesn't pin down.
    signals: List[Signal] = []
    for el in root.iter():
        code = (el.findtext("ARPT") or "").strip()
        if code not in watched:
            continue
        ap, place = watched[code]
        if el.tag == "Airport":
            sig = _closure(el, ap, place, trip, updated)
        else:
            sig = _live_delay(el, ap, place, trip, updated)
        if sig:
            signals.append(sig)

    flagged = {s.location for s in signals}
    for code, (ap, place) in watched.items():
        if code not in flagged:
            signals.append(Signal(
                source=SOURCE, location=code, category="airport_ops", severity=Severity.none,
                headline=f"{code}: no ground stops, delay programs or closures reported",
                url=LINK, observed_at=updated,
                valid_for_travel_date=trip.travel_date == local_today(place.timezone),
            ))
    return signals


# --- closures --------------------------------------------------------------

def _closure(el, ap: Airport, place: Place, trip: Trip, updated: datetime) -> Optional[Signal]:
    reason = el.findtext("Reason") or ""
    if any(p in reason.upper() for p in IGNORE_CLOSURE_PATTERNS):
        return None
    window = _notam_window(reason)
    if window is None:
        return None
    exposed = overlap_hours(window, operating_window(trip.travel_date, place.timezone))
    if exposed == 0:
        return None  # e.g. LGA's routine overnight closure: no scheduled flights affected
    sev = Severity.severe if exposed >= 3 else Severity.high if exposed >= 1 else Severity.moderate
    return Signal(
        source=SOURCE, location=ap.iata, category="airport_ops", severity=sev,
        headline=(f"{ap.iata} closed {fmt_local(window[0], place.timezone)} - "
                  f"{fmt_local(window[1], place.timezone)} (~{exposed:.1f} h of operating hours)"),
        detail=f"NOTAM: {reason}", url=LINK, observed_at=updated,
    )


def _notam_window(reason: str) -> Optional[Window]:
    """NOTAMs end with 'YYMMDDHHMM-YYMMDDHHMM' in UTC."""
    m = re.search(r"(\d{10})-(\d{10})", reason)
    if not m:
        return None
    fmt = "%y%m%d%H%M"
    start = datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc)
    end = datetime.strptime(m.group(2), fmt).replace(tzinfo=timezone.utc)
    return start, end


# --- live delays -----------------------------------------------------------

def _live_delay(el, ap: Airport, place: Place, trip: Trip, updated: datetime) -> Optional[Signal]:
    reason = (el.findtext("Reason") or "unspecified").strip()

    if el.tag == "Program":  # Ground_Stop_List/Program
        sev = Severity.high
        what = f"ground stop until {el.findtext('End_Time') or 'further notice'}"
    elif el.tag == "Ground_Delay":
        avg = parse_minutes(el.findtext("Avg"))
        sev = Severity.high if avg >= 60 else Severity.moderate if avg >= 30 else Severity.low
        what = f"ground delay program, avg {el.findtext('Avg')}, max {el.findtext('Max')}"
    elif el.tag == "Delay":  # Arrival_Departure_Delay_List/Delay
        parts, worst = [], 0
        for ad in el.findall("Arrival_Departure"):
            mx = parse_minutes(ad.findtext("Max"))
            worst = max(worst, mx)
            parts.append(f"{ad.get('Type', 'delay').lower()} delays "
                         f"{ad.findtext('Min')}-{ad.findtext('Max')} ({ad.findtext('Trend') or 'steady'})")
        sev = Severity.moderate if worst >= 45 else Severity.low
        what = "; ".join(parts) or "arrival/departure delays"
    else:
        sev, what = Severity.low, el.tag.replace("_", " ").lower()

    is_today = trip.travel_date == local_today(place.timezone)
    if not is_today:
        sev = min(sev, Severity.low, key=int)
    return Signal(
        source=SOURCE, location=ap.iata, category="airport_ops", severity=sev,
        headline=(f"{ap.iata}: {what}" if is_today
                  else f"{ap.iata} right now: {what} (live status; may clear before your trip)"),
        detail=f"Reason: {reason}", url=LINK, observed_at=updated,
        valid_for_travel_date=is_today,
    )


def parse_minutes(text: Optional[str]) -> int:
    """'1 hour and 3 minutes' -> 63, '45 minutes' -> 45."""
    if not text:
        return 0
    h = re.search(r"(\d+)\s*hour", text)
    m = re.search(r"(\d+)\s*min", text)
    return (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)


def _parse_update_time(text: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.strptime((text or "").strip(), "%a %b %d %H:%M:%S %Y GMT").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
