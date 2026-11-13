"""Read dates and times the way Shortcuts and people write them.

Accepts ISO 8601, today / yesterday / tomorrow, weekday names ("friday",
"next friday": the next one after today) and the unambiguous formats the
Shortcuts date actions produce, such as "Nov 13, 2026 at 9:00 AM" or
"13 November 2026 09:00". Numeric day/month orders like 11/12/26 are
refused because they mean different days in different regions.
"""
from __future__ import annotations

import datetime as dt
import re

RELATIVE_DAYS = {"today": 0, "yesterday": -1, "tomorrow": 1}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y",
                "%b %d %Y", "%B %d %Y"]
TIME_FORMATS = ["%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p", "%I %p"]


def _clean(value) -> str:
    # iOS puts a narrow no-break space before AM/PM.
    s = re.sub(r"[   ]", " ", str(value)).strip()
    s = re.sub(r"\s+at\s+", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*,\s+(?=\S+\s+\S)", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s)


def _relative(s: str, today: dt.date):
    s = s.lower()
    if s in RELATIVE_DAYS:
        return today + dt.timedelta(days=RELATIVE_DAYS[s])
    name = s[5:] if s.startswith("next ") else s
    if name in WEEKDAYS:
        ahead = (WEEKDAYS.index(name) - today.weekday()) % 7 or 7
        return today + dt.timedelta(days=ahead)
    return None


def parse(value, today: dt.date | None = None):
    """Returns a date when the text has no time of day, else a datetime.
    Raises ValueError when the text can't be read."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value
    s = _clean(value)
    day = _relative(s, today or dt.date.today())
    if day:
        return day
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        pass
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        pass
    for date_format in DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, date_format).date()
        except ValueError:
            pass
        for time_format in TIME_FORMATS:
            try:
                return dt.datetime.strptime(s, f"{date_format} {time_format}")
            except ValueError:
                pass
    raise ValueError(f"not a date: {value!r}")


def as_date(value, today: dt.date | None = None) -> dt.date:
    out = parse(value, today)
    return out.date() if isinstance(out, dt.datetime) else out


def as_datetime(value, today: dt.date | None = None) -> dt.datetime:
    out = parse(value, today)
    return out if isinstance(out, dt.datetime) else dt.datetime.combine(out, dt.time())
