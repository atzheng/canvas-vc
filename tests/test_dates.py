from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from canvas_sync.dates import format_local, from_canvas, parse_local, to_canvas
from canvas_sync.errors import ValidationError

VAN = ZoneInfo("America/Vancouver")
T = time(23, 59)


def test_default_times() -> None:
    assert format_local(parse_local("2025-01-10", VAN, time(23, 59))) == "2025-01-10 23:59"
    assert format_local(parse_local("2025-01-10", VAN, time(9, 0))) == "2025-01-10 09:00"
    assert format_local(parse_local(date(2025, 1, 10), VAN, time(9, 0))) == "2025-01-10 09:00"
    assert format_local(parse_local("2025-01-10 14:05", VAN, T)) == "2025-01-10 14:05"
    assert format_local(parse_local(datetime(2025, 1, 10, 14, 5), VAN, T)) == "2025-01-10 14:05"


def test_invalid() -> None:
    for bad in ("tomorrow", "2025-13-01", "2025-01-10 25:00"):
        with pytest.raises(ValidationError):
            parse_local(bad, VAN, T)
    with pytest.raises(ValidationError):
        from_canvas("nope", VAN)


def test_to_canvas_both_sides_of_dst() -> None:
    # PST (UTC-8) before the March 9 2025 transition, PDT (UTC-7) after.
    assert to_canvas(parse_local("2025-03-08 12:00", VAN, T)) == "2025-03-08T20:00:00Z"
    assert to_canvas(parse_local("2025-03-10 12:00", VAN, T)) == "2025-03-10T19:00:00Z"
    # PDT before the November 2 2025 transition, PST after.
    assert to_canvas(parse_local("2025-11-01 12:00", VAN, T)) == "2025-11-01T19:00:00Z"
    assert to_canvas(parse_local("2025-11-03 12:00", VAN, T)) == "2025-11-03T20:00:00Z"


def test_none() -> None:
    assert to_canvas(None) is None
    assert from_canvas(None, VAN) is None


def test_nonexistent_time_normalises() -> None:
    dt = parse_local("2025-03-09 02:30", VAN, T)
    assert format_local(dt) == "2025-03-09 03:30"


def test_ambiguous_time_is_first_occurrence() -> None:
    dt = parse_local("2025-11-02 01:30", VAN, T)
    assert to_canvas(dt) == "2025-11-02T08:30:00Z"  # PDT, fold=0


def test_roundtrip_across_dst() -> None:
    start = datetime(2025, 2, 1)
    for i in range(0, 24 * 2 * 365, 7):
        naive = start + timedelta(minutes=30 * i)
        dt = parse_local(naive, VAN, T)
        back = from_canvas(to_canvas(dt), VAN)
        assert back is not None
        assert back == dt
        assert format_local(back) == format_local(dt)
