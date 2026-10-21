from datetime import datetime, timedelta, timezone

from app import risk
from app.models import Severity, Signal, SourceStatus
from app.timewin import local_today
from tests.helpers import ny_to_sf

NOW = datetime.now(timezone.utc)
OK = [SourceStatus(source=s, ok=True) for s in
      ("FAA NAS Status", "AviationWeather TAF", "NWS Alerts", "Open-Meteo Forecast")]


def sig(source, location, sev, headline="x", valid=True, area="origin"):
    return Signal(source=source, location=location, area=area, category="weather", severity=sev,
                  headline=headline, observed_at=NOW, valid_for_travel_date=valid)


def run(signals, days_out=1, sources=OK):
    trip = ny_to_sf(local_today("America/New_York") + timedelta(days=days_out))
    score, drivers = risk.score(signals)
    level, _ = risk.level_for(score)
    conf, reasons = risk.confidence(trip, signals, sources)
    action = risk.recommend(trip, level, conf, signals, sources)
    return score, level, conf, action, drivers, reasons


def test_all_clear_means_no_action():
    score, level, conf, action, drivers, _ = run([sig("NWS Alerts", "JFK", Severity.none)])
    assert (score, level, action.level, drivers) == (0, "Minimal", "none", [])


def test_single_moderate_finding_means_monitor():
    score, level, _, action, *_ = run([sig("Open-Meteo Forecast", "SFO/OAK", Severity.moderate)])
    assert (score, level, action.level) == (25, "Moderate", "monitor")


def test_independent_sources_agreeing_raise_the_score():
    signals = [
        sig("AviationWeather TAF", "JFK", Severity.high, "JFK forecast: thunderstorms"),
        sig("Open-Meteo Forecast", "LGA/JFK", Severity.moderate, "thunderstorms"),
        sig("NWS Alerts", "JFK", Severity.high, "Severe Thunderstorm Warning"),
    ]
    score, level, _, action, drivers, _ = run(signals)
    assert score == 50 + 10 + 10
    assert level == "High" and action.level == "mitigate"
    assert drivers[0].reason.startswith("Worst finding") and "Corroborated" in drivers[1].reason


def test_findings_at_different_ends_of_the_trip_dont_corroborate():
    # Regression: gusty winds at Norfolk were "corroborated" by an Atlanta alert.
    signals = [
        sig("AviationWeather TAF", "ORF", Severity.moderate, area="origin"),
        sig("NWS Alerts", "ATL", Severity.moderate, area="destination"),
    ]
    score, _, _, _, drivers, _ = run(signals)
    assert score == 25 + 3
    assert not any("Corroborated" in d.reason for d in drivers)


def test_route_findings_corroborate_only_at_the_same_point():
    flood = sig("NWS Alerts", "km 450 near Gallup, NM, km 600 near Albuquerque, NM", Severity.moderate, area="route")
    far_fog = sig("Open-Meteo Forecast", "km 0 near Phoenix, AZ", Severity.moderate, area="route")
    near_rain = sig("Open-Meteo Forecast", "km 600 near Albuquerque, NM", Severity.moderate, area="route")
    assert run([flood, far_fog])[0] == 25 + 3        # 600 km apart: no corroboration
    assert run([flood, near_rain])[0] == 25 + 10     # same point: corroborated
    assert not risk.same_place(sig("X", "km 15", 1, area="route"), sig("Y", "km 150", 1, area="route"))


def test_same_source_repeating_itself_is_not_corroboration():
    signals = [sig("Open-Meteo Forecast", f"km {i}", Severity.moderate) for i in range(5)]
    score, *_ = run(signals)
    assert score == 25 + 10  # base + breadth (capped), no corroboration bonus


def test_severe_closure_means_rebook():
    score, level, _, action, *_ = run([sig("FAA NAS Status", "SFO", Severity.severe, "SFO closed")])
    assert level == "Severe" and action.level == "rebook"


def test_context_only_signals_are_not_scored():
    score, *_ = run([sig("FAA NAS Status", "JFK", Severity.low, "JFK right now: delays", valid=False)])
    assert score == 0


def test_suggests_clear_alternate_airport():
    signals = [sig("AviationWeather TAF", "JFK", Severity.high), sig("AviationWeather TAF", "LGA", Severity.none)]
    *_, action, _, _ = run(signals)
    assert any("consider flying via LGA" in s for s in action.steps)


def test_failed_source_lowers_confidence_and_asks_for_manual_check():
    sources = OK[:1] + [SourceStatus(source="AviationWeather TAF", ok=False, error="timeout")] + OK[2:]
    _, _, conf, action, _, reasons = run([], sources=sources)
    assert conf == "medium"
    assert any("unavailable" in r for r in reasons)
    assert any("Verify manually: AviationWeather TAF" in s for s in action.steps)


def test_far_future_quiet_trip_is_not_an_all_clear():
    _, level, conf, action, _, _ = run([], days_out=10,
                                       sources=OK[:1] + [SourceStatus(source="NWS Alerts", ok=False)] + OK[2:])
    assert level == "Minimal" and conf == "low"
    assert action.level == "monitor" and "not an all-clear" in action.steps[0]
