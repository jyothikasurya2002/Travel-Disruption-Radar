from datetime import date

import pytest

from app.dates import DateError, parse_travel_date

MONDAY = date(2026, 10, 19)


@pytest.mark.parametrize("text,expected", [
    ("today", "2026-10-19"), ("tomorrow", "2026-10-20"), ("in 3 days", "2026-10-22"),
    ("friday", "2026-10-23"), ("this friday", "2026-10-23"), ("next friday", "2026-10-30"),
    ("monday", "2026-10-19"), ("next monday", "2026-10-26"),
    ("10/24", "2026-10-24"), ("Oct 24", "2026-10-24"), ("2026-10-30", "2026-10-30"),
])
def test_parses(text, expected):
    assert parse_travel_date(text, today=MONDAY).isoformat() == expected


@pytest.mark.parametrize("text", ["2026-10-01", "2026-12-25", "someday"])
def test_rejects(text):
    with pytest.raises(DateError):
        parse_travel_date(text, today=MONDAY)
