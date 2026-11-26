"""Local <-> UTC date handling (CONTRACTS.md section 3)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

LocalDT = datetime  # always timezone-aware, in the course zone, seconds = 0


def parse_local(value: str | date | datetime, tz: ZoneInfo, default_time: time) -> LocalDT:
    raise NotImplementedError


def format_local(dt: LocalDT) -> str:
    raise NotImplementedError


def to_canvas(dt: LocalDT | None) -> str | None:
    raise NotImplementedError


def from_canvas(s: str | None, tz: ZoneInfo) -> LocalDT | None:
    raise NotImplementedError
