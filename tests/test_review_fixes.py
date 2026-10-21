"""Regression tests for issues found in a full review and input stress test."""
import asyncio
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app import geo, resolve, risk, route
from app.connectors import nws, taf
from app.dates import DateError, parse_travel_date
from app.models import Mode, Severity, Signal, SourceStatus, Trip
from app.timewin import drive_departure, operating_window
from tests.helpers import ny_to_sf, place

NOW = datetime.now(timezone.utc)
MONDAY = date(2026, 10, 19)


# --- place input -----------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Chicago IL", ("Chicago", "Illinois")),
    ("chicago, il", ("chicago", "Illinois")),
    ("Austin TX", ("Austin", "Texas")),
    ("Portland Oregon", ("Portland", "Oregon")),
    ("Santa Fe New Mexico", ("Santa Fe", "New Mexico")),
    ("New York", ("New York", "")),          # a state name alone stays a city
    ("Kansas City", ("Kansas City", "")),
    ("Washington", ("Washington", "")),
])
def test_split_state(text, expected):
    assert geo.split_state(text) == expected


def test_saint_and_st_match():
    assert geo.normalize_name("Saint Louis") == geo.normalize_name("St. Louis")
    assert geo._expand_abbreviations("Ft Lauderdale") == "fort Lauderdale"


def test_saint_spelling_searches_both_forms(monkeypatch):
    # Regression: the geocoder never returns St. Louis, MO for "Saint Louis".
    async def fake_search(name):
        if name == "St. Louis":
            return [{"name": "St. Louis", "admin1": "Missouri", "population": 300000,
                     "latitude": 38.63, "longitude": -90.2, "timezone": "America/Chicago"}]
        return [{"name": "Saint Louis", "admin1": "Michigan", "population": 7000,
                 "latitude": 43.4, "longitude": -84.6, "timezone": "America/Detroit"}]
    monkeypatch.setattr(geo, "_search", fake_search)
    assert asyncio.run(geo.geocode("Saint Louis")).name == "St. Louis, MO"


def test_town_ending_in_a_state_name_falls_back_to_the_full_name(monkeypatch):
    # Regression: "Port Washington" was split into "Port" + Washington state.
    async def fake_search(name):
        if name == "Port Washington":
            return [{"name": "Port Washington", "admin1": "New York", "population": 15000,
                     "latitude": 40.83, "longitude": -73.7, "timezone": "America/New_York"}]
        return [{"name": "Port", "admin1": "Oklahoma", "population": 100,
                 "latitude": 35.0, "longitude": -98.0, "timezone": "America/Chicago"}]
    monkeypatch.setattr(geo, "_search", fake_search)
    assert asyncio.run(geo.geocode("Port Washington")).name == "Port Washington, NY"


def test_number_only_input_is_rejected_without_a_lookup():
    with pytest.raises(geo.LocationError, match="isn't a place name"):
        asyncio.run(geo.geocode("123"))


def test_airport_code_does_not_need_city_geocoding(monkeypatch):
    # Regression: DFW's city is "Dallas-Fort Worth", which the geocoder can't find.
    async def tz(lat, lon):
        return "America/Chicago"
    monkeypatch.setattr(geo, "timezone_at", tz)
    p = asyncio.run(geo.geocode("DFW"))
    assert [a.iata for a in p.airports] == ["DFW"] and p.timezone == "America/Chicago"


def test_same_origin_and_destination_is_rejected(monkeypatch):
    ny = place("New York, NY", "America/New_York", "JFK")

    async def fake_geocode(text):
        return ny
    monkeypatch.setattr(resolve, "geocode", fake_geocode)
    with pytest.raises(geo.LocationError, match="both New York"):
        asyncio.run(resolve.resolve_trip("JFK", "LGA", "tomorrow", Mode.flight))


def test_no_road_route_gives_a_clear_message(monkeypatch):
    async def no_route(url, params=None):
        req = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("400", request=req, response=httpx.Response(400, request=req))
    monkeypatch.setattr(route, "get_json", no_route)
    honolulu = place("Honolulu, HI", "Pacific/Honolulu", "HNL")
    la = place("Los Angeles, CA", "America/Los_Angeles", "LAX")
    with pytest.raises(ValueError, match="no driving route"):
        asyncio.run(route.drive_route(honolulu, la))


# --- dates -----------------------------------------------------------------

def test_calendar_limit_comes_from_the_same_constant_as_the_server():
    from fastapi.testclient import TestClient

    from app import main
    from app.dates import MAX_DAYS_AHEAD
    assert TestClient(main.app).get("/api/config").json() == {"max_days_ahead": MAX_DAYS_AHEAD}


@pytest.mark.parametrize("text,expected", [
    ("next week", "2026-10-26"), ("in 2 weeks", "2026-11-02"), ("in 1 week", "2026-10-26"),
    ("in 15 days", "2026-11-03"),
])
def test_more_date_phrases(text, expected):
    assert parse_travel_date(text, today=MONDAY).isoformat() == expected


@pytest.mark.parametrize("text,msg", [("yesterday", "in the past"), ("in 16 days", "more than 15 days")])
def test_out_of_range_dates(text, msg):
    with pytest.raises(DateError, match=msg):
        parse_travel_date(text, today=MONDAY)


