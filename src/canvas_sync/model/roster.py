"""Roster model and cache (CONTRACTS.md section 4)."""

from __future__ import annotations

import json
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, field_validator

from canvas_sync.errors import ValidationError
from canvas_sync.model.overrides import Override


class Roster(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sections: list[str] = []
    groups_category: str | None = None

    @field_validator("sections")
    @classmethod
    def _unique_sections(cls, v: list[str]) -> list[str]:
        dup = [s for s in v if v.count(s) > 1]
        if dup:
            raise ValidationError(f"roster.yaml: section {dup[0]!r} is listed more than once")
        return v


class RosterCache(BaseModel):
    """Untracked: .canvas/roster-cache.json."""

    students: dict[str, int] = {}  # ref -> canvas user id
    sections: dict[str, int] = {}  # name -> id
    groups: dict[str, int] = {}  # name -> id

    def ref_for(self, user_id: int) -> str | None:
        """The student ref for a Canvas user id, or None if that user is not in the cache."""
        for ref, uid in self.students.items():
            if uid == user_id:
                return ref
        return None

    def to_json(self) -> str:
        """Deterministic JSON (sorted keys, trailing newline)."""
        return json.dumps(self.model_dump(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RosterCache:
        try:
            return cls.model_validate_json(text)
        except ValueError as e:
            first = str(e).splitlines()[0]
            raise ValidationError(f"roster-cache.json: {first}") from None


def check_override_targets(
    overrides: Iterable[Override], roster: Roster, cache: RosterCache, where: str
) -> list[str]:
    """One-line problems for overrides naming sections, groups or students that are unknown.

    Sections are known if listed in ``roster.yaml`` or present in the cache; groups and
    students must be in the cache. ``where`` is the path of the object involved.
    """
    known_sections = set(roster.sections) | set(cache.sections)
    problems: list[str] = []
    for o in overrides:
        if o.section is not None and o.section not in known_sections:
            problems.append(f"{where}: unknown section {o.section!r}")
        if o.group is not None and o.group not in cache.groups:
            problems.append(f"{where}: unknown group {o.group!r}")
        for s in o.students or []:
            if s not in cache.students:
                problems.append(f"{where}: unknown student {s!r}")
    return problems
