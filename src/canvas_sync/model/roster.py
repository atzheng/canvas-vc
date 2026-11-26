"""Roster model and cache (CONTRACTS.md section 4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Roster(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    sections: list[str] = []
    groups_category: str | None = None


class RosterCache(BaseModel):
    """Untracked: .canvas/roster-cache.json."""

    students: dict[str, int] = {}  # ref -> canvas user id
    sections: dict[str, int] = {}  # name -> id
    groups: dict[str, int] = {}  # name -> id
