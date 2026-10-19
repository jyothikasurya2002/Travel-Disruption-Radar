"""Open-Meteo hourly forecast: the only source covering the full 16-day range
and every point along a drive. One batched request for all locations.

Thresholds are deliberately simple and documented so ops can tune them:
weather codes are WMO 4677 (95-99 thunderstorm, 56/57/66/67 freezing).
"""
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

from app.dates import MAX_DAYS_AHEAD
from app.geo import nearest_town_label
from app.http import get_json
from app.models import Mode, Severity, Signal, Trip
from app.timewin import Window, area_of, eta, fmt_local, operating_window

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
SOURCE = "Open-Meteo Forecast"
HOURLY = "precipitation,snowfall,wind_gusts_10m,visibility,weather_code"

Hazard = Tuple[Severity, str]
Loc = Tuple[str, float, float, Window, str, str]  # label, lat, lon, exposure window, tz, area


async def fetch(trip: Trip) -> List[Signal]:
    locs = _locations(trip)
    start = trip.travel_date
    end = min(
        max(l[3][1] for l in locs).date() + timedelta(days=1),
        date.today() + timedelta(days=MAX_DAYS_AHEAD - 1),
    )
    data = await get_json(FORECAST_URL, {
        "latitude": ",".join(f"{l[1]:.4f}" for l in locs),
        "longitude": ",".join(f"{l[2]:.4f}" for l in locs),
        "hourly": HOURLY, "timezone": "UTC", "wind_speed_unit": "kmh",
        "start_date": (start - timedelta(days=1)).isoformat(), "end_date": end.isoformat(),
    })
    return parse(data if isinstance(data, list) else [data], locs, trip.mode)


def _locations(trip: Trip) -> List[Loc]:
    if trip.mode == Mode.flight:
        # One point per city (its main airport): metro airports share weather.
        out = []
        for p in (trip.origin, trip.destination):
            a = p.airports[0]
            label = "/".join(x.iata for x in p.airports)
            out.append((label, a.lat, a.lon, operating_window(trip.travel_date, p.timezone), p.timezone,
                        area_of(trip, p)))
        return out
    out = []
    for pt in trip.route.samples:
        t = eta(trip, pt)
        label = f"km {pt.km_from_start:.0f} {nearest_town_label(pt.lat, pt.lon)}"
        out.append((label, pt.lat, pt.lon, (t - timedelta(hours=1), t + timedelta(hours=1)),
                    trip.origin.timezone, "route"))
    return out


def parse(data: List[dict], locs: List[Loc], mode: Mode) -> List[Signal]:
    signals = []
    now = datetime.now(timezone.utc)
    for (label, _, _, window, tz, area), loc in zip(locs, data):
        h = loc["hourly"]
        hazards: List[Tuple[Severity, str, datetime]] = []
        peak_gust, total_precip, n = 0.0, 0.0, 0
        for i, ts in enumerate(h["time"]):
            t = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
            if not (window[0] <= t < window[1]):
                continue
            n += 1
            row = {k: h[k][i] for k in h if k != "time"}
            peak_gust = max(peak_gust, row.get("wind_gusts_10m") or 0)
            total_precip += row.get("precipitation") or 0
            for sev, text in assess_hour(row, mode):
                hazards.append((sev, text, t))

        link = "https://open-meteo.com/en/docs"
        if n == 0:
            signals.append(Signal(
                source=SOURCE, area=area, location=label, category="weather", severity=Severity.none,
                headline=f"{label}: travel time is outside the forecast range",
                url=link, observed_at=now, valid_for_travel_date=False,
            ))
            continue
        if hazards:
            worst = max(int(x[0]) for x in hazards)
            top = [x for x in hazards if int(x[0]) == worst]
            first = min(x[2] for x in top)
            kinds = sorted({x[1] for x in top})
            headline = f"{label}: {'; '.join(kinds)} (from {fmt_local(first, tz)})"
            sev = Severity(worst)
        else:
            sev = Severity.none
            headline = f"{label}: no significant weather forecast"
        signals.append(Signal(
            source=SOURCE, area=area, location=label, category="weather", severity=sev, headline=headline,
            detail=(f"{n} forecast hours checked; peak gust {peak_gust:.0f} km/h, "
                    f"total precipitation {total_precip:.1f} mm"),
            url=link, observed_at=now,
        ))
    return signals


def assess_hour(row: dict, mode: Mode) -> List[Hazard]:
    out: List[Hazard] = []
    code = row.get("weather_code") or 0
    snow = row.get("snowfall") or 0          # cm/h
    rain = row.get("precipitation") or 0     # mm/h
    gust = row.get("wind_gusts_10m") or 0    # km/h
    vis: Optional[float] = row.get("visibility")  # m

    if code in (95, 96, 99):
        out.append((Severity.high, "thunderstorms" + (" with hail" if code != 95 else "")))
    if code in (66, 67):
        out.append((Severity.high, "freezing rain"))
    elif code in (56, 57):
        out.append((Severity.moderate, "freezing drizzle"))

    if snow >= 2.5:
        out.append((Severity.high, f"heavy snow ({snow:.1f} cm/h)"))
    elif snow >= 1:
        out.append((Severity.moderate, f"snow ({snow:.1f} cm/h)"))
    elif snow > 0.2:
        out.append((Severity.low, "light snow"))

    if rain >= 8:
        out.append((Severity.moderate, f"heavy rain ({rain:.0f} mm/h)"))
    elif rain >= 4 and mode == Mode.drive:
        out.append((Severity.low, f"steady rain ({rain:.0f} mm/h)"))

    if gust >= 90:
        out.append((Severity.high, f"damaging wind gusts ({gust:.0f} km/h)"))
    elif gust >= 65:
        out.append((Severity.moderate, f"strong wind gusts ({gust:.0f} km/h)"))
    elif gust >= 50:
        out.append((Severity.low, f"gusty winds ({gust:.0f} km/h)"))

    if vis is not None:
        if vis < 400:
            out.append((Severity.high, f"dense fog (visibility {vis:.0f} m)"))
        elif vis < 1600:
            out.append((Severity.moderate, f"fog (visibility {vis:.0f} m)"))
    return out
