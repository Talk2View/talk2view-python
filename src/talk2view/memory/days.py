"""The user's days (R64). The store keeps every time in UTC, such as
``2026-10-21T10:00:00Z``, and every day it derives from one is a day in the user's
timezone: "today" for expiry, a search's date filter, and the date a hit, an entry or a
conversation goes with.

``timezone`` is an IANA name such as "Australia/Perth". The system's current local zone,
as ``datetime.astimezone()`` takes it, stands in when it is None or a name this platform
doesn't know, or when the platform has no tz data (Windows without ``tzdata``). Nothing
here raises over a timezone. Every read of the system's zone goes through one helper,
``_system_zone``, so a test can pin it on any platform.
"""

from __future__ import annotations

import calendar
import functools
import zoneinfo
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional, Tuple

_UTC = timezone.utc


def local(moment: datetime, timezone: Optional[str]) -> datetime:
    """``moment``, an aware datetime, in the user's timezone."""
    return moment.astimezone(_zone(timezone))


def _zone(timezone: Optional[str]) -> Optional[tzinfo]:
    """The zone ``timezone`` names, else the system's (``_system_zone``)."""
    named = _named_zone(timezone) if isinstance(timezone, str) else None
    return _system_zone() if named is None else named


def _system_zone() -> Optional[tzinfo]:
    """The system's current local zone. It is None, which ``astimezone`` takes for the
    system's zone, as a naive datetime is in it, so each moment takes the offset the system
    gives it, daylight saving included. Nothing else here reads the system's zone, so a test
    pins it here, on any platform (``time.tzset`` is Unix only)."""
    return None


@functools.lru_cache(maxsize=32)
def _named_zone(name: str) -> Optional[tzinfo]:
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:  # an unknown or malformed name, or no tz data: never raise
        return None


def today(moment: datetime, timezone: Optional[str]) -> date:
    """The user's date at ``moment``, an aware datetime."""
    return local(moment, timezone).date()


def day(timestamp: str, timezone: Optional[str]) -> str:
    """The user's day at ``timestamp``, a time as the store writes it, as YYYY-MM-DD. One
    that isn't such a time, or whose day can't be worked out (year 1 or 9999 in a zone that
    leaves the range), gives its first ten characters: its UTC day."""
    if len(timestamp) != 20 or not timestamp.endswith("Z"):
        return timestamp[:10]
    try:
        moment = datetime.fromisoformat(timestamp[:-1]).replace(tzinfo=_UTC)
        return local(moment, timezone).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return timestamp[:10]


def bounds(
    from_date: Optional[str], to_date: Optional[str], timezone: Optional[str]
) -> Optional[Tuple[Optional[str], Optional[str]]]:
    """A search's date filter as two times as the store writes them, ``(start, end)``: what
    was recorded or said on the user's days from ``from_date`` to ``to_date``, both included,
    is at or after ``start`` and before ``end``, the start of the day after ``to_date``. A
    date left out (None) leaves its side open, and so does one whose day starts outside what
    a datetime holds (year 1 or 9999): nothing the store keeps is there.

    The dates are YYYY-MM-DD. A day the calendar lacks, such as 2026-04-31, is read as
    comparing the text would read it, as the nearest real day on its side: the last one
    before it as a ``to_date``, the first one after it as a ``from_date``. None when there
    is no such day: nothing can match."""
    start = end = None
    if from_date is not None:
        first = _real_date(from_date, last=False)
        if first is None:
            return None
        start = _day_start(first, 0, timezone)
    if to_date is not None:
        final = _real_date(to_date, last=True)
        if final is None:
            return None
        end = _day_start(final, 1, timezone)
    return start, end


def _real_date(text: str, *, last: bool) -> Optional[date]:
    """The real date the YYYY-MM-DD ``text`` stands for: the last one on or before it
    (``last``), else the first one on or after it, as the text compares."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    year, month, mday = int(text[:4]), int(text[5:7]), int(text[8:])
    if last:
        if year == 0:
            return None
        if month > 12:
            return date(year, 12, 31)
        if month == 0 or mday == 0:  # "2026-03-00" comes after the whole of February
            year, month = (year - 1, 12) if month <= 1 else (year, month - 1)
            if year == 0:
                return None
        return date(year, month, calendar.monthrange(year, month)[1])
    if year == 0:
        return date(1, 1, 1)
    if month == 0:
        return date(year, 1, 1)
    if mday == 0 and month <= 12:
        return date(year, month, 1)
    # Past the month's last day, or month 13 or more: the next month's first day comes next.
    year, month = (year + 1, 1) if month >= 12 else (year, month + 1)
    return date(year, month, 1) if year <= 9999 else None


def _day_start(when: date, days_after: int, timezone: Optional[str]) -> Optional[str]:
    """The time, as the store writes it, at which the user's day ``days_after`` days after
    ``when`` starts, or None when that is outside what a datetime holds. A day that starts
    in a daylight-saving gap starts when the clocks jump."""
    try:
        start = when + timedelta(days=days_after)
        midnight = datetime(start.year, start.month, start.day, tzinfo=_zone(timezone))
        moment = midnight.astimezone(_UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


__all__ = ["bounds", "day", "local", "today"]