# --- time windows ----------------------------------------------------------

def test_destination_already_on_next_day_uses_its_current_day():
    # 10:30 PM in LA = 1:30 AM in New York; LA's "today" is over in New York.
    now = datetime(2026, 10, 22, 5, 30, tzinfo=timezone.utc)
    start, end = operating_window(date(2026, 10, 21), "America/New_York", now=now)
    assert start > now and end > now
    assert start == datetime(2026, 10, 22, 10, tzinfo=timezone.utc)  # 6 AM EDT on the 22nd


def test_same_day_drive_after_8am_departs_now():
    trip = Trip(origin=place("Denver, CO", "America/Denver", "DEN"),
                destination=place("Salt Lake City, UT", "America/Denver", "SLC"),
                travel_date=date(2026, 10, 21), mode=Mode.drive)
    six_pm = datetime(2026, 10, 22, 0, tzinfo=timezone.utc)          # 6 PM MDT
    assert drive_departure(trip, now=six_pm) == six_pm
    early = datetime(2026, 10, 21, 10, tzinfo=timezone.utc)          # 4 AM MDT
    assert drive_departure(trip, now=early) == datetime(2026, 10, 21, 14, tzinfo=timezone.utc)


# --- TAF -------------------------------------------------------------------

def test_freezing_fog_is_not_freezing_rain():
    hazards = taf.assess_period({"wxString": "FZFG", "visib": 0.25})
    assert not any("freezing precipitation" in t for _, t in hazards)
    assert any("LIFR" in t for _, t in hazards)       # still counted, as fog
    assert any("freezing" in t for _, t in taf.assess_period({"wxString": "-FZRA"}))


def test_taf_ending_mid_day_is_flagged_as_partial():
    trip = ny_to_sf(date.today() + timedelta(days=1))
    window_start, _ = operating_window(trip.travel_date, "America/New_York")
    ends = int((window_start + timedelta(hours=3)).timestamp())
    data = [{"icaoId": "KJFK", "issueTime": NOW.isoformat(), "rawTAF": "TAF KJFK ...",
             "validTimeFrom": ends - 24 * 3600, "validTimeTo": ends,
             "fcsts": [{"timeFrom": ends - 24 * 3600, "timeTo": ends, "visib": "6+"}]}]
    jfk = trip.origin.airports[0]
    [sig] = taf.parse(data, [(jfk, trip.origin)], trip)
    assert sig.coverage_note and "only runs until" in sig.coverage_note
    assert "on travel day" not in sig.headline


# --- Open-Meteo ------------------------------------------------------------

def test_late_night_window_still_gets_the_current_hour():
    # Regression: at 11:20 PM the window 11:20-midnight contained no whole hour,
    # so the forecast was wrongly reported as out of range.
    from app.connectors import openmeteo
    start = datetime(2026, 10, 22, 3, 20, tzinfo=timezone.utc)
    window = (start, datetime(2026, 10, 22, 4, tzinfo=timezone.utc))
    hours = ["2026-10-22T02:00", "2026-10-22T03:00", "2026-10-22T04:00"]
    data = [{"hourly": {"time": hours, "precipitation": [0] * 3, "snowfall": [0] * 3,
                        "wind_gusts_10m": [10] * 3, "visibility": [20000] * 3, "weather_code": [1] * 3}}]
    [sig] = openmeteo.parse(data, [("JFK", 0, 0, window, "America/New_York", "origin")], Mode.flight)
    assert sig.valid_for_travel_date and "1 forecast hours checked" in sig.detail


# --- NWS -------------------------------------------------------------------

def test_clear_results_are_reported_per_part_of_trip():
    window = (NOW, NOW + timedelta(hours=6))
    checks = [("JFK", 0, 0, window, "America/New_York", "origin"),
              ("SFO", 0, 0, window, "America/Los_Angeles", "destination")]
    sigs = nws.parse([{"features": []}, {"features": []}], checks)
    assert {(s.area, s.location) for s in sigs} == {("origin", "JFK"), ("destination", "SFO")}


# --- risk ------------------------------------------------------------------

def _sig(source, location, sev, area="origin", note=None):
    return Signal(source=source, location=location, area=area, category="weather", severity=sev,
                  headline="x", observed_at=NOW, coverage_note=note)


def test_no_alternate_airport_when_the_storm_covers_it_too():
    # Regression: a blizzard over JFK and LGA plus a JFK closure suggested "fly via LGA".
    trip = ny_to_sf(date.today() + timedelta(days=1))
    signals = [_sig("NWS Alerts", "JFK, LGA", Severity.severe), _sig("FAA NAS Status", "JFK", Severity.high)]
    assert risk._alternate_airports(trip, signals) == []


def test_partial_coverage_lowers_confidence():
    trip = ny_to_sf(date.today() + timedelta(days=1))
    ok = [SourceStatus(source="NWS Alerts", ok=True)]
    conf, reasons = risk.confidence(trip, [_sig("NWS Alerts", "km 900", Severity.none, note="NWS alerts couldn't be checked for km 900")], ok)
    assert conf != "high" and any("couldn't be checked" in r for r in reasons)
