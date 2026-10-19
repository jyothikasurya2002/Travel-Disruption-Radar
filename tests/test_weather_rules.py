from datetime import datetime, timedelta, timezone

from app.connectors import nws, openmeteo, taf
from app.models import Mode, Severity


def _sev(hazards):
    return max((int(s) for s, _ in hazards), default=0)


# --- TAF -------------------------------------------------------------------

def test_taf_thunderstorm_is_high():
    assert _sev(taf.assess_period({"wxString": "TSRA", "visib": "6+"})) == Severity.high


def test_taf_flight_categories():
    lifr = {"visib": 0.5, "clouds": [{"cover": "OVC", "base": 200}]}
    ifr = {"visib": 2, "clouds": [{"cover": "BKN", "base": 800}]}
    mvfr = {"visib": "6+", "clouds": [{"cover": "OVC", "base": 2500}]}
    vfr = {"visib": "6+", "clouds": [{"cover": "FEW", "base": 1500}, {"cover": "SCT", "base": 5000}]}
    assert _sev(taf.assess_period(lifr)) == Severity.high
    assert _sev(taf.assess_period(ifr)) == Severity.moderate
    assert _sev(taf.assess_period(mvfr)) == Severity.low
    assert _sev(taf.assess_period(vfr)) == Severity.none  # FEW/SCT layers aren't a ceiling


def test_taf_prob_group_is_downgraded():
    assert _sev(taf.assess_period({"wxString": "TSRA", "probability": 30})) == Severity.moderate


def test_taf_snow_intensity():
    assert _sev(taf.assess_period({"wxString": "+SN"})) == Severity.high
    assert _sev(taf.assess_period({"wxString": "-SN"})) == Severity.low


# --- NWS -------------------------------------------------------------------

def test_nws_filters_marine_and_agricultural():
    for event in ("Small Craft Advisory", "Gale Warning", "Frost Advisory", "Storm Warning", "Rip Current Statement"):
        assert not nws._relevant(event), event
    for event in ("Winter Storm Warning", "Dense Fog Advisory", "Wind Advisory"):
        assert nws._relevant(event), event


def test_nws_travel_floor_and_watch_cap():
    assert nws._severity({"event": "Dense Fog Advisory", "severity": "Minor"}) == Severity.moderate
    assert nws._severity({"event": "Blizzard Warning", "severity": "Severe"}) == Severity.severe
    assert nws._severity({"event": "Winter Storm Watch", "severity": "Severe"}) == Severity.moderate


def test_nws_ignores_alert_outside_exposure_window():
    now = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    window = (now, now + timedelta(hours=4))
    expired = {"features": [{"properties": {
        "id": "a1", "event": "Wind Advisory", "severity": "Moderate",
        "onset": "2026-10-19T10:00:00+00:00", "ends": "2026-10-19T22:00:00+00:00",
    }}]}
    sigs = nws.parse([expired], [("JFK", 0, 0, window, "America/New_York", "origin")])
    assert all(s.severity == Severity.none for s in sigs)


# --- Open-Meteo ------------------------------------------------------------

def test_openmeteo_hour_rules():
    calm = {"weather_code": 1, "snowfall": 0, "precipitation": 0, "wind_gusts_10m": 20, "visibility": 20000}
    assert openmeteo.assess_hour(calm, Mode.flight) == []
    assert _sev(openmeteo.assess_hour({**calm, "weather_code": 95}, Mode.flight)) == Severity.high
    assert _sev(openmeteo.assess_hour({**calm, "snowfall": 3.0}, Mode.drive)) == Severity.high
    assert _sev(openmeteo.assess_hour({**calm, "visibility": 300}, Mode.drive)) == Severity.high
    assert _sev(openmeteo.assess_hour({**calm, "wind_gusts_10m": 70}, Mode.flight)) == Severity.moderate
    # Steady rain matters on the road, not for a flight.
    assert openmeteo.assess_hour({**calm, "precipitation": 5}, Mode.flight) == []
    assert _sev(openmeteo.assess_hour({**calm, "precipitation": 5}, Mode.drive)) == Severity.low
