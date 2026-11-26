"""Local <-> UTC date handling (CONTRACTS.md section 3).

Dates in the course tree are written in the course's local time zone and
converted to UTC only when talking to Canvas. All conversion goes through
``zoneinfo`` so DST is handled by the tz database.

Ambiguous and nonexistent local times
-------------------------------------
During the November "fall back" transition a local time such as 01:30 occurs
twice; we resolve it with ``fold=0``, i.e. the first occurrence (still on
daylight time). During the March "spring forward" transition a time such as
02:30 does not exist; with ``fold=0`` it is read using the offset in force
before the gap and so normalises to 03:30 (daylight time). ``parse_local``
always returns the normalised, valid local time.

Known limitation: the written form ``YYYY-MM-DD HH:MM`` does not say which
occurrence of an ambiguous time is meant, so a time fetched from the second
(standard-time) occurrence re-parses as the first and drifts by an hour. Use
``is_ambiguous_local`` to warn. Compare round trips with ``to_canvas`` strings:
datetime ``==`` ignores ``fold``.

Seconds are preserved (Canvas commonly uses 23:59:59): ``format_local`` omits
``:SS`` only when seconds are zero.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from canvas_sync.errors import ValidationError

LocalDT = datetime  # always timezone-aware, in the course zone, microseconds = 0

_FORMAT = "%Y-%m-%d %H:%M"
_FORMAT_SECONDS = "%Y-%m-%d %H:%M:%S"


def _normalise(naive: datetime, tz: ZoneInfo) -> LocalDT:
    """Attach ``tz`` (fold=0) and normalise via UTC so nonexistent times are fixed."""
    naive = naive.replace(microsecond=0, fold=0, tzinfo=None)
    return naive.replace(tzinfo=tz).astimezone(UTC).astimezone(tz)


def parse_local(value: str | date | datetime, tz: ZoneInfo, default_time: time) -> LocalDT:
    """Parse a date or datetime in course-local time.

    Accepts ``"YYYY-MM-DD"`` (uses ``default_time``), ``"YYYY-MM-DD HH:MM"``,
    a ``date`` or a ``datetime``. Naive datetimes are taken as local; aware
    ones are converted to ``tz``.
    """
    if not isinstance(value, str | date):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValidationError(f"invalid date {value!r}: expected a string or date")
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC).astimezone(tz).replace(microsecond=0)
        return _normalise(value, tz)
    if isinstance(value, date):
        return _normalise(datetime.combine(value, default_time), tz)
    text = value.strip()
    try:
        if len(text) == 10:
            return _normalise(datetime.combine(date.fromisoformat(text), default_time), tz)
        fmt = _FORMAT_SECONDS if len(text) > 16 else _FORMAT
        return _normalise(datetime.strptime(text, fmt), tz)
    except ValueError:
        raise ValidationError(
            f"invalid date {value!r}: expected YYYY-MM-DD, YYYY-MM-DD HH:MM or YYYY-MM-DD HH:MM:SS"
        ) from None


def format_local(dt: LocalDT) -> str:
    """``"YYYY-MM-DD HH:MM"``, or with ``:SS`` appended if seconds are non-zero."""
    return dt.strftime(_FORMAT if dt.second == 0 else _FORMAT_SECONDS)


def is_ambiguous_local(dt: LocalDT) -> bool:
    """True if this wall-clock time occurs twice (the fall-back hour)."""
    wall = dt.replace(fold=0)
    return wall.utcoffset() != dt.replace(fold=1).utcoffset()


def to_canvas(dt: LocalDT | None) -> str | None:
    """UTC ISO 8601 with a ``Z`` suffix, e.g. ``2025-01-10T07:59:00Z``."""
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_canvas(s: str | None, tz: ZoneInfo) -> LocalDT | None:
    """Parse a Canvas UTC timestamp into course-local time ."""
    if s is None:
        return None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"invalid Canvas timestamp {s!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(tz).replace(microsecond=0)
