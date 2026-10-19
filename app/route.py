"""Driving route + sample points to check weather along the road."""
from typing import List

from app.geo import haversine_km
from app.http import get_json
from app.models import DriveRoute, Place, RoutePoint

OSRM_URL = "https://router.project-osrm.org/route/v1/driving/{coords}"

# One check every ~150 km (~1.5 h of driving) is enough to catch a storm
# system without hammering the weather APIs on long cross-country routes.
SAMPLE_EVERY_KM = 150
MAX_SAMPLES = 12


async def drive_route(origin: Place, dest: Place) -> DriveRoute:
    coords = f"{origin.lon},{origin.lat};{dest.lon},{dest.lat}"
    data = await get_json(
        OSRM_URL.format(coords=coords),
        {"overview": "full", "geometries": "geojson"},
    )
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError(f"No driving route found between {origin.name} and {dest.name}.")

    route = data["routes"][0]
    distance_km = route["distance"] / 1000
    duration_h = route["duration"] / 3600
    line = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]

    spacing = max(SAMPLE_EVERY_KM, distance_km / MAX_SAMPLES)
    return DriveRoute(
        distance_km=round(distance_km, 1),
        duration_hours=round(duration_h, 1),
        samples=_sample(line, spacing, distance_km, duration_h),
    )


def _sample(line, spacing_km: float, total_km: float, total_h: float) -> List[RoutePoint]:
    """Walk the polyline, emitting a point every `spacing_km`, plus both ends.

    Time-at-point assumes constant average speed -- good enough to pick the
    right forecast hour, and OSRM doesn't model traffic anyway.
    """
    def point(lat, lon, km):
        hours = total_h * (km / total_km) if total_km else 0.0
        return RoutePoint(lat=lat, lon=lon, km_from_start=round(km), hours_from_start=round(hours, 1))

    out = [point(line[0][0], line[0][1], 0.0)]
    travelled, next_mark = 0.0, spacing_km
    for (lat1, lon1), (lat2, lon2) in zip(line, line[1:]):
        travelled += haversine_km(lat1, lon1, lat2, lon2)
        if travelled >= next_mark:
            out.append(point(lat2, lon2, travelled))
            next_mark += spacing_km
    # Always include the destination; drop a sample sitting right next to it.
    if out and total_km - out[-1].km_from_start < spacing_km / 3 and len(out) > 1:
        out.pop()
    out.append(point(line[-1][0], line[-1][1], total_km))
    return out
