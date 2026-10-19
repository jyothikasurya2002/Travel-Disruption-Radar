"""Shared data contracts.

Every connector returns a list of Signals in the same shape, so the risk
engine and UI never need to know which source a piece of evidence came from.
"""
from datetime import date, datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Mode(str, Enum):
    flight = "flight"
    drive = "drive"


class Severity(int, Enum):
    none = 0
    low = 1
    moderate = 2
    high = 3
    severe = 4


class Airport(BaseModel):
    iata: str                        # "JFK" - used by FAA NAS status
    icao: str                        # "KJFK" - used by aviation weather (TAF/METAR)
    name: str
    city: str
    state: str
    is_hub: bool
    lat: float
    lon: float
    distance_km: Optional[float] = None


class Place(BaseModel):
    query: str                       # what the user typed
    name: str                        # resolved, e.g. "New York, NY"
    state: str
    lat: float
    lon: float
    timezone: str                    # IANA, e.g. "America/New_York"
    airports: List[Airport] = []


class RoutePoint(BaseModel):
    lat: float
    lon: float
    km_from_start: float
    hours_from_start: float


class DriveRoute(BaseModel):
    distance_km: float
    duration_hours: float
    samples: List[RoutePoint]        # points along the road we check weather at


class Trip(BaseModel):
    """A fully resolved trip: what every connector receives."""

    origin: Place
    destination: Place
    travel_date: date
    mode: Mode
    route: Optional[DriveRoute] = None


class Signal(BaseModel):
    """One piece of evidence from one external source."""

    source: str                      # e.g. "FAA NAS Status"
    location: str                    # e.g. "JFK" or "I-80 near Cheyenne, WY"
    category: str                    # weather | airport_ops | road | alert
    severity: Severity
    headline: str                    # one-line human-readable finding
    detail: Optional[str] = None     # raw-ish evidence (TAF text, alert body…)
    url: Optional[str] = None        # link back to the source
    observed_at: datetime            # when the source published it
    valid_for_travel_date: bool = True


class SourceStatus(BaseModel):
    """Whether each source answered. Missing data is itself information."""

    source: str
    ok: bool
    error: Optional[str] = None
    latency_ms: Optional[int] = None


class TripRequest(BaseModel):
    origin: str = Field(..., examples=["New York"])
    destination: str = Field(..., examples=["San Francisco"])
    travel_date: date
    mode: Mode = Mode.flight


class Assessment(BaseModel):
    request: TripRequest
    overall_severity: Severity
    score: int                       # 0-100
    recommended_action: str
    summary: str
    summary_source: str              # "llm" | "rules"
    signals: List[Signal]
    sources: List[SourceStatus]
    generated_at: datetime
