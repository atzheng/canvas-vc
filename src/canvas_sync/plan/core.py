"""Operation, Plan, Risk and ordering (CONTRACTS.md section 9)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from rich.console import RenderableType

from canvas_sync.model.base import CanvasObject, ObjectKey
from canvas_sync.model.course import Course


class Action(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    UNPUBLISH = "unpublish"
    UPLOAD = "upload"
    REORDER = "reorder"


class RiskKind(StrEnum):
    HAS_SUBMISSIONS = "has_submissions"
    GRADING_CHANGE = "grading_change"
    LATE_POLICY_REGRADE = "late_policy_regrade"
    ACCESS_REMOVED = "access_removed"
    POSTED_ANNOUNCEMENT = "posted_announcement"
    FILE_ID_CHANGE = "file_id_change"
    NOTIFIES_STUDENTS = "notifies_students"  # announcement posted now: needs --confirm-notify


@dataclass(frozen=True)
class Risk:
    kind: RiskKind
    message: str
    needs_confirm: str | None  # flag name required to proceed, e.g. "--confirm-grading-change"


@dataclass(frozen=True)
class Operation:
    key: ObjectKey
    action: Action
    before: CanvasObject | None
    after: CanvasObject | None
    changes: dict[str, tuple[Any, Any]]  # field -> (old, new), for display
    risks: tuple[Risk, ...] = ()


@dataclass
class Plan:
    ops: list[Operation]  # in apply order

    def render(self) -> RenderableType:
        raise NotImplementedError

    def is_empty(self) -> bool:
        raise NotImplementedError


def diff(base: Course, target: Course) -> Plan:
    """base = canvas/main tree, target = HEAD tree."""
    raise NotImplementedError
