"""Roster model and cache (CONTRACTS.md section 4)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Literal, NamedTuple

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


class TargetProblem(NamedTuple):
    message: str  # one line, starts with the path of the object involved
    severity: Literal["error", "warning"]


def unknown_ref_id(ref: str) -> int | None:
    """The Canvas id in an ``unknown:<id>`` ref (a positive integer), else None."""
    tail = ref.removeprefix("unknown:")
    if tail == ref or not tail.isascii() or not tail.isdigit() or int(tail) <= 0:
        return None
    return int(tail)


def check_override_targets(
    overrides: Iterable[Override], roster: Roster, cache: RosterCache, where: str
) -> list[TargetProblem]:
    """Problems for overrides naming sections, groups or students that are unknown.

    Sections are known if listed in ``roster.yaml`` or present in the cache; groups and
    students must be in the cache. An ``unknown:<id>`` ref with a positive integer id is
    what fetch writes for a target missing from the cache (dropped students, the Student
    View test student): it is a warning, not an error, and apply sends that id. Any other
    ``unknown:`` value is malformed and an error. ``where`` is the path of the object involved.
    """
    known_sections = set(roster.sections) | set(cache.sections)
    problems: list[TargetProblem] = []

    def check(kind: str, name: str, known: bool) -> None:
        if known:
            return
        if name.startswith("unknown:"):
            if unknown_ref_id(name) is not None:
                msg = f"{where}: {kind} {name!r} is not in the roster cache"
                problems.append(TargetProblem(msg, "warning"))
            else:
                msg = f"{where}: malformed {kind} ref {name!r} (expected unknown:<canvas id>)"
                problems.append(TargetProblem(msg, "error"))
            return
        problems.append(TargetProblem(f"{where}: unknown {kind} {name!r}", "error"))

    for o in overrides:
        if o.section is not None:
            check("section", o.section, o.section in known_sections)
        if o.group is not None:
            check("group", o.group, o.group in cache.groups)
        for s in o.students or []:
            check("student", s, s in cache.students)
    return problems
