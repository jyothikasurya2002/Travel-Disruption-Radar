"""Turn free-text places into coordinates and nearby commercial airports."""
import csv
import math
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from app.http import get_json
from app.models import Airport, Place

AIRPORTS_CSV = Path(__file__).resolve().parent.parent / "data" / "airports_us.csv"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Airports farther than this from the city centre aren't "serving" that city.
AIRPORT_RADIUS_KM = 100
MAX_AIRPORTS_PER_CITY = 3

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
    "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
    "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico",
}

# Common shorthand people actually type.
ALIASES = {
    "nyc": "New York", "sf": "San Francisco", "la": "Los Angeles",
    "dc": "Washington, DC", "philly": "Philadelphia", "vegas": "Las Vegas",
}


class LocationError(ValueError):
    """Raised when a place can't be resolved; message is shown to the user."""


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@lru_cache(maxsize=1)
def load_airports() -> List[Airport]:
    with AIRPORTS_CSV.open() as f:
        return [
            Airport(
                iata=r["iata"], icao=r["icao"], name=r["name"], city=r["city"],
                state=r["state"], is_hub=r["type"] == "large_airport",
                lat=float(r["lat"]), lon=float(r["lon"]),
            )
            for r in csv.DictReader(f)
        ]


def airport_by_code(code: str) -> Optional[Airport]:
    code = code.strip().upper()
    for a in load_airports():
        if code in (a.iata, a.icao):
            return a
    return None


def airports_near(lat: float, lon: float) -> List[Airport]:
    """If the city has hubs nearby, only those (that's where business travellers
    actually fly; e.g. skip Gary for Chicago). Otherwise fall back to the
    nearest medium airports so smaller cities still resolve."""
    nearby = []
    for a in load_airports():
        d = haversine_km(lat, lon, a.lat, a.lon)
        if d <= AIRPORT_RADIUS_KM:
            nearby.append(a.model_copy(update={"distance_km": round(d, 1)}))
    hubs = [a for a in nearby if a.is_hub]
    chosen = hubs or nearby
    chosen.sort(key=lambda a: a.distance_km)
    return chosen[:MAX_AIRPORTS_PER_CITY]


async def geocode(text: str) -> Place:
    raw = text.strip()
    if not raw:
        raise LocationError("Please enter a place.")

    # An airport code typed directly ("SFO", "KJFK") is the most precise input.
    # We still geocode its city to learn the timezone, then pin that airport.
    if raw.isalpha() and len(raw) in (3, 4) and raw.lower() not in ALIASES:
        ap = airport_by_code(raw)
        if ap:
            city = await geocode(f"{ap.city}, {ap.state}")
            return city.model_copy(update={
                "query": raw, "lat": ap.lat, "lon": ap.lon,
                "airports": [ap.model_copy(update={"distance_km": 0.0})],
            })

    expanded = ALIASES.get(raw.lower(), raw)
    city, _, state_hint = (p.strip() for p in expanded.partition(","))
    state_full = STATES.get(state_hint.upper(), state_hint) if state_hint else ""

    data = await get_json(
        GEOCODE_URL,
        {"name": city, "count": 10, "countryCode": "US", "language": "en"},
    )
    results = data.get("results") or []
    if state_full:
        results = [r for r in results if r.get("admin1", "").lower() == state_full.lower()]
    if not results:
        raise LocationError(f"Couldn't find a U.S. place called '{text}'.")

    # Exact name matches beat partial ones ("Aspen" must not become "Aspen Hill").
    # Among exact matches, the most populous wins, so "Portland" means
    # Portland, OR unless the user adds ", ME".
    exact = [r for r in results if r["name"].lower() == city.lower()]
    best = max(exact or results[:5], key=lambda r: r.get("population") or 0)
    state_code = next(
        (k for k, v in STATES.items() if v == best.get("admin1")), best.get("admin1", "")
    )
    return Place(
        query=raw,
        name=f"{best['name']}, {state_code}",
        state=state_code,
        lat=best["latitude"],
        lon=best["longitude"],
        timezone=best.get("timezone") or "America/New_York",
        airports=airports_near(best["latitude"], best["longitude"]),
    )


def nearest_town_label(lat: float, lon: float) -> str:
    """Cheap human label for a point on a road ("near Cheyenne, WY"), using the
    bundled airport list instead of another reverse-geocoding API call."""
    ap = min(load_airports(), key=lambda a: haversine_km(lat, lon, a.lat, a.lon))
    return f"near {ap.city}, {ap.state}"
