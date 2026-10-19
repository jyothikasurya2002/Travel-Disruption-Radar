from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.connectors import gather_signals
from app.dates import DateError
from app.geo import LocationError
from app.models import Mode, Trip
from app.resolve import resolve_trip

load_dotenv()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Travel Disruption Radar")


async def _trip_or_400(origin: str, destination: str, date: str, mode: Mode) -> Trip:
    try:
        return await resolve_trip(origin, destination, date, mode)
    except (DateError, LocationError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/resolve")
async def resolve(origin: str, destination: str, date: str = "tomorrow", mode: Mode = Mode.flight):
    """Debug view of step 2: what did we understand from the user's input?"""
    return await _trip_or_400(origin, destination, date, mode)


@app.get("/api/signals")
async def signals(origin: str, destination: str, date: str = "tomorrow", mode: Mode = Mode.flight):
    """Debug view of step 3: raw evidence from every source, before scoring."""
    trip = await _trip_or_400(origin, destination, date, mode)
    sigs, statuses = await gather_signals(trip)
    sigs.sort(key=lambda s: -int(s.severity))
    return {"trip": trip, "sources": statuses, "signals": sigs}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
