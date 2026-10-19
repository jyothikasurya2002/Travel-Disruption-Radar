"""AviationWeather.gov TAF: the terminal forecast airlines and ATC plan around.

Thresholds follow the FAA flight categories (LIFR / IFR / MVFR), which drive
arrival-rate reductions and ground delay programs, plus the weather that
causes ground stops (thunderstorms, freezing precipitation, heavy snow).
TAFs only reach ~24-30 h ahead; beyond that we say so instead of guessing.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.http import get_json
from app.models import Airport, Place, Severity, Signal, Trip
from app.timewin import Window, flight_airports, fmt_local, operating_window, overlap_hours

TAF_URL = "https://aviationweather.gov/api/data/taf"
SOURCE = "AviationWeather TAF"

Hazard = Tuple[Severity, str]


async def fetch(trip: Trip) -> List[Signal]:
    pairs = flight_airports(trip)
    data = await get_json(TAF_URL, {"ids": ",".join(a.icao for a, _ in pairs), "format": "json"})
    return parse(data, pairs, trip)


def parse(data: list, pairs: List[Tuple[Airport, Place]], trip: Trip) -> List[Signal]:
    by_icao: Dict[str, dict] = {t["icaoId"]: t for t in data or []}
    signals = []
    for ap, place in pairs:
        taf = by_icao.get(ap.icao)
        link = f"https://aviationweather.gov/data/taf/?id={ap.icao}"
        if not taf:
            continue  # smaller airports don't issue TAFs; not an error
        issued = _iso(taf.get("issueTime"))
        window = operating_window(trip.travel_date, place.timezone)
        valid: Window = (_ts(taf["validTimeFrom"]), _ts(taf["validTimeTo"]))

        if overlap_hours(valid, window) == 0:
            signals.append(Signal(
                source=SOURCE, location=ap.iata, category="weather", severity=Severity.none,
                headline=(f"{ap.iata}: TAF only covers until {fmt_local(valid[1], place.timezone)}; "
                          "travel date is beyond its range"),
                detail=taf.get("rawTAF"), url=link, observed_at=issued, valid_for_travel_date=False,
            ))
            continue

        hazards: List[Tuple[Severity, str, datetime]] = []
        for f in taf.get("fcsts", []):
            period: Window = (_ts(f["timeFrom"]), _ts(f["timeTo"]))
            if overlap_hours(period, window) == 0:
                continue
            starts = max(period[0], window[0])  # don't report "from 2 AM" for a 5 AM day
            for sev, text in assess_period(f):
                hazards.append((sev, text, starts))

        if hazards:
            worst = max(int(h[0]) for h in hazards)
            reasons = sorted({f"{t} from {fmt_local(at, place.timezone)}" for s, t, at in hazards if int(s) == worst})
            headline = f"{ap.iata} forecast: " + "; ".join(reasons[:3])
            sev = Severity(worst)
        else:
            sev, headline = Severity.none, f"{ap.iata} forecast: VFR, no significant weather on travel day"
        signals.append(Signal(
            source=SOURCE, location=ap.iata, category="weather", severity=sev,
            headline=headline, detail=taf.get("rawTAF"), url=link, observed_at=issued,
        ))
    return signals


def assess_period(f: dict) -> List[Hazard]:
    out: List[Hazard] = []
    wx = (f.get("wxString") or "").upper()
    tokens = wx.split()

    if any("TS" in t for t in tokens):
        out.append((Severity.high, f"thunderstorms ({wx})"))
    if any("FZ" in t for t in tokens):
        out.append((Severity.high, f"freezing precipitation ({wx})"))
    for t in tokens:
        if "SN" in t and "FZ" not in t:
            sev = Severity.high if t.startswith("+") else Severity.low if t.startswith("-") else Severity.moderate
            out.append((sev, f"snow ({t})"))
            break

    vis = _visibility(f.get("visib"))
    ceil = _ceiling(f)
    if (ceil is not None and ceil < 500) or (vis is not None and vis < 1):
        out.append((Severity.high, f"LIFR conditions ({_cv(ceil, vis)})"))
    elif (ceil is not None and ceil < 1000) or (vis is not None and vis < 3):
        out.append((Severity.moderate, f"IFR conditions ({_cv(ceil, vis)})"))
    elif (ceil is not None and ceil <= 3000) or (vis is not None and vis <= 5):
        out.append((Severity.low, f"MVFR conditions ({_cv(ceil, vis)})"))

    gust, spd = f.get("wgst") or 0, f.get("wspd") or 0
    if gust >= 35 or spd >= 30:
        out.append((Severity.high, f"strong winds {spd} kt gusting {gust} kt"))
    elif gust >= 25 or spd >= 20:
        out.append((Severity.moderate, f"gusty winds {spd} kt gusting {gust} kt"))
    if f.get("wshearHgt"):
        out.append((Severity.moderate, "low-level wind shear"))

    # PROB30/PROB40 groups are possibilities, not the forecast: one level lower.
    if f.get("probability"):
        out = [(Severity(max(int(s) - 1, 0)), f"{t} ({f['probability']}% chance)") for s, t in out]
    return out


def _visibility(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).rstrip("+"))
    except ValueError:
        return None


def _ceiling(f: dict) -> Optional[int]:
    """Lowest broken/overcast layer (or vertical visibility) in feet."""
    bases = [c["base"] for c in f.get("clouds") or []
             if c.get("cover") in ("BKN", "OVC", "OVX") and c.get("base") is not None]
    if f.get("vertVis"):
        bases.append(f["vertVis"])
    return min(bases) if bases else None


def _cv(ceil: Optional[int], vis: Optional[float]) -> str:
    parts = []
    if ceil is not None:
        parts.append(f"ceiling {ceil} ft")
    if vis is not None:
        parts.append(f"visibility {vis:g} mi")
    return ", ".join(parts)


def _ts(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _iso(s: Optional[str]) -> datetime:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
