"""Turn free-text places into coordinates and nearby commercial airports."""
import csv
import math
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Tuple

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
    "dc": "Washington, DC", "washington": "Washington, DC",
    "washington dc": "Washington, DC", "washington d.c.": "Washington, DC",
    "philly": "Philadelphia", "vegas": "Las Vegas", "nola": "New Orleans",
    "bay area": "San Francisco", "silicon valley": "San Jose",
}
# Abbreviations the geocoder doesn't expand itself ("Ft Lauderdale").
ABBREVIATIONS = {"ft": "fort", "mt": "mount", "pt": "port"}
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


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


def split_state(text: str) -> Tuple[str, str]:
    """'Chicago, IL' / 'Chicago IL' / 'Portland Oregon' -> (city, full state name).
    Without a comma, the last word(s) only count as a state if something is
    left for the city, so 'New York' and 'Kansas City' stay whole."""
    if "," in text:
        city, _, hint = (p.strip() for p in text.partition(","))
        return city, STATES.get(hint.upper().replace(".", ""), hint)
    words = text.split()
    for n in (2, 1):  # "New Mexico" before "Mexico"
        if len(words) > n:
            tail = " ".join(words[-n:])
            if n == 1 and tail.upper() in STATES:
                return " ".join(words[:-n]), STATES[tail.upper()]
            if tail.lower() in {s.lower() for s in STATES.values()}:
                return " ".join(words[:-n]), tail
    return text, ""


def normalize_name(name: str) -> str:
    """For comparing place names: 'St. Louis' == 'Saint Louis' == 'st louis'."""
    words = name.lower().replace(".", "").replace("-", " ").split()
    words = ["st" if w == "saint" else w for w in words]
    return " ".join(words)


def _expand_abbreviations(city: str) -> str:
    words = city.split()
    if words and words[0].lower().rstrip(".") in ABBREVIATIONS:
        words[0] = ABBREVIATIONS[words[0].lower().rstrip(".")]
    return " ".join(words)


async def _search(name: str) -> list:
    data = await get_json(GEOCODE_URL, {"name": name, "count": 10, "countryCode": "US", "language": "en"})
    return data.get("results") or []


async def _candidates(city: str, state_full: str) -> list:
    results = await _search(city)
    # The geocoder treats "Saint Louis" and "St. Louis" as different names, so
    # search both spellings and let the exact-match rule pick.
    first, _, rest = city.partition(" ")
    if rest and first.lower().rstrip(".") in ("saint", "st"):
        other = ("St. " if first.lower() == "saint" else "Saint ") + rest
        results += await _search(other)
    if state_full:
        results = [r for r in results if r.get("admin1", "").lower() == state_full.lower()]
    return results


async def timezone_at(lat: float, lon: float) -> str:
    data = await get_json(FORECAST_URL, {"latitude": lat, "longitude": lon,
                                         "timezone": "auto", "forecast_days": 1})
    return data["timezone"]


async def geocode(text: str) -> Place:
    raw = text.strip()
    if not raw:
        raise LocationError("Please enter a place.")
    if not any(c.isalpha() for c in raw):
        raise LocationError(f"'{raw}' isn't a place name. Enter a city or an airport code.")

    # An airport code typed directly ("SFO", "KJFK") is the most precise input.
    # Built from the airport itself: city names like "Dallas-Fort Worth"
    # don't geocode, and we already know where the airport is.
    if raw.isalpha() and len(raw) in (3, 4) and raw.lower() not in ALIASES:
        ap = airport_by_code(raw)
        if ap:
            return Place(
                query=raw, name=f"{ap.city}, {ap.state}", state=ap.state,
                lat=ap.lat, lon=ap.lon, timezone=await timezone_at(ap.lat, ap.lon),
                airports=[ap.model_copy(update={"distance_km": 0.0})],
            )

    expanded = ALIASES.get(raw.lower(), raw)
    city, state_full = split_state(expanded)
    city = _expand_abbreviations(city)
    results = await _candidates(city, state_full)

    # Without a comma, a trailing state name might really be part of the town:
    # "Port Washington", "West New York". If reading it as a state finds
    # nothing, try the whole text as the name.
    if not results and state_full and "," not in expanded:
        city, state_full = _expand_abbreviations(expanded), ""
        results = await _candidates(city, state_full)
    if not results:
        raise LocationError(f"Couldn't find a U.S. place called '{text}'.")

    # Exact name matches beat partial ones ("Aspen" must not become "Aspen Hill").
    # Among exact matches, the most populous wins, so "Portland" means
    # Portland, OR unless the user adds ", ME".
    exact = [r for r in results if normalize_name(r["name"]) == normalize_name(city)]
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
