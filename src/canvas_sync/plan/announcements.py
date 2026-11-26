"""Differ for announcements (CONTRACTS.md section 9)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from canvas_sync.model.course import Course
    from canvas_sync.plan.core import Operation


def diff_type(base: Course, target: Course) -> list[Operation]:
    raise NotImplementedError
