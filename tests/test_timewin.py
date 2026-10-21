from datetime import date, datetime, timezone

from app.timewin import operating_window, overlap_hours

NY = "America/New_York"
DAY = date(2026, 10, 21)


def test_future_day_covers_6am_to_midnight_local():
    start, end = operating_window(DAY, NY, now=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 21, 10, tzinfo=timezone.utc)  # 06:00 EDT
    assert end == datetime(2026, 10, 22, 4, tzinfo=timezone.utc)     # 00:00 EDT


def test_routine_overnight_closure_ending_at_6am_is_not_exposure():
    # Regression: LGA's nightly 00:05-06:00 closure was flagged High at 4:30 AM.
    window = operating_window(DAY, NY, now=datetime(2026, 10, 21, 8, 30, tzinfo=timezone.utc))
    closure = (datetime(2026, 10, 21, 4, 5, tzinfo=timezone.utc), datetime(2026, 10, 21, 10, tzinfo=timezone.utc))
    assert overlap_hours(closure, window) == 0


def test_today_starts_now_so_finished_events_dont_count():
    # Regression: at 10:32 AM, LGA's 00:05-06:00 overnight closure was flagged High.
    now = datetime(2026, 10, 21, 14, 32, tzinfo=timezone.utc)        # 10:32 AM EDT
    window = operating_window(DAY, NY, now=now)
    assert window[0] == now
    closure = (datetime(2026, 10, 21, 4, 5, tzinfo=timezone.utc), datetime(2026, 10, 21, 10, tzinfo=timezone.utc))
    assert overlap_hours(closure, window) == 0
