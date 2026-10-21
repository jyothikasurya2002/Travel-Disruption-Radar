"""Raw user input -> resolved Trip (places, airports, date, route)."""
import asyncio

from app.dates import parse_travel_date
from app.geo import LocationError, geocode
from app.models import Mode, Trip
from app.route import drive_route
from app.timewin import local_today


async def resolve_trip(origin: str, destination: str, date_text: str, mode: Mode) -> Trip:
    """Raises DateError / LocationError / ValueError with user-facing messages."""
    o, d = await asyncio.gather(geocode(origin), geocode(destination))
    # Resolve the date in the origin's timezone: "tomorrow" means tomorrow
    # where the traveller is, not where the server is.
    travel_date = parse_travel_date(date_text, today=local_today(o.timezone))

    if o.name == d.name:
        raise LocationError(f"Origin and destination are both {o.name}. Enter two different places.")

    if mode == Mode.flight:
        for place in (o, d):
            if not place.airports:
                raise LocationError(
                    f"No commercial airport within 100 km of {place.name}. Try driving mode."
                )
        return Trip(origin=o, destination=d, travel_date=travel_date, mode=mode)

    route = await drive_route(o, d)
    return Trip(origin=o, destination=d, travel_date=travel_date, mode=mode, route=route)
