"""When is the traveller exposed? Every connector asks this the same way.

Assumptions (we don't ask for a flight number or departure time):
- Flights: exposure is the travel day's operating hours, 06:00-24:00 local
  at each airport (first departure banks at major U.S. hubs start ~6 AM).
  Overnight-only events, like LGA's routine 00:05-06:00 closure, don't
  affect scheduled flights.
- Driving: departure 08:00 local at the origin, constant average speed, so
  each sample point gets an estimated time of arrival (ETA).
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.models import Airport, Place, RoutePoint, Trip

OPERATING_START_HOUR = 6
DRIVE_DEPARTURE_HOUR = 8
# NWS zone alerts are coarse; treat a point as exposed for a few hours
# around its ETA so we don't miss an alert that starts just after we pass.
DRIVE_EXPOSURE_PAD = timedelta(hours=2)

Window = Tuple[datetime, datetime]  # UTC-aware [start, end)


def operating_window(d: date, tz: str, now: Optional[datetime] = None) -> Window:
    """Travel-day operating hours, minus whatever has already passed: for a
    trip today, a closure that ended at 6 AM is irrelevant at 10 AM."""
    z = ZoneInfo(tz)
    start = datetime.combine(d, time(OPERATING_START_HOUR), z).astimezone(timezone.utc)
    end = datetime.combine(d + timedelta(days=1), time(0), z).astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if start < now < end:
        start = now
    return start, end


def drive_departure(trip: Trip) -> datetime:
    z = ZoneInfo(trip.origin.timezone)
    return datetime.combine(trip.travel_date, time(DRIVE_DEPARTURE_HOUR), z).astimezone(timezone.utc)


def eta(trip: Trip, p: RoutePoint) -> datetime:
    return drive_departure(trip) + timedelta(hours=p.hours_from_start)


def drive_window(trip: Trip, p: RoutePoint) -> Window:
    t = eta(trip, p)
    return t - DRIVE_EXPOSURE_PAD, t + DRIVE_EXPOSURE_PAD


def overlap_hours(a: Window, b: Window) -> float:
    start, end = max(a[0], b[0]), min(a[1], b[1])
    return max(0.0, (end - start).total_seconds() / 3600)


def flight_airports(trip: Trip) -> List[Tuple[Airport, Place]]:
    """Every airport we check, paired with the city (and so timezone) it serves."""
    return [(a, p) for p in (trip.origin, trip.destination) for a in p.airports]


def area_of(trip: Trip, place: Place) -> str:
    return "origin" if place is trip.origin else "destination"


def local_today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def fmt_local(dt: Optional[datetime], tz: str) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %b %d %I:%M %p %Z").replace(" 0", " ")
