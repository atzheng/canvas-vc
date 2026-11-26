"""Assignment/module date overrides (CONTRACTS.md section 4)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from canvas_sync.dates import LocalDT

KEY_ORDER = ["section", "group", "students", "title", "unlock", "due", "lock"]


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
