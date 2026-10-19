"""Parse human travel dates ("tomorrow", "friday", "10/24") into a date.

Deliberately small and dependency-free: the brief's example is "tomorrow",
and ops staff mostly type relative days or short dates.
"""
import re
from datetime import date, datetime, timedelta
from typing import Optional

# Open-Meteo forecasts go 16 days out; beyond that any "risk" is a guess.
MAX_DAYS_AHEAD = 16

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class DateError(ValueError):
    """Raised for unparseable / out-of-range dates; message is shown to the user."""


def parse_travel_date(text: str, today: Optional[date] = None) -> date:
    today = today or date.today()
    s = text.strip().lower()

    d = _parse(s, today)
    if d is None:
        raise DateError(
            f"Couldn't understand the date '{text}'. Try 'tomorrow', 'friday', "
            "'10/24' or '2026-10-24'."
        )
    if d < today:
        raise DateError(f"{d.isoformat()} is in the past.")
    if (d - today).days > MAX_DAYS_AHEAD:
        raise DateError(
            f"{d.isoformat()} is more than {MAX_DAYS_AHEAD} days away - weather "
            "forecasts don't reach that far, so an assessment wouldn't be meaningful."
        )
    return d


def _parse(s: str, today: date) -> Optional[date]:
    if s in ("", "today", "tonight", "now"):
        return today
    if s in ("tomorrow", "tmrw", "tmr"):
        return today + timedelta(days=1)
    if s in ("day after tomorrow", "overmorrow"):
        return today + timedelta(days=2)

    m = re.fullmatch(r"in (\d{1,2}) days?", s)
    if m:
        return today + timedelta(days=int(m.group(1)))

    # Assumption: "friday" / "this friday" = the coming Friday (today if it is
    # Friday); "next friday" = the one after that. People disagree on this, so
    # the UI always echoes back the resolved date.
    m = re.fullmatch(r"(next |this )?(" + "|".join(WEEKDAYS) + r")", s)
    if m:
        delta = (WEEKDAYS.index(m.group(2)) - today.weekday()) % 7
        if m.group(1) == "next ":
            delta += 7
        return today + timedelta(days=delta)

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(s.replace(",", ""), fmt).date()
        except ValueError:
            pass

    # Year-less formats ("10/24", "oct 24"): assume the next occurrence.
    # The year is appended before parsing so Feb 29 works (and Python 3.15+
    # doesn't reject year-less strptime).
    for fmt in ("%m/%d", "%b %d", "%B %d", "%d %b", "%d %B"):
        for year in (today.year, today.year + 1):
            try:
                candidate = datetime.strptime(f"{s.replace(',', '')} {year}", f"{fmt} %Y").date()
            except ValueError:
                continue
            if candidate >= today:
                return candidate

    return None
