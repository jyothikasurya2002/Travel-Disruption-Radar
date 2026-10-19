import asyncio
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.dates import DateError, parse_travel_date
from app.geo import LocationError, geocode
from app.models import Mode
from app.route import drive_route

load_dotenv()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Travel Disruption Radar")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/resolve")
async def resolve(origin: str, destination: str, date: str = "tomorrow", mode: Mode = Mode.flight):
    """Debug view of step 2: what did we understand from the user's input?"""
    try:
        travel_date = parse_travel_date(date)
        o, d = await asyncio.gather(geocode(origin), geocode(destination))
    except (DateError, LocationError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    if mode == Mode.flight:
        for place in (o, d):
            if not place.airports:
                raise HTTPException(
                    400, f"No commercial airport within 100 km of {place.name}. Try driving mode."
                )
        route = None
    else:
        route = await drive_route(o, d)

    return {"travel_date": travel_date, "mode": mode, "origin": o, "destination": d, "route": route}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
