from datetime import date

from app.geo import airport_by_code
from app.models import Mode, Place, Trip


def place(name: str, tz: str, *codes: str) -> Place:
    aps = [airport_by_code(c) for c in codes]
    return Place(query=name, name=name, state=aps[0].state, lat=aps[0].lat, lon=aps[0].lon,
                 timezone=tz, airports=aps)


def ny_to_sf(travel_date: date) -> Trip:
    return Trip(
        origin=place("New York, NY", "America/New_York", "JFK", "LGA"),
        destination=place("San Francisco, CA", "America/Los_Angeles", "SFO"),
        travel_date=travel_date, mode=Mode.flight,
    )
