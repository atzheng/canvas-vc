"""Assignment model (CONTRACTS.md section 4). Stub: load and dump nothing yet."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from canvas_sync.config import CourseConfig
from canvas_sync.dates import LocalDT
from canvas_sync.model.base import CanvasObject, Kind
from canvas_sync.model.overrides import Override

KEY_ORDER = [
    "canvas_id",
    "title",
    "points",
    "group",
    "submission_types",
    "allowed_extensions",
    "grading_type",
    "omit_from_final_grade",
    "published",
    "unlock",
    "due",
    "lock",
    "assign_to_overrides_only",
    "overrides",
    "body_format",
]


class Assignment(CanvasObject):
    title: str = ""
    points: float | None = None
    group: str | None = None
    submission_types: list[str] = []
    allowed_extensions: list[str] = []
    grading_type: str = "points"
    omit_from_final_grade: bool = False
    published: bool | None = None
    unlock: LocalDT | None = None
    due: LocalDT | None = None
    lock: LocalDT | None = None
    assign_to_overrides_only: bool = False
    overrides: list[Override] = []
    body_format: Literal["md", "html"] = "md"
    body: str = ""


class _Spec:
    kind = Kind.ASSIGNMENT
    folder = "assignments"

    def load(self, root: Path, cfg: CourseConfig) -> dict[str, CanvasObject]:
        return {}

    def dump(self, objs: dict[str, CanvasObject], cfg: CourseConfig) -> dict[str, bytes]:
        return {}


ASSIGNMENT_SPEC = _Spec()
