"""FAA parsing against the element structure in nasstatus.faa.gov/AirportStatus.dtd.

The live feed is often quiet, so these fixtures exercise ground stops, delay
programs, delays and closures deterministically.
"""
from datetime import timedelta

from app.connectors import faa
from app.models import Severity
from app.timewin import local_today
from tests.helpers import ny_to_sf


def _notam(d, start_hhmm_utc, end_d, end_hhmm_utc):
    return f"{d:%y%m%d}{start_hhmm_utc}-{end_d:%y%m%d}{end_hhmm_utc}"


def _feed(today):
    tomorrow = today + timedelta(days=1)
    return f"""<AIRPORT_STATUS_INFORMATION>
  <Update_Time>Mon Oct 19 15:00:00 2026 GMT</Update_Time>
  <Dtd_File>http://www.fly.faa.gov/AirportStatus.dtd</Dtd_File>
  <Delay_type><Name>Ground Stops</Name><Ground_Stop_List>
    <Program><ARPT>JFK</ARPT><Reason>thunderstorms</Reason><End_Time>8:45 pm EDT.</End_Time></Program>
  </Ground_Stop_List></Delay_type>
  <Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List>
    <Ground_Delay><ARPT>SFO</ARPT><Reason>low ceilings</Reason><Avg>1 hour and 10 minutes</Avg><Max>2 hours and 5 minutes</Max></Ground_Delay>
  </Ground_Delay_List></Delay_type>
  <Delay_type><Name>General Arrival/Departure Delay Info</Name><Arrival_Departure_Delay_List>
    <Delay><ARPT>LGA</ARPT><Reason>VOL:Volume</Reason>
      <Arrival_Departure Type="Departure"><Min>31 minutes</Min><Max>45 minutes</Max><Trend>Increasing</Trend></Arrival_Departure>
    </Delay>
    <Delay><ARPT>ORD</ARPT><Reason>WX</Reason>
      <Arrival_Departure Type="Arrival"><Min>1 hour</Min><Max>2 hours</Max><Trend>Steady</Trend></Arrival_Departure>
    </Delay>
  </Arrival_Departure_Delay_List></Delay_type>
  <Delay_type><Name>Airport Closures</Name><Airport_Closure_List>
    <Airport><ARPT>JFK</ARPT><Reason>!JFK AD AP CLSD TO NON SKED TRANSIENT GA ACFT {_notam(today, '0000', tomorrow + timedelta(days=90), '0000')}</Reason><Start>x</Start><Reopen>y</Reopen></Airport>
    <Airport><ARPT>LGA</ARPT><Reason>!LGA AD AP CLSD EXC HEL {_notam(today, '0418', today, '0900')}</Reason><Start>x</Start><Reopen>y</Reopen></Airport>
    <Airport><ARPT>SFO</ARPT><Reason>!SFO AD AP CLSD {_notam(tomorrow, '1600', tomorrow, '2200')}</Reason><Start>x</Start><Reopen>y</Reopen></Airport>
  </Airport_Closure_List></Delay_type>
</AIRPORT_STATUS_INFORMATION>"""


def _by(signals, location, min_sev=Severity.low):
    return [s for s in signals if s.location == location and s.severity >= min_sev]


def test_live_delays_count_fully_when_travelling_today():
    today = local_today("America/New_York")
    sigs = faa.parse(_feed(today), ny_to_sf(today))

    jfk = _by(sigs, "JFK")
    assert len(jfk) == 1 and jfk[0].severity == Severity.high and "ground stop" in jfk[0].headline
    assert _by(sigs, "SFO")[0].severity == Severity.high          # GDP avg 70 min
    assert _by(sigs, "LGA")[0].severity == Severity.moderate      # max 45 min departures
    assert not _by(sigs, "ORD", Severity.none)                    # not on this route


def test_live_delays_are_context_only_for_future_trips():
    today = local_today("America/New_York")
    sigs = faa.parse(_feed(today), ny_to_sf(today + timedelta(days=2)))
    live = [s for s in sigs if "right now" in s.headline]
    assert live and all(s.severity <= Severity.low and not s.valid_for_travel_date for s in live)


def test_closures_filter_ga_only_and_overnight_but_catch_daytime():
    today = local_today("America/New_York")
    sigs = faa.parse(_feed(today), ny_to_sf(today + timedelta(days=1)))
    closures = [s for s in sigs if "closed" in s.headline]
    # JFK GA-only restriction and LGA overnight closure are ignored;
    # SFO 16:00-22:00 UTC = 09:00-15:00 PDT is 6 h of operating time.
    assert [s.location for s in closures] == ["SFO"]
    assert closures[0].severity == Severity.severe


def test_parse_minutes():
    assert faa.parse_minutes("1 hour and 3 minutes") == 63
    assert faa.parse_minutes("45 minutes") == 45
    assert faa.parse_minutes("2 hours") == 120
    assert faa.parse_minutes(None) == 0
