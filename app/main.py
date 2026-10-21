from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.assess import assess
from app.connectors import gather_signals
from app.dates import DateError
from app.geo import LocationError
from app.models import Assessment, Mode, Trip, TripRequest
from app.resolve import resolve_trip

load_dotenv()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Travel Disruption Radar")


async def _trip_or_400(origin: str, destination: str, date: str, mode: Mode) -> Trip:
    try:
        return await resolve_trip(origin, destination, date, mode)
    except (DateError, LocationError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except httpx.HTTPError:
        # Geocoding / routing are needed before any assessment can start.
        raise HTTPException(
            status_code=503,
            detail="The location or routing service didn't respond. Please try again in a moment.",
        )


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/resolve")
async def resolve(origin: str, destination: str, date: str = "tomorrow", mode: Mode = Mode.flight):
    """Debug: how the input was resolved (places, airports, date, route)."""
    return await _trip_or_400(origin, destination, date, mode)


@app.get("/api/signals")
async def signals(origin: str, destination: str, date: str = "tomorrow", mode: Mode = Mode.flight):
    """Debug: raw signals from every source, before scoring."""
    trip = await _trip_or_400(origin, destination, date, mode)
    sigs, statuses = await gather_signals(trip)
    sigs.sort(key=lambda s: -int(s.severity))
    return {"trip": trip, "sources": statuses, "signals": sigs}


@app.post("/api/assess", response_model=Assessment)
async def assess_trip(req: TripRequest):
    trip = await _trip_or_400(req.origin, req.destination, req.date, req.mode)
    return await assess(trip)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
