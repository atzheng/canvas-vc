"""Assignment/module date overrides (CONTRACTS.md section 4)."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from canvas_sync.dates import LocalDT, parse_local
from canvas_sync.errors import ValidationError

KEY_ORDER = ["section", "group", "students", "title", "unlock", "due", "lock"]
DATE_KEYS = ("unlock", "due", "lock")


class Override(BaseModel):
    """Exactly one of section, group, students is set."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    section: str | None = None
    group: str | None = None
    students: list[str] | None = None  # sorted
    title: str | None = None
    unlock: LocalDT | None = None
    due: LocalDT | None = None
    lock: LocalDT | None = None

    @field_validator("students")
    @classmethod
    def _sorted_students(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        if not v:
            raise ValidationError("override 'students' must not be empty")
        if len(set(v)) != len(v):
            dup = next(s for s in v if v.count(s) > 1)
            raise ValidationError(f"override lists student {dup!r} more than once")
        return sorted(v)

    @model_validator(mode="after")
    def _one_target(self) -> Override:
        targets = [n for n in ("section", "group", "students") if getattr(self, n) is not None]
        if len(targets) != 1:
            found = ", ".join(targets) if targets else "none"
            raise ValidationError(
                f"override must target exactly one of section, group, students (found: {found})"
            )
        return self

    def target(self) -> tuple[str, str]:
        """``("section", name)``, ``("group", name)`` or ``("students", "a,b")``."""
        if self.section is not None:
            return ("section", self.section)
        if self.group is not None:
            return ("group", self.group)
        return ("students", ",".join(self.students or []))


def _sort_key(o: Override) -> tuple[int, str, tuple[str, ...], str]:
    if o.section is not None:
        return (0, o.section, (), "")
    if o.group is not None:
        return (1, o.group, (), "")
    return (2, "", tuple(o.students or ()), o.title or "")


def sort_overrides(overrides: Iterable[Override]) -> list[Override]:
    """Canonical order: sections by name, groups by name, student sets by first student."""
    return sorted(overrides, key=_sort_key)


def validate_overrides(overrides: Iterable[Override], what: str = "overrides") -> list[Override]:
    """Canonically ordered copy; raises ValidationError if a section or group repeats."""
    ordered = sort_overrides(overrides)
    seen: set[tuple[str, str]] = set()
    for o in ordered:
        kind, name = o.target()
        if kind == "students":
            continue
        if (kind, name) in seen:
            raise ValidationError(f"{what}: {kind} {name!r} appears in more than one override")
        seen.add((kind, name))
    return ordered


def parse_override_dates(
    raw: dict[str, Any], tz: ZoneInfo, unlock_time: time, due_time: time
) -> dict[str, Any]:
    """Copy of a raw override mapping with unlock/due/lock parsed to LocalDT."""
    out = dict(raw)
    for key in DATE_KEYS:
        if out.get(key) is not None:
            out[key] = parse_local(out[key], tz, unlock_time if key == "unlock" else due_time)
    return out
